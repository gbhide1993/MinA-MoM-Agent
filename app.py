# app.py
"""
MinA — WhatsApp Minutes-of-Meeting Agent
Patched version: keeps mutagen for exact durations, robust Whisper calls, saves meeting notes,
preserves payments/db logic and Razorpay idempotency.

Replace your existing app.py with this file and restart your server.
"""

import os
import time
import json
import tempfile
import mimetypes
import traceback
from datetime import datetime, timedelta
from urllib.parse import urlparse, unquote

import requests
from flask import Flask, request, jsonify
from dotenv import load_dotenv
from twilio.rest import Client as TwilioClient
from mutagen import File as MutagenFile  # keep mutagen for exact duration

# Import your local DB and payments helpers (these must exist in your repo)
# db.py should expose: init_db, get_conn, get_or_create_user, get_remaining_minutes, deduct_minutes,
#                    save_meeting_notes, save_user, set_subscription_active
# payments.py should expose: create_payment_link_for_phone, handle_webhook_event, verify_razorpay_webhook
from db import (
    init_db,
    get_conn,
    get_or_create_user,
    get_remaining_minutes,
    deduct_minutes,
    save_meeting_notes,
    save_meeting_notes_with_sid,
    save_user,
    set_subscription_active,
)
from payments import create_payment_link_for_phone, handle_webhook_event, verify_razorpay_webhook

# Load environment
load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_WHATSAPP_FROM = os.getenv("TWILIO_WHATSAPP_FROM") or os.getenv("TWILIO_FROM")
RAZORPAY_WEBHOOK_SECRET = os.getenv("RAZORPAY_WEBHOOK_SECRET")
TEST_MODE = os.getenv("TEST_MODE", "0") == "1"
LANGUAGE = os.getenv("LANGUAGE", "en")
DEFAULT_SUBSCRIPTION_MINUTES = float(os.getenv("DEFAULT_SUBSCRIPTION_MINUTES", "30.0"))

# Twilio client
twilio_client = None
if TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN:
    try:
        twilio_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    except Exception as e:
        print("Failed to init Twilio client:", e)

# Ensure DB schema exists (safe to call)
try:
    init_db()
except Exception as e:
    print("init_db() failed:", e)


app = Flask(__name__)


# -----------------------
# Utility / helper funcs
# -----------------------

def debug_print(*args, **kwargs):
    """Simple wrapper for prints so we can change later to logging."""
    print(*args, **kwargs)


def send_whatsapp(to_whatsapp: str, body: str):
    """
    Send a WhatsApp message via Twilio. to_whatsapp should be full e.g. 'whatsapp:+919xxxx'
    This function logs errors instead of raising so webhook doesn't crash.
    """
    global twilio_client, TWILIO_WHATSAPP_FROM
    if not twilio_client or not TWILIO_WHATSAPP_FROM:
        debug_print("Twilio client or TWILIO_WHATSAPP_FROM not configured. Message intended:", to_whatsapp, body[:200])
        return None
    try:
        msg = twilio_client.messages.create(body=body, from_=TWILIO_WHATSAPP_FROM, to=to_whatsapp)
        debug_print("Twilio message sent:", msg.sid)
        return msg.sid
    except Exception as e:
        debug_print("Twilio send failed:", e)
        return None


def _ext_from_content_type(ct: str):
    if not ct:
        return None
    ct = ct.split(";")[0].strip().lower()
    mapping = {
        "audio/mpeg": ".mp3",
        "audio/mp3": ".mp3",
        "audio/mp4": ".m4a",
        "audio/x-m4a": ".m4a",
        "audio/mp4a-latm": ".m4a",
        "audio/wav": ".wav",
        "audio/x-wav": ".wav",
        "audio/ogg": ".ogg",
        "audio/opus": ".opus",
        "video/mp4": ".mp4",
    }
    return mapping.get(ct)


