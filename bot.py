"""
FamPay Auto-Verification Gateway - Telegram Bot
User Panel: connect Gmail, set UPI ID, API key, webhook, guide, test order, stats
Admin Panel: stats, merchants, orders, broadcast
Plain-text messages only (no Markdown) to avoid parse errors.
"""
import os
import io
import json
import time
import asyncio
import imaplib
import urllib.request
from datetime import datetime

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters,
)
from dotenv import load_dotenv

import common as C
import starter_code as SC
import legal as L

load_dotenv()
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")
BOT_TOKEN = os.getenv("PAYBOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")


# ---------- force join ----------
FORCE_JOIN_CHANNEL = "@magservicesbots"
FORCE_JOIN_URL = "https://t.me/magservicesbots"


# Cache channel membership: user_id -> (is_member, timestamp).
# is_channel_member() does a BLOCKING urllib call — calling it directly from
# async handlers freezes the whole bot event loop (this was making every
# button press feel slow). The cached async wrapper below runs it in a thread
# and caches results for 5 minutes.
_member_cache = {}
MEMBER_CACHE_TTL = 300  # seconds


def is_channel_member(user_id):
    """Check via getChatMember if the user joined the force-join channel.
    Returns True on any API error (fail open) so a misconfigured channel
    never locks everyone out. The bot must be an admin of the channel.
    NOTE: blocking call — always use is_channel_member_cached() from async code."""
    try:
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{BOT_TOKEN}/getChatMember",
            data=json.dumps({"chat_id": FORCE_JOIN_CHANNEL,
                             "user_id": user_id}).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            d = json.load(r)
        if d.get("ok"):
            return d["result"].get("status", "") in (
                "creator", "administrator", "member", "restricted")
        return True
    except Exception:
        return True


async def is_channel_member_cached(user_id):
    """Async wrapper: runs the blocking check in a thread + 5-min cache.
    Use this from all async handlers instead of is_channel_member()."""
    now = time.time()
    hit = _member_cache.get(user_id)
    if hit and now - hit[1] < MEMBER_CACHE_TTL:
        return hit[0]
    result = await asyncio.to_thread(is_channel_member, user_id)
    _member_cache[user_id] = (result, now)
    return result


def force_join_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 Join Channel", url=FORCE_JOIN_URL)],
        [InlineKeyboardButton("✅ I've Joined", callback_data="check_joined")],
    ])


async def require_channel_member(update, context):
    """True if the user may proceed, False if they were shown the join prompt."""
    user = update.effective_user
    if C.is_admin(user.id):
        return True
    if await is_channel_member_cached(user.id):
        return True
    text = ("🔒 Please join our channel first!\n\n"
            "To use this bot, you must join our official channel.\n"
            "Tap the button below to join, then tap 'I've Joined'.")
    if update.callback_query:
        q = update.callback_query
        await q.answer()
        try:
            await q.edit_message_text(text, reply_markup=force_join_kb())
        except Exception:
            await q.message.reply_text(text, reply_markup=force_join_kb())
    elif update.message:
        await update.message.reply_text(text, reply_markup=force_join_kb())
    return False


# ---------- helpers ----------
def register_user(user):
    users = C.load_json(C.USERS_FILE, {})
    uid = str(user.id)
    if uid not in users:
        users[uid] = {"name": user.first_name or "", "username": user.username or "",
                      "joined": datetime.now().isoformat()}
        C.save_json(C.USERS_FILE, users)


def check_gmail_login(email_addr, app_password):
    """Validate Gmail IMAP login. Never logs the password."""
    try:
        m = imaplib.IMAP4_SSL("imap.gmail.com", timeout=20)
        m.login(email_addr.strip(), app_password.strip())
        m.logout()
        return True
    except Exception:
        return False


def validate_bot_token(token):
    """Validate a Telegram bot token via getMe. Returns (ok, username, first_name)."""
    token = (token or "").strip()
    if ":" not in token:
        return False, "", ""
    try:
        req = urllib.request.Request(f"https://api.telegram.org/bot{token}/getMe")
        with urllib.request.urlopen(req, timeout=15) as r:
            d = json.load(r)
        if d.get("ok"):
            res = d["result"]
            un = res.get("username") or ""
            return True, ("@" + un if un else ""), res.get("first_name") or ""
        return False, "", ""
    except Exception:
        return False, "", ""


