"""Workable public widget source adapter."""
from __future__ import annotations

import logging
import re
from urllib.parse import urlparse

from ..classifier import classify
from ..utils.http import get_session
from .base import BaseSource, Job, clean_text, make_location, merge_text

log = logging.getLogger(__name__)

_API_BASE = "https://apply.workable.com/api/v1/widget/accounts"


def _slug(board_url: str) -> str:
    parts = [p for p in (urlparse(board_url or "").path or "").split("/") if p]
    return parts[0] if parts else ""


def _board_id(board_url: str) -> str:
    slug = _slug(board_url)
    return f"workable:{slug}" if slug else "workable:"


def _clean_html(text: str) -> str:
    cleaned = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", cleaned).strip()


def _extract_location(raw: dict) -> str:
    location = raw.get("location")
    if isinstance(location, dict):
        return make_location(
            [
                location.get("city") or raw.get("location_city"),
                location.get("region") or location.get("state") or raw.get("location_region"),
                location.get("country") or location.get("country") or raw.get("country"),
            ]
        )
    if isinstance(location, str) and location.strip():
        return location.strip()
    return make_location(
        [
            raw.get("location_city"),
            raw.get("location_region"),
            raw.get("country"),
        ]
    ) or "Unknown Location"


def _extract_description(raw: dict) -> str:
    parts = []
    description_fields = (
        "description",
        "description_html",
        "requirements",
        "requirements_html",
        "benefits",
        "benefits_html",
        "employment_type",
        "experience",
        "role_description",
    )
    for field in description_fields:
        value = raw.get(field)
        if isinstance(value, str):
            parts.append(_clean_html(value))
        elif isinstance(value, (list, dict)):
            parts.append(merge_text(value))
    structured = merge_text(*parts)
    return clean_text(structured)


class WorkableSource(BaseSource):
    def __init__(self, company: str, board_url: str) -> None:
        self.company = company
        self.board_url = board_url
        self._slug = _slug(board_url)
        self.board_id = _board_id(board_url)
        self.name = f"workable:{self._slug}"

    def fetch(self, seen_keys: set[str], timeout: int = 30) -> list[Job]:
        if not self._slug:
            return []

        sess = get_session("workable")
        url = f"{_API_BASE}/{self._slug}"
        resp = sess.get(url, params={"details": "true"}, timeout=timeout)
        resp.raise_for_status()
        payload = resp.json() if resp.content else {}
        raw_jobs = payload.get("results") or payload.get("jobs") or []
        if not isinstance(raw_jobs, list):
            return []

        result: list[Job] = []
        for raw in raw_jobs:
            if not isinstance(raw, dict):
                continue
            shortcode = str(raw.get("shortcode") or raw.get("id") or "")
            title = str(raw.get("title") or raw.get("name") or "Unknown Title")
            location = _extract_location(raw)
            posted = str(raw.get("published") or raw.get("created_at") or raw.get("updated_at") or "")
            description = _extract_description(raw)
            job_url = (
                raw.get("url")
                or raw.get("application_url")
                or (f"https://apply.workable.com/{self._slug}/j/{shortcode}/" if shortcode else self.board_url)
            )
            key = f"workable:{self._slug}:{shortcode}" if shortcode else f"workable:{self._slug}:url:{job_url}"
            cr = classify(title)
            result.append(
                Job(
                    key=key,
                    source="workable",
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

        log.debug("workable:%s: fetched %d jobs", self._slug, len(result))
        return result