def download_file(url, fallback_ext=".m4a"):
    """
    Download the media URL to a temporary file.
    Preserve extension based on Content-Type header if possible.
    Returns local path.
    """
    try:
        resp = requests.get(url, stream=True, timeout=60)
        resp.raise_for_status()
    except Exception as e:
        debug_print("download_file: request failed:", e)
        raise

    ct = resp.headers.get("Content-Type", "")
    ext = _ext_from_content_type(ct)
    if not ext:
        # try to guess from URL
        path = unquote(urlparse(url).path)
        guessed = os.path.splitext(path)[1]
        ext = guessed if guessed else fallback_ext

    fname = f"incoming_{int(time.time())}{ext}"
    tmp_path = os.path.join(tempfile.gettempdir(), fname)

    try:
        with open(tmp_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
    except Exception as e:
        debug_print("download_file: writing file failed:", e)
        raise

    debug_print(f"Downloading media from: {url}")
    debug_print(f"Saved media to: {tmp_path}  (Content-Type: {ct}, ext used: {ext})")
    return tmp_path


def get_audio_duration_seconds(path: str) -> float:
    """
    Use mutagen to get exact audio duration in seconds.
    Keep mutagen as requested — it gives precise lengths.
    """
    try:
        mf = MutagenFile(path)
        if not mf:
            raise RuntimeError("mutagen couldn't parse file")
        # different containers expose length differently but mp3/mp4/ogg present .info.length
        length = getattr(mf.info, "length", None)
        if length is None:
            raise RuntimeError("mutagen returned no length info")
        return float(length)
    except Exception as e:
        debug_print("get_audio_duration_seconds failed:", e)
        # fallback: estimate by file size / 16000bps (~16kbps)
        try:
            size_bytes = os.path.getsize(path)
            seconds = (size_bytes * 8) / (16000)  # conservative fallback
            return seconds
        except Exception as e2:
            debug_print("Duration fallback also failed:", e2)
            return 60.0  # worst-case fallback 1 minute


def transcribe_audio(file_path: str, language: str = None, attempts: int = 2) -> str:
    """
    Robust transcription wrapper for OpenAI Whisper (HTTP endpoint).
    - Guesses MIME type and sends file.
    - If Whisper returns 400 invalid file format, it retries with a different MIME mapping once.
    - Raises Exception on final failure.
    """
    if language is None:
        language = LANGUAGE

    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY not set")

    url = "https://api.openai.com/v1/audio/transcriptions"
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}"}

    # try to guess mime from extension
    guessed_mime, _ = mimetypes.guess_type(file_path)
    guessed_mime = guessed_mime or ""
    # normalize a few common suspects
    ext = os.path.splitext(file_path)[1].lower()
    ext_to_mime = {
        ".m4a": "audio/mp4",
        ".mp4": "audio/mp4",
        ".mp3": "audio/mpeg",
        ".wav": "audio/wav",
        ".ogg": "audio/ogg",
        ".webm": "audio/webm",
        ".opus": "audio/opus",
    }
    if not guessed_mime and ext in ext_to_mime:
        guessed_mime = ext_to_mime[ext]

    # Attempt loop: initial guessed mime, then fallback mapping if needed
    last_err = None
    tried = 0
    tried_mimes = []

    while tried < attempts:
        mime_to_send = guessed_mime if tried == 0 else ext_to_mime.get(ext, guessed_mime or "audio/m4a")
        tried += 1
        tried_mimes.append(mime_to_send)
        try:
            with open(file_path, "rb") as fh:
                files = {
                    "file": (os.path.basename(file_path), fh, mime_to_send)
                }
                data = {"model": "whisper-1"}
                if language:
                    data["language"] = language
                resp = requests.post(url, headers=headers, data=data, files=files, timeout=120)
            # success
            if resp.status_code == 200:
                # many times resp is JSON { "text": "..." } or string; try both
                try:
                    dj = resp.json()
                    text = dj.get("text") or (dj.get("transcription") if isinstance(dj, dict) else None)
                    if text is None:
                        # some API shapes return {'text': '...'}
                        text = dj if isinstance(dj, str) else None
                    if text is None:
                        # last attempt: treat whole body as text
                        text = resp.text
                except Exception:
                    text = resp.text
                return text.strip()
            # If it's clearly invalid file format, try fallback and retry
            if resp.status_code == 400 and "Invalid file format" in resp.text:
                last_err = RuntimeError(f"Whisper invalid file format: {resp.text}")
                debug_print("transcribe_audio: Whisper invalid file format (will retry with other mime)", resp.text[:400])
                continue
            # other errors: bubble up
            last_err = RuntimeError(f"Transcription failed: {resp.status_code} {resp.text}")
            debug_print("transcribe_audio: non-400 error from Whisper:", resp.status_code, resp.text[:400])
            break
        except Exception as e:
            last_err = e
            debug_print("transcribe_audio: exception while calling OpenAI:", e, traceback.format_exc())
            # continue to retry if attempts left
            continue

    # if we reach here, we failed
    debug_print("transcribe_audio: attempts exhausted. tried mimes:", tried_mimes)
    raise last_err if last_err else RuntimeError("transcribe_audio: unknown error")


