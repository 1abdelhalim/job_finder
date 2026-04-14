"""Pipeline orchestrator — background daemon for automated job application.

Runs the full loop: scrape → match → customize CV → cover letter → form answers → email.
Can run as a one-shot or as a daemon on a 2-day interval.
"""

import json
import logging
import os
import signal
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import yaml
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

from models import Job, JobBoard, SearchQuery
from scrapers import SCRAPERS
from matcher import JobMatcher
from storage import (
    save_jobs, get_db, get_top_jobs,
    create_application, update_application, get_application_by_job,
    start_pipeline_run, finish_pipeline_run,
    start_ingestion_run, finish_ingestion_run,
    get_new_jobs_since, get_last_email_sent,
)
from cv_customizer import customize_cv_for_job, LIFE_STORY_PATH
from cover_letter import create_cover_letter
from form_answers import generate_form_answers
from notifier import send_digest_email, send_empty_digest_email, should_send_digest

logger = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).parent / "profile.yaml"
LOCATION_AGNOSTIC_BOARDS = {"remotive", "arbeitnow", "himalayas", "greenhouse", "lever", "linkedin_posts", "internet"}

# Graceful shutdown flag
_shutdown = False


def _signal_handler(signum, frame):
    global _shutdown
    logger.info("Shutdown signal received. Finishing current cycle...")
    _shutdown = True


def load_profile() -> dict:
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def _resolve_ingestion_source(explicit: Optional[str]) -> str:
    """How the pipeline was triggered (stored on ingestion_runs)."""
    if explicit:
        return explicit
    if os.environ.get("GITHUB_ACTIONS", "").lower() == "true":
        return "github_actions"
    return "local"


