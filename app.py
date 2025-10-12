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
import openai 
from datetime import datetime, timedelta
from urllib.parse import urlparse, unquote
import hashlib
import requests
from flask import Flask, request, jsonify
from dotenv import load_dotenv
from twilio.rest import Client as TwilioClient
from mutagen import File as MutagenFile  # keep mutagen for exact duration

# Import your local DB and payments helpers (these must exist in your repo)
# db.py should expose: init_db, get_conn, get_or_create_user, get_remaining_minutes, deduct_minutes,
#                    save_meeting_notes, save_user, set_subscription_active
# payments.py should expose: create_payment_link_for_phone, handle_webhook_event, verify_razorpay_webhook

from db import (init_db, get_conn, get_or_create_user, get_remaining_minutes, deduct_minutes, save_meeting_notes, save_meeting_notes_with_sid, save_user, decrement_minutes_if_available, set_subscription_active)
import re

from payments import create_payment_link_for_phone, handle_webhook_event, verify_razorpay_webhook

# Load environment
load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_WHATSAPP_FROM = os.getenv("TWILIO_WHATSAPP_FROM") or os.getenv("TWILIO_FROM")
RAZORPAY_KEY_ID = os.getenv("RAZORPAY_KEY_ID")
RAZORPAY_KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET")
RAZORPAY_WEBHOOK_SECRET = os.getenv("RAZORPAY_WEBHOOK_SECRET")
LANGUAGE = os.getenv("LANGUAGE", "en")
DATABASE_URL = os.getenv("DATABASE_URL")
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


# ======================================================
# 🧩 HELPER FUNCTIONS — Audio + Text Summarization
# ======================================================

def normalize_phone_for_db(phone):
    """Ensure consistent phone format for DB keys."""
    if not phone:
        return None
    # Twilio sends whatsapp:+91XXXXXXXXXX — keep consistent
    return phone.strip().lower().replace(" ", "")




def download_media_to_local(url, fallback_ext=".m4a"):
    """Download Twilio media (with Basic Auth if needed) to temp file and return local path."""
    if not url:
        debug_print("download_media_to_local: no url")
        return None
    try:
        auth = None
        parsed = urlparse(url)
        # If Twilio domain, use Basic Auth
        if "twilio.com" in parsed.netloc and TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN:
            auth = (TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        resp = requests.get(url, stream=True, timeout=60, auth=auth)
        resp.raise_for_status()
    except Exception as e:
        debug_print("download_media_to_local: request failed:", e)
        return None

    # decide extension
    ct = resp.headers.get("Content-Type", "")
    ext = _ext_from_content_type(ct) or os.path.splitext(unquote(parsed.path))[1] or fallback_ext
    tmp_path = tempfile.mktemp(suffix=ext)
    try:
        with open(tmp_path, "wb") as f:
            for chunk in resp.iter_content(8192):
                if chunk:
                    f.write(chunk)
        debug_print(f"Saved media to {tmp_path} (Content-Type: {ct})")
        return tmp_path
    except Exception as e:
        debug_print("download_media_to_local: write failed:", e)
        return None



def compute_audio_duration_seconds(file_path):
    """Compute audio duration safely using Mutagen."""
    try:
        audio = MutagenFile(file_path)
        if not audio or not getattr(audio.info, 'length', None):
            return 0.0
        return round(audio.info.length, 2)
    except Exception as e:
        print("⚠️ Could not compute duration:", e)
        return 0.0


def transcribe_audio_local_file(file_path):
    """Transcribe the given local audio file using OpenAI Whisper."""
    try:
        openai.api_key = os.getenv("OPENAI_API_KEY")
        with open(file_path, "rb") as audio_file:
            response = openai.audio.transcriptions.create(
                model="whisper-1",
                file=audio_file
            )
        transcript = response.text.strip()
        print("✅ Transcription success (first 100 chars):", transcript[:100])
        return transcript
    except Exception as e:
        print("❌ Transcription failed:", e)
        raise RuntimeError(f"Transcription failed: {e}")


def call_llm_summarize(text):
    """Summarize and structure the meeting into readable bullet points."""
    openai.api_key = os.getenv("OPENAI_API_KEY")
    prompt = f"""
    You are MinA, an AI meeting summarizer. 
    Summarize the following transcript in 5-8 crisp, structured bullet points with clarity and brevity.
    Focus on:
    - Key discussion points
    - Decisions made
    - Action items
    - Deadlines or follow-ups if any
    
    Transcript:
    {text}
    """
    try:
        response = openai.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.4,
        )
        summary = response.choices[0].message.content.strip()
        print("✅ LLM Summary generated (first 100 chars):", summary[:100])
        return summary
    except Exception as e:
        print("❌ LLM summarization failed:", e)
        raise RuntimeError(f"LLM summarization failed: {e}")


