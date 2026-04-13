"""Automated email notification system.

Sends digest emails every 2-3 days with new high-match job positions.
Uses Python smtplib with Gmail App Password for fully automated sending.
"""

import logging
import smtplib
import os
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import List, Dict, Optional

from storage import get_last_email_sent, get_new_jobs_since, log_email_sent

logger = logging.getLogger(__name__)

# Domain labels for tagging jobs in emails
DOMAIN_TAGS = {
    "3d": "3D Vision",
    "gaussian": "3D Vision",
    "nerf": "3D Vision",
    "reconstruction": "3D Vision",
    "rendering": "3D Vision",
    "slam": "3D Vision",
    "autonomous": "Autonomous Driving",
    "self-driving": "Autonomous Driving",
    "vehicle": "Autonomous Driving",
    "robotics": "Robotics",
    "robot": "Robotics",
    "manipulation": "Robotics",
    "embodied": "Robotics",
    "vlm": "VLM/Multimodal",
    "vision language": "VLM/Multimodal",
    "multimodal": "VLM/Multimodal",
    "llm": "NLP/LLM",
    "nlp": "NLP/LLM",
    "language model": "NLP/LLM",
    "generative": "Generative AI",
    "diffusion": "Generative AI",
    "computer vision": "Computer Vision",
    "object detection": "Computer Vision",
    "segmentation": "Computer Vision",
    "perception": "Perception",
    "machine learning": "ML Engineering",
    "ml engineer": "ML Engineering",
    "deep learning": "ML Engineering",
    "research": "Research",
}


def _tag_job(job: Dict) -> str:
    """Assign a domain tag to a job based on title and description."""
    text = (job.get("title", "") + " " + job.get("description", "")[:500]).lower()
    for keyword, tag in DOMAIN_TAGS.items():
        if keyword in text:
            return tag
    return "General ML"


def _build_digest_html(jobs: List[Dict], min_score: float) -> str:
    """Build HTML email body for job digest."""
    # Group by tag
    tagged = {}
    for job in jobs:
        tag = _tag_job(job)
        tagged.setdefault(tag, []).append(job)

    pct = int(round(min_score * 100))
    html = """
    <html><body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; max-width: 700px; margin: 0 auto; color: #333;">
    <h1 style="color: #1a1a2e; border-bottom: 3px solid #88AC0B; padding-bottom: 10px;">
        Job Finder Digest
    </h1>
    <p style="color: #666; font-size: 14px;">
        {count} position(s) with match score ≥ {pct}% (since last digest).
    </p>
    """.format(count=len(jobs), pct=pct)

    for tag in sorted(tagged.keys()):
        tag_jobs = tagged[tag]
        html += f"""
        <h2 style="color: #88AC0B; margin-top: 25px; font-size: 18px;">
            {tag} ({len(tag_jobs)})
        </h2>
        <table style="width: 100%; border-collapse: collapse; font-size: 14px;">
        <tr style="background: #f5f5f5; text-align: left;">
            <th style="padding: 8px; border-bottom: 2px solid #ddd;">Role</th>
            <th style="padding: 8px; border-bottom: 2px solid #ddd;">Company</th>
            <th style="padding: 8px; border-bottom: 2px solid #ddd;">Location</th>
            <th style="padding: 8px; border-bottom: 2px solid #ddd;">Score</th>
            <th style="padding: 8px; border-bottom: 2px solid #ddd;">Link</th>
        </tr>
        """
        for job in sorted(tag_jobs, key=lambda j: j.get("match_score", 0), reverse=True):
            score = job.get("match_score", 0)
            score_color = "#27ae60" if score >= 0.7 else "#f39c12" if score >= 0.5 else "#e74c3c"
            html += f"""
            <tr style="border-bottom: 1px solid #eee;">
                <td style="padding: 8px;">{job.get('title', 'N/A')}</td>
                <td style="padding: 8px;">{job.get('company', 'N/A')}</td>
                <td style="padding: 8px; font-size: 12px;">{job.get('location', 'N/A')[:30]}</td>
                <td style="padding: 8px; color: {score_color}; font-weight: bold;">{score:.0%}</td>
                <td style="padding: 8px;">
                    <a href="{job.get('url', '#')}" style="color: #B6073F;">Apply</a>
                </td>
            </tr>
            """
        html += "</table>"

    html += """
    <hr style="margin-top: 30px; border: none; border-top: 1px solid #ddd;">
    <p style="color: #999; font-size: 12px;">
        Sent by Job Finder Automation Pipeline
    </p>
    </body></html>
    """
    return html


def should_send_digest(interval_days: int = 2) -> bool:
    """Check if enough time has passed since the last digest email."""
    last = get_last_email_sent()
    if not last:
        return True
    try:
        last_sent = datetime.fromisoformat(last["sent_at"])
        return datetime.now() - last_sent >= timedelta(days=interval_days)
    except (ValueError, KeyError):
        return True


def send_digest_email(
    jobs: List[Dict],
    recipient: str,
    gmail_user: Optional[str] = None,
    gmail_app_password: Optional[str] = None,
    min_score: float = 0.75,
) -> bool:
    """Send a digest email with new job matches.

    Uses Gmail SMTP with App Password for fully automated sending.
    Set GMAIL_USER and GMAIL_APP_PASSWORD in .env or environment.
    """
    if not jobs:
        logger.info("No new jobs to send in digest")
        return False

    gmail_user = gmail_user or os.environ.get("GMAIL_USER", recipient)
    gmail_app_password = gmail_app_password or os.environ.get("GMAIL_APP_PASSWORD", "")

    if not gmail_app_password:
        logger.error(
            "GMAIL_APP_PASSWORD not set. Generate one at "
            "https://myaccount.google.com/apppasswords and add to .env"
        )
        return False

    pct = int(round(min_score * 100))
    subject = f"Job Finder: {len(jobs)} roles (≥{pct}% match)"
    html_body = _build_digest_html(jobs, min_score=min_score)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = gmail_user
    msg["To"] = recipient
    msg.attach(MIMEText(html_body, "html"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(gmail_user, gmail_app_password)
            server.send_message(msg)

        log_email_sent(subject, len(jobs), recipient)
        logger.info("Digest email sent to %s with %d jobs", recipient, len(jobs))
        return True

    except smtplib.SMTPAuthenticationError:
        logger.error(
            "Gmail authentication failed. Check GMAIL_USER and GMAIL_APP_PASSWORD. "
            "You may need to generate an App Password at https://myaccount.google.com/apppasswords"
        )
        return False
    except Exception as e:
        logger.error("Failed to send email: %s", e)
        return False
