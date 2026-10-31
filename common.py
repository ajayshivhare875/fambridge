"""
FamPay Auto-Verification Gateway - shared logic
Used by bot.py (Telegram panels) and api.py (orders API + email poller).

How verification works (FamPay has no official API):
- Merchant connects their Gmail (app password) once.
- Background poller reads FamPay/FamApp payment confirmation emails via IMAP.
- Each email is parsed for amount + 12-digit UTR and matched to a pending
  order of the same merchant by exact amount. Match -> order marked paid.
"""
import os
import re
import sys
import json
import io
import base64
import secrets
import random
import urllib.parse
from datetime import datetime, timedelta

from cryptography.fernet import Fernet

import db_store  # Supabase/Postgres backend; falls back to files when DATABASE_URL is unset

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)

USERS_FILE = os.path.join(DATA_DIR, "users.json")
MERCHANTS_FILE = os.path.join(DATA_DIR, "merchants.json")
ORDERS_FILE = os.path.join(DATA_DIR, "orders.json")
ADMIN_FILE = os.path.join(DATA_DIR, "admin.json")
FERNET_KEY_FILE = os.path.join(DATA_DIR, "fernet.key")
PENDING_FILE = os.path.join(DATA_DIR, "pending.json")
SUBORDERS_FILE = os.path.join(DATA_DIR, "suborders.json")
SUB_ORDER_EXPIRY_MIN = 30

ADMIN_ID_ENV = os.getenv("ADMIN_ID", "").strip()
EMAIL_SENDERS = [s.strip().lower() for s in os.getenv("PAY_EMAIL_SENDERS", "fampay,famapp").split(",") if s.strip()]
ORDER_EXPIRY_MIN = int(os.getenv("ORDER_EXPIRY_MIN", "15"))
POLL_INTERVAL_SEC = int(os.getenv("POLL_INTERVAL_SEC", "25"))

# ---------- subscription plans (monthly quotas) ----------
PLANS = {
    "free": {"quota": 60, "price": 0},
    "starter": {"quota": 150, "price": 80},
    "best": {"quota": 250, "price": 150},
    "premium": {"quota": 500, "price": 250},
}
PLAN_ORDER = ["free", "starter", "best", "premium"]


# ---------- json storage (Supabase kv_store when DATABASE_URL is set, else files) ----------
_MISSING = object()


def _doc_key(path):
    return os.path.basename(path)


# Docs whose last load hit a DB error. While a doc is tainted, save_json
# REFUSES to write it to Supabase: our in-memory copy came from a failed
# read, and writing it would permanently wipe the real data. A later
# successful load_json clears the taint.
_tainted_docs = set()


def load_json(path, default):
    if db_store.db_enabled():
        val = db_store.db_load(_doc_key(path), _MISSING)
        if val is db_store._DB_ERROR:
            # DB unreachable: this is NOT "no data". Serve defaults so the
            # bot keeps responding, but taint the doc so no save can
            # overwrite Supabase from this empty read.
            print(f"[storage] DB read failed for {_doc_key(path)!r}: using defaults, "
                  f"Supabase writes paused for this doc", file=sys.stderr, flush=True)
            _tainted_docs.add(_doc_key(path))
            return default
        _tainted_docs.discard(_doc_key(path))
        if val is not _MISSING:
            return val
        # One-time seed: a local file left behind (e.g. VM backup) seeds the DB.
        if os.path.exists(path):
            try:
                with open(path, "r") as f:
                    file_data = json.load(f)
                db_store.db_save(_doc_key(path), file_data)
                return file_data
            except Exception:
                pass
        return default
    if os.path.exists(path):
        try:
            with open(path, "r") as f:
                return json.load(f)
        except Exception:
            return default
    return default


def save_json(path, data):
    """Save a doc. Returns True if it reached Supabase, False otherwise.
    A doc tainted by a failed read is NEVER written to Supabase: the
    in-memory copy may be empty defaults, and persisting it would wipe
    the real data (this is how deploys used to lose Gmail/API keys)."""
    doc = _doc_key(path)
    if db_store.db_enabled():
        if doc in _tainted_docs:
            print(f"[storage] REFUSING Supabase write for {doc!r}: last read failed; "
                  f"kept local file copy only", file=sys.stderr, flush=True)
        elif db_store.db_save(doc, data):
            return True
    # DB unset/unreachable/refused -> keep the file copy so nothing is ever lost.
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
    try:
        os.chmod(path, 0o600)
    except Exception:
        pass
    return False


