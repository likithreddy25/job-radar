"""Base class and shared data types for all job sources."""
from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional

US_STATE_ABBRS = frozenset({
    "al","ak","az","ar","ca","co","ct","de","fl","ga","hi","id","il","in","ia","ks","ky","la",
    "me","md","ma","mi","mn","ms","mo","mt","ne","nv","nh","nj","nm","ny","nc","nd","oh","ok",
    "or","pa","ri","sc","sd","tn","tx","ut","vt","va","wa","wv","wi","wy","dc",
})

NON_US_LOCATION_MARKERS = (
    " india", "(ind", "canada", "united kingdom", " uk", "europe", "emea",
    "apac", "latam", "australia", "germany", "france", "spain", "netherlands",
    "ireland", "singapore", "japan", "brazil", "mexico", "poland", "portugal",
    "argentina", "sweden", "switzerland", "israel", "philippines", "denmark",
    "finland", "norway", "romania",
)
COUNTRY_CODE_MARKERS = frozenset({
    "ar", "au", "br", "ca", "ch", "cn", "de", "es", "fr", "gb", "ie",
    "il", "in", "jp", "mx", "nl", "ph", "pl", "pt", "se", "sg", "uk",
})


@dataclass
class Job:
    key: str
    source: str
    company: str
    title: str
    location: str
    url: str
    posted: str = ""
    description: str = ""
    score: int = 0
    label: str = "no"


def make_location(parts: list[Optional[str]]) -> str:
    clean = [str(p).strip() for p in parts if p and str(p).strip()]
    return ", ".join(clean) if clean else "Unknown Location"


def clean_text(text: str) -> str:
    cleaned = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", cleaned).strip()


def flatten_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return clean_text(value)
    if isinstance(value, (int, float, bool)):
        return str(value).strip()
    if isinstance(value, list):
        parts = [flatten_text(item) for item in value]
        return merge_text(*parts)
    if isinstance(value, dict):
        parts = [flatten_text(item) for item in value.values()]
        return merge_text(*parts)
    return clean_text(str(value))


def merge_text(*parts: Any) -> str:
    cleaned: list[str] = []
    seen: set[str] = set()
    for part in parts:
        text = flatten_text(part)
        if not text:
            continue
        norm = text.lower()
        if norm in seen:
            continue
        seen.add(norm)
        cleaned.append(text)
    return "\n\n".join(cleaned)