def send_via_token(token, chat_id, text):
    """Send a Telegram message using any bot token. Returns True/False."""
    try:
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=json.dumps({"chat_id": int(chat_id), "text": text}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            d = json.load(r)
        return bool(d.get("ok"))
    except Exception:
        return False


def _get_gateway_uptime():
    """Fetch gateway uptime from the /health endpoint. Returns string like '2d 5h 23m'."""
    base = PUBLIC_URL or "https://fambridge-gateway.onrender.com"
    try:
        req = urllib.request.Request(f"{base}/health", method="GET")
        with urllib.request.urlopen(req, timeout=10) as r:
            d = json.load(r)
        return d.get("uptime", "?") if d.get("ok") else "?"
    except Exception:
        return "unreachable"


# ---------- user_data backed by disk (pending input survives restarts) ----------
def _ud(context, uid):
    if not context.user_data:
        context.user_data.update(C.load_user_state(uid))
    return context.user_data


def _save_ud(context, uid):
    C.save_user_state(uid, context.user_data)


def _set_ud(context, uid, key, value=True):
    _ud(context, uid)[key] = value
    _save_ud(context, uid)


def _pop_ud(context, uid, key, default=False):
    v = _ud(context, uid).pop(key, default)
    _save_ud(context, uid)
    return v


def _clear_ud(context, uid):
    context.user_data.clear()
    C.clear_user_state(uid)


# ---------- keyboards ----------
def main_menu_kb(user_id):
    rows = [[InlineKeyboardButton("👤 User Panel", callback_data="menu_user")]]
    if C.is_admin(user_id):
        rows.append([InlineKeyboardButton("🛠 Admin Panel", callback_data="menu_admin")])
    return InlineKeyboardMarkup(rows)


def user_panel_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📧 Connect Gmail", callback_data="u_gmail"),
         InlineKeyboardButton("🆔 Set UPI ID", callback_data="u_upi")],
        [InlineKeyboardButton("🔑 My API Key", callback_data="u_apikey"),
         InlineKeyboardButton("🔗 Webhook URL", callback_data="u_webhook")],
        [InlineKeyboardButton("🤖 Connect My Bot", callback_data="u_mybot")],
        [InlineKeyboardButton("🧪 Test Order", callback_data="u_testorder"),
         InlineKeyboardButton("📊 My Stats", callback_data="u_usage")],
        [InlineKeyboardButton("💳 My Plan", callback_data="u_plan"),
         InlineKeyboardButton("📖 Integration Guide", callback_data="u_guide")],
        [InlineKeyboardButton("🆘 Support", callback_data="u_support"),
         InlineKeyboardButton("📜 Terms & Privacy", callback_data="u_legal")],
        [InlineKeyboardButton("⬅️ Back", callback_data="menu_main")],
    ])


def legal_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📜 Terms of Service", callback_data="u_terms"),
         InlineKeyboardButton("🔒 Privacy Policy", callback_data="u_privacy")],
        [InlineKeyboardButton("⬅️ Back", callback_data="menu_user")],
    ])


def gmail_kb(connected):
    rows = []
    if connected:
        rows.append([InlineKeyboardButton("🔄 Change", callback_data="u_gmail_connect"),
                     InlineKeyboardButton("🗑 Disconnect", callback_data="u_gmail_disconnect")])
    else:
        rows.append([InlineKeyboardButton("➕ Connect Gmail", callback_data="u_gmail_connect")])
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="menu_user")])
    return InlineKeyboardMarkup(rows)


def apikey_kb(has_key):
    rows = []
    if has_key:
        rows.append([InlineKeyboardButton("🔄 Regenerate", callback_data="u_key_regen"),
                     InlineKeyboardButton("🗑 Revoke", callback_data="u_key_revoke")])
    else:
        rows.append([InlineKeyboardButton("➕ Create API Key", callback_data="u_key_create")])
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="menu_user")])
    return InlineKeyboardMarkup(rows)


def mybot_kb(connected):
    rows = []
    if connected:
        rows.append([InlineKeyboardButton("🔄 Change", callback_data="u_mybot_connect"),
                     InlineKeyboardButton("🗑 Disconnect", callback_data="u_mybot_disconnect")])
    else:
        rows.append([InlineKeyboardButton("➕ Connect My Bot", callback_data="u_mybot_connect")])
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="menu_user")])
    return InlineKeyboardMarkup(rows)


def admin_panel_kb():
    maint_on = C.get_maintenance_mode()
    maint_btn = f"🔧 Maintenance: {'ON' if maint_on else 'OFF'}"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Stats", callback_data="a_stats"),
         InlineKeyboardButton("🏪 Merchants", callback_data="a_merchants")],
        [InlineKeyboardButton("🧾 Orders", callback_data="a_orders"),
         InlineKeyboardButton("📢 Broadcast", callback_data="a_broadcast")],
        [InlineKeyboardButton("💳 Subscriptions", callback_data="a_subs"),
         InlineKeyboardButton("💰 Sub. UPI", callback_data="a_subupi")],
        [InlineKeyboardButton(maint_btn, callback_data="a_maint_toggle")],
        [InlineKeyboardButton("⬅️ Back", callback_data="menu_main")],
    ])


def guide_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📄 Python Bot Code", callback_data="u_code_py"),
         InlineKeyboardButton("📄 Website (JS) Code", callback_data="u_code_js")],
        [InlineKeyboardButton("📄 Webhook Server Code", callback_data="u_code_wh")],
        [InlineKeyboardButton("⬅️ Back", callback_data="menu_user")],
    ])


