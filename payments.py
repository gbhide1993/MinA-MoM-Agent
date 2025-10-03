# payments.py
import os
import razorpay
import hmac
import hashlib
import base64
import json
from datetime import datetime
import traceback

from db import record_payment, set_subscription_active, save_user, get_or_create_user
from db import get_user, get_conn  # get_conn used for direct queries

RAZORPAY_KEY_ID = os.getenv("RAZORPAY_KEY_ID")
RAZORPAY_KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET")
RAZORPAY_WEBHOOK_SECRET = os.getenv("RAZORPAY_WEBHOOK_SECRET")
PLATFORM_URL = os.getenv("PLATFORM_URL")  # e.g. https://mina-mom-agent.onrender.com

# Create client (singleton)
_client = None
def get_client():
    global _client
    if _client is None:
        if not RAZORPAY_KEY_ID or not RAZORPAY_KEY_SECRET:
            raise RuntimeError("Razorpay keys not configured in environment")
        _client = razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))
    return _client


def create_payment_link_for_phone(phone, amount_in_rupees=499, purpose="MinA subscription"):
    """
    Create a Razorpay Payment Link and return a dict with details.
    Also records a payments row with status 'created'.
    """
    client = get_client()

    amount_paise = int(amount_in_rupees * 100)
    ref_id = f"{phone.replace('whatsapp:', '').replace('+','')}-{int(datetime.utcnow().timestamp())}"
    payload = {
        "amount": amount_paise,
        "currency": "INR",
        "accept_partial": False,
        "reference_id": ref_id,
        "description": purpose,
        "customer": {
            "contact": phone.replace("whatsapp:", "")
        },
        "notify": { "sms": False, "email": False },
        "reminder_enable": True,
    }

    link = client.payment_link.create(payload)
    # Persist a pending payment record
    try:
        record_payment(
            phone=phone,
            razorpay_payment_id=link.get("id"),
            amount=link.get("amount"),
            currency=link.get("currency", "INR"),
            status=link.get("status", "created")
        )
    except Exception as e:
        print("Warning: record_payment failed after creating link:", e, traceback.format_exc())

    return {
        "id": link.get("id"),
        "short_url": link.get("short_url"),
        "amount": link.get("amount"),
        "status": link.get("status"),
        "reference_id": link.get("reference_id")
    }


def verify_razorpay_webhook(payload_body: str, header_signature: str) -> bool:
    """
    Verify webhook signature using Razorpay SDK if possible, fallback to manual HMAC.
    payload_body must be a decoded string (UTF-8).
    """
    if not RAZORPAY_WEBHOOK_SECRET:
        print("verify_razorpay_webhook: missing RAZORPAY_WEBHOOK_SECRET")
        return False

    # Try SDK verification (preferred)
    try:
        client = get_client()
        client.utility.verify_webhook_signature(payload_body, header_signature, RAZORPAY_WEBHOOK_SECRET)
        # If no exception, signature verified
        return True
    except Exception as e:
        # SDK verification may fail if keys missing or mismatch; print for debug and fall back.
        print("verify_razorpay_webhook: SDK verification failed:", repr(e))

    # Fallback: manual HMAC-SHA256 + base64 compare
    try:
        computed = base64.b64encode(
            hmac.new(RAZORPAY_WEBHOOK_SECRET.encode("utf-8"), payload_body.encode("utf-8"), hashlib.sha256).digest()
        ).decode()
        # constant-time comparison
        ok = hmac.compare_digest(computed, header_signature)
        if not ok:
            print("verify_razorpay_webhook: fallback verification failed. header:", header_signature, "computed:", computed)
        return ok
    except Exception as e:
        print("verify_razorpay_webhook: fallback verification exception:", e, traceback.format_exc())
        return False


# payments.py — replace handle_webhook_event with this

