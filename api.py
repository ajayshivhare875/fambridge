"""
FamPay Auto-Verification Gateway - API + background email poller
- POST /api/create_order  -> UPI QR/link for an amount
- GET  /api/order_status  -> pending / paid / expired
- Poller thread reads merchant Gmail (IMAP) for FamPay payment emails,
  matches amount -> marks order paid -> fires webhook -> notifies merchant on Telegram.
"""
import os
import json
import time
import hmac
import hashlib
import imaplib
import email
import threading
from datetime import datetime, timedelta

import requests
from flask import Flask, request, jsonify
from dotenv import load_dotenv

import common as C
import legal as L

load_dotenv()

app = Flask(__name__)
BOT_TOKEN = os.getenv("PAYBOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")


def _get_api_key():
    data = request.get_json(force=True, silent=True) or {}
    return data.get("api_key") or request.headers.get("X-API-Key") or request.args.get("api_key")


def _valid_merchant(api_key):
    merchants = C.load_json(C.MERCHANTS_FILE, {})
    return merchants.get(api_key)


# ---------- simple in-memory rate limiting ----------
_RATE_BUCKETS = {}

def _rate_ok(bucket, limit, window_sec):
    """Sliding-window rate limiter (per API key / per IP). Single-process safe."""
    now = time.time()
    hits = [t for t in _RATE_BUCKETS.get(bucket, []) if now - t < window_sec]
    if len(hits) >= limit:
        _RATE_BUCKETS[bucket] = hits
        return False
    hits.append(now)
    _RATE_BUCKETS[bucket] = hits
    return True


def _ip_limited():
    """Global per-IP flood guard. Returns an error response or None."""
    ip = request.remote_addr or "unknown"
    if not _rate_ok(f"ip:{ip}", 300, 60):
        return jsonify({"success": False, "error": "Rate limit exceeded."}), 429
    return None


@app.after_request
def _cors_headers(response):
    # Allow browser-based integrations (demo pages, merchant websites)
    # to call the API directly.
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, X-API-Key"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return response


@app.route("/api/create_order", methods=["POST", "OPTIONS"])
def api_create_order():
    if request.method == "OPTIONS":
        return "", 204
    limited = _ip_limited()
    if limited:
        return limited
    data = request.get_json(force=True, silent=True) or {}
    api_key = _get_api_key()
    if not _valid_merchant(api_key):
        return jsonify({"success": False, "error": "Invalid API key"}), 401
    if not _rate_ok(f"create:{api_key}", 60, 60):
        return jsonify({"success": False, "error": "Rate limit exceeded. Slow down."}), 429
    order, err = C.create_order(api_key, data.get("amount"), data.get("note", ""))
    if not order:
        return jsonify({"success": False, "error": err}), 400
    merchant = _valid_merchant(api_key)
    if merchant:
        notify_new_order(merchant, order)
    return jsonify({
        "success": True,
        "order_id": order["order_id"],
        "upi_link": order["upi_link"],
        "qr_base64": C.make_qr_base64(order["upi_link"]),
        "amount_paise": order["amount_paise"],
        "expires_in_sec": C.ORDER_EXPIRY_MIN * 60,
    })


@app.route("/api/order_status", methods=["GET", "OPTIONS"])
def api_order_status():
    if request.method == "OPTIONS":
        return "", 204
    limited = _ip_limited()
    if limited:
        return limited
    api_key = _get_api_key()
    order_id = request.args.get("order_id", "")
    if not _valid_merchant(api_key):
        return jsonify({"success": False, "error": "Invalid API key"}), 401
    if not _rate_ok(f"status:{api_key}", 600, 60):
        return jsonify({"success": False, "error": "Rate limit exceeded. Slow down."}), 429
    o = C.get_order(api_key, order_id)
    if not o:
        return jsonify({"success": False, "error": "Order not found"}), 404
    return jsonify({
        "success": True,
        "order_id": o["order_id"],
        "status": o["status"],
        "amount_paise": o["amount_paise"],
        "paid_at": o.get("paid_at", ""),
        "utr": o.get("utr", ""),
    })


