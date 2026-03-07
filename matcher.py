"""Job matching engine — scores jobs against a user profile."""

import re
import math
import logging
from datetime import datetime, timedelta
from collections import Counter
from pathlib import Path

from models import Job

LIFE_STORY_PATH = Path(__file__).parent.parent / "A_Customised_CurVe_CV__1_" / "life-story.md"

logger = logging.getLogger(__name__)

# Keywords that indicate AI/ML/CV relevance — a job must contain at least one
AI_KEYWORDS = {
    # Core ML/AI
    "machine learning", "deep learning", "artificial intelligence", "neural network",
    "reinforcement learning", "supervised learning", "unsupervised learning",
    "ml", "ai", "dl",
    # CV / 3D
    "computer vision", "image processing", "object detection", "image recognition",
    "3d reconstruction", "point cloud", "lidar", "depth estimation", "stereo vision",
    "gaussian splatting", "nerf", "neural rendering", "slam", "visual odometry",
    "pose estimation", "segmentation", "tracking",
    # NLP / LLM / VLM
    "natural language processing", "nlp", "llm", "large language model",
    "vision language", "vlm", "gpt", "transformer", "bert", "generative ai",
    "gen ai", "genai", "prompt engineering", "rag", "retrieval augmented",
    # Frameworks / tools (strong signal)
    "pytorch", "tensorflow", "jax", "keras", "huggingface", "cuda",
    "tensorrt", "onnx", "diffusion model", "stable diffusion",
    # Roles (strong signal in title)
    "data scientist", "research scientist", "applied scientist",
    "ml engineer", "ai engineer", "perception engineer",
    # Domains
    "autonomous driving", "self-driving", "adas", "robotics perception",
    "robot learning", "embodied ai", "physical ai", "digital twin",
    "medical imaging", "speech recognition", "recommender system",
}


def is_ai_related(job: Job) -> bool:
    """Check if a job is AI/ML/CV related based on title and description."""
    text = f"{job.title} {job.description}".lower()
    return any(kw in text for kw in AI_KEYWORDS)


def tokenize(text: str) -> list[str]:
    """Lowercase tokenization, strip non-alphanumeric."""
    return re.findall(r"[a-z0-9#+\-\.]+", text.lower())


def tf(tokens: list[str]) -> dict[str, float]:
    """Term frequency (normalized by document length)."""
    counts = Counter(tokens)
    total = len(tokens)
    if total == 0:
        return {}
    return {t: c / total for t, c in counts.items()}


def cosine_sim(vec_a: dict[str, float], vec_b: dict[str, float]) -> float:
    """Cosine similarity between two sparse vectors."""
    common = set(vec_a) & set(vec_b)
    if not common:
        return 0.0
    dot = sum(vec_a[k] * vec_b[k] for k in common)
    mag_a = math.sqrt(sum(v ** 2 for v in vec_a.values()))
    mag_b = math.sqrt(sum(v ** 2 for v in vec_b.values()))
    if mag_a == 0 or mag_b == 0:
        return 0.0
    return dot / (mag_a * mag_b)


def load_life_story() -> str:
    """Load life-story.md if available, return empty string otherwise."""
    if LIFE_STORY_PATH.exists():
        try:
            return LIFE_STORY_PATH.read_text(encoding="utf-8")
        except Exception:
            return ""
    return ""


def extract_life_story_signals(text: str) -> dict:
    """Extract extra matching signals from the life-story markdown."""
    if not text:
        return {"technologies": set(), "domains": set(), "companies": set(), "full_tokens": []}

    # Extract technology keywords from "Technologies:" lines and code blocks
    tech_pattern = re.compile(r"(?:Technologies(?: used)?[:\s]*|Technologies:)(.*?)(?:\n|$)", re.IGNORECASE)
    techs = set()
    for m in tech_pattern.finditer(text):
        for t in re.split(r"[,;|.]", m.group(1)):
            t = t.strip().lower()
            if t and len(t) > 1:
                techs.add(t)

    # Extract domain-specific phrases from section headers and bold text
    domains = set()
    for m in re.finditer(r"\*\*(.*?)\*\*", text):
        phrase = m.group(1).strip().lower()
        if len(phrase) > 3 and len(phrase) < 60:
            domains.add(phrase)

    # Extract company/institution names from work experience headers
    companies = set()
    for m in re.finditer(r"###\s+.*?—\s+(.*?)(?:,|\n)", text):
        companies.add(m.group(1).strip().lower())

    # Tokenize the entire life story for broader matching
    full_tokens = tokenize(text)

    return {
        "technologies": techs,
        "domains": domains,
        "companies": companies,
        "full_tokens": full_tokens,
    }