def _scrape_all(profile: dict, max_per_query: int = 50) -> List[Job]:
    """Scrape all configured boards. Returns deduplicated job list."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    search = profile.get("search", {})
    board_names = search.get("boards", ["remotive", "linkedin", "indeed", "arbeitnow"])
    boards = []
    for b in board_names:
        try:
            boards.append(JobBoard(b))
        except ValueError:
            pass

    queries = []
    for kw in search.get("queries", ["machine learning engineer"]):
        for loc in search.get("locations", [""]):
            queries.append(SearchQuery(
                keywords=kw, location=loc,
                remote=search.get("remote", False),
                max_age_days=search.get("max_age_days", 14),
                boards=boards,
            ))

    all_jobs = []
    seen_combos = set()
    futures = []

    with ThreadPoolExecutor(max_workers=8) as pool:
        for query in queries:
            for board in query.boards:
                board_name = board.value
                if board_name in LOCATION_AGNOSTIC_BOARDS:
                    combo = (board_name, query.keywords)
                    if combo in seen_combos:
                        continue
                    seen_combos.add(combo)

                scraper_cls = SCRAPERS.get(board_name)
                if not scraper_cls:
                    continue
                fut = pool.submit(_scrape_one, scraper_cls, query, max_per_query)
                futures.append(fut)

        for fut in as_completed(futures):
            try:
                all_jobs.extend(fut.result())
            except Exception as e:
                logger.error("Scraper error: %s", e)

    # Deduplicate
    seen_urls = set()
    seen_fp = set()
    unique = []
    for j in all_jobs:
        fp = f"{j.title.lower().strip()}|{j.company.lower().strip()}"
        if j.url not in seen_urls and fp not in seen_fp:
            seen_urls.add(j.url)
            seen_fp.add(fp)
            unique.append(j)

    return unique


def _scrape_one(scraper_cls, query, max_results):
    scraper = scraper_cls()
    return scraper.scrape(query, max_results=max_results)


def run_pipeline(
    profile: Optional[dict] = None,
    dry_run: bool = False,
    max_applications: int = 10,
    threshold: float = 0.5,
    model: str = "qwen3.5:9b",
    skip_applications: bool = False,
    force_digest: bool = False,
    ingestion_source: Optional[str] = None,
) -> Dict:
    """Run one full pipeline cycle.

    Returns dict with stats: jobs_scraped, jobs_matched, applications_created, emails_sent.

    If skip_applications is True (e.g. GitHub Actions without Ollama), only scrape, match,
    save jobs, and send digest — no CV/cover letter/form generation.
    """
    if profile is None:
        profile = load_profile()

    pipeline_config = profile.get("pipeline", {})
    threshold = pipeline_config.get("auto_apply_threshold", threshold)
    max_applications = pipeline_config.get("max_applications_per_run", max_applications)
    model = pipeline_config.get("ollama_model", model)
    recipient = pipeline_config.get("email_recipient", "ahmed.tawfik96@gmail.com")
    interval_days = pipeline_config.get("email_digest_interval_days", 2)
    skip_applications = bool(
        skip_applications or pipeline_config.get("skip_applications", False)
    )
    force_digest = bool(
        force_digest
        or pipeline_config.get("force_digest", False)
        or os.environ.get("FORCE_DIGEST", "").strip().lower() in ("1", "true", "yes")
    )
    # Digest email: only jobs at or above this match score (0–1). Independent of auto_apply_threshold.
    _dms = os.environ.get("DIGEST_MIN_SCORE", "").strip()
    digest_min_score = (
        float(_dms)
        if _dms
        else float(pipeline_config.get("digest_min_score", 0.75))
    )

    run_id = start_pipeline_run()
    src = _resolve_ingestion_source(ingestion_source)
    ig_id = start_ingestion_run(src, kind="pipeline")
    n_saved = 0
    ranked_total = 0
    stats = {
        "jobs_scraped": 0,
        "jobs_matched": 0,
        "applications_created": 0,
        "emails_sent": 0,
    }
    log_lines = []

    try:
        # --- Step 1: Scrape ---
        logger.info("=== Pipeline Step 1: Scraping ===")
        if not dry_run:
            jobs = _scrape_all(profile)
            matcher = JobMatcher(profile)
            ranked = matcher.rank(jobs)
            n_saved = save_jobs(ranked)
            ranked_total = len(ranked)
            stats["jobs_scraped"] = len(ranked)
            log_lines.append(f"Scraped {len(ranked)} jobs, {n_saved} new")
            logger.info("Scraped %d jobs, %d new saved", len(ranked), n_saved)
        else:
            logger.info("[DRY RUN] Would scrape jobs")

        # --- Step 2: Get top matches for application ---
        logger.info("=== Pipeline Step 2: Selecting top matches ===")
        candidates = []
        if not skip_applications:
            top_jobs = get_top_jobs(limit=max_applications * 2, min_score=threshold)
            for job in top_jobs:
                existing = get_application_by_job(job["url"])
                if not existing and job.get("description"):
                    candidates.append(job)
                if len(candidates) >= max_applications:
                    break
            stats["jobs_matched"] = len(candidates)
            logger.info("Found %d jobs to process (score >= %.2f)", len(candidates), threshold)
        else:
            stats["jobs_matched"] = 0
            logger.info("Skipping CV/application generation (--skip-applications)")

        # --- Step 3: Generate applications ---
        logger.info("=== Pipeline Step 3: Generating applications ===")
        life_story = ""
        if LIFE_STORY_PATH.exists():
            life_story = LIFE_STORY_PATH.read_text(encoding="utf-8")

        if skip_applications:
            log_lines.append("Skipped application generation (CI / email-only mode)")
        for i, job in enumerate(candidates):
            if _shutdown:
                logger.info("Shutdown requested, stopping pipeline")
                break

            logger.info(
                "Processing %d/%d: %s at %s (score: %.2f)",
                i + 1, len(candidates),
                job["title"], job["company"], job["match_score"],
            )

            if dry_run:
                logger.info("[DRY RUN] Would generate application")
                continue

            try:
                # Generate customized CV
                cv_result = customize_cv_for_job(
                    job_url=job["url"],
                    title=job["title"],
                    company=job["company"],
                    location=job.get("location", ""),
                    description=job.get("description", ""),
                    model=model,
                )

                if not cv_result:
                    log_lines.append(f"FAILED CV: {job['title']} at {job['company']}")
                    continue

                # Create application record
                app_id = create_application(job["url"], cv_result["slug"])
                update_application(
                    app_id,
                    status="cv_generated",
                    cv_pdf_path=cv_result["cv_pdf_path"],
                )

                # Generate cover letter
                from cv_customizer import analyze_job
                job_analysis = analyze_job(
                    job.get("description", ""),
                    job["title"],
                    job["company"],
                    model=model,
                )

                cl_path = create_cover_letter(
                    app_dir=cv_result["app_dir"],
                    title=job["title"],
                    company=job["company"],
                    location=job.get("location", ""),
                    description=job.get("description", ""),
                    life_story=life_story,
                    job_analysis=job_analysis,
                    model=model,
                )

                if cl_path:
                    update_application(
                        app_id,
                        status="letter_generated",
                        cover_letter_pdf_path=cl_path,
                    )

                # Generate form answers
                answers = generate_form_answers(
                    life_story=life_story,
                    title=job["title"],
                    company=job["company"],
                    description=job.get("description", ""),
                    job_analysis=job_analysis,
                    model=model,
                )

                if answers:
                    update_application(
                        app_id,
                        status="ready",
                        form_answers_json=json.dumps(answers),
                    )

                stats["applications_created"] += 1
                log_lines.append(f"OK: {job['title']} at {job['company']}")
                logger.info("Application ready: %s", cv_result["slug"])

            except Exception as e:
                logger.error("Failed to process job: %s", e)
                log_lines.append(f"ERROR: {job['title']} at {job['company']}: {e}")

        # --- Step 4: Send email digest ---
        logger.info("=== Pipeline Step 4: Email digest ===")
        digest_due = force_digest or should_send_digest(interval_days)
        if digest_due and not dry_run:
            last_email = get_last_email_sent()
            since = last_email["sent_at"] if last_email else "2000-01-01T00:00:00"
            new_jobs = get_new_jobs_since(since, min_score=digest_min_score)

            if new_jobs:
                success = send_digest_email(
                    new_jobs, recipient, min_score=digest_min_score
                )
                if success:
                    stats["emails_sent"] = 1
                    log_lines.append(
                        f"Email sent: {len(new_jobs)} jobs (match ≥ {digest_min_score:.0%})"
                    )
            else:
                logger.info(
                    "No jobs for digest (none new since last email with match ≥ %.2f)",
                    digest_min_score,
                )
                notify_empty = os.environ.get(
                    "DIGEST_NOTIFY_ON_EMPTY", ""
                ).strip().lower() in ("1", "true", "yes")
                if notify_empty and force_digest:
                    if send_empty_digest_email(
                        recipient, min_score=digest_min_score
                    ):
                        stats["emails_sent"] = 1
                        log_lines.append(
                            "Empty digest email sent (no new jobs above digest threshold)"
                        )
        else:
            if dry_run:
                logger.info("Digest skipped (dry run)")
            elif not digest_due:
                logger.info("Digest not due yet (interval %s days); use --force-digest or FORCE_DIGEST=1", interval_days)
            else:
                logger.info("Digest skipped")

        # --- Done ---
        finish_pipeline_run(
            run_id,
            status="completed",
            log="\n".join(log_lines),
            **stats,
        )
        finish_ingestion_run(
            ig_id,
            status="completed",
            jobs_new=n_saved,
            jobs_seen=ranked_total,
        )
        logger.info("Pipeline complete: %s", stats)

    except Exception as e:
        logger.error("Pipeline failed: %s", e)
        finish_pipeline_run(run_id, status="failed", log=str(e), **stats)
        finish_ingestion_run(
            ig_id,
            status="failed",
            jobs_new=n_saved,
            jobs_seen=ranked_total,
            error=str(e),
        )

    return stats


def run_daemon(interval_hours: float = 48.0):
    """Run pipeline in a loop. Default: every 48 hours (2 days)."""
    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    interval_seconds = interval_hours * 3600
    logger.info(
        "Starting pipeline daemon (interval: %.1f hours). Press Ctrl+C to stop.",
        interval_hours,
    )

    while not _shutdown:
        logger.info("=== Starting pipeline cycle at %s ===", datetime.now().isoformat())
        try:
            run_pipeline()
        except Exception as e:
            logger.error("Pipeline cycle failed: %s", e)

        if _shutdown:
            break

        logger.info("Next cycle in %.1f hours. Sleeping...", interval_hours)
        # Sleep in small increments to allow graceful shutdown
        for _ in range(int(interval_seconds)):
            if _shutdown:
                break
            time.sleep(1)

    logger.info("Daemon stopped.")
