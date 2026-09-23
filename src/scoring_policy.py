from __future__ import annotations

from dataclasses import dataclass
from statistics import median

DEFAULT_YES_THRESHOLD = 70
DEFAULT_MAYBE_THRESHOLD = 40
MIN_THRESHOLD_FEEDBACK_ROWS = 8

PREFERENCE_ACTIONS = frozenset({"interested", "applied", "dismissed", "archived", "shortlisted"})
OUTCOME_POSITIVE_ACTIONS = frozenset({"responded", "interview", "onsite", "offer"})
OUTCOME_NEGATIVE_ACTIONS = frozenset({"screen_reject", "rejected", "ghosted"})
STRONG_POSITIVE_ACTIONS = frozenset({"interview", "onsite", "offer"})


@dataclass(frozen=True)
class LabelThresholds:
    yes: int = DEFAULT_YES_THRESHOLD
    maybe: int = DEFAULT_MAYBE_THRESHOLD
    calibrated: bool = False
    positive_count: int = 0
    negative_count: int = 0
    reason: str = "default"


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


def label_for_score(score: int, *, yes_threshold: int = DEFAULT_YES_THRESHOLD, maybe_threshold: int = DEFAULT_MAYBE_THRESHOLD) -> str:
    if score >= yes_threshold:
        return "yes"
    if score >= maybe_threshold:
        return "maybe"
    return "no"


def calibrate_thresholds(feedback_rows: list[dict]) -> LabelThresholds:
    scored_rows = []
    for row in feedback_rows:
        action = (row.get("action") or "").strip().lower()
        try:
            score = int(row.get("score") or 0)
        except (TypeError, ValueError):
            continue
        if action not in OUTCOME_POSITIVE_ACTIONS | OUTCOME_NEGATIVE_ACTIONS:
            continue
        scored_rows.append((action, score))

    positives = [score for action, score in scored_rows if action in OUTCOME_POSITIVE_ACTIONS]
    negatives = [score for action, score in scored_rows if action in OUTCOME_NEGATIVE_ACTIONS]
    strong_positives = [score for action, score in scored_rows if action in STRONG_POSITIVE_ACTIONS]

    if len(scored_rows) < MIN_THRESHOLD_FEEDBACK_ROWS or len(positives) < 3 or len(negatives) < 3:
        return LabelThresholds(
            calibrated=False,
            positive_count=len(positives),
            negative_count=len(negatives),
            reason="insufficient_outcome_data",
        )

    positive_anchor = strong_positives or positives
    yes_threshold = round(median(positive_anchor) - 3)
    maybe_threshold = round((median(positives) + median(negatives)) / 2)
    maybe_threshold = _clamp(maybe_threshold, 38, 60)
    yes_threshold = _clamp(yes_threshold, max(68, maybe_threshold + 10), 85)

    return LabelThresholds(
        yes=yes_threshold,
        maybe=maybe_threshold,
        calibrated=True,
        positive_count=len(positives),
        negative_count=len(negatives),
        reason="outcome_calibrated",
    )


def resume_fit_cap(resume_match_score: int) -> tuple[int, str]:
    if resume_match_score >= 55:
        return 100, ""
    if resume_match_score >= 45:
        return 82, "Capped because the resume/JD match is only moderate."
    if resume_match_score >= 35:
        return 74, "Capped because the resume/JD match is weak for a high-confidence recommendation."
    if resume_match_score >= 25:
        return 64, "Capped because the JD overlap is thin and employer boosts should not rescue the role."
    return 54, "Capped because the resume/JD match is very weak."
