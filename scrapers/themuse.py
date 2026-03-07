"""The Muse API — free, no API key needed. Large index (400k+ jobs)."""

import logging

import requests

from models import Job, JobBoard, SearchQuery

logger = logging.getLogger(__name__)

API_URL = "https://www.themuse.com/api/public/jobs"

# The Muse category mappings relevant to ML/AI/engineering
CATEGORY_MAP = {
    "Data Science": "Data Science",
    "Software Engineering": "Software Engineering",
    "IT": "IT",
    "Data and Analytics": "Data and Analytics",
    "Science and Engineering": "Science and Engineering",
}

# Level mappings
LEVEL_MAP = {
    "entry": "Entry Level",
    "mid": "Mid Level",
    "senior": "Senior Level",
    "management": "Management",
}


class TheMuseScraper:
    """Fetch jobs from The Muse API (free, no key). 400k+ jobs index."""

    def scrape(self, query: SearchQuery, max_results: int = 50) -> list[Job]:
        jobs = []
        page = 0
        query_lower = query.keywords.lower()

        while len(jobs) < max_results:
            params = {
                "page": page,
                "descending": "true",
            }

            # Location filter
            if query.location:
                params["location"] = query.location

            try:
                resp = requests.get(API_URL, params=params, timeout=15)
                resp.raise_for_status()
                data = resp.json()
            except Exception as e:
                logger.error(f"The Muse API error: {e}")
                break

            results = data.get("results", [])
            if not results:
                break

            for item in results:
                if len(jobs) >= max_results:
                    break

                title = item.get("name", "")
                contents = item.get("contents", "")
                searchable = f"{title} {contents}".lower()

                # Filter: must match at least one keyword from the query
                keywords = [w for w in query_lower.split() if len(w) >= 3]
                if not any(kw in searchable for kw in keywords):
                    continue

                company_data = item.get("company", {})
                locations = item.get("locations", [])
                location_str = ", ".join(loc.get("name", "") for loc in locations) if locations else ""

                levels = item.get("levels", [])
                level_str = ", ".join(lv.get("name", "") for lv in levels) if levels else ""

                refs = item.get("refs", {})
                url = refs.get("landing_page", "")

                job = Job(
                    title=title,
                    company=company_data.get("name", "Unknown"),
                    location=location_str,
                    url=url,
                    board=JobBoard.THEMUSE,
                    description=_strip_html(contents),
                    salary="",
                    date_posted=item.get("publication_date", ""),
                    job_type=level_str,
                )
                if job.url and job.title:
                    jobs.append(job)

            page += 1
            page_count = data.get("page_count", 0)
            if page >= page_count or page > 10:  # Limit pages to avoid hammering
                break

        logger.info(f"  The Muse: {len(jobs)} jobs found")
        return jobs[:max_results]

    def get_job_details(self, job: Job) -> Job:
        # API returns full descriptions
        return job


def _strip_html(text: str) -> str:
    """Basic HTML tag stripping."""
    import re
    clean = re.sub(r"<[^>]+>", " ", text)
    clean = re.sub(r"\s+", " ", clean).strip()
    return clean