# ---------- encryption for Gmail app passwords ----------
def _get_fernet():
    key = os.getenv("PAYMENTS_FERNET_KEY", "").strip()
    if key:
        return Fernet(key.encode())
    if db_store.db_enabled():
        db_key = db_store.db_get_fernet_key()
        if db_key is db_store._DB_ERROR:
            # DB unreachable: NEVER generate a fresh key and overwrite the
            # real one. Reuse the local key file when present, else fail
            # loudly so no secret is encrypted with a throwaway key.
            if os.path.exists(FERNET_KEY_FILE):
                return Fernet(open(FERNET_KEY_FILE, "rb").read().strip())
            raise RuntimeError("Fernet key unavailable: database unreachable and no local key file")
        if db_key:
            return Fernet(db_key)
    if os.path.exists(FERNET_KEY_FILE):
        file_key = open(FERNET_KEY_FILE, "rb").read().strip()
        if db_store.db_enabled():
            # Persist so future redeploys can still decrypt stored secrets.
            db_store.db_set_fernet_key(file_key)
        return Fernet(file_key)
    new_key = Fernet.generate_key()
    with open(FERNET_KEY_FILE, "wb") as f:
        f.write(new_key)
    try:
        os.chmod(FERNET_KEY_FILE, 0o600)
    except Exception:
        pass
    if db_store.db_enabled():
        db_store.db_set_fernet_key(new_key)
    return Fernet(new_key)


def fernet_encrypt(plain_text):
    return _get_fernet().encrypt(plain_text.encode()).decode()


def fernet_decrypt(token):
    return _get_fernet().decrypt(token.encode()).decode()


# ---------- admin ----------
def get_admin_id():
    if ADMIN_ID_ENV:
        return ADMIN_ID_ENV
    return str(load_json(ADMIN_FILE, {}).get("admin_id", ""))


def get_admin_settings():
    """Admin-level settings (e.g. sub_upi_id for subscription payments)."""
    return load_json(ADMIN_FILE, {})


def set_admin_setting(key, value):
    s = load_json(ADMIN_FILE, {})
    s[key] = value
    save_json(ADMIN_FILE, s)


def ensure_admin(user_id):
    if ADMIN_ID_ENV:
        return
    admin = load_json(ADMIN_FILE, {})
    if not admin.get("admin_id"):
        admin["admin_id"] = str(user_id)
        save_json(ADMIN_FILE, admin)


def is_admin(user_id):
    return str(user_id) == get_admin_id()


# ---------- merchant's own telegram bot ("Apna Bot Connect") ----------
def get_merchant_bot_token(merchant):
    """Decrypt and return merchant's own bot token, or '' if not connected."""
    enc = (merchant or {}).get("bot_token_enc", "")
    if not enc:
        return ""
    try:
        return fernet_decrypt(enc)
    except Exception:
        return ""


# ---------- pending user input state (disk-backed, survives restarts) ----------
def save_user_state(user_id, state):
    """Persist a user's in-progress input state (e.g. awaiting_bot_token)."""
    p = load_json(PENDING_FILE, {})
    if state:
        p[str(user_id)] = {"state": dict(state), "at": datetime.now().isoformat()}
    else:
        p.pop(str(user_id), None)
    save_json(PENDING_FILE, p)


def load_user_state(user_id):
    p = load_json(PENDING_FILE, {})
    st = p.get(str(user_id), {}).get("state", {})
    return dict(st) if isinstance(st, dict) else {}


def clear_user_state(user_id):
    save_user_state(user_id, {})


# ---------- security: SSRF guard for webhook URLs ----------
def is_safe_webhook_url(url):
    """True only if url is http(s) and resolves exclusively to public IPs.

    Blocks webhooks pointed at private/loopback/link-local addresses so a
    malicious merchant cannot make the server probe the internal network.
    """
    import socket
    import ipaddress
    try:
        p = urllib.parse.urlparse((url or "").strip())
        if p.scheme not in ("http", "https") or not p.hostname:
            return False
        for _fam, _typ, _proto, _canon, sockaddr in socket.getaddrinfo(p.hostname, None):
            ip = ipaddress.ip_address(sockaddr[0])
            if (ip.is_private or ip.is_loopback or ip.is_link_local
                    or ip.is_multicast or ip.is_reserved or ip.is_unspecified):
                return False
        return True
    except Exception:
        return False


# ---------- merchants (one API key per merchant) ----------
def get_merchant_by_owner(owner_id):
    merchants = load_json(MERCHANTS_FILE, {})
    for key, m in merchants.items():
        if str(m.get("owner_id")) == str(owner_id):
            return key, m
    return None, None


