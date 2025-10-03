# db.py (PostgreSQL version)
import psycopg2
from psycopg2.extras import RealDictCursor
from datetime import datetime, timedelta
import os
from dotenv import load_dotenv
load_dotenv()


DB_URL = os.getenv("DATABASE_URL")

def get_conn():
    return psycopg2.connect(DB_URL, cursor_factory=RealDictCursor)

def init_db():
    """Create tables and helpful indexes if they don't exist."""
    with get_conn() as conn, conn.cursor() as cur:
         # Users table
        cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            phone TEXT UNIQUE NOT NULL,
            created_at TIMESTAMP DEFAULT NOW(),
            credits_remaining FLOAT DEFAULT 30.0,
            subscription_active BOOLEAN DEFAULT FALSE,
            subscription_expiry TIMESTAMP,
            razorpay_customer_id TEXT
        );
        """)

        # Payments table
        cur.execute("""
        CREATE TABLE IF NOT EXISTS payments (
            id SERIAL PRIMARY KEY,
            phone TEXT,
            razorpay_payment_id TEXT UNIQUE,
            amount INTEGER,
            currency TEXT DEFAULT 'INR',
            status TEXT,
            created_at TIMESTAMP DEFAULT NOW(),
            updated_at TIMESTAMP
        );
        """)

        # Meeting notes table
        cur.execute("""
        CREATE TABLE IF NOT EXISTS meeting_notes (
            id SERIAL PRIMARY KEY,
            phone TEXT NOT NULL,
            audio_file TEXT,
            transcript TEXT,
            summary TEXT,
            created_at TIMESTAMP DEFAULT NOW()
        );
        """)


        # indexes (idempotent with IF NOT EXISTS)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_payments_phone ON payments (phone)")
        # create a unique index on razorpay_payment_id to prevent duplicates
        cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_payments_razorpay_payment_id ON payments (razorpay_payment_id)")
        conn.commit()


def get_user(phone):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM users WHERE phone=%s", (phone,))
        return cur.fetchone()

def save_user(user):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("""
            INSERT INTO users (phone, created_at, credits_remaining, subscription_active, subscription_expiry, razorpay_customer_id)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (phone)
            DO UPDATE SET
              created_at = EXCLUDED.created_at,
              credits_remaining = EXCLUDED.credits_remaining,
              subscription_active = EXCLUDED.subscription_active,
              subscription_expiry = EXCLUDED.subscription_expiry,
              razorpay_customer_id = EXCLUDED.razorpay_customer_id
        """, (
            user.get("phone"),
            user.get("created_at"),
            user.get("credits_remaining"),
            user.get("subscription_active"),
            user.get("subscription_expiry"),
            user.get("razorpay_customer_id")
        ))
        conn.commit()

def get_or_create_user(phone, free_minutes=30.0):
    user = get_user(phone)
    if user:
        return user
    user = {
        "phone": phone,
        "created_at": datetime.utcnow(),
        "credits_remaining": free_minutes,
        "subscription_active": False,
        "subscription_expiry": None,
        "razorpay_customer_id": None
    }
    save_user(user)
    return user

def deduct_minutes(phone, minutes):
    user = get_user(phone)
    if not user:
        user = get_or_create_user(phone)
    if user["subscription_active"]:
        return float("inf")
    remaining = float(user["credits_remaining"] or 0.0)
    remaining_after = max(0.0, remaining - float(minutes))
    user["credits_remaining"] = remaining_after
    save_user(user)
    return remaining_after

def get_remaining_minutes(phone):
    user = get_user(phone)
    if not user:
        return 0.0
    if user["subscription_active"]:
        return float("inf")
    return float(user["credits_remaining"] or 0.0)

def set_subscription_active(phone, days=30):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("""
            UPDATE users
            SET subscription_active = TRUE,
                subscription_expiry = NOW() + (%s || ' days')::interval,
                credits_remaining = GREATEST(COALESCE(credits_remaining, 0), 0)
            WHERE phone = %s
        """, (days, phone))
        conn.commit()


# db.py (partial) — replace record_payment with this
from datetime import datetime

def record_payment(phone, razorpay_payment_id, amount, currency="INR", status="created"):
    """
    Insert or update a payment row for razorpay_payment_id.
    This operation is idempotent: repeated calls with same
    razorpay_payment_id will update the row instead of raising.
    """
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("""
            INSERT INTO payments (phone, razorpay_payment_id, amount, currency, status, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (razorpay_payment_id)
            DO UPDATE SET
                phone = EXCLUDED.phone,
                amount = EXCLUDED.amount,
                currency = EXCLUDED.currency,
                status = EXCLUDED.status,
                updated_at = EXCLUDED.updated_at
            RETURNING id, status;
        """, (phone, razorpay_payment_id, amount, currency, status, datetime.utcnow(), datetime.utcnow()))
        row = cur.fetchone()
        conn.commit()
        # Return the id and status for further logic if needed
        if row:
            # RealDictCursor returns dict; handle both
            if isinstance(row, dict):
                return row.get("id"), row.get("status")
            return row[0], row[1]
        return None, None

def save_meeting_notes(phone, audio_file, transcript, summary):
    """
    Store the meeting transcription + summary for a user.
    """
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("""
            INSERT INTO meeting_notes (phone, audio_file, transcript, summary, created_at)
            VALUES (%s, %s, %s, %s, NOW())
        """, (phone, audio_file, transcript, summary))
        conn.commit()

def save_meeting_notes_with_sid(phone, audio_file, transcript, summary, message_sid=None):
    """
    Save meeting notes but include message_sid for deduplication.
    If message_sid is already in DB, skip insert.
    """
    with get_conn() as conn, conn.cursor() as cur:
        if message_sid:
            cur.execute("SELECT 1 FROM meeting_notes WHERE message_sid=%s", (message_sid,))
            if cur.fetchone():
                # Already saved, skip
                return

        cur.execute("""
            INSERT INTO meeting_notes (phone, audio_file, transcript, summary, message_sid)
            VALUES (%s, %s, %s, %s, %s)
        """, (phone, audio_file, transcript, summary, message_sid))

