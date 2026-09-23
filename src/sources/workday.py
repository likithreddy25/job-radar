"""Workday CXS board source adapter with URL normalization."""
from __future__ import annotations

import logging
import re
from urllib.parse import urlparse

from ..classifier import classify
from ..utils.http import get_session
from .base import (
    BaseSource,
    Job,
    clean_text,
    extract_json_text_fields,
    extract_text_by_selectors,
    find_jobposting_ldjson,
    looks_like_metadata_only_description,
    make_location,
    merge_text,
)

log = logging.getLogger(__name__)

_LOCALE_RE = re.compile(r"^[a-z]{2}[-_][a-zA-Z]{2}$")
_HEADERS = {"accept": "application/json", "content-type": "application/json", "user-agent": "Mozilla/5.0"}
_HTML_HEADERS = {"accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8", "user-agent": "Mozilla/5.0"}
_WML_APP_ERROR = "<wml:Application_Error"
_EXTRA_SHALLOW_DETAIL_BUDGET = 8

_DETAIL_SKIP_TITLE_MARKERS = (
    "warehouse",
    "stock & fulfillment",
    "stock and fulfillment",
    "cashier",
    "merchandiser",
    "retail",
    "sales floor",
    "software engineer",
    "senior software engineer",
    "associate business partner",
    "solutions architect",
    "enterprise architect",
    "technical architect",
    "software architect",
)
_SECOND_CHANCE_DETAIL_MARKERS = (
    "data",
    "analytics",
    "analyst",
    "scientist",
    "machine learning",
    " ml ",
    "business intelligence",
    " bi ",
    "insights",
    "reporting",
    "forecast",
    "experiment",
    "mlops",
    "platform",
    "ai ",
)
def _canon_locale(seg: str) -> str:
    s = (seg or "").strip().replace("_", "-")
    parts = s.split("-")
    if len(parts) == 2:
        return f"{parts[0].lower()}-{parts[1].upper()}"
    return s


def _parse(board_url: str) -> tuple[str, str, str]:
    """Return (origin, tenant, site) from a Workday board URL."""
    u = urlparse(board_url)
    host = u.netloc
    tenant = host.split(".")[0]
    origin = f"{u.scheme}://{host}"
    segs = [s for s in (u.path or "").split("/") if s]
    if not segs:
        raise ValueError(f"Workday board_url has no path: {board_url}")
    site = segs[1] if (len(segs) >= 2 and _LOCALE_RE.match(segs[0])) else segs[0]
    return origin, tenant, site


def _board_id(board_url: str) -> str:
    try:
        _, tenant, site = _parse(board_url)
        return f"workday:{tenant}:{site}"
    except ValueError:
        return "workday:"


def _locale(board_url: str) -> str:
    segs = [s for s in (urlparse(board_url).path or "").split("/") if s]
    if segs and _LOCALE_RE.match(segs[0]):
        return _canon_locale(segs[0]) or "en-US"
    return "en-US"


def _normalize_url(board_url: str, ext: str) -> str:
    """Normalize Workday external job paths into full clickable URLs."""
    ext = (ext or "").strip()
    if not ext:
        return ""

    locale = _locale(board_url)
    try:
        _, _, site = _parse(board_url)
    except ValueError:
        return ext

    bu = urlparse(board_url)
    base_host = bu.netloc

    if ext.startswith("http"):
        eu = urlparse(ext)
        host = eu.netloc or base_host
        path = eu.path or ""
    else:
        host = base_host
        path = ext

    if not host or not path.startswith("/"):
        return ext

    segs = [s for s in path.split("/") if s]

    if len(segs) >= 2 and _LOCALE_RE.match(segs[0]) and site and segs[1] == site:
        # Already in /locale/site/... form — canonicalize locale
        canon0 = _canon_locale(segs[0])
        if canon0 != segs[0]:
            segs[0] = canon0
        new_path = "/" + "/".join(segs)
    elif len(segs) >= 2 and _LOCALE_RE.match(segs[0]) and site and segs[1] in {"job", "jobs"}:
        # /locale/job/... — insert site
        rest = "/".join(segs[1:])
        new_path = f"/{_canon_locale(segs[0]) or segs[0]}/{site}/{rest}"
    elif len(segs) >= 1 and site and segs[0] == site:
        # /site/... — insert locale
        new_path = f"/{locale}{path}"
    elif len(segs) >= 1 and segs[0] in {"job", "jobs"} and site:
        # /job/... — insert locale + site
        new_path = f"/{locale}/{site}{path}"
    else:
        new_path = path

    return f"https://{host}{new_path}"