GUIDE = """📖 Integration Guide - FamPay auto-verification for your bot or website

🌐 Your Gateway URL: {BASE}

🧪 Try it live first! Open our test bot @Teisgsksbtes_bot — it sells a Rs.1 test product through this gateway so you can see the full payment flow (QR -> pay -> auto-verify -> delivery) before integrating.

STEP 1: One-time setup (in this bot)
User Panel > Connect Gmail (the SAME Gmail linked to your FamPay account + App Password)
User Panel > Set UPI ID (your FamPay UPI ID, e.g. name@fam)
User Panel > My API Key > Create
User Panel > Connect My Bot (optional — get payment notifications from your own bot)

Plans (monthly): FREE = 60 requests (₹0) • STARTER = 150 (₹80) • BEST = 250 (₹150) • PREMIUM = 500 (₹250). Counter resets on the 1st.
Upgrade anytime from My Plan — pay via UPI QR, your plan auto-activates when the payment is detected!

STEP 2: Customer flow (in your own bot or website)
1) Customer taps "Pay Rs.99" -> you call POST /api/create_order
2) You send the customer the QR (build a photo from qr_base64)
3) Customer pays in their UPI app
   (A "Payment Done" button is just UX — verification does not depend on it)
4) Our gateway sees the FamPay email in Gmail and auto-verifies (~25 sec)
5) As soon as status is "paid", your bot/website delivers the product — fully automatic!

STEP 3: API calls
Create an order:
POST {BASE}/api/create_order
Body: {"api_key": "your_key", "amount": 99, "note": "premium"}
Response: {"order_id": "ord_...", "upi_link": "upi://pay?...", "qr_base64": "..."}

Check status (poll, or use a webhook):
GET {BASE}/api/order_status?api_key=KEY&order_id=ord_...
Response: {"status": "paid"} -> deliver the product
("pending" = wait, "expired" = 15 minutes passed)

STEP 4 (recommended): Webhook
User Panel > Webhook URL — enter your URL.
On every verified payment we POST:
{"event": "payment.verified", "order_id": "...", "amount_paise": 9900, "utr": "..."}
The X-Signature header carries an HMAC-SHA256 signature (secret = your API key).
With a webhook you don't need polling!

--- Telegram bot example (full flow, Python) ---
import requests, time
GATEWAY = "{BASE}"; API_KEY = "your_key"
r = requests.post(GATEWAY + "/api/create_order",
    json={"api_key": API_KEY, "amount": 99, "note": "vip"}).json()
# build a QR photo from r["qr_base64"] and send it to the customer
for _ in range(60):
    s = requests.get(GATEWAY + "/api/order_status",
        params={"api_key": API_KEY, "order_id": r["order_id"]}).json()
    if s["status"] == "paid":
        # deliver the product!
        break
    time.sleep(15)

--- Website example (JavaScript) ---
const GATEWAY = "{BASE}", API_KEY = "your_key";
// 1) create the order
let r = await fetch(GATEWAY + "/api/create_order", {
  method: "POST", headers: {"Content-Type": "application/json"},
  body: JSON.stringify({api_key: API_KEY, amount: 99, note: "premium"})
}).then(x => x.json());
// 2) show the QR on your page:
// document.getElementById("qr").src = "data:image/png;base64," + r.qr_base64;
// 3) poll for the payment
let timer = setInterval(async () => {
  let s = await fetch(GATEWAY + "/api/order_status?api_key=" + API_KEY + "&order_id=" + r.order_id).then(x => x.json());
  if (s.status === "paid") { clearInterval(timer); deliverProduct(); }
  if (s.status === "expired") { clearInterval(timer); showExpired(); }
}, 15000);

Note: the public API URL depends on your server.
Gmail App Password: Google Account > Security > 2-Step Verification > App passwords.
"""

BASE_NOTE = PUBLIC_URL or "https://fambridge-gateway.onrender.com"


def _merchants_view():
    """Returns (text, keyboard) for the admin merchants list with plan toggles."""
    merchants = C.load_json(C.MERCHANTS_FILE, {})
    lines = [f"🏪 Merchants ({len(merchants)}):\n"]
    rows = []
    for k, m in list(merchants.items())[-10:]:
        plan, used, limit = C.get_quota(k)
        lines.append(f"- {m.get('owner_name','')} | {plan.upper()} ({used}/{limit}) | "
                     f"UPI: {m.get('upi_id') or '-'} | Gmail: {'✅' if m.get('gmail_email') else '❌'}")
        nxt = C.PLAN_ORDER[(C.PLAN_ORDER.index(plan) + 1) % len(C.PLAN_ORDER)] if plan in C.PLAN_ORDER else "free"
        label = f"{(m.get('owner_name') or '?')[:12]}: → {nxt.upper()}"
        rows.append([InlineKeyboardButton(label, callback_data=f"a_plan_{k}")])
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="menu_admin")])
    text = "\n".join(lines) if merchants else "No merchants."
    return text, InlineKeyboardMarkup(rows)


def _subscriptions_view():
    """Admin subscriptions dashboard: plan counts, est. MRR, sub UPI, plan toggles."""
    merchants = C.load_json(C.MERCHANTS_FILE, {})
    counts = {p: 0 for p in C.PLAN_ORDER}
    for m in merchants.values():
        p = m.get("plan", "free")
        counts[p] = counts.get(p, 0) + 1
    mrr = sum(C.PLANS[p]["price"] * c for p, c in counts.items() if p in C.PLANS)
    sub_upi = C.get_admin_settings().get("sub_upi_id", "")
    lines = ["💳 Subscriptions:\n"]
    for p in C.PLAN_ORDER:
        lines.append(f"• {p.upper()}: {counts[p]} merchants — ₹{C.PLANS[p]['price']}/mo")
    lines.append(f"\n💰 Est. MRR: ₹{mrr}/month")
    lines.append(f"💰 Subscription UPI: {sub_upi or 'not set (tap 💰 Sub. UPI)'}")
    lines.append("\nTap a merchant to change their plan (cycles):")
    rows = []
    for k, m in list(merchants.items())[-10:]:
        plan = m.get("plan", "free")
        nxt = C.cycle_plan(plan)
        rows.append([InlineKeyboardButton(
            f"{(m.get('owner_name') or '?')[:12]}: {plan.upper()} → {nxt.upper()}",
            callback_data=f"a_subplan_{k}")])
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="menu_admin")])
    return "\n".join(lines), InlineKeyboardMarkup(rows)


# ---------- commands ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    # Maintenance mode: only admin can use the bot
    if C.get_maintenance_mode() and not C.is_admin(user.id):
        await update.message.reply_text(
            "🔧 Bot is under maintenance.\n\nPlease try again later.")
        return
    if not await require_channel_member(update, context):
        return
    _clear_ud(context, user.id)  # /start cancels any pending input
    C.ensure_admin(user.id)
    register_user(user)
    text = (f"👋 Hello {user.first_name}!\n\n"
            f"I'm the FamPay auto-verification gateway.\n\n"
            f"👤 User Panel: connect Gmail, set UPI ID, get your API key.\n")
    if C.is_admin(user.id):
        text += "🛠 Admin Panel: stats, merchants, orders, broadcast.\n"
    await update.message.reply_text(text, reply_markup=main_menu_kb(user.id))


