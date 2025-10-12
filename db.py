# db.py (PostgreSQL version)
import psycopg2
from utils import normalize_phone_for_db
from psycopg2.extras import RealDictCursor
from datetime import datetime, timedelta
from contextlib import contextmanager
import os
import json




# Use DATABASE_URL from environment
DB_URL = os.getenv("DATABASE_URL")
if not DB_URL:
    raise RuntimeError("DATABASE_URL must be set in environment (production).")

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
            reference_id TEXT,
            notes JSONB,
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

def get_or_create_user(raw_phone: str):
    phone = normalize_phone_for_db(raw_phone)
    with get_conn() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:

        cur.execute("SELECT * FROM users WHERE phone = %s", (phone,))
        row = cur.fetchone()
        if row:
            return dict(row)
        # create default user row
        cur.execute("""
            INSERT INTO users (phone, credits_remaining, subscription_active, subscription_expiry, created_at)
            VALUES (%s, %s, %s, %s, now())
            RETURNING *
        """, (phone, 30.0, False, None))
        new_row = cur.fetchone()
        conn.commit()
        return dict(new_row) if new_row else None

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

def record_payment(phone, razorpay_payment_id, amount, currency="INR", status="created", reference_id=None, notes=None):
    """
    Insert or update a payment row for razorpay_payment_id.
    Idempotent — repeated calls update existing row.
    Returns (id, status).
    """
    from datetime import datetime
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("""
            INSERT INTO payments (phone, razorpay_payment_id, amount, currency, status, reference_id, notes, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (razorpay_payment_id)
            DO UPDATE SET
                phone = COALESCE(EXCLUDED.phone, payments.phone),
                amount = EXCLUDED.amount,
                currency = EXCLUDED.currency,
                status = EXCLUDED.status,
                reference_id = COALESCE(EXCLUDED.reference_id, payments.reference_id),
                notes = COALESCE(EXCLUDED.notes, payments.notes),
                updated_at = EXCLUDED.updated_at
            RETURNING id, status;
        """, (
            phone,
            razorpay_payment_id,
            amount,
            currency,
            status,
            reference_id,
            json.dumps(notes) if notes is not None else None,
            datetime.utcnow(),
            datetime.utcnow()
        ))
        row = cur.fetchone()
        conn.commit()
        if not row:
            return None, None
        # handle RealDictCursor vs tuple
        if isinstance(row, dict):
            return row.get("id"), row.get("status")
        return row[0], row[1]


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

def save_meeting_notes_with_sid(raw_phone, audio_file, transcript, summary, message_sid=None):
    """
    Save meeting notes and dedupe by message_sid.
    Accepts phone as raw string (e.g. '+919876543210' or 'whatsapp:+919876543210').
    Returns dict {id: ..., skipped: True/False}
    """
    phone = normalize_phone_for_db(raw_phone)
    with get_conn() as conn, conn.cursor() as cur:
        if message_sid:
            cur.execute("SELECT 1 FROM meeting_notes WHERE message_sid=%s LIMIT 1", (message_sid,))
            if cur.fetchone():
                return {"skipped": True, "id": None}

        cur.execute("""
            INSERT INTO meeting_notes (phone, audio_file, transcript, summary, message_sid, created_at)
            VALUES (%s, %s, %s, %s, %s, now())
            RETURNING id
        """, (phone, audio_file, transcript, summary, message_sid))
        row = cur.fetchone()
        conn.commit()
        return {"skipped": False, "id": row[0] if row else None}



def upsert_payment_and_activate(raw_phone, razorpay_payment_id, amount, status):
    """
    Upsert a payment row using razorpay_payment_id unique index and activate user if status=='captured'
    """
    phone = normalize_phone_for_db(raw_phone)
    with get_conn() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:

        # Upsert payment (requires unique index on razorpay_payment_id)
        cur.execute("""
          INSERT INTO payments (phone, razorpay_payment_id, amount, status, created_at)
          VALUES (%s, %s, %s, %s, now())
          ON CONFLICT (razorpay_payment_id)
          DO UPDATE SET status = EXCLUDED.status, amount = EXCLUDED.amount
          RETURNING id, razorpay_payment_id, status;
        """, (phone, razorpay_payment_id, amount, status))
        payment_row = cur.fetchone()

        activated = False
        if status and str(status).lower() == 'captured':
            # create user if missing, and activate/extend subscription
            cur.execute("""
                INSERT INTO users (phone, credits_remaining, subscription_active, subscription_expiry, created_at)
                VALUES (%s, %s, TRUE, now() + interval '30 days', now())
                ON CONFLICT (phone) DO UPDATE
                  SET subscription_active = TRUE,
                      subscription_expiry = now() + interval '30 days'
                RETURNING phone, subscription_active, subscription_expiry;
            """, (phone, 30.0))
            _ = cur.fetchone()
            activated = True

        conn.commit()
        return {"payment": dict(payment_row) if payment_row else None, "activated": activated}

def get_user_by_phone(raw_phone):
    phone = normalize_phone_for_db(raw_phone)
    with get_conn() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:

        cur.execute("SELECT * FROM users WHERE phone = %s", (phone,))
        row = cur.fetchone()
        return dict(row) if row else None
    
