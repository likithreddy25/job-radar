"""Configuration management: YAML file + environment variable overrides."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

try:
    import yaml
    _HAS_YAML = True
except ImportError:
    _HAS_YAML = False

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(SCRIPT_DIR)


@dataclass
class EmailConfig:
    user: str = ""
    password: str = ""
    to: str = ""
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587


@dataclass
class SlackConfig:
    webhook_url: str = ""


@dataclass
class DiscordConfig:
    webhook_url: str = ""


@dataclass
class DatabaseConfig:
    path: str = "state/gha-jobs.db"


@dataclass
class FilterConfig:
    require_us_location: bool = True


@dataclass
class BoardsConfig:
    csv: str = ""
    batch_size: int = 50
    workers: int = 12
    timeout: int = 30
    rescan_cooldown_hours: int = 1


@dataclass
class SourceConfig:
    enabled: bool = True
    max_jobs: int = 300


@dataclass
class FeaturesConfig:
    scanner_main: bool = True
    scanner_boards: bool = True
    notifications: bool = True
    manual_jd: bool = True
    resume_generation: bool = True


@dataclass
class LLMScoringConfig:
    enabled: bool = False
    provider: str = "google"
    model: str = "gemini-3.5-flash-lite"
    api_key: str = ""
    endpoint: str = "https://generativelanguage.googleapis.com/v1beta"
    timeout: int = 20
    only_score_min: int = 55
    only_score_max: int = 80
    max_score_adjustment: int = 8
    max_description_chars: int = 6000
    batch_size: int = 10
    max_calls_per_process: int = 100
    max_daily_calls: int = 300


@dataclass
class Config:
    email: EmailConfig = field(default_factory=EmailConfig)
    slack: SlackConfig = field(default_factory=SlackConfig)
    discord: DiscordConfig = field(default_factory=DiscordConfig)
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    filter: FilterConfig = field(default_factory=FilterConfig)
    boards: BoardsConfig = field(default_factory=BoardsConfig)
    features: FeaturesConfig = field(default_factory=FeaturesConfig)
    llm_scoring: LLMScoringConfig = field(default_factory=LLMScoringConfig)
    http_timeout: int = 30
    sources: dict[str, SourceConfig] = field(default_factory=dict)

    def source(self, name: str) -> SourceConfig:
        return self.sources.get(name, SourceConfig())

    @classmethod
    def load(cls, path: Optional[str] = None) -> "Config":
        """Load config from YAML file, then apply environment variable overrides."""
        raw: dict = {}

        config_path = path or os.environ.get("CONFIG_PATH", "config.yaml")
        resolved = _resolve_path(config_path)
        if resolved and _HAS_YAML:
            with open(resolved, encoding="utf-8") as f:
                raw = yaml.safe_load(f) or {}
        elif resolved and not _HAS_YAML:
            print(f"[WARN] PyYAML not installed; ignoring config file {resolved}")

        cfg = cls()

        # Email — env vars always win
        e = raw.get("email", {}) or {}
        cfg.email.user = os.environ.get("EMAIL_USER", e.get("user", ""))
        cfg.email.password = os.environ.get("EMAIL_APP_PASSWORD", e.get("password", ""))
        cfg.email.to = os.environ.get("ALERT_TO_EMAIL", e.get("to", ""))
        cfg.email.smtp_host = e.get("smtp_host", "smtp.gmail.com")
        cfg.email.smtp_port = int(e.get("smtp_port", 587))

        # Slack
        sl = raw.get("slack", {}) or {}
        cfg.slack.webhook_url = os.environ.get("SLACK_WEBHOOK_URL", sl.get("webhook_url", ""))

        # Discord
        dc = raw.get("discord", {}) or {}
        cfg.discord.webhook_url = os.environ.get("DISCORD_WEBHOOK_URL", dc.get("webhook_url", ""))

        # Database
        db = raw.get("database", {}) or {}
        cfg.database.path = os.environ.get("DB_PATH", db.get("path", "state/gha-jobs.db"))

        # Filter
        fi = raw.get("filter", {}) or {}
        cfg.filter.require_us_location = _bool(
            os.environ.get("REQUIRE_US_LOCATION", fi.get("require_us_location", True))
        )

        # HTTP timeout
        cfg.http_timeout = _int_env("HTTP_TIMEOUT", raw.get("http_timeout", 30))

        # Boards
        bo = raw.get("boards", {}) or {}
        cfg.boards.csv = os.environ.get("BOARDS_CSV", bo.get("csv", ""))
        cfg.boards.batch_size = _int_env("BOARDS_BATCH_SIZE", bo.get("batch_size", 50))
        cfg.boards.workers = _int_env("BOARDS_WORKERS", bo.get("workers", 12))
        cfg.boards.timeout = _int_env("BOARDS_TIMEOUT", bo.get("timeout", 30))
        cfg.boards.rescan_cooldown_hours = _int_env(
            "BOARDS_RESCAN_COOLDOWN_HOURS", bo.get("rescan_cooldown_hours", 1)
        )

        # Feature toggles
        ft = raw.get("features", {}) or {}
        cfg.features.scanner_main = _bool(os.environ.get("FEATURE_SCANNER_MAIN", ft.get("scanner_main", True)))
        cfg.features.scanner_boards = _bool(os.environ.get("FEATURE_SCANNER_BOARDS", ft.get("scanner_boards", True)))
        cfg.features.notifications = _bool(os.environ.get("FEATURE_NOTIFICATIONS", ft.get("notifications", True)))
        cfg.features.manual_jd = _bool(os.environ.get("FEATURE_MANUAL_JD", ft.get("manual_jd", True)))
        cfg.features.resume_generation = _bool(os.environ.get("FEATURE_RESUME_GENERATION", ft.get("resume_generation", True)))

        # Optional LLM reviewer for second-pass scoring. Disabled by default so
        # scans remain deterministic unless API credentials are explicitly set.
        llm = raw.get("llm_scoring", {}) or {}
        cfg.llm_scoring.enabled = _bool(os.environ.get("LLM_SCORING_ENABLED", llm.get("enabled", False)))
        cfg.llm_scoring.provider = os.environ.get("LLM_SCORING_PROVIDER", llm.get("provider", "google"))
        provider = str(cfg.llm_scoring.provider or "google").strip().lower()
        default_llm_models = {
            "google": "gemini-3.5-flash-lite",
            "groq": "openai/gpt-oss-20b",
            "openrouter": "openrouter/free",
        }
        cfg.llm_scoring.model = os.environ.get(
            "LLM_SCORING_MODEL",
            llm.get("model") or default_llm_models.get(provider, "gemini-3.5-flash-lite"),
        )
        provider_key_env = {
            "google": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
            "groq": ("GROQ_API_KEY",),
            "openrouter": ("OPENROUTER_API_KEY",),
        }
        api_key = os.environ.get("LLM_SCORING_API_KEY", "")
        if not api_key:
            for env_name in provider_key_env.get(provider, ()):
                api_key = os.environ.get(env_name, "")
                if api_key:
                    break
        cfg.llm_scoring.api_key = api_key or llm.get("api_key", "")
        cfg.llm_scoring.endpoint = os.environ.get(
            "LLM_SCORING_ENDPOINT",
            llm.get("endpoint", "https://generativelanguage.googleapis.com/v1beta"),
        )
        cfg.llm_scoring.timeout = _int_env("LLM_SCORING_TIMEOUT", llm.get("timeout", 20))
        cfg.llm_scoring.only_score_min = _int_env("LLM_SCORING_ONLY_SCORE_MIN", llm.get("only_score_min", 55))
        cfg.llm_scoring.only_score_max = _int_env("LLM_SCORING_ONLY_SCORE_MAX", llm.get("only_score_max", 80))
        cfg.llm_scoring.max_score_adjustment = _int_env(
            "LLM_SCORING_MAX_SCORE_ADJUSTMENT", llm.get("max_score_adjustment", 8)
        )
        cfg.llm_scoring.max_description_chars = _int_env(
            "LLM_SCORING_MAX_DESCRIPTION_CHARS", llm.get("max_description_chars", 6000)
        )
        cfg.llm_scoring.batch_size = _int_env("LLM_SCORING_BATCH_SIZE", llm.get("batch_size", 10))
        cfg.llm_scoring.max_calls_per_process = _int_env(
            "LLM_SCORING_MAX_CALLS_PER_PROCESS", llm.get("max_calls_per_process", 100)
        )
        cfg.llm_scoring.max_daily_calls = _int_env("LLM_SCORING_MAX_DAILY_CALLS", llm.get("max_daily_calls", 300))

        # Per-source config
        src_raw = raw.get("sources", {}) or {}
        defaults = {
            "microsoft": 300, "nvidia": 300, "amazon": 300,
            "goldman_sachs": 200, "ibm": 200, "oracle": 200,
            "meta": 200, "google": 200, "apple": 200,
            "netflix": 200, "stripe": 200,
            "linkedin": 100, "google_x": 200, "tiktok_usds": 100,
        }
        for name, default_max in defaults.items():
            s = src_raw.get(name, {}) or {}
            enabled_default = False if name == "tiktok_usds" else True
            cfg.sources[name] = SourceConfig(
                enabled=_bool(s.get("enabled", enabled_default)),
                max_jobs=_int_env(f"MAX_{name.upper()}_JOBS", s.get("max_jobs", default_max)),
            )

        return cfg


def _resolve_path(raw: str) -> Optional[str]:
    if not raw:
        return None
    p = Path(os.path.expanduser(raw))
    if p.is_absolute():
        return str(p) if p.exists() else None
    for base in (Path.cwd(), Path(ROOT_DIR)):
        candidate = base / p
        if candidate.exists():
            return str(candidate)
    return None


def _bool(val) -> bool:
    if isinstance(val, bool):
        return val
    return str(val).lower() in ("1", "true", "yes")


def _int_env(name: str, default) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        raw = default
    return int(raw)
