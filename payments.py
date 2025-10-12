# payments.py
import os
import razorpay
import hmac
import hashlib
import base64
import json
from datetime import datetime
import traceback
import time
import logging
import requests



from db import record_payment, set_subscription_active, save_user, get_or_create_user
from db import record_payment as insert_payment
from db import get_user, get_conn  # get_conn used for direct queries
from db import upsert_payment_and_activate
from utils import normalize_phone_for_db
from typing import Optional

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


logger = logging.getLogger(__name__)

def create_payment_link_for_phone(phone: str, amount_in_rupees: float, currency: str = "INR", reference_id: Optional[str] = None):
    """
    Create a Razorpay order/payment link (production-safe):
      - amount_in_rupees: e.g. 10.5 -> converted to paise (1050)
      - phone: in normalized form (use normalize_phone_for_db before calling)
      - reference_id: optional external reference; if not provided one will be generated

    This function:
      - builds a stable reference_id (if not provided)
      - creates an order via SDK or REST
      - upserts a row in payments table (idempotent) using insert_payment / record_payment helper
      - returns the order dict (as returned by Razorpay) or raises an exception.
    """
    if amount_in_rupees is None:
        raise ValueError("amount_in_rupees required")

    # Normalize phone (best-effort)
    try:
        normalized_phone = normalize_phone_for_db(phone)
    except Exception:
        normalized_phone = phone

    # ensure integer paise
    try:
        amount_paise = int(round(float(amount_in_rupees) * 100))
    except Exception:
        raise ValueError("amount_in_rupees must be numeric")

    # stable reference id so we can look up / re-run idempotently
    if not reference_id:
        cleaned_phone = normalized_phone.replace("whatsapp:", "").replace("+", "")
        reference_id = f"ref-{cleaned_phone}-{int(time.time())}"

    # Build payload for Razorpay order
    payload = {
        "amount": amount_paise,
        "currency": currency,
        "receipt": reference_id,
        "payment_capture": 1,  # auto-capture recommended for simplicity
        "notes": {
            "phone": normalized_phone,
            "reference_id": reference_id
        }
    }

    # Create the order via SDK if available, otherwise via REST
    order = None
    try:
        client = get_client()  # your existing helper that returns a razorpay.Client or None
        if client:
            order = client.order.create(data=payload)
        else:
            # fallback to REST
            r = requests.post(
                "https://api.razorpay.com/v1/orders",
                auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET),
                json=payload,
                timeout=30
            )
            r.raise_for_status()
            order = r.json()
    except Exception as e:
        logger.exception("Failed to create Razorpay order: %s", e)
        raise

    # Ensure we have an order id
    order_id = order.get("id") or order.get("order_id") or order.get("entity", {}).get("id")
    if not order_id:
        logger.error("Razorpay order returned without id: %s", order)
        raise RuntimeError("Razorpay order creation failed (no id)")

    # Persist a payment record in DB (idempotent upsert)
    try:
        # Prefer insert_payment helper (insert or update based on razorpay_payment_id)
        # The amount column in DB expects paise (store consistent integer)
        insert_payment(
            phone=normalized_phone,
            razorpay_payment_id=order_id,
            amount=amount_paise,
            currency=currency,
            status=order.get("status", "created"),
            reference_id=reference_id
        )
    except Exception as e:
        # Log but do not delete the created order automatically; operator can reconcile
        logger.exception("Failed to persist payment row for order %s: %s", order_id, e)

    # Return the order object for the caller to construct payment link / respond to client
    return {
        "order": order,
        "reference_id": reference_id,
        "order_id": order_id,
        "amount_paise": amount_paise,
        "currency": currency,
    }


def verify_razorpay_webhook(payload_body: bytes, header_signature: str) -> bool:
    """
    Verify Razorpay webhook signature.
    - payload_body: raw request body bytes (important: exact bytes)
    - header_signature: X-Razorpay-Signature header string
    Returns True when verified.
    """
    if not RAZORPAY_WEBHOOK_SECRET:
        print("verify_razorpay_webhook: missing RAZORPAY_WEBHOOK_SECRET")
        return False

    # Try SDK verification first (preferred)
    try:
        client = get_client()
        # SDK expects string payload; pass decoded utf-8 string
        client.utility.verify_webhook_signature(payload_body.decode("utf-8"), header_signature, RAZORPAY_WEBHOOK_SECRET)
        return True
    except Exception as e:
        # SDK verification failed — fall back
        print("verify_razorpay_webhook: SDK verification failed:", repr(e))

    # Fallback: HMAC-SHA256
    try:
        # HMAC raw digest
        digest = hmac.new(RAZORPAY_WEBHOOK_SECRET.encode("utf-8"), payload_body, hashlib.sha256).digest()

        # Try base64 encoding (Razorpay examples often use base64)
        computed_b64 = base64.b64encode(digest).decode()
        if hmac.compare_digest(computed_b64, header_signature):
            return True

        # Try hex digest (some integrations/SDKs use hexdigest)
        computed_hex = hashlib.sha256(payload_body + b"").hexdigest()
        # the correct hex to compare would be hmac.new(secret, payload, sha256).hexdigest()
        computed_hex = hmac.new(RAZORPAY_WEBHOOK_SECRET.encode("utf-8"), payload_body, hashlib.sha256).hexdigest()
        if hmac.compare_digest(computed_hex, header_signature):
            return True

        # Nope
        print("verify_razorpay_webhook: fallback verification failed. header:", header_signature, "computed_b64:", computed_b64, "computed_hex:", computed_hex)
        return False
    except Exception as e:
        print("verify_razorpay_webhook: fallback exception:", e, traceback.format_exc())
        return False



# payments.py — replace handle_webhook_event with this