@app.route("/")
def index():
    return jsonify({"service": "FamPay Auto-Verification Gateway",
                    "endpoints": ["/api/create_order", "/api/order_status",
                                  "/terms", "/privacy"]})


@app.route("/debug/telegram")
def debug_telegram():
    # Temporary diagnostic: tests Telegram API reachability + token validity.
    # Returns status only; never exposes the token.
    import urllib.request, json as _json
    token = os.getenv("PAYBOT_TOKEN", "")
    if not token:
        return jsonify({"ok": False, "error": "PAYBOT_TOKEN not set"}), 500
    try:
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/getMe", method="GET")
        with urllib.request.urlopen(req, timeout=15) as r:
            data = _json.loads(r.read().decode())
        if data.get("ok"):
            res = data["result"]
            return jsonify({"ok": True,
                            "username": res.get("username"),
                            "bot_id": res.get("id")})
        return jsonify({"ok": False, "error": "getMe not ok",
                        "detail": str(data)[:200]}), 502
    except Exception as e:
        return jsonify({"ok": False, "error": type(e).__name__,
                        "detail": str(e)[:300]}), 502


@app.route("/debug/polling")
def debug_polling():
    # Temporary diagnostic: reports the Telegram polling loop state.
    try:
        import bot as _botmod
        return jsonify({"ok": True,
                        "poll_status": _botmod.POLL_STATUS})
    except Exception as e:
        return jsonify({"ok": False, "error": type(e).__name__,
                        "detail": str(e)[:200]}), 500


@app.route("/terms")
def terms():
    return L.terms_html()


@app.route("/privacy")
def privacy():
    return L.privacy_html()


# ---------- webhook + telegram notify ----------
def fire_webhook(merchant, order):
    url = (merchant.get("webhook_url") or "").strip()
    if not url:
        return
    if not C.is_safe_webhook_url(url):
        print(f"webhook blocked (unsafe URL): {url[:60]}")
        return
    payload = {
        "event": "payment.verified",
        "order_id": order["order_id"],
        "amount_paise": order["amount_paise"],
        "utr": order.get("utr", ""),
        "paid_at": order.get("paid_at", ""),
        "note": order.get("note", ""),
    }
    sig = hmac.new(merchant_api_key(merchant, order).encode(),
                   json.dumps(payload, sort_keys=True).encode(),
                   hashlib.sha256).hexdigest()
    try:
        requests.post(url, json=payload,
                      headers={"X-Signature": sig, "Content-Type": "application/json"},
                      timeout=10)
    except Exception as e:
        print(f"webhook failed: {type(e).__name__}")


def merchant_api_key(merchant, order):
    return order.get("api_key", "")


def notify_merchant_telegram(merchant, order):
    chat_id = merchant.get("owner_id")
    if not chat_id:
        return
    # If the merchant connected their own bot, the notification goes from it (white-label),
    # otherwise from the gateway bot.
    token = C.get_merchant_bot_token(merchant) or BOT_TOKEN
    if not token:
        return
    text = (f"✅ Payment received!\n\nAmount: ₹{order['amount_paise']/100:.2f}\n"
            f"Order: {order['order_id']}\nUTR: {order.get('utr') or '-'}")
    old = {}
    for k in ("no_proxy", "NO_PROXY"):
        old[k] = os.environ.pop(k, None)
    try:
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      json={"chat_id": chat_id, "text": text}, timeout=15)
    except Exception:
        pass
    finally:
        for k, v in old.items():
            if v is not None:
                os.environ[k] = v


def notify_new_order(merchant, order):
    """Tell the merchant a new order/request was created via their API key."""
    chat_id = merchant.get("owner_id")
    if not chat_id:
        return
    token = C.get_merchant_bot_token(merchant) or BOT_TOKEN
    if not token:
        return
    note = (order.get("note") or "").strip()
    lines = [f"🆕 New request!\n",
             f"Order: {order['order_id']}",
             f"Amount: ₹{order['amount_paise']/100:.2f}"]
    if note:
        lines.append(f"Note: {note[:100]}")
    lines.append(f"Time: {order.get('created_at', '')[:16].replace('T', ' ')}")
    text = "\n".join(lines)
    old = {}
    for k in ("no_proxy", "NO_PROXY"):
        old[k] = os.environ.pop(k, None)
    try:
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      json={"chat_id": chat_id, "text": text}, timeout=15)
    except Exception:
        pass
    finally:
        for k, v in old.items():
            if v is not None:
                os.environ[k] = v


