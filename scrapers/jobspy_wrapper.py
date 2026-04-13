"""python-jobspy wrappers for Indeed, Glassdoor, and Google Jobs.

These replace the old HTML-scraping versions which break due to JS rendering
and bot detection. python-jobspy handles all of that internally.

Install: pip install python-jobspy
"""

from __future__ import annotations

import logging
import math
from typing import Optional

from models import Job, JobBoard, SearchQuery

logger = logging.getLogger(__name__)

try:
    from jobspy import scrape_jobs as _jobspy_scrape
    JOBSPY_AVAILABLE = True
except ImportError:
    JOBSPY_AVAILABLE = False
    logger.warning("python-jobspy not installed — run: pip install python-jobspy")

# Maps profile location strings → jobspy country_indeed values (see jobspy.model.Country)
_COUNTRY_MAP = {
    "germany": "Germany",
    "netherlands": "Netherlands",
    "switzerland": "Switzerland",
    "united kingdom": "UK",
    "uk": "UK",
    "france": "France",
    "spain": "Spain",
    "usa": "USA",
    "united states": "USA",
    "uae": "United Arab Emirates",
    "saudi arabia": "Saudi Arabia",
    "qatar": "Qatar",
    "belgium": "Belgium",
    "austria": "Austria",
    "denmark": "Denmark",
    "sweden": "Sweden",
    "norway": "Norway",
    "finland": "Finland",
    "poland": "Poland",
    "estonia": "Estonia",
    "tallinn": "Estonia",
    "egypt": "Egypt",
    "cairo": "Egypt",
    "luxembourg": "Luxembourg",
    "malta": "Malta",
    "ireland": "Ireland",
    "portugal": "Portugal",
    "czech republic": "Czech Republic",
    "czechia": "Czech Republic",
    "europe": "Germany",   # Europe queries → Germany as hub
    "remote": "Germany",   # hub for remote-inclusive Indeed/Glassdoor filters
}


def _country(location: str) -> str:
    return _COUNTRY_MAP.get(location.lower().strip(), "USA")


def _clean(value) -> str:
    """Convert a DataFrame value to string, treating NaN/None/nan as empty."""
    s = str(value or "").strip()
    return "" if s.lower() in ("nan", "none", "nat", "null") else s


def _finite_number(value) -> Optional[float]:
    """Return a finite float for salary fields, or None for NaN/None/missing."""
    if value is None:
        return None
    try:
        import pandas as pd

        if pd.isna(value):
            return None
    except Exception:
        pass
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(x):
        return None
    return x


def _format_salary_line(currency: str, min_amt: Optional[float], max_amt: Optional[float]) -> str:
    """Build salary string from amounts (may be only one side)."""
    cur = (currency or "").strip()
    if min_amt is not None and max_amt is not None:
        return f"{cur} {int(min_amt):,}–{int(max_amt):,}".strip()
    if min_amt is not None:
        return f"{cur} {int(min_amt):,}+".strip()
    if max_amt is not None:
        return f"{cur} up to {int(max_amt):,}".strip()
    return ""


def _df_to_jobs(df, board: JobBoard) -> list[Job]:
    if df is None or df.empty:
        return []
    jobs = []
    for _, row in df.iterrows():
        url = _clean(row.get("job_url"))
        title = _clean(row.get("title"))
        if not url or not title:
            continue
        min_amt = _finite_number(row.get("min_amount"))
        max_amt = _finite_number(row.get("max_amount"))
        currency = _clean(row.get("currency"))
        salary = _format_salary_line(currency, min_amt, max_amt)
        jobs.append(Job(
            title=title,
            company=_clean(row.get("company")) or "Unknown",
            location=_clean(row.get("location")),
            url=url,
            board=board,
            description=_clean(row.get("description")),
            salary=salary,
            date_posted=_clean(row.get("date_posted")),
            job_type=_clean(row.get("job_type")),
        ))
    logger.info(f"  JobSpy {board.value}: {len(jobs)} jobs found")
    return jobs