class JobMatcher:
    """Score and rank jobs against a user profile."""

    def __init__(self, profile: dict):
        """
        profile should contain:
          - skills: list of skill strings
          - titles: list of desired job title strings
          - keywords: list of important keyword strings
          - min_salary: int (optional, annual)
          - preferred_locations: list of location strings (optional)
          - remote_preferred: bool (optional)
          - weights: dict with keys 'skills', 'title', 'keywords', 'location' (optional)
        """
        self.profile = profile
        self.weights = profile.get("weights", {
            "skills": 0.35,
            "title": 0.25,
            "keywords": 0.15,
            "location": 0.10,
            "experience": 0.15,
        })

        # Pre-tokenize profile components
        self._skills_tokens = tokenize(" ".join(profile.get("skills", [])))
        self._skills_set = set(self._skills_tokens)
        self._title_tokens = tokenize(" ".join(profile.get("titles", [])))
        self._keyword_tokens = tokenize(" ".join(profile.get("keywords", [])))
        self._locations = [loc.lower() for loc in profile.get("preferred_locations", [])]

        # Load life-story for deeper matching
        life_story_text = load_life_story()
        self._life_story = extract_life_story_signals(life_story_text)
        self._life_story_tf = tf(self._life_story["full_tokens"]) if self._life_story["full_tokens"] else {}
        # Merge life-story technologies into skills set
        for tech in self._life_story["technologies"]:
            self._skills_set.update(tokenize(tech))

    def score(self, job: Job) -> tuple[float, dict]:
        """
        Score a job from 0.0 to 1.0.
        Returns (score, details_dict).
        """
        job_text = f"{job.title} {job.description}".lower()
        job_tokens = tokenize(job_text)
        job_tf = tf(job_tokens)
        job_token_set = set(job_tokens)

        # 1. Skills match — fraction of profile skills found in job
        if self._skills_set:
            matched_skills = self._skills_set & job_token_set
            skills_score = len(matched_skills) / len(self._skills_set)
            skills_matched = sorted(matched_skills)
        else:
            skills_score = 0.0
            skills_matched = []

        # 2. Title similarity — cosine similarity between desired titles and job title
        title_tokens = tokenize(job.title)
        title_tf = tf(title_tokens)
        profile_title_tf = tf(self._title_tokens)
        title_score = cosine_sim(title_tf, profile_title_tf)

        # 3. Keyword match — cosine similarity of full text
        profile_kw_tf = tf(self._keyword_tokens + self._skills_tokens)
        keyword_score = cosine_sim(job_tf, profile_kw_tf)

        # 4. Location match
        location_score = 0.0
        if self._locations:
            job_loc = job.location.lower()
            for pref_loc in self._locations:
                if pref_loc in job_loc or job_loc in pref_loc:
                    location_score = 1.0
                    break
            if self.profile.get("remote_preferred") and "remote" in job_loc:
                location_score = 1.0

        # 5. Experience match — life-story deep matching
        experience_score = 0.0
        if self._life_story_tf:
            experience_score = cosine_sim(job_tf, self._life_story_tf)
            # Boost if job mentions domains from life-story
            for domain in self._life_story["domains"]:
                if domain in job_text:
                    experience_score = min(1.0, experience_score + 0.1)

        # 6. Recency boost — newer jobs get up to 0.10 bonus
        recency_score = self._recency_score(job)

        # Weighted sum
        w = self.weights
        total = (
            w["skills"] * skills_score
            + w["title"] * title_score
            + w["keywords"] * keyword_score
            + w["location"] * location_score
            + w.get("experience", 0.15) * experience_score
            + 0.10 * recency_score
        )
        # Normalize back (weights now sum to ~1.1 with recency)
        total = min(total, 1.0)

        details = {
            "skills_score": round(skills_score, 3),
            "skills_matched": skills_matched,
            "title_score": round(title_score, 3),
            "keyword_score": round(keyword_score, 3),
            "location_score": round(location_score, 3),
            "experience_score": round(experience_score, 3),
            "recency_score": round(recency_score, 3),
            "weighted_total": round(total, 3),
        }

        return round(total, 3), details

    def _recency_score(self, job: Job) -> float:
        """Score from 0-1 based on how recently the job was posted. 1.0 = today."""
        if not job.date_posted:
            return 0.3  # Unknown date gets a small default
        try:
            # Handle various date formats
            date_str = job.date_posted[:10]
            posted = datetime.fromisoformat(date_str)
            days_ago = (datetime.now() - posted).days
            if days_ago < 0:
                days_ago = 0
            # Linear decay: 1.0 for today, 0.0 for 30+ days
            return max(0.0, 1.0 - days_ago / 30.0)
        except (ValueError, TypeError):
            return 0.3

    def rank(self, jobs: list[Job], min_score: float = 0.0) -> list[Job]:
        """Score, filter non-AI jobs, and return sorted by score (descending), then date."""
        # Filter out non-AI/ML/CV jobs
        ai_jobs = [j for j in jobs if is_ai_related(j)]
        filtered_count = len(jobs) - len(ai_jobs)
        if filtered_count > 0:
            logger.info(f"Filtered out {filtered_count} non-AI jobs")

        for job in ai_jobs:
            score, details = self.score(job)
            job.match_score = score
            job.match_details = details

        # Sort by score first, then by date (newer first) as tiebreaker
        ranked = sorted(ai_jobs, key=lambda j: (j.match_score, str(j.date_posted or "")), reverse=True)
        if min_score > 0:
            ranked = [j for j in ranked if j.match_score >= min_score]

        return ranked