def format_summary_for_whatsapp(summary_text):
    """Make the summary WhatsApp-friendly (bold, emoji, bullet formatting)."""
    formatted = re.sub(r"^- ", "• ", summary_text, flags=re.MULTILINE)
    header = "📝 *Meeting Summary:*\n\n"
    return header + formatted.strip()


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
        sender_raw = request.values.get("From") or request.form.get("From")
        sender = normalize_phone_for_db(sender_raw)
        message_sid = request.values.get("MessageSid") or request.form.get("MessageSid")
        media_url = request.values.get("MediaUrl0") or request.form.get("MediaUrl0")
        # compute media_hash fallback if no MessageSid
        media_hash = None
        if not message_sid and media_url:
            media_hash = hashlib.sha256(media_url.encode("utf-8")).hexdigest()
        dedupe_key = message_sid or media_hash

        # Check dedupe before doing heavy work
        if dedupe_key:
            # quick SQL check
            with get_conn() as conn, conn.cursor() as cur:
                cur.execute("SELECT 1 FROM meeting_notes WHERE message_sid=%s LIMIT 1", (dedupe_key,))
                if cur.fetchone():
                    print("Duplicate message detected (dedupe_key). Skipping processing.")
                    return ("", 204)
        
        # If no media present, respond politely to text-only users and stop
        body_text = (request.values.get("Body") or request.form.get("Body") or "").strip()
        if not media_url:
            # Use the normalized sender we already computed
            try:
                if body_text:
                    send_whatsapp(sender, (
                        "Hi 👋 — I can generate meeting minutes from a short *voice note* (audio). "
                        "Please send a voice message and I will transcribe and summarize it for you. 🎙️"
                    ))
                else:
                    send_whatsapp(sender, (
                        "Hi 👋 — please send a short *voice note* (audio) and I will create meeting minutes for you."
                    ))
            except Exception as e:
                debug_print("Failed to send guidance reply:", e)
            return ("", 204)


        # download media to local file (your existing function)
        local_path = download_media_to_local(media_url)  # your existing helper

        # compute duration using mutagen or your helper
        duration_seconds = compute_audio_duration_seconds(local_path)  # existing helper
        minutes = round(duration_seconds / 60.0, 2)

        # Now perform an atomic reservation: lock user row, check credits/subscription, deduct and insert meeting row
        with get_conn() as conn, conn.cursor() as cur:
            # normalize sender again to be safe
            phone = sender
            # lock user row to avoid race conditions
            cur.execute("SELECT credits_remaining, subscription_active, subscription_expiry FROM users WHERE phone=%s FOR UPDATE", (phone,))
            row = cur.fetchone()
            if row:
                credits_remaining = float(row[0]) if row[0] is not None else 0.0
                sub_active = bool(row[1])
                sub_expiry = row[2]
            else:
                # create user if missing
                cur.execute("INSERT INTO users (phone, credits_remaining, subscription_active, created_at) VALUES (%s,%s,%s,now()) RETURNING credits_remaining, subscription_active, subscription_expiry", (phone, 30.0, False))
                newr = cur.fetchone()
                credits_remaining = float(newr[0])
                sub_active = bool(newr[1])
                sub_expiry = newr[2]

            # If subscription active and not expired → do not deduct
            from datetime import datetime, timezone
            now = datetime.now(timezone.utc)
            if sub_active and (sub_expiry is None or sub_expiry > now):
                to_deduct = 0.0
            else:
                to_deduct = minutes

            if to_deduct > 0 and credits_remaining < to_deduct:
                # Not enough credits
                conn.rollback()
                send_whatsapp(phone, "⚠️ You have insufficient free minutes. Please subscribe to continue: <link>")
                return ("", 204)

            # Deduct credits if needed
            if to_deduct > 0:
                new_credits = credits_remaining - to_deduct
                cur.execute("UPDATE users SET credits_remaining=%s WHERE phone=%s", (new_credits, phone))
            # Insert meeting row with message_sid = dedupe_key
            cur.execute("""
                INSERT INTO meeting_notes (phone, audio_file, transcript, summary, message_sid, created_at)
                VALUES (%s, %s, %s, %s, %s, now())
                RETURNING id
            """, (phone, media_url, None, None, dedupe_key))
            meeting_id = cur.fetchone()[0]
            conn.commit()

        # Now transcribe & summarize outside transaction (or you can transcribe before the transaction and include in insert)
        transcript = transcribe_audio_local_file(local_path)  # your function
        summary_text = call_llm_summarize(transcript)

        # Update the meeting_notes row with transcript and summary
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("UPDATE meeting_notes SET transcript=%s, summary=%s WHERE id=%s", (transcript, summary_text, meeting_id))
            conn.commit()

        # Reply to user
        send_whatsapp(phone, format_summary_for_whatsapp(summary_text))

        return ("", 204)

    except Exception as e:
        print("ERROR processing twilio webhook:", e, traceback.format_exc())
        # if you want: refund in case we deducted but failed to insert (double-safety)
        # refund_credits_if_needed(phone, minutes)  <-- implement if required
        return ("", 204)



