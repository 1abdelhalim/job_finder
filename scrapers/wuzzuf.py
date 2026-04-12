"""Wuzzuf.net scraper — Egypt's largest job board.

Covers Egypt and wider MENA tech market. Free, no API key needed.
"""

import logging
import re
import time
import random
import urllib.parse
from typing import Optional

import requests
from bs4 import BeautifulSoup

from models import Job, JobBoard, SearchQuery

logger = logging.getLogger(__name__)

BASE_URL = "https://wuzzuf.net"
SEARCH_URL = f"{BASE_URL}/search/jobs/"

EGYPT_LOCATIONS = {
    "egypt", "cairo", "alexandria", "giza", "hurghada", "luxor",
    "mansoura", "tanta", "zagazig", "aswan", "ismailia",
    "مصر", "القاهرة",  # Arabic variants
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


class WuzzufScraper:
    """Scrape Wuzzuf.net job listings (HTML scraping, no API key needed)."""

    def __init__(self, delay_range: tuple[float, float] = (1.5, 3.0)):
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        self.delay_range = delay_range

    def scrape(self, query: SearchQuery, max_results: int = 50) -> list[Job]:
        # Only run for Egypt/MENA or unspecified locations
        if not self._is_egypt_location(query.location):
            return []

        jobs = []
        start = 0
        keywords = urllib.parse.quote_plus(query.keywords)

        while len(jobs) < max_results:
            params = {
                "q": query.keywords,
                "a[]=": "New",  # show newer jobs first
                "start": start,
            }
            if query.location and self._is_egypt_location(query.location):
                params["l[]"] = "Egypt"

            time.sleep(random.uniform(*self.delay_range))

            try:
                resp = self.session.get(SEARCH_URL, params=params, timeout=20)
                if resp.status_code == 404:
                    break
                resp.raise_for_status()
            except Exception as e:
                logger.error(f"Wuzzuf scrape error: {e}")
                break

            soup = BeautifulSoup(resp.text, "html.parser")

            # Wuzzuf job cards
            cards = soup.select("div[class*='css-'][data-search-result]")
            if not cards:
                # Fallback selectors
                cards = soup.select("div.css-1gatmva, article.css-1xaesre")
            if not cards:
                # Last resort: any article with a job title link
                cards = soup.select("article")

            if not cards:
                logger.info("Wuzzuf: no more results at start=%d", start)
                break

            for card in cards:
                if len(jobs) >= max_results:
                    break
                job = self._parse_card(card)
                if job:
                    jobs.append(job)

            if len(cards) < 5:
                break
            start += len(cards)

        logger.info(f"  Wuzzuf: {len(jobs)} jobs found")
        return jobs[:max_results]

    def _parse_card(self, card) -> Optional[Job]:
        # Title
        title_el = card.select_one("h2 a, h3 a, a[class*='css-'][data-pk]")
        if not title_el:
            title_el = card.select_one("a[href*='/jobs/p/']")
        if not title_el:
            return None

        title = title_el.get_text(strip=True)
        href = title_el.get("href", "")
        url = f"{BASE_URL}{href}" if href.startswith("/") else href
        if not url:
            return None

        # Company
        company_el = card.select_one(
            "a[class*='company'], span[class*='company'], "
            "a[href*='/company/'], div[class*='company']"
        )
        company = company_el.get_text(strip=True) if company_el else "Unknown"

        # Location
        loc_el = card.select_one(
            "span[class*='location'], a[class*='location'], "
            "span[class*='city'], span[class*='area']"
        )
        location = loc_el.get_text(strip=True) if loc_el else "Egypt"

        # Date posted
        date_el = card.select_one("span[class*='ago'], time, span[class*='date']")
        date_posted = date_el.get("datetime", date_el.get_text(strip=True)) if date_el else ""

        # Brief description from card
        desc_el = card.select_one(
            "div[class*='desc'], p[class*='desc'], div[class*='job-description']"
        )
        description = desc_el.get_text(strip=True) if desc_el else ""

        return Job(
            title=title,
            company=company,
            location=location,
            url=url,
            board=JobBoard.WUZZUF,
            description=description,
            date_posted=date_posted,
        )

    def get_job_details(self, job: Job) -> Job:
        """Fetch full job description from the job page."""
        time.sleep(random.uniform(*self.delay_range))
        try:
            resp = self.session.get(job.url, timeout=20)
            resp.raise_for_status()
        except Exception:
            return job

        soup = BeautifulSoup(resp.text, "html.parser")
        desc_el = soup.select_one(
            "section[class*='description'], div[class*='description'], "
            "div[class*='job-body'], div[class*='details']"
        )
        if desc_el:
            job.description = desc_el.get_text(separator="\n", strip=True)
        return job

    def _is_egypt_location(self, location: str) -> bool:
        """Return True if the location is Egypt or unspecified (global searches)."""
        if not location:
            return True  # run for unspecified locations
        loc = location.lower()
        return any(kw in loc for kw in EGYPT_LOCATIONS) or "mena" in loc
