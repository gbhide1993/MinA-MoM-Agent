# config.py
import os
from dotenv import load_dotenv

load_dotenv()

ENV = os.getenv("ENV", "production").lower()
TEST_MODE = os.getenv("TEST_MODE", "0") in ("1", "true", "True") or ENV in ("sandbox", "beta", "test")

def env_for(key: str, default=None):
    """
    Return environment variable for key, but when TEST_MODE is True prefer
    KEY_SANDBOX or KEY_TEST before falling back to KEY.
    Example: env_for("DATABASE_URL") => DATABASE_URL_SANDBOX when TEST_MODE.
    """
    if TEST_MODE:
        for suffix in ("_SANDBOX", "_TEST"):
            k = f"{key}{suffix}"
            v = os.getenv(k)
            if v:
                return v
    return os.getenv(key, default)
