"""TikTok USDS careers source adapter.

This source uses the signed public search endpoint. It is disabled by default
because the request currently requires a fresh `_signature` plus session-shaped
headers/tokens from a browser session.
"""
from __future__ import annotations

import logging
import os

from ..classifier import classify
from ..utils.http import get_session
from .base import BaseSource, Job, merge_text

log = logging.getLogger(__name__)

_CAREERS_URL = "https://careers.tiktokusds.com/usds/position?keywords=&category=&location=&project=&type=1&job_hot_flag=&current=1&limit=10&functionCategory=&tag="
_API_BASE = "https://careers.tiktokusds.com/api/v1/search/job/posts"


def _env(name: str) -> str:
    return (os.environ.get(name, "") or "").strip()


def _extract_jobs(payload: object) -> list[dict]:
    if isinstance(payload, dict):
        for key in (
            "job_post_list",
            "posts",
            "job_list",
            "jobPosts",
            "data",
        ):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
            if isinstance(value, dict):
                nested = _extract_jobs(value)
                if nested:
                    return nested
    return []


class TikTokUSDSSource(BaseSource):
    name = "tiktok_usds"

    def __init__(self, max_jobs: int = 100) -> None:
        self.max_jobs = max_jobs

    def fetch(self, seen_keys: set[str], timeout: int = 30) -> list[Job]:
        signature = _env("TIKTOK_USDS_SIGNATURE")
        if not signature:
            log.warning("tiktok_usds: missing TIKTOK_USDS_SIGNATURE; source is disabled until a fresh signed request is configured.")
            return []

        sess = get_session("main")
        bootstrap = sess.get(_CAREERS_URL, timeout=timeout, headers={"accept": "text/html,application/xhtml+xml"})
        bootstrap.raise_for_status()

        csrf_token = _env("TIKTOK_USDS_CSRF_TOKEN") or sess.cookies.get("atsx-csrf-token", "")
        raw_cookie_header = _env("TIKTOK_USDS_COOKIE")
        if raw_cookie_header:
            for part in raw_cookie_header.split(";"):
                piece = part.strip()
                if "=" not in piece:
                    continue
                name, value = piece.split("=", 1)
                if name and value:
                    sess.cookies.set(name.strip(), value.strip(), domain="careers.tiktokusds.com")

        params = {
            "keyword": "",
            "limit": str(self.max_jobs),
            "offset": "0",
            "job_category_id_list": "",
            "tag_id_list": "",
            "location_code_list": "",
            "subject_id_list": "",
            "recruitment_id_list": "1",
            "portal_type": "10",
            "job_function_id_list": "",
            "storefront_id_list": "",
            "portal_entrance": "1",
            "_signature": signature,
        }
        headers = {
            "accept": "application/json, text/plain, */*",
            "content-type": "application/json",
            "origin": "https://careers.tiktokusds.com",
            "portal-channel": "tiktok",
            "portal-platform": "pc",
            "referer": _CAREERS_URL,
            "website-path": "usds",
        }
        if csrf_token:
            headers["x-csrf-token"] = csrf_token
        response = sess.post(_API_BASE, params=params, json={}, headers=headers, timeout=timeout)
        response.raise_for_status()
        payload = response.json()
        raw_jobs = _extract_jobs(payload)

        jobs: list[Job] = []
        for raw_job in raw_jobs[: self.max_jobs]:
            job_id = str(
                raw_job.get("id")
                or raw_job.get("job_id")
                or raw_job.get("post_id")
                or raw_job.get("requisition_id")
                or ""
            ).strip()
            title = str(raw_job.get("title") or raw_job.get("name") or "Unknown Title").strip()
            location = str(
                raw_job.get("location")
                or raw_job.get("location_name")
                or ((raw_job.get("location_obj") or {}) or {}).get("name")
                or "Unknown Location"
            ).strip()
            posted = str(
                raw_job.get("create_time")
                or raw_job.get("update_time")
                or raw_job.get("posted_at")
                or ""
            ).strip()
            description = merge_text(
                raw_job.get("description"),
                raw_job.get("job_description"),
                raw_job.get("requirement"),
                raw_job.get("requirements"),
                raw_job.get("responsibility"),
            )
            result = classify(title)
            jobs.append(
                Job(
                    key=f"tiktok_usds:{job_id}" if job_id else f"tiktok_usds:url:{title.lower()}",
                    source=self.name,
                    company="TikTok USDS",
                    title=title,
                    location=location,
                    url=_CAREERS_URL,
                    posted=posted,
                    description=description,
                    score=result.score,
                    label=result.label,
                )
            )

        log.info("tiktok_usds: fetched %d positions", len(jobs))
        return jobs
