"""Bayt.com scraper — largest job board in the Middle East/North Africa."""

import logging
import time
import random
import urllib.parse
from typing import Optional

import requests
from bs4 import BeautifulSoup

from models import Job, JobBoard, SearchQuery

logger = logging.getLogger(__name__)

BASE_URL = "https://www.bayt.com"

LOCATION_MAP = {
    "uae": "ae",
    "united arab emirates": "ae",
    "dubai": "ae",
    "abu dhabi": "ae",
    "saudi arabia": "sa",
    "riyadh": "sa",
    "jeddah": "sa",
    "qatar": "qa",
    "doha": "qa",
    "kuwait": "kw",
    "bahrain": "bh",
    "oman": "om",
    "jordan": "jo",
    "egypt": "eg",
    "lebanon": "lb",
    "middle east": None,  # search all
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


class BaytScraper:
    """Scrape Bayt.com job listings (HTML scraping, no API key needed)."""

    def __init__(self, delay_range: tuple[float, float] = (1.5, 3.0)):
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        self.delay_range = delay_range

    def scrape(self, query: SearchQuery, max_results: int = 50) -> list[Job]:
        # Only run for Middle East locations
        if not self._is_mena_location(query.location):
            return []

        jobs = []
        page = 1
        keywords_slug = urllib.parse.quote_plus(query.keywords)

        while len(jobs) < max_results:
            url = f"{BASE_URL}/en/international/jobs/{keywords_slug}-jobs/"
            params = {"page": page}

            # Add country filter if location maps to a specific country
            country_code = self._resolve_country(query.location)
            if country_code:
                url = f"{BASE_URL}/en/{country_code}/jobs/{keywords_slug}-jobs/"

            time.sleep(random.uniform(*self.delay_range))

            try:
                resp = self.session.get(url, params=params, timeout=20)
                if resp.status_code == 404:
                    break
                resp.raise_for_status()
            except Exception as e:
                logger.error(f"Bayt scrape error: {e}")
                break

            soup = BeautifulSoup(resp.text, "html.parser")
            cards = soup.select("li[data-js-job]")
            if not cards:
                # fallback selector
                cards = soup.select("div.has-pointer-d")
            if not cards:
                logger.info("Bayt: no more results")
                break

            for card in cards:
                job = self._parse_card(card)
                if job:
                    jobs.append(job)

            page += 1
            if len(cards) < 10:
                break

        logger.info(f"  Bayt: {len(jobs)} jobs found")
        return jobs[:max_results]

    def _parse_card(self, card) -> Optional[Job]:
        title_el = card.select_one("h2.jb-title a, h2 a[data-js-aid]")
        if not title_el:
            return None
        title = title_el.get_text(strip=True)
        href = title_el.get("href", "")
        url = f"{BASE_URL}{href}" if href.startswith("/") else href
        if not url:
            return None

        company_el = card.select_one("b.jb-company, span[data-js-aid='jobCompany']")
        company = company_el.get_text(strip=True) if company_el else "Unknown"

        loc_el = card.select_one("span.jb-loc, span[data-js-aid='jobLocation']")
        location = loc_el.get_text(strip=True) if loc_el else ""

        date_el = card.select_one("span.jb-date, time")
        date_posted = date_el.get("datetime", date_el.get_text(strip=True)) if date_el else ""

        desc_el = card.select_one("p.jb-desc, div.jb-description")
        description = desc_el.get_text(strip=True) if desc_el else ""

        return Job(
            title=title,
            company=company,
            location=location,
            url=url,
            board=JobBoard.BAYT,
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
            "div#jobDescription, div.jobDescription, section.jobDescription, div[data-js-job-description]"
        )
        if desc_el:
            job.description = desc_el.get_text(separator="\n", strip=True)
        return job

    def _is_mena_location(self, location: str) -> bool:
        if not location:
            return False
        loc = location.lower()
        return any(key in loc for key in LOCATION_MAP)

    def _resolve_country(self, location: str) -> Optional[str]:
        if not location:
            return None
        loc = location.lower()
        for key, code in LOCATION_MAP.items():
            if key in loc:
                return code
        return None