def create_merchant_key(owner_id, owner_name):
    merchants = load_json(MERCHANTS_FILE, {})
    for k in [k for k, m in merchants.items() if str(m.get("owner_id")) == str(owner_id)]:
        del merchants[k]
    api_key = "fp_" + secrets.token_urlsafe(24)
    merchants[api_key] = {
        "owner_id": str(owner_id),
        "owner_name": owner_name,
        "upi_id": "",
        "gmail_email": "",
        "gmail_apppass_enc": "",
        "webhook_url": "",
        "bot_token_enc": "",
        "bot_username": "",
        "plan": "free",
        "quota_used": 0,
        "quota_period": datetime.now().strftime("%Y-%m"),
        "last_uid": 0,
        "created_at": datetime.now().isoformat(),
    }
    save_json(MERCHANTS_FILE, merchants)
    return api_key


def revoke_merchant_key(owner_id):
    merchants = load_json(MERCHANTS_FILE, {})
    removed = False
    for k in [k for k, m in merchants.items() if str(m.get("owner_id")) == str(owner_id)]:
        del merchants[k]
        removed = True
    if removed:
        save_json(MERCHANTS_FILE, merchants)
    return removed


# ---------- subscription plans & quota ----------
def _quota_period():
    return datetime.now().strftime("%Y-%m")


def get_quota(api_key):
    """Returns (plan, used, limit) for a merchant. Counter resets every month."""
    merchants = load_json(MERCHANTS_FILE, {})
    m = merchants.get(api_key)
    if not m:
        return "free", 0, PLANS["free"]["quota"]
    period = _quota_period()
    if m.get("quota_period") != period or "plan" not in m:
        m["quota_period"] = period
        m["quota_used"] = 0
        m.setdefault("plan", "free")
        merchants[api_key] = m
        save_json(MERCHANTS_FILE, merchants)
    plan = m.get("plan", "free")
    if plan not in PLANS:
        plan = "free"
    used = int(m.get("quota_used") or 0)
    return plan, used, PLANS[plan]["quota"]


def check_quota(api_key):
    """True if the merchant may create another order. Returns (ok, error_msg)."""
    plan, used, limit = get_quota(api_key)
    if used >= limit:
        return False, (f"{plan.upper()} plan limit over ({limit} payment requests/month). "
                       "Upgrade for more — contact @Magvenomsuper.")
    return True, ""


def bump_quota(api_key):
    merchants = load_json(MERCHANTS_FILE, {})
    m = merchants.get(api_key)
    if not m:
        return
    period = _quota_period()
    if m.get("quota_period") != period:
        m["quota_period"] = period
        m["quota_used"] = 0
    m["quota_used"] = int(m.get("quota_used") or 0) + 1
    merchants[api_key] = m
    save_json(MERCHANTS_FILE, merchants)


def set_plan(api_key, plan):
    merchants = load_json(MERCHANTS_FILE, {})
    m = merchants.get(api_key)
    if not m or plan not in PLANS:
        return False
    m["plan"] = plan
    merchants[api_key] = m
    save_json(MERCHANTS_FILE, merchants)
    return True


def cycle_plan(plan):
    """Next plan in upgrade order (wraps free <- premium)."""
    if plan in PLAN_ORDER:
        return PLAN_ORDER[(PLAN_ORDER.index(plan) + 1) % len(PLAN_ORDER)]
    return "free"


def plan_summary(plan):
    """Short human line: 'STARTER (150 requests/month, ₹80)'."""
    p = PLANS.get(plan, PLANS["free"])
    return f"{plan.upper()} ({p['quota']} requests/month, ₹{p['price']})"


# ---------- subscription auto-purchase (sub-orders) ----------
def _admin_merchant_with_gmail():
    """The admin's own merchant record (needed for auto-verifying plan payments)."""
    merchants = load_json(MERCHANTS_FILE, {})
    for k, m in merchants.items():
        if str(m.get("owner_id")) == get_admin_id() and m.get("gmail_email"):
            return k, m
    return None, None