def handle_webhook_event(event_json: dict):
    """
    Idempotent webhook handler.
    - Upserts payment record for razorpay_payment_id.
    - Activates subscription only when status transitions into a 'paid/captured' state.
    - Returns a dict describing the result for logging.
    """
    try:
        event = event_json.get("event")
        data = event_json.get("payload", {})

        # We focus on payment_link.paid and payment.captured and related events.
        if event not in ("payment_link.paid", "payment_link.payment_paid", "payment.captured", "payment.authorized", "payment.failed"):
            return {"status": "ignored", "event": event}

        # Extract payment entity
        payment_entity = None
        if isinstance(data.get("payment"), dict):
            payment_entity = data.get("payment", {}).get("entity")
        # fallback scan
        if not payment_entity:
            for val in data.values():
                if isinstance(val, dict) and isinstance(val.get("entity"), dict):
                    ent = val["entity"]
                    if ent.get("id") and ent.get("status"):
                        payment_entity = ent
                        break

        if not payment_entity:
            print("handle_webhook_event: no payment entity found in payload")
            return {"status": "no_payment_entity", "event": event}

        razorpay_payment_id = payment_entity.get("id")
        amount = payment_entity.get("amount")
        status = (payment_entity.get("status") or "").lower()
        contact = payment_entity.get("contact") or payment_entity.get("customer_id") or None

        # Normalize phone: prefer whatsapp:+<number>
        phone = None
        if contact:
            # contact may be '919xxxxxxxx'
            phone = f"whatsapp:{contact}" if not str(contact).startswith("whatsapp:") else contact

        # ------------- DB: fetch existing payment if any -------------
        existing = None
        try:
            with get_conn() as conn, conn.cursor() as cur:
                cur.execute("SELECT razorpay_payment_id, status, phone FROM payments WHERE razorpay_payment_id=%s LIMIT 1", (razorpay_payment_id,))
                existing = cur.fetchone()
                # existing could be dict (RealDictCursor) or tuple
        except Exception as e:
            print("handle_webhook_event: DB lookup failed:", e)

        prev_status = None
        if existing:
            if isinstance(existing, dict):
                prev_status = (existing.get("status") or "").lower()
                if not phone:
                    phone = existing.get("phone")
            else:
                # tuple fallback: assume columns in order (id, phone, razorpay_payment_id, amount, currency, status, created_at)
                # adjust indexing if your select returns different order; safer to use select with named columns
                try:
                    # try to find status and phone by keys if row is sequence - best effort
                    prev_status = existing.get("status") if hasattr(existing, "get") else existing[5] if len(existing) > 5 else None
                except Exception:
                    prev_status = None

        # ------------- Upsert the payment (idempotent) -------------
        try:
            _, latest_status = record_payment(phone=phone, razorpay_payment_id=razorpay_payment_id, amount=amount, currency="INR", status=status)
        except Exception as e:
            print("handle_webhook_event: record_payment failed:", e)
            # continue — we may still decide not to crash on payment logging failure
            latest_status = status

        latest_status = (latest_status or status).lower()

        # ------------- Decide whether to activate subscription -------------
        paid_states = ("captured", "paid", "authorized")
        should_activate = False
        if latest_status in paid_states:
            # If prev_status is None (new record) OR prev_status not in paid_states, we need to activate now.
            if not prev_status or prev_status not in paid_states:
                should_activate = True

        if should_activate and phone:
            try:
                # set_subscription_active should be idempotent (update existing row)
                set_subscription_active(phone, days=30)
                print("handle_webhook_event: subscription activated for", phone)
            except Exception as e:
                print("handle_webhook_event: set_subscription_active failed:", e)

        return {"phone": phone, "razorpay_payment_id": razorpay_payment_id, "prev_status": prev_status, "latest_status": latest_status, "activated": should_activate}
    except Exception as e:
        print("handle_webhook_event: unhandled exception:", e, traceback.format_exc())
        return {"status": "error", "error": str(e)}