# ---------- button router ----------
async def button_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    user = update.effective_user
    uid = user.id
    data = q.data

    # Maintenance mode: only admin can use the bot
    if C.get_maintenance_mode() and not C.is_admin(uid):
        await q.edit_message_text(
            "🔧 Bot is under maintenance.\n\nPlease try again later.")
        return

    # "I've Joined" button: re-check membership, then show main menu or prompt again
    if data == "check_joined":
        if C.is_admin(uid) or await is_channel_member_cached(uid):
            await q.edit_message_text("Main menu:", reply_markup=main_menu_kb(uid))
        else:
            await q.edit_message_text(
                "🔒 You haven't joined yet!\n\n"
                "Please join our channel first, then tap 'I've Joined'.",
                reply_markup=force_join_kb())
        return

    if not await require_channel_member(update, context):
        return
    register_user(user)

    if data == "menu_main":
        await q.edit_message_text("Main menu:", reply_markup=main_menu_kb(uid))
    elif data == "menu_user":
        await q.edit_message_text("👤 User Panel:", reply_markup=user_panel_kb())
    elif data == "menu_admin":
        if not C.is_admin(uid):
            await q.edit_message_text("❌ Admin only.")
            return
        await q.edit_message_text("🛠 Admin Panel:", reply_markup=admin_panel_kb())

    # ----- gmail -----
    elif data == "u_gmail":
        key, m = C.get_merchant_by_owner(uid)
        em = (m or {}).get("gmail_email", "")
        txt = (f"📧 Gmail: {em if em else 'not connected'}\n\n"
               f"⚠️ IMPORTANT: use the same Gmail that is linked to your FAMPAY account!\n"
               f"With any other email, payment emails will never arrive and verification will never work.\n\n"
               f"Use an App Password (never send your real password) — Google Account > Security > App passwords.")
        await q.edit_message_text(txt, reply_markup=gmail_kb(bool(em)))
    elif data == "u_gmail_connect":
        _set_ud(context, uid, "awaiting_gmail_email")
        await q.edit_message_text(
            "📧 Send your Gmail address.\n\n"
            "⚠️ Send the email linked to your FAMPAY account!\n"
            "Any other email and payment verification will NOT work.\n\nCancel: /start")
    elif data == "u_gmail_disconnect":
        key, m = C.get_merchant_by_owner(uid)
        if m:
            merchants = C.load_json(C.MERCHANTS_FILE, {})
            m["gmail_email"] = ""
            m["gmail_apppass_enc"] = ""
            merchants[key] = m
            C.save_json(C.MERCHANTS_FILE, merchants)
        await q.edit_message_text("🗑 Gmail disconnected.", reply_markup=gmail_kb(False))

    # ----- upi id -----
    elif data == "u_upi":
        key, m = C.get_merchant_by_owner(uid)
        cur = (m or {}).get("upi_id", "")
        _set_ud(context, uid, "awaiting_upi_id")
        await q.edit_message_text(
            f"🆔 Current UPI ID: {cur if cur else 'not set'}\n\n"
            f"Send your FamPay UPI ID (e.g.: name@fam):\n\nCancel: /start")

    # ----- api key -----
    elif data == "u_apikey":
        key, m = C.get_merchant_by_owner(uid)
        txt = f"🔑 API Key:\n{key}\n\nKeep it secret!" if key else "No API key yet. Create one below."
        await q.edit_message_text(txt, reply_markup=apikey_kb(bool(key)))
    elif data == "u_key_create":
        key = C.create_merchant_key(uid, user.first_name or "")
        await q.edit_message_text(f"✅ API Key:\n\n{key}\n\nSave it on your server.", reply_markup=apikey_kb(True))
    elif data == "u_key_regen":
        key = C.create_merchant_key(uid, user.first_name or "")
        await q.edit_message_text(f"🔄 New key:\n\n{key}\n\nThe old one no longer works.", reply_markup=apikey_kb(True))
    elif data == "u_key_revoke":
        ok = C.revoke_merchant_key(uid)
        await q.edit_message_text("🗑 Revoked." if ok else "No key found.", reply_markup=apikey_kb(False))

    # ----- webhook -----
    elif data == "u_webhook":
        key, m = C.get_merchant_by_owner(uid)
        cur = (m or {}).get("webhook_url", "")
        _set_ud(context, uid, "awaiting_webhook")
        await q.edit_message_text(
            f"🔗 Current webhook: {cur if cur else 'not set'}\n\n"
            f"Send your webhook URL (we POST there on verified payment).\n"
            f"Send 'off' to remove.\n\nCancel: /start")

    # ----- apna bot connect -----
    elif data == "u_mybot":
        key, m = C.get_merchant_by_owner(uid)
        tok = C.get_merchant_bot_token(m or {})
        bun = (m or {}).get("bot_username", "")
        if tok:
            txt = (f"🤖 My Bot: {bun or 'connected'} ✅\n\n"
                   f"Payment notifications will now come from your own bot.\n"
                   f"Note: you must press /start on your bot first.")
        else:
            txt = ("🤖 Connect My Bot\n\n"
                   "Get payment notifications from your own Telegram bot — "
                   "looks professional.")
        await q.edit_message_text(txt, reply_markup=mybot_kb(bool(tok)))
    elif data == "u_mybot_connect":
        _set_ud(context, uid, "awaiting_bot_token")
        await q.edit_message_text(
            "🤖 Send your bot token.\n\nSteps:\n"
            "1) Open @BotFather > /newbot > create your bot\n"
            "2) Open your new bot on Telegram and press /start (REQUIRED — otherwise no message can be delivered)\n"
            "3) Send the BotFather token here\n\nCancel: /start")
    elif data == "u_mybot_disconnect":
        key, m = C.get_merchant_by_owner(uid)
        if m:
            merchants = C.load_json(C.MERCHANTS_FILE, {})
            m["bot_token_enc"] = ""
            m["bot_username"] = ""
            merchants[key] = m
            C.save_json(C.MERCHANTS_FILE, merchants)
        await q.edit_message_text("🗑 Bot disconnected. Notifications will come from this bot again.",
                                  reply_markup=mybot_kb(False))

    # ----- guide / usage / test order -----
    elif data == "u_guide":
        await q.edit_message_text(GUIDE.replace("{BASE}", BASE_NOTE), reply_markup=guide_kb())
    elif data in ("u_code_py", "u_code_js", "u_code_wh"):
        key, m = C.get_merchant_by_owner(uid)
        if not key:
            await q.edit_message_text("Create an API key first (User Panel > My API Key).",
                                      reply_markup=guide_kb())
            return
        if data == "u_code_py":
            fname, code = ("fampay_merchant_bot.py",
                           SC.MERCHANT_BOT_PY.replace("{API_KEY}", key)
                                             .replace("https://YOUR-GATEWAY-URL", BASE_NOTE))
            cap = "🤖 Ready-made Telegram bot code!\n\nAPI key + Gateway URL pre-filled ✅\nJust set BOT_TOKEN at the top and run it."
        elif data == "u_code_js":
            fname, code = ("fampay_website.js",
                           SC.MERCHANT_WEB_JS.replace("{API_KEY}", key)
                                             .replace("https://YOUR-GATEWAY-URL", BASE_NOTE))
            cap = "🌐 Ready-made website code!\n\nAPI key + Gateway URL pre-filled ✅\nJust call buyNow() from your Buy button."
        else:
            fname, code = "fampay_webhook_server.py", SC.MERCHANT_WEBHOOK_PY.replace("{API_KEY}", key)
            cap = "🔗 Webhook server code!\n\nYour API key is pre-filled ✅\nRun it publicly, then paste its URL in User Panel > Webhook URL."
        await q.message.reply_document(document=io.BytesIO(code.encode()),
                                       filename=fname, caption=cap)
        await q.message.reply_text("Integration Guide:", reply_markup=guide_kb())
    elif data == "u_usage":
        key, m = C.get_merchant_by_owner(uid)
        orders = C.load_json(C.ORDERS_FILE, {})
        mine = [o for o in orders.values() if o.get("api_key") == key] if key else []
        paid = [o for o in mine if o["status"] == "paid"]
        revenue = sum(o["amount_paise"] for o in paid) / 100
        plan, used, limit = C.get_quota(key) if key else ("free", 0, C.PLANS["free"]["quota"])
        qline = f"{used}/{limit} this month"
        await q.edit_message_text(
            f"📊 Your Usage:\n\nPlan: {plan.upper()}\nPayment requests: {qline}\n"
            f"Orders: {len(mine)}\nPaid: {len(paid)}\nRevenue: ₹{revenue:.2f}",
            reply_markup=user_panel_kb())
    elif data == "u_plan":
        key, m = C.get_merchant_by_owner(uid)
        plan, used, limit = C.get_quota(key) if key else ("free", 0, C.PLANS["free"]["quota"])
        remaining = limit - used
        lines = [f"💳 My Plan: {plan.upper()}\n",
                 f"📊 Usage this month:",
                 f"   ✔ Used: {used} requests",
                 f"   📦 Remaining: {remaining} requests",
                 f"   (Total: {limit})"]
        if remaining <= 0:
            lines.append("   ⛔ Quota over! Upgrade to continue.")
        elif remaining <= limit * 0.2:
            lines.append("   ⚠️ Low quota — consider upgrading!")
        lines += ["Counter resets on the 1st of every month.\n",
                  "Plans (monthly):"]
        for p in C.PLAN_ORDER:
            mark = " ← current" if p == plan else ""
            lines.append(f"• {p.upper()} — {C.PLANS[p]['quota']} requests — ₹{C.PLANS[p]['price']}{mark}")
        if plan != "premium":
            lines.append("\nTap a plan to upgrade — pay via UPI, auto-activates on payment! ⚡")
            kb_rows = []
            for p in C.PLAN_ORDER[C.PLAN_ORDER.index(plan) + 1:]:
                kb_rows.append([InlineKeyboardButton(
                    f"⬆️ {p.upper()} — ₹{C.PLANS[p]['price']}/mo", callback_data=f"u_sub_{p}")])
        else:
            lines.append("\n🎉 You are on the highest plan!")
            kb_rows = []
        kb_rows.append([InlineKeyboardButton("⬅️ Back", callback_data="menu_user")])
        await q.edit_message_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(kb_rows))
    elif data.startswith("u_sub_"):
        plan2 = data[len("u_sub_"):]
        key, m = C.get_merchant_by_owner(uid)
        if not key:
            await q.edit_message_text("Create an API key first (User Panel > My API Key).",
                                      reply_markup=user_panel_kb())
            return
        sub = C.get_pending_sub_order(key)
        if sub and sub.get("plan") != plan2:
            sub = None  # different plan requested — make a fresh order
        if not sub:
            sub, err = C.create_sub_order(key, uid, user.first_name or "", plan2)
            if not sub:
                await q.edit_message_text(f"❌ {err}", reply_markup=user_panel_kb())
                return
        qr_bytes = C.make_qr_png_bytes(sub["upi_link"])
        price = sub["amount_paise"] // 100
        await q.message.reply_photo(
            photo=io.BytesIO(qr_bytes),
            caption=(f"💳 Upgrade to {sub['plan'].upper()} — ₹{price}/month\n\n"
                     f"Scan the QR and pay within 30 minutes.\n"
                     f"Your plan auto-activates when the payment is detected! ⚡"))
        await q.message.reply_text("User Panel:", reply_markup=user_panel_kb())
    elif data == "u_support":
        await q.edit_message_text(
            "🆘 Support\n\n"
            "📢 Channel: https://t.me/magservicesbots\n"
            "💬 DM us: @magsupportchat_bot\n\n"
            "Plan upgrades, payment issues, or anything else — we're here to help!",
            reply_markup=user_panel_kb())
    elif data == "u_legal":
        await q.edit_message_text(
            "📜 Legal\n\n"
            "Please read our Terms of Service and Privacy Policy before using FamBridge.",
            reply_markup=legal_kb())
    elif data == "u_terms":
        text = L.TERMS_TEXT
        if PUBLIC_URL:
            text += f"\n\n🌐 Full version: {PUBLIC_URL}/terms"
        await q.edit_message_text(text, reply_markup=legal_kb())
    elif data == "u_privacy":
        text = L.PRIVACY_TEXT
        if PUBLIC_URL:
            text += f"\n\n🌐 Full version: {PUBLIC_URL}/privacy"
        await q.edit_message_text(text, reply_markup=legal_kb())
    elif data == "u_testorder":
        key, m = C.get_merchant_by_owner(uid)
        if not key:
            await q.edit_message_text("Create an API key first.", reply_markup=user_panel_kb())
            return
        order, err = C.create_order(key, 1, "test")
        if not order:
            await q.edit_message_text(f"❌ {err}", reply_markup=user_panel_kb())
            return
        qr_bytes = C.make_qr_png_bytes(order["upi_link"])
        await q.message.reply_photo(
            photo=io.BytesIO(qr_bytes),
            caption=(f"🧪 Test Order (₹1.00)\nOrder: {order['order_id']}\n\n"
                     f"Scan the QR and pay — it auto-verifies when the email arrives.\n"
                     f"Status: /api/order_status?order_id={order['order_id']}"))
        await q.message.reply_text("User Panel:", reply_markup=user_panel_kb())

    # ----- admin -----
    elif data == "a_stats":
        if not C.is_admin(uid):
            return
        users = C.load_json(C.USERS_FILE, {})
        merchants = C.load_json(C.MERCHANTS_FILE, {})
        orders = C.load_json(C.ORDERS_FILE, {})
        paid = [o for o in orders.values() if o["status"] == "paid"]
        rev = sum(o["amount_paise"] for o in paid) / 100
        uptime = await asyncio.to_thread(_get_gateway_uptime)
        await q.edit_message_text(
            f"📊 Stats:\n\n👥 Users: {len(users)}\n🏪 Merchants: {len(merchants)}\n"
            f"🧾 Orders: {len(orders)} (paid: {len(paid)})\n💰 Revenue: ₹{rev:.2f}\n"
            f"⏱️ Uptime: {uptime}",
            reply_markup=admin_panel_kb())
    elif data == "a_merchants":
        if not C.is_admin(uid):
            return
        text, kb = _merchants_view()
        await q.edit_message_text(text, reply_markup=kb)
    elif data.startswith("a_plan_"):
        if not C.is_admin(uid):
            return
        key = data[len("a_plan_"):]
        merchants = C.load_json(C.MERCHANTS_FILE, {})
        m = merchants.get(key)
        if m:
            new_plan = C.cycle_plan(m.get("plan", "free"))
            C.set_plan(key, new_plan)
            try:
                await context.bot.send_message(
                    chat_id=int(m.get("owner_id")),
                    text=(f"💳 Your plan is now {C.plan_summary(new_plan)}."))
            except Exception:
                pass
        text, kb = _merchants_view()
        await q.edit_message_text(text, reply_markup=kb)
    elif data == "a_subs":
        if not C.is_admin(uid):
            return
        text, kb = _subscriptions_view()
        await q.edit_message_text(text, reply_markup=kb)
    elif data.startswith("a_subplan_"):
        if not C.is_admin(uid):
            return
        key = data[len("a_subplan_"):]
        merchants = C.load_json(C.MERCHANTS_FILE, {})
        m = merchants.get(key)
        if m:
            new_plan = C.cycle_plan(m.get("plan", "free"))
            C.set_plan(key, new_plan)
            try:
                await context.bot.send_message(
                    chat_id=int(m.get("owner_id")),
                    text=(f"💳 Your plan is now {C.plan_summary(new_plan)}."))
            except Exception:
                pass
        text, kb = _subscriptions_view()
        await q.edit_message_text(text, reply_markup=kb)
    elif data == "a_subupi":
        if not C.is_admin(uid):
            return
        cur = C.get_admin_settings().get("sub_upi_id", "")
        _set_ud(context, uid, "awaiting_sub_upi")
        await q.edit_message_text(
            f"💰 Current subscription UPI: {cur or 'not set'}\n\n"
            f"Send your FamPay UPI ID where merchants will pay for plans\n"
            f"(e.g.: name@fam):\n\nCancel: /start")
    elif data == "a_maint_toggle":
        if not C.is_admin(uid):
            return
        new_state = not C.get_maintenance_mode()
        C.set_maintenance_mode(new_state)
        await q.answer(f"Maintenance {'ON' if new_state else 'OFF'}", show_alert=False)
        await q.edit_message_text(
            f"🔧 Maintenance mode is now **{'ON' if new_state else 'OFF'}**.\n\n"
            f"{'Only you (admin) can use the bot now.' if new_state else 'All users can use the bot again.'}",
            reply_markup=admin_panel_kb())
    elif data == "a_orders":
        if not C.is_admin(uid):
            return
        orders = C.load_json(C.ORDERS_FILE, {})
        recent = sorted(orders.values(), key=lambda o: o["created_at"], reverse=True)[:10]
        lines = [f"🧾 Recent orders:\n"]
        for o in recent:
            lines.append(f"- {o['order_id']} | ₹{o['amount_paise']/100:.2f} | {o['status']} | {o['created_at'][:16]}")
        await q.edit_message_text("\n".join(lines) if recent else "No orders.", reply_markup=admin_panel_kb())
    elif data == "a_broadcast":
        if not C.is_admin(uid):
            return
        _set_ud(context, uid, "awaiting_broadcast")
        await q.edit_message_text("📢 Send the broadcast text (in your next message). Cancel: /start")
    elif data == "bc_send":
        if not C.is_admin(uid):
            return
        text = _pop_ud(context, uid, "broadcast_text", "")
        users = C.load_json(C.USERS_FILE, {})
        await q.edit_message_text(f"📢 Sending to {len(users)} users...")
        ok = fail = 0
        for uid2 in users:
            try:
                await context.bot.send_message(chat_id=int(uid2), text=text)
                ok += 1
            except Exception:
                fail += 1
        await q.message.reply_text(f"✅ Done! Success: {ok}, Failed: {fail}", reply_markup=admin_panel_kb())
    elif data == "bc_cancel":
        _pop_ud(context, uid, "broadcast_text", None)
        await q.edit_message_text("❌ Cancel.", reply_markup=admin_panel_kb())


