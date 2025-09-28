import os
import re
import json
import time
import tempfile
import traceback
import requests
import sqlite3
from datetime import datetime, timedelta
from flask import Flask, request
from dotenv import load_dotenv
from twilio.rest import Client
from requests.exceptions import HTTPError, RequestException

# Load .env
load_dotenv()
app = Flask(__name__)

# Env variables
TWILIO_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_FROM = os.getenv("TWILIO_WHATSAPP_FROM")
OPENAI_KEY = os.getenv("OPENAI_API_KEY")
LANGUAGE = os.getenv("LANGUAGE")  # e.g. "hi" for Hindi

twilio_client = Client(TWILIO_SID, TWILIO_TOKEN)

# Validate essential env vars at import/start time
missing = []
if not TWILIO_SID: missing.append("TWILIO_ACCOUNT_SID")
if not TWILIO_TOKEN: missing.append("TWILIO_AUTH_TOKEN")
if not TWILIO_FROM: missing.append("TWILIO_WHATSAPP_FROM")
if not OPENAI_KEY: missing.append("OPENAI_API_KEY")
if missing:
    debug_print("WARNING: Missing environment variables:", missing)
else:
    debug_print("All required environment variables present. TWILIO_FROM =", TWILIO_FROM)


# --- Helpers ---

DB_PATH = "users.db"

def init_db():
    """Initialize SQLite DB if not exists."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            phone TEXT PRIMARY KEY,
            credits_remaining REAL,
            subscription_active INTEGER,
            subscription_expiry TEXT
        )
    """)
    conn.commit()
    conn.close()

def get_user(phone):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT phone, credits_remaining, subscription_active, subscription_expiry FROM users WHERE phone=?", (phone,))
    row = c.fetchone()
    conn.close()
    if row:
        return {
            "phone": row[0],
            "credits_remaining": row[1],
            "subscription_active": bool(row[2]),
            "subscription_expiry": row[3]
        }
    return None

