"""Lightweight job intelligence helpers for repost detection and JD extraction."""
from __future__ import annotations

from difflib import SequenceMatcher
import json
import re
from urllib.parse import urlsplit, urlunsplit

from .profile import PROFILE


_AGENCY_PATTERNS = (
    r"staffing",
    r"recruit(?:er|ers|ing|ment)",
    r"headhunt(?:er|ers|ing)?",
    r"agency",
    r"outsourcing",
    r"placement",
    r"employment\s+agency",
    r"search\s+firm",
    r"talent\s+(?:acquisition|agency|partners?|solutions|staffing|sourcing)",
)
_VENDOR_PATTERNS = (
    r"consult(?:ing|ants?)",
    r"stott\s+and\s+may",
    r"bcforward",
    r"insight\s+global",
    r"ascendion",
    r"wissen(?:\s+technology)?",
    r"intellibee",
    r"nasscomm",
    r"capgemini",
    r"tech\s+consulting",
)

_DIRECT_SOURCE_SCORES = {
    "amazon": 96,
    "apple": 96,
    "google": 96,
    "ibm": 95,
    "linkedin": 88,
    "meta": 96,
    "microsoft": 96,
    "netflix": 96,
    "nvidia": 96,
    "oracle": 95,
    "stripe": 96,
    "goldman_sachs": 92,
    "greenhouse": 88,
    "lever": 88,
    "ashby": 88,
    "workday": 84,
    "workable": 84,
    "jobvite": 84,
    "icims": 82,
    "smartrecruiters": 78,
    "manual": 70,
}

_SALARY_RANGE_RE = re.compile(
    r"(?P<currency>\$|usd|cad|eur|gbp)?\s*"
    r"(?P<min>\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s*(?P<min_suffix>[km])?"
    r"\s*(?:-|to|–|—)\s*"
    r"(?P<currency2>\$|usd|cad|eur|gbp)?\s*"
    r"(?P<max>\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s*(?P<max_suffix>[km])?"
    r"\s*(?P<period>/\s*(?:yr|year|hour|hr|month|mo)|per\s+(?:year|hour|month)|annual|hourly|monthly)?",
    re.IGNORECASE,
)
_SALARY_CONTEXT_RE = re.compile(
    r"(salary|compensation|pay range|base pay|hourly rate|annual(?:ized)?|per year|per hour|\$|usd|cad|eur|gbp)",
    re.IGNORECASE,
)
_YEARS_RE = re.compile(
    r"(?P<min>\d{1,2})\+?\s*(?:-|to|–|—)?\s*(?P<max>\d{1,2})?\s+years?\s+(?:of\s+)?(?:experience|exp)\b",
    re.IGNORECASE,
)
_CLEARANCE_RE = re.compile(
    r"\b(secret|top secret|ts/sci|ts sci|security clearance|public trust)\b",
    re.IGNORECASE,
)
_YEARS_RE = re.compile(
    r"(?P<min>\d{1,2})\+?\s*(?:-|to|–|—)?\s*(?P<max>\d{1,2})?\s+years?\s+(?:of\s+)?"
    r"(?:total\s+)?(?:relevant\s+)?(?:professional\s+|work\s+|industry\s+)?(?:experience|exp)\b",
    re.IGNORECASE,
)
_YEARS_RE = re.compile(
    r"(?P<min>\d{1,2})\+?\s*(?:-|to)?\s*(?P<max>\d{1,2})?\s+years?\s+(?:of\s+)?"
    r"(?:total\s+)?(?:relevant\s+)?(?:professional\s+|work\s+|industry\s+)?"
    r"(?:[a-z0-9+/#,\-\s]{0,80}\s+)?(?:experience|exp)\b",
    re.IGNORECASE,
)
_CITIZENSHIP_REQ_RE = re.compile(
    r"\b(must\s+be\s+(?:a\s+)?u\.?s\.?\s+citizen|u\.?s\.?\s+citizen(?:ship)?\s+(?:is\s+)?required|citizenship\s+(?:is\s+)?required)\b",
    re.IGNORECASE,
)
_YEARS_REQUIREMENT_CONTEXT_RE = re.compile(
    r"\b(required|requirements?|qualifications?|minimum|preferred|candidate|role|work|professional|industry|relevant)\b",
    re.IGNORECASE,
)
_EMPLOYER_HISTORY_CONTEXT_RE = re.compile(
    r"\b(company|firm|organization|business|provider|founded|serving|premier)\b",
    re.IGNORECASE,
)
_WORKDAY_REQ_RE = re.compile(r"(R-\d+)(?:-\d+)?", re.IGNORECASE)
_AGENCY_RE = re.compile(r"\b(?:" + "|".join(_AGENCY_PATTERNS) + r")\b", re.IGNORECASE)
_VENDOR_RE = re.compile(r"\b(?:" + "|".join(_VENDOR_PATTERNS) + r")\b", re.IGNORECASE)
_MOJIBAKE_MARKERS = ("â", "Ã", "�")
_MOJIBAKE_REPLACEMENTS = {
    "â€™": "'",
    "â€˜": "'",
    "â€œ": '"',
    "â€\x9d": '"',
    "â€“": "-",
    "â€”": "-",
    "â€¦": "...",
    "Â ": " ",
    "Â": "",
}


