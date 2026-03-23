"""Lever ATS public job board scraper.

Lever is used by many AI/ML companies and exposes a free, unauthenticated
public JSON API for all open job postings.

Usage: configure target companies in profile.yaml under companies.lever
"""

import logging
from pathlib import Path

import requests
import yaml

from models import Job, JobBoard, SearchQuery

logger = logging.getLogger(__name__)

LEVER_API = "https://api.lever.co/v0/postings/{slug}"
_PROFILE_PATH = Path(__file__).parent.parent / "profile.yaml"


def _load_companies() -> list[dict]:
    """Load Lever company list from profile.yaml."""
    try:
        with open(_PROFILE_PATH) as f:
            profile = yaml.safe_load(f)
        return profile.get("companies", {}).get("lever", [])
    except Exception as e:
        logger.warning(f"Could not load Lever companies from profile: {e}")
        return []


class LeverScraper:
    """Scrape all open roles from Lever-hosted company career pages.

    No API key required. Returns jobs matching query keywords from
    companies listed under profile.yaml > companies > lever.
    """

    def scrape(self, query: SearchQuery, max_results: int = 50) -> list[Job]:
        companies = _load_companies()
        if not companies:
            logger.debug("No Lever companies configured in profile.yaml")
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
                    logger.warning(f"Lever: '{company['slug']}' not found (check slug)")
                else:
                    logger.warning(f"Lever {company.get('name', '?')}: HTTP error {e}")
            except Exception as e:
                logger.warning(f"Lever {company.get('name', '?')}: {e}")

        logger.info(f"  Lever: {len(all_jobs)} matching jobs across {len(companies)} companies")
        return all_jobs[:max_results]

    def _scrape_company(self, company: dict, keywords: list[str]) -> list[Job]:
        slug = company["slug"]
        name = company.get("name", slug)
        resp = requests.get(
            LEVER_API.format(slug=slug),
            params={"mode": "json"},
            timeout=10,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        resp.raise_for_status()
        postings = resp.json()

        jobs = []
        for item in postings:
            title = item.get("text", "")
            if not title:
                continue
            title_lower = title.lower()
            desc_lower = (item.get("descriptionPlain") or "").lower()
            if keywords and not any(kw in title_lower or kw in desc_lower for kw in keywords):
                continue

            categories = item.get("categories") or {}
            location = categories.get("location", "") or categories.get("allLocations", [""])[0]
            job_type = categories.get("commitment", "")

            url = item.get("hostedUrl", "")
            if not url:
                continue

            # Prefer plain text description, fall back to HTML
            description = item.get("descriptionPlain") or item.get("description") or ""

            jobs.append(Job(
                title=title,
                company=name,
                location=location,
                url=url,
                board=JobBoard.LEVER,
                description=description,
                date_posted=str(item.get("createdAt", "")),
                job_type=job_type,
            ))

        logger.debug(f"  Lever {name}: {len(jobs)} matching jobs")
        return jobs

    def get_job_details(self, job: Job) -> Job:
        return job  # full description already in list response