# ---------- admin alerts ----------
_ALERT_LAST = {}  # key -> timestamp of last sent alert (rate limiting)


def alert_admin(text, key="generic", min_interval_sec=3600):
    """Send an alert to the admin via Telegram. Rate-limited per key
    (default: at most once per hour) to avoid spam."""
    import time
    now = time.time()
    if now - _ALERT_LAST.get(key, 0) < min_interval_sec:
        return
    _ALERT_LAST[key] = now
    admin_id = C.get_admin_id()
    if not admin_id or not BOT_TOKEN:
        return
    old = {}
    for k in ("no_proxy", "NO_PROXY"):
        old[k] = os.environ.pop(k, None)
    try:
        requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                      json={"chat_id": int(admin_id),
                            "text": f"⚠️ FamBridge Alert\n\n{text}"},
                      timeout=15)
    except Exception:
        pass
    finally:
        for k, v in old.items():
            if v is not None:
                os.environ[k] = v


def notify_sub_paid(sub, admin_merchant):
    """Tell the buyer their plan is live + tell the admin about the sale."""
    buyer_id = sub.get("buyer_id")
    plan = sub.get("plan")
    summary = C.plan_summary(plan)
    old = {}
    for k in ("no_proxy", "NO_PROXY"):
        old[k] = os.environ.pop(k, None)
    try:
        if buyer_id:
            requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                          json={"chat_id": int(buyer_id),
                                "text": f"💳 Subscription activated!\n\nYour plan is now {summary}.\nEnjoy! 🎉"},
                          timeout=15)
        admin_id = admin_merchant.get("owner_id")
        if admin_id:
            requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                          json={"chat_id": int(admin_id),
                                "text": f"💰 New subscription!\n\n{sub.get('buyer_name') or 'A merchant'} "
                                       f"upgraded to {summary}.\nAmount: ₹{sub['amount_paise'] // 100}"},
                          timeout=15)
    except Exception:
        pass
    finally:
        for k, v in old.items():
            if v is not None:
                os.environ[k] = v


# ---------- matching ----------
def match_and_mark(api_key, merchant, amount_paise, utr):
    orders = C.load_json(C.ORDERS_FILE, {})
    now = datetime.now()
    candidates = []
    for o in orders.values():
        if o.get("api_key") != api_key or o.get("status") != "pending":
            continue
        if o.get("amount_paise") != amount_paise:
            continue
        try:
            created = datetime.fromisoformat(o["created_at"])
        except Exception:
            continue
        if now - created > timedelta(minutes=45):
            continue
        candidates.append(o)
    if not candidates:
        return None
    candidates.sort(key=lambda o: o["created_at"])
    order = C.mark_order_paid(candidates[0]["order_id"], utr)
    if order:
        fire_webhook(merchant, order)
        notify_merchant_telegram(merchant, order)
    return order