def _clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


def _norm_token_text(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def make_canonical_key(company: str, title: str) -> str:
    company_key = "-".join(_norm_token_text(company).split())
    title_key = "-".join(_norm_token_text(title).split())
    return f"{company_key}:{title_key}".strip(":")


def normalize_compare_url(url: str) -> str:
    raw = (url or "").strip()
    if not raw:
        return ""
    parts = urlsplit(raw)
    path = re.sub(r"/+", "/", parts.path.rstrip("/"))
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, "", ""))


def extract_workday_req_id(value: str) -> str:
    match = _WORKDAY_REQ_RE.search((value or "").strip())
    if not match:
        return ""
    return match.group(1).upper()


def normalize_text_encoding(text: str) -> str:
    raw = text or ""
    if not raw or not any(marker in raw for marker in _MOJIBAKE_MARKERS):
        return raw
    replaced = raw
    for bad, good in _MOJIBAKE_REPLACEMENTS.items():
        replaced = replaced.replace(bad, good)
    candidates = [raw, replaced]
    try:
        candidates.append(raw.encode("latin1", errors="ignore").decode("utf-8", errors="ignore"))
    except Exception:
        pass
    try:
        candidates.append(raw.encode("cp1252", errors="ignore").decode("utf-8", errors="ignore"))
    except Exception:
        pass

    def _badness(value: str) -> tuple[int, int]:
        lowered = value or ""
        return (sum(lowered.count(marker) for marker in _MOJIBAKE_MARKERS), -len(lowered))

    best = min(candidates, key=_badness)
    return best.strip() if best.strip() else raw


def _token_set(text: str) -> set[str]:
    tokens = [token for token in _norm_token_text(text).split() if len(token) >= 3]
    return set(tokens)


def description_similarity(left: str, right: str) -> float:
    left_clean = _clean_text(left)
    right_clean = _clean_text(right)
    if not left_clean or not right_clean:
        return 0.0
    left_tokens = _token_set(left_clean)
    right_tokens = _token_set(right_clean)
    if not left_tokens or not right_tokens:
        return 0.0
    overlap = len(left_tokens & right_tokens) / max(len(left_tokens | right_tokens), 1)
    char_ratio = SequenceMatcher(None, left_clean[:4000].lower(), right_clean[:4000].lower()).ratio()
    return max(overlap, char_ratio)


def score_employer_quality(source: str, company: str, url: str = "") -> tuple[int, str]:
    source_key = (source or "").strip().lower()
    company_text = (company or "").strip().lower()
    score = _DIRECT_SOURCE_SCORES.get(source_key, 70)
    reasons: list[str] = []

    if source_key in _DIRECT_SOURCE_SCORES:
        reasons.append(f"{source_key} is treated as direct-employer inventory.")
    else:
        reasons.append("Source quality is unknown, using a neutral baseline.")

    if _AGENCY_RE.search(company_text):
        score = min(score, 25)
        reasons.append("Company name looks like staffing or recruiting inventory.")
    elif _VENDOR_RE.search(company_text):
        score = min(score, 55)
        reasons.append("Company name looks like consulting or vendor inventory.")
    else:
        reasons.append("Company name does not look like staffing or consulting inventory.")

    compare_url = normalize_compare_url(url)
    if compare_url.startswith("manual://"):
        score = min(score, 70)
        reasons.append("Manual entries are kept below verified scraper sources.")
    elif compare_url:
        reasons.append("Posting has a stable company or ATS URL.")
    else:
        score = max(score - 5, 0)
        reasons.append("Posting URL is missing, which lowers trust slightly.")

    return max(0, min(score, 100)), " ".join(reasons)


