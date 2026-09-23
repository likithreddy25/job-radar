"""Google X careers source adapter."""
from __future__ import annotations

import html
import json
import logging
import re

from ..classifier import classify
from ..utils.http import get_session
from .base import BaseSource, Job, merge_text

log = logging.getLogger(__name__)

_CAREERS_URL = "https://x.company/careers/"
_PAYLOAD_RE = re.compile(
    r'<x-island[^>]+component="JobsListWidget"[^>]+props="(?P<payload>.*?)">',
    re.IGNORECASE | re.DOTALL,
)


def _department_anchor(name: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower())
    return cleaned.strip("-") or "all-positions"


def _extract_payload(html_text: str) -> dict:
    match = _PAYLOAD_RE.search(html_text or "")
    if not match:
        return {}
    raw_payload = html.unescape(match.group("payload"))
    try:
        payload = json.loads(raw_payload)
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


class GoogleXSource(BaseSource):
    name = "google_x"

    def __init__(self, max_jobs: int = 300) -> None:
        self.max_jobs = max_jobs

    def fetch(self, seen_keys: set[str], timeout: int = 30) -> list[Job]:
        sess = get_session("main")
        response = sess.get(_CAREERS_URL, timeout=timeout, headers={"accept": "text/html,application/xhtml+xml"})
        response.raise_for_status()
        payload = _extract_payload(response.text)
        departments = payload.get("departments") or []
        if not isinstance(departments, list):
            log.warning("google_x: JobsListWidget payload missing departments list")
            return []

        jobs: list[Job] = []
        for department in departments:
            if not isinstance(department, dict):
                continue
            department_name = str(department.get("name") or "").strip() or "Unknown Department"
            department_url = f"{_CAREERS_URL}#{_department_anchor(department_name)}"
            for raw_job in department.get("jobs") or []:
                if not isinstance(raw_job, dict):
                    continue
                job_id = str(raw_job.get("id") or "").strip()
                title = str(raw_job.get("title") or "Unknown Title").strip()
                location = str(((raw_job.get("location") or {}) or {}).get("name") or "Unknown Location").strip()
                description = merge_text(f"Department: {department_name}")
                result = classify(title)
                key = f"google_x:{job_id}" if job_id else f"google_x:url:{department_url}:{title.lower()}"
                jobs.append(
                    Job(
                        key=key,
                        source=self.name,
                        company="Google X",
                        title=title,
                        location=location,
                        url=department_url,
                        description=description,
                        score=result.score,
                        label=result.label,
                    )
                )
                if len(jobs) >= self.max_jobs:
                    log.info("google_x: fetched %d positions", len(jobs))
                    return jobs

        log.info("google_x: fetched %d positions", len(jobs))
        return jobs
