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


def handle_webhook_event(event_json: dict):
    """
    Called after webhook signature verified. Update DB accordingly.
    Expects events like 'payment_link.paid' or 'payment.captured'.
    Returns a dict describing what happened for logging.
    """
    try:
        event = event_json.get("event")
        data = event_json.get("payload", {})

        # Handle payment_link.paid and similar events
        if event in ("payment_link.paid", "payment_link.payment_paid", "payment.captured", "payment.authorized"):
            # Try to locate payment entity
            payment_entity = None
            link_entity = None

            # Common payload structure
            if isinstance(data.get("payment"), dict):
                payment_entity = data.get("payment", {}).get("entity")
            if isinstance(data.get("payment_link"), dict):
                link_entity = data.get("payment_link", {}).get("entity")

            # Fallback scanning
            if not payment_entity:
                for key, val in data.items():
                    if isinstance(val, dict) and val.get("entity") and isinstance(val["entity"], dict):
                        ent = val["entity"]
                        # Heuristic: payment entity usually has 'status' and 'id'
                        if ent.get("status") and ent.get("id"):
                            payment_entity = ent
                            break

            # If we have a payment entity, extract fields
            if payment_entity:
                razorpay_payment_id = payment_entity.get("id")
                amount = payment_entity.get("amount")
                status = payment_entity.get("status")
                contact = payment_entity.get("contact") or payment_entity.get("customer_id")

                # Try to find phone from payments table by razorpay_payment_id
                phone = None
                try:
                    with get_conn() as conn, conn.cursor() as cur:
                        cur.execute(
                            "SELECT phone FROM payments WHERE razorpay_payment_id=%s LIMIT 1",
                            (razorpay_payment_id,)
                        )
                        row = cur.fetchone()
                        if row:
                            # row is likely a dict from RealDictCursor
                            if isinstance(row, dict):
                                phone = row.get("phone")
                            else:
                                # tuple fallback
                                phone = row[0]
                except Exception as e:
                    print("handle_webhook_event: DB lookup failed:", e, traceback.format_exc())

                # fallback to contact field if necessary
                if not phone and contact:
                    # ensure contact is numeric phone like 919xxxx
                    phone = "whatsapp:" + str(contact)

                if not phone:
                    # can't determine which user to credit; log and return
                    print("handle_webhook_event: could not determine phone for payment id:", razorpay_payment_id)
                    # Still record payment row with unknown phone so you can reconcile later
                    try:
                        record_payment(phone=None, razorpay_payment_id=razorpay_payment_id, amount=amount, currency="INR", status=status)
                    except Exception:
                        pass
                    return {"status": "no_phone", "razorpay_payment_id": razorpay_payment_id}

                # Record/update payment status
                try:
                    record_payment(phone=phone, razorpay_payment_id=razorpay_payment_id, amount=amount, currency="INR", status=status)
                except Exception as e:
                    print("handle_webhook_event: record_payment failed:", e, traceback.format_exc())

                # If payment captured/paid/authorized -> activate subscription
                if status in ("captured", "authorized", "paid"):
                    try:
                        set_subscription_active(phone, days=30)
                    except Exception as e:
                        print("handle_webhook_event: set_subscription_active failed:", e, traceback.format_exc())

                    # update user's razorpay_customer_id if present
                    try:
                        user = get_or_create_user(phone)
                        user["razorpay_customer_id"] = payment_entity.get("customer_id") or user.get("razorpay_customer_id")
                        save_user(user)
                    except Exception as e:
                        print("handle_webhook_event: saving user failed:", e, traceback.format_exc())

                    return {"phone": phone, "status": "activated"}

                return {"phone": phone, "status": status}

        # Unknown / ignored event
        return {"status": "ignored", "event": event}
    except Exception as e:
        print("handle_webhook_event: unhandled exception:", e, traceback.format_exc())
        return {"status": "error", "error": str(e)}
