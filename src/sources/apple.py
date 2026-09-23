"""Apple careers source adapter."""
from __future__ import annotations

import json
import logging
import re

from ..classifier import classify
from ..utils.http import get_session
from .base import BaseSource, Job, merge_text

log = logging.getLogger(__name__)

_SEARCH_URL = "https://jobs.apple.com/en-us/search"
_HEADERS = {
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "referer": "https://jobs.apple.com/en-us/search",
    "user-agent": "Mozilla/5.0",
}
_HYDRATION_RE = re.compile(r'window\.__staticRouterHydrationData\s*=\s*JSON\.parse\("(.*?)"\);', re.S)


def _key(job: dict) -> str:
    job_id = str(job.get("positionId") or job.get("id") or "")
    return f"apple:{job_id}" if job_id else f"apple:url:{job.get('url','')}"


def _normalize(job: dict) -> dict:
    title = job.get("postingTitle") or job.get("title") or "Unknown Title"
    locs = job.get("locations") or []
    if isinstance(locs, list) and locs:
        first = locs[0] if isinstance(locs[0], dict) else {}
        loc = first.get("name") or "United States"
    else:
        loc = "United States"
    posted = job.get("postDateInGMT") or job.get("postDate") or ""
    job_id = str(job.get("positionId") or "")
    url = f"https://jobs.apple.com/en-us/details/{job_id}" if job_id else "https://jobs.apple.com/en-us/search"
    description = merge_text(
        job.get("description"),
        job.get("jobSummary"),
        job.get("roleDescription"),
        job.get("keyQualifications"),
        job.get("minimumQualifications"),
        job.get("preferredQualifications"),
        job.get("educationExperience"),
    )
    return {
        "key": _key(job),
        "title": str(title),
        "location": str(loc),
        "posted": str(posted),
        "url": url,
        "description": description,
    }


class AppleSource(BaseSource):
    name = "apple"

    def __init__(self, max_jobs: int = 200) -> None:
        self.max_jobs = max_jobs

    def fetch(self, seen_keys: set[str], timeout: int = 30) -> list[Job]:
        sess = get_session("main")
        all_raw: list[dict] = []
        page = 1

        while True:
            try:
                r = sess.get(
                    _SEARCH_URL,
                    headers=_HEADERS,
                    params={"location": "united-states-USA", "sort": "newest", "page": page},
                    timeout=timeout,
                )
                r.raise_for_status()
                m = _HYDRATION_RE.search(r.text)
                if not m:
                    raise RuntimeError("Apple hydration data was not found in the search page.")
                raw = m.group(1).encode("utf-8").decode("unicode_escape")
                data = json.loads(raw)
            except Exception as exc:
                log.warning("apple: page %d failed — %s", page, exc)
                break

            search = ((data.get("loaderData") or {}).get("search") or {})
            jobs = search.get("searchResults") or []
            if not jobs:
                break

            all_raw.extend(jobs)
            if len(all_raw) >= self.max_jobs:
                all_raw = all_raw[:self.max_jobs]
                break

            total = int(search.get("totalRecords") or 0)
            if len(all_raw) >= total:
                break
            page += 1

        result: list[Job] = []
        for raw in all_raw:
            n = _normalize(raw)
            cr = classify(n["title"])
            result.append(Job(
                key=n["key"], source=self.name, company="Apple",
                title=n["title"], location=n["location"], url=n["url"],
                posted=n["posted"], description=n["description"], score=cr.score, label=cr.label,
            ))

        log.info("apple: fetched %d positions", len(result))
        return result