def create_sub_order(buyer_key, buyer_id, buyer_name, plan):
    """Create a subscription purchase order. Returns (suborder, error)."""
    merchants = load_json(MERCHANTS_FILE, {})
    m = merchants.get(buyer_key)
    if not m:
        return None, "Create an API key first (User Panel > My API Key)."
    if plan not in PLANS or plan == "free":
        return None, "Invalid plan."
    cur = m.get("plan", "free")
    if PLAN_ORDER.index(plan) <= PLAN_ORDER.index(cur):
        return None, f"You are already on {cur.upper()} or higher."
    sub_upi = get_admin_settings().get("sub_upi_id", "")
    if not sub_upi:
        return None, "Subscriptions are not set up yet. Contact @Magvenomsuper."
    admin_key, admin_m = _admin_merchant_with_gmail()
    if not admin_m:
        return None, "Auto-verification is not ready yet. Contact @Magvenomsuper."
    price = PLANS[plan]["price"]
    sub_id = "sub_" + secrets.token_hex(6)
    now = datetime.now()
    upi_link = make_upi_link(sub_upi, "Gateway Admin", price, sub_id, f"plan-{plan}")
    sub = {
        "sub_id": sub_id,
        "buyer_key": buyer_key,
        "buyer_id": str(buyer_id),
        "buyer_name": buyer_name or "",
        "admin_key": admin_key,
        "plan": plan,
        "amount_paise": price * 100,
        "upi_link": upi_link,
        "status": "pending",
        "created_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=SUB_ORDER_EXPIRY_MIN)).isoformat(),
        "paid_at": "",
        "utr": "",
    }
    subs = load_json(SUBORDERS_FILE, {})
    subs[sub_id] = sub
    save_json(SUBORDERS_FILE, subs)
    return sub, ""


def get_pending_sub_order(buyer_key):
    subs = load_json(SUBORDERS_FILE, {})
    now = datetime.now()
    for s in subs.values():
        if s.get("buyer_key") == buyer_key and s.get("status") == "pending":
            try:
                if datetime.fromisoformat(s["expires_at"]) > now:
                    return s
            except Exception:
                pass
    return None


def match_sub_order(admin_api_key, amount_paise, utr):
    """Match a pending subscription payment by exact amount. Upgrades the buyer."""
    subs = load_json(SUBORDERS_FILE, {})
    now = datetime.now()
    cands = []
    for s in subs.values():
        if s.get("status") != "pending":
            continue
        if s.get("admin_key") != admin_api_key:
            continue
        if s.get("amount_paise") != amount_paise:
            continue
        try:
            if datetime.fromisoformat(s["expires_at"]) < now:
                continue
        except Exception:
            continue
        cands.append(s)
    if not cands:
        return None
    cands.sort(key=lambda s: s["created_at"])
    s = cands[0]
    s["status"] = "paid"
    s["paid_at"] = now.isoformat()
    s["utr"] = utr or ""
    subs[s["sub_id"]] = s
    save_json(SUBORDERS_FILE, subs)
    set_plan(s["buyer_key"], s["plan"])
    return s


def expire_sub_orders():
    subs = load_json(SUBORDERS_FILE, {})
    now = datetime.now()
    changed = False
    for s in subs.values():
        if s.get("status") == "pending":
            try:
                if datetime.fromisoformat(s["expires_at"]) < now:
                    s["status"] = "expired"
                    changed = True
            except Exception:
                pass
    # Auto-delete sub-orders older than 30 days
    cutoff = now - timedelta(days=30)
    old_ids = [sid for sid, s in subs.items()
               if _safe_old(s.get("created_at", ""), cutoff)]
    for sid in old_ids:
        del subs[sid]
        changed = True
    if changed:
        save_json(SUBORDERS_FILE, subs)


def _safe_old(created_at_str, cutoff):
    try:
        return datetime.fromisoformat(created_at_str) < cutoff
    except Exception:
        return False


# ---------- UPI ----------
def make_upi_link(upi_id, payee_name, amount_rupees, order_id, note=""):
    params = {
        "pa": upi_id,
        "pn": payee_name or "Merchant",
        "am": f"{amount_rupees:.2f}",
        "cu": "INR",
        "tn": order_id,
    }
    if note:
        params["tn"] = f"{order_id} {note}"[:80]
    return "upi://pay?" + urllib.parse.urlencode(params)


def make_qr_png_bytes(text):
    import qrcode
    img = qrcode.make(text)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def make_qr_base64(text):
    return base64.b64encode(make_qr_png_bytes(text)).decode()


# ---------- orders ----------
def _unique_amount_paise(api_key, base_paise, orders):
    """QR amount for a new order. Uses the EXACT requested amount when no
    other pending order from this merchant has the same amount (clean UX).
    Only when a collision risk exists (another pending order with the same
    amount) it subtracts the SMALLEST possible paise (1, then 2, ...) until
    unique -- so the customer pays minimally less, never a random cut."""
    pending_amounts = {
        o.get("amount_paise") for o in orders.values()
        if o.get("api_key") == api_key and o.get("status") == "pending"
    }
    # No collision: exact amount, no reduction
    if base_paise not in pending_amounts:
        return base_paise
    # Collision risk: smallest reduction that stays unique (1 paisa first)
    for reduction in range(1, 51):
        candidate = base_paise - reduction
        if candidate > 0 and candidate not in pending_amounts:
            return candidate
    return base_paise  # fallback: extremely rare