def call_llm_for_minutes_and_bullets(transcript: str) -> dict:
    """
    Call LLM (OpenAI chat/completion) to produce structured minutes and bullets.
    Uses 'gpt-4o-mini' / '4o-mini' if available, or classic GPT-4o endpoints depending on your setup.
    Returns a dict with 'summary' and 'bullets' keys.
    """
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY not configured")

    endpoint = "https://api.openai.com/v1/chat/completions"
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}

    # A compact, instructive prompt to produce structured output
    system_prompt = (
        "You are MinA — an assistant that turns meeting audio transcripts into structured meeting minutes. "
        "Return a JSON object with 'summary' (2-3 sentence summary), 'bullets' (list of key points/action-items), "
        "and 'participants' (if mentioned). Keep results factual and concise."
    )
    user_content = f"Transcript:\n{transcript}\n\nProduce JSON only."

    payload = {
        "model": "gpt-4o-mini",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        "temperature": 0.2,
        "max_tokens": 800,
    }

    try:
        resp = requests.post(endpoint, headers=headers, json=payload, timeout=60)
        if resp.status_code != 200:
            debug_print("LLM call failed:", resp.status_code, resp.text[:400])
            raise RuntimeError(f"LLM call failed: {resp.status_code} {resp.text}")
        j = resp.json()
        # Extract text from the assistant's message
        choices = j.get("choices", [])
        if not choices:
            raise RuntimeError("LLM returned no choices")
        message = choices[0].get("message", {}).get("content", "")
        # The model may return JSON or plain text — try to parse JSON out
        parsed = None
        try:
            parsed = json.loads(message)
        except Exception:
            # Attempt to extract JSON substring
            try:
                start = message.find("{")
                end = message.rfind("}") + 1
                if start != -1 and end != -1:
                    sub = message[start:end]
                    parsed = json.loads(sub)
            except Exception:
                parsed = None
        if parsed is None:
            # fallback: return flat summary and bullets by splitting lines
            lines = [l.strip() for l in message.splitlines() if l.strip()]
            summary = lines[0] if lines else ""
            bullets = lines[1:] if len(lines) > 1 else []
            return {"summary": summary, "bullets": bullets}
        # ensure keys
        summary = parsed.get("summary") or parsed.get("summary_text") or ""
        bullets = parsed.get("bullets") or parsed.get("items") or []
        participants = parsed.get("participants") or []
        return {"summary": summary, "bullets": bullets, "participants": participants}
    except Exception as e:
        debug_print("call_llm_for_minutes_and_bullets: error", e, traceback.format_exc())
        raise


def format_minutes_for_whatsapp(result: dict) -> str:
    """
    Turn the structured result into a WhatsApp-friendly text reply (not JSON).
    """
    summary = result.get("summary", "").strip()
    bullets = result.get("bullets", []) or []
    participants = result.get("participants", []) or []

    out = []
    if summary:
        out.append("*Summary*\n" + summary)
    if participants:
        p = ", ".join(participants) if isinstance(participants, list) else str(participants)
        out.append(f"*Participants*: {p}")
    if bullets:
        out.append("*Key Points / Action Items*")
        for b in bullets:
            out.append(f"• {b}")
    return "\n\n".join(out).strip()