def _canonical_req_id(value: str) -> str:
    raw = (value or "").strip()
    if not raw:
        return ""
    patterns = (
        r"\b(R-\d+)(?:-\d+)?\b",
        r"\b(RQ\d+)(?:-\d+)?\b",
        r"\b(JR\d+)(?:-\d+)?\b",
        r"\b(P\d+)(?:-\d+)?\b",
        r"\b(R\d+)(?:-\d+)?\b",
        r"\b(\d{6,})(?:-\d+)?\b",
    )
    for pattern in patterns:
        match = re.search(pattern, raw, re.I)
        if match:
            return match.group(1).upper()
    return raw


def _fetch_detail_description(sess, url: str, timeout: int) -> str:
    if not url or not url.startswith("http"):
        return ""
    resp = sess.get(url, headers=_HTML_HEADERS, timeout=timeout)
    resp.raise_for_status()
    html = resp.text or ""
    schema = find_jobposting_ldjson(html)
    detail = merge_text(
        clean_text(str(schema.get("description") or "")),
        extract_json_text_fields(
            html,
            [
                "jobPostingDescription",
                "jobDescription",
                "description",
                "descriptionTeaser",
                "summary",
                "responsibilities",
                "qualifications",
                "skills",
                "formattedData",
            ],
        ),
        extract_text_by_selectors(
            html,
            [
                '[data-automation-id="jobPostingDescription"]',
                '[data-automation-id="jobDescription"]',
                '[data-automation-id="jobDetails"]',
                '[data-automation-id="details"]',
                '[data-automation-id="description"]',
                '[data-automation-id="Responsibilities"]',
                '[data-automation-id="Qualifications"]',
                '[data-automation-id="postedOn"] + div',
                'section[aria-label*="Description"]',
                'section[aria-label*="Responsibilities"]',
                'section[aria-label*="Qualifications"]',
                'div[class*="job-description"]',
                'div[class*="description"]',
                'article',
            ],
            max_nodes=8,
        ),
    )
    if looks_like_metadata_only_description(detail):
        return ""
    return detail


def _should_skip_detail_fetch(title: str, location: str) -> bool:
    normalized_title = (title or "").strip().lower()
    normalized_location = (location or "").strip().lower()
    if any(marker in normalized_title for marker in _DETAIL_SKIP_TITLE_MARKERS):
        return True
    non_us_markers = (
        "india",
        "japan",
        "ireland",
        "tokyo",
        "dublin",
        "melbourne",
        "remote-ind",
    )
    if any(marker in normalized_location for marker in non_us_markers):
        return True
    return False


def _should_second_chance_enrich(title: str) -> bool:
    normalized_title = f" {(title or '').strip().lower()} "
    return any(marker in normalized_title for marker in _SECOND_CHANCE_DETAIL_MARKERS)


