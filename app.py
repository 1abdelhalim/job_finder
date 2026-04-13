"""Flask web UI for AI Apply."""

import json
import logging
import sqlite3
import threading
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode

import yaml
from dotenv import load_dotenv
from flask import Flask, render_template, request, jsonify, redirect, url_for

load_dotenv(Path(__file__).parent / ".env")

from models import Job, JobBoard, SearchQuery
from scrapers import SCRAPERS
from matcher import JobMatcher
from ui_config import DEFAULT_UI, get_ui_config
from storage import (
    get_db, save_jobs, update_scores, get_top_jobs, zero_scores_for_jobs_not_in,
    mark_applied, mark_hidden, DB_PATH,
    get_applications, get_application_by_job,
    get_pipeline_runs,
    start_ingestion_run, finish_ingestion_run,
    get_last_successful_ingestion, get_recent_ingestion_runs,
    create_application, update_application, delete_application,
)


def _app_error_from_row(application: Optional[dict]) -> Optional[str]:
    """Human-readable error when application generation failed."""
    if not application or application.get("status") != "failed":
        return None
    raw = application.get("form_answers_json") or "{}"
    try:
        data = json.loads(raw)
        if isinstance(data, dict) and "__error__" in data:
            return str(data["__error__"])
    except (json.JSONDecodeError, TypeError):
        pass
    return "Generation failed. Check the server log for details."


def _short_ts(iso_val) -> Optional[str]:
    """Format scraped_at / ISO-ish string for dashboard display."""
    if not iso_val:
        return None
    s = str(iso_val).strip()
    if len(s) >= 16:
        return s[:16].replace("T", " · ")
    return s


INGESTION_SOURCE_LABELS = {
    "ui": "Web UI",
    "github_actions": "GitHub Actions",
    "cli": "CLI",
    "local": "Local pipeline",
}


def _ingestion_label(source: Optional[str]) -> str:
    if not source:
        return ""
    return INGESTION_SOURCE_LABELS.get(source, source.replace("_", " ").title())

logger = logging.getLogger(__name__)
CONFIG_PATH = Path(__file__).parent / "profile.yaml"


