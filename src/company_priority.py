"""Lightweight company priority and exclusion rules.

These rules are intentionally small and explicit. They sit on top of the core
job-fit evaluator and let us:
  - drop categories that are consistently poor fits
  - boost employers we want to bias toward in alert ordering
"""
from __future__ import annotations

HARD_EXCLUDE_COMPANIES: tuple[str, ...] = (
    "teksystems",
    "robert half",
    "insight global",
    "randstad",
    "adecco",
    "collabera",
    "kforce",
    "cybercoders",
    "aerotek",
    "booz allen",
    "leidos",
    "peraton",
    "saic",
    "caci",
    "raytheon",
    "northrop grumman",
    "general dynamics",
)

TARGET_COMPANY_BONUSES: tuple[tuple[str, int], ...] = (
    ("anthropic", 10),
    ("databricks", 10),
    ("stripe", 10),
    ("hugging face", 8),
    ("weights & biases", 8),
    ("deepgram", 8),
    ("hightouch", 8),
    ("workos", 8),
    ("intercom", 8),
    ("mongodb", 6),
    ("airtable", 6),
    ("applied intuition", 6),
    ("pinterest", 6),
    ("lyft", 6),
    ("instacart", 6),
    ("amazon", 4),
    ("google", 4),
    ("meta", 4),
    ("microsoft", 4),
    ("nvidia", 4),
)


def company_score_adjustment(company: str) -> tuple[int, str]:
    normalized = (company or "").strip().lower()
    if not normalized:
        return 0, "no_company"

    for excluded in HARD_EXCLUDE_COMPANIES:
        if excluded in normalized:
            return -999, f"excluded:{excluded}"

    for target, bonus in TARGET_COMPANY_BONUSES:
        if target in normalized:
            return bonus, f"target:{target}"

    return 0, "neutral"