def _parse_salary_amount(raw: str, suffix: str) -> int | None:
    try:
        value = float((raw or "").replace(",", ""))
    except ValueError:
        return None
    mult = 1
    if (suffix or "").lower() == "k":
        mult = 1000
    elif (suffix or "").lower() == "m":
        mult = 1_000_000
    return int(value * mult)


def _salary_match_score(match: re.Match[str], text: str) -> tuple[int, int, int]:
    window_start = max(match.start() - 64, 0)
    window_end = min(match.end() + 64, len(text))
    salary_context = text[window_start:window_end]
    has_currency = bool((match.group("currency") or "").strip() or (match.group("currency2") or "").strip())
    has_period = bool((match.group("period") or "").strip())
    context_hits = len(_SALARY_CONTEXT_RE.findall(salary_context))
    match_len = match.end() - match.start()
    return (
        1 if has_currency else 0,
        1 if has_period else 0,
        context_hits,
        match_len,
    )


def _salary_period_name(period_raw: str) -> str:
    lowered = (period_raw or "").lower()
    if "hour" in lowered or "hr" in lowered:
        return "hour"
    if "month" in lowered or "mo" in lowered:
        return "month"
    return "year"


def _salary_range_is_reasonable(
    *,
    salary_min: int,
    salary_max: int,
    min_raw: str,
    min_suffix: str,
    period: str,
) -> bool:
    if salary_min <= 0 or salary_max <= 0 or salary_min > salary_max:
        return False
    if salary_max > 10_000_000:
        return False
    if period == "hour":
        return 5 <= salary_min <= 1_000 and 5 <= salary_max <= 1_000
    if period == "month":
        return 1_000 <= salary_min <= 500_000 and 1_000 <= salary_max <= 500_000
    if salary_min < 10_000 or salary_max < 10_000 or salary_max > 5_000_000:
        return False
    if salary_max >= 10_000 and salary_min < 1_000 and "," not in (min_raw or "") and not (min_suffix or ""):
        return False
    if salary_max >= 100_000 and salary_min * 100 < salary_max and "," not in (min_raw or "") and not (min_suffix or ""):
        return False
    return True


def _accepted_years_match(match: re.Match[str], text: str) -> bool:
    try:
        years = int(match.group("min"))
    except (IndexError, ValueError):
        return False
    if years > 20:
        return False
    start = max(0, match.start() - 180)
    context = text[start : min(len(text), match.end() + 120)]
    if years >= 15 and _EMPLOYER_HISTORY_CONTEXT_RE.search(context) and not _YEARS_REQUIREMENT_CONTEXT_RE.search(context):
        return False
    return True


def _candidate_degree_keyword() -> str:
    education = str(PROFILE.get("education") or "").lower()
    if any(token in education for token in ("ph.d", "phd", "doctor")):
        return "phd"
    if any(token in education for token in ("m.s", "ms ", "master", "masters", "graduate")):
        return "master"
    if any(token in education for token in ("b.s", "bs ", "bachelor", "undergraduate")):
        return "bachelor"
    return ""


def _select_years_match_for_candidate(matches: list[re.Match[str]], text: str) -> re.Match[str] | None:
    if not matches:
        return None
    degree = _candidate_degree_keyword()
    degree_patterns = {
        "phd": r"\b(ph\.?d|doctorate|doctoral)\b",
        "master": r"\b(master|masters|master['’`]?s|masterâ€™s|m\.?s\.?)",
        "bachelor": r"\b(bachelor|bachelors|bachelor['’`]?s|bachelorâ€™s|b\.?s\.?)",
    }
    pattern = degree_patterns.get(degree)
    if pattern:
        for match in matches:
            context = text[max(0, match.start() - 120) : match.start()]
            if re.search(pattern, context, flags=re.IGNORECASE):
                return match
    return max(matches, key=lambda match: int(match.group("min")))