def load_profile() -> dict:
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def create_app():
    app = Flask(__name__, template_folder="templates", static_folder="static")

    @app.context_processor
    def inject_ui_config():
        try:
            return {"ui": get_ui_config(load_profile())}
        except Exception:
            return {"ui": get_ui_config({})}

    @app.route("/")
    def dashboard():
        """Main dashboard showing stats and top jobs."""
        profile = load_profile()
        ui = get_ui_config(profile)
        enabled_boards = profile.get("search", {}).get("boards") or list(SCRAPERS.keys())
        rp = ui["kpi_review_pct"] / 100.0
        sp = ui["kpi_strong_pct"] / 100.0

        conn = get_db()
        total = conn.execute("SELECT COUNT(*) FROM jobs WHERE hidden = 0").fetchone()[0]
        applied = conn.execute("SELECT COUNT(*) FROM jobs WHERE applied = 1").fetchone()[0]
        avg_score = conn.execute("SELECT AVG(match_score) FROM jobs WHERE hidden = 0").fetchone()[0] or 0
        hidden_n = conn.execute("SELECT COUNT(*) FROM jobs WHERE hidden = 1").fetchone()[0]
        high_match = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE hidden = 0 AND match_score >= ?", (sp,)
        ).fetchone()[0]
        to_review = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE hidden = 0 AND applied = 0 AND match_score >= ?",
            (rp,),
        ).fetchone()[0]
        jobs_max_scraped = conn.execute("SELECT MAX(scraped_at) FROM jobs").fetchone()[0]

        # Board distribution
        boards = conn.execute(
            "SELECT board, COUNT(*) as cnt FROM jobs WHERE hidden = 0 GROUP BY board ORDER BY cnt DESC"
        ).fetchall()

        # Top jobs
        top = conn.execute(
            "SELECT * FROM jobs WHERE hidden = 0 ORDER BY match_score DESC, CASE WHEN date_posted IS NULL OR date_posted = '' OR LOWER(date_posted) IN ('nan','nat','none','null') THEN 0 ELSE 1 END DESC, date_posted DESC LIMIT 20"
        ).fetchall()

        conn.close()

        last_ing = get_last_successful_ingestion()
        if last_ing and last_ing.get("finished_at"):
            last_scraped_raw = last_ing["finished_at"]
            last_ingest_source = last_ing.get("source")
        else:
            last_scraped_raw = jobs_max_scraped
            last_ingest_source = None

        jobs = []
        for r in top:
            j = dict(r)
            j["match_details"] = json.loads(j.get("match_details", "{}"))
            jobs.append(j)

        search_queries = profile.get("search", {}).get("queries") or []
        q_limit = ui.get("dashboard_search_queries_limit", 8)

        return render_template(
            "dashboard.html",
            total=total,
            applied=applied,
            avg_score=round(avg_score, 3),
            hidden_n=hidden_n,
            high_match=high_match,
            to_review=to_review,
            last_scraped_display=_short_ts(last_scraped_raw),
            last_ingest_source=last_ingest_source,
            last_ingest_source_label=_ingestion_label(last_ingest_source),
            boards=[dict(b) for b in boards],
            jobs=jobs,
            enabled_boards=enabled_boards,
            search_queries=search_queries[:q_limit],
            browse_min_pct=ui["browse_min_pct"],
        )

    @app.route("/jobs")
    def jobs_list():
        """Paginated, filterable job list."""
        page = int(request.args.get("page", 1))
        per_page = 25
        offset = (page - 1) * per_page
        board_filter = request.args.get("board", "")
        country_filter = request.args.get("country", "")
        min_score_raw = float(request.args.get("min_score", 0))
        # Accept both 0-1 range and 0-100 percentage
        min_score = min_score_raw / 100.0 if min_score_raw > 1 else min_score_raw
        search = request.args.get("q", "")
        sort = request.args.get("sort", "score")  # score, date, company
        hide_applied = request.args.get("hide_applied", "") == "1"
        applied_only = request.args.get("applied_only", "") == "1"

        conn = get_db()
        where = ["hidden = 0"]
        params = []
        if applied_only:
            where.append("applied = 1")
        elif hide_applied:
            where.append("applied = 0")
        if board_filter:
            where.append("board = ?")
            params.append(board_filter)
        if country_filter:
            where.append("location LIKE ?")
            params.append(f"%{country_filter}%")
        if min_score > 0:
            where.append("match_score >= ?")
            params.append(min_score)
        if search:
            where.append("(title LIKE ? OR company LIKE ? OR description LIKE ?)")
            params.extend([f"%{search}%"] * 3)

        where_sql = " AND ".join(where)

        # CASE pushes blank/NaN/NaT dates to the bottom regardless of sort direction
        _valid_date = "CASE WHEN date_posted IS NULL OR date_posted = '' OR LOWER(date_posted) IN ('nan','nat','none','null') THEN 0 ELSE 1 END"
        order_map = {
            "score": f"match_score DESC, {_valid_date} DESC, date_posted DESC",
            "date": f"{_valid_date} DESC, date_posted DESC, match_score DESC",
            "company": f"company ASC, match_score DESC",
            "title": f"title ASC, match_score DESC",
        }
        order_sql = order_map.get(sort, f"match_score DESC, {_valid_date} DESC, date_posted DESC")

        total = conn.execute(f"SELECT COUNT(*) FROM jobs WHERE {where_sql}", params).fetchone()[0]
        rows = conn.execute(
            f"SELECT * FROM jobs WHERE {where_sql} ORDER BY {order_sql} LIMIT ? OFFSET ?",
            params + [per_page, offset]
        ).fetchall()

        # Get distinct countries from locations for the filter dropdown
        country_rows = conn.execute(
            "SELECT DISTINCT location FROM jobs WHERE hidden = 0 AND location != '' ORDER BY location"
        ).fetchall()
        conn.close()

        # Extract country-like values from locations
        countries = set()
        for r in country_rows:
            loc = r["location"]
            # Take the last part after comma as likely country
            parts = [p.strip() for p in loc.split(",")]
            if parts:
                countries.add(parts[-1])
        countries = sorted(countries)

        jobs = []
        for r in rows:
            j = dict(r)
            j["match_details"] = json.loads(j.get("match_details", "{}"))
            jobs.append(j)

        total_pages = (total + per_page - 1) // per_page
        if total_pages < 1:
            total_pages = 1

        qargs = request.args.to_dict()
        qargs["page"] = str(max(1, page - 1))
        prev_url = "/jobs?" + urlencode(qargs, doseq=True) if page > 1 else None
        qargs["page"] = str(page + 1)
        next_url = "/jobs?" + urlencode(qargs, doseq=True) if page < total_pages else None

        return render_template(
            "jobs.html",
            jobs=jobs,
            page=page,
            total_pages=total_pages,
            total=total,
            board_filter=board_filter,
            country_filter=country_filter,
            min_score=min_score,
            search=search,
            sort=sort,
            hide_applied=hide_applied,
            applied_only=applied_only,
            prev_url=prev_url,
            next_url=next_url,
            boards=[b.value for b in JobBoard],
            countries=countries,
        )

    @app.route("/job")
    def job_detail():
        """Show single job details."""
        url = request.args.get("url", "")
        if not url:
            return "Missing job URL", 400
        conn = get_db()
        row = conn.execute("SELECT * FROM jobs WHERE url = ?", (url,)).fetchone()
        conn.close()
        if not row:
            return "Job not found", 404
        job = dict(row)
        job["match_details"] = json.loads(job.get("match_details", "{}"))
        application = get_application_by_job(url)
        form_answers = {}
        if application:
            try:
                form_answers = json.loads(application.get("form_answers_json", "{}"))
            except (json.JSONDecodeError, TypeError):
                pass
        return render_template(
            "job_detail.html",
            job=job,
            application=application,
            form_answers=form_answers,
            generation_error=_app_error_from_row(application),
        )

    @app.route("/settings")
    def settings():
        """Settings page — profile-driven; lists load via API."""
        return render_template("settings.html")

    @app.route("/api/scrape", methods=["POST"])
    def api_scrape():
        """Trigger a scrape via the API."""
        data = request.json or {}
        boards = data.get("boards", [])
        max_results = data.get("max_results", 30)
        keywords = data.get("keywords", "")
        excluded_boards = set(data.get("excluded_boards", []))

        def run_scrape():
            ig_id = start_ingestion_run("ui", kind="scrape")
            try:
                _run_scrape_inner(ig_id)
            except Exception as e:
                logger.error("Background scrape failed: %s", e)
                finish_ingestion_run(
                    ig_id, status="failed", error=str(e), jobs_new=0, jobs_seen=0
                )

        def _run_scrape_inner(ig_id: int):
            profile = load_profile()
            matcher = JobMatcher(profile)
            all_jobs = []

            excluded_countries_lc = [c.lower() for c in data.get("excluded_countries", [])]
            if not excluded_countries_lc:
                excluded_countries_lc = [
                    c.lower() for c in profile.get("search", {}).get("excluded_countries", [])
                ]

            if keywords:
                queries = [SearchQuery(
                    keywords=keywords,
                    location=data.get("location", ""),
                    remote=data.get("remote", True),
                    max_age_days=14,
                    boards=[JobBoard(b) for b in boards] if boards else [
                        JobBoard(b) for b in profile.get("search", {}).get("boards", ["remotive"])
                    ],
                )]
            else:
                search = profile.get("search", {})
                board_list = [JobBoard(b) for b in boards] if boards else [
                    JobBoard(b) for b in search.get("boards", ["remotive"])
                ]
                queries = []
                for kw in search.get("queries", ["machine learning engineer"])[:3]:
                    for loc in search.get("locations", [""])[:3]:
                        queries.append(SearchQuery(
                            keywords=kw, location=loc, remote=search.get("remote", True),
                            max_age_days=search.get("max_age_days", 14), boards=board_list,
                        ))

            for query in queries:
                for board in query.boards:
                    # Skip excluded boards (legacy client override)
                    if board.value in excluded_boards:
                        continue
                    scraper_cls = SCRAPERS.get(board.value)
                    if not scraper_cls:
                        continue
                    try:
                        scraper = scraper_cls()
                        jobs = scraper.scrape(query, max_results=max_results)
                        all_jobs.extend(jobs)
                    except Exception as e:
                        logger.error(f"Scrape error ({board.value}): {e}")

            # Deduplicate by URL and title+company fingerprint
            seen_urls = set()
            seen_fingerprints = set()
            unique = []
            for j in all_jobs:
                fp = f"{j.title.lower().strip()}|{j.company.lower().strip()}"
                if j.url not in seen_urls and fp not in seen_fingerprints:
                    seen_urls.add(j.url)
                    seen_fingerprints.add(fp)
                    unique.append(j)

            # Filter out jobs from excluded countries
            if excluded_countries_lc:
                unique = [j for j in unique
                          if not any(c in j.location.lower() for c in excluded_countries_lc)]

            ranked = matcher.rank(unique)
            inserted = save_jobs(ranked)
            finish_ingestion_run(
                ig_id,
                status="completed",
                jobs_new=inserted,
                jobs_seen=len(ranked),
            )

        thread = threading.Thread(target=run_scrape)
        thread.start()

        return jsonify({"status": "started", "message": "Scraping in background..."})

    @app.route("/api/rescore", methods=["POST"])
    def api_rescore():
        """Re-score all jobs."""
        profile = load_profile()
        matcher = JobMatcher(profile)
        conn = get_db()
        rows = conn.execute("SELECT * FROM jobs WHERE hidden = 0").fetchall()
        conn.close()

        jobs = []
        for r in rows:
            jobs.append(Job(
                title=r["title"], company=r["company"], location=r["location"],
                url=r["url"], board=JobBoard(r["board"]),
                description=r["description"] or "", salary=r["salary"] or "",
            ))
        ranked = matcher.rank(jobs)
        update_scores(ranked)
        cleared = zero_scores_for_jobs_not_in({j.url for j in ranked})
        return jsonify({"status": "ok", "rescored": len(ranked), "cleared_stale": cleared})

    @app.route("/api/job/apply", methods=["POST"])
    def api_apply():
        data = request.get_json(silent=True) or {}
        url = (data.get("url") or "").strip()
        if url:
            mark_applied(url)
        return jsonify({"status": "ok"})

    @app.route("/api/job/hide", methods=["POST"])
    def api_hide():
        data = request.get_json(silent=True) or {}
        url = (data.get("url") or "").strip()
        if url:
            mark_hidden(url)
        return jsonify({"status": "ok"})

    @app.route("/api/hide_by_countries", methods=["POST"])
    def api_hide_by_countries():
        """Hide all jobs from specified countries."""
        data = request.json or {}
        countries = [c.lower() for c in data.get("countries", [])]
        if not countries:
            return jsonify({"status": "ok", "hidden": 0})
        conn = get_db()
        hidden_count = 0
        for country in countries:
            result = conn.execute(
                "UPDATE jobs SET hidden = 1 WHERE hidden = 0 AND LOWER(location) LIKE ?",
                (f"%{country}%",)
            )
            hidden_count += result.rowcount
        conn.commit()
        conn.close()
        return jsonify({"status": "ok", "hidden": hidden_count})

    @app.route("/api/stats")
    def api_stats():
        conn = get_db()
        total = conn.execute("SELECT COUNT(*) FROM jobs WHERE hidden = 0").fetchone()[0]
        applied = conn.execute("SELECT COUNT(*) FROM jobs WHERE applied = 1").fetchone()[0]
        by_board = conn.execute(
            "SELECT board, COUNT(*) as cnt FROM jobs WHERE hidden = 0 GROUP BY board"
        ).fetchall()
        by_score = conn.execute(
            "SELECT CASE WHEN match_score >= 0.7 THEN 'excellent' "
            "WHEN match_score >= 0.4 THEN 'good' "
            "WHEN match_score >= 0.2 THEN 'fair' "
            "ELSE 'low' END as tier, COUNT(*) as cnt "
            "FROM jobs WHERE hidden = 0 GROUP BY tier"
        ).fetchall()
        conn.close()
        return jsonify({
            "total": total, "applied": applied,
            "by_board": {r["board"]: r["cnt"] for r in by_board},
            "by_score": {r["tier"]: r["cnt"] for r in by_score},
        })

    # --- New routes for automation pipeline ---

    @app.route("/applications")
    def applications_page():
        apps = get_applications(limit=100)
        return render_template("applications.html", applications=apps)

    @app.route("/pipeline")
    def pipeline_page():
        runs = get_pipeline_runs(limit=500)
        apps = get_applications()
        total_applications = len(apps)
        total_emails = sum(r.get("emails_sent", 0) for r in runs)
        total_runs = len(runs)
        # Email enabled flag stored in a simple file
        email_flag = Path(__file__).parent / ".email_enabled"
        email_enabled = email_flag.exists()
        ingestion_rows = []
        for r in get_recent_ingestion_runs(40):
            d = dict(r)
            d["source_label"] = _ingestion_label(d.get("source"))
            ingestion_rows.append(d)
        return render_template(
            "pipeline.html",
            total_runs=total_runs,
            total_applications=total_applications,
            total_emails=total_emails,
            email_enabled=email_enabled,
            ingestion_runs=ingestion_rows,
        )

    @app.route("/download")
    def download_file():
        """Serve a generated PDF file."""
        from flask import send_file
        filepath = request.args.get("path", "")
        if not filepath or not Path(filepath).exists():
            return "File not found", 404
        return send_file(filepath, as_attachment=True)

    @app.route("/api/application-status")
    def api_application_status():
        """Poll generation progress for a job URL."""
        url = request.args.get("url", "")
        if not url:
            return jsonify({"status": "error", "error": "url required"}), 400
        app_row = get_application_by_job(url)
        if not app_row:
            return jsonify({"status": "ok", "app_status": "none", "error": None})
        err = _app_error_from_row(app_row) if app_row.get("status") == "failed" else None
        return jsonify(
            {
                "status": "ok",
                "app_status": app_row.get("status", "unknown"),
                "error": err,
            }
        )

    @app.route("/api/generate-application", methods=["POST"])
    def api_generate_application():
        """Generate customized CV + cover letter for a job (runs in background thread)."""
        from llm import check_ollama_available
        from cv_customizer import application_slug, resolve_cv_dir, resolve_life_story_path

        data = request.get_json(silent=True) or {}
        url = (data.get("url") or "").strip()
        if not url:
            return jsonify({"status": "error", "error": "URL required"})

        conn = get_db()
        row = conn.execute("SELECT * FROM jobs WHERE url = ?", (url,)).fetchone()
        conn.close()
        if not row:
            return jsonify({"status": "error", "error": "Job not found"})

        job = dict(row)
        profile = load_profile()

        existing = get_application_by_job(job["url"])
        if existing:
            st = existing.get("status") or ""
            if st == "generating":
                return jsonify(
                    {
                        "status": "error",
                        "error": "Generation already in progress. Wait and refresh this page, or try again in a minute.",
                    }
                )
            if st in ("ready", "letter_generated", "cv_generated"):
                return jsonify(
                    {
                        "status": "error",
                        "error": "Application already generated. Use the links on this page or open Applications.",
                    }
                )
            if st == "failed":
                delete_application(existing["id"])

        if not check_ollama_available():
            return jsonify(
                {
                    "status": "error",
                    "error": (
                        "Ollama is not usable at the configured URL: nothing responds with Ollama's "
                        "generation API. Run `ollama serve` (or start the Ollama app), then "
                        "`ollama pull` your pipeline.ollama_model. If `ollama serve` says 'address "
                        "already in use', Ollama may already be running — run `ollama pull <model>` "
                        "(your models list was empty). If another program uses port 11434, run "
                        "`lsof -iTCP:11434 -sTCP:LISTEN` or set OLLAMA_BASE in .env to your Ollama URL."
                    ),
                }
            )

        cv_dir = resolve_cv_dir(profile)
        life_path = resolve_life_story_path(cv_dir)
        try:
            if not life_path.exists() or not life_path.read_text(encoding="utf-8").strip():
                return jsonify(
                    {
                        "status": "error",
                        "error": f"life-story.md is missing or empty. Expected at: {life_path}",
                    }
                )
        except OSError as e:
            return jsonify({"status": "error", "error": f"Cannot read life-story: {e}"})

        slug = application_slug(job.get("company") or "", job.get("title") or "")
        try:
            app_id = create_application(job["url"], slug, status="generating")
        except sqlite3.IntegrityError:
            return jsonify(
                {
                    "status": "error",
                    "error": "An application record already exists for this job. Refresh the page.",
                }
            )

        def generate():
            try:
                from cv_customizer import customize_cv_for_job, analyze_job, LIFE_STORY_PATH
                from cover_letter import create_cover_letter
                from form_answers import generate_form_answers as gen_answers

                profile_inner = load_profile()
                model_inner = profile_inner.get("pipeline", {}).get("ollama_model", "qwen3.5:9b")

                result = customize_cv_for_job(
                    job_url=job["url"],
                    title=job["title"],
                    company=job["company"],
                    location=job.get("location", ""),
                    description=job.get("description", ""),
                    model=model_inner,
                    profile=profile_inner,
                )
                if not result:
                    update_application(
                        app_id,
                        status="failed",
                        form_answers_json=json.dumps(
                            {
                                "__error__": "CV or PDF step failed (Ollama, LaTeX/pdflatex, or life-story). Check the server log."
                            }
                        ),
                    )
                    return

                update_application(app_id, status="cv_generated", cv_pdf_path=result["cv_pdf_path"])

                life_story = (
                    LIFE_STORY_PATH.read_text(encoding="utf-8")
                    if LIFE_STORY_PATH.exists()
                    else ""
                )
                job_analysis = analyze_job(
                    job.get("description", ""),
                    job["title"],
                    job["company"],
                    model=model_inner,
                )

                cl_path = create_cover_letter(
                    app_dir=result["app_dir"],
                    title=job["title"],
                    company=job["company"],
                    location=job.get("location", ""),
                    description=job.get("description", ""),
                    life_story=life_story,
                    job_analysis=job_analysis,
                    model=model_inner,
                )
                if cl_path:
                    update_application(app_id, status="letter_generated", cover_letter_pdf_path=cl_path)

                answers = gen_answers(
                    life_story=life_story,
                    title=job["title"],
                    company=job["company"],
                    description=job.get("description", ""),
                    job_analysis=job_analysis,
                    model=model_inner,
                )
                if answers:
                    update_application(app_id, status="ready", form_answers_json=json.dumps(answers))
                elif cl_path:
                    update_application(app_id, status="ready")

                logger.info("Application generated for %s at %s", job["title"], job["company"])
            except Exception as e:
                logger.error("Application generation failed: %s", e)
                try:
                    update_application(
                        app_id,
                        status="failed",
                        form_answers_json=json.dumps({"__error__": str(e)}),
                    )
                except Exception:
                    pass

        thread = threading.Thread(target=generate)
        thread.start()
        return jsonify(
            {
                "status": "ok",
                "message": "Generation started. This usually takes 1–3 minutes.",
                "poll": True,
            }
        )

    def _fetch_and_score(url: str, location: str = "") -> dict:
        """Fetch a job URL, extract description, score against profile. Returns score dict."""
        import requests as req
        from bs4 import BeautifulSoup

        _DESC_SELECTORS = [
            "div[class*='job-description']", "div[id*='job-description']",
            "div[class*='description']", "div[id*='description']",
            "section[class*='description']", "div[class*='job-detail']",
            "article", "main",
        ]
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
        }
        try:
            resp = req.get(url, headers=headers, timeout=20, allow_redirects=True)
            resp.raise_for_status()
        except Exception as e:
            return {"error": f"Could not fetch URL: {e}"}

        soup = BeautifulSoup(resp.text, "html.parser")

        # Title
        og_title = soup.find("meta", property="og:title")
        title = (
            og_title["content"].strip()
            if og_title and og_title.get("content")
            else (soup.find("title").get_text(strip=True) if soup.find("title") else url)
        )

        # Description
        desc = ""
        for sel in _DESC_SELECTORS:
            el = soup.select_one(sel)
            if el and len(el.get_text(strip=True)) > 200:
                desc = el.get_text(separator="\n", strip=True)
                break
        if not desc:
            for tag in soup(["nav", "header", "footer", "script", "style"]):
                tag.decompose()
            desc = soup.get_text(separator="\n", strip=True)

        desc = desc[:5000]

        job = Job(
            title=title, company="", location=location,
            url=url, board=JobBoard.LINKEDIN, description=desc,
        )
        profile = load_profile()
        matcher = JobMatcher(profile)
        score, details = matcher.score(job)
        job.match_score = score
        job.match_details = details

        return {
            "title": title,
            "description_length": len(desc),
            "match_score": round(score, 3),
            "details": details,
            "_job": job,
        }

    @app.route("/api/score-url", methods=["POST"])
    def api_score_url():
        """Fetch a job URL and return its similarity score without saving."""
        data = request.json or {}
        url = data.get("url", "")
        if not url:
            return jsonify({"status": "error", "error": "URL required"})

        result = _fetch_and_score(url, location=data.get("location", ""))
        if "error" in result:
            return jsonify({"status": "error", "error": result["error"]})

        return jsonify({
            "status": "ok",
            "title": result["title"],
            "description_length": result["description_length"],
            "match_score": result["match_score"],
            "details": result["details"],
        })

    @app.route("/api/add-job", methods=["POST"])
    def api_add_job():
        """Manually add a job. If only URL is provided, auto-fetches the page."""
        data = request.json or {}
        url = data.get("url", "")
        if not url:
            return jsonify({"status": "error", "error": "URL required"})

        description = data.get("description", "").strip()

        if not description:
            # Auto-fetch mode: pull page and extract info
            result = _fetch_and_score(url, location=data.get("location", ""))
            if "error" in result:
                return jsonify({"status": "error", "error": result["error"]})
            job = result["_job"]
            # Override title/company if caller provided them
            if data.get("title"):
                job.title = data["title"]
            if data.get("company"):
                job.company = data["company"]
            if data.get("location"):
                job.location = data["location"]
        else:
            title = data.get("title", "Unknown Position")
            company = data.get("company", "Unknown Company")
            location = data.get("location", "")
            job = Job(
                title=title, company=company, location=location,
                url=url, board=JobBoard.LINKEDIN, description=description,
            )
            profile = load_profile()
            matcher = JobMatcher(profile)
            matcher.score(job)
            ranked = matcher.rank([job])
            job = ranked[0] if ranked else job

        n_saved = save_jobs([job])
        score = job.match_score or 0
        details = job.match_details or {}

        return jsonify({
            "status": "ok",
            "saved": n_saved,
            "title": job.title,
            "match_score": round(score, 3),
            "details": details,
            "message": f"Job added with {score:.0%} match score",
        })

    @app.route("/api/run-pipeline", methods=["POST"])
    def api_run_pipeline():
        """Trigger a full pipeline run in background."""
        data = request.json or {}
        dry_run = data.get("dry_run", False)

        def run():
            try:
                from pipeline import run_pipeline
                profile = load_profile()
                run_pipeline(profile=profile, dry_run=dry_run, ingestion_source="ui")
            except Exception as e:
                logger.error("Pipeline failed: %s", e)

        thread = threading.Thread(target=run)
        thread.start()
        return jsonify({"status": "ok", "message": "Pipeline started in background"})

    # --- Profile management APIs ---

    def _save_profile(profile: dict):
        with open(CONFIG_PATH, "w") as f:
            yaml.dump(profile, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

    @app.route("/api/profile/queries", methods=["GET"])
    def api_get_queries():
        profile = load_profile()
        return jsonify({"queries": profile.get("search", {}).get("queries", [])})

    @app.route("/api/profile/queries", methods=["POST"])
    def api_add_query():
        data = request.json or {}
        query = data.get("query", "").strip()
        if not query:
            return jsonify({"status": "error", "error": "Query required"})
        profile = load_profile()
        queries = profile.setdefault("search", {}).setdefault("queries", [])
        if query not in queries:
            queries.append(query)
            _save_profile(profile)
        return jsonify({"status": "ok", "queries": queries})

    @app.route("/api/profile/queries", methods=["DELETE"])
    def api_delete_query():
        data = request.json or {}
        query = data.get("query", "")
        profile = load_profile()
        queries = profile.get("search", {}).get("queries", [])
        queries = [q for q in queries if q != query]
        profile["search"]["queries"] = queries
        _save_profile(profile)
        return jsonify({"status": "ok", "queries": queries})

    @app.route("/api/profile/skills", methods=["GET"])
    def api_get_skills():
        profile = load_profile()
        return jsonify({"skills": profile.get("skills", [])})

    @app.route("/api/profile/skills", methods=["POST"])
    def api_add_skill():
        data = request.json or {}
        skill = data.get("skill", "").strip()
        if not skill:
            return jsonify({"status": "error", "error": "Skill required"})
        profile = load_profile()
        skills = profile.setdefault("skills", [])
        if skill not in skills:
            skills.append(skill)
            _save_profile(profile)
        return jsonify({"status": "ok", "skills": skills})

    @app.route("/api/profile/skills", methods=["DELETE"])
    def api_delete_skill():
        data = request.json or {}
        skill = data.get("skill", "")
        profile = load_profile()
        skills = [s for s in profile.get("skills", []) if s != skill]
        profile["skills"] = skills
        _save_profile(profile)
        return jsonify({"status": "ok", "skills": skills})

    @app.route("/api/life-story", methods=["GET"])
    def api_get_life_story():
        from cv_customizer import resolve_life_story_path, _DEFAULT_CV_DIR
        life_story_path = resolve_life_story_path(_DEFAULT_CV_DIR)
        text = life_story_path.read_text(encoding="utf-8") if life_story_path.exists() else ""
        return jsonify({"text": text, "path": str(life_story_path)})

    @app.route("/api/life-story", methods=["POST"])
    def api_save_life_story():
        from cv_customizer import resolve_life_story_path, _DEFAULT_CV_DIR
        data = request.json or {}
        text = data.get("text", "")
        life_story_path = resolve_life_story_path(_DEFAULT_CV_DIR)
        life_story_path.parent.mkdir(parents=True, exist_ok=True)
        life_story_path.write_text(text, encoding="utf-8")
        return jsonify({"status": "ok", "path": str(life_story_path)})

    @app.route("/api/reset-search", methods=["POST"])
    def api_reset_search():
        """Archive the current DB and start fresh."""
        from datetime import datetime as _dt
        if DB_PATH.exists():
            archive = DB_PATH.with_name(f"jobs_archive_{_dt.now().strftime('%Y%m%d_%H%M%S')}.db")
            DB_PATH.rename(archive)
        # get_db creates tables on first connect
        conn = get_db()
        conn.close()
        return jsonify({"status": "ok", "message": "Search reset. Old data archived."})

    @app.route("/api/toggle-emails", methods=["POST"])
    def api_toggle_emails():
        """Toggle email notifications on/off."""
        flag_file = Path(__file__).parent / ".email_enabled"
        if flag_file.exists():
            flag_file.unlink()
            enabled = False
        else:
            flag_file.touch()
            enabled = True
        return jsonify({"status": "ok", "enabled": enabled})

    @app.route("/api/profile/ui", methods=["GET", "POST"])
    def api_profile_ui():
        """Read or update dashboard / nav copy and score thresholds (profile.ui)."""
        profile = load_profile()
        if request.method == "GET":
            return jsonify({"status": "ok", "ui": get_ui_config(profile)})
        patch = request.json or {}
        raw_ui = profile.setdefault("ui", {})
        for key in DEFAULT_UI:
            if key not in patch:
                continue
            val = patch[key]
            if key == "quick_links":
                if isinstance(val, list):
                    raw_ui[key] = val
                continue
            if key in (
                "kpi_review_pct",
                "kpi_strong_pct",
                "browse_min_pct",
                "nav_top_matches_pct",
                "dashboard_search_queries_limit",
            ):
                try:
                    raw_ui[key] = int(val)
                except (TypeError, ValueError):
                    pass
                continue
            if isinstance(val, str) or val is None:
                raw_ui[key] = val if val is not None else ""
        _save_profile(profile)
        return jsonify({"status": "ok", "ui": get_ui_config(load_profile())})

    @app.route("/api/profile/locations", methods=["GET", "POST"])
    def api_profile_locations():
        """Search / scrape locations and preferred locations (kept in sync)."""
        profile = load_profile()
        if request.method == "GET":
            locs = profile.get("search", {}).get("locations")
            if not locs:
                locs = profile.get("preferred_locations", [])
            return jsonify({"status": "ok", "locations": locs or []})
        data = request.json or {}
        locs = data.get("locations")
        if not isinstance(locs, list):
            return jsonify({"status": "error", "error": "locations must be a list"}), 400
        locs = [str(x).strip() for x in locs if str(x).strip()]
        profile.setdefault("search", {})["locations"] = locs
        profile["preferred_locations"] = locs
        _save_profile(profile)
        return jsonify({"status": "ok", "locations": locs})

    @app.route("/api/profile/boards", methods=["GET", "POST"])
    def api_profile_boards():
        """Enabled job boards for scraping (profile.search.boards)."""
        profile = load_profile()
        all_vals = [b.value for b in JobBoard]
        if request.method == "GET":
            enabled = profile.get("search", {}).get("boards") or []
            return jsonify({"status": "ok", "enabled": enabled, "all": all_vals})
        data = request.json or {}
        boards = data.get("boards")
        if not isinstance(boards, list):
            return jsonify({"status": "error", "error": "boards must be a list"}), 400
        cleaned = []
        for b in boards:
            s = str(b).strip().lower()
            if s in all_vals and s not in cleaned:
                cleaned.append(s)
        profile.setdefault("search", {})["boards"] = cleaned
        _save_profile(profile)
        return jsonify({"status": "ok", "enabled": cleaned})

    @app.route("/api/profile/excluded-countries", methods=["GET", "POST"])
    def api_profile_excluded_countries():
        """Countries to filter out when scraping (profile.search.excluded_countries)."""
        profile = load_profile()
        if request.method == "GET":
            countries = profile.get("search", {}).get("excluded_countries", [])
            return jsonify({"status": "ok", "countries": countries or []})
        data = request.json or {}
        countries = data.get("countries")
        if not isinstance(countries, list):
            return jsonify({"status": "error", "error": "countries must be a list"}), 400
        countries = [str(x).strip() for x in countries if str(x).strip()]
        profile.setdefault("search", {})["excluded_countries"] = countries
        _save_profile(profile)
        return jsonify({"status": "ok", "countries": countries})

    @app.route("/api/profile/pipeline", methods=["GET", "POST"])
    def api_profile_pipeline():
        """Pipeline + related search knobs stored in profile.yaml."""
        profile = load_profile()
        if request.method == "GET":
            pipe = profile.get("pipeline", {})
            search = profile.get("search", {})
            return jsonify(
                {
                    "status": "ok",
                    "ollama_model": pipe.get("ollama_model", "qwen3.5:9b"),
                    "email_recipient": pipe.get("email_recipient", ""),
                    "email_digest_interval_days": pipe.get("email_digest_interval_days", 2),
                    "auto_apply_threshold": pipe.get("auto_apply_threshold", 0.45),
                    "max_applications_per_run": pipe.get("max_applications_per_run", 10),
                    "cv_dir": pipe.get("cv_dir", ""),
                    "max_age_days": search.get("max_age_days", 14),
                    "remote": search.get("remote", True),
                }
            )
        data = request.json or {}
        pipe = profile.setdefault("pipeline", {})
        search = profile.setdefault("search", {})
        if "ollama_model" in data:
            pipe["ollama_model"] = str(data["ollama_model"] or "").strip()
        if "email_recipient" in data:
            pipe["email_recipient"] = str(data["email_recipient"] or "").strip()
        if "email_digest_interval_days" in data:
            try:
                pipe["email_digest_interval_days"] = max(1, int(data["email_digest_interval_days"]))
            except (TypeError, ValueError):
                pass
        if "auto_apply_threshold" in data:
            try:
                pipe["auto_apply_threshold"] = float(data["auto_apply_threshold"])
            except (TypeError, ValueError):
                pass
        if "max_applications_per_run" in data:
            try:
                pipe["max_applications_per_run"] = max(0, int(data["max_applications_per_run"]))
            except (TypeError, ValueError):
                pass
        if "cv_dir" in data:
            pipe["cv_dir"] = str(data["cv_dir"] or "").strip()
        if "max_age_days" in data:
            try:
                search["max_age_days"] = max(1, int(data["max_age_days"]))
            except (TypeError, ValueError):
                pass
        if "remote" in data:
            search["remote"] = bool(data["remote"])
        _save_profile(profile)
        return jsonify({"status": "ok"})

    @app.route("/api/profile/titles", methods=["GET", "POST", "DELETE"])
    def api_profile_titles():
        """Target job titles for matching (profile.titles)."""
        profile = load_profile()
        if request.method == "GET":
            return jsonify({"titles": profile.get("titles") or []})
        data = request.json or {}
        title = data.get("title", "").strip()
        titles = list(profile.get("titles") or [])
        if request.method == "POST":
            if not title:
                return jsonify({"status": "error", "error": "title required"})
            if title not in titles:
                titles.append(title)
            profile["titles"] = titles
            _save_profile(profile)
            return jsonify({"status": "ok", "titles": titles})
        if request.method == "DELETE":
            rm = data.get("title", "")
            profile["titles"] = [t for t in titles if t != rm]
            _save_profile(profile)
            return jsonify({"status": "ok", "titles": profile["titles"]})
        return jsonify({"status": "error"}), 400

    @app.route("/api/profile/keywords", methods=["GET", "POST", "DELETE"])
    def api_profile_keywords():
        """Matcher keywords (profile.keywords)."""
        profile = load_profile()
        if request.method == "GET":
            return jsonify({"keywords": profile.get("keywords") or []})
        data = request.json or {}
        kw = data.get("keyword", "").strip()
        keywords = list(profile.get("keywords") or [])
        if request.method == "POST":
            if not kw:
                return jsonify({"status": "error", "error": "keyword required"})
            if kw not in keywords:
                keywords.append(kw)
            profile["keywords"] = keywords
            _save_profile(profile)
            return jsonify({"status": "ok", "keywords": keywords})
        if request.method == "DELETE":
            rm = data.get("keyword", "")
            profile["keywords"] = [k for k in keywords if k != rm]
            _save_profile(profile)
            return jsonify({"status": "ok", "keywords": profile["keywords"]})
        return jsonify({"status": "error"}), 400

    @app.route("/api/form-answers/<path:url>")
    def api_form_answers(url):
        """Get pre-generated form answers for a job."""
        app_record = get_application_by_job(url)
        if not app_record:
            return jsonify({"status": "error", "error": "No application found"})
        try:
            answers = json.loads(app_record.get("form_answers_json", "{}"))
        except json.JSONDecodeError:
            answers = {}
        return jsonify({"status": "ok", "answers": answers})

    return app
