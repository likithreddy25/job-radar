"""Breezy HR public career portal adapter."""
from __future__ import annotations

import logging
from urllib.parse import urljoin

from ..classifier import classify
from ..utils.http import get_session
from .base import Job, flatten_text
from .generic_html import GenericHTMLBoardSource, board_id, slug_from_url

log = logging.getLogger(__name__)


def _board_id(board_url: str) -> str:
    return board_id("breezyhr", board_url)


def _slug(board_url: str) -> str:
    return slug_from_url(board_url, "breezyhr")


class BreezyHRSource(GenericHTMLBoardSource):
    platform = "breezyhr"
    link_patterns = ("/p/",)

    def fetch(self, seen_keys: set[str], timeout: int = 30) -> list[Job]:
        sess = get_session(self.platform)
        api_candidates = [
            f"{self.board_url}?output=json",
            urljoin(self.board_url + "/", "?output=json"),
        ]
        for url in api_candidates:
            try:
                resp = sess.get(url, timeout=timeout)
                resp.raise_for_status()
                payload = resp.json()
            except Exception:
                continue
            if not isinstance(payload, list):
                continue
            jobs: list[Job] = []
            for raw in payload:
                if not isinstance(raw, dict):
                    continue
                title = str(raw.get("name") or raw.get("title") or "Unknown Title")
                location = flatten_text(raw.get("location") or raw.get("location_name"))
                job_url = str(raw.get("url") or raw.get("absolute_url") or raw.get("apply_url") or self.board_url)
                posted = str(raw.get("date") or raw.get("created_at") or raw.get("published_at") or "")
                description = flatten_text(raw.get("description") or raw.get("content"))
                job_id = str(raw.get("id") or raw.get("_id") or "")
                cr = classify(title)
                jobs.append(
                    Job(
                        key=f"breezyhr:{_slug(self.board_url)}:{job_id}" if job_id else f"breezyhr:{_slug(self.board_url)}:url:{job_url}",
                        source="breezyhr",
                        company=self.company,
                        title=title,
                        location=location or "Unknown Location",
                        url=job_url,
                        posted=posted,
                        description=description,
                        score=cr.score,
                        label=cr.label,
                    )
                )
            if jobs:
                log.debug("breezyhr:%s: fetched %d jobs via JSON", _slug(self.board_url), len(jobs))
                return jobs
        return super().fetch(seen_keys=seen_keys, timeout=timeout)
