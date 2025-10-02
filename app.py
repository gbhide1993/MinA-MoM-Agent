import os
import re
import json
import time
import tempfile
import traceback
import requests
from datetime import datetime, timedelta
from flask import Flask, request
from twilio.rest import Client
from requests.exceptions import HTTPError, RequestException
from dotenv import load_dotenv
from mutagen import File 
load_dotenv()



from payments import create_payment_link_for_phone, verify_razorpay_webhook, handle_webhook_event


from db import (
    init_db,
    get_or_create_user,
    deduct_minutes,
    get_remaining_minutes,
    set_subscription_active,
    record_payment,
    save_user
)

# Initialize DB
init_db()


# Load .env
app = Flask(__name__)

# Env variables
TWILIO_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_FROM = os.getenv("TWILIO_WHATSAPP_FROM")
OPENAI_KEY = os.getenv("OPENAI_API_KEY")
LANGUAGE = os.getenv("LANGUAGE")  # e.g. "hi" for Hindi

twilio_client = Client(TWILIO_SID, TWILIO_TOKEN)

# Validate essential env vars at import/start time
def debug_print(*args):
    print(*args)
missing = []
if not TWILIO_SID: missing.append("TWILIO_ACCOUNT_SID")
if not TWILIO_TOKEN: missing.append("TWILIO_AUTH_TOKEN")
if not TWILIO_FROM: missing.append("TWILIO_WHATSAPP_FROM")
if not OPENAI_KEY: missing.append("OPENAI_API_KEY")
if missing:
    debug_print("WARNING: Missing environment variables:", missing)
else:
    debug_print("All required environment variables present. TWILIO_FROM =", TWILIO_FROM)



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

def get_audio_duration_seconds(path):
    """
    Return duration in seconds for given audio file path.
    Uses mutagen to extract metadata, works with mp3, m4a, ogg, etc.
    """
    audio = File(path)
    if audio is None or not hasattr(audio, 'info'):
        raise ValueError("Unsupported audio format or corrupted file")
    return audio.info.length

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

            # --- compute audio duration ---
            try:
                duration_seconds = get_audio_duration_seconds(filename)
            except Exception as e:
                debug_print("Failed to compute audio duration:", e)
                send_whatsapp(sender, "Sorry — couldn't determine audio length. Please try a shorter clip.")
                return ("", 204)

            minutes = float(duration_seconds) / 60.0

            # Ensure user record exists and check credits/subscription (Postgres helpers)
            user = get_or_create_user(sender)
            is_premium = bool(user.get("subscription_active"))
            remaining = get_remaining_minutes(sender)

            # Optional: expire subscription if expiry passed (if you store subscription_expiry)
            try:
                exp = user.get("subscription_expiry")
                if exp:
                    from datetime import datetime
                    expiry_dt = datetime.fromisoformat(exp) if isinstance(exp, str) else exp
                    if expiry_dt and datetime.utcnow() > expiry_dt:
                        user["subscription_active"] = False
                        user["subscription_expiry"] = None
                        save_user(user)
                        is_premium = False
                        remaining = get_remaining_minutes(sender)
            except Exception:
                pass

            # If not premium, enforce free minutes
            if not is_premium:
                if remaining <= 0:
                    # create and send a payment link
                    try:
                        pl = create_payment_link_for_phone(sender, amount_in_rupees=499)
                        payment_link = pl.get("short_url")
                    except Exception as e:
                        debug_print("Payment link creation failed:", e)
                        payment_link = os.getenv("FALLBACK_PAYMENT_URL", "https://your-website.example/subscribe")
                    send_whatsapp(sender, f"⚠️ You have used your free 30 minutes. Subscribe for ₹499/month to continue: {payment_link}")
                    return ("", 204)
                if remaining < minutes:
                    try:
                        pl = create_payment_link_for_phone(sender, amount_in_rupees=499)
                        payment_link = pl.get("short_url")
                    except Exception as e:
                        debug_print("Payment link creation failed:", e)
                        payment_link = os.getenv("FALLBACK_PAYMENT_URL", "https://your-website.example/subscribe")
                    send_whatsapp(sender, f"This recording is {minutes:.1f} min but you have only {remaining:.1f} free minutes left. Subscribe: {payment_link}")
                    return ("", 204)

            # Deduct minutes for non-premium
            if not is_premium:
                try:
                    new_remaining = deduct_minutes(sender, minutes)
                    debug_print(f"Deducted {minutes:.2f} minutes from {sender}, remaining {new_remaining:.2f}")
                except Exception as e:
                    debug_print("Failed to deduct minutes:", e)
                    # continue but warn user
                    send_whatsapp(sender, "Warning: couldn't update your usage record. Proceeding to process audio anyway.")

            # Proceed to transcription + LLM
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

# ---------------- Razorpay Webhook ----------------
@app.route("/razorpay-webhook", methods=["POST"])
def razorpay_webhook():
    raw = request.get_data()
    hdr = request.headers.get("X-Razorpay-Signature", "")
    print("DEBUG — header signature:", hdr)
    print("DEBUG — raw body (first 300 bytes):", raw[:300])

    # 🔑 FIX: decode raw bytes to string
    payload_str = raw.decode("utf-8")

    if not verify_razorpay_webhook(payload_str, hdr):
        return ("Signature mismatch", 400)

    try:
        event_json = request.get_json(force=True)
    except Exception as e:
        print("Invalid Razorpay webhook JSON:", e)
        return ("Invalid JSON", 400)

    res = handle_webhook_event(event_json)
    print("Razorpay webhook handled:", res)

    return ("OK", 200)





if __name__ == "__main__":
    debug_print("Starting Flask app on port 5000")
    app.run(port=5000, debug=True)