# ---------- text router ----------
async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    uid = user.id
    # Maintenance mode: only admin can use the bot
    if C.get_maintenance_mode() and not C.is_admin(uid):
        await update.message.reply_text(
            "🔧 Bot is under maintenance.\n\nPlease try again later.")
        return
    if not await require_channel_member(update, context):
        return
    text = (update.message.text or "").strip()
    register_user(user)

    if _pop_ud(context, uid, "awaiting_gmail_email"):
        if "@" not in text or "." not in text:
            _set_ud(context, uid, "awaiting_gmail_email")
            await update.message.reply_text("❌ Send a valid Gmail address.")
            return
        _set_ud(context, uid, "tmp_gmail", text)
        _set_ud(context, uid, "awaiting_gmail_apppass")
        await update.message.reply_text(
            "Now send the App Password (16 characters, spaces are fine).\n"
            "Google Account > Security > 2-Step Verification > App passwords.\n"
            "Never send your real Gmail password!")
        return

    if _pop_ud(context, uid, "awaiting_gmail_apppass"):
        email_addr = _pop_ud(context, uid, "tmp_gmail", "")
        app_pass = text.replace(" ", "")
        await update.message.reply_text("⏳ Checking Gmail...")
        try:
            gmail_ok = await asyncio.wait_for(
                asyncio.to_thread(check_gmail_login, email_addr, app_pass),
                timeout=45)
        except asyncio.TimeoutError:
            gmail_ok = False
            _set_ud(context, uid, "awaiting_gmail_email")
            await update.message.reply_text(
                "⏳ Gmail check timed out (network was slow).\nSend the email again to retry, or /start to cancel.")
            return
        if not gmail_ok:
            _set_ud(context, uid, "awaiting_gmail_email")
            await update.message.reply_text(
                "❌ Gmail login failed. Email or App Password is wrong.\nSend the email again, or /start to cancel.")
            return
        key, m = C.get_merchant_by_owner(uid)
        merchants = C.load_json(C.MERCHANTS_FILE, {})
        if not key:
            key = C.create_merchant_key(uid, user.first_name or "")
            m = merchants[key]
        m["gmail_email"] = email_addr
        try:
            m["gmail_apppass_enc"] = C.fernet_encrypt(app_pass)
        except RuntimeError:
            # Storage unreachable: never persist half a record. Nothing changed.
            await update.message.reply_text(
                "⚠️ Storage is temporarily unreachable, so the password can't be saved safely.\n"
                "Please try again in a minute — nothing was changed.")
            return
        merchants[key] = m
        if C.save_json(C.MERCHANTS_FILE, merchants):
            await update.message.reply_text(
                f"✅ Gmail connected!\n\nOrders will now auto-verify when FamPay payment emails arrive.\n"
                f"Your API Key:\n{key}", reply_markup=user_panel_kb())
        else:
            await update.message.reply_text(
                "⚠️ Gmail verified, but saving hit a temporary storage hiccup.\n"
                "Please try connecting once more in a minute.")
        return

    if _pop_ud(context, uid, "awaiting_upi_id"):
        if "@" not in text:
            _set_ud(context, uid, "awaiting_upi_id")
            await update.message.reply_text("❌ Send a valid UPI ID (e.g. name@fam).")
            return
        key, m = C.get_merchant_by_owner(uid)
        merchants = C.load_json(C.MERCHANTS_FILE, {})
        if not key:
            key = C.create_merchant_key(uid, user.first_name or "")
            m = merchants[key]
        m["upi_id"] = text.strip()
        merchants[key] = m
        C.save_json(C.MERCHANTS_FILE, merchants)
        await update.message.reply_text(f"✅ UPI ID set: {text.strip()}", reply_markup=user_panel_kb())
        return

    if _pop_ud(context, uid, "awaiting_webhook"):
        key, m = C.get_merchant_by_owner(uid)
        merchants = C.load_json(C.MERCHANTS_FILE, {})
        if not key:
            key = C.create_merchant_key(uid, user.first_name or "")
            m = merchants[key]
        cleaned = text.strip()
        if cleaned.lower() != "off" and not (
                cleaned.startswith("http://") or cleaned.startswith("https://")):
            _set_ud(context, uid, "awaiting_webhook")
            await update.message.reply_text("❌ Send a valid http(s) URL, or 'off' to remove.")
            return
        m["webhook_url"] = "" if cleaned.lower() == "off" else cleaned
        merchants[key] = m
        C.save_json(C.MERCHANTS_FILE, merchants)
        await update.message.reply_text(f"✅ Webhook set: {m['webhook_url'] or 'off'}", reply_markup=user_panel_kb())
        return

    if _pop_ud(context, uid, "awaiting_sub_upi"):
        if not C.is_admin(uid):
            return
        if "@" not in text:
            _set_ud(context, uid, "awaiting_sub_upi")
            await update.message.reply_text("❌ Send a valid UPI ID (e.g. name@fam).")
            return
        C.set_admin_setting("sub_upi_id", text.strip())
        await update.message.reply_text(
            f"✅ Subscription UPI set: {text.strip()}\n\n"
            f"Merchants will see this in My Plan when upgrading.",
            reply_markup=admin_panel_kb())
        return

    if _pop_ud(context, uid, "awaiting_bot_token"):
        ok, username, fname = await asyncio.to_thread(validate_bot_token, text)
        if not ok:
            _set_ud(context, uid, "awaiting_bot_token")
            await update.message.reply_text(
                "❌ Invalid token. Copy the correct token from @BotFather and send it again.\nCancel: /start")
            return
        key, m = C.get_merchant_by_owner(uid)
        merchants = C.load_json(C.MERCHANTS_FILE, {})
        if not key:
            key = C.create_merchant_key(uid, user.first_name or "")
            m = merchants[key]
        m["bot_token_enc"] = C.fernet_encrypt(text.strip())
        m["bot_username"] = username or fname
        merchants[key] = m
        C.save_json(C.MERCHANTS_FILE, merchants)
        sent = await asyncio.to_thread(
            send_via_token, text.strip(), uid,
            "✅ Your bot is connected!\n\nYou will now receive payment notifications from this bot. 🎉")
        if sent:
            await update.message.reply_text(
                f"✅ Connected: {username or fname}\n\nThe test message reached your bot!",
                reply_markup=user_panel_kb())
        else:
            await update.message.reply_text(
                f"⚠️ Token saved ({username or fname}), but the test message did not arrive.\n\n"
                f"Open your bot on Telegram and press /start — then tap Connect here again.",
                reply_markup=user_panel_kb())
        return

    if _pop_ud(context, uid, "awaiting_broadcast"):
        if not C.is_admin(uid):
            return
        _set_ud(context, uid, "broadcast_text", text)
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Send", callback_data="bc_send"),
                                    InlineKeyboardButton("❌ Cancel", callback_data="bc_cancel")]])
        await update.message.reply_text(f"Preview:\n\n{text}\n\nSend it?", reply_markup=kb)
        return


