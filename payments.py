# payments.py
import os
import razorpay
import hmac
import hashlib
import base64
import json
from datetime import datetime

from db import record_payment, set_subscription_active, save_user, get_or_create_user
from db import get_user  # optional for extra checks

RAZORPAY_KEY_ID = os.getenv("RAZORPAY_KEY_ID")
RAZORPAY_KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET")
RAZORPAY_WEBHOOK_SECRET = os.getenv("RAZORPAY_WEBHOOK_SECRET")
PLATFORM_URL = os.getenv("PLATFORM_URL")  # e.g. https://mina-mom-agent.onrender.com

# Create client
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
    Create a Razorpay Payment Link and return a dict with {short_url, id, amount}.
    amount_in_rupees: integer rupees (e.g. 499)
    phone: user's whatsapp phone string (like 'whatsapp:+919999...')
    """
    client = get_client()

    amount_paise = int(amount_in_rupees * 100)
    # keep a unique reference id for linking back. Use phone and timestamp
    ref_id = f"{phone.replace('whatsapp:', '').replace('+','')}-{int(datetime.utcnow().timestamp())}"
    payload = {
        "amount": amount_paise,
        "currency": "INR",
        "accept_partial": False,
        "reference_id": ref_id,
        "description": purpose,
        "customer": {
            # Razorpay customer fields: name/email/phone optional. We'll use phone (without whatsapp:)
            "contact": phone.replace("whatsapp:", "")
        },
        "notify": { "sms": False, "email": False },  # we will send link via WhatsApp
        "reminder_enable": True,
        # optional: "callback_url": f"{PLATFORM_URL}/razorpay-return", "callback_method": "get"
    }

    link = client.payment_link.create(payload)
    # example response: link["id"], link["short_url"], link["status"], link["amount"]
    # Persist a 'pending' record in payments table (use record_payment or raw SQL)
    # We'll store razorpay_payment_id = link["id"], amount in paise, status = 'created'
    # record_payment(phone, link["id"], link["amount"], "INR", "created")
    record_payment(phone=phone, razorpay_payment_id=link.get("id"), amount=link.get("amount"), currency=link.get("currency", "INR"), status=link.get("status", "created"))
    return {
        "id": link.get("id"),
        "short_url": link.get("short_url"),
        "amount": link.get("amount"),
        "status": link.get("status"),
        "reference_id": link.get("reference_id")
    }

def verify_razorpay_webhook(payload_body: bytes, header_signature: str) -> bool:
    """
    Verify Razorpay webhook signature using HMAC SHA256 and the webhook secret.
    Razorpay docs: signature = base64(HMAC_SHA256(body, webhook_secret))
    """
    if not RAZORPAY_WEBHOOK_SECRET:
        # no secret configured - fail safe
        return False
    computed_hmac = hmac.new(RAZORPAY_WEBHOOK_SECRET.encode("utf-8"), payload_body, hashlib.sha256).digest()
    computed_signature = base64.b64encode(computed_hmac).decode()
    # header_signature typically contains the signature string
    return hmac.compare_digest(computed_signature, header_signature)

def handle_webhook_event(event_json: dict):
    """
    Called after webhook signature verified. Update DB accordingly.
    We expect events like 'payment_link.paid' or 'payment.captured' etc.
    """
    event = event_json.get("event")
    data = event_json.get("payload", {})

    # Payment link paid event
    if event == "payment_link.paid" or event == "payment_link.payment_paid":
        # payload structure: payload.payment.entity or payload.payment_link.entity
        payment_entity = data.get("payment", {}).get("entity")
        link_entity = data.get("payment_link", {}).get("entity") or data.get("payment_link", {}).get("entity")
        # Fallback: search inside payload for payment and link
        if not payment_entity:
            # try alternative nesting
            for key in data:
                if isinstance(data[key], dict) and data[key].get("entity") and data[key]["entity"].get("status"):
                    payment_entity = data[key]["entity"]
                    break

        # Safely extract basic fields
        if payment_entity:
            razorpay_payment_id = payment_entity.get("id")
            amount = payment_entity.get("amount")  # paise
            status = payment_entity.get("status")  # "captured"
            contact = payment_entity.get("contact") or payment_entity.get("customer_id")
            # We stored user phone in payments table earlier using reference_id; but we also recorded by phone when creating link.
            # To be safe, check payments table for payment id -> get phone. We'll record by phone if found.
            # Update payments table
            # record_payment(phone, razorpay_payment_id, amount, "INR", status)
            # But we need phone: try to find by looking up payments row matching razorpay_payment_id
            # In db.py we recorded payment rows with phone, so get_user_by_payment function could be added; for now,
            # we'll try to find the phone by scanning payments table via db functions - we will open a DB cursor here.
            from db import get_conn
            phone = None
            with get_conn() as conn, conn.cursor() as cur:
                cur.execute("SELECT phone FROM payments WHERE razorpay_payment_id=%s LIMIT 1", (razorpay_payment_id,))
                row = cur.fetchone()
                if row:
                    phone = row[0]
            if not phone:
                # As fallback try contact: assume it's mobile number e.g. '919xxxx...'
                if contact:
                    phone = "whatsapp:" + str(contact)
            if phone:
                # record payment status
                record_payment(phone=phone, razorpay_payment_id=razorpay_payment_id, amount=amount, currency="INR", status=status)
                # if captured or paid -> enable subscription for user
                if status in ("captured", "authorized", "paid"):
                    set_subscription_active(phone, days=30)
                    # Optionally update user's razorpay_customer_id
                    user = get_or_create_user(phone)
                    user["razorpay_customer_id"] = payment_entity.get("customer_id") or user.get("razorpay_customer_id")
                    save_user(user)
                    return {"phone": phone, "status": "activated"}
    # handle other event types if needed
    return {"status": "ignored", "event": event}