def _iter_json_objects(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        items = [value]
        graph = value.get("@graph")
        if isinstance(graph, list):
            for child in graph:
                items.extend(_iter_json_objects(child))
        for child in value.values():
            if isinstance(child, (dict, list)):
                items.extend(_iter_json_objects(child))
        return items
    if isinstance(value, list):
        items: list[dict[str, Any]] = []
        for child in value:
            items.extend(_iter_json_objects(child))
        return items
    return []


def find_jobposting_ldjson(html: str) -> dict[str, Any]:
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return {}

    soup = BeautifulSoup(html or "", "html.parser")
    for script in soup.find_all("script"):
        script_type = (script.get("type") or "").lower()
        if "ld+json" not in script_type:
            continue
        raw = script.string or script.get_text(" ", strip=True)
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except Exception:
            continue
        for item in _iter_json_objects(payload):
            type_value = item.get("@type")
            if isinstance(type_value, list):
                types = [str(entry).strip().lower() for entry in type_value]
            else:
                types = [str(type_value).strip().lower()]
            if "jobposting" in types:
                return item
    return {}


def jobposting_location_text(payload: dict[str, Any]) -> str:
    raw = payload.get("jobLocation") or payload.get("applicantLocationRequirements") or []
    values = raw if isinstance(raw, list) else [raw]
    locations: list[str] = []
    for item in values:
        if isinstance(item, dict):
            address = item.get("address")
            if isinstance(address, dict):
                country = address.get("addressCountry")
                if isinstance(country, dict):
                    country = country.get("name") or country.get("addressCountry") or ""
                pieces = [
                    address.get("addressLocality"),
                    address.get("addressRegion"),
                    country or address.get("addressCountry"),
                ]
                text = ", ".join(str(piece).strip() for piece in pieces if piece and str(piece).strip())
                if text:
                    locations.append(text)
                    continue
            name = item.get("name")
            if isinstance(name, str) and name.strip():
                locations.append(name.strip())
                continue
            text = flatten_text(item)
            if text:
                locations.append(text)
                continue
        elif isinstance(item, str) and item.strip():
            locations.append(item.strip())
    deduped: list[str] = []
    seen: set[str] = set()
    for location in locations:
        norm = location.lower()
        if norm in seen:
            continue
        seen.add(norm)
        deduped.append(location)
    return " / ".join(deduped[:5])


def extract_text_by_selectors(html: str, selectors: list[str], *, max_nodes: int = 5) -> str:
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return ""

    soup = BeautifulSoup(html or "", "html.parser")
    parts: list[str] = []
    seen: set[str] = set()
    for selector in selectors:
        try:
            nodes = soup.select(selector)
        except Exception:
            continue
        for node in nodes[:max_nodes]:
            text = clean_text(node.get_text("\n", strip=True))
            if not text:
                continue
            norm = text.lower()
            if norm in seen:
                continue
            seen.add(norm)
            parts.append(text)
        if parts:
            break
    return merge_text(*parts)


def extract_json_text_fields(text: str, field_names: list[str]) -> str:
    parts: list[str] = []
    for field in field_names:
        pattern = re.compile(rf'"{re.escape(field)}"\s*:\s*"((?:\\.|[^"\\])*)"')
        for match in pattern.finditer(text or ""):
            raw = match.group(1)
            try:
                decoded = json.loads(f'"{raw}"')
            except Exception:
                try:
                    decoded = bytes(raw, "utf-8").decode("unicode_escape", errors="ignore")
                except Exception:
                    decoded = raw
            cleaned = clean_text(decoded)
            if cleaned:
                parts.append(cleaned)
    return merge_text(*parts)


def looks_like_metadata_only_description(text: str) -> bool:
    cleaned = clean_text(text or "")
    if not cleaned:
        return True
    lowered = cleaned.lower()
    content_markers = (
        "responsibilities",
        "qualifications",
        "requirements",
        "about the role",
        "about you",
        "what you'll do",
        "what you will do",
        "minimum qualifications",
        "preferred qualifications",
        "experience",
        "job description",
        "in this role",
        "you will",
    )
    if any(marker in lowered for marker in content_markers):
        return False
    metadata_markers = (
        "posted",
        "days ago",
        "job id",
        "req id",
        "requisition id",
        "locations",
        "department",
        "employment type",
    )
    if len(cleaned) < 160 and any(marker in lowered for marker in metadata_markers):
        return True
    if re.search(r"\b(?:r-\d+|req(?:uisition)?[:\s-]*\w+\d+|job id[:\s-]*\w+\d+)\b", cleaned, re.I):
        return len(cleaned) < 220
    lines = [line.strip() for line in re.split(r"[\n\r]+", text or "") if line.strip()]
    if len(lines) <= 3 and not any(marker in lowered for marker in content_markers):
        return len(cleaned.split()) <= 24
    return False


def is_us_location(location: str) -> bool:
    """Return True if the location string is plausibly US-based."""
    loc = (location or "").strip().lower()
    if not loc or loc == "unknown location":
        return False
    scope = remote_scope_status(loc)
    if scope == "us":
        return True
    if scope in {"non_us", "unspecified"}:
        return False
    parts = [part.strip() for part in loc.split(",") if part.strip()]
    if "united states" in loc or "u.s." in loc:
        return True
    if re.search(r"\busa\b", loc):
        return True
    if re.search(r"\bus\b", loc):
        return True
    if len(parts) >= 3 and parts[-1] in COUNTRY_CODE_MARKERS:
        return False
    if "washington, dc" in loc or "district of columbia" in loc:
        return True
    # City, State abbreviation — e.g. "Seattle, WA"
    m = re.search(r",\s*([a-z]{2})(\b|[^a-z])", loc)
    if m and m.group(1) in US_STATE_ABBRS:
        return True
    return False


def remote_scope_status(location: str) -> str:
    """Classify remote scope as us / non_us / unspecified / not_remote."""
    loc = (location or "").strip().lower()
    if "remote" not in loc:
        return "not_remote"
    has_us_marker = (
        "united states" in loc
        or "u.s." in loc
        or bool(re.search(r"\busa\b", loc))
        or bool(re.search(r"\bus\b", loc))
        or "remote us" in loc
        or "remote - us" in loc
        or "remote, us" in loc
        or "remote within the united states" in loc
    )
    if has_us_marker:
        return "us"
    if any(marker in f" {loc}" for marker in NON_US_LOCATION_MARKERS):
        return "non_us"
    return "unspecified"


class BaseSource(ABC):
    """Abstract base class every job source must implement."""

    name: str  # unique source identifier (e.g. "microsoft")

    @abstractmethod
    def fetch(self, seen_keys: set[str], timeout: int = 30) -> list[Job]:
        """Fetch jobs from the source and return a list of Job objects.

        Args:
            seen_keys: Set of job keys already in the database (for early-exit).
            timeout: HTTP timeout in seconds.

        Returns:
            All retrieved jobs (scored + labelled). Deduplication is done by
            the orchestrator — sources do not need to filter against seen_keys.
        """
        ...
