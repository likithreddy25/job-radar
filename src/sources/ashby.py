"""Ashby public job board source adapter."""
from __future__ import annotations

import logging
import re
from urllib.parse import urlparse

from ..classifier import classify
from ..utils.http import get_session
from .base import BaseSource, Job

log = logging.getLogger(__name__)

_API_BASE = "https://api.ashbyhq.com/posting-api/job-board"


def _slug(board_url: str) -> str:
    u = urlparse(board_url or "")
    parts = [p for p in (u.path or "").split("/") if p]
    if parts:
        return parts[0]
    host = (u.netloc or "").split(".")
    return host[0] if host else ""


def _board_id(board_url: str) -> str:
    slug = _slug(board_url)
    return f"ashby:{slug}" if slug else "ashby:"


def _clean_html(text: str) -> str:
    cleaned = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", cleaned).strip()


def _location(raw: dict) -> str:
    if raw.get("locationName"):
        return str(raw.get("locationName"))
    if isinstance(raw.get("locations"), list):
        names = [str(loc.get("locationName") or loc.get("name") or "").strip() for loc in raw["locations"] if isinstance(loc, dict)]
        names = [n for n in names if n]
        if names:
            return " / ".join(names[:3])
    if isinstance(raw.get("secondaryLocations"), list):
        names = [str(loc.get("locationName") or "").strip() for loc in raw["secondaryLocations"] if isinstance(loc, dict)]
        names = [n for n in names if n]
        if names:
            return " / ".join(names[:3])
    return "Unknown Location"


class AshbySource(BaseSource):
    def __init__(self, company: str, board_url: str) -> None:
        self.company = company
        self.board_url = board_url
        self._slug = _slug(board_url)
        self.board_id = _board_id(board_url)
        self.name = f"ashby:{self._slug}"

    def fetch(self, seen_keys: set[str], timeout: int = 30) -> list[Job]:
        if not self._slug:
            return []

        sess = get_session("ashby")
        url = f"{_API_BASE}/{self._slug}"
        resp = sess.get(url, params={"includeCompensation": "true"}, timeout=timeout)
        resp.raise_for_status()
        payload = resp.json() if resp.content else {}
        raw_jobs = payload.get("jobs") or []
        if not isinstance(raw_jobs, list):
            return []

        result: list[Job] = []
        for raw in raw_jobs:
            if not isinstance(raw, dict):
                continue
            if raw.get("isListed") is False:
                continue
            job_id = str(raw.get("id") or raw.get("jobId") or raw.get("slug") or "")
            title = str(raw.get("title") or "Unknown Title")
            location = _location(raw)
            posted = str(raw.get("publishedAt") or raw.get("updatedAt") or raw.get("createdAt") or "")
            job_url = (
                raw.get("jobUrl")
                or raw.get("absoluteUrl")
                or raw.get("applyUrl")
                or (f"https://jobs.ashbyhq.com/{self._slug}/{raw.get('slug')}" if raw.get("slug") else self.board_url)
            )
            description = _clean_html(str(raw.get("descriptionPlain") or raw.get("descriptionHtml") or raw.get("description") or ""))
            key = f"ashby:{self._slug}:{job_id}" if job_id else f"ashby:{self._slug}:url:{job_url}"
            cr = classify(title)
            result.append(
                Job(
                    key=key,
                    source="ashby",
                    company=self.company,
                    title=title,
                    location=location,
                    url=str(job_url),
                    posted=posted,
                    description=description,
                    score=cr.score,
                    label=cr.label,
                )
            )

        log.debug("ashby:%s: fetched %d jobs", self._slug, len(result))
        return result
