"""GulfTalent scraper — specialist job board for the Gulf/Middle East region.
Covers UAE, Saudi Arabia, Qatar, Kuwait, Bahrain, Oman. Strong for senior tech roles."""

import logging
import time
import random
import urllib.parse
from typing import Optional

import requests
from bs4 import BeautifulSoup

from models import Job, JobBoard, SearchQuery

logger = logging.getLogger(__name__)

BASE_URL = "https://www.gulftalent.com"

COUNTRY_MAP = {
    "uae": "united-arab-emirates",
    "united arab emirates": "united-arab-emirates",
    "dubai": "united-arab-emirates",
    "abu dhabi": "united-arab-emirates",
    "saudi arabia": "saudi-arabia",
    "riyadh": "saudi-arabia",
    "jeddah": "saudi-arabia",
    "qatar": "qatar",
    "doha": "qatar",
    "kuwait": "kuwait",
    "bahrain": "bahrain",
    "oman": "oman",
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


class GulfTalentScraper:
    """Scrape GulfTalent.com job listings (HTML scraping, no API key needed)."""

    def __init__(self, delay_range: tuple[float, float] = (2.0, 4.0)):
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        self.delay_range = delay_range

    def scrape(self, query: SearchQuery, max_results: int = 50) -> list[Job]:
        # Only run for Gulf/Middle East locations
        if not self._is_gulf_location(query.location):
            return []

        jobs = []
        page = 1
        country_slug = self._resolve_country(query.location)
        keywords_encoded = urllib.parse.quote_plus(query.keywords)

        while len(jobs) < max_results:
            if country_slug:
                url = f"{BASE_URL}/jobs/in-{country_slug}/all-industries/all-functions/{page}/"
            else:
                url = f"{BASE_URL}/jobs/in-gulf/all-industries/all-functions/{page}/"

            params = {"q": query.keywords}

            time.sleep(random.uniform(*self.delay_range))

            try:
                resp = self.session.get(url, params=params, timeout=20)
                if resp.status_code in (404, 403):
                    break
                resp.raise_for_status()
            except Exception as e:
                logger.error(f"GulfTalent scrape error: {e}")
                break

            soup = BeautifulSoup(resp.text, "html.parser")
            cards = soup.select("div.job_listing, article.job-listing, div[class*='job-item']")
            if not cards:
                cards = soup.select("div.listing")
            if not cards:
                logger.info("GulfTalent: no more results")
                break

            for card in cards:
                job = self._parse_card(card)
                if job:
                    jobs.append(job)

            page += 1
            if len(cards) < 5:
                break

        logger.info(f"  GulfTalent: {len(jobs)} jobs found")
        return jobs[:max_results]

    def _parse_card(self, card) -> Optional[Job]:
        title_el = card.select_one("h3 a, h2 a, a.job-title, a[class*='title']")
        if not title_el:
            return None
        title = title_el.get_text(strip=True)
        href = title_el.get("href", "")
        url = f"{BASE_URL}{href}" if href.startswith("/") else href
        if not url:
            return None

        company_el = card.select_one("span.company, div.company, a[class*='company']")
        company = company_el.get_text(strip=True) if company_el else "Unknown"

        loc_el = card.select_one("span.location, div.location, span[class*='location']")
        location = loc_el.get_text(strip=True) if loc_el else ""

        date_el = card.select_one("span.date, time, span[class*='date']")
        date_posted = date_el.get("datetime", date_el.get_text(strip=True)) if date_el else ""

        desc_el = card.select_one("p.description, div.description, p[class*='desc']")
        description = desc_el.get_text(strip=True) if desc_el else ""

        return Job(
            title=title,
            company=company,
            location=location,
            url=url,
            board=JobBoard.GULFTALENT,
            description=description,
            date_posted=date_posted,
        )

    def get_job_details(self, job: Job) -> Job:
        time.sleep(random.uniform(*self.delay_range))
        try:
            resp = self.session.get(job.url, timeout=20)
            resp.raise_for_status()
        except Exception:
            return job

        soup = BeautifulSoup(resp.text, "html.parser")
        desc_el = soup.select_one(
            "div#job_description, div.job_description, div[class*='job-description'], section[class*='description']"
        )
        if desc_el:
            job.description = desc_el.get_text(separator="\n", strip=True)
        return job

    def _is_gulf_location(self, location: str) -> bool:
        if not location:
            return False
        loc = location.lower()
        return any(key in loc for key in COUNTRY_MAP)

    def _resolve_country(self, location: str) -> Optional[str]:
        if not location:
            return None
        loc = location.lower()
        for key, slug in COUNTRY_MAP.items():
            if key in loc:
                return slug
        return None
