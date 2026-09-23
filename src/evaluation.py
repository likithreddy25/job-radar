"""Structured job-fit evaluation with weighted dimensions and letter grades."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from functools import lru_cache
import json
from pathlib import Path
import re

from .classifier import (
    CLEARANCE_EXCLUDE_PHRASES,
    CLEARANCE_EXCLUDE_REGEXES,
    HARD_EXCLUDE_REGEXES,
    SENIORITY_TOKENS,
    VERY_SENIOR,
    classify,
)
from .config import Config
from .llm_scorer import apply_llm_delta, maybe_review_job
from .profile import PROFILE, SKILLS_MODERATE, SKILLS_STRONG
from .scoring_policy import DEFAULT_MAYBE_THRESHOLD, DEFAULT_YES_THRESHOLD, label_for_score
from .sources.base import is_us_location, remote_scope_status

EVAL_CFG = Config.load()
EVALUATION_PAYLOAD_VERSION = 2
ROOT_DIR = Path(__file__).resolve().parents[1]
CANDIDATE_EXPERIENCE_YEARS = int(PROFILE.get("experience_years") or 3)
TARGET_EXPERIENCE_MAX_YEARS = CANDIDATE_EXPERIENCE_YEARS
TARGET_EXPERIENCE_RANGE_LABEL = f"1-{TARGET_EXPERIENCE_MAX_YEARS} year target range"
LOCAL_CANDIDATE_EVIDENCE_PATH = ROOT_DIR / "data" / "resume" / "candidate_evidence.local.md"
CANDIDATE_EVIDENCE_PATH = ROOT_DIR / "data" / "resume" / "candidate_evidence.md"
LOCAL_BASE_RESUME_PATH = ROOT_DIR / "data" / "resume" / "base_resume.local.md"
BASE_RESUME_PATH = ROOT_DIR / "data" / "resume" / "base_resume.md"
STRONG_DIRECT_SOURCES = frozenset({
    "amazon",
    "apple",
    "google",
    "goldman_sachs",
    "ibm",
    "linkedin",
    "meta",
    "microsoft",
    "netflix",
    "nvidia",
    "oracle",
    "stripe",
})
CRITICAL_SKILL_HINTS = (
    " years ",
    "experience with",
    "experience in",
    "expertise",
    "proficiency",
    "strong ",
    "deep experience",
    "hands-on",
    "required",
    "must",
    "need ",
    "needs ",
)
OPTIONAL_PORTFOLIO_CONTEXT_HINTS = (
    "if you have code",
    "if you have written",
    "please share",
    "open domain",
    "for example github",
    "github)",
    "github )",
)
SPECIALIZED_UNSUPPORTED_REQUIREMENT_ALIASES: dict[str, tuple[str, ...]] = {
    "asr/speech-to-text": (
        "asr",
        "automatic speech recognition",
        "speech to text",
        "speech-to-text",
        "stt",
    ),
    "multilingual speech": (
        "multilingual asr",
        "multilingual speech",
        "code switching",
        "code-switching",
        "accent",
        "accents",
    ),
    "audio preprocessing": (
        "audio preprocessing",
        "audio pipeline",
        "audio pipelines",
        "torchaudio",
        "librosa",
        "whisper",
        "wav2vec",
        "ctc",
    ),
    "speech evaluation": (
        "wer",
        "word error rate",
        "cer",
        "character error rate",
    ),
    "parameter-efficient tuning": (
        "lora",
        "qlora",
        "peft",
        "parameter efficient fine tuning",
        "parameter-efficient fine-tuning",
    ),
    "model compression": (
        "quantization",
        "distillation",
        "model distillation",
        "pruning",
        "model compression",
    ),
    "on-device optimization": (
        "on device",
        "on-device",
        "iphone",
        "ios",
        "mobile inference",
        "memory optimization",
        "battery optimization",
    ),
}
SKILL_EVIDENCE_ALIASES: dict[str, tuple[str, ...]] = {
    "apache spark": ("apache spark", "spark", "pyspark"),
    "ci/cd": ("ci/cd", "ci cd", "github actions"),
    "elt": ("elt", "etl", "data pipeline", "data pipelines", "pipeline workflow", "pipeline workflows"),
    "etl": ("etl", "elt", "data pipeline", "data pipelines", "pipeline workflow", "pipeline workflows"),
    "hugging face transformers": ("hugging face transformers", "hugging face", "transformers"),
    "machine learning": ("machine learning", "ml"),
    "ml": ("ml", "machine learning"),
    "pipeline": ("pipeline", "pipelines", "workflow", "workflows"),
    "pyspark": ("pyspark", "spark", "apache spark"),
    "rest api": ("rest api", "rest apis", "api", "apis"),
    "spark": ("spark", "pyspark", "apache spark"),
}
JD_SECTION_ALIASES: dict[str, tuple[str, ...]] = {
    "required": (
        "minimum qualifications",
        "minimum qualification",
        "required qualifications",
        "required qualification",
        "requirements",
        "basic qualifications",
        "what you bring",
        "what you'll bring",
        "what you’ll bring",
        "what we're looking for",
        "what we’re looking for",
        "your expertise",
        "about you",
        "you have",
        "who you are",
        "must have",
    ),
    "preferred": (
        "preferred qualifications",
        "preferred qualification",
        "nice to have",
        "nice-to-have",
        "bonus points",
        "bonus",
        "preferred skills",
    ),
    "responsibilities": (
        "responsibilities",
        "what you'll do",
        "what you will do",
        "what you’ll do",
        "in this role",
        "role summary",
        "day to day",
        "typical week",
    ),
    "company": (
        "about us",
        "about the company",
        "who we are",
        "company overview",
    ),
    "benefits": (
        "benefits",
        "perks",
        "compensation",
        "what we offer",
    ),
}
TARGET_ROLE_ALIASES: dict[str, tuple[str, ...]] = {
    "Business Intelligence Analyst": ("business intelligence analyst", "bi analyst"),
    "Business Intelligence Developer": ("business intelligence developer", "bi developer"),
    "Machine Learning Engineer": ("machine learning engineer", "mle"),
    "Data Scientist": ("data scientist", "ds"),
    "AI Engineer": ("ai engineer", "genai engineer", "generative ai engineer"),
}
EVAL_CLEARANCE_CONTEXT_REGEXES: tuple[str, ...] = (
    r"\bts[/\s\-]?sci\b",
    r"\btop\s+secret\b",
    r"\bpolygraph\b",
    r"\bpublic\s+trust\b",
    r"\bmust\s+be\s+(?:a\s+)?u\.?s\.?\s+citizen\b",
    r"\bu\.?s\.?\s+citizen(?:ship)?\s+(?:is\s+)?required\b",
    r"\bcitizenship\s+(?:is\s+)?required\b",
    r"\brequires?\s+(?:an?\s+)?(?:active\s+)?(?:security\s+)?clearance\b",
    r"\bmust\s+hold\s+(?:an?\s+)?(?:active\s+)?(?:security\s+)?clearance\b",
    r"\bability\s+to\s+obtain\s+(?:an?\s+)?(?:active\s+)?(?:security\s+)?clearance\b",
    r"\bclearance\s+eligible\b",
    r"\bactive\s+(?:secret|top\s+secret|ts[/\s\-]?sci)\b",
)
VISA_SPONSORSHIP_BLOCK_REGEXES: tuple[str, ...] = (
    r"\bnot\s+offering\s+visa\s+sponsorship\b",
    r"\bno\s+visa\s+sponsorship\b",
    r"\bunable\s+to\s+sponsor\b",
    r"\bwill\s+not\s+sponsor\b",
    r"\bdoes\s+not\s+offer\s+visa\s+sponsorship\b",
    r"\bno\s+(?:employment|work)\s+visa\s+sponsorship\b",
    r"\bu\.?s\.?\s+work\s+visa\s+sponsorship\b.{0,120}\bnot\s+available\b",
    r"\bvisa\s+sponsorship\s+(?:is\s+)?not\s+available\b",
    r"\bvisa\s+sponsorship\s+(?:is\s+)?unavailable\b",
    r"\bnot\s+eligible\s+for\s+visa\s+sponsorship\b",
    r"\brequires?\s+permanent\s+work\s+authorization\s+in\s+the\s+united\s+states\b",
    r"\bpermanent\s+work\s+authorization\s+in\s+the\s+united\s+states\s+(?:is\s+)?required\b",
    r"\bmust\s+have\s+current\s+authorization\s+to\s+work\b",
    r"\bunrestricted\s+(?:work\s+)?authorization\s+to\s+work\s+in\s+the\s+u\.?s\.?\b",
    r"\bauthori[sz]ation\s+to\s+work\s+in\s+the\s+u\.?s\.?.{0,160}\bwithout\s+(?:the\s+)?need\s+for\s+(?:employer\s+)?sponsorship\b",
    r"\bwithout\s+(?:the\s+)?need\s+for\s+(?:employer\s+)?sponsorship\s+(?:now\s+or\s+in\s+the\s+future|now\s+or\s+future|currently\s+or\s+in\s+the\s+future)\b",
    r"\bwithout\s+(?:requiring|requiring\s+any|needing)\s+(?:employer\s+)?sponsorship\s+(?:now\s+or\s+in\s+the\s+future|now\s+or\s+future|currently\s+or\s+in\s+the\s+future)\b",
)


@dataclass
class EvaluationDimension:
    name: str
    weight: float
    score: int
    reason: str

    @property
    def weighted_points(self) -> float:
        return (self.score / 100.0) * self.weight * 100.0


@dataclass
class EvaluationResult:
    score: int
    label: str
    grade: str
    matched_strong: list[str]
    matched_moderate: list[str]
    unsupported_strong: list[str]
    unsupported_moderate: list[str]
    critical_skill_gaps: list[str]
    reasons: list[str]
    fit_summary: str
    dimensions: list[EvaluationDimension]
    llm_review: dict | None = None

    def to_dict(self) -> dict:
        payload = {
            "version": EVALUATION_PAYLOAD_VERSION,
            "score": self.score,
            "label": self.label,
            "grade": self.grade,
            "matched_strong": list(self.matched_strong),
            "matched_moderate": list(self.matched_moderate),
            "unsupported_strong": list(self.unsupported_strong),
            "unsupported_moderate": list(self.unsupported_moderate),
            "critical_skill_gaps": list(self.critical_skill_gaps),
            "reasons": list(self.reasons),
            "fit_summary": self.fit_summary,
            "dimensions": [asdict(d) | {"weighted_points": round(d.weighted_points, 2)} for d in self.dimensions],
        }
        if self.llm_review:
            payload["llm_review"] = self.llm_review
        return payload

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=True, sort_keys=True)


def _norm(*parts: str) -> str:
    return "\n".join((p or "").strip().lower() for p in parts if p).strip()


def _phrase_match(text: str, phrase: str) -> bool:
    normalized_text = re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()
    normalized_phrase = re.sub(r"[^a-z0-9]+", " ", phrase.lower()).strip()
    if not normalized_text or not normalized_phrase:
        return False
    if len(normalized_phrase) == 1:
        return False
    return re.search(rf"\b{re.escape(normalized_phrase)}\b", normalized_text) is not None


@lru_cache(maxsize=1)
def _resume_evidence_text() -> str:
    for path in (
        LOCAL_CANDIDATE_EVIDENCE_PATH,
        CANDIDATE_EVIDENCE_PATH,
        LOCAL_BASE_RESUME_PATH,
        BASE_RESUME_PATH,
    ):
        try:
            markdown = path.read_text(encoding="utf-8")
        except OSError:
            continue
        sections: list[str] = []
        current = ""
        for line in markdown.splitlines():
            stripped = line.strip()
            if stripped.startswith("## "):
                current = stripped[3:].strip().lower()
                continue
            if current in {"professional experience", "selected projects"} and stripped:
                sections.append(stripped)
        if sections:
            return _norm("\n".join(sections))
    return ""


def _find_jd_headings(description: str) -> list[tuple[int, int, str]]:
    text = description or ""
    headings: list[tuple[int, int, str]] = []
    for section, aliases in JD_SECTION_ALIASES.items():
        for alias in aliases:
            standalone_pattern = re.compile(rf"(?im)^[#\-\*\s]*{re.escape(alias)}\s*:?\s*$")
            inline_pattern = re.compile(rf"(?im)^[#\-\*\s]*{re.escape(alias)}\s*:\s*(?=\S)")
            for match in standalone_pattern.finditer(text):
                headings.append((match.start(), match.end(), section))
            for match in inline_pattern.finditer(text):
                headings.append((match.start(), match.end(), section))
    headings.sort(key=lambda item: item[0])
    deduped: list[tuple[int, int, str]] = []
    seen_positions: set[tuple[int, str]] = set()
    for start, end, section in headings:
        key = (start, section)
        if key in seen_positions:
            continue
        seen_positions.add(key)
        deduped.append((start, end, section))
    return deduped


@lru_cache(maxsize=256)
def _jd_sections(description: str) -> dict[str, str]:
    text = description or ""
    sections: dict[str, list[str]] = {name: [] for name in JD_SECTION_ALIASES}
    headings = _find_jd_headings(text)
    if not headings:
        return {"full_text": text}
    for idx, (_start, end, section) in enumerate(headings):
        next_start = headings[idx + 1][0] if idx + 1 < len(headings) else len(text)
        body = text[end:next_start].strip()
        if body:
            sections.setdefault(section, []).append(body)
    output = {name: "\n".join(parts).strip() for name, parts in sections.items() if parts}
    output["full_text"] = text
    return output


def _skill_aliases(skill: str) -> tuple[str, ...]:
    aliases = SKILL_EVIDENCE_ALIASES.get(skill, ())
    if skill not in aliases:
        return (skill,) + aliases
    return aliases


def _has_resume_evidence(skill: str, evidence_text: str) -> bool:
    if not evidence_text:
        return False
    return any(_phrase_match(evidence_text, alias) for alias in _skill_aliases(skill))


def _skill_requirement_level(skill: str, description: str) -> str:
    sections = _jd_sections(description)
    required_text = _norm(sections.get("required", ""))
    preferred_text = _norm(sections.get("preferred", ""))
    responsibilities_text = _norm(sections.get("responsibilities", ""))
    full_text = _norm(sections.get("full_text", ""))
    aliases = _skill_aliases(skill)

    if required_text and any(_phrase_match(required_text, alias) for alias in aliases):
        return "required"
    if responsibilities_text and any(_phrase_match(responsibilities_text, alias) for alias in aliases):
        return "responsibility"
    if preferred_text and any(_phrase_match(preferred_text, alias) for alias in aliases):
        return "preferred"
    if not any(_phrase_match(full_text, alias) for alias in aliases):
        return ""
    for sentence in re.split(r"[.\n]+", full_text):
        if not sentence.strip():
            continue
        if not any(_phrase_match(sentence, alias) for alias in aliases):
            continue
        if skill == "github":
            lowered_sentence = sentence.lower()
            if any(hint in lowered_sentence for hint in OPTIONAL_PORTFOLIO_CONTEXT_HINTS):
                return "preferred"
        if any(hint in sentence for hint in CRITICAL_SKILL_HINTS):
            return "required"
    return "mentioned"


@dataclass
class SkillEvidenceAssessment:
    supported_strong: list[str]
    supported_moderate: list[str]
    unsupported_strong: list[str]
    unsupported_moderate: list[str]
    critical_skill_gaps: list[str]
    responsibility_skill_gaps: list[str]
    supported_strong_points: int
    supported_moderate_points: int


def _assess_skill_evidence(
    title: str,
    description: str,
    matched_strong: list[str],
    matched_moderate: list[str],
) -> SkillEvidenceAssessment:
    evidence_text = _resume_evidence_text()
    title_text = _norm(title)
    supported_strong: list[str] = []
    supported_moderate: list[str] = []
    unsupported_strong: list[str] = []
    unsupported_moderate: list[str] = []
    critical_skill_gaps: list[str] = []
    responsibility_skill_gaps: list[str] = []
    supported_strong_points = 0
    supported_moderate_points = 0

    for skill in matched_strong:
        if _has_resume_evidence(skill, evidence_text):
            supported_strong.append(skill)
            supported_strong_points += 18 if _phrase_match(title_text, skill) else 12
        else:
            unsupported_strong.append(skill)
            requirement_level = _skill_requirement_level(skill, description)
            if requirement_level == "required":
                critical_skill_gaps.append(skill)
            elif requirement_level == "responsibility":
                responsibility_skill_gaps.append(skill)

    for skill in matched_moderate:
        if _has_resume_evidence(skill, evidence_text):
            supported_moderate.append(skill)
            supported_moderate_points += 8 if _phrase_match(title_text, skill) else 5
        else:
            unsupported_moderate.append(skill)
            requirement_level = _skill_requirement_level(skill, description)
            if requirement_level == "required":
                critical_skill_gaps.append(skill)
            elif requirement_level == "responsibility":
                responsibility_skill_gaps.append(skill)

    return SkillEvidenceAssessment(
        supported_strong=supported_strong,
        supported_moderate=supported_moderate,
        unsupported_strong=unsupported_strong,
        unsupported_moderate=unsupported_moderate,
        critical_skill_gaps=critical_skill_gaps,
        responsibility_skill_gaps=responsibility_skill_gaps,
        supported_strong_points=supported_strong_points,
        supported_moderate_points=supported_moderate_points,
    )


def _match_skills(title: str, description: str) -> tuple[list[str], list[str], int, int]:
    title_text = _norm(title)
    description_text = _norm(description)
    matched_strong: list[str] = []
    matched_moderate: list[str] = []
    strong_points = 0
    moderate_points = 0

    for skill in sorted(SKILLS_STRONG):
        in_title = _phrase_match(title_text, skill)
        in_desc = _phrase_match(description_text, skill)
        if in_title or in_desc:
            matched_strong.append(skill)
            strong_points += 18 if in_title else 12

    for skill in sorted(SKILLS_MODERATE):
        in_title = _phrase_match(title_text, skill)
        in_desc = _phrase_match(description_text, skill)
        if in_title or in_desc:
            matched_moderate.append(skill)
            moderate_points += 8 if in_title else 5

    return matched_strong, matched_moderate, strong_points, moderate_points


def _target_role_hits(text: str) -> list[str]:
    hits: list[str] = []
    normalized_text = _norm(text)
    for role in PROFILE.get("target_roles", []):
        aliases = TARGET_ROLE_ALIASES.get(role, (role,))
        if any(_phrase_match(normalized_text, alias) for alias in aliases):
            hits.append(role)
    return hits


def _score_to_label(score: int, *, yes_threshold: int = DEFAULT_YES_THRESHOLD, maybe_threshold: int = DEFAULT_MAYBE_THRESHOLD) -> str:
    return label_for_score(score, yes_threshold=yes_threshold, maybe_threshold=maybe_threshold)


def _score_to_grade(score: int) -> str:
    if score >= 90:
        return "A"
    if score >= 80:
        return "B"
    if score >= 70:
        return "C"
    if score >= 55:
        return "D"
    if score >= 40:
        return "E"
    return "F"


def _clearance_signal_reason(title: str, text: str) -> str:
    title_text = (title or "").strip().lower()
    full_text = text or ""
    for phrase in CLEARANCE_EXCLUDE_PHRASES:
        if phrase in full_text:
            return f"Clearance or citizenship requirement detected: {phrase}."
    for pattern in EVAL_CLEARANCE_CONTEXT_REGEXES:
        if re.search(pattern, full_text):
            return "Clearance or citizenship requirement detected in the job description."
    for pattern in CLEARANCE_EXCLUDE_REGEXES:
        if re.search(pattern, title_text):
            return "Clearance or citizenship requirement detected in the job title."
    return ""


def _visa_sponsorship_block_reason(text: str) -> str:
    if not PROFILE.get("needs_visa_sponsorship", False):
        return ""
    full_text = (text or "").strip().lower()
    if not full_text:
        return ""
    for pattern in VISA_SPONSORSHIP_BLOCK_REGEXES:
        match = re.search(pattern, full_text)
        if match:
            return f"Blocked because the posting does not offer visa sponsorship: {match.group(0)}."
    return ""


def _find_seniority_token(text: str) -> str:
    for token in SENIORITY_TOKENS:
        if re.search(rf"\b{re.escape(token)}\b", text):
            return token
    return ""


def _find_title_seniority_token(title: str) -> str:
    return _find_seniority_token((title or "").strip().lower())


def _has_junior_title_signal(title: str) -> bool:
    normalized = (title or "").strip().lower()
    junior_tokens = (
        "junior",
        "jr",
        "entry level",
        "entry-level",
        "new grad",
        "graduate",
        "early career",
        "intern",
        "apprentice",
        "associate",
    )
    return any(re.search(rf"\b{re.escape(token)}\b", normalized) for token in junior_tokens)


def _body_seniority_signal(text: str) -> str:
    normalized = (text or "").strip().lower()
    explicit_patterns = (
        r"\bsenior[\s\-]+level\b",
        r"\bstaff[\s\-]+level\b",
        r"\bprincipal[\s\-]+level\b",
        r"\blead[\s\-]+role\b",
        r"\bmanager[\s\-]+role\b",
        r"\bthis\s+is\s+a\s+senior\b",
        r"\bthis\s+is\s+a\s+lead\b",
        r"\bthis\s+is\s+a\s+staff\b",
        r"\bthis\s+is\s+a\s+principal\b",
    )
    for pattern in explicit_patterns:
        if re.search(pattern, normalized):
            return pattern
    return ""


def _title_level_block(title: str) -> str:
    normalized = (title or "").strip().lower()
    blocked_titles = (
        "principal",
        "senior staff",
        "staff",
        "distinguished",
        "fellow",
    )
    for token in blocked_titles:
        if re.search(rf"\b{re.escape(token)}\b", normalized):
            return f"Blocked because the title contains {token}, which is above your target level."
    return ""


def _source_is_strong_first_party(source: str) -> bool:
    return (source or "").strip().lower() in STRONG_DIRECT_SOURCES


def _extract_years_requirement(text: str) -> int:
    normalized = (text or "").replace("–", "-").replace("—", "-")
    if not normalized:
        return 0
    mins: list[int] = []

    def accept(match: re.Match) -> bool:
        try:
            years = int(match.group(1))
        except ValueError:
            return False
        if years > 20:
            return False
        window_start = max(0, match.start() - 180)
        context = normalized[window_start : min(len(normalized), match.end() + 120)].lower()
        if years >= 15 and re.search(r"\b(company|firm|organization|business|provider|founded|serving|premier)\b", context):
            if not re.search(r"\b(required|requirements?|qualifications?|minimum|preferred|candidate|role|work|professional|industry|relevant)\b", context):
                return False
        return True

    # Treat ranges by their minimum accepted experience, e.g. "4-7+ years" is a
    # 4-year minimum, not a 7-year hard requirement.
    range_pattern = re.compile(
        r"\b(\d{1,2})\s*(?:-|to)\s*(\d{1,2})\+?\s+years?\s+(?:of\s+)?"
        r"(?:total\s+)?(?:relevant\s+)?(?:professional\s+|work\s+|industry\s+)?(?:experience|exp)\b",
        flags=re.IGNORECASE,
    )
    masked = normalized
    for match in range_pattern.finditer(normalized):
        if not accept(match):
            continue
        try:
            mins.append(int(match.group(1)))
        except ValueError:
            pass
        masked = masked[: match.start()] + (" " * (match.end() - match.start())) + masked[match.end() :]

    standalone_pattern = re.compile(
        r"\b(\d{1,2})\+?\s+years?\s+(?:of\s+)?"
        r"(?:total\s+)?(?:relevant\s+)?(?:professional\s+|work\s+|industry\s+)?(?:experience|exp)\b",
        flags=re.IGNORECASE,
    )
    for match in standalone_pattern.finditer(masked):
        if not accept(match):
            continue
        try:
            mins.append(int(match.group(1)))
        except ValueError:
            continue
        masked = masked[: match.start()] + (" " * (match.end() - match.start())) + masked[match.end() :]

    broad_experience_pattern = re.compile(
        r"\b(\d{1,2})\+?\s+years?\s+(?:of\s+)?"
        r"(?:total\s+)?(?:relevant\s+)?(?:professional\s+|work\s+|industry\s+)?"
        r"(?:[a-z0-9+/#,\-\s]{0,80}\s+)?(?:experience|exp)\b",
        flags=re.IGNORECASE,
    )
    for match in broad_experience_pattern.finditer(masked):
        if not accept(match):
            continue
        try:
            mins.append(int(match.group(1)))
        except ValueError:
            continue
        masked = masked[: match.start()] + (" " * (match.end() - match.start())) + masked[match.end() :]

    seniority_pattern = re.compile(
        r"\b(\d{1,2})\+?\s+years?\s+(?:of\s+)?"
        r"(?:consistent\s+track\s+record|validated\s+ability|hands[-\s]+on\s+experience|"
        r"professional\s+experience|work\s+experience|industry\s+experience|"
        r"as\s+a|as\s+an|in\s+|with\s+)",
        flags=re.IGNORECASE,
    )
    for match in seniority_pattern.finditer(masked):
        if not accept(match):
            continue
        try:
            mins.append(int(match.group(1)))
        except ValueError:
            continue
    return max(mins) if mins else 0


def _content_alignment_strength(
    matched_strong: list[str],
    matched_moderate: list[str],
    assessment: SkillEvidenceAssessment,
) -> str:
    supported_strong = len(assessment.supported_strong)
    supported_moderate = len(assessment.supported_moderate)
    if assessment.critical_skill_gaps:
        return "weak"
    if supported_strong >= 3:
        return "strong"
    if supported_strong >= 2 and supported_moderate >= 1:
        return "strong"
    if supported_strong >= 1 and supported_moderate >= 3:
        return "strong"
    if supported_strong >= 1 or supported_moderate >= 2:
        return "moderate"
    if matched_strong or matched_moderate:
        return "light"
    return "weak"


def _dimension_title_fit(
    title: str,
    matched_strong: list[str],
    matched_moderate: list[str],
    assessment: SkillEvidenceAssessment,
) -> tuple[int, str]:
    title_result = classify(title)
    content_strength = _content_alignment_strength(matched_strong, matched_moderate, assessment)
    if title_result.label == "yes":
        return 92, "The title is strongly aligned with your target roles."
    if title_result.label == "maybe":
        if content_strength == "strong":
            return 78, "The title is adjacent, and the skills plus JD work content are strongly aligned."
        if content_strength == "moderate":
            return 72, "The title is adjacent, and the skills plus JD work content support the match."
        return 65, "The title is adjacent to your target roles but needs review."
    if content_strength == "strong":
        return 58, "The title is off-target, but the skills and JD work content are strongly aligned."
    if content_strength == "moderate":
        return 42, "The title is weaker than your target roles, but the skills and JD work content are reasonably aligned."
    return 20, "The title is weak or ambiguous for your target role set."


def _dimension_skill_overlap(
    matched_strong: list[str],
    matched_moderate: list[str],
    strong_points: int,
    moderate_points: int,
    assessment: SkillEvidenceAssessment,
) -> tuple[int, str]:
    raw_score = min(100, strong_points + moderate_points)
    supported_score = min(100, assessment.supported_strong_points + assessment.supported_moderate_points)
    if assessment.critical_skill_gaps:
        score = max(35, min(raw_score - 28, supported_score + 18))
        return score, (
            "Keyword overlap exists, but JD-critical skills are not backed by experience/project evidence: "
            f"{', '.join(assessment.critical_skill_gaps[:4])}."
        )
    if assessment.responsibility_skill_gaps:
        score = max(45, min(raw_score - 16, supported_score + 20))
        return score, (
            "Core JD responsibilities mention skills that are not strongly evidenced in your work/projects: "
            f"{', '.join(assessment.responsibility_skill_gaps[:4])}."
        )
    if assessment.unsupported_strong or assessment.unsupported_moderate:
        score = max(40, min(raw_score - 12, supported_score + 22))
        unsupported = assessment.unsupported_strong + assessment.unsupported_moderate
        return score, (
            "Some overlap is only declared in the skills list and not evidenced in experience/projects: "
            f"{', '.join(unsupported[:4])}."
        )
    if matched_strong:
        return raw_score, f"Direct skill overlap found with supporting evidence: {', '.join(matched_strong[:6])}."
    if matched_moderate:
        return max(raw_score, 35), f"Secondary overlap found with supporting evidence: {', '.join(matched_moderate[:6])}."
    return 18, "The stored title/JD text shows little direct skill overlap."


def _dimension_target_alignment(role_hits: list[str]) -> tuple[int, str]:
    if role_hits:
        return min(100, 60 + len(role_hits) * 12), f"Target role overlap: {', '.join(role_hits[:4])}."
    return 30, "The posting does not explicitly mention your preferred role families."


def _dimension_seniority(title: str, text: str) -> tuple[int, str]:
    years_required = _extract_years_requirement(text)
    if years_required > TARGET_EXPERIENCE_MAX_YEARS:
        return 5, (
            f"The posting requires {years_required}+ years of experience, which is above your "
            f"{TARGET_EXPERIENCE_RANGE_LABEL}."
        )
    if years_required == TARGET_EXPERIENCE_MAX_YEARS:
        return 62, (
            f"The posting requires {years_required}+ years of experience, a reasonable stretch for a "
            f"{CANDIDATE_EXPERIENCE_YEARS}-year profile."
        )
    if _has_junior_title_signal(title):
        return 92, "The title signals an early-career role, which fits your current experience range."
    title_token = _find_title_seniority_token(title)
    if title_token:
        if title_token in VERY_SENIOR:
            return 10, f"The title looks far above your target level because it mentions {title_token}."
        return 45, f"The title may be slightly above your target level because it mentions {title_token}."
    body_signal = _body_seniority_signal(text)
    if not body_signal:
        return 85, "The role does not appear overly senior from the visible text."
    return 45, "The posting language suggests a somewhat senior role."


def _experience_penalty(text: str) -> tuple[int, str]:
    years_required = _extract_years_requirement(text)
    if years_required <= TARGET_EXPERIENCE_MAX_YEARS:
        return 0, ""
    return 100, (
        f"Blocked because the role requires {years_required}+ years of experience, above your "
        f"{TARGET_EXPERIENCE_RANGE_LABEL}."
    )


def _location_block(location: str, text: str, *, require_us_location: bool) -> str:
    if not require_us_location:
        return ""
    loc = (location or "").strip()
    if not loc or loc.lower() == "unknown location":
        return ""
    remote_scope = remote_scope_status(loc)
    if remote_scope == "us":
        return ""
    if remote_scope == "unspecified":
        return ""
    if is_us_location(loc):
        return ""
    if "remote" in text and ("united states" in text or re.search(r"\busa?\b", text)):
        return ""
    return f"Blocked because the visible location is outside the US: {location}."


def _dimension_location(location: str, text: str) -> tuple[int, str]:
    loc = (location or "").lower()
    remote_scope = remote_scope_status(loc)
    preferred_locations = [str(item).lower() for item in PROFILE.get("preferred_locations", []) if item]
    preferred_tokens: set[str] = set()
    for item in preferred_locations:
        preferred_tokens.update(part.strip() for part in re.split(r"[,/]", item) if part.strip())

    if remote_scope == "us" or "united states" in loc or re.search(r"\busa?\b", loc):
        return 90, "The location appears remote-friendly or clearly US-based."
    if remote_scope == "unspecified":
        return 69, "Remote scope is not specified clearly enough to assume US eligibility."
    if any(token and token in loc for token in preferred_tokens):
        return 92, f"The location matches one of your approved markets: {location}."
    if any(token and token in text for token in preferred_tokens):
        return 85, "The job text references one of your approved target markets."
    if location:
        return 65, f"The visible location is {location}."
    if "remote" in text:
        return 80, "The job text suggests remote work."
    return 40, "Location information is weak or missing."


def _remote_scope_review_reason(location: str, text: str) -> str:
    loc_text = f" {location or ''} {text or ''} ".lower()
    if "remote" not in loc_text:
        return ""
    if remote_scope_status(loc_text) == "us":
        return ""
    if remote_scope_status(loc_text) == "non_us":
        return ""
    return "Remote scope not specified — verify US eligibility before applying."


def _onsite_mismatch_cap(location: str, text: str) -> tuple[int, str]:
    loc = (location or "").lower()
    body = (text or "").lower()
    remote_like = any(token in loc or token in body for token in ("remote", "hybrid"))
    onsite_like = any(token in loc or token in body for token in ("onsite", "on-site", "in office", "in-office"))
    if remote_like or not onsite_like:
        return 100, ""

    preferred_locations = [str(item).lower() for item in PROFILE.get("preferred_locations", []) if item]
    preferred_tokens: set[str] = set()
    for item in preferred_locations:
        preferred_tokens.update(part.strip() for part in re.split(r"[,/]", item) if part.strip())

    if any(token and token in loc for token in preferred_tokens):
        return 100, ""
    return 58, f"Capped because this looks like an onsite role outside your preferred markets: {location or 'unknown location'}."


def _unsupported_specialized_requirement_gaps(title: str, description: str) -> list[str]:
    sections = _jd_sections(description)
    title_text = _norm(title)
    required_scope = _norm(
        sections.get("required", ""),
        sections.get("responsibilities", ""),
    )
    full_scope = _norm(sections.get("full_text", description))
    central_audio_role = any(
        token in title_text
        for token in (
            "audio",
            "speech",
            "asr",
            "speech to text",
            "speech-to-text",
        )
    )
    scan_scope = required_scope or (full_scope if central_audio_role else "")
    if not scan_scope:
        return []

    evidence_text = _resume_evidence_text()
    gaps: list[str] = []
    for label, aliases in SPECIALIZED_UNSUPPORTED_REQUIREMENT_ALIASES.items():
        if not any(_phrase_match(scan_scope, alias) for alias in aliases):
            continue
        if any(_phrase_match(evidence_text, alias) for alias in aliases):
            continue
        gaps.append(label)
    return gaps


def _specialized_domain_score_cap(title: str, description: str) -> tuple[int, str]:
    gaps = _unsupported_specialized_requirement_gaps(title, description)
    if len(gaps) >= 4:
        return 58, (
            "Capped because this role centers on specialized speech/audio or model-compression requirements "
            f"not backed by resume/project evidence: {', '.join(gaps[:5])}."
        )
    if len(gaps) >= 2:
        return 64, (
            "Capped because important specialized speech/audio or model-compression requirements are not backed "
            f"by resume/project evidence: {', '.join(gaps[:4])}."
        )
    return 100, ""


def _dimension_evidence(
    description: str,
    matched_strong: list[str],
    matched_moderate: list[str],
    assessment: SkillEvidenceAssessment,
) -> tuple[int, str]:
    desc = (description or "").strip()
    if assessment.critical_skill_gaps:
        return 34, (
            "The JD is detailed, but important requirements are not verified in experience/project evidence: "
            f"{', '.join(assessment.critical_skill_gaps[:4])}."
        )
    if assessment.responsibility_skill_gaps:
        return 48, (
            "The JD responsibilities are clear, but some core execution skills are not backed by experience/project evidence: "
            f"{', '.join(assessment.responsibility_skill_gaps[:4])}."
        )
    if assessment.unsupported_strong:
        return 52, (
            "The JD has overlap, but some strong-skill matches are only declared and not demonstrated: "
            f"{', '.join(assessment.unsupported_strong[:4])}."
        )
    if len(desc) >= 600 and (matched_strong or matched_moderate):
        return 88, "There is enough JD detail to support a more confident fit assessment."
    if len(desc) >= 200:
        return 68, "There is some JD text, but the evidence is still partial."
    return 32, "The evaluation is based mostly on title and limited metadata."


def _evidence_score_cap(description: str, source: str) -> tuple[int, str]:
    desc = (description or "").strip()
    if len(desc) >= 120:
        return 100, ""
    if _source_is_strong_first_party(source):
        return 100, ""
    if desc:
        return 59, "Capped because the stored JD is too thin to support a high-confidence match."
    return 45, "Capped because no job description is stored yet for this role."


def _resume_gap_score_cap(assessment: SkillEvidenceAssessment) -> tuple[int, str]:
    if assessment.critical_skill_gaps:
        return 72, (
            "Capped because at least one JD-critical skill is not backed by resume experience or project evidence: "
            f"{', '.join(assessment.critical_skill_gaps[:4])}."
        )
    if assessment.responsibility_skill_gaps:
        return 80, (
            "Capped because some core JD responsibility skills are not yet backed by resume experience or project evidence: "
            f"{', '.join(assessment.responsibility_skill_gaps[:4])}."
        )
    if len(assessment.unsupported_strong) >= 3:
        return 84, (
            "Capped because several strong keyword matches are skills-list-only and not supported by bullets/projects."
        )
    return 100, ""


def _dimension_risk(text: str) -> tuple[int, str]:
    sponsorship_block = _visa_sponsorship_block_reason(text)
    if sponsorship_block:
        return 0, sponsorship_block
    risk_flag = _clearance_signal_reason("", text)
    if risk_flag:
        return 18, risk_flag
    if PROFILE.get("needs_visa_sponsorship", False):
        return 92, "No clearance, citizenship, or sponsorship blockers were detected."
    return 92, "No clearance or citizenship blockers were detected."


def evaluate_job(
    title: str,
    description: str = "",
    *,
    company: str = "",
    location: str = "",
    source: str = "",
    require_us_location: bool | None = None,
    yes_threshold: int = DEFAULT_YES_THRESHOLD,
    maybe_threshold: int = DEFAULT_MAYBE_THRESHOLD,
    use_llm: bool = True,
) -> EvaluationResult:
    if require_us_location is None:
        require_us_location = EVAL_CFG.filter.require_us_location
    text = _norm(title, description, company, location)
    matched_strong, matched_moderate, strong_points, moderate_points = _match_skills(title, description)
    assessment = _assess_skill_evidence(title, description, matched_strong, matched_moderate)
    role_hits = _target_role_hits(text)

    location_block = _location_block(location, text, require_us_location=require_us_location)
    if location_block:
        dimensions = [
            EvaluationDimension("location_fit", 0.05, 0, location_block),
            EvaluationDimension("title_fit", 0.25, 0, "Evaluation stopped because the location is outside your allowed market."),
            EvaluationDimension("skill_overlap", 0.25, 0, "Evaluation stopped because the location is outside your allowed market."),
            EvaluationDimension("target_alignment", 0.15, 0, "Evaluation stopped because the location is outside your allowed market."),
            EvaluationDimension("seniority_fit", 0.10, 0, "Evaluation stopped because the location is outside your allowed market."),
            EvaluationDimension("evidence_quality", 0.10, 0, "Evaluation stopped because the location is outside your allowed market."),
            EvaluationDimension("risk", 0.10, 92, "No clearance or citizenship blockers were detected."),
        ]
        return EvaluationResult(
            score=0,
            label="no",
            grade="F",
            matched_strong=matched_strong,
            matched_moderate=matched_moderate,
            unsupported_strong=assessment.unsupported_strong,
            unsupported_moderate=assessment.unsupported_moderate,
            critical_skill_gaps=assessment.critical_skill_gaps,
            reasons=[location_block],
            fit_summary=location_block,
            dimensions=dimensions,
        )

    title_block = _title_level_block(title)
    if title_block:
        dimensions = [
            EvaluationDimension("seniority_fit", 0.10, 0, title_block),
            EvaluationDimension("title_fit", 0.25, 0, "Evaluation stopped because the title is above your target level."),
            EvaluationDimension("skill_overlap", 0.25, 0, "Evaluation stopped because the title is above your target level."),
            EvaluationDimension("target_alignment", 0.15, 0, "Evaluation stopped because the title is above your target level."),
            EvaluationDimension("location_fit", 0.05, 0, "Evaluation stopped because the title is above your target level."),
            EvaluationDimension("evidence_quality", 0.10, 0, "Evaluation stopped because the title is above your target level."),
            EvaluationDimension("risk", 0.10, 92, "No clearance or citizenship blockers were detected."),
        ]
        return EvaluationResult(
            score=0,
            label="no",
            grade="F",
            matched_strong=matched_strong,
            matched_moderate=matched_moderate,
            unsupported_strong=assessment.unsupported_strong,
            unsupported_moderate=assessment.unsupported_moderate,
            critical_skill_gaps=assessment.critical_skill_gaps,
            reasons=[title_block],
            fit_summary=title_block,
            dimensions=dimensions,
        )

    years_required = _extract_years_requirement(text)
    if years_required > TARGET_EXPERIENCE_MAX_YEARS:
        block_reason = (
            f"Blocked because the role requires {years_required}+ years of experience, above your "
            f"{TARGET_EXPERIENCE_RANGE_LABEL}."
        )
        dimensions = [
            EvaluationDimension("seniority_fit", 0.10, 0, block_reason),
            EvaluationDimension("title_fit", 0.25, 0, "Evaluation stopped because the experience requirement is above your target range."),
            EvaluationDimension("skill_overlap", 0.25, 0, "Evaluation stopped because the experience requirement is above your target range."),
            EvaluationDimension("target_alignment", 0.15, 0, "Evaluation stopped because the experience requirement is above your target range."),
            EvaluationDimension("location_fit", 0.05, 0, "Evaluation stopped because the experience requirement is above your target range."),
            EvaluationDimension("evidence_quality", 0.10, 0, "Evaluation stopped because the experience requirement is above your target range."),
            EvaluationDimension("risk", 0.10, 92, "No clearance or citizenship blockers were detected."),
        ]
        return EvaluationResult(
            score=0,
            label="no",
            grade="F",
            matched_strong=matched_strong,
            matched_moderate=matched_moderate,
            unsupported_strong=assessment.unsupported_strong,
            unsupported_moderate=assessment.unsupported_moderate,
            critical_skill_gaps=assessment.critical_skill_gaps,
            reasons=[block_reason],
            fit_summary=block_reason,
            dimensions=dimensions,
        )

    sponsorship_block = _visa_sponsorship_block_reason(text)
    if sponsorship_block:
        dimensions = [
            EvaluationDimension("risk", 0.10, 0, sponsorship_block),
            EvaluationDimension("title_fit", 0.25, 0, "Evaluation stopped because the posting does not offer visa sponsorship."),
            EvaluationDimension("skill_overlap", 0.25, 0, "Evaluation stopped because the posting does not offer visa sponsorship."),
            EvaluationDimension("target_alignment", 0.15, 0, "Evaluation stopped because the posting does not offer visa sponsorship."),
            EvaluationDimension("seniority_fit", 0.10, 0, "Evaluation stopped because the posting does not offer visa sponsorship."),
            EvaluationDimension("location_fit", 0.05, 0, "Evaluation stopped because the posting does not offer visa sponsorship."),
            EvaluationDimension("evidence_quality", 0.10, 0, "Evaluation stopped because the posting does not offer visa sponsorship."),
        ]
        return EvaluationResult(
            score=0,
            label="no",
            grade="F",
            matched_strong=matched_strong,
            matched_moderate=matched_moderate,
            unsupported_strong=assessment.unsupported_strong,
            unsupported_moderate=assessment.unsupported_moderate,
            critical_skill_gaps=assessment.critical_skill_gaps,
            reasons=[sponsorship_block],
            fit_summary=sponsorship_block,
            dimensions=dimensions,
        )

    dimensions = [
        EvaluationDimension("title_fit", 0.25, *_dimension_title_fit(title, matched_strong, matched_moderate, assessment)),
        EvaluationDimension("skill_overlap", 0.25, *_dimension_skill_overlap(matched_strong, matched_moderate, strong_points, moderate_points, assessment)),
        EvaluationDimension("target_alignment", 0.15, *_dimension_target_alignment(role_hits)),
        EvaluationDimension("seniority_fit", 0.10, *_dimension_seniority(title, text)),
        EvaluationDimension("location_fit", 0.05, *_dimension_location(location, text)),
        EvaluationDimension("evidence_quality", 0.10, *_dimension_evidence(description, matched_strong, matched_moderate, assessment)),
        EvaluationDimension("risk", 0.10, *_dimension_risk(text)),
    ]

    experience_penalty, experience_reason = _experience_penalty(text)
    score = round(sum(d.weighted_points for d in dimensions) - experience_penalty)
    evidence_cap, evidence_cap_reason = _evidence_score_cap(description, source)
    resume_cap, resume_cap_reason = _resume_gap_score_cap(assessment)
    specialized_cap, specialized_cap_reason = _specialized_domain_score_cap(title, description)
    onsite_cap, onsite_cap_reason = _onsite_mismatch_cap(location, text)
    remote_review_reason = _remote_scope_review_reason(location, text)
    score = min(score, evidence_cap)
    score = min(score, resume_cap)
    score = min(score, specialized_cap)
    score = min(score, onsite_cap)
    if remote_review_reason:
        score = min(score, 69)
    score = max(0, min(score, 100))
    grade = _score_to_grade(score)
    label = _score_to_label(score, yes_threshold=yes_threshold, maybe_threshold=maybe_threshold)

    ordered = sorted(dimensions, key=lambda d: d.weighted_points, reverse=True)
    reasons = [d.reason for d in ordered[:4]]
    if experience_reason:
        reasons.insert(0, experience_reason)
    if evidence_cap_reason and evidence_cap_reason not in reasons:
        reasons.insert(0, evidence_cap_reason)
    if resume_cap_reason and resume_cap_reason not in reasons:
        reasons.insert(0, resume_cap_reason)
    if specialized_cap_reason and specialized_cap_reason not in reasons:
        reasons.insert(0, specialized_cap_reason)
    if onsite_cap_reason and onsite_cap_reason not in reasons:
        reasons.insert(0, onsite_cap_reason)
    if remote_review_reason and remote_review_reason not in reasons:
        reasons.insert(0, remote_review_reason)
    reasons = reasons[:4]
    fit_summary = f"Grade {grade} ({score}/100). " + " ".join(reasons[:3])

    result = EvaluationResult(
        score=score,
        label=label,
        grade=grade,
        matched_strong=matched_strong,
        matched_moderate=matched_moderate,
        unsupported_strong=assessment.unsupported_strong,
        unsupported_moderate=assessment.unsupported_moderate,
        critical_skill_gaps=assessment.critical_skill_gaps,
        reasons=reasons,
        fit_summary=fit_summary,
        dimensions=dimensions,
    )
    llm_review = maybe_review_job(
        cfg=EVAL_CFG,
        title=title,
        company=company,
        location=location,
        description=description,
        deterministic_payload=result.to_dict(),
    ) if use_llm else None
    if llm_review is None:
        return result
    if llm_review.used:
        adjusted_score = apply_llm_delta(
            score=result.score,
            critical_skill_gaps=result.critical_skill_gaps,
            review=llm_review,
            max_adjustment=EVAL_CFG.llm_scoring.max_score_adjustment,
        )
        result.llm_review = llm_review.to_dict()
        if adjusted_score != result.score:
            result.score = adjusted_score
            result.grade = _score_to_grade(result.score)
            result.label = _score_to_label(
                result.score,
                yes_threshold=yes_threshold,
                maybe_threshold=maybe_threshold,
            )
            if llm_review.reason:
                result.reasons.insert(0, f"LLM reviewer adjusted score by {llm_review.score_delta}: {llm_review.reason}")
        elif llm_review.reason:
            result.reasons.append(f"LLM reviewer kept score unchanged: {llm_review.reason}")
        result.reasons = result.reasons[:4]
        result.fit_summary = f"Grade {result.grade} ({result.score}/100). " + " ".join(result.reasons[:3])
    return result
