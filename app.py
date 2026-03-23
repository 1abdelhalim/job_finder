"""Flask web UI for AI Apply."""

import json
import logging
import threading
from pathlib import Path

import yaml
from dotenv import load_dotenv
from flask import Flask, render_template, request, jsonify, redirect, url_for

load_dotenv(Path(__file__).parent / ".env")

from models import Job, JobBoard, SearchQuery
from scrapers import SCRAPERS
from matcher import JobMatcher
from storage import (
    get_db, save_jobs, update_scores, get_top_jobs,
    mark_applied, mark_hidden, DB_PATH,
    get_applications, get_application_by_job,
    get_pipeline_runs,
)

logger = logging.getLogger(__name__)
CONFIG_PATH = Path(__file__).parent / "profile.yaml"


def load_profile() -> dict:
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def create_app():
    app = Flask(__name__, template_folder="templates", static_folder="static")

    @app.route("/")
    def dashboard():
        """Main dashboard showing stats and top jobs."""
        conn = get_db()
        total = conn.execute("SELECT COUNT(*) FROM jobs WHERE hidden = 0").fetchone()[0]
        applied = conn.execute("SELECT COUNT(*) FROM jobs WHERE applied = 1").fetchone()[0]
        avg_score = conn.execute("SELECT AVG(match_score) FROM jobs WHERE hidden = 0").fetchone()[0] or 0

        # Board distribution
        boards = conn.execute(
            "SELECT board, COUNT(*) as cnt FROM jobs WHERE hidden = 0 GROUP BY board ORDER BY cnt DESC"
        ).fetchall()

        # Top jobs
        top = conn.execute(
            "SELECT * FROM jobs WHERE hidden = 0 ORDER BY match_score DESC, date_posted DESC LIMIT 20"
        ).fetchall()

        conn.close()

        jobs = []
        for r in top:
            j = dict(r)
            j["match_details"] = json.loads(j.get("match_details", "{}"))
            jobs.append(j)

        return render_template("dashboard.html",
            total=total, applied=applied, avg_score=round(avg_score, 2),
            boards=[dict(b) for b in boards], jobs=jobs)

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

        conn = get_db()
        where = ["hidden = 0"]
        params = []
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

        order_map = {
            "score": "match_score DESC, date_posted DESC",
            "date": "date_posted DESC, match_score DESC",
            "company": "company ASC, match_score DESC",
            "title": "title ASC, match_score DESC",
        }
        order_sql = order_map.get(sort, "match_score DESC, date_posted DESC")

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

        return render_template("jobs.html",
            jobs=jobs, page=page, total_pages=total_pages, total=total,
            board_filter=board_filter, country_filter=country_filter,
            min_score=min_score, search=search, sort=sort,
            boards=[b.value for b in JobBoard], countries=countries)

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
        return render_template("job_detail.html", job=job, application=application, form_answers=form_answers)

    @app.route("/settings")
    def settings():
        """Settings page for exclusions."""
        profile = load_profile()
        locations = profile.get("preferred_locations", [])
        all_boards = [b.value for b in JobBoard]
        return render_template("settings.html", locations=locations, all_boards=all_boards)

    @app.route("/api/scrape", methods=["POST"])
    def api_scrape():
        """Trigger a scrape via the API."""
        data = request.json or {}
        boards = data.get("boards", [])
        max_results = data.get("max_results", 30)
        keywords = data.get("keywords", "")
        excluded_boards = set(data.get("excluded_boards", []))
        excluded_countries = [c.lower() for c in data.get("excluded_countries", [])]

        def run_scrape():
            profile = load_profile()
            matcher = JobMatcher(profile)
            all_jobs = []

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
                    # Skip excluded boards
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
            if excluded_countries:
                unique = [j for j in unique
                          if not any(c in j.location.lower() for c in excluded_countries)]

            ranked = matcher.rank(unique)
            save_jobs(ranked)

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
        return jsonify({"status": "ok", "rescored": len(ranked)})

    @app.route("/api/job/apply", methods=["POST"])
    def api_apply():
        url = request.json.get("url", "")
        if url:
            mark_applied(url)
        return jsonify({"status": "ok"})

    @app.route("/api/job/hide", methods=["POST"])
    def api_hide():
        url = request.json.get("url", "")
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
        runs = get_pipeline_runs(limit=20)
        apps = get_applications()
        total_applications = len(apps)
        total_emails = sum(r.get("emails_sent", 0) for r in runs)
        # Email enabled flag stored in a simple file
        email_flag = Path(__file__).parent / ".email_enabled"
        email_enabled = email_flag.exists()
        return render_template("pipeline.html",
            runs=runs, total_applications=total_applications,
            total_emails=total_emails, email_enabled=email_enabled)

    @app.route("/download")
    def download_file():
        """Serve a generated PDF file."""
        from flask import send_file
        filepath = request.args.get("path", "")
        if not filepath or not Path(filepath).exists():
            return "File not found", 404
        return send_file(filepath, as_attachment=True)

    @app.route("/api/generate-application", methods=["POST"])
    def api_generate_application():
        """Generate customized CV + cover letter for a job (runs in background thread)."""
        data = request.json or {}
        url = data.get("url", "")
        if not url:
            return jsonify({"status": "error", "error": "URL required"})

        conn = get_db()
        row = conn.execute("SELECT * FROM jobs WHERE url = ?", (url,)).fetchone()
        conn.close()
        if not row:
            return jsonify({"status": "error", "error": "Job not found"})

        job = dict(row)

        def generate():
            try:
                from cv_customizer import customize_cv_for_job, analyze_job, LIFE_STORY_PATH
                from cover_letter import create_cover_letter
                from form_answers import generate_form_answers as gen_answers
                from storage import create_application, update_application

                profile = load_profile()
                model = profile.get("pipeline", {}).get("ollama_model", "qwen3.5:9b")

                result = customize_cv_for_job(
                    job_url=job["url"], title=job["title"],
                    company=job["company"], location=job.get("location", ""),
                    description=job.get("description", ""), model=model,
                )
                if not result:
                    return

                app_id = create_application(job["url"], result["slug"])
                update_application(app_id, status="cv_generated", cv_pdf_path=result["cv_pdf_path"])

                life_story = LIFE_STORY_PATH.read_text(encoding="utf-8") if LIFE_STORY_PATH.exists() else ""
                job_analysis = analyze_job(job.get("description", ""), job["title"], job["company"], model=model)

                cl_path = create_cover_letter(
                    app_dir=result["app_dir"], title=job["title"],
                    company=job["company"], location=job.get("location", ""),
                    description=job.get("description", ""),
                    life_story=life_story, job_analysis=job_analysis, model=model,
                )
                if cl_path:
                    update_application(app_id, status="letter_generated", cover_letter_pdf_path=cl_path)

                answers = gen_answers(
                    life_story=life_story, title=job["title"],
                    company=job["company"], description=job.get("description", ""),
                    job_analysis=job_analysis, model=model,
                )
                if answers:
                    update_application(app_id, status="ready", form_answers_json=json.dumps(answers))

                logger.info("Application generated for %s at %s", job["title"], job["company"])
            except Exception as e:
                logger.error("Application generation failed: %s", e)

        thread = threading.Thread(target=generate)
        thread.start()
        return jsonify({"status": "ok", "message": "Generating application in background..."})

    @app.route("/api/add-job", methods=["POST"])
    def api_add_job():
        """Manually add a job from any source (Indeed MCP, LinkedIn, etc.)."""
        data = request.json or {}
        url = data.get("url", "")
        if not url:
            return jsonify({"status": "error", "error": "URL required"})

        title = data.get("title", "Unknown Position")
        company = data.get("company", "Unknown Company")
        location = data.get("location", "")
        description = data.get("description", "")

        job = Job(
            title=title, company=company, location=location,
            url=url, board=JobBoard.INDEED,  # Default board for manual entries
            description=description,
        )

        # Score the job
        profile = load_profile()
        matcher = JobMatcher(profile)
        ranked = matcher.rank([job])
        n_saved = save_jobs(ranked)

        score = ranked[0].match_score if ranked else 0
        return jsonify({
            "status": "ok",
            "saved": n_saved,
            "match_score": score,
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
                run_pipeline(profile=profile, dry_run=dry_run)
            except Exception as e:
                logger.error("Pipeline failed: %s", e)

        thread = threading.Thread(target=run)
        thread.start()
        return jsonify({"status": "ok", "message": "Pipeline started in background"})

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
