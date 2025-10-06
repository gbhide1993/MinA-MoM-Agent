# utils.py
import re
from datetime import datetime, timezone

def normalize_phone_for_db(raw_phone: str) -> str:
    """
    Normalize any phone number into a consistent format:
      'whatsapp:+<country><number>'
    Works with:
      - 919876543210
      - +919876543210
      - whatsapp:+919876543210
      - 09876543210
    """
    if not raw_phone:
        return raw_phone
    p = raw_phone.strip()

    # If already has whatsapp prefix
    if p.startswith("whatsapp:"):
        return p

    # Remove spaces and dashes
    p = p.replace(" ", "").replace("-", "")

    # Extract digits and keep leading +
    if p.startswith("+"):
        digits = p[1:]
    elif p.startswith("00") and p[2:].isdigit():
        digits = p[2:]
    elif p.isdigit():
        digits = p
    else:
        digits = re.sub(r"\D", "", p)

    return f"whatsapp:+{digits}"

def now_utc():
    """Return current UTC datetime."""
    return datetime.now(timezone.utc)