def decrement_minutes_if_available(raw_phone, minutes_to_deduct: float):
    phone = normalize_phone_for_db(raw_phone)
    with get_conn() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:

        # fetch current values
        cur.execute("SELECT credits_remaining, subscription_active, subscription_expiry FROM users WHERE phone = %s FOR UPDATE", (phone,))
        row = cur.fetchone()
        if not row:
            # user missing — create default
            cur.execute("INSERT INTO users (phone, credits_remaining) VALUES (%s, %s) RETURNING credits_remaining", (phone, 30.0))
            row = cur.fetchone()

        # If subscription active & not expired, allow unlimited (or don't decrement)
        sub_active = bool(row.get('subscription_active'))
        expiry = row.get('subscription_expiry')
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        if sub_active and (expiry is None or expiry > now):
            # subscription active: do not deduct (or deduct differently)
            conn.commit()
            return {"ok": True, "deducted": 0.0, "remaining": row.get('credits_remaining')}

        current = float(row.get('credits_remaining') or 0.0)
        if current < minutes_to_deduct:
            return {"ok": False, "reason": "insufficient_credits", "remaining": current}
        new_remaining = current - minutes_to_deduct
        cur.execute("UPDATE users SET credits_remaining = %s WHERE phone = %s", (new_remaining, phone))
        conn.commit()
        return {"ok": True, "deducted": minutes_to_deduct, "remaining": new_remaining}




# --- Task CRUD ---
def create_task(phone_or_user_id, title, description=None, due_at=None, priority=3, source='whatsapp', metadata=None, recurring_rule=None):
    """
    Accepts either normalized phone string OR user_id integer.
    Returns created task row as dict.
    """
    metadata = metadata or {}
    with get_conn() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        if isinstance(phone_or_user_id, str):
            user = get_or_create_user(phone_or_user_id)
            user_id = user['id']
        else:
            user_id = int(phone_or_user_id)

        cur.execute("""
            INSERT INTO tasks (user_id, title, description, due_at, priority, source, metadata, recurring_rule, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now(), now())
            RETURNING *;
        """, (user_id, title, description, due_at, priority, source, json.dumps(metadata), recurring_rule))
        row = cur.fetchone()
        conn.commit()
        return dict(row) if row else None

def get_tasks_for_user(phone_or_user_id, status='open', limit=50):
    with get_conn() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        if isinstance(phone_or_user_id, str):
            user = get_user_by_phone(phone_or_user_id)
            if not user:
                return []
            user_id = user['id']
        else:
            user_id = int(phone_or_user_id)
        cur.execute("""
            SELECT * FROM tasks
            WHERE user_id = %s AND status = %s AND deleted = false
            ORDER BY due_at NULLS LAST, created_at DESC
            LIMIT %s;
        """, (user_id, status, limit))
        rows = cur.fetchall()
        return [dict(r) for r in rows]

def mark_task_done(task_id, phone_or_user_id=None):
    with get_conn() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        # optional ownership check
        if phone_or_user_id is not None:
            if isinstance(phone_or_user_id, str):
                user = get_user_by_phone(phone_or_user_id)
                if not user:
                    return False
                user_id = user['id']
            else:
                user_id = int(phone_or_user_id)
            cur.execute("UPDATE tasks SET status='done', updated_at=now() WHERE id=%s AND user_id=%s RETURNING *", (task_id, user_id))
        else:
            cur.execute("UPDATE tasks SET status='done', updated_at=now() WHERE id=%s RETURNING *", (task_id,))
        updated = cur.fetchone()
        # cancel pending reminders
        cur.execute("UPDATE reminders SET sent = true, sent_at = now() WHERE task_id = %s AND sent = false", (task_id,))
        conn.commit()
        return dict(updated) if updated else None

# --- Reminders ---
def get_pending_reminders(limit=100):
    with get_conn() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("""
            SELECT r.*, t.title AS task_title, u.phone AS phone
            FROM reminders r
            JOIN tasks t ON t.id = r.task_id
            JOIN users u ON u.id = r.user_id
            WHERE r.sent = false AND r.remind_at <= now()
            ORDER BY r.remind_at
            LIMIT %s
        """, (limit,))
        return [dict(r) for r in cur.fetchall()]

def mark_reminder_sent(reminder_id):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("UPDATE reminders SET sent = true, sent_at = now(), attempts = attempts + 1 WHERE id = %s", (reminder_id,))
        conn.commit()

# --- Search helper ---
import json
def search_tasks(phone_or_user_id, query_text, limit=25):
    with get_conn() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        if isinstance(phone_or_user_id, str):
            user = get_user_by_phone(phone_or_user_id)
            if not user:
                return []
            user_id = user['id']
        else:
            user_id = int(phone_or_user_id)

        cur.execute("""
            SELECT id, title, description, due_at, status
            FROM tasks
            WHERE user_id = %s AND search_vector @@ plainto_tsquery('simple', %s)
            ORDER BY due_at NULLS LAST
            LIMIT %s
        """, (user_id, query_text, limit))
        return [dict(r) for r in cur.fetchall()]

# --- Sharing & tags ---
def share_task(task_id, team_user_phone_or_id, permission='view'):
    with get_conn() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        if isinstance(team_user_phone_or_id, str):
            partner = get_or_create_user(team_user_phone_or_id)
            team_user_id = partner['id']
        else:
            team_user_id = int(team_user_phone_or_id)
        cur.execute("""
           INSERT INTO task_shares (task_id, team_user_id, permission, created_at)
           VALUES (%s, %s, %s, now())
           ON CONFLICT DO NOTHING
           RETURNING *;
        """, (task_id, team_user_id, permission))
        row = cur.fetchone()
        conn.commit()
        return dict(row) if row else None

def add_tag(task_id, tag):
    with get_conn() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("INSERT INTO task_tags (task_id, tag, created_at) VALUES (%s, %s, now()) ON CONFLICT DO NOTHING RETURNING *", (task_id, tag))
        row = cur.fetchone()
        conn.commit()
        return dict(row) if row else None
