"""Dashboard / nav copy and score thresholds — merged from profile.yaml under `ui:`."""

from copy import deepcopy
from typing import Any, Dict, List

DEFAULT_UI: Dict[str, Any] = {
    "brand_name": "AI Apply",
    "tagline": "Job discovery & matching",
    "hero_title": "Your job command center",
    "hero_subtitle": (
        "Scrape sources, re-score against your profile, and work the list — "
        "same tools as the CLI, in one place."
    ),
    "kpi_review_pct": 40,
    "kpi_strong_pct": 50,
    "browse_min_pct": 30,
    "nav_top_matches_pct": 40,
    "kpi_visible_label": "Visible jobs",
    "kpi_applied_label": "Applied",
    "kpi_to_review_label": "To review",
    "kpi_strong_label": "Strong",
    "kpi_avg_label": "Avg match",
    "kpi_hidden_label": "Hidden",
    "quick_links": [
        {"label": "≥40% not applied", "query": "min_score=40&sort=score&hide_applied=1"},
        {"label": "≥60% matches", "query": "min_score=60&sort=score"},
        {"label": "Newest first", "query": "sort=date"},
        {"label": "Applied history", "query": "applied_only=1"},
        {"label": "All jobs", "query": ""},
    ],
    "empty_state_title": "No jobs yet",
    "empty_state_body": (
        "Click <strong>Scrape all sources</strong>, wait ~1–2 minutes, then refresh. "
        "For boards that need API keys, add them to <code>.env</code> (see <code>.env.example</code>)."
    ),
    "actions_help": (
        "Scrape runs in the <strong>background</strong> — this page reloads in ~45s, or refresh manually. "
        "Queries, locations, and boards are configured in Settings."
    ),
    "dashboard_footer_label": "Browse all filtered jobs",
    "dashboard_search_queries_limit": 8,
    "jobs_page_title": "All jobs",
    "applications_blurb": "Auto-generated CVs, cover letters, and form answers for matched jobs.",
}


def _clamp_pct(v: Any, default: int) -> int:
    try:
        x = int(v)
    except (TypeError, ValueError):
        x = default
    return max(0, min(100, x))


def _normalize_quick_links(raw: Any) -> List[Dict[str, str]]:
    if not isinstance(raw, list) or not raw:
        return deepcopy(DEFAULT_UI["quick_links"])
    out: List[Dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        label = item.get("label")
        query = item.get("query")
        if label is None or query is None:
            continue
        out.append({"label": str(label), "query": str(query)})
    return out if out else deepcopy(DEFAULT_UI["quick_links"])


def get_ui_config(profile: Dict[str, Any]) -> Dict[str, Any]:
    """Return UI config merged with defaults (safe if profile is empty)."""
    raw = profile.get("ui") if isinstance(profile, dict) else None
    if not isinstance(raw, dict):
        raw = {}
    merged = {**DEFAULT_UI, **raw}
    merged["quick_links"] = _normalize_quick_links(raw.get("quick_links"))
    merged["kpi_review_pct"] = _clamp_pct(merged.get("kpi_review_pct"), DEFAULT_UI["kpi_review_pct"])
    merged["kpi_strong_pct"] = _clamp_pct(merged.get("kpi_strong_pct"), DEFAULT_UI["kpi_strong_pct"])
    merged["browse_min_pct"] = _clamp_pct(merged.get("browse_min_pct"), DEFAULT_UI["browse_min_pct"])
    merged["nav_top_matches_pct"] = _clamp_pct(
        merged.get("nav_top_matches_pct"), DEFAULT_UI["nav_top_matches_pct"]
    )
    key = "dashboard_search_queries_limit"
    merged[key] = max(1, min(100, int(merged.get(key) or DEFAULT_UI[key])))
    for key in (
        "brand_name",
        "tagline",
        "hero_title",
        "hero_subtitle",
        "kpi_visible_label",
        "kpi_applied_label",
        "kpi_to_review_label",
        "kpi_strong_label",
        "kpi_avg_label",
        "kpi_hidden_label",
        "empty_state_title",
        "actions_help",
        "dashboard_footer_label",
        "jobs_page_title",
        "applications_blurb",
    ):
        if not isinstance(merged.get(key), str) or not str(merged.get(key)).strip():
            merged[key] = DEFAULT_UI[key]
    if not isinstance(merged.get("empty_state_body"), str):
        merged["empty_state_body"] = DEFAULT_UI["empty_state_body"]
    return merged
