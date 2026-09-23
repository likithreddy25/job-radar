"""Netflix careers source adapter."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
import re

from bs4 import BeautifulSoup

from ..classifier import classify
from ..utils.http import get_session
from .base import BaseSource, Job, clean_text, find_jobposting_ldjson, jobposting_location_text, merge_text

log = logging.getLogger(__name__)

_SEARCH_URL = "https://explore.jobs.netflix.net/careers"
_HEADERS = {
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "user-agent": "Mozilla/5.0",
    "referer": _SEARCH_URL,
}
_QUERIES = ["data", "analytics", "machine learning", "science"]


def _key(job: dict) -> str:
    job_id = str(job.get("id") or "")
    return f"netflix:{job_id}" if job_id else f"netflix:url:{job.get('external_link', '')}"


def _normalize(job: dict) -> dict:
    title = job.get("text") or job.get("title") or job.get("name") or "Unknown Title"

    loc_raw = job.get("location") or {}
    if isinstance(loc_raw, dict):
        loc = loc_raw.get("name") or "United States"
    elif isinstance(loc_raw, list) and loc_raw:
        first = loc_raw[0]
        loc = first if isinstance(first, str) else (first.get("name") if isinstance(first, dict) else "United States")
    else:
        loc = str(loc_raw) if loc_raw else "United States"

    posted = job.get("updated_at") or job.get("created_at") or ""
    if isinstance(job.get("t_update"), (int, float)) and not posted:
        posted = datetime.fromtimestamp(float(job["t_update"]), tz=timezone.utc).strftime("%Y-%m-%d")
    elif isinstance(job.get("t_create"), (int, float)) and not posted:
        posted = datetime.fromtimestamp(float(job["t_create"]), tz=timezone.utc).strftime("%Y-%m-%d")
    job_id = str(job.get("id") or "")
    external = job.get("external_link") or job.get("canonicalPositionUrl") or ""
    url = external if external else (f"https://explore.jobs.netflix.net/careers/job/{job_id}" if job_id else _SEARCH_URL)

    description = merge_text(
        job.get("description"),
        job.get("job_description"),
        job.get("summary"),
        job.get("responsibilities"),
        job.get("qualifications"),
        job.get("preferred_qualifications"),
        job.get("team"),
        job.get("business_unit"),
        job.get("department"),
        job.get("work_location_option"),
    )

    return {
        "key": _key(job),
        "title": str(title),
        "location": str(loc),
        "posted": str(posted),
        "url": url,
        "description": description,
    }


def _clean_location_name(text: str) -> str:
    value = clean_text(text)
    value = re.sub(r",\s*([A-Z]{2}),\s*\1\b", r", \1", value)
    value = re.sub(r"\s*,\s*", ", ", value)
    return value.strip(" ,")


def _parse_netflix_payload(html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    code = soup.find("code", id="smartApplyData")
    if code is None:
        return {}
    try:
        return json.loads(code.get_text())
    except Exception:
        return {}


def _extract_detail(sess, job: dict, timeout: int) -> dict:
    detail_url = job.get("external_link") or job.get("canonicalPositionUrl") or ""
    if not detail_url:
        return {}
    try:
        resp = sess.get(detail_url, headers=_HEADERS, timeout=timeout)
        resp.raise_for_status()
    except Exception as exc:
        log.debug("netflix: detail fetch failed for %s: %s", detail_url, exc)
        return {}

    soup = BeautifulSoup(resp.text, "html.parser")
    schema = find_jobposting_ldjson(resp.text)
    payload = _parse_netflix_payload(resp.text)
    positions = payload.get("positions") if isinstance(payload.get("positions"), list) else []
    position = positions[0] if positions and isinstance(positions[0], dict) else {}
    description = merge_text(
        schema.get("description"),
        position.get("description"),
        position.get("job_description"),
        position.get("summary"),
        position.get("responsibilities"),
        position.get("qualifications"),
        position.get("preferred_qualifications"),
        position.get("business_unit"),
        position.get("department"),
        position.get("work_location_option"),
    )
    if not description:
        for selector in ("main", "article", ".job-description", ".position-description"):
            node = soup.select_one(selector)
            if node is None:
                continue
            text = clean_text(node.get_text(" ", strip=True))
            if len(text) >= 120:
                description = text
                break
    location = jobposting_location_text(schema)
    if not location:
        locations = payload.get("all_applicable_locations")
        if isinstance(locations, list):
            location = " / ".join(
                _clean_location_name(str(item)) for item in locations if _clean_location_name(str(item))
            )
    return {
        "title": str(schema.get("title") or position.get("name") or "").strip(),
        "posted": str(schema.get("datePosted") or "").strip(),
        "location": location,
        "description": description,
    }


class NetflixSource(BaseSource):
    name = "netflix"

    def __init__(self, max_jobs: int = 200) -> None:
        self.max_jobs = max_jobs

    def fetch(self, seen_keys: set[str], timeout: int = 30) -> list[Job]:
        sess = get_session("main")
        all_raw: list[dict] = []
        seen_ids: set[str] = set()

        for query in _QUERIES:
            try:
                r = sess.get(
                    _SEARCH_URL,
                    headers=_HEADERS,
                    params={"query": query, "location": "United States"},
                    timeout=timeout,
                )
                r.raise_for_status()
                soup = BeautifulSoup(r.text, "html.parser")
                code = soup.find("code", id="smartApplyData")
                if code is None:
                    raise RuntimeError("Netflix smartApplyData payload was not found in the careers page.")
                payload = json.loads(code.get_text())
                jobs = payload.get("positions") or []
            except Exception as exc:
                log.warning("netflix: query=%r failed — %s", query, exc)
                continue

            for job in jobs:
                jid = str(job.get("id") or "")
                if jid and jid in seen_ids:
                    continue
                if jid:
                    seen_ids.add(jid)
                all_raw.append(job)
                if len(all_raw) >= self.max_jobs:
                    break
            if len(all_raw) >= self.max_jobs:
                break

        result: list[Job] = []
        for raw in all_raw:
            detail = _extract_detail(sess, raw, timeout)
            if detail:
                merged = dict(raw)
                if detail.get("title"):
                    merged["name"] = detail["title"]
                if detail.get("posted"):
                    merged["updated_at"] = detail["posted"]
                if detail.get("location"):
                    merged["location"] = detail["location"]
                if detail.get("description"):
                    merged["description"] = detail["description"]
                raw = merged
            n = _normalize(raw)
            cr = classify(n["title"])
            result.append(
                Job(
                    key=n["key"],
                    source=self.name,
                    company="Netflix",
                    title=n["title"],
                    location=n["location"],
                    url=n["url"],
                    posted=n["posted"],
                    description=n["description"],
                    score=cr.score,
                    label=cr.label,
                )
            )

        log.info("netflix: fetched %d positions", len(result))
        return result
