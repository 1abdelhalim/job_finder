"""SQLite storage for scraped jobs."""

import json
import sqlite3
from pathlib import Path

from models import Job, JobBoard

DB_PATH = Path(__file__).parent / "jobs.db"


def get_db(db_path: Path = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS jobs (
            url TEXT PRIMARY KEY,
            title TEXT,
            company TEXT,
            location TEXT,
            board TEXT,
            description TEXT,
            salary TEXT,
            date_posted TEXT,
            job_type TEXT,
            scraped_at TEXT,
            match_score REAL DEFAULT 0,
            match_details TEXT DEFAULT '{}',
            applied INTEGER DEFAULT 0,
            hidden INTEGER DEFAULT 0
        )
    """)
    conn.commit()
    return conn


def save_jobs(jobs: list[Job], db_path: Path = DB_PATH) -> int:
    """Save jobs to SQLite. Returns number of new jobs inserted.
    Deduplicates by both URL and title+company fingerprint."""
    conn = get_db(db_path)
    # Load existing fingerprints to skip title+company duplicates
    existing = conn.execute(
        "SELECT LOWER(TRIM(title)) || '|' || LOWER(TRIM(company)) FROM jobs"
    ).fetchall()
    seen_fingerprints = {row[0] for row in existing}

    inserted = 0
    for job in jobs:
        fingerprint = f"{job.title.lower().strip()}|{job.company.lower().strip()}"
        if fingerprint in seen_fingerprints:
            continue
        try:
            conn.execute(
                """INSERT OR IGNORE INTO jobs
                   (url, title, company, location, board, description,
                    salary, date_posted, job_type, scraped_at, match_score, match_details)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    job.url, job.title, job.company, job.location,
                    job.board.value, job.description, job.salary,
                    job.date_posted, job.job_type, job.scraped_at,
                    job.match_score, json.dumps(job.match_details),
                ),
            )
            seen_fingerprints.add(fingerprint)
            inserted += 1
        except sqlite3.IntegrityError:
            pass
    conn.commit()
    conn.close()
    return inserted


def update_scores(jobs: list[Job], db_path: Path = DB_PATH):
    """Update match scores for existing jobs."""
    conn = get_db(db_path)
    for job in jobs:
        conn.execute(
            "UPDATE jobs SET match_score = ?, match_details = ? WHERE url = ?",
            (job.match_score, json.dumps(job.match_details), job.url),
        )
    conn.commit()
    conn.close()


def get_top_jobs(limit: int = 20, min_score: float = 0.0, db_path: Path = DB_PATH) -> list[dict]:
    """Get top-scored jobs from the database."""
    conn = get_db(db_path)
    rows = conn.execute(
        """SELECT * FROM jobs
           WHERE match_score >= ? AND hidden = 0
           ORDER BY match_score DESC, date_posted DESC
           LIMIT ?""",
        (min_score, limit),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def mark_applied(url: str, db_path: Path = DB_PATH):
    conn = get_db(db_path)
    conn.execute("UPDATE jobs SET applied = 1 WHERE url = ?", (url,))
    conn.commit()
    conn.close()


def mark_hidden(url: str, db_path: Path = DB_PATH):
    conn = get_db(db_path)
    conn.execute("UPDATE jobs SET hidden = 1 WHERE url = ?", (url,))
    conn.commit()
    conn.close()
