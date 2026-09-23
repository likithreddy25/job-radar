"""LinkedIn Jobs source adapter using the public guest search API.

⚠️  IMPORTANT LIMITATIONS:
    - LinkedIn aggressively rate-limits automated requests.
    - GitHub Actions cloud IPs are frequently blocked (429 / CAPTCHA).
    - This source works best when run locally on your own machine.
    - If you see consistent 429/403 errors in GitHub Actions, disable this
      source in config.yaml (linkedin: enabled: false) and run it manually.
    - Max 1 page (25 jobs) by default to stay under rate limits.
"""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone

from ..classifier import classify
from ..job_intelligence import to_structured_json
from ..utils.http import get_session
from .base import BaseSource, Job, clean_text

log = logging.getLogger(__name__)

_SEARCH_URL = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
_HEADERS = {
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "accept-language": "en-US,en;q=0.9",
    "user-agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "referer": "https://www.linkedin.com/jobs/search/",
}

# LinkedIn geoId for "United States"
_GEO_US = "103644278"

# Search queries to run — LinkedIn searches one keyword set at a time
_QUERIES = [
    "data analyst",
    "data scientist",
    "data engineer",
    "analytics engineer",
    "business intelligence analyst",
    "machine learning engineer",
]

# Delay between pages to avoid rate limits (seconds)
_PAGE_DELAY = 3.0
_MAX_AGE_HOURS = 24
_VERIFIED_BADGE_LABELS = {
    "verified",
    "verified employer",
    "verified company",
}


def _parse_posted_value(raw_value: str, *, now: datetime) -> datetime | None:
    text = (raw_value or "").strip().lower()
    if not text:
        return None

    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        try:
            # LinkedIn often exposes only a date. Treat it as end-of-day UTC so
            # "today" and late "yesterday" jobs are not dropped too aggressively.
            dt = datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            return dt + timedelta(hours=23, minutes=59, seconds=59)
        except ValueError:
            return None

    m = re.search(r"(\d+)\s+(minute|minutes|hour|hours|day|days)\s+ago", text)
    if m:
        qty = int(m.group(1))
        unit = m.group(2)
        if unit.startswith("minute"):
            return now - timedelta(minutes=qty)
        if unit.startswith("hour"):
            return now - timedelta(hours=qty)
        return now - timedelta(days=qty)

    if "today" in text or "just now" in text:
        return now
    if "yesterday" in text:
        return now - timedelta(days=1)

    return None


def _format_posted(dt: datetime | None, fallback: str) -> str:
    if dt is None:
        return fallback
    return dt.astimezone(timezone.utc).isoformat()


def _html_has_verified_badge(html: str) -> bool:
    if not html:
        return False
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        lowered = clean_text(html).lower()
        return "verified employer" in lowered or "verified company" in lowered

    soup = BeautifulSoup(html, "html.parser")
    for node in soup.select("span, div, small, strong"):
        text = clean_text(node.get_text(" ", strip=True)).lower()
        if text in _VERIFIED_BADGE_LABELS:
            return True

    body_text = clean_text(soup.get_text(" ", strip=True)).lower()
    return "verified employer" in body_text or "verified company" in body_text


def _fetch_detail(sess, job_id: str, timeout: int) -> tuple[str, bool]:
    if not job_id:
        return "", False
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return "", False

    detail_url = f"https://www.linkedin.com/jobs/view/{job_id}"
    try:
        resp = sess.get(detail_url, headers=_HEADERS, timeout=timeout)
        resp.raise_for_status()
    except Exception as exc:
        log.debug("linkedin: detail fetch failed for %s: %s", job_id, exc)
        return "", False

    soup = BeautifulSoup(resp.text, "html.parser")
    verified = _html_has_verified_badge(resp.text)
    for selector in (
        "div.show-more-less-html__markup",
        "div.description__text",
        "section.show-more-less-html",
    ):
        node = soup.select_one(selector)
        if node is not None:
            return clean_text(node.get_text(" ", strip=True)), verified
    return "", verified