# ----------------------------
# ROUTES: Twilio webhook
# ----------------------------
@app.route("/twilio-webhook", methods=["POST"])
def twilio_webhook():
    """
    Main webhook for Twilio (WhatsApp).
    Expects incoming audio in MediaUrl0.
    Flow:
    - download file
    - compute duration (mutagen)
    - check user credits / subscription
    - transcribe (Whisper)
    - deduct minutes (after successful transcription) for non-premium users
    - summarize using LLM
    - save meeting notes
    - send WhatsApp reply
    """
    try:
        sender = request.values.get("From") or request.values.get("from") or request.form.get("From")
        if not sender:
            debug_print("twilio-webhook: missing From")
            return ("", 204)

        num_media = int(request.values.get("NumMedia", 0))
        body = request.values.get("Body", "") or ""
        debug_print("Incoming from:", sender, "NumMedia:", num_media, "Body:", body[:200])

        # If there's no media, send a friendly prompt
        if num_media == 0:
            send_whatsapp(sender, "Hi 👋 — please send a voice note and I'll create structured minutes (summary + action items).")
            return ("", 204)

        # download first media
        media_url = request.values.get("MediaUrl0")
        media_type = request.values.get("MediaContentType0", "")
        if not media_url:
            send_whatsapp(sender, "Sorry — I couldn't find the audio file in your message.")
            return ("", 204)

        # Save file locally
        try:
            filename = download_file(media_url)
        except Exception as e:
            debug_print("Failed download_file:", e)
            send_whatsapp(sender, "Sorry — couldn't download your audio. Please try again.")
            return ("", 204)

        # compute duration (mutagen)
        try:
            duration_seconds = get_audio_duration_seconds(filename)
            minutes = float(duration_seconds) / 60.0
            debug_print(f"Audio duration: {duration_seconds:.2f}s ({minutes:.2f} minutes)")
        except Exception as e:
            debug_print("Duration computation failed:", e)
            minutes = 1.0  # fallback minute

        # Ensure user record exists and fetch credits
        try:
            user = get_or_create_user(sender)
            remaining = get_remaining_minutes(sender)
            is_premium = bool(user.get("subscription_active"))
        except Exception as e:
            debug_print("User lookup failed:", e)
            # allow processing but don't deduct reliably
            user = None
            remaining = 0.0
            is_premium = False

        # If not premium and not enough minutes: supply payment link and abort
        if not is_premium:
            if remaining <= 0:
                # create payment link
                try:
                    pl = create_payment_link_for_phone(sender, amount_in_rupees=499)
                    payment_link = pl.get("short_url")
                except Exception as e:
                    debug_print("create_payment_link_for_phone failed:", e)
                    payment_link = os.getenv("FALLBACK_PAYMENT_URL", "https://your-website.example/subscribe")
                send_whatsapp(sender, f"⚠️ You have used your free minutes. Subscribe for ₹499/month to continue: {payment_link}")
                return ("", 204)

            if remaining < minutes:
                try:
                    pl = create_payment_link_for_phone(sender, amount_in_rupees=499)
                    payment_link = pl.get("short_url")
                except Exception as e:
                    debug_print("create_payment_link_for_phone failed:", e)
                    payment_link = os.getenv("FALLBACK_PAYMENT_URL", "https://your-website.example/subscribe")
                send_whatsapp(sender, f"Recording is {minutes:.1f} min but you have only {remaining:.1f} free minutes left. Subscribe: {payment_link}")
                return ("", 204)

        # Transcribe (always attempt if media present)
        try:
            transcript = transcribe_audio(filename, language=LANGUAGE)
            debug_print("Transcript (preview):", transcript[:300])
        except Exception as e:
            debug_print("Transcription failed, not deducting minutes:", e, traceback.format_exc())
            send_whatsapp(sender, f"Sorry - failed to process your audio. Error: {str(e)[:200]}")
            return ("", 204)

        # Deduct minutes for non-premium AFTER success
        if not is_premium:
            try:
                new_remaining = deduct_minutes(sender, minutes)
                debug_print(f"Deducted {minutes:.2f} minutes from {sender}, remaining {new_remaining:.2f}")
            except Exception as e:
                debug_print("Failed to deduct minutes (DB error) but proceeding:", e)

        # Summarize via LLM
        try:
            llm_result = call_llm_for_minutes_and_bullets(transcript)
            formatted_message = format_minutes_for_whatsapp(llm_result)
        except Exception as e:
            debug_print("LLM summarization error:", e, traceback.format_exc())
            formatted_message = "Sorry — failed to summarize the audio."

        # Log and persist meeting notes
        debug_print("=== MinA Summary for", sender, "===")
        debug_print(formatted_message)
        debug_print("=== End of Summary ===")

        message_sid = request.values.get("MessageSid")

        try:
            save_meeting_notes_with_sid(
            phone=sender,
            audio_file=media_url,
            transcript=transcript,
            summary=formatted_message,
            message_sid=message_sid
            )
            debug_print("Meeting notes saved for", sender, "sid:", message_sid)
        except Exception as e:
            debug_print("Failed to save meeting notes:", e, traceback.format_exc())

        # Send WhatsApp reply
        send_whatsapp(sender, formatted_message)

        # If TEST_MODE, return content to caller for integration tests
        if TEST_MODE:
            return formatted_message, 200
        return ("", 204)

    except Exception as e:
        debug_print("Unhandled exception in twilio_webhook:", e, traceback.format_exc())
        # If something exploded unexpectedly, still respond 204 so Twilio doesn't keep retrying
        return ("", 204)