def _build_app():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_router))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))
    return app


# Diagnostic: tracks the polling loop state for /debug/polling.
POLL_STATUS = {"state": "starting", "last_error": None,
               "retries": 0, "started_at": None}


def main():
    if not BOT_TOKEN:
        print("❌ Set PAYBOT_TOKEN in .env")
        return
    # Admin is only @Magvenomsuper (ADMIN_ID) — refuse to start if env is missing,
    # so nobody else can ever become admin by accident.
    if not C.get_admin_id():
        print("❌ Set ADMIN_ID in .env (owner's Telegram numeric ID only)")
        return
    print(f"🔐 Admin locked to ID ...{C.get_admin_id()[-4:]}")
    # Self-healing: this VM's egress proxy to api.telegram.org is flaky.
    # - bootstrap_retries=-1: PTB itself retries the startup handshake forever.
    # - close_loop=False: keeps the event loop usable for our outer retry.
    # - outer loop: catches anything that still escapes and restarts.
    # A clean shutdown (SIGTERM/SIGINT) makes run_polling() return normally,
    # which breaks the loop so restarts actually stop the bot.
    import time as _time
    import asyncio as _asyncio
    import datetime as _dt
    POLL_STATUS["started_at"] = _dt.datetime.utcnow().isoformat() + "Z"
    while True:
        try:
            print("🤖 FamPay Gateway Bot running...")
            POLL_STATUS["state"] = "polling"
            POLL_STATUS["last_error"] = None
            _asyncio.set_event_loop(_asyncio.new_event_loop())
            _build_app().run_polling(bootstrap_retries=-1, close_loop=False)
            POLL_STATUS["state"] = "stopped_clean"
            break
        except (KeyboardInterrupt, SystemExit):
            POLL_STATUS["state"] = "stopped_clean"
            break
        except Exception as e:
            POLL_STATUS["state"] = "retrying"
            POLL_STATUS["last_error"] = f"{type(e).__name__}: {e}"[:300]
            POLL_STATUS["retries"] += 1
            print(f"bot crashed ({type(e).__name__}: {e}) — restarting in 15s")
        _time.sleep(15)


if __name__ == "__main__":
    main()
