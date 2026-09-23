"""Shared helpers for HTML-first public job board adapters."""
from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import re
from typing import Iterable, Optional
from urllib.parse import parse_qs, urljoin, urlparse

from bs4 import BeautifulSoup

from ..classifier import classify
from ..utils.http import get_session
from .base import (
    BaseSource,
    Job,
    clean_text,
    extract_json_text_fields,
    extract_text_by_selectors,
    find_jobposting_ldjson,
    flatten_text,
    jobposting_location_text,
    looks_like_metadata_only_description,
)

log = logging.getLogger(__name__)


def slug_from_url(board_url: str, fallback_prefix: str) -> str:
    parsed = urlparse(board_url or "")
    host = (parsed.netloc or "").lower()
    if host:
        host = host.split("@")[-1]
        for prefix in ("www.", "jobs.", "careers.", "apply."):
            if host.startswith(prefix):
                host = host[len(prefix):]
        host = host.replace(".", "-")
    path_parts = [part for part in (parsed.path or "").split("/") if part]
    path_slug = "-".join(path_parts[:2]) if path_parts else ""
    slug = host or path_slug or fallback_prefix
    return re.sub(r"[^a-z0-9-]+", "-", slug).strip("-")


def board_id(prefix: str, board_url: str) -> str:
    slug = slug_from_url(board_url, prefix)
    return f"{prefix}:{slug}" if slug else f"{prefix}:"


def _same_host(url_a: str, url_b: str) -> bool:
    host_a = (urlparse(url_a or "").netloc or "").lower()
    host_b = (urlparse(url_b or "").netloc or "").lower()
    return bool(host_a) and host_a == host_b


def extract_links_by_patterns(html: str, base_url: str, patterns: Iterable[str]) -> list[str]:
    soup = BeautifulSoup(html or "", "html.parser")
    found: list[str] = []
    seen: set[str] = set()
    lowered_patterns = tuple(pattern.lower() for pattern in patterns if pattern)
    for anchor in soup.find_all("a", href=True):
        href = str(anchor.get("href") or "").strip()
        if not href or href.startswith("#") or href.lower().startswith("javascript:"):
            continue
        absolute = urljoin(base_url, href)
        if lowered_patterns and not any(pattern in absolute.lower() for pattern in lowered_patterns):
            continue
        if not _same_host(base_url, absolute):
            continue
        normalized = absolute.rstrip("/")
        if normalized in seen:
            continue
        seen.add(normalized)
        found.append(normalized)
    return found


def page_title(html: str) -> str:
    soup = BeautifulSoup(html or "", "html.parser")
    if soup.title and soup.title.string:
        return clean_text(soup.title.string)
    heading = soup.find(["h1", "h2"])
    if heading:
        return clean_text(heading.get_text(" ", strip=True))
    return ""


def posted_from_ldjson(schema: dict) -> str:
    return str(
        schema.get("datePosted")
        or schema.get("validThrough")
        or schema.get("datePublished")
        or ""
    )


def description_from_ldjson(schema: dict) -> str:
    return clean_text(str(schema.get("description") or ""))


def extract_description(html: str) -> str:
    selectors = [
        "[data-testid='job-description']",
        "[class*='job-description']",
        "[class*='description']",
        "main article",
        "main section",
        "article",
    ]
    description = extract_text_by_selectors(html, selectors, max_nodes=8)
    if description and not looks_like_metadata_only_description(description):
        return description
    json_text = extract_json_text_fields(
        html,
        [
            "description",
            "content",
            "body",
            "jobDescription",
            "descriptionPlain",
            "description_text",
        ],
    )
    if json_text and not looks_like_metadata_only_description(json_text):
        return json_text
    return description


def extract_location(html: str, schema: dict) -> str:
    location = jobposting_location_text(schema)
    if location:
        return location
    selectors = [
        "[data-testid='job-location']",
        "[class*='location']",
        "[class*='job-location']",
        "meta[property='og:locale']",
    ]
    text = extract_text_by_selectors(html, selectors, max_nodes=3)
    return text or "Unknown Location"


@dataclass
class JobPageDetails:
    title: str
    location: str
    posted: str
    description: str


def parse_job_page(html: str, fallback_title: str = "") -> JobPageDetails:
    schema = find_jobposting_ldjson(html)
    title = str(schema.get("title") or schema.get("name") or "").strip() or fallback_title or page_title(html) or "Unknown Title"
    location = extract_location(html, schema)
    posted = posted_from_ldjson(schema)
    description = description_from_ldjson(schema) or extract_description(html)
    return JobPageDetails(
        title=title,
        location=location,
        posted=posted,
        description=description,
    )


