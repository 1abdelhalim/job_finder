"""Adzuna API — free registration at developer.adzuna.com gives app_id + app_key.
Covers UK, DE, FR, NL, AU, and many more countries."""

import logging
import os

import requests

from models import Job, JobBoard, SearchQuery

logger = logging.getLogger(__name__)

BASE_URL = "https://api.adzuna.com/v1/api/jobs"

# Country codes for target regions
COUNTRY_MAP = {
    "germany": "de",
    "deutschland": "de",
    "de": "de",
    "uk": "gb",
    "united kingdom": "gb",
    "gb": "gb",
    "france": "fr",
    "fr": "fr",
    "netherlands": "nl",
    "nl": "nl",
    "belgium": "be",
    "be": "be",
    "switzerland": "ch",
    "ch": "ch",
    "austria": "at",
    "at": "at",
    "italy": "it",
    "it": "it",
    "spain": "es",
    "es": "es",
    "poland": "pl",
    "pl": "pl",
    "sweden": "se",
    "se": "se",
    "uae": "ae",
    "united arab emirates": "ae",
    "ae": "ae",
    "saudi arabia": "sa",
    "sa": "sa",
    "qatar": "qa",
    "qa": "qa",
}


class AdzunaScraper:
    """Fetch jobs from the Adzuna API (free tier)."""

    # Track (country, keywords) combos already queried across scraper instances
    # to avoid redundant API calls when "Europe" overlaps with individual countries.
    _queried_combos: set[tuple[str, str]] = set()

    def __init__(self):
        self.app_id = os.environ.get("ADZUNA_APP_ID", "")
        self.app_key = os.environ.get("ADZUNA_APP_KEY", "")
        if not self.app_id or not self.app_key:
            logger.warning(
                "ADZUNA_APP_ID and ADZUNA_APP_KEY not set. "
                "Register free at https://developer.adzuna.com to get them."
            )

    def scrape(self, query: SearchQuery, max_results: int = 50) -> list[Job]:
        if not self.app_id or not self.app_key:
            logger.warning("Skipping Adzuna (no API keys)")
            return []

        jobs = []
        seen_ids: set[str] = set()  # Adzuna job IDs to deduplicate across countries
        countries = self._resolve_countries(query.location)

        for country in countries:
            # Skip if this country+keywords combo was already queried
            combo = (country, query.keywords.lower().strip())
            if combo in AdzunaScraper._queried_combos:
                logger.debug(f"  Skipping Adzuna {country} for '{query.keywords}' (already queried)")
                continue
            AdzunaScraper._queried_combos.add(combo)

            page = 1
            while len(jobs) < max_results:
                params = {
                    "app_id": self.app_id,
                    "app_key": self.app_key,
                    "results_per_page": min(50, max_results - len(jobs)),
                    "what": query.keywords,
                    "max_days_old": query.max_age_days,
                    "content-type": "application/json",
                }
                if query.job_type:
                    params["full_time"] = "1" if query.job_type == "full-time" else "0"

                url = f"{BASE_URL}/{country}/search/{page}"

                try:
                    resp = requests.get(url, params=params, timeout=15)
                    resp.raise_for_status()
                    data = resp.json()
                except Exception as e:
                    logger.error(f"Adzuna API error ({country}): {e}")
                    break

                results = data.get("results", [])
                if not results:
                    break

                for item in results:
                    # Deduplicate by Adzuna job ID or title+company fingerprint
                    adzuna_id = str(item.get("id", ""))
                    title = item.get("title", "")
                    company = item.get("company", {}).get("display_name", "Unknown")
                    fingerprint = adzuna_id or f"{title.lower().strip()}|{company.lower().strip()}"
                    if fingerprint in seen_ids:
                        continue
                    seen_ids.add(fingerprint)

                    job = Job(
                        title=title,
                        company=company,
                        location=item.get("location", {}).get("display_name", country.upper()),
                        url=item.get("redirect_url", ""),
                        board=JobBoard.ADZUNA,
                        description=item.get("description", ""),
                        salary=self._format_salary(item),
                        date_posted=item.get("created", ""),
                        job_type=item.get("contract_time", ""),
                    )
                    if job.url and job.title:
                        jobs.append(job)

                page += 1
                if len(results) < 50:
                    break

        logger.info(f"  Adzuna: {len(jobs)} jobs found")
        return jobs[:max_results]

    def get_job_details(self, job: Job) -> Job:
        # Adzuna API returns descriptions in search results
        return job

    def _resolve_countries(self, location: str) -> list[str]:
        """Map location string to Adzuna country codes."""
        if not location:
            return ["de", "gb", "nl", "fr"]  # Default: Western Europe

        loc_lower = location.lower()
        for key, code in COUNTRY_MAP.items():
            if key in loc_lower:
                return [code]

        # If location is a broad region, search multiple countries
        if any(w in loc_lower for w in ["europe", "west europe", "western europe", "eu"]):
            return ["de", "gb", "nl", "fr", "be", "ch", "at"]

        return ["de"]  # Default to Germany

    def _format_salary(self, item: dict) -> str:
        min_sal = item.get("salary_min")
        max_sal = item.get("salary_max")
        if min_sal and max_sal:
            return f"{int(min_sal):,} - {int(max_sal):,}"
        elif min_sal:
            return f"From {int(min_sal):,}"
        elif max_sal:
            return f"Up to {int(max_sal):,}"
        return ""