# ----------------------------------
# Razorpay webhook (idempotent)
# ----------------------------------
@app.route("/razorpay-webhook", methods=["POST"])
def razorpay_webhook():
    """
    Razorpay webhook endpoint (production-safe).
    - Verify signature using raw request bytes and the X-Razorpay-Signature header.
    - If verification passes, parse JSON and delegate to handle_webhook_event().
    - Returns:
        200 OK  -> processed successfully
        204 No Content -> event ignored (not relevant)
        400 Bad Request -> signature or JSON invalid
        500 Internal Server Error -> processing exception
    """
    # raw bytes (important — do not decode before verification)
    raw_bytes = request.get_data()
    signature_hdr = request.headers.get("X-Razorpay-Signature", "") or request.headers.get("x-razorpay-signature", "")

    debug_print("DEBUG — razorpay webhook received, signature header:", signature_hdr)
    debug_print("DEBUG — raw body (first 300 bytes):", raw_bytes[:300])

    # 1) Verify signature (use bytes + header). verify_razorpay_webhook expects bytes.
    try:
        verified = verify_razorpay_webhook(raw_bytes, signature_hdr)
    except Exception as e:
        debug_print("verify_razorpay_webhook raised exception:", e, traceback.format_exc())
        verified = False

    if not verified:
        # In production we should reject invalid signatures
        debug_print("Razorpay webhook signature verification FAILED. Rejecting with 400.")
        return ("Signature verification failed", 400)

    # 2) Parse JSON after successful signature verification
    try:
        event_json = request.get_json(force=True)
    except Exception as e:
        debug_print("Invalid Razorpay webhook JSON:", e, traceback.format_exc())
        return ("Invalid JSON", 400)

    # 3) Delegate to handler (idempotent). handle_webhook_event returns a dict summary.
    try:
        res = handle_webhook_event(event_json)
        debug_print("Razorpay webhook handled:", res)

        # Map handler response to HTTP code:
        status = res.get("status", "").lower()
        if status in ("ignored", "no_payment_entity"):
            return ("", 204)
        if status == "ok":
            return ("OK", 200)

        # anything else -> internal error
        debug_print("Unhandled handler result (treat as error):", res)
        return (str(res), 500)
    except Exception as e:
        debug_print("Error handling Razorpay webhook:", e, traceback.format_exc())
        return ("Internal error", 500)



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
    # Use FLASK_DEBUG env var for local debugging only. Defaults to False in production.
    flask_debug = str(os.getenv("FLASK_DEBUG", "0")).lower() in ("1", "true", "yes")
    debug_print("Starting Flask app (FLASK_DEBUG=%s) on port %s" % (flask_debug, os.getenv("PORT", "5000")))
    # Always bind to 0.0.0.0 so Render/local dev can reach it
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)), debug=flask_debug)

