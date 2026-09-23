"""Google careers source adapter."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
import re

from ..classifier import classify
from ..utils.http import get_session
from .base import BaseSource, Job, merge_text

log = logging.getLogger(__name__)

_SEARCH_URL = "https://www.google.com/about/careers/applications/jobs/results/"
_HEADERS = {
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "referer": "https://www.google.com/about/careers/applications/jobs/results/",
    "user-agent": "Mozilla/5.0",
}
_BASE_PARAMS = {
    "q": "data analyst OR data scientist OR data engineer OR analytics engineer OR business intelligence",
    "location": "United States",
    "page": 1,
}
_DATA_RE = re.compile(r"AF_initDataCallback\(\{key: 'ds:1'.*?data:(\[.*?\])\s*, sideChannel:", re.S)


def _key(job: dict) -> str:
    job_id = str(job.get("id") or job.get("job_id") or "")
    return f"google:{job_id}" if job_id else f"google:url:{job.get('apply_url','')}"


def _normalize(job: dict) -> dict:
    title = job.get("title") or "Unknown Title"
    locs = job.get("locations") or []
    if isinstance(locs, list) and locs:
        first = locs[0] if isinstance(locs[0], dict) else {}
        loc = first.get("display") or first.get("city") or "United States"
    else:
        loc = "United States"
    apply_url = job.get("apply_url") or job.get("url") or "https://careers.google.com/jobs/"
    posted = job.get("date") or ""
    description = merge_text(
        job.get("description"),
        job.get("responsibilities"),
        job.get("minimum_qualifications"),
        job.get("preferred_qualifications"),
        job.get("qualifications"),
        job.get("team"),
    )
    return {
        "key": _key(job),
        "title": str(title),
        "location": str(loc),
        "posted": str(posted),
        "url": apply_url,
        "description": description,
    }

def _extract_jobs(html: str) -> list[dict]:
    m = _DATA_RE.search(html)
    if not m:
        raise RuntimeError("Google jobs data block was not found in the page HTML.")

    data = json.loads(m.group(1))
    rows = data[0] if data and isinstance(data[0], list) else []
    result: list[dict] = []
    for row in rows:
        if not isinstance(row, list) or len(row) < 3:
            continue
        posted = ""
        posted_raw = row[12] if len(row) > 12 else None
        if isinstance(posted_raw, list) and posted_raw and isinstance(posted_raw[0], (int, float)):
            posted = datetime.fromtimestamp(float(posted_raw[0]), tz=timezone.utc).strftime("%Y-%m-%d")

        result.append(
            {
                "id": str(row[0]),
                "title": str(row[1]),
                "apply_url": str(row[2]),
                "responsibilities": row[3][1] if len(row) > 3 and isinstance(row[3], list) and len(row[3]) > 1 else "",
                "qualifications": row[4][1] if len(row) > 4 and isinstance(row[4], list) and len(row[4]) > 1 else "",
                "locations": [
                    {"display": str(loc[0])}
                    for loc in (row[9] if len(row) > 9 and isinstance(row[9], list) else [])
                    if isinstance(loc, list) and loc
                ],
                "description": row[10][1] if len(row) > 10 and isinstance(row[10], list) and len(row[10]) > 1 else "",
                "preferred_qualifications": row[18][1] if len(row) > 18 and isinstance(row[18], list) and len(row[18]) > 1 else "",
                "team": row[15][1] if len(row) > 15 and isinstance(row[15], list) and len(row[15]) > 1 else "",
                "minimum_qualifications": row[19][1] if len(row) > 19 and isinstance(row[19], list) and len(row[19]) > 1 else "",
                "date": posted,
            }
        )
    return result


class GoogleSource(BaseSource):
    name = "google"

    def __init__(self, max_jobs: int = 200) -> None:
        self.max_jobs = max_jobs

    def fetch(self, seen_keys: set[str], timeout: int = 30) -> list[Job]:
        sess = get_session("main")
        all_raw: list[dict] = []
        page = 1

        while True:
            params = dict(_BASE_PARAMS)
            params["page"] = page
            try:
                r = sess.get(_SEARCH_URL, params=params, headers=_HEADERS, timeout=timeout)
                r.raise_for_status()
                jobs = _extract_jobs(r.text)
            except Exception as exc:
                log.warning("google: page %d failed — %s", page, exc)
                break

            if not jobs:
                break

            all_raw.extend(jobs)
            if len(all_raw) >= self.max_jobs:
                all_raw = all_raw[:self.max_jobs]
                break

            if len(jobs) < 20:
                break
            page += 1

        result: list[Job] = []
        for raw in all_raw:
            n = _normalize(raw)
            cr = classify(n["title"])
            result.append(Job(
                key=n["key"], source=self.name, company="Google",
                title=n["title"], location=n["location"], url=n["url"],
                posted=n["posted"], description=n["description"], score=cr.score, label=cr.label,
            ))

        log.info("google: fetched %d positions", len(result))
        return result
