"""Candidate profile template with optional local overrides.

Tracked file stays generic. Personal local data should live in
``src/profile_local.py`` which is gitignored.
"""
from __future__ import annotations

PROFILE = {
    "name": "Likhith Reddy Dosakayala",
    "email": "",  # left blank on purpose: this repo is public
    "location": "Fairfax, VA",
    # Open to relocating anywhere in the US; Fairfax/DC-area and remote are simply listed first.
    "preferred_locations": [
        "Fairfax, VA",
        "Virginia",
        "Washington, DC",
        "Remote",
        "United States",
    ],
    "location_addresses": {"Fairfax, VA": "Fairfax, VA"},
    # Real total is ~2.9 years (35 months). The evaluator hard-blocks any JD whose minimum
    # years requirement is ABOVE this value, so 3 means "3 years or less is OK, 4+ blocked".
    "experience_years": 3,
    "education": "M.S. Data Analytics - George Mason University (May 2026, GPA 3.7)",
    "work_authorization": "F-1 OPT; needs H-1B sponsorship; no security clearance",
    "certifications": [
        "AWS Cloud Practitioner Essentials",
        "Microsoft Certified: Azure Fundamentals",
        "Microsoft Certified: Azure AI Fundamentals",
        "Microsoft Certified: Azure Developer Associate",
        "Google GenAI",
    ],
    "target_roles": [
        "Software Engineer",
        "Software Development Engineer",
        "Backend Engineer",
        "Python Engineer",
        "Python Developer",
        "Data Engineer",
        "Analytics Engineer",
        "Data Analyst",
        "Data Scientist",
        "Machine Learning Engineer",
        "ML Engineer",
        "MLOps Engineer",
        "AI Engineer",
        "Applied AI Engineer",
        "Applied Scientist",
        "Research Engineer",
        "LLM Engineer",
        "GenAI Engineer",
        "Generative AI Engineer",
        "Agent Engineer",
        "AI Solutions Engineer",
        "Conversational AI Engineer",
        "NLP Engineer",
        "Prompt Engineer",
        "AI Developer",
    ],
    "contact_phone": "",
    "linkedin_url": "https://linkedin.com/in/likhith-reddy-d-69a1412a5",
    "github_url": "https://github.com/likithreddy25",
}

# These sets are the skills the evaluator WATCHES FOR in a JD. Whether Likhith actually has
# a skill is decided separately by data/resume/candidate_evidence.md: a watched skill that is
# REQUIRED by the JD but missing from the evidence file becomes a "critical skill gap" and
# caps the score. So the no-evidence languages/domains (Java, C#, C++, Go, Rust, Scala,
# blockchain, ML compilers) are listed here on purpose, and must NOT be added to the
# evidence file.
SKILLS_STRONG: set[str] = {
    # watched gaps (no evidence) — keep so required ones get flagged
    "java", "c++", "c#", "golang", "rust", "scala", "kotlin", "blockchain", "solidity",
    "smart contracts", "cuda", "ml compilers",
    # languages
    "python", "sql", "typescript", "javascript",
    # ML / stats
    "machine learning", "scikit-learn", "sklearn", "xgboost", "random forest", "k-means",
    "clustering", "customer segmentation", "feature engineering", "arima", "sarimax",
    "time series", "forecasting", "learning to rank", "causal inference",
    "propensity score", "a/b testing", "ab testing", "experimentation",
    "statistical analysis", "pytorch", "hugging face", "transformers", "qlora", "lora fine-tuning",
    "fine-tuning", "clip embeddings", "embeddings",
    # GenAI / agents
    "llm", "large language model", "generative ai", "genai", "rag",
    "retrieval-augmented generation", "retrieval augmented generation", "vector database",
    "vector store", "openai api", "openai", "anthropic", "claude", "prompt engineering",
    "agents", "agentic", "tool use", "tool calling", "function calling",
    "llm evaluation", "llm-as-judge", "model routing",
    # backend / full stack
    "fastapi", "django", "django rest framework", "rest api", "restful", "api design",
    "react", "node.js", "express", "microservices", "backend",
    # data engineering / streaming
    "data pipeline", "data pipelines", "etl", "elt", "real-time", "streaming", "kafka",
    "pyspark", "spark", "apache beam", "dataflow", "pub/sub", "pubsub", "airflow", "dbt",
    "data modeling", "data quality", "data governance",
    # databases / warehouses
    "postgresql", "postgres", "mongodb", "mysql", "snowflake", "bigquery", "redshift",
    # cloud
    "aws", "gcp", "google cloud", "kinesis", "firehose", "s3", "glue", "emr",
    # devops / mlops
    "docker", "kubernetes", "terraform", "ci/cd", "github actions",
    "prometheus", "grafana", "observability", "monitoring", "model deployment", "mlops",
    # analytics / BI
    "power bi", "tableau", "dashboards", "streamlit",
}

SKILLS_MODERATE: set[str] = {
    "azure", "linux", "bash", "pandas", "numpy", "nlp", "computer vision",
    "recommendation systems", "data warehouse", "lambda", "sagemaker", "databricks",
    "mlflow", "pytest", "unit testing", "agile", "scrum", "product owner", "excel",
}

try:
    from .profile_local import PROFILE as LOCAL_PROFILE
    from .profile_local import SKILLS_MODERATE as LOCAL_SKILLS_MODERATE
    from .profile_local import SKILLS_STRONG as LOCAL_SKILLS_STRONG
except ImportError:
    LOCAL_PROFILE = None
    LOCAL_SKILLS_STRONG = None
    LOCAL_SKILLS_MODERATE = None

if isinstance(LOCAL_PROFILE, dict):
    PROFILE = LOCAL_PROFILE
if isinstance(LOCAL_SKILLS_STRONG, set):
    SKILLS_STRONG = LOCAL_SKILLS_STRONG
if isinstance(LOCAL_SKILLS_MODERATE, set):
    SKILLS_MODERATE = LOCAL_SKILLS_MODERATE

_STRONG_BONUS = 8
_MODERATE_BONUS = 3


def skill_bonus(text: str, cap: int = 20) -> int:
    """Return a skill-match bonus (0-cap) based on skills found in ``text``."""
    t = (text or "").lower()
    bonus = 0
    for skill in SKILLS_STRONG:
        if skill in t:
            bonus += _STRONG_BONUS
    for skill in SKILLS_MODERATE:
        if skill in t:
            bonus += _MODERATE_BONUS
    return min(bonus, cap)


def profile_summary_html() -> str:
    return (
        f"<p style='font-size:12px;color:#666;margin:0 0 8px'>"
        f"Matched for <strong>{PROFILE['name']}</strong> - "
        f"targeting <em>{', '.join(PROFILE['target_roles'][:4])}...</em></p>"
    )


def profile_summary_text() -> str:
    return (
        f"Profile: {PROFILE['name']} | "
        f"Target: {', '.join(PROFILE['target_roles'][:3])}..."
    )
