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
from utils import send_whatsapp
from openai_client import transcribe_file, summarize_text
from redis import Redis
from rq import Queue


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
                # create user if missing (RETURNING may return a mapping or tuple depending on cursor factory)
                cur.execute("""
                    INSERT INTO users (phone, credits_remaining, subscription_active, created_at)
                    VALUES (%s, %s, %s, now())
                    RETURNING credits_remaining, subscription_active, subscription_expiry
                """, (phone, 30.0, False))
                newr = cur.fetchone()
                # Normalize fetch result whether it's mapping-like (RealDictRow) or tuple-like
                if newr is None:
                    # fallback defaults
                    credits_remaining = 30.0
                    sub_active = False
                    sub_expiry = None
                else:
                    if hasattr(newr, "get"):
                        # mapping-like
                        credits_remaining = float(newr.get("credits_remaining") or 0.0)
                        sub_active = bool(newr.get("subscription_active") or False)
                        sub_expiry = newr.get("subscription_expiry")
                    else:
                        # tuple-like
                        credits_remaining = float(newr[0] or 0.0)
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
                # Not enough credits: create Razorpay payment link and send it
                conn.rollback()
                try:
                    # Decide how much to charge — simplest: full subscription price (e.g., 30 minutes)
                    # Option A: charge flat subscription amount (recommended for simplicity)
                    SUBSCRIPTION_PRICE_RUPEES = float(os.getenv("SUBSCRIPTION_PRICE_RUPEES", "499.0"))

                    # Option B (alternative): charge pro-rata based on minutes_needed. Uncomment to use.
                    # price_per_min = float(os.getenv("PRICE_PER_MIN_RUPEE", "2.0"))
                    # amount_needed = round((to_deduct - credits_remaining) * price_per_min, 2)

                    # We'll generate a payment link for the subscription price so user gets full 30 minutes on capture.
                    payment = create_payment_link_for_phone(phone, SUBSCRIPTION_PRICE_RUPEES)
                    order_id = payment.get("order_id") or payment.get("order", {}).get("id")
                    # Construct a human-friendly link — Razorpay has hosted payment pages; often client builds it using order id.
                    # The function returns order object — you can persist and use it. For now, send the order id and reference.
                    if payment and payment.get("order"):
                        # If SDK returns 'order' with 'id' or 'short_url' (if using payment link API) use that
                        url = payment.get("order").get("short_url") if payment.get("order").get("short_url") else f"{os.getenv('PLATFORM_URL','')}/pay?order_id={order_id}"
                    else:
                        url = f"{os.getenv('PLATFORM_URL','')}/pay?order_id={order_id}"

                    send_whatsapp(phone, (
                        "⚠️ You don’t have enough free minutes to transcribe this audio. "
                        "Top up to continue — follow this secure payment link:\n\n" + url
                    ))
                except Exception as e:
                    debug_print("Failed to create/send payment link:", e, traceback.format_exc())
                    send_whatsapp(phone, "⚠️ You have insufficient free minutes. Please visit the app to subscribe.")
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
            new_row = cur.fetchone()
            # Normalize whether fetchone returns mapping-like (RealDictRow) or tuple-like
            if not new_row:
                conn.rollback()
                raise RuntimeError("Failed to insert meeting_notes row (no row returned)")
            if hasattr(new_row, "get"):
                meeting_id = new_row.get("id")
            else:
                meeting_id = new_row[0]
            if meeting_id is None:
                conn.rollback()
                raise RuntimeError("Failed to read meeting id after insert")
            conn.commit()

                # 🧩 NEW BLOCK: Send payment link if balance is zero
            try:
                from db import get_user_credits
                credits_remaining = get_user_credits(phone)
            except Exception:
                credits_remaining = None

            if credits_remaining is not None and credits_remaining <= 0.0:
                from payments import create_payment_link_for_phone
                
                amount = float(os.getenv("SUBSCRIPTION_PRICE_RUPEES", "499.0"))
                payment = create_payment_link_for_phone(phone, amount)
                url = payment.get("order", {}).get("short_url") or f"{os.getenv('PLATFORM_URL','')}/pay?order_id={payment.get('order', {}).get('id')}"
                send_whatsapp(phone, f"ℹ️ You've used your free minutes. Top up here: {url}")




        # --- ENQUEUE RQ JOB (asynchronous processing) ---
        # Use Redis URL from env or default to redis service defined in docker-compose
        REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")

        try:
            redis_conn = Redis.from_url(REDIS_URL)
            q = Queue("default", connection=redis_conn)
            # Enqueue the background job. Use the module: function path for the worker to import.
            # If you moved the task to repo root as process_meeting_task.py use "process_meeting_task.process_meeting"
            # If you still have it named process_meeting in root adjust accordingly.
            job = q.enqueue(
                "process_meeting_task.process_meeting",   # module.function
                meeting_id,                               # first arg: meeting id in DB
                media_url,                                # second arg: the media URL (optional)
                job_timeout=60 * 60,                      # allow up to 1 hour for long files
                result_ttl=60 * 60
            )
            debug_print(f"Enqueued RQ job {job.id} for meeting_id={meeting_id}")
        except Exception as e:
            debug_print("Failed to enqueue RQ job:", e)
            # Inform user that async processing couldn't start (best-effort)
            try:
                send_whatsapp(phone, "⚠️ We couldn't start background processing for your audio. Please try again in a moment.")
            except Exception:
                pass
        # End enqueue block. Worker will transcribe, summarize, update DB and reply.

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

            if hasattr(row, "get"):
                user_obj = dict(row)
            else:
                # tuple-like: map columns by order
                user_obj = {
                    "phone": row[0],
                    "credits_remaining": row[1],
                    "subscription_active": row[2],
                    "subscription_expiry": row[3],
                    "created_at": row[4]
                }
            return jsonify({"user": user_obj}), 200
    except Exception as e:
        debug_print("admin_get_user error:", e, traceback.format_exc())
        return jsonify({"error": str(e)}), 500


@app.route("/admin/notes/<path:phone>", methods=["GET"])
def admin_get_notes(phone):
    try:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT id, audio_file, summary, created_at FROM meeting_notes WHERE phone=%s ORDER BY created_at DESC LIMIT 50", (phone,))
            rows = cur.fetchall()
            normalized = []
            for r in rows or []:
                if hasattr(r, "get"):
                    normalized.append(dict(r))
                else:
                    normalized.append({
                        "id": r[0],
                        "audio_file": r[1],
                        "summary": r[2],
                        "created_at": r[3]
                    })
            return jsonify({"notes": normalized}), 200
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



