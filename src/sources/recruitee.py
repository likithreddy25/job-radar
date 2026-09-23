"""Recruitee public careers site adapter."""
from __future__ import annotations

import logging
from urllib.parse import urljoin, urlparse

from ..utils.http import get_session
from .base import Job
from .generic_html import GenericHTMLBoardSource, board_id, parse_recruitee_jobs, slug_from_url

log = logging.getLogger(__name__)


def _board_id(board_url: str) -> str:
    return board_id("recruitee", board_url)


def _slug(board_url: str) -> str:
    return slug_from_url(board_url, "recruitee")


class RecruiteeSource(GenericHTMLBoardSource):
    platform = "recruitee"
    link_patterns = ("/o/", "/job/", "/careers/")

    def extract_listing_links(self, html: str, base_url: str) -> list[str]:
        links = super().extract_listing_links(html, base_url)
        return [link for link in links if "/api/" not in link]

    def fetch(self, seen_keys: set[str], timeout: int = 30) -> list[Job]:
        sess = get_session(self.platform)
        api_candidates = [
            urljoin(self.board_url + "/", "api/offers/"),
            urljoin(self.board_url + "/", "api/offers"),
        ]
        for url in api_candidates:
            try:
                resp = sess.get(url, timeout=timeout)
                resp.raise_for_status()
                payload = resp.json()
            except Exception:
                continue
            jobs = parse_recruitee_jobs(payload, self.company, _slug(self.board_url), self.board_url)
            if jobs:
                log.debug("recruitee:%s: fetched %d jobs via API", _slug(self.board_url), len(jobs))
                return jobs
        return super().fetch(seen_keys=seen_keys, timeout=timeout)
