"""Jobvite public careers board source adapter."""
from __future__ import annotations

import logging
import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from ..classifier import classify
from ..utils.http import get_session
from .base import BaseSource, Job, clean_text, find_jobposting_ldjson, jobposting_location_text, merge_text

log = logging.getLogger(__name__)

_HEADERS = {
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "user-agent": "Mozilla/5.0",
}


def _slug(board_url: str) -> str:
    parts = [part for part in (urlparse(board_url or "").path or "").split("/") if part]
    for part in parts:
        if part.lower() != "jobs":
            return part
    return parts[0] if parts else ""


def _board_id(board_url: str) -> str:
    slug = _slug(board_url)
    return f"jobvite:{slug}" if slug else "jobvite:"


def _listing_url(board_url: str) -> str:
    parsed = urlparse(board_url)
    parts = [part for part in (parsed.path or "").split("/") if part]
    if not parts:
        path = "/job"
    elif parts[-1].lower() in {"job", "jobs"}:
        path = "/" + "/".join(parts)
    else:
        path = "/" + "/".join(parts + ["job"])
    return parsed._replace(path=path, query="", fragment="").geturl()


def _job_key(slug: str, url: str) -> str:
    match = re.search(r"/job/([^/?#]+)", url)
    job_id = match.group(1) if match else ""
    return f"jobvite:{slug}:{job_id}" if job_id else f"jobvite:{slug}:url:{url}"


def _find_job_links(html: str, base_url: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    links: list[str] = []
    seen: set[str] = set()
    base_host = urlparse(base_url).netloc.lower()
    for anchor in soup.select("a[href]"):
        href = anchor.get("href") or ""
        if "/job/" not in href:
            continue
        absolute = urljoin(base_url, href)
        parsed = urlparse(absolute)
        if parsed.netloc.lower() != base_host:
            continue
        normalized = parsed._replace(fragment="").geturl()
        if normalized in seen:
            continue
        seen.add(normalized)
        links.append(normalized)
    return links


def _extract_description(soup: BeautifulSoup, schema: dict) -> str:
    if schema:
        description = clean_text(str(schema.get("description") or ""))
        if description:
            return description
    for selector in (
        "article",
        "main",
        "[data-automation='job-description']",
        ".job-description",
        ".jv-job-detail-description",
    ):
        node = soup.select_one(selector)
        if node is not None:
            text = clean_text(node.get_text(" ", strip=True))
            if len(text) >= 120:
                return text
    return ""


def _extract_detail(sess, company: str, slug: str, url: str, timeout: int) -> Job | None:
    resp = sess.get(url, headers=_HEADERS, timeout=timeout)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    schema = find_jobposting_ldjson(resp.text)

    title = str(schema.get("title") or "").strip()
    if not title:
        title_node = soup.select_one("h1")
        title = clean_text(title_node.get_text(" ", strip=True)) if title_node is not None else "Unknown Title"

    location = jobposting_location_text(schema)
    if not location:
        text = clean_text(soup.get_text("\n", strip=True))
        match = re.search(r"(?:Location|Locations)\s*[:\-]?\s*([A-Z][^\n|]{2,120})", text)
        location = match.group(1).strip() if match else "Unknown Location"

    posted = str(schema.get("datePosted") or "").strip()
    description = merge_text(
        _extract_description(soup, schema),
        schema.get("employmentType"),
        schema.get("qualifications"),
        schema.get("responsibilities"),
    )
    result = classify(title)
    return Job(
        key=_job_key(slug, url),
        source="jobvite",
        company=company,
        title=title,
        location=location or "Unknown Location",
        url=url,
        posted=posted,
        description=description,
        score=result.score,
        label=result.label,
    )


class JobviteSource(BaseSource):
    def __init__(self, company: str, board_url: str) -> None:
        self.company = company
        self.board_url = board_url
        self._slug = _slug(board_url)
        self.board_id = _board_id(board_url)
        self.name = f"jobvite:{self._slug}"

    def fetch(self, seen_keys: set[str], timeout: int = 30) -> list[Job]:
        if not self._slug:
            return []

        sess = get_session("boards")
        listing_url = _listing_url(self.board_url)
        resp = sess.get(listing_url, headers=_HEADERS, timeout=timeout)
        resp.raise_for_status()
        detail_urls = _find_job_links(resp.text, resp.url)

        jobs: list[Job] = []
        for detail_url in detail_urls:
            try:
                job = _extract_detail(sess, self.company, self._slug, detail_url, timeout)
            except Exception as exc:
                log.debug("jobvite:%s detail failed for %s: %s", self._slug, detail_url, exc)
                continue
            if job is None:
                continue
            jobs.append(job)

        log.debug("jobvite:%s: fetched %d jobs", self._slug, len(jobs))
        return jobs