def _log_jobspy_failure(board: JobBoard, err: BaseException) -> None:
    """Log JobSpy failures; downgrade expected / environment issues to warning."""
    msg = str(err)
    if board == JobBoard.GLASSDOOR and (
        "Glassdoor is not available" in msg or "not available for" in msg.lower()
    ):
        logger.warning("JobSpy Glassdoor: %s", msg)
        return
    if board == JobBoard.GOOGLE and (
        isinstance(err, KeyError)
        or msg in ("'GOOGLE'", "GOOGLE")
    ):
        logger.warning(
            "JobSpy Google failed (%s). Upgrade python-jobspy "
            "(pip install -U python-jobspy) or remove \"google\" from search.boards.",
            msg,
        )
        return
    if board == JobBoard.LINKEDIN and "Invalid country string" in msg:
        logger.warning(
            "JobSpy LinkedIn: %s — upgrade python-jobspy or use a broader location "
            "(e.g. Finland/Germany instead of a small country) in profile search.locations.",
            msg,
        )
        return
    logger.error("JobSpy %s error: %s", board.value, err)


class JobSpyIndeedScraper:
    """Indeed via python-jobspy (handles Cloudflare + JS rendering)."""

    def scrape(self, query: SearchQuery, max_results: int = 50) -> list[Job]:
        if not JOBSPY_AVAILABLE:
            logger.warning("Skipping Indeed (python-jobspy not installed)")
            return []
        try:
            df = _jobspy_scrape(
                site_name=["indeed"],
                search_term=query.keywords,
                location=query.location or "",
                results_wanted=max_results,
                hours_old=query.max_age_days * 24,
                country_indeed=_country(query.location or ""),
                is_remote=True if query.remote else None,
                verbose=0,
            )
            return _df_to_jobs(df, JobBoard.INDEED)
        except Exception as e:
            _log_jobspy_failure(JobBoard.INDEED, e)
            return []

    def get_job_details(self, job: Job) -> Job:
        return job  # jobspy returns full descriptions


class JobSpyGlassdoorScraper:
    """Glassdoor via python-jobspy."""

    def scrape(self, query: SearchQuery, max_results: int = 50) -> list[Job]:
        if not JOBSPY_AVAILABLE:
            logger.warning("Skipping Glassdoor (python-jobspy not installed)")
            return []
        try:
            df = _jobspy_scrape(
                site_name=["glassdoor"],
                search_term=query.keywords,
                location=query.location or "",
                results_wanted=max_results,
                hours_old=query.max_age_days * 24,
                country_indeed=_country(query.location or ""),
                verbose=0,
            )
            return _df_to_jobs(df, JobBoard.GLASSDOOR)
        except Exception as e:
            _log_jobspy_failure(JobBoard.GLASSDOOR, e)
            return []

    def get_job_details(self, job: Job) -> Job:
        return job


class JobSpyGoogleScraper:
    """Google Jobs via python-jobspy (no API key needed)."""

    def scrape(self, query: SearchQuery, max_results: int = 50) -> list[Job]:
        if not JOBSPY_AVAILABLE:
            logger.warning("Skipping Google Jobs (python-jobspy not installed)")
            return []
        try:
            # Build a descriptive Google search term
            google_term = query.keywords
            if query.location:
                google_term += f" {query.location}"
            if query.remote:
                google_term += " remote"
            age_label = {1: "1d", 3: "3d", 7: "1w", 14: "2w"}.get(
                query.max_age_days, f"{query.max_age_days}d"
            )
            google_term += f" since:{age_label}"

            df = _jobspy_scrape(
                site_name=["google"],
                search_term=query.keywords,
                google_search_term=google_term,
                location=query.location or "",
                results_wanted=max_results,
                hours_old=query.max_age_days * 24,
                verbose=0,
            )
            return _df_to_jobs(df, JobBoard.GOOGLE)
        except Exception as e:
            _log_jobspy_failure(JobBoard.GOOGLE, e)
            return []

    def get_job_details(self, job: Job) -> Job:
        return job


class JobSpyLinkedInScraper:
    """LinkedIn via python-jobspy (handles anti-bot + returns full descriptions)."""

    def scrape(self, query: SearchQuery, max_results: int = 50) -> list[Job]:
        if not JOBSPY_AVAILABLE:
            logger.warning("Skipping LinkedIn JobSpy (python-jobspy not installed)")
            return []
        try:
            df = _jobspy_scrape(
                site_name=["linkedin"],
                search_term=query.keywords,
                location=query.location or "",
                results_wanted=max_results,
                hours_old=query.max_age_days * 24,
                is_remote=bool(query.remote),
                verbose=0,
            )
            return _df_to_jobs(df, JobBoard.LINKEDIN)
        except Exception as e:
            _log_jobspy_failure(JobBoard.LINKEDIN, e)
            return []

    def get_job_details(self, job: Job) -> Job:
        return job  # jobspy returns full descriptions
