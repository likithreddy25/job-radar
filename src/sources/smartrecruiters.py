"""SmartRecruiters ATS board source adapter."""
from __future__ import annotations

import logging
import re
from urllib.parse import urlparse

from ..classifier import classify
from ..utils.http import get_session
from .base import BaseSource, Job, looks_like_metadata_only_description, make_location, merge_text

log = logging.getLogger(__name__)

_API_BASE = "https://api.smartrecruiters.com/v1/companies"
_DETAIL_BUDGET_PER_BOARD = 12
_DETAIL_TITLE_MARKERS = (
    "data",
    "analytics",
    "analyst",
    "scientist",
    "machine learning",
    "ml ",
    " ai",
    "business intelligence",
    "bi ",
    "insights",
    "reporting",
    "forecast",
    "experiment",
    "mlops",
    "platform",
)


def _company_slug(board_url: str) -> str:
    parts = [p for p in (urlparse(board_url or "").path or "").split("/") if p]
    return parts[0].strip() if parts else ""


def _board_id(board_url: str) -> str:
    slug = _company_slug(board_url).lower()
    return f"smartrecruiters:{slug}" if slug else "smartrecruiters:"


def _extract_description(detail: dict) -> str:
    if not isinstance(detail, dict):
        return ""
    sections = detail.get("jobAd", {}).get("sections") or detail.get("sections") or []
    parts: list[str] = []
    if isinstance(sections, list):
        for section in sections:
            if not isinstance(section, dict):
                continue
            title = str(section.get("title") or section.get("name") or "").strip()
            text = str(section.get("text") or section.get("content") or "").strip()
            if text:
                cleaned = re.sub(r"<[^>]+>", " ", text)
                cleaned = re.sub(r"\s+", " ", cleaned).strip()
                parts.append(f"{title}: {cleaned}" if title else cleaned)
    structured = merge_text(
        parts,
        detail.get("description"),
        detail.get("jobDescription"),
        detail.get("descriptionTeaser"),
        detail.get("summary"),
        detail.get("responsibilities"),
        detail.get("qualifications"),
        detail.get("skills"),
    )
    structured = structured.strip()
    if looks_like_metadata_only_description(structured):
        return ""
    return structured


def _should_fetch_detail(title: str, *, label: str, budget_remaining: int) -> bool:
    if label in {"yes", "maybe"}:
        return True
    if budget_remaining <= 0:
        return False
    normalized = f" {str(title or '').strip().lower()} "
    return any(marker in normalized for marker in _DETAIL_TITLE_MARKERS)


class SmartRecruitersSource(BaseSource):
    """Fetches jobs from a single SmartRecruiters board."""

    def __init__(self, company: str, board_url: str) -> None:
        slug = _company_slug(board_url)
        self.name = f"smartrecruiters:{slug}"
        self.company = company
        self.board_url = board_url
        self._slug = slug
        self.board_id = _board_id(board_url)

    def fetch(self, seen_keys: set[str], timeout: int = 30) -> list[Job]:
        if not self._slug:
            return []

        sess = get_session("smartrecruiters")
        headers = {"accept": "application/json", "user-agent": "Mozilla/5.0"}

        all_raw: list[dict] = []
        offset = 0
        limit = 100
        safety_cap = 5000

        while True:
            url = f"{_API_BASE}/{self._slug}/postings"
            r = sess.get(url, params={"offset": offset, "limit": limit}, headers=headers, timeout=timeout)
            r.raise_for_status()

            data = r.json() if r.content else {}
            posts = data.get("content") or data.get("postings") or []
            if not isinstance(posts, list) or not posts:
                break

            all_raw.extend(posts)
            if len(all_raw) >= 500:
                break
            offset += len(posts)
            if offset >= safety_cap:
                break

        result: list[Job] = []
        detail_budget_remaining = _DETAIL_BUDGET_PER_BOARD
        for raw in all_raw:
            pid = str(raw.get("id") or raw.get("ref") or "")
            key = (
                f"smartrecruiters:{self._slug}:{pid}" if pid
                else f"smartrecruiters:{self._slug}:url:{raw.get('referrer','')}"
            )
            title = raw.get("name") or raw.get("jobTitle") or "Unknown Title"
            loc_obj = raw.get("location") or {}
            if isinstance(loc_obj, dict):
                loc = make_location([loc_obj.get("city"), loc_obj.get("region") or loc_obj.get("state"), loc_obj.get("country")])
            else:
                loc = str(loc_obj) if loc_obj else "Unknown Location"
            posted = raw.get("releasedDate") or raw.get("publicationDate") or raw.get("createdOn") or ""
            url_job = raw.get("referrer") or raw.get("applyUrl") or raw.get("url") or self.board_url
            description = ""
            cr = classify(title)
            fetch_detail = bool(pid) and _should_fetch_detail(title, label=cr.label, budget_remaining=detail_budget_remaining)
            if fetch_detail:
                try:
                    detail_url = f"{_API_BASE}/{self._slug}/postings/{pid}"
                    detail_resp = sess.get(detail_url, headers=headers, timeout=timeout)
                    detail_resp.raise_for_status()
                    description = _extract_description(detail_resp.json() if detail_resp.content else {})
                    if cr.label not in {"yes", "maybe"}:
                        detail_budget_remaining -= 1
                except Exception as exc:
                    log.debug("smartrecruiters:%s detail fetch failed for %s: %s", self._slug, pid, exc)
            result.append(Job(
                key=key, source="smartrecruiters", company=self.company,
                title=title, location=loc, url=url_job,
                posted=posted, description=description, score=cr.score, label=cr.label,
            ))

        log.debug("smartrecruiters:%s: fetched %d jobs", self._slug, len(result))
        return result
