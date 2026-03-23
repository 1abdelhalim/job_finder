"""python-jobspy wrappers for Indeed, Glassdoor, and Google Jobs.

These replace the old HTML-scraping versions which break due to JS rendering
and bot detection. python-jobspy handles all of that internally.

Install: pip install python-jobspy
"""

import logging
from typing import Optional

from models import Job, JobBoard, SearchQuery

logger = logging.getLogger(__name__)

try:
    from jobspy import scrape_jobs as _jobspy_scrape
    JOBSPY_AVAILABLE = True
except ImportError:
    JOBSPY_AVAILABLE = False
    logger.warning("python-jobspy not installed — run: pip install python-jobspy")

# Maps profile location strings → jobspy country_indeed values
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
    "europe": "Germany",   # Europe queries → Germany as hub
}


def _country(location: str) -> str:
    return _COUNTRY_MAP.get(location.lower().strip(), "USA")


def _df_to_jobs(df, board: JobBoard) -> list[Job]:
    if df is None or df.empty:
        return []
    jobs = []
    for _, row in df.iterrows():
        url = str(row.get("job_url") or "")
        title = str(row.get("title") or "")
        if not url or not title:
            continue
        min_amt = row.get("min_amount")
        max_amt = row.get("max_amount")
        currency = str(row.get("currency") or "")
        if min_amt and max_amt:
            salary = f"{currency} {int(min_amt):,}–{int(max_amt):,}".strip()
        elif min_amt:
            salary = f"{currency} {int(min_amt):,}+".strip()
        else:
            salary = ""
        jobs.append(Job(
            title=title,
            company=str(row.get("company") or "Unknown"),
            location=str(row.get("location") or ""),
            url=url,
            board=board,
            description=str(row.get("description") or ""),
            salary=salary,
            date_posted=str(row.get("date_posted") or ""),
            job_type=str(row.get("job_type") or ""),
        ))
    logger.info(f"  JobSpy {board.value}: {len(jobs)} jobs found")
    return jobs


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
            logger.error(f"JobSpy Indeed error: {e}")
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
            logger.error(f"JobSpy Glassdoor error: {e}")
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
            logger.error(f"JobSpy Google error: {e}")
            return []

    def get_job_details(self, job: Job) -> Job:
        return job
