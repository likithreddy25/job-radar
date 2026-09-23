"""Meta (Facebook) careers source adapter."""
from __future__ import annotations

import json
import logging
import re

from ..classifier import classify
from ..utils.http import get_session
from .base import BaseSource, Job, clean_text, extract_json_text_fields, extract_text_by_selectors, find_jobposting_ldjson, is_us_location, looks_like_metadata_only_description, merge_text

log = logging.getLogger(__name__)

_JOBSEARCH_URL = "https://www.metacareers.com/jobsearch"
_ENDPOINT = "https://www.metacareers.com/api/graphql/"
_HEADERS = {
    "accept": "application/json",
    "content-type": "application/x-www-form-urlencoded",
    "origin": "https://www.metacareers.com",
    "referer": _JOBSEARCH_URL,
    "user-agent": "Mozilla/5.0",
}
_DOC_ID = "26228555073499023"
_FRIENDLY_NAME = "CareersJobSearchInputDropdownDataQuery"
_LSD_RE = re.compile(r'\["LSD",\[\],\{"token":"([^"]+)"\}', re.S)


def _key(job: dict) -> str:
    job_id = str(job.get("id") or "")
    return f"meta:{job_id}" if job_id else f"meta:url:{job.get('url','')}"


def _normalize(job: dict) -> dict:
    title = job.get("title") or "Unknown Title"
    locations = job.get("locations") or []
    if isinstance(locations, list) and locations:
        loc = locations[0] if isinstance(locations[0], str) else str(locations[0])
    else:
        loc = "United States"
    posted = job.get("post_date") or job.get("updated_time") or ""
    job_id = str(job.get("id") or "")
    url = f"https://www.metacareers.com/jobs/{job_id}" if job_id else "https://www.metacareers.com/jobs"
    description = merge_text(
        job.get("description"),
        job.get("team"),
        job.get("teams"),
        job.get("responsibilities"),
        job.get("minimum_qualifications"),
        job.get("preferred_qualifications"),
        job.get("summary"),
    )
    return {
        "key": _key(job),
        "title": str(title),
        "location": str(loc),
        "posted": str(posted),
        "url": url,
        "description": description,
    }


def _fetch_detail_description(sess, url: str, timeout: int) -> str:
    if not url:
        return ""
    resp = sess.get(
        url,
        headers={"accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8", "user-agent": _HEADERS["user-agent"]},
        timeout=timeout,
    )
    resp.raise_for_status()
    html = resp.text or ""
    schema = find_jobposting_ldjson(html)
    detail = merge_text(
        clean_text(str(schema.get("description") or "")),
        extract_json_text_fields(
            html,
            [
                "description",
                "jobDescription",
                "descriptionTeaser",
                "minimumQualifications",
                "preferredQualifications",
                "responsibilities",
                "summary",
            ],
        ),
        extract_text_by_selectors(
            html,
            [
                '[data-testid="job-details"]',
                '[data-testid="job-description"]',
                'div[class*="job-description"]',
                'section[class*="description"]',
                'section[class*="qualif"]',
            ],
        ),
    )
    if looks_like_metadata_only_description(detail):
        return ""
    return detail


class MetaSource(BaseSource):
    name = "meta"

    def __init__(self, max_jobs: int = 200) -> None:
        self.max_jobs = max_jobs

    def fetch(self, seen_keys: set[str], timeout: int = 30) -> list[Job]:
        sess = get_session("main")

        try:
            landing = sess.get(_JOBSEARCH_URL, headers={"user-agent": _HEADERS["user-agent"]}, timeout=timeout)
            landing.raise_for_status()
            m = _LSD_RE.search(landing.text)
            if not m:
                raise RuntimeError("Meta LSD token was not found on the job search page.")
            lsd = m.group(1)

            variables = {"search_input": {"q": "data", "results_per_page": None}}
            payload = {
                "fb_api_caller_class": "RelayModern",
                "fb_api_req_friendly_name": _FRIENDLY_NAME,
                "variables": json.dumps(variables, ensure_ascii=True),
                "server_timestamps": "true",
                "doc_id": _DOC_ID,
                "lsd": lsd,
            }
            headers = dict(_HEADERS)
            headers["x-fb-friendly-name"] = _FRIENDLY_NAME
            headers["x-fb-lsd"] = lsd

            r = sess.post(_ENDPOINT, headers=headers, data=payload, timeout=timeout)
            r.raise_for_status()
            payload = r.json()
            jobs = (((payload.get("data") or {}).get("job_search_with_featured_jobs") or {}).get("all_jobs") or [])
        except Exception as exc:
            log.warning("meta: fetch failed — %s", exc)
            return []

        result: list[Job] = []
        for raw in jobs:
            n = _normalize(raw)
            if not is_us_location(n["location"]):
                continue
            if not n["description"]:
                try:
                    n["description"] = _fetch_detail_description(sess, n["url"], timeout)
                except Exception as exc:
                    log.debug("meta detail fetch failed for %s: %s", n["url"], exc)
            cr = classify(n["title"])
            result.append(Job(
                key=n["key"], source=self.name, company="Meta",
                title=n["title"], location=n["location"], url=n["url"],
                posted=n["posted"], description=n["description"], score=cr.score, label=cr.label,
            ))
            if self.max_jobs and len(result) >= self.max_jobs:
                break

        log.info("meta: fetched %d positions", len(result))
        return result