def create_order(api_key, amount_rupees, note=""):
    merchants = load_json(MERCHANTS_FILE, {})
    m = merchants.get(api_key)
    if not m:
        return None, "Invalid API key"
    if not m.get("upi_id"):
        return None, "UPI ID not set. Set it in the bot: User Panel > Set UPI ID."
    ok, qerr = check_quota(api_key)
    if not ok:
        return None, qerr
    try:
        amount = float(amount_rupees)
    except Exception:
        return None, "Amount must be a number."
    if amount <= 0 or amount > 100000:
        return None, "Amount must be more than 0 and less than 100000."
    note = (note or "")[:200]
    amount_paise = int(round(amount * 100))
    order_id = "ord_" + secrets.token_hex(6)
    now = datetime.now()
    orders = load_json(ORDERS_FILE, {})
    # Unique QR amount: slightly less, unique per pending order
    qr_paise = _unique_amount_paise(api_key, amount_paise, orders)
    qr_amount = qr_paise / 100
    upi_link = make_upi_link(m["upi_id"], m.get("owner_name", "Merchant"), qr_amount, order_id, note)
    order = {
        "order_id": order_id,
        "api_key": api_key,
        "merchant_id": str(m.get("owner_id")),
        "amount_paise": qr_paise,
        "requested_amount_paise": amount_paise,
        "note": note or "",
        "upi_link": upi_link,
        "status": "pending",
        "created_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=ORDER_EXPIRY_MIN)).isoformat(),
        "paid_at": "",
        "utr": "",
    }
    orders[order_id] = order
    save_json(ORDERS_FILE, orders)
    bump_quota(api_key)
    return order, ""


def get_order(api_key, order_id):
    orders = load_json(ORDERS_FILE, {})
    o = orders.get(order_id)
    if not o or o.get("api_key") != api_key:
        return None
    return o


def mark_order_paid(order_id, utr=""):
    orders = load_json(ORDERS_FILE, {})
    o = orders.get(order_id)
    if not o or o.get("status") != "pending":
        return None
    o["status"] = "paid"
    o["paid_at"] = datetime.now().isoformat()
    o["utr"] = utr or ""
    save_json(ORDERS_FILE, orders)
    return o


def expire_old_orders():
    orders = load_json(ORDERS_FILE, {})
    now = datetime.now()
    changed = False
    for o in orders.values():
        if o.get("status") == "pending":
            try:
                if datetime.fromisoformat(o["expires_at"]) < now:
                    o["status"] = "expired"
                    changed = True
            except Exception:
                pass
    # Auto-delete orders older than 30 days (keeps DB small, enough for disputes)
    cutoff = now - timedelta(days=30)
    old_ids = []
    for oid, o in orders.items():
        try:
            if datetime.fromisoformat(o.get("created_at", "")) < cutoff:
                old_ids.append(oid)
        except Exception:
            pass
    for oid in old_ids:
        del orders[oid]
        changed = True
    if old_ids:
        print(f"[cleanup] deleted {len(old_ids)} orders older than 30 days", flush=True)
    if changed:
        save_json(ORDERS_FILE, orders)


# ---------- email parsing ----------
def sender_match(text):
    t = text.lower()
    return any(s in t for s in EMAIL_SENDERS)


def parse_payment_email(body):
    """
    Returns list of (amount_paise, utr). UTR = 12-digit number.
    Amount = first ₹/Rs/INR amount found (payment emails state it clearly).
    """
    results = []
    utrs = re.findall(r"\b\d{12}\b", body)
    amts = re.findall(r"(?:₹|Rs\.?|INR)\s*([\d,]+(?:\.\d{1,2})?)", body)
    if not amts:
        return results
    try:
        amount_paise = int(round(float(amts[0].replace(",", "")) * 100))
    except Exception:
        return results
    if utrs:
        for u in utrs:
            results.append((amount_paise, u))
    else:
        results.append((amount_paise, ""))
    return results


def get_email_body(msg):
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and "attachment" not in str(part.get("Content-Disposition", "")):
                try:
                    return part.get_payload(decode=True).decode(errors="ignore")
                except Exception:
                    continue
    else:
        try:
            return msg.get_payload(decode=True).decode(errors="ignore")
        except Exception:
            return ""
    return ""