class GenericHTMLBoardSource(BaseSource):
    """HTML-first adapter for public board pages that expose job links."""

    platform: str = "generic"
    link_patterns: tuple[str, ...] = ()

    def __init__(self, company: str, board_url: str) -> None:
        self.company = company
        self.board_url = board_url.rstrip("/")
        slug = slug_from_url(board_url, self.platform)
        self.name = f"{self.platform}:{slug}"
        self.board_id = board_id(self.platform, board_url)

    def listing_urls(self) -> list[str]:
        return [self.board_url]

    def extract_listing_links(self, html: str, base_url: str) -> list[str]:
        return extract_links_by_patterns(html, base_url, self.link_patterns)

    def _job_key(self, job_url: str) -> str:
        return f"{self.platform}:{slug_from_url(self.board_url, self.platform)}:url:{job_url}"

    def _detail_job(self, job_url: str, timeout: int) -> Optional[Job]:
        sess = get_session(self.platform)
        resp = sess.get(job_url, timeout=timeout)
        resp.raise_for_status()
        details = parse_job_page(resp.text)
        cr = classify(details.title)
        return Job(
            key=self._job_key(job_url),
            source=self.platform,
            company=self.company,
            title=details.title,
            location=details.location,
            url=job_url,
            posted=details.posted,
            description=details.description,
            score=cr.score,
            label=cr.label,
        )

    def fetch(self, seen_keys: set[str], timeout: int = 30) -> list[Job]:
        sess = get_session(self.platform)
        all_links: list[str] = []
        seen_links: set[str] = set()
        for url in self.listing_urls():
            resp = sess.get(url, timeout=timeout)
            resp.raise_for_status()
            for link in self.extract_listing_links(resp.text, url):
                if link in seen_links:
                    continue
                seen_links.add(link)
                all_links.append(link)

        results: list[Job] = []
        for job_url in all_links:
            try:
                job = self._detail_job(job_url, timeout)
            except Exception as exc:
                log.debug("%s detail failed for %s: %s", self.name, job_url, exc)
                continue
            if job is not None:
                results.append(job)

        log.debug("%s: fetched %d jobs", self.name, len(results))
        return results


class GenericJobsHTMLBoardSource(GenericHTMLBoardSource):
    """Conservative generic adapter that only follows job/career-like links."""

    platform = "generic_html"
    link_patterns = (
        "/careers",
        "/career",
        "/jobs",
        "/job",
        "/positions",
        "/openings",
        "/roles",
    )


def parse_recruitee_jobs(payload: object, company: str, platform_slug: str, board_url: str) -> list[Job]:
    jobs: list[Job] = []
    raw_jobs = payload.get("offers") if isinstance(payload, dict) else payload
    if not isinstance(raw_jobs, list):
        return jobs
    for raw in raw_jobs:
        if not isinstance(raw, dict):
            continue
        title = str(raw.get("title") or raw.get("name") or "Unknown Title")
        locations = raw.get("locations") or raw.get("location") or []
        if isinstance(locations, list):
            location = " / ".join(
                str(loc.get("name") or loc.get("city") or "").strip()
                for loc in locations
                if isinstance(loc, dict) and str(loc.get("name") or loc.get("city") or "").strip()
            )
        else:
            location = flatten_text(locations)
        job_id = str(raw.get("id") or raw.get("uuid") or "")
        job_url = str(
            raw.get("careers_url")
            or raw.get("careers_apply_url")
            or raw.get("apply_url")
            or raw.get("url")
            or board_url
        )
        posted = str(raw.get("published_at") or raw.get("updated_at") or raw.get("created_at") or "")
        description = flatten_text(
            raw.get("description")
            or raw.get("description_html")
            or raw.get("description_text")
            or raw.get("requirements")
        )
        cr = classify(title)
        jobs.append(
            Job(
                key=f"recruitee:{platform_slug}:{job_id}" if job_id else f"recruitee:{platform_slug}:url:{job_url}",
                source="recruitee",
                company=company,
                title=title,
                location=location or "Unknown Location",
                url=job_url,
                posted=posted,
                description=description,
                score=cr.score,
                label=cr.label,
            )
        )
    return jobs


def normalize_slug_from_query(url: str, keys: Iterable[str]) -> str:
    parsed = urlparse(url or "")
    query = parse_qs(parsed.query)
    for key in keys:
        values = query.get(key) or []
        if values and values[0].strip():
            return values[0].strip()
    return ""
