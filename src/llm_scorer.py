"""Optional LLM second-pass reviewer for Job Radar scoring.

The deterministic scorer remains the source of truth. This module only adds a
small, auditable adjustment for borderline roles when explicitly enabled.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import logging
import re
import threading
from typing import Any

import requests

log = logging.getLogger(__name__)

_DEFAULT_ENDPOINTS = {
    "google": "https://generativelanguage.googleapis.com/v1beta",
    "groq": "https://api.groq.com/openai/v1",
    "openrouter": "https://openrouter.ai/api/v1",
}
_DEFAULT_MODELS = {
    "google": "gemini-3.5-flash-lite",
    "groq": "openai/gpt-oss-20b",
    "openrouter": "openrouter/free",
}
_GEMINI_FALLBACK_MODELS = (
    "gemini-3.5-flash-lite",
    "gemini-3.6-flash",
    "gemini-2.5-flash-lite",
    "gemini-2.5-flash",
)

_COUNTER_LOCK = threading.Lock()
_COUNTER_DAY = ""
_PROCESS_CALLS = 0
_DAILY_CALLS = 0
_FAILURES = 0
_DISABLED_BY_FAILURE = False
_FAILURE_LIMIT = 5
_RATE_LIMITED = False


@dataclass
class LLMReview:
    provider: str
    model: str
    used: bool
    score_delta: int = 0
    reason: str = ""
    evidence_gaps: list[str] | None = None
    seniority_risk: str = ""
    sponsorship_risk: str = ""
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["evidence_gaps"] = list(self.evidence_gaps or [])
        return data


def maybe_review_job(
    *,
    cfg: Any,
    title: str,
    company: str,
    location: str,
    description: str,
    deterministic_payload: dict[str, Any],
) -> LLMReview:
    llm_cfg = getattr(cfg, "llm_scoring", None)
    if not llm_cfg or not getattr(llm_cfg, "enabled", False):
        return LLMReview(provider="", model="", used=False)

    provider = str(getattr(llm_cfg, "provider", "google") or "google").strip().lower()
    model = str(getattr(llm_cfg, "model", "") or _DEFAULT_MODELS.get(provider, "gemini-2.5-flash-lite")).strip()
    if provider not in _DEFAULT_ENDPOINTS:
        return LLMReview(provider=provider, model=model, used=False, error=f"Unsupported LLM provider: {provider}")

    api_key = str(getattr(llm_cfg, "api_key", "") or "").strip()
    if not api_key:
        return LLMReview(provider=provider, model=model, used=False, error=f"Missing API key for {provider}")
    if _rate_limited():
        return LLMReview(provider=provider, model=model, used=False, error="LLM scoring skipped after provider rate limit")
    if _disabled_by_failures():
        return LLMReview(provider=provider, model=model, used=False, error="LLM scoring disabled after repeated API failures")

    score = int(deterministic_payload.get("score") or 0)
    min_score = int(getattr(llm_cfg, "only_score_min", 55) or 55)
    max_score = int(getattr(llm_cfg, "only_score_max", 80) or 80)
    if score < min_score or score > max_score:
        return LLMReview(provider=provider, model=model, used=False)

    if not _reserve_call(llm_cfg):
        return LLMReview(provider=provider, model=model, used=False, error="LLM scoring call cap reached")

    try:
        review: LLMReview
        if provider == "google":
            review = _review_with_gemini(
                llm_cfg=llm_cfg,
                api_key=api_key,
                model=model,
                title=title,
                company=company,
                location=location,
                description=description,
                deterministic_payload=deterministic_payload,
            )
        else:
            review = _review_with_chat_api(
                llm_cfg=llm_cfg,
                provider=provider,
                api_key=api_key,
                model=model,
                title=title,
                company=company,
                location=location,
                description=description,
                deterministic_payload=deterministic_payload,
            )
        _record_success()
        return review
    except Exception as exc:
        safe_error = _safe_error_message(exc)
        log.warning("LLM scoring review failed for %s at %s: %s", title, company, safe_error)
        _record_failure(provider, model, safe_error, rate_limited=_is_rate_limit_error(exc))
        return LLMReview(provider=provider, model=model, used=False, error=f"{type(exc).__name__}: {safe_error}")


def review_jobs_batch(*, cfg: Any, jobs: list[dict[str, Any]]) -> dict[str, LLMReview]:
    """Review multiple deterministic evaluations with one provider request."""
    llm_cfg = getattr(cfg, "llm_scoring", None)
    if not jobs or not llm_cfg or not getattr(llm_cfg, "enabled", False):
        return {}

    provider = str(getattr(llm_cfg, "provider", "google") or "google").strip().lower()
    model = str(getattr(llm_cfg, "model", "") or _DEFAULT_MODELS.get(provider, "gemini-2.5-flash-lite")).strip()
    if provider not in _DEFAULT_ENDPOINTS:
        return {}

    api_key = str(getattr(llm_cfg, "api_key", "") or "").strip()
    if not api_key or _rate_limited() or _disabled_by_failures():
        return {}

    min_score = int(getattr(llm_cfg, "only_score_min", 55) or 55)
    max_score = int(getattr(llm_cfg, "only_score_max", 80) or 80)
    eligible: list[dict[str, Any]] = []
    for job in jobs:
        payload = job.get("deterministic_payload") or {}
        try:
            score = int(payload.get("score") or 0)
        except Exception:
            score = 0
        if min_score <= score <= max_score:
            eligible.append(job)
    if not eligible:
        return {}

    batch_size = max(1, min(20, int(getattr(llm_cfg, "batch_size", 10) or 10)))
    eligible = eligible[:batch_size]
    request_jobs: list[dict[str, Any]] = []
    key_by_request_id: dict[str, str] = {}
    for idx, job in enumerate(eligible, start=1):
        request_id = str(idx)
        request_job = dict(job)
        request_job["_llm_batch_id"] = request_id
        request_jobs.append(request_job)
        key_by_request_id[request_id] = str(job.get("key") or "")
    if not _reserve_call(llm_cfg):
        return {}

    try:
        if provider == "google":
            reviews = _review_batch_with_gemini(
                llm_cfg=llm_cfg,
                api_key=api_key,
                model=model,
                jobs=request_jobs,
                key_by_request_id=key_by_request_id,
            )
        else:
            reviews = _review_batch_with_chat_api(
                llm_cfg=llm_cfg,
                provider=provider,
                api_key=api_key,
                model=model,
                jobs=request_jobs,
                key_by_request_id=key_by_request_id,
            )
        _record_success()
        return reviews
    except Exception as exc:
        safe_error = _safe_error_message(exc)
        log.warning("LLM batch scoring review failed for %d job(s): %s", len(eligible), safe_error)
        _record_failure(provider, model, safe_error, rate_limited=_is_rate_limit_error(exc))
        return {}


def apply_llm_delta(
    *,
    score: int,
    critical_skill_gaps: list[str],
    review: LLMReview,
    max_adjustment: int,
) -> int:
    """Apply a bounded delta without letting LLMs erase hard evidence gaps."""
    if not review.used:
        return score
    delta = max(-max_adjustment, min(max_adjustment, int(review.score_delta or 0)))
    if critical_skill_gaps and delta > 0:
        delta = 0
    return max(0, min(100, score + delta))


def _reserve_call(llm_cfg: Any) -> bool:
    global _COUNTER_DAY, _DAILY_CALLS, _PROCESS_CALLS
    today = datetime.now(timezone.utc).date().isoformat()
    with _COUNTER_LOCK:
        if _COUNTER_DAY != today:
            _COUNTER_DAY = today
            _DAILY_CALLS = 0
        process_limit = int(getattr(llm_cfg, "max_calls_per_process", 100) or 100)
        daily_limit = int(getattr(llm_cfg, "max_daily_calls", 300) or 300)
        if _PROCESS_CALLS >= process_limit or _DAILY_CALLS >= daily_limit:
            return False
        _PROCESS_CALLS += 1
        _DAILY_CALLS += 1
        return True


def _rate_limited() -> bool:
    with _COUNTER_LOCK:
        return _RATE_LIMITED


def _disabled_by_failures() -> bool:
    with _COUNTER_LOCK:
        return _DISABLED_BY_FAILURE


def _record_failure(provider: str, model: str, error: str, *, rate_limited: bool = False) -> None:
    global _DISABLED_BY_FAILURE, _FAILURES, _RATE_LIMITED
    with _COUNTER_LOCK:
        if rate_limited:
            _RATE_LIMITED = True
            log.warning(
                "LLM scoring disabled for this process after provider rate limit for %s/%s. Last error: %s",
                provider,
                model,
                error,
            )
            return
        _FAILURES += 1
        if _FAILURES >= _FAILURE_LIMIT and not _DISABLED_BY_FAILURE:
            _DISABLED_BY_FAILURE = True
            log.warning(
                "LLM scoring disabled for this process after %d consecutive API failures for %s/%s. Last error: %s",
                _FAILURES,
                provider,
                model,
                error,
            )


def _record_success() -> None:
    global _FAILURES
    with _COUNTER_LOCK:
        _FAILURES = 0


def _review_with_gemini(
    *,
    llm_cfg: Any,
    api_key: str,
    model: str,
    title: str,
    company: str,
    location: str,
    description: str,
    deterministic_payload: dict[str, Any],
) -> LLMReview:
    endpoint = _endpoint_for_provider(llm_cfg, "google")
    timeout = int(getattr(llm_cfg, "timeout", 20) or 20)
    max_desc = int(getattr(llm_cfg, "max_description_chars", 6000) or 6000)
    prompt = _build_prompt(
        title=title,
        company=company,
        location=location,
        description=(description or "")[:max_desc],
        deterministic_payload=deterministic_payload,
    )
    body = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0,
            "responseMimeType": "application/json",
        },
    }
    response = None
    last_error: Exception | None = None
    for candidate_model in _candidate_gemini_models(model):
        url = f"{endpoint}/models/{candidate_model}:generateContent"
        try:
            response = requests.post(url, headers={"x-goog-api-key": api_key}, json=body, timeout=timeout)
            response.raise_for_status()
            model = candidate_model
            break
        except requests.HTTPError as exc:
            last_error = exc
            status = exc.response.status_code if exc.response is not None else 0
            if status != 404:
                raise
        except Exception as exc:
            last_error = exc
            raise
    if response is None:
        raise last_error or RuntimeError("Gemini request failed before receiving a response")
    text = _extract_gemini_text(response.json())
    parsed = _parse_json_object(text)
    max_adjustment = int(getattr(llm_cfg, "max_score_adjustment", 8) or 8)
    delta = max(-max_adjustment, min(max_adjustment, int(parsed.get("score_delta") or 0)))
    return LLMReview(
        provider="google",
        model=model,
        used=True,
        score_delta=delta,
        reason=str(parsed.get("reason") or "")[:500],
        evidence_gaps=[str(item)[:80] for item in parsed.get("evidence_gaps") or []][:8],
        seniority_risk=str(parsed.get("seniority_risk") or "")[:80],
        sponsorship_risk=str(parsed.get("sponsorship_risk") or "")[:80],
    )


def _candidate_gemini_models(model: str) -> list[str]:
    models: list[str] = []
    for item in (model, *_GEMINI_FALLBACK_MODELS):
        cleaned = str(item or "").strip()
        if cleaned and cleaned not in models:
            models.append(cleaned)
    return models


def _review_with_chat_api(
    *,
    llm_cfg: Any,
    provider: str,
    api_key: str,
    model: str,
    title: str,
    company: str,
    location: str,
    description: str,
    deterministic_payload: dict[str, Any],
) -> LLMReview:
    endpoint = _endpoint_for_provider(llm_cfg, provider)
    timeout = int(getattr(llm_cfg, "timeout", 20) or 20)
    max_desc = int(getattr(llm_cfg, "max_description_chars", 6000) or 6000)
    prompt = _build_prompt(
        title=title,
        company=company,
        location=location,
        description=(description or "")[:max_desc],
        deterministic_payload=deterministic_payload,
    )
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    if provider == "openrouter":
        headers["HTTP-Referer"] = "http://127.0.0.1:8080"
        headers["X-Title"] = "Job Radar"
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": "Return only valid JSON. Be conservative and evidence-bound."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0,
        "response_format": {"type": "json_object"},
    }
    response = requests.post(f"{endpoint}/chat/completions", headers=headers, json=body, timeout=timeout)
    response.raise_for_status()
    text = _extract_chat_text(response.json())
    parsed = _parse_json_object(text)
    max_adjustment = int(getattr(llm_cfg, "max_score_adjustment", 8) or 8)
    delta = max(-max_adjustment, min(max_adjustment, int(parsed.get("score_delta") or 0)))
    return LLMReview(
        provider=provider,
        model=model,
        used=True,
        score_delta=delta,
        reason=str(parsed.get("reason") or "")[:500],
        evidence_gaps=[str(item)[:80] for item in parsed.get("evidence_gaps") or []][:8],
        seniority_risk=str(parsed.get("seniority_risk") or "")[:80],
        sponsorship_risk=str(parsed.get("sponsorship_risk") or "")[:80],
    )


def _review_batch_with_gemini(
    *,
    llm_cfg: Any,
    api_key: str,
    model: str,
    jobs: list[dict[str, Any]],
    key_by_request_id: dict[str, str],
) -> dict[str, LLMReview]:
    endpoint = _endpoint_for_provider(llm_cfg, "google")
    timeout = max(45, int(getattr(llm_cfg, "timeout", 20) or 20))
    prompt = _build_batch_prompt(jobs=jobs, llm_cfg=llm_cfg)
    body = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0,
            "responseMimeType": "application/json",
        },
    }
    response = None
    last_error: Exception | None = None
    for candidate_model in _candidate_gemini_models(model):
        url = f"{endpoint}/models/{candidate_model}:generateContent"
        try:
            response = requests.post(url, headers={"x-goog-api-key": api_key}, json=body, timeout=timeout)
            response.raise_for_status()
            model = candidate_model
            break
        except requests.HTTPError as exc:
            last_error = exc
            status = exc.response.status_code if exc.response is not None else 0
            if status != 404:
                raise
        except Exception as exc:
            last_error = exc
            raise
    if response is None:
        raise last_error or RuntimeError("Gemini batch request failed before receiving a response")
    text = _extract_gemini_text(response.json())
    try:
        parsed = _parse_json_object(text)
    except json.JSONDecodeError as exc:
        preview = re.sub(r"\s+", " ", text or "")[:500]
        raise ValueError(f"Gemini returned malformed batch JSON: {exc}; preview={preview}") from exc
    return _reviews_from_batch_payload(
        parsed,
        provider="google",
        model=model,
        key_by_request_id=key_by_request_id,
        max_adjustment=int(getattr(llm_cfg, "max_score_adjustment", 8) or 8),
    )


def _review_batch_with_chat_api(
    *,
    llm_cfg: Any,
    provider: str,
    api_key: str,
    model: str,
    jobs: list[dict[str, Any]],
    key_by_request_id: dict[str, str],
) -> dict[str, LLMReview]:
    endpoint = _endpoint_for_provider(llm_cfg, provider)
    timeout = max(45, int(getattr(llm_cfg, "timeout", 20) or 20))
    prompt = _build_batch_prompt(jobs=jobs, llm_cfg=llm_cfg)
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    if provider == "openrouter":
        headers["HTTP-Referer"] = "http://127.0.0.1:8080"
        headers["X-Title"] = "Job Radar"
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": "Return only valid JSON. Be conservative and evidence-bound."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0,
        "response_format": {"type": "json_object"},
    }
    response = requests.post(f"{endpoint}/chat/completions", headers=headers, json=body, timeout=timeout)
    response.raise_for_status()
    text = _extract_chat_text(response.json())
    try:
        parsed = _parse_json_object(text)
    except json.JSONDecodeError as exc:
        preview = re.sub(r"\s+", " ", text or "")[:500]
        raise ValueError(f"{provider} returned malformed batch JSON: {exc}; preview={preview}") from exc
    return _reviews_from_batch_payload(
        parsed,
        provider=provider,
        model=model,
        key_by_request_id=key_by_request_id,
        max_adjustment=int(getattr(llm_cfg, "max_score_adjustment", 8) or 8),
    )


def _endpoint_for_provider(llm_cfg: Any, provider: str) -> str:
    configured = str(getattr(llm_cfg, "endpoint", "") or "").rstrip("/")
    default_google = _DEFAULT_ENDPOINTS["google"]
    if configured and (provider == "google" or configured != default_google):
        return configured
    return _DEFAULT_ENDPOINTS[provider]


def _build_prompt(
    *,
    title: str,
    company: str,
    location: str,
    description: str,
    deterministic_payload: dict[str, Any],
) -> str:
    compact_payload = {
        "score": deterministic_payload.get("score"),
        "label": deterministic_payload.get("label"),
        "grade": deterministic_payload.get("grade"),
        "matched_strong": deterministic_payload.get("matched_strong", [])[:12],
        "matched_moderate": deterministic_payload.get("matched_moderate", [])[:12],
        "critical_skill_gaps": deterministic_payload.get("critical_skill_gaps", [])[:8],
        "reasons": deterministic_payload.get("reasons", [])[:4],
    }
    return (
        "You are a conservative second-pass job-fit reviewer. The deterministic scorer is the source of truth.\n"
        "Candidate summary: 3 years combined data science/data engineering/analytics experience; strong Python, SQL, "
        "Spark/PySpark, Hadoop/HDFS/MapReduce, Hive/HiveQL, AWS Glue/S3/Lambda/Kinesis/SageMaker/Redshift/EMR, "
        "Snowflake, Airflow, dbt, Docker, GitHub Actions, Tableau/Power BI; GenAI experience with OpenAI API, RAG, "
        "MCP, LangChain local RAG, FAISS, embeddings, and evaluation harnesses. React is basic exposure only. "
        "Databricks is project-level. Delta Lake is concepts/working knowledge only. Future visa sponsorship is needed.\n\n"
        "Rules:\n"
        "- Return only JSON.\n"
        "- Do not override no-sponsorship, citizenship/clearance, non-US location, or >3 years required blockers.\n"
        "- Do not give positive adjustment when critical skill gaps are real.\n"
        "- score_delta must be an integer from -8 to 8.\n"
        "- Prefer 0 unless the deterministic score missed a clear evidence-backed signal.\n\n"
        f"Job: {title} at {company}, {location}\n"
        f"Deterministic evaluation JSON: {json.dumps(compact_payload, ensure_ascii=True)}\n"
        f"Job description:\n{description}\n\n"
        "JSON schema: {"
        "\"score_delta\": 0, "
        "\"evidence_gaps\": [\"gap\"], "
        "\"seniority_risk\": \"low|medium|high\", "
        "\"sponsorship_risk\": \"none_detected|blocker|unclear\", "
        "\"reason\": \"short explanation\""
        "}"
    )


def _compact_deterministic_payload(deterministic_payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "score": deterministic_payload.get("score"),
        "label": deterministic_payload.get("label"),
        "grade": deterministic_payload.get("grade"),
        "matched_strong": deterministic_payload.get("matched_strong", [])[:12],
        "matched_moderate": deterministic_payload.get("matched_moderate", [])[:12],
        "critical_skill_gaps": deterministic_payload.get("critical_skill_gaps", [])[:8],
        "reasons": deterministic_payload.get("reasons", [])[:4],
    }


def _build_batch_prompt(*, jobs: list[dict[str, Any]], llm_cfg: Any) -> str:
    max_desc = int(getattr(llm_cfg, "max_description_chars", 6000) or 6000)
    max_desc_per_job = max(500, min(1200, max_desc))
    compact_jobs: list[dict[str, Any]] = []
    for job in jobs:
        compact_jobs.append(
            {
                "id": str(job.get("_llm_batch_id") or "")[:20],
                "title": str(job.get("title") or "")[:180],
                "company": str(job.get("company") or "")[:120],
                "location": str(job.get("location") or "")[:160],
                "deterministic_evaluation": _compact_deterministic_payload(job.get("deterministic_payload") or {}),
                "description": str(job.get("description") or "")[:max_desc_per_job],
            }
        )
    return (
        "You are a conservative second-pass job-fit reviewer. The deterministic scorer is the source of truth.\n"
        "Candidate summary: 3 years combined data science/data engineering/analytics experience; strong Python, SQL, "
        "Spark/PySpark, Hadoop/HDFS/MapReduce, Hive/HiveQL, AWS Glue/S3/Lambda/Kinesis/SageMaker/Redshift/EMR, "
        "Snowflake, Airflow, dbt, Docker, GitHub Actions, Tableau/Power BI; GenAI experience with OpenAI API, RAG, "
        "MCP, LangChain local RAG, FAISS, embeddings, and evaluation harnesses. React is basic exposure only. "
        "Databricks is project-level. Delta Lake is concepts/working knowledge only. Future visa sponsorship is needed.\n\n"
        "Rules:\n"
        "- Return only JSON.\n"
        "- Return exactly one short review object per input id.\n"
        "- Copy each id exactly from the input.\n"
        "- Do not override no-sponsorship, citizenship/clearance, non-US location, or >3 years required blockers.\n"
        "- Do not give positive adjustment when critical skill gaps are real.\n"
        "- score_delta must be an integer from -8 to 8.\n"
        "- Keep reason under 120 characters.\n"
        "- Prefer 0 unless the deterministic score missed a clear evidence-backed signal.\n\n"
        f"Jobs JSON: {json.dumps(compact_jobs, ensure_ascii=True)}\n\n"
        "JSON schema: {\"reviews\":[{"
        "\"id\":\"same id from input\", "
        "\"score_delta\":0, "
        "\"seniority_risk\":\"low|medium|high\", "
        "\"sponsorship_risk\":\"none_detected|blocker|unclear\", "
        "\"reason\":\"short explanation\""
        "}]}"
    )


def _reviews_from_batch_payload(
    payload: dict[str, Any],
    *,
    provider: str,
    model: str,
    key_by_request_id: dict[str, str],
    max_adjustment: int,
) -> dict[str, LLMReview]:
    raw_reviews = payload.get("reviews") if isinstance(payload, dict) else []
    if isinstance(raw_reviews, dict):
        raw_reviews = [dict(value, id=key) for key, value in raw_reviews.items() if isinstance(value, dict)]
    if not isinstance(raw_reviews, list):
        return {}
    reviews: dict[str, LLMReview] = {}
    for item in raw_reviews:
        if not isinstance(item, dict):
            continue
        request_id = str(item.get("id") or "").strip()
        key = key_by_request_id.get(request_id, "")
        if not key:
            continue
        delta = max(-max_adjustment, min(max_adjustment, int(item.get("score_delta") or 0)))
        gaps = item.get("evidence_gaps") or []
        if not isinstance(gaps, list):
            gaps = []
        reviews[key] = LLMReview(
            provider=provider,
            model=model,
            used=True,
            score_delta=delta,
            reason=str(item.get("reason") or "")[:500],
            evidence_gaps=[str(gap)[:80] for gap in gaps][:8],
            seniority_risk=str(item.get("seniority_risk") or "")[:80],
            sponsorship_risk=str(item.get("sponsorship_risk") or "")[:80],
        )
    return reviews


def _extract_gemini_text(payload: dict[str, Any]) -> str:
    candidates = payload.get("candidates") or []
    if not candidates:
        return ""
    content = candidates[0].get("content") or {}
    parts = content.get("parts") or []
    return "\n".join(str(part.get("text") or "") for part in parts if isinstance(part, dict)).strip()


def _extract_chat_text(payload: dict[str, Any]) -> str:
    choices = payload.get("choices") or []
    if not choices:
        return ""
    message = choices[0].get("message") or {}
    content = message.get("content") or ""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                parts.append(str(item.get("text") or item.get("content") or ""))
        return "\n".join(parts).strip()
    return str(content).strip()


def _safe_error_message(exc: Exception) -> str:
    message = str(exc)
    message = re.sub(r"([?&]key=)[^&\s]+", r"\1<redacted>", message)
    message = re.sub(r"(x-goog-api-key[=:]\s*)\S+", r"\1<redacted>", message, flags=re.I)
    message = re.sub(r"(Bearer\s+)[A-Za-z0-9._\-]+", r"\1<redacted>", message)
    return message


def _is_rate_limit_error(exc: Exception) -> bool:
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        return exc.response.status_code == 429
    return "429" in str(exc) and "Too Many Requests" in str(exc)


def _parse_json_object(text: str) -> dict[str, Any]:
    cleaned = (text or "").strip()
    if not cleaned:
        return {}
    try:
        parsed = json.loads(cleaned)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, flags=re.S)
        if not match:
            return {}
        parsed = json.loads(match.group(0))
        return parsed if isinstance(parsed, dict) else {}