def extract_structured_fields(title: str, description: str, *, location: str = "") -> dict:
    text = "\n".join(part for part in (title, location, description) if part).strip()
    text_lower = text.lower()
    title_lower = (title or "").lower()
    data: dict[str, object] = {}

    if "hybrid" in text_lower:
        data["remote_mode"] = "hybrid"
    elif "remote" in text_lower or "work from home" in text_lower:
        data["remote_mode"] = "remote"
    elif "on-site" in text_lower or "onsite" in text_lower or "on site" in text_lower:
        data["remote_mode"] = "onsite"

    salary_match = None
    salary_matches = list(_SALARY_RANGE_RE.finditer(text))
    if salary_matches:
        salary_match = max(salary_matches, key=lambda match: _salary_match_score(match, text))
    if salary_match:
        salary_context = salary_match.group(0)
        window_start = max(salary_match.start() - 48, 0)
        window_end = min(salary_match.end() + 48, len(text))
        salary_context = text[window_start:window_end]
        salary_min = _parse_salary_amount(salary_match.group("min"), salary_match.group("min_suffix"))
        salary_max = _parse_salary_amount(salary_match.group("max"), salary_match.group("max_suffix"))
        has_currency = bool((salary_match.group("currency") or "").strip() or (salary_match.group("currency2") or "").strip())
        has_period = bool((salary_match.group("period") or "").strip())
        has_context = bool(_SALARY_CONTEXT_RE.search(salary_context))
        if (
            salary_min is not None
            and salary_max is not None
            and (has_currency or has_period or has_context)
            and _salary_range_is_reasonable(
                salary_min=salary_min,
                salary_max=salary_max,
                min_raw=salary_match.group("min"),
                min_suffix=salary_match.group("min_suffix") or "",
                period=_salary_period_name(salary_match.group("period") or ""),
            )
        ):
            data["salary_min"] = salary_min
            data["salary_max"] = salary_max
            currency = (salary_match.group("currency") or salary_match.group("currency2") or "$").upper()
            data["salary_currency"] = "USD" if currency == "$" else currency
            data["salary_period"] = _salary_period_name(salary_match.group("period") or "")

    years_matches = [match for match in _YEARS_RE.finditer(text) if _accepted_years_match(match, text)]
    years_match = _select_years_match_for_candidate(years_matches, text)
    if years_match:
        data["years_experience_min"] = int(years_match.group("min"))
        if years_match.group("max"):
            data["years_experience_max"] = int(years_match.group("max"))

    if re.search(r"\b(sponsor|sponsorship available|visa support)\b", text_lower):
        data["visa_sponsorship"] = True
    elif re.search(
        r"\b(no visa sponsorship|unable to sponsor|will not sponsor|cannot sponsor|"
        r"visa sponsorship(?: is)? not available|u\.?s\.? work visa sponsorship.{0,120}not available|"
        r"permanent work authorization in the united states)\b",
        text_lower,
    ):
        data["visa_sponsorship"] = False

    if _CLEARANCE_RE.search(text):
        data["security_clearance"] = True
    if _CITIZENSHIP_REQ_RE.search(text):
        data["citizenship_requirement"] = True

    text_head = text_lower[:800]
    internship_role = (
        re.search(r"\b(intern|internship|co[- ]?op)\b", title_lower)
        or re.search(r"\b(internship|intern program|co[- ]?op)\s+(role|position|program|opportunity)\b", text_head)
        or re.search(r"\b(role|position|employment)\s+type\s*[:\-]\s*(internship|intern|co[- ]?op)\b", text_head)
        or re.search(r"\b(this|the)\s+(internship|intern program|co[- ]?op)\b", text_head)
    )
    if internship_role:
        data["employment_type"] = "internship"
    elif re.search(r"\b(contract|contractor)\b", text_lower):
        data["employment_type"] = "contract"
    elif re.search(r"\bpart[ -]?time\b", text_lower):
        data["employment_type"] = "part-time"
    elif re.search(r"\bfull[ -]?time\b", text_lower):
        data["employment_type"] = "full-time"

    return data


def to_structured_json(data: dict) -> str:
    if not data:
        return ""
    return json.dumps(data, ensure_ascii=True, sort_keys=True)
