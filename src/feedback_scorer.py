"""Lightweight feedback-based score adjustment."""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass

PREFERENCE_ACTIONS = frozenset({"interested", "shortlisted", "applied", "dismissed", "archived"})
OUTCOME_ACTIONS = frozenset({"responded", "interview", "onsite", "offer", "screen_reject", "rejected", "ghosted"})
ACTION_WEIGHTS = {
    "interested": 0.22,
    "shortlisted": 0.38,
    "applied": 0.50,
    "dismissed": -0.70,
    "archived": -0.35,
}
POSITIVE_ACTIONS = frozenset(action for action, weight in ACTION_WEIGHTS.items() if weight > 0)
NEGATIVE_ACTIONS = frozenset(action for action, weight in ACTION_WEIGHTS.items() if weight < 0)
STOPWORDS = frozenset(
    {
        "a", "an", "the", "and", "or", "of", "in", "at", "to", "for", "with", "on", "by",
        "engineer", "engineering", "analyst", "data", "senior", "staff", "lead", "ii", "iii", "iv",
    }
)
MIN_FEEDBACK_ROWS = 6
MAX_BOOST = 12


@dataclass
class FeedbackAdjustment:
    delta: int
    reasons: list[str]


def _tokenize_title(title: str) -> list[str]:
    tokens = re.findall(r"[a-z0-9]+", (title or "").lower())
    return [token for token in tokens if token not in STOPWORDS and len(token) > 2]


def _signal_score(total_weight: float, count: int) -> float:
    if count <= 0:
        return 0.0
    return total_weight / count


def build_feedback_adjustments(jobs: list, feedback_rows: list[dict]) -> dict[str, FeedbackAdjustment]:
    usable_rows = [
        row for row in feedback_rows
        if (row.get("action") or "").strip().lower() in PREFERENCE_ACTIONS
    ]
    if len(usable_rows) < MIN_FEEDBACK_ROWS:
        return {getattr(job, "key", ""): FeedbackAdjustment(delta=0, reasons=[]) for job in jobs}

    token_stats: dict[str, list[float | int]] = defaultdict(lambda: [0.0, 0])
    company_stats: dict[str, list[float | int]] = defaultdict(lambda: [0.0, 0])
    source_stats: dict[str, list[float | int]] = defaultdict(lambda: [0.0, 0])

    for row in usable_rows:
        action = (row.get("action") or "").strip().lower()
        weight = ACTION_WEIGHTS.get(action, 0.0)
        company = (row.get("company") or "").strip().lower()
        source = (row.get("source") or "").strip().lower()
        title_tokens = _tokenize_title(row.get("title") or "")
        if company:
            company_stats[company][0] += weight
            company_stats[company][1] += 1
        if source:
            source_stats[source][0] += weight
            source_stats[source][1] += 1
        for token in title_tokens:
            token_stats[token][0] += weight
            token_stats[token][1] += 1

    adjustments: dict[str, FeedbackAdjustment] = {}
    for job in jobs:
        reasons: list[str] = []
        raw = 0.0

        company = (getattr(job, "company", "") or "").strip().lower()
        source = (getattr(job, "source", "") or "").strip().lower()
        title_tokens = _tokenize_title(getattr(job, "title", "") or "")

        if company in company_stats:
            signal = _signal_score(company_stats[company][0], company_stats[company][1])
            raw += signal * 5.0
            if signal > 0.35:
                reasons.append("company feedback positive")
            elif signal < -0.35:
                reasons.append("company feedback negative")

        if source in source_stats:
            signal = _signal_score(source_stats[source][0], source_stats[source][1])
            raw += signal * 2.5
            if signal > 0.45:
                reasons.append("source feedback positive")
            elif signal < -0.45:
                reasons.append("source feedback negative")

        token_contrib = 0.0
        token_hits = 0
        for token in title_tokens[:6]:
            if token not in token_stats:
                continue
            token_hits += 1
            token_contrib += _signal_score(token_stats[token][0], token_stats[token][1])
        if token_hits:
            avg_token_signal = token_contrib / token_hits
            raw += avg_token_signal * 6.5
            if avg_token_signal > 0.30:
                reasons.append("title feedback positive")
            elif avg_token_signal < -0.30:
                reasons.append("title feedback negative")

        delta = max(-MAX_BOOST, min(MAX_BOOST, int(round(raw))))
        adjustments[getattr(job, "key", "")] = FeedbackAdjustment(delta=delta, reasons=reasons[:2])

    return adjustments

