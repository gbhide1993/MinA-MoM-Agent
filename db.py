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
        # users table
        cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            phone TEXT PRIMARY KEY,
            created_at TIMESTAMP,
            credits_remaining REAL,
            subscription_active BOOLEAN,
            subscription_expiry TIMESTAMP,
            razorpay_customer_id TEXT
        )
        """)
        # payments table
        cur.execute("""
        CREATE TABLE IF NOT EXISTS payments (
            id SERIAL PRIMARY KEY,
            phone TEXT,
            razorpay_payment_id TEXT,
            amount INTEGER,
            currency TEXT,
            status TEXT,
            created_at TIMESTAMP
        )
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
    user = get_or_create_user(phone)
    user["subscription_active"] = True
    user["subscription_expiry"] = datetime.utcnow() + timedelta(days=days)
    save_user(user)

def record_payment(phone, razorpay_payment_id, amount, currency="INR", status="success"):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("""
            INSERT INTO payments (phone, razorpay_payment_id, amount, currency, status, created_at)
            VALUES (%s, %s, %s, %s, %s, %s)
        """, (phone, razorpay_payment_id, amount, currency, status, datetime.utcnow()))
        conn.commit()
