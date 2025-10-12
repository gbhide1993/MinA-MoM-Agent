# config.py - production-only env helper
import os
from dotenv import load_dotenv

# Load .env locally if present (Render will use service env)
load_dotenv()

def env(key: str, default=None):
    """Production-only: return the environment variable or default."""
    return os.getenv(key, default)
