# config.py
"""
Centralized environment helpers and typed getters.

- Keeps backward-compatible env(key, default) function.
- Adds required() for startup-time checks.
- Adds a few convenience getters used across the project.
- Loads local .env via python-dotenv (useful for dev; production envs should be set in the host).
"""

import os
from dotenv import load_dotenv
from typing import Optional

# Load .env for local development (no-op if not present in production)
load_dotenv()


def env(key: str, default: Optional[str] = None) -> Optional[str]:
    """
    Return environment variable or default.
    Use this for optional values.
    """
    return os.getenv(key, default)


def required(key: str) -> str:
    """
    Return environment variable if present; otherwise raise RuntimeError.
    Use this at app startup for required secrets/config.
    """
    val = os.getenv(key)
    if val is None or val == "":
        raise RuntimeError(f"Required environment variable '{key}' is not set.")
    return val


def as_bool(value: Optional[str], default: bool = False) -> bool:
    if value is None:
        return default
    v = value.strip().lower()
    if v in ("1", "true", "yes", "y", "on"):
        return True
    if v in ("0", "false", "no", "n", "off"):
        return False
    return default


def get_int(value: Optional[str], default: int = 0) -> int:
    try:
        return int(value) if value is not None and value != "" else default
    except ValueError:
        return default


# ----- Common getters (standardize names here) -----

def get_twilio_account_sid() -> Optional[str]:
    return env("TWILIO_ACCOUNT_SID")


def get_twilio_auth_token() -> Optional[str]:
    return env("TWILIO_AUTH_TOKEN")


def get_twilio_whatsapp_from() -> Optional[str]:
    """
    Standardized Twilio WhatsApp 'from' number, e.g. 'whatsapp:+91XXXXXXXXXX'
    Use this everywhere (replaces TWILIO_FROM / TWILIO_WHATSAPP_NUMBER variants).
    """
    return env("TWILIO_WHATSAPP_FROM")


def get_openai_api_key() -> Optional[str]:
    return env("OPENAI_API_KEY")


def get_database_url() -> Optional[str]:
    """
    Return SQLAlchemy-style DATABASE_URL or None.
    e.g. postgresql+psycopg2://user:pass@host:5432/dbname
    """
    return env("DATABASE_URL")


def is_debug_mode() -> bool:
    return as_bool(env("FLASK_DEBUG", env("DEBUG")), default=False)


# ----- Startup checklist helper -----
def startup_validate(min_required: Optional[list] = None):
    """
    Validate a set of required env vars at startup. Raise RuntimeError if missing.
    By default, validate a sensible set (DB, Twilio from, OpenAI key).
    """
    if min_required is None:
        min_required = [
            "DATABASE_URL",
            "TWILIO_ACCOUNT_SID",
            "TWILIO_AUTH_TOKEN",
            "TWILIO_WHATSAPP_FROM",
            "OPENAI_API_KEY",
        ]
    missing = []
    for k in min_required:
        v = os.getenv(k)
        if v is None or v == "":
            missing.append(k)
    if missing:
        raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")


# Example usage inside app startup:
# from config import startup_validate
# startup_validate()   # will raise if essential envs are not set
