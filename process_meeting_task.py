# tasks/process_meeting.py
import os
import tempfile
import requests
import traceback
from rq import get_current_job

# Import local helpers - these should exist in your repo
from db import get_conn  # used for DB updates
from utils import (
    send_whatsapp,
    normalize_phone_for_db,
    compute_audio_duration_seconds,
)
from openai_client import transcribe_file, summarize_text

# Config
REDIS_URL = os.getenv("REDIS_URL")
if not REDIS_URL:
    raise RuntimeError("REDIS_URL environment variable is not set. Set REDIS_URL to your Redis connection string.")

SUMMARIZE_INSTRUCTIONS = os.getenv(
    "SUMMARIZE_INSTRUCTIONS",
    "Extract TL;DR, action items, decisions. Return JSON or short bullet format."
)

def safe_download(url, timeout=60):
    """
    Download media URL to a temporary local file path and return path.
    Uses requests streaming.
    """
    if not url:
        return None
    resp = requests.get(url, stream=True, timeout=timeout)
    resp.raise_for_status()
    content_type = resp.headers.get("content-type", "")
    ext = ""
    if "mpeg" in content_type:
        ext = ".mp3"
    elif "wav" in content_type:
        ext = ".wav"
    elif "ogg" in content_type:
        ext = ".ogg"
    elif "m4a" in content_type or "mp4" in content_type:
        ext = ".m4a"
    else:
        ext = os.path.splitext(url.split("?")[0])[1] or ".m4a"

    fd, tmp = tempfile.mkstemp(suffix=ext)
    os.close(fd)
    with open(tmp, "wb") as f:
        for chunk in resp.iter_content(chunk_size=8192):
            if chunk:
                f.write(chunk)
    return tmp

def fetch_meeting_row(meeting_id):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, phone, audio_file, transcript, summary, message_sid, created_at "
            "FROM meeting_notes WHERE id=%s LIMIT 1",
            (meeting_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        # If your cursor returns a dict-like, return directly
        if hasattr(row, "get"):
            return dict(row)
        return {
            "id": row[0],
            "phone": row[1],
            "audio_file": row[2],
            "transcript": row[3],
            "summary": row[4],
            "message_sid": row[5],
            "created_at": row[6],
        }

def mark_meeting_processed(meeting_id, transcript, summary):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE meeting_notes SET transcript=%s, summary=%s, updated_at=now() WHERE id=%s",
            (transcript, summary, meeting_id),
        )
        conn.commit()

def process_meeting(meeting_id, media_url=None):
    """
    Entry point for RQ worker.
    - meeting_id: id in meeting_notes table
    - media_url: optional override (if webhook passes direct media URL)
    Returns dict with outcome.
    """
    job = get_current_job()
    job_id = job.id if job else None

    try:
        row = fetch_meeting_row(meeting_id)
        if not row:
            print(f"[process_meeting] meeting_id {meeting_id} not found")
            return {"ok": False, "reason": "missing_meeting"}

        phone = row.get("phone")
        phone_norm = normalize_phone_for_db(phone)

        # Idempotency guard: if transcript exists, skip reprocessing
        if row.get("transcript"):
            print(f"[process_meeting] meeting {meeting_id} already processed — skipping.")
            return {"ok": True, "skipped": True}

        # Decide media_url (argument > DB)
        media_url = media_url or row.get("audio_file")
        if not media_url:
            print(f"[process_meeting] no media_url for meeting {meeting_id}")
            return {"ok": False, "reason": "no_media"}

        # Download media
        local_path = None
        try:
            local_path = safe_download(media_url)
        except Exception as e:
            print(f"[process_meeting] download failed: {e}")
            traceback.print_exc()
            try:
                send_whatsapp(phone_norm, "⚠️ I couldn't download the audio you sent. Please resend.")
            except Exception:
                pass
            return {"ok": False, "reason": "download_failed"}

        # Try compute duration (best-effort)
        try:
            duration_seconds = compute_audio_duration_seconds(local_path)
            minutes = round(duration_seconds / 60.0, 2)
        except Exception:
            minutes = None

        # Transcribe audio file
        try:
            transcript = transcribe_file(local_path)
            transcript = transcript or ""
        except Exception as e:
            print(f"[process_meeting] transcription failed: {e}")
            traceback.print_exc()
            try:
                send_whatsapp(phone_norm, "⚠️ I couldn't transcribe your audio. Try sending a shorter clip.")
            except Exception:
                pass
            return {"ok": False, "reason": "transcription_failed"}

        # Summarize (one-pass; can be replaced with hierarchical later)
        try:
            summary_text = summarize_text(transcript, instructions=SUMMARIZE_INSTRUCTIONS)
        except Exception as e:
            print(f"[process_meeting] summarization failed: {e}")
            traceback.print_exc()
            summary_text = None

        # Persist transcript + summary
        try:
            mark_meeting_processed(meeting_id, transcript, summary_text)
        except Exception as e:
            print(f"[process_meeting] DB update failed: {e}")
            traceback.print_exc()

        # Send result back to user
        try:
            final_msg = summary_text or "📝 Transcription complete. (No summary available.)"
            send_whatsapp(phone_norm, final_msg)
        except Exception as e:
            print(f"[process_meeting] send_whatsapp failed: {e}")
            traceback.print_exc()

        # Cleanup
        try:
            if local_path and os.path.exists(local_path):
                os.remove(local_path)
        except Exception:
            pass

        return {"ok": True, "meeting_id": meeting_id, "minutes": minutes, "job_id": job_id}

    except Exception as e:
        print(f"[process_meeting] unexpected error: {e}")
        traceback.print_exc()
        return {"ok": False, "reason": "unexpected_error", "error": str(e)}
        
