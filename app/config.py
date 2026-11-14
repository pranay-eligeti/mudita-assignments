"""Central configuration. Every secret comes from the environment; nothing is committed."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


@dataclass
class Settings:
    # --- app ---
    base_url: str = field(default_factory=lambda: _env("BASE_URL", "http://localhost:8000"))
    database_url: str = field(default_factory=lambda: _env("DATABASE_URL"))  # postgres://... ; empty = use SQLite at DB_PATH
    db_path: str = field(default_factory=lambda: _env("DB_PATH", str(ROOT / "data" / "app.db")))
    secret_key: str = field(default_factory=lambda: _env("SECRET_KEY", "dev-only-change-me"))
    admin_password: str = field(default_factory=lambda: _env("ADMIN_PASSWORD", ""))  # protects A1 dashboard if set

    # --- LLM ---
    llm_provider: str = field(default_factory=lambda: _env("LLM_PROVIDER", "gemini"))  # gemini (free tier) | anthropic
    anthropic_api_key: str = field(default_factory=lambda: _env("ANTHROPIC_API_KEY"))
    gemini_api_key: str = field(default_factory=lambda: _env("GEMINI_API_KEY"))
    # Model id is provider-specific. Gemini default is UNVERIFIED against today's free-tier model list; set MODEL from AI Studio.
    model: str = field(default_factory=lambda: _env("MODEL", "gemini-3.5-flash-lite" if _env("LLM_PROVIDER", "gemini") == "gemini" else "claude-sonnet-5-5"))
    # Comma-separated fallback model ids tried (in order) when the primary stays 429/5xx after its retries.
    model_fallbacks: str = field(default_factory=lambda: _env("MODEL_FALLBACKS"))
    llm_max_attempts: int = field(default_factory=lambda: int(_env("LLM_MAX_ATTEMPTS", "5")))  # incl. first try, for 429/5xx backoff
    llm_timeout_s: float = field(default_factory=lambda: float(_env("LLM_TIMEOUT_S", "60")))
    # USD per million tokens. ASSUMPTION: verify against current Anthropic pricing before quoting cost.
    price_in_per_mtok: float = field(default_factory=lambda: float(_env("PRICE_IN_PER_MTOK", "0" if _env("LLM_PROVIDER", "gemini") == "gemini" else "3")))
    price_out_per_mtok: float = field(default_factory=lambda: float(_env("PRICE_OUT_PER_MTOK", "0" if _env("LLM_PROVIDER", "gemini") == "gemini" else "15")))

    # --- Google (Gmail + Calendar), OAuth web flow ---
    google_client_id: str = field(default_factory=lambda: _env("GOOGLE_CLIENT_ID"))
    google_client_secret: str = field(default_factory=lambda: _env("GOOGLE_CLIENT_SECRET"))
    gmail_poll_seconds: int = field(default_factory=lambda: int(_env("GMAIL_POLL_SECONDS", "30")))
    gmail_polling_enabled: bool = field(default_factory=lambda: _env("GMAIL_POLLING", "1") == "1")
    # Comma-separated sender addresses the assistant may auto-reply to. EMPTY = nobody (safe default): only authorised test senders.
    email_allowed_senders: str = field(default_factory=lambda: _env("EMAIL_ALLOWED_SENDERS"))

    # --- WhatsApp Cloud API ---
    wa_verify_token: str = field(default_factory=lambda: _env("WA_VERIFY_TOKEN"))
    wa_app_secret: str = field(default_factory=lambda: _env("WA_APP_SECRET"))
    wa_access_token: str = field(default_factory=lambda: _env("WA_ACCESS_TOKEN"))
    wa_phone_number_id: str = field(default_factory=lambda: _env("WA_PHONE_NUMBER_ID"))
    wa_graph_version: str = field(default_factory=lambda: _env("WA_GRAPH_VERSION", "v23.0"))

    # --- Instagram API with Instagram Login ---
    ig_verify_token: str = field(default_factory=lambda: _env("IG_VERIFY_TOKEN"))
    ig_app_secret: str = field(default_factory=lambda: _env("IG_APP_SECRET"))
    ig_access_token: str = field(default_factory=lambda: _env("IG_ACCESS_TOKEN"))
    ig_account_id: str = field(default_factory=lambda: _env("IG_ACCOUNT_ID"))
    ig_graph_version: str = field(default_factory=lambda: _env("IG_GRAPH_VERSION", "v25.0"))

    # --- A2 ---
    a2_max_review_rounds: int = field(default_factory=lambda: int(_env("A2_MAX_REVIEW_ROUNDS", "3")))
    a2_enable_fault_injection: bool = field(default_factory=lambda: _env("A2_FAULT_INJECTION", "1") == "1")


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def reset_settings() -> None:
    """Re-read environment (used by tests)."""
    global _settings
    _settings = None
