"""Greenhouse ATS public job board scraper.

Greenhouse is used by many AI/robotics/autonomy companies and exposes
a completely free, unauthenticated public API.

Usage: configure target companies in profile.yaml under companies.greenhouse
"""

import logging
from pathlib import Path
from typing import Optional

import requests
import yaml

from models import Job, JobBoard, SearchQuery

logger = logging.getLogger(__name__)

GREENHOUSE_API = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
_PROFILE_PATH = Path(__file__).parent.parent / "profile.yaml"


def _load_companies() -> list[dict]:
    """Load Greenhouse company list from profile.yaml."""
    try:
        with open(_PROFILE_PATH) as f:
            profile = yaml.safe_load(f)
        return profile.get("companies", {}).get("greenhouse", [])
    except Exception as e:
        logger.warning(f"Could not load Greenhouse companies from profile: {e}")
        return []


class GreenhouseScraper:
    """Scrape all open roles from Greenhouse-hosted company career pages.

    No API key required. Returns jobs matching query keywords from
    companies listed under profile.yaml > companies > greenhouse.
    """

    def scrape(self, query: SearchQuery, max_results: int = 50) -> list[Job]:
        companies = _load_companies()
        if not companies:
            logger.debug("No Greenhouse companies configured in profile.yaml")
            return []

        keywords = [kw.lower() for kw in query.keywords.split()]
        all_jobs: list[Job] = []

        for company in companies:
            if len(all_jobs) >= max_results:
                break
            try:
                jobs = self._scrape_company(company, keywords)
                all_jobs.extend(jobs)
            except requests.HTTPError as e:
                if e.response is not None and e.response.status_code == 404:
                    logger.warning(f"Greenhouse: '{company['slug']}' not found (check slug)")
                else:
                    logger.warning(f"Greenhouse {company.get('name', '?')}: HTTP error {e}")
            except Exception as e:
                logger.warning(f"Greenhouse {company.get('name', '?')}: {e}")

        logger.info(f"  Greenhouse: {len(all_jobs)} matching jobs across {len(companies)} companies")
        return all_jobs[:max_results]

    def _scrape_company(self, company: dict, keywords: list[str]) -> list[Job]:
        slug = company["slug"]
        name = company.get("name", slug)
        resp = requests.get(
            GREENHOUSE_API.format(slug=slug),
            params={"content": "true"},
            timeout=10,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        resp.raise_for_status()
        data = resp.json()

        jobs = []
        for item in data.get("jobs", []):
            title = item.get("title", "")
            if not title:
                continue
            title_lower = title.lower()
            content_lower = (item.get("content", "") or "").lower()
            # keyword match in title or description
            if keywords and not any(kw in title_lower or kw in content_lower for kw in keywords):
                continue

            # location: Greenhouse puts it under offices[] or location{}
            location = ""
            offices = item.get("offices") or []
            loc_obj = item.get("location") or {}
            if offices:
                location = offices[0].get("name", "")
            elif isinstance(loc_obj, dict):
                location = loc_obj.get("name", "")

            url = item.get("absolute_url", "")
            if not url:
                continue

            jobs.append(Job(
                title=title,
                company=name,
                location=location,
                url=url,
                board=JobBoard.GREENHOUSE,
                description=item.get("content", ""),
                date_posted=item.get("updated_at", ""),
            ))

        logger.debug(f"  Greenhouse {name}: {len(jobs)} matching jobs")
        return jobs

    def get_job_details(self, job: Job) -> Job:
        return job  # content already included in list response