# ---------- IMAP poller ----------
def poll_gmail_once():
    merchants = C.load_json(C.MERCHANTS_FILE, {})
    dirty = False
    for api_key, m in merchants.items():
        email_addr = (m.get("gmail_email") or "").strip()
        enc = m.get("gmail_apppass_enc") or ""
        if not email_addr or not enc:
            continue
        try:
            app_pass = C.fernet_decrypt(enc)
        except Exception:
            print(f"decrypt failed for merchant {m.get('owner_name')}")
            continue
        try:
            mail = imaplib.IMAP4_SSL("imap.gmail.com")
            mail.login(email_addr, app_pass)
            mail.select("INBOX")
            last_uid = int(m.get("last_uid") or 0)
            since = (datetime.now() - timedelta(days=2)).strftime("%d-%b-%Y")
            typ, data = mail.uid("search", None, f"(SINCE {since})")
            uids = [int(x) for x in data[0].split()] if data and data[0] else []
            max_uid = last_uid
            for uid in sorted(uids):
                if uid <= last_uid:
                    continue
                max_uid = max(max_uid, uid)
                typ, msg_data = mail.uid("fetch", str(uid), "(BODY[])")
                if not msg_data or not msg_data[0]:
                    continue
                msg = email.message_from_bytes(msg_data[0][1])
                frm = str(msg.get("From", ""))
                subj = str(msg.get("Subject", ""))
                if not C.sender_match(frm + " " + subj):
                    continue
                body = C.get_email_body(msg)
                parsed = list(C.parse_payment_email(frm + "\n" + subj + "\n" + body))
                if not parsed:
                    # FamPay email arrived but we couldn't parse amount/UTR.
                    # Email format may have changed — alert the admin!
                    alert_admin(
                        f"Email parse FAILED!\n\n"
                        f"From: {frm[:80]}\n"
                        f"Subject: {subj[:80]}\n"
                        f"Merchant: {m.get('owner_name', '?')} ({email_addr})\n\n"
                        f"FamPay may have changed their email format. "
                        f"Check and update the parser!",
                        key=f"parse_fail_{api_key}",
                        min_interval_sec=3600)
                for amount_paise, utr in parsed:
                    matched = match_and_mark(api_key, m, amount_paise, utr)
                    # Subscription auto-upgrade: only against the admin's own Gmail,
                    # and only if no regular customer order claimed this payment.
                    if not matched and str(m.get("owner_id")) == C.get_admin_id():
                        sub = C.match_sub_order(api_key, amount_paise, utr)
                        if sub:
                            notify_sub_paid(sub, m)
            if max_uid != last_uid:
                m["last_uid"] = max_uid
                dirty = True
            mail.logout()
        except imaplib.IMAP4.error as e:
            print(f"IMAP login/fetch failed for {email_addr}: {e}")
            alert_admin(
                f"Gmail connection FAILED!\n\n"
                f"Email: {email_addr}\n"
                f"Merchant: {m.get('owner_name', '?')}\n"
                f"Error: {str(e)[:100]}\n\n"
                f"Payment verification is STOPPED for this merchant. "
                f"They may need to reconnect Gmail (App Password expired?)",
                key=f"imap_fail_{api_key}",
                min_interval_sec=7200)
        except Exception as e:
            print(f"poll error: {type(e).__name__}: {e}")
    if dirty:
        C.save_json(C.MERCHANTS_FILE, merchants)
    C.expire_old_orders()
    C.expire_sub_orders()


def poller_loop():
    print(f"📧 Email poller started (every {C.POLL_INTERVAL_SEC}s)")
    while True:
        try:
            poll_gmail_once()
        except Exception as e:
            print(f"poller loop error: {e}")
        time.sleep(C.POLL_INTERVAL_SEC)


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    t = threading.Thread(target=poller_loop, daemon=True)
    t.start()
    # To run bot + API in a single process on free hosts like Render:
    # set RUN_BOT=1 in env.
    # NOTE: the Telegram bot MUST run in the main thread. PTB's run_polling()
    # uses asyncio signal handlers (loop.set_wakeup_fd), which raise
    # "RuntimeError: set_wakeup_fd only works in main thread" in any other
    # thread -> infinite crash loop. So Flask goes to a background thread
    # and the bot takes the main thread.
    if os.getenv("RUN_BOT", "0") == "1":
        ft = threading.Thread(
            target=lambda: app.run(host="0.0.0.0", port=port,
                                   debug=False, use_reloader=False),
            daemon=True, name="flask-api")
        ft.start()
        print(f"🚀 FamPay Gateway API on http://localhost:{port} (background thread)")
        import bot as botmod
        print("🤖 Telegram bot starting in main thread (RUN_BOT=1)")
        botmod.main()
    else:
        print(f"🚀 FamPay Gateway API on http://localhost:{port}")
        app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