import traceback
from typing import Optional

def handle_webhook_event(event_json: dict) -> dict:
    """
    Clean, idempotent Razorpay webhook handler.

    Input: event_json (decoded JSON payload from Razorpay)
    Output: dict with keys:
      - status: "ok" | "ignored" | "error" | "no_payment_entity"
      - event: original event name
      - razorpay_payment_id: (if present)
      - prev_status: previous status from DB (if found)
      - latest_status: current status determined after upsert
      - activated: True if subscription activation attempted & succeeded
      - note: optional human-readable note
    """
    try:
        event = event_json.get("event")
        payload = event_json.get("payload", {}) or {}

        # Only handle relevant events; ignore others gracefully
        interested = {
            "payment_link.paid",
            "payment_link.payment_paid",
            "payment.captured",
            "payment.authorized",
            "payment.failed",
            "payment.authorized",
            "order.paid"
        }
        if event not in interested:
            return {"status": "ignored", "event": event, "note": "event not in interested set"}

        # --- Extract payment entity robustly ---
        payment_entity = None

        # Common safe extraction paths:
        if isinstance(payload.get("payment"), dict):
            payment_entity = payload.get("payment", {}).get("entity")

        # Fallback: scan payload values for something that looks like a payment entity
        if not payment_entity:
            for v in payload.values():
                if isinstance(v, dict):
                    ent = v.get("entity") or v.get("payment") or v.get("entity", None)
                    if isinstance(ent, dict) and ent.get("id") and ent.get("status"):
                        payment_entity = ent
                        break
                # sometimes payload has nested structure: payload -> payment -> entity
                # we already tried the common path above

        if not payment_entity:
            # nothing to do
            return {"status": "no_payment_entity", "event": event, "note": "no payment entity found"}

        # --- Read core fields ---
        razorpay_payment_id = payment_entity.get("id")
        amount = payment_entity.get("amount")  # usually in paise
        status_raw = (payment_entity.get("status") or "")
        latest_status_in_payload = status_raw.lower()

        # Try to extract a phone/contact if present
        contact = payment_entity.get("contact") or payment_entity.get("customer") or payment_entity.get("phone") or None
        phone: Optional[str] = None
        if contact:
            try:
                phone = normalize_phone_for_db(str(contact))
            except Exception:
                # best-effort fallback
                phone = f"whatsapp:{contact}" if not str(contact).startswith("whatsapp:") else contact

        # --- Read existing DB row for this razorpay_payment_id (if any) ---
        existing_map = None
        prev_status = None
        try:
            with get_conn() as conn, conn.cursor() as cur:
                cur.execute(
                    "SELECT razorpay_payment_id, status, phone FROM payments WHERE razorpay_payment_id = %s LIMIT 1",
                    (razorpay_payment_id,)
                )
                existing = cur.fetchone()  # may be tuple or mapping
                if existing:
                    if hasattr(existing, "get"):  # mapping-like (RealDictRow)
                        existing_map = dict(existing)
                    else:
                        # tuple-like: map columns by order selected above
                        # existing -> (razorpay_payment_id, status, phone)
                        existing_map = {
                            "razorpay_payment_id": existing[0] if len(existing) > 0 else None,
                            "status": existing[1] if len(existing) > 1 else None,
                            "phone": existing[2] if len(existing) > 2 else None
                        }
        except Exception as e:
            # don't fail the whole handler — log and continue
            print("handle_webhook_event: DB lookup failed:", e, traceback.format_exc())

        if existing_map:
            prev_status = (existing_map.get("status") or "").lower() if existing_map.get("status") else None
            # recover phone from DB if not present
            if not phone and existing_map.get("phone"):
                phone = existing_map.get("phone")

        # --- Upsert / record the payment in DB via your helper (idempotent) ---
        latest_status = latest_status_in_payload or None
        try:
            # record_payment may return tuple (id, status) or a dict or None
            rec = record_payment(
                phone=phone,
                razorpay_payment_id=razorpay_payment_id,
                amount=amount,
                currency=payment_entity.get("currency", "INR"),
                status=latest_status_in_payload
            )
            # Normalize the return
            if isinstance(rec, dict):
                latest_status = (rec.get("status") or latest_status_in_payload or "").lower()
            elif isinstance(rec, (list, tuple)) and len(rec) >= 2:
                latest_status = (rec[1] or latest_status_in_payload or "").lower()
            else:
                # if rec is scalar or None, fall back to payload
                latest_status = (latest_status_in_payload or latest_status or "").lower()
        except Exception as e:
            print("handle_webhook_event: record_payment failed:", e, traceback.format_exc())
            latest_status = (latest_status_in_payload or latest_status or "").lower()

        # --- Decide if we should activate subscription (transition to paid/captured) ---
        paid_states = {"captured", "paid", "authorized"}
        should_activate = False
        try:
            if latest_status in paid_states:
                # only activate if previous state was not a paid state
                if not prev_status or prev_status not in paid_states:
                    should_activate = True
        except Exception:
            should_activate = False

        activated = False
        activation_note = None
        if should_activate and phone:
            try:
                set_subscription_active(phone, days=30)
                activated = True
                activation_note = "subscription activated"
            except Exception as e:
                activation_note = f"activation failed: {e}"
                print("handle_webhook_event: set_subscription_active failed:", e, traceback.format_exc())

        # Return a clear summary for logging & tests
        return {
            "status": "ok",
            "event": event,
            "razorpay_payment_id": razorpay_payment_id,
            "prev_status": prev_status,
            "latest_status": latest_status,
            "activated": activated,
            "note": activation_note
        }

    except Exception as e:
        print("handle_webhook_event: unhandled exception:", e, traceback.format_exc())
        return {"status": "error", "error": str(e)}