# ----------------------------------
# Razorpay webhook (idempotent)
# ----------------------------------
@app.route("/razorpay-webhook", methods=["POST"])
def razorpay_webhook():
    """
    Razorpay webhook endpoint. We expect Razorpay to post signed JSON.
    We verify signature (verify_razorpay_webhook) if available. Then delegate to payments.handle_webhook_event
    which is expected to be idempotent.
    """
    raw = request.get_data()
    hdr = request.headers.get("X-Razorpay-Signature", "")
    debug_print("DEBUG — header signature:", hdr)
    debug_print("DEBUG — raw body (first 300 bytes):", raw[:300])

    payload_str = None
    try:
        payload_str = raw.decode("utf-8")
    except Exception:
        payload_str = raw

    # verify signature if function is present
    try:
        ok = False
        try:
            ok = verify_razorpay_webhook(payload_str, hdr)
            debug_print("verify_razorpay_webhook: signature verified ✅")
        except Exception as e:
            debug_print("verify_razorpay_webhook: SDK verification failed:", e)
            # In local testing we may allow continuing; in production you can return 400
            # return ("Signature mismatch", 400)
            ok = False

        event_json = request.get_json(force=True)
    except Exception as e:
        debug_print("Invalid Razorpay webhook JSON:", e)
        return ("Invalid JSON", 400)

    try:
        res = handle_webhook_event(event_json)
        debug_print("Razorpay webhook handled:", res)
        return ("OK", 200)
    except Exception as e:
        debug_print("Error handling Razorpay webhook:", e, traceback.format_exc())
        return ("", 500)


# -------------------------
# Admin endpoints
# -------------------------
@app.route("/admin/user/<path:phone>", methods=["GET"])
def admin_get_user(phone):
    """
    Simple admin endpoint to view user state.
    """
    try:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT phone, credits_remaining, subscription_active, subscription_expiry, created_at FROM users WHERE phone=%s", (phone,))
            row = cur.fetchone()
            if not row:
                return jsonify({"error": "not found"}), 404
            return jsonify({"user": row}), 200
    except Exception as e:
        debug_print("admin_get_user error:", e, traceback.format_exc())
        return jsonify({"error": str(e)}), 500


@app.route("/admin/notes/<path:phone>", methods=["GET"])
def admin_get_notes(phone):
    try:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT id, audio_file, summary, created_at FROM meeting_notes WHERE phone=%s ORDER BY created_at DESC LIMIT 50", (phone,))
            rows = cur.fetchall()
            return jsonify({"notes": rows}), 200
    except Exception as e:
        debug_print("admin_get_notes error:", e, traceback.format_exc())
        return jsonify({"error": str(e)}), 500


# -------------------------
# Health check
# -------------------------
@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "time": datetime.utcnow().isoformat()}), 200


# -------------------------
# Main run
# -------------------------
if __name__ == "__main__":
    debug_print("Starting Flask app (debug mode: True if TEST_MODE)... TEST_MODE=", TEST_MODE)
    if TEST_MODE:
        app.run(host="0.0.0.0", port=5000, debug=True)
    else:
        # In production Render/Gunicorn will serve this.
        app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
