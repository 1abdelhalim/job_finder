"""LinkedIn post scraper via DuckDuckGo search.

Searches for LinkedIn posts (not job listings) where people announce open roles,
team expansions, or direct recruiter outreach. These often surface jobs that never
get posted to traditional job boards.

No API key required — uses DuckDuckGo's unofficial search API.
Post details (date + full text) are fetched from each post's og/JSON-LD meta tags.
"""

import json
import logging
import re

import requests
from bs4 import BeautifulSoup

from models import Job, JobBoard, SearchQuery

logger = logging.getLogger(__name__)

HIRING_TERMS = ["hiring", "we're hiring", "looking for", "open role", "job opening", "join our team"]
HIRING_QUERY = " OR ".join(f'"{t}"' for t in HIRING_TERMS[:3])  # keep query short

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


def _extract_company(title: str, body: str) -> str:
    """Best-effort extraction of company name from search result."""
    for pattern in [
        r"\bat\s+([A-Z][A-Za-z0-9&\s\-\.]+?)(?:[!\.,]|$)",
        r"\bjoining\s+([A-Z][A-Za-z0-9&\s\-\.]+?)(?:[!\.,]|$)",
        r"([A-Z][A-Za-z0-9&\s\-\.]+?)\s+is\s+hiring",
    ]:
        m = re.search(pattern, title + " " + body)
        if m:
            company = m.group(1).strip()
            if 2 <= len(company.split()) <= 5 and len(company) <= 40:
                return company
    return "LinkedIn Post"


def _extract_location(body: str) -> str:
    """Best-effort extraction of location from post snippet."""
    m = re.search(
        r"\b(remote|hybrid|on[\s-]?site|"
        r"[A-Z][a-z]+(?:,\s*[A-Z]{2,})?)\b",
        body,
    )
    return m.group(1) if m else ""


def _fetch_post_details(url: str) -> dict:
    """Fetch a LinkedIn post page and extract date + full text from meta/JSON-LD.

    Returns a dict with optional keys: description, date_posted, company, location.
    Falls back gracefully — LinkedIn may require login for some posts.
    """
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=10, allow_redirects=True)
        if resp.status_code != 200:
            return {}
        soup = BeautifulSoup(resp.text, "lxml")
    except Exception as e:
        logger.debug(f"Could not fetch LinkedIn post {url}: {e}")
        return {}

    result: dict = {}

    # 1. JSON-LD — most reliable when present
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
            if not isinstance(data, dict):
                continue
            date = data.get("datePublished") or data.get("dateCreated") or data.get("uploadDate")
            if date:
                result["date_posted"] = date
            text = data.get("articleBody") or data.get("description")
            if text:
                result["description"] = text.strip()
            author = data.get("author")
            if isinstance(author, dict):
                org = author.get("worksFor") or {}
                if isinstance(org, dict) and org.get("name"):
                    result["company"] = org["name"]
            if result.get("date_posted") and result.get("description"):
                break
        except (json.JSONDecodeError, AttributeError):
            continue

    # 2. OpenGraph / meta tags as fallback
    def _meta(prop: str, attr: str = "property") -> str:
        tag = soup.find("meta", {attr: prop})
        return (tag.get("content") or "").strip() if tag else ""

    if not result.get("description"):
        og_desc = _meta("og:description") or _meta("description", "name")
        if og_desc:
            result["description"] = og_desc

    if not result.get("date_posted"):
        pub_time = _meta("article:published_time") or _meta("datePublished", "name")
        if pub_time:
            result["date_posted"] = pub_time

    return result


class LinkedInPostsScraper:
    """Scrape LinkedIn posts that announce job openings via DuckDuckGo.

    Searches site:linkedin.com/posts with hiring keywords and the query terms.
    Location-agnostic: run once per keyword, not per location.
    get_job_details() fetches each post URL to extract real date + full text.
    """

    def scrape(self, query: SearchQuery, max_results: int = 20) -> list[Job]:
        try:
            from ddgs import DDGS
        except ImportError:
            try:
                from duckduckgo_search import DDGS
            except ImportError:
                logger.warning("ddgs not installed — run: pip install ddgs")
                return []

        search_query = (
            f'site:linkedin.com/posts ({HIRING_QUERY}) "{query.keywords}"'
        )

        jobs: list[Job] = []
        try:
            results = DDGS().text(search_query, max_results=max_results)
        except Exception as e:
            logger.warning(f"LinkedIn posts search failed: {e}")
            return []

        for r in results or []:
            url = r.get("href", "")
            if not url or "linkedin.com/posts/" not in url:
                continue

            title = r.get("title", "")
            body = r.get("body", "")

            company = _extract_company(title, body)
            location = _extract_location(body)

            jobs.append(Job(
                title=query.keywords.title(),
                company=company,
                location=location,
                url=url,
                board=JobBoard.LINKEDIN_POSTS,
                description=f"{title}\n\n{body}",
            ))

        logger.info(f"  LinkedIn Posts: {len(jobs)} posts for '{query.keywords}'")
        return jobs

    def get_job_details(self, job: Job) -> Job:
        """Fetch the post page to get real date and full post text."""
        details = _fetch_post_details(job.url)
        if details.get("description"):
            job.description = details["description"]
        if details.get("date_posted"):
            job.date_posted = details["date_posted"]
        if details.get("company") and job.company == "LinkedIn Post":
            job.company = details["company"]
        return job