class WorkdaySource(BaseSource):
    """Fetches jobs from a Workday CXS board endpoint."""

    def __init__(self, company: str, board_url: str) -> None:
        self.company = company
        self.board_url = board_url
        self.board_id = _board_id(board_url)
        try:
            _, self._tenant, self._site = _parse(board_url)
        except ValueError:
            self._tenant = ""
            self._site = ""
        self.name = f"workday:{self._tenant}:{self._site}"

    def fetch(self, seen_keys: set[str], timeout: int = 30) -> list[Job]:
        if not self._tenant or not self._site:
            return []

        u = urlparse(self.board_url)
        origin = f"{u.scheme}://{u.netloc}"
        approot = f"{origin}/wday/cxs/{self._tenant}/{self._site}/approot"
        jobs_url = f"{origin}/wday/cxs/{self._tenant}/{self._site}/jobs"

        sess = get_session("workday")

        # Bootstrap call
        boot = sess.get(approot, timeout=timeout)
        boot.raise_for_status()

        all_raw: list[dict] = []
        offset = 0
        limit = 20
        safety_cap = 5000

        while True:
            payload = {"limit": limit, "offset": offset, "searchText": "", "appliedFacets": {}}
            resp = sess.post(jobs_url, headers=_HEADERS, json=payload, timeout=timeout)
            resp.raise_for_status()

            if _WML_APP_ERROR in (resp.text or ""):
                raise RuntimeError(f"Workday application error: {resp.text[:200]}")

            ct = (resp.headers.get("content-type") or "").lower()
            if "json" not in ct:
                raise RuntimeError(f"Workday non-JSON response (ct={ct}): {resp.text[:200]}")

            data = resp.json() if resp.content else {}
            posts = data.get("jobPostings") or data.get("items") or []
            if not isinstance(posts, list) or not posts:
                break

            all_raw.extend(posts)
            if len(all_raw) >= 500:
                break
            offset += len(posts)
            if offset >= safety_cap:
                break

        result: list[Job] = []
        extra_detail_budget_remaining = _EXTRA_SHALLOW_DETAIL_BUDGET
        seen_job_keys: set[str] = set()
        for post in all_raw:
            title = post.get("title") or post.get("jobTitle") or "Unknown Title"
            raw_loc = post.get("locationsText") or post.get("location") or "Unknown Location"
            loc = make_location([str(x) for x in raw_loc]) if isinstance(raw_loc, list) else str(raw_loc)
            posted = post.get("postedOn") or post.get("postedDate") or post.get("timePosted") or ""

            ext = post.get("externalPath") or post.get("externalUrl") or ""
            if ext:
                if not ext.startswith("/"):
                    ext = "/" + ext
                url_job = _normalize_url(self.board_url, ext)
            else:
                url_job = self.board_url

            pid = _canonical_req_id(str(post.get("jobPostingId") or post.get("id") or ""))
            if not pid:
                pid = _canonical_req_id(url_job)
            key = (
                f"workday:{self._tenant}:{self._site}:{pid}" if pid
                else f"workday:{self._tenant}:{self._site}:url:{url_job}"
            )
            if key in seen_job_keys:
                continue
            seen_job_keys.add(key)

            description = merge_text(
                post.get("description"),
                post.get("jobDescription"),
                post.get("formattedData"),
            )
            cr = classify(title)
            needs_enrichment = (
                looks_like_metadata_only_description(description)
                or len((description or "").strip()) < 1000
            )
            should_enrich = False
            if bool(url_job) and not _should_skip_detail_fetch(title, loc):
                if cr.label in {"yes", "maybe"}:
                    should_enrich = True
                elif _should_second_chance_enrich(title):
                    should_enrich = True
                elif needs_enrichment and extra_detail_budget_remaining > 0:
                    should_enrich = True
                    extra_detail_budget_remaining -= 1
            if should_enrich:
                try:
                    detail = _fetch_detail_description(sess, url_job, timeout)
                    if detail:
                        description = merge_text(description, detail)
                except Exception as exc:
                    log.debug("workday:%s:%s detail fetch failed for %s: %s", self._tenant, self._site, url_job, exc)
            result.append(Job(
                key=key, source="workday", company=self.company,
                title=title, location=loc, url=url_job,
                posted=str(posted), description=description, score=cr.score, label=cr.label,
            ))

        log.debug("workday:%s:%s: fetched %d jobs", self._tenant, self._site, len(result))
        return result
