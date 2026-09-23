import unittest
from types import SimpleNamespace
from unittest.mock import patch

import requests

from src.evaluation import _extract_years_requirement, _jd_sections, _visa_sponsorship_block_reason, evaluate_job
from src.job_intelligence import extract_structured_fields
from src import llm_scorer
from src.llm_scorer import LLMReview, _safe_error_message, maybe_review_job


class EvaluationEvidenceTests(unittest.TestCase):
    def test_critical_skill_without_resume_evidence_caps_score(self) -> None:
        jd = (
            "Data Engineer. Required: 3 years of enterprise Java experience, "
            "ETL pipelines, BigQuery, Power BI, and data modeling."
        )
        evidence = (
            "Built Python and SQL data pipelines with ETL workflows. "
            "Created BigQuery reporting tables and Power BI dashboards."
        )

        with patch("src.evaluation._resume_evidence_text", return_value=evidence):
            result = evaluate_job(
                "Data Engineer",
                jd,
                company="Google",
                location="New York, NY",
                source="google",
                require_us_location=False,
            )

        self.assertIn("java", result.matched_strong)
        self.assertIn("java", result.unsupported_strong)
        self.assertIn("java", result.critical_skill_gaps)
        self.assertLessEqual(result.score, 72)
        self.assertTrue(any("java" in reason.lower() for reason in result.reasons))

    def test_plain_text_jd_without_headings_still_scores_from_general_text(self) -> None:
        jd = (
            "Data Scientist role focused on python, sql, experimentation, product analytics, "
            "and stakeholder reporting for growth teams."
        )
        evidence = (
            "Built python and sql analytics workflows. Designed experimentation readouts and "
            "product analytics dashboards for growth stakeholders."
        )

        with patch("src.evaluation._resume_evidence_text", return_value=evidence):
            result = evaluate_job(
                "Data Scientist",
                jd,
                company="Example",
                location="Remote, United States",
                source="linkedin",
                require_us_location=False,
            )

        self.assertGreaterEqual(result.score, 60)
        self.assertNotIn("python", result.critical_skill_gaps)

    def test_preferred_skill_gap_does_not_become_critical(self) -> None:
        jd = (
            "Required Qualifications\n"
            "- Python\n"
            "- SQL\n\n"
            "Preferred Qualifications\n"
            "- Kubernetes\n"
        )
        evidence = "Built Python and SQL pipelines for analytics reporting."

        with patch("src.evaluation._resume_evidence_text", return_value=evidence):
            result = evaluate_job(
                "Data Engineer",
                jd,
                company="Example",
                location="Austin, TX",
                source="google",
                require_us_location=False,
            )

        self.assertIn("kubernetes", result.unsupported_strong)
        self.assertNotIn("kubernetes", result.critical_skill_gaps)

    def test_responsibility_skill_gap_uses_middle_tier_penalty(self) -> None:
        jd = (
            "What You'll Do\n"
            "- Build Kafka streaming workflows and python services.\n\n"
            "Minimum Qualifications\n"
            "- Python\n"
        )
        evidence = "Built Python services and analytics APIs."

        with patch("src.evaluation._resume_evidence_text", return_value=evidence):
            result = evaluate_job(
                "Data Engineer",
                jd,
                company="Example",
                location="Remote, United States",
                source="google",
                require_us_location=False,
            )

        self.assertNotIn("kafka", result.critical_skill_gaps)
        self.assertTrue(any("core jd responsibilities" in reason.lower() for reason in result.reasons))
        self.assertLessEqual(result.score, 80)

    def test_nonstandard_required_alias_maps_to_required_bucket(self) -> None:
        jd = (
            "Your Expertise\n"
            "- Strong experience with Java and SQL\n"
        )
        evidence = "Built SQL reporting workflows and python data pipelines."

        with patch("src.evaluation._resume_evidence_text", return_value=evidence):
            result = evaluate_job(
                "Data Engineer",
                jd,
                company="Example",
                location="New York, NY",
                source="google",
                require_us_location=False,
            )

        self.assertIn("java", result.critical_skill_gaps)

    def test_inline_required_heading_is_parsed(self) -> None:
        jd = (
            "Required Qualifications: Python, SQL, Java\n"
            "Preferred Qualifications: Tableau\n"
        )

        sections = _jd_sections(jd)

        self.assertIn("python", sections.get("required", "").lower())
        self.assertIn("tableau", sections.get("preferred", "").lower())

    def test_part_time_in_benefits_text_does_not_hard_block_role(self) -> None:
        jd = (
            "What you'll do: Build python and sql data pipelines.\n"
            "Benefits include support for full-time and part-time associates.\n"
            "Preferred Qualifications: Kafka.\n"
        )
        evidence = "Built Python and SQL data pipelines for production analytics systems."

        with patch("src.evaluation._resume_evidence_text", return_value=evidence):
            result = evaluate_job(
                "Data Engineer II",
                jd,
                company="Walmart",
                location="Sunnyvale, CA",
                source="linkedin",
                require_us_location=False,
            )

        self.assertNotEqual(result.score, 0)
        self.assertNotEqual(result.label, "no")
        self.assertFalse(any("hard exclusion" in reason.lower() for reason in result.reasons))

    def test_company_history_years_do_not_override_requirement_years(self) -> None:
        jd = (
            "As a premier global asset management organization with more than 85 years of experience, "
            "we provide investment solutions.\n"
            "Required Qualifications\n"
            "3+ years of total relevant work experience.\n"
            "Proficiency in SQL, Python, Tableau, Power BI, and applied statistics."
        )

        self.assertEqual(_extract_years_requirement(jd), 3)
        structured = extract_structured_fields("Data Analyst, Rapid Insights", jd, location="Colorado Springs, CO")
        self.assertEqual(structured.get("years_experience_min"), 3)

    def test_equal_opportunity_citizenship_text_does_not_hard_block_role(self) -> None:
        jd = (
            "Build scalable evaluations for LLM performance on scientific reasoning.\n"
            "Strong background in LLM training and deployment.\n"
            "Equal employment opportunity regardless of race, color, age, citizenship, or veteran status.\n"
        )
        evidence = (
            "Built LLM evaluation harnesses, RAG systems, and model quality workflows for scientific use cases."
        )

        with patch("src.evaluation._resume_evidence_text", return_value=evidence):
            result = evaluate_job(
                "Machine Learning Scientist, LLM Training & Inference Research",
                jd,
                company="Lila Sciences",
                location="Cambridge, MA",
                source="linkedin",
                require_us_location=False,
            )

        self.assertNotEqual(result.score, 0)
        self.assertFalse(any("citizenship" in reason.lower() and "blocked" in reason.lower() for reason in result.reasons))

    def test_citizenship_requirement_is_scored_not_blocked(self) -> None:
        jd = (
            "Build scalable evaluations for LLM performance.\n"
            "U.S. citizenship is required for this role.\n"
            "Strong background in model evaluation and inference systems.\n"
        )
        evidence = "Built LLM evaluation harnesses and inference quality workflows."

        with patch("src.evaluation._resume_evidence_text", return_value=evidence):
            result = evaluate_job(
                "Machine Learning Scientist",
                jd,
                company="Example",
                location="Cambridge, MA",
                source="linkedin",
                require_us_location=False,
        )

        self.assertNotEqual(result.score, 0)
        risk_dimension = next(d for d in result.dimensions if d.name == "risk")
        self.assertIn("citizenship requirement detected", risk_dimension.reason.lower())

    @patch.dict("src.evaluation.PROFILE", {"needs_visa_sponsorship": True})
    def test_unrestricted_authorization_without_future_sponsorship_blocks_role(self) -> None:
        jd = (
            "Partner with data engineering and AI teams to define agile stories for RAG and LLM workflows.\n"
            "Candidates must possess unrestricted authorization to work in the U.S. for any employer, "
            "without the need for sponsorship now or in the future.\n"
            "Required: Python, SQL, agile delivery, prompt engineering, and AI evaluation criteria.\n"
        )
        evidence = (
            "Built Python and SQL data workflows, RAG systems, prompt engineering workflows, "
            "and AI evaluation harnesses with agile stakeholder delivery."
        )

        with patch("src.evaluation._resume_evidence_text", return_value=evidence):
            result = evaluate_job(
                "IT Business Analyst - Intelligence & AI",
                jd,
                company="Northmarq",
                location="Bloomington, MN",
                source="linkedin",
                require_us_location=False,
            )

        self.assertEqual(result.score, 0)
        self.assertEqual(result.label, "no")
        self.assertEqual(result.grade, "F")
        self.assertTrue(any("does not offer visa sponsorship" in reason.lower() for reason in result.reasons))

    @patch.dict("src.evaluation.PROFILE", {"needs_visa_sponsorship": True})
    def test_us_work_visa_not_available_blocks_role(self) -> None:
        jd = (
            "Role Summary: Build hybrid RAG, agentic workflows, predictive models, and evaluation harnesses.\n"
            "Strong Python engineer fluent in modern AI/ML tools, including model APIs, LangChain, "
            "HuggingFace, PyTorch, cloud deployment, containerization, and CI/CD.\n"
            "Basic Qualifications: PhD with 1+ years of experience OR Master's degree with 5+ years "
            "of software or ML engineering experience OR Bachelor's degree and 6+ years of experience. "
            "U.S. work visa sponsorship (such as TN, O-1, H-1B, etc.) is not available for this role now or in the future. "
            "This position requires permanent work authorization in the United States."
        )
        evidence = "Built Python, RAG, model APIs, PyTorch, LangChain, and evaluation harnesses."

        with patch("src.evaluation._resume_evidence_text", return_value=evidence):
            result = evaluate_job(
                "Translational AI Engineer",
                jd,
                company="Pfizer",
                location="Cambridge, MA",
                source="workday",
                require_us_location=False,
            )

        structured = extract_structured_fields("Translational AI Engineer", jd, location="Cambridge, MA")
        self.assertEqual(result.score, 0)
        self.assertEqual(result.label, "no")
        self.assertEqual(result.grade, "F")
        self.assertIs(structured.get("visa_sponsorship"), False)
        self.assertEqual(structured.get("years_experience_min"), 5)
        self.assertTrue(_visa_sponsorship_block_reason(jd))

    def test_audio_asr_model_compression_role_is_capped_without_domain_evidence(self) -> None:
        jd = (
            "Required Qualifications\n"
            "- Python, PyTorch, and Hugging Face experience.\n"
            "- Practical ASR and speech-to-text model development across multiple languages.\n"
            "- Audio preprocessing with torchaudio or librosa and WER/CER evaluation.\n"
            "- LoRA or QLoRA adaptation plus quantization, distillation, or pruning for on-device iPhone inference.\n"
        )
        evidence = (
            "Built Python and PyTorch model evaluation workflows, RAG systems, and FastAPI model-serving utilities."
        )

        with patch("src.evaluation._resume_evidence_text", return_value=evidence):
            result = evaluate_job(
                "Audio AI Engineer / Multilingual Speech-to-Text Engineer",
                jd,
                company="Dev Technology",
                location="Reston, VA",
                source="linkedin",
                require_us_location=False,
            )

        self.assertLessEqual(result.score, 58)
        self.assertTrue(any("specialized speech/audio" in reason.lower() for reason in result.reasons))

    def test_llm_review_can_adjust_borderline_score(self) -> None:
        jd = (
            "Data Scientist role focused on Python, SQL, product analytics, experimentation, "
            "A/B testing, and stakeholder reporting."
        )
        evidence = "Built Python and SQL product analytics workflows with A/B testing readouts."

        review = LLMReview(
            provider="google",
            model="gemini-2.5-flash-lite",
            used=True,
            score_delta=4,
            reason="The JD has clear product analytics and experimentation alignment.",
        )
        with patch("src.evaluation._resume_evidence_text", return_value=evidence), patch(
            "src.evaluation.maybe_review_job", return_value=review
        ):
            baseline = evaluate_job(
                "Data Scientist",
                jd,
                company="Example",
                location="New York, NY",
                source="linkedin",
                require_us_location=False,
            )

        self.assertEqual(baseline.llm_review["score_delta"], 4)
        self.assertTrue(any("llm reviewer adjusted" in reason.lower() for reason in baseline.reasons))

    def test_llm_review_cannot_boost_critical_skill_gap(self) -> None:
        jd = (
            "Data Engineer. Required: Python, SQL, ETL pipelines, and enterprise Java services."
        )
        evidence = "Built Python, SQL, and ETL data pipelines."
        review = LLMReview(
            provider="google",
            model="gemini-2.5-flash-lite",
            used=True,
            score_delta=8,
            reason="Looks adjacent.",
        )

        with patch("src.evaluation._resume_evidence_text", return_value=evidence), patch(
            "src.evaluation.maybe_review_job", return_value=review
        ):
            result = evaluate_job(
                "Data Engineer",
                jd,
                company="Example",
                location="New York, NY",
                source="linkedin",
                require_us_location=False,
            )

        self.assertIn("java", result.critical_skill_gaps)
        self.assertLessEqual(result.score, 72)

    def test_llm_error_redacts_api_key(self) -> None:
        message = _safe_error_message(
            RuntimeError(
                "404 Client Error for url: https://generativelanguage.googleapis.com/v1beta/"
                "models/gemini-2.5-flash-lite:generateContent?key=secret-token"
            )
        )
        self.assertNotIn("secret-token", message)
        self.assertIn("key=<redacted>", message)

    def test_llm_rate_limit_disables_followup_calls(self) -> None:
        llm_scorer._RATE_LIMITED = False
        llm_scorer._DISABLED_BY_FAILURE = False
        llm_scorer._FAILURES = 0
        llm_scorer._PROCESS_CALLS = 0
        llm_scorer._DAILY_CALLS = 0
        response = requests.Response()
        response.status_code = 429
        error = requests.HTTPError("429 Client Error: Too Many Requests", response=response)
        cfg = SimpleNamespace(
            llm_scoring=SimpleNamespace(
                enabled=True,
                provider="google",
                model="gemini-3.5-flash-lite",
                api_key="test-key",
                endpoint="https://generativelanguage.googleapis.com/v1beta",
                timeout=1,
                only_score_min=55,
                only_score_max=80,
                max_score_adjustment=8,
                max_description_chars=1000,
                max_calls_per_process=100,
                max_daily_calls=300,
            )
        )

        with patch("src.llm_scorer.requests.post", side_effect=error) as post:
            first = maybe_review_job(
                cfg=cfg,
                title="Data Engineer",
                company="Example",
                location="Remote US",
                description="Python SQL data pipelines",
                deterministic_payload={"score": 72, "label": "yes"},
            )
            second = maybe_review_job(
                cfg=cfg,
                title="Data Scientist",
                company="Example",
                location="Remote US",
                description="Python SQL experiments",
                deterministic_payload={"score": 70, "label": "yes"},
            )

        self.assertFalse(first.used)
        self.assertIn("429", first.error)
        self.assertFalse(second.used)
        self.assertIn("rate limit", second.error.lower())
        self.assertEqual(post.call_count, 1)


if __name__ == "__main__":
    unittest.main()