def save_user(user):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        INSERT OR REPLACE INTO users (phone, credits_remaining, subscription_active, subscription_expiry)
        VALUES (?, ?, ?, ?)
    """, (
        user["phone"],
        user["credits_remaining"],
        int(user["subscription_active"]),
        user["subscription_expiry"]
    ))
    conn.commit()
    conn.close()

def get_or_create_user(phone):
    user = get_user(phone)
    if not user:
        # new user → give 30 free minutes
        user = {
            "phone": phone,
            "credits_remaining": 30.0,
            "subscription_active": False,
            "subscription_expiry": None
        }
        save_user(user)
    return user


def debug_print(*args):
    print(*args)

def download_file(url, ext=".ogg"):
    tmpdir = tempfile.gettempdir()
    filename = os.path.join(tmpdir, f"incoming_{int(time.time())}{ext}")
    debug_print("Downloading media from:", url)
    try:
        r = requests.get(url, stream=True, timeout=30)
        r.raise_for_status()
    except Exception as e:
        debug_print("Download failed:", e)
        r = requests.get(url, auth=(TWILIO_SID, TWILIO_TOKEN), stream=True, timeout=30)
        r.raise_for_status()
    with open(filename, "wb") as f:
        for chunk in r.iter_content(chunk_size=8192):
            if chunk:
                f.write(chunk)
    debug_print("Saved media to:", filename)
    return filename

def transcribe_with_whisper(filepath, model="whisper-1", language=None, max_retries=4):
    """
    Use Whisper for transcription. Retries on 429/5xx errors.
    """
    url = "https://api.openai.com/v1/audio/transcriptions"
    headers = {"Authorization": f"Bearer {OPENAI_KEY}"}

    for attempt in range(1, max_retries + 1):
        try:
            with open(filepath, "rb") as f:
                files = {"file": f}
                data = {"model": model}
                if language:
                    data["language"] = language
                debug_print(f"[Whisper] Attempt {attempt} - POSTing {filepath}")
                resp = requests.post(url, headers=headers, files=files, data=data, timeout=120)
            debug_print(f"[Whisper] Response {resp.status_code}: {resp.text[:200]}")
            resp.raise_for_status()
            return resp.json().get("text", "")
        except HTTPError as http_err:
            status = getattr(http_err.response, "status_code", None)
            if status == 429 or (status and 500 <= status < 600):
                backoff = 2 ** (attempt - 1)
                debug_print(f"[Whisper] Retrying after {backoff}s...")
                time.sleep(backoff)
                continue
            raise
        except RequestException as e:
            debug_print(f"[Whisper] Network error: {e}")
            time.sleep(2 ** (attempt - 1))
            continue
    raise RuntimeError("Whisper transcription failed after retries")

def parse_json_from_text(text):
    """Extract first JSON object from model output."""
    if not text or "{" not in text:
        raise ValueError("No JSON found")
    start = text.find("{")
    depth = 0
    end = -1
    for i, ch in enumerate(text[start:], start=start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i
                break
    if end == -1:
        end = text.rfind("}")
    candidate = text[start:end+1]
    candidate = re.sub(r"^```(?:json)?", "", candidate).strip("` \n")
    try:
        return json.loads(candidate)
    except Exception:
        # minor repair: replace single quotes, remove trailing commas
        repaired = candidate.replace("'", "\"")
        repaired = re.sub(r",\s*([}\]])", r"\1", repaired)
        return json.loads(repaired)

def call_llm_for_minutes_and_bullets(transcript, model="gpt-4o-mini", max_tokens=1500):
    safe_transcript = transcript if len(transcript) <= 12000 else transcript[-12000:]
    system = {
        "role": "system",
        "content": (
            "You are a meeting-minutes assistant. "
            "Return ONLY valid JSON with these keys:\n"
            "summary_bullets, title, datetime, attendees, summary_3_bullets, decisions, action_items, notes, raw_transcript."
        )
    }
    user = {
        "role": "user",
        "content": f"Transcript:\n'''{safe_transcript}'''\n\nGenerate JSON."
    }
    headers = {"Authorization": f"Bearer {OPENAI_KEY}", "Content-Type": "application/json"}
    payload = {"model": model, "messages": [system, user], "temperature": 0, "max_tokens": max_tokens}

    try:
        resp = requests.post("https://api.openai.com/v1/chat/completions", headers=headers, json=payload, timeout=120)
        resp.raise_for_status()
        text = resp.json()["choices"][0]["message"]["content"]
        return parse_json_from_text(text)
    except Exception as e:
        debug_print("Error with gpt-4o-mini:", e)
        # fallback to 3.5
        payload["model"] = "gpt-3.5-turbo"
        resp = requests.post("https://api.openai.com/v1/chat/completions", headers=headers, json=payload, timeout=120)
        resp.raise_for_status()
        text = resp.json()["choices"][0]["message"]["content"]
        return parse_json_from_text(text)

def send_whatsapp(to_whatsapp, body):
    """Send a WhatsApp message via Twilio, with guard for empty recipient and error logging."""
    if not to_whatsapp:
        debug_print("send_whatsapp skipped: empty 'to_whatsapp'. Body:", body)
        return None
    if not TWILIO_FROM:
        debug_print("send_whatsapp skipped: TWILIO_FROM is not set. Body:", body)
        return None
    try:
        msg = twilio_client.messages.create(body=body, from_=TWILIO_FROM, to=to_whatsapp)
        debug_print("Twilio message sent:", getattr(msg, "sid", "<no-sid>"))
        return msg
    except Exception as e:
        # Log the full exception but do not raise (we don't want the webhook to 500 because of a send failure)
        debug_print("Twilio send failed:", repr(e))
        return None


def format_minutes_for_whatsapp(result: dict) -> str:
    """Format the parsed JSON meeting minutes into a readable WhatsApp message."""
    reply_lines = []
    reply_lines.append("📌 *Meeting Summary*")

    # Title / datetime
    if result.get("title"):
        reply_lines.append(f"*Title:* {result['title']}")
    if result.get("datetime"):
        reply_lines.append(f"*Date/Time:* {result['datetime']}")

    # Attendees
    if result.get("attendees"):
        reply_lines.append("*Attendees:* " + ", ".join(result["attendees"]))

    # Summary bullets
    if result.get("summary_bullets"):
        reply_lines.append("\n*Key Points:*")
        for b in result["summary_bullets"][:5]:
            reply_lines.append(f"• {b}")

    # Decisions
    if result.get("decisions"):
        reply_lines.append("\n*Decisions:*")
        for d in result["decisions"]:
            reply_lines.append(f"- {d}")

    # Action items
    if result.get("action_items"):
        reply_lines.append("\n*Action Items:*")
        for item in result["action_items"]:
            who = item.get("who") or "Someone"
            action = item.get("action") or "—"
            due = f" (due {item['due']})" if item.get("due") else ""
            reply_lines.append(f"- {who}: {action}{due}")

    # Notes
    if result.get("notes"):
        reply_lines.append("\n*Notes:*")
        reply_lines.append(result["notes"])

    # Fallback if nothing else
    if len(reply_lines) == 1:  # only header present
        reply_lines.append("_No structured details extracted._")

    return "\n".join(reply_lines)


# --- Flask route ---

@app.route("/twilio-webhook", methods=["POST"])
def twilio_webhook():

    try:
        num_media = int(request.values.get("NumMedia", 0))
        sender = request.values.get("From")
        body_text = (request.values.get("Body") or "").strip()

        # If From is missing, log keys and return success (Twilio expects 200/204)
        if not sender:
            debug_print("Webhook received without 'From'. Keys:", list(request.values.keys()))
            return ("", 204)

        debug_print("Incoming from:", sender, "NumMedia:", num_media, "Body:", body_text)

        if num_media > 0:
            media_url = request.values.get("MediaUrl0")
            media_type = request.values.get("MediaContentType0", "")
            ext = ".ogg" if "ogg" in media_type or "opus" in media_type else ".mp3"
            filename = download_file(media_url, ext)

            transcript = transcribe_with_whisper(filename, language=LANGUAGE)
            debug_print("Transcript:", transcript[:200])

            result = call_llm_for_minutes_and_bullets(transcript)
            formatted_message = format_minutes_for_whatsapp(result)
            send_whatsapp(sender, formatted_message)
        else:
            send_whatsapp(sender, "Hi 👋 — send me a voice note and I'll create structured minutes.")

        return ("", 204)
    except Exception as e:
        traceback.print_exc()
        if 'sender' in locals():
            send_whatsapp(sender, f"Sorry — failed to process audio. Error: {str(e)[:200]}")
        return (str(e), 500)

if __name__ == "__main__":
    init_db()  # make sure users.db exists
    debug_print("Starting Flask app on port 5000")
    app.run(port=5000, debug=True)