def _parse_cards(html: str) -> list[dict]:
    """Parse LinkedIn job card HTML and return list of raw job dicts."""
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        log.error("linkedin: beautifulsoup4 not installed. Run: pip install beautifulsoup4")
        return []

    soup = BeautifulSoup(html, "html.parser")
    cards = soup.find_all("div", class_="base-card")
    results = []

    now = datetime.now(timezone.utc)
    for card in cards:
        try:
            # Extract job ID from data-entity-urn="urn:li:jobPosting:1234567890"
            urn = card.get("data-entity-urn", "")
            job_id = ""
            m = re.search(r"jobPosting:(\d+)", urn)
            if m:
                job_id = m.group(1)

            title_el = card.find(class_="base-search-card__title")
            company_el = card.find(class_="base-search-card__subtitle")
            location_el = card.find(class_="job-search-card__location")
            date_el = card.find("time")

            title = title_el.get_text(strip=True) if title_el else ""
            company = company_el.get_text(strip=True) if company_el else "Unknown"
            location = location_el.get_text(strip=True) if location_el else "United States"
            posted_attr = date_el.get("datetime", "") if date_el else ""
            posted_text = date_el.get_text(strip=True) if date_el else ""
            posted_dt = _parse_posted_value(posted_text or posted_attr, now=now)
            posted = _format_posted(posted_dt, posted_attr or posted_text)

            if not title or not job_id:
                continue

            results.append({
                "job_id": job_id,
                "title": title,
                "company": company,
                "location": location,
                "posted": posted,
                "url": f"https://www.linkedin.com/jobs/view/{job_id}",
                "verified": _html_has_verified_badge(str(card)),
            })
        except Exception:
            continue

    return results


class LinkedInSource(BaseSource):
    name = "linkedin"

    def __init__(self, max_jobs: int = 100) -> None:
        # Default low cap — LinkedIn blocks aggressively
        self.max_jobs = max_jobs

    def fetch(self, seen_keys: set[str], timeout: int = 30) -> list[Job]:
        sess = get_session("main")
        all_raw: list[dict] = []
        seen_ids: set[str] = set()

        for query in _QUERIES:
            if len(all_raw) >= self.max_jobs:
                break

            params = {
                "keywords": query,
                "location": "United States",
                "geoId": _GEO_US,
                "f_TPR": "r86400",   # posted in last 24 hours
                "start": 0,
                "count": 25,
            }

            try:
                r = sess.get(_SEARCH_URL, headers=_HEADERS, params=params, timeout=timeout)

                if r.status_code == 429:
                    log.warning("linkedin: rate limited (429) — skipping remaining queries")
                    break
                if r.status_code in (403, 999):
                    log.warning("linkedin: blocked (HTTP %d) — LinkedIn may be blocking cloud IPs", r.status_code)
                    break

                r.raise_for_status()
                cards = _parse_cards(r.text)

                for card in cards:
                    jid = card["job_id"]
                    if jid not in seen_ids:
                        seen_ids.add(jid)
                        all_raw.append(card)

                log.debug("linkedin: query=%r fetched %d cards", query, len(cards))

            except Exception as exc:
                log.warning("linkedin: query=%r failed — %s", query, exc)

            # Polite delay between queries
            time.sleep(_PAGE_DELAY)

        if len(all_raw) > self.max_jobs:
            all_raw = all_raw[: self.max_jobs]

        result: list[Job] = []
        cutoff = datetime.now(timezone.utc) - timedelta(hours=_MAX_AGE_HOURS)
        for raw in all_raw:
            posted = raw["posted"]
            if posted:
                posted_dt = _parse_posted_value(posted, now=datetime.now(timezone.utc))
                if posted_dt is not None and posted_dt < cutoff:
                    continue
            key = f"linkedin:{raw['job_id']}"
            cr = classify(raw["title"])
            description, detail_verified = _fetch_detail(sess, raw["job_id"], timeout)
            structured: dict[str, object] = {}
            if raw.get("verified") or detail_verified:
                structured["linkedin_verified"] = True
            result.append(
                Job(
                    key=key,
                    source=self.name,
                    company=raw["company"],
                    title=raw["title"],
                    location=raw["location"],
                    url=raw["url"],
                    posted=posted,
                    description=description,
                    score=cr.score,
                    label=cr.label,
                )
            )
            if structured:
                result[-1].structured_json = to_structured_json(structured)

        log.info("linkedin: fetched %d positions", len(result))
        return result
