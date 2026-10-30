"""
FamPay Gateway - starter code templates for merchants.
Each template has an {API_KEY} placeholder; bot.py fills in the merchant's
real key with .replace() (never .format() — templates contain JSON braces).
"""

MERCHANT_BOT_PY = '''"""
FamPay Gateway - example merchant Telegram bot.
YOUR API KEY IS ALREADY FILLED IN BELOW. Just:
1) pip install python-telegram-bot requests
2) Set GATEWAY to your gateway's public URL (e.g. from Render)
3) Set BOT_TOKEN from @BotFather
4) Run: python fampay_merchant_bot.py
"""
import io
import time
import base64
import threading
import asyncio
import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes,
)

# ---------------- CONFIG ----------------
GATEWAY = "https://YOUR-GATEWAY-URL"  # <-- replace with your gateway's public URL
API_KEY = "{API_KEY}"                  # your gateway API key (pre-filled ✅)
BOT_TOKEN = "YOUR_BOT_TOKEN_HERE"      # from @BotFather
PRICE = 99                             # what you charge
PRODUCT_TEXT = "Here is your premium access! 🎉"  # what the buyer gets
# ----------------------------------------


def gateway_create_order(amount, note=""):
    r = requests.post(
        GATEWAY + "/api/create_order",
        json={"api_key": API_KEY, "amount": amount, "note": note},
        timeout=20,
    ).json()
    if not r.get("success"):
        raise Exception(r.get("error", "order failed"))
    return r


def gateway_status(order_id):
    r = requests.get(
        GATEWAY + "/api/order_status",
        params={"api_key": API_KEY, "order_id": order_id},
        timeout=20,
    ).json()
    return r.get("status")


def wait_and_deliver(bot, chat_id, order_id):
    """Background watcher: delivers the product the moment payment verifies."""
    for _ in range(60):  # ~15 minutes
        try:
            if gateway_status(order_id) == "paid":
                asyncio.run(bot.send_message(
                    chat_id=chat_id,
                    text="✅ Payment detected automatically!\\n\\n" + PRODUCT_TEXT,
                ))
                return
        except Exception:
            pass
        time.sleep(15)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton(f"💳 Buy Premium - Rs.{PRICE}", callback_data="buy"),
    ]])
    await update.message.reply_text(
        "Welcome! Tap below to buy premium. 👇", reply_markup=kb)


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data == "buy":
        order = gateway_create_order(PRICE, note="premium")
        qr = base64.b64decode(order["qr_base64"])
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Payment Done", callback_data="done_" + order["order_id"]),
        ]])
        await q.message.reply_photo(
            photo=io.BytesIO(qr),
            caption=(f"Scan the QR and pay Rs.{PRICE}.\\n"
                     f"Order: {order['order_id']}\\n\\n"
                     "Then tap Payment Done — or just wait, "
                     "we detect your payment automatically! ⚡"),
            reply_markup=kb,
        )
        threading.Thread(
            target=wait_and_deliver,
            args=(context.bot, q.message.chat_id, order["order_id"]),
            daemon=True,
        ).start()
    elif q.data.startswith("done_"):
        order_id = q.data[len("done_"):]
        status = gateway_status(order_id)
        if status == "paid":
            await q.message.reply_text("✅ Payment confirmed!\\n\\n" + PRODUCT_TEXT)
        elif status == "expired":
            await q.message.reply_text("⌛ This order expired. Tap Buy again for a fresh QR.")
        else:
            await q.message.reply_text(
                "⏳ Not detected yet — payments verify within ~30 sec. Hold on!")


def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(on_button))
    print("Merchant bot running...")
    app.run_polling()


if __name__ == "__main__":
    main()
'''

MERCHANT_WEB_JS = '''// FamPay Gateway - website integration.
// YOUR API KEY IS ALREADY FILLED IN BELOW. Just:
// 1) Set GATEWAY to your gateway's public URL (e.g. from Render).
// 2) Add <img id="qr"> and <div id="pay-box" style="display:none"> to your page.
// 3) Call buyNow() from your Buy button.

const GATEWAY = "https://YOUR-GATEWAY-URL"; // <-- replace with your gateway's public URL
const API_KEY = "{API_KEY}";                // your gateway API key (pre-filled ✅)
const PRICE = 99;                           // what you charge

async function buyNow() {
  // 1) create the order
  const r = await fetch(GATEWAY + "/api/create_order", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ api_key: API_KEY, amount: PRICE, note: "premium" }),
  }).then((x) => x.json());
  if (!r.success) {
    alert("Order failed: " + r.error);
    return;
  }

  // 2) show the QR code
  document.getElementById("qr").src = "data:image/png;base64," + r.qr_base64;
  document.getElementById("pay-box").style.display = "block";

  // 3) watch for the payment (auto-detect, ~30 sec)
  const timer = setInterval(async () => {
    const s = await fetch(
      GATEWAY + "/api/order_status?api_key=" + API_KEY + "&order_id=" + r.order_id
    ).then((x) => x.json());
    if (s.status === "paid") {
      clearInterval(timer);
      deliverProduct(); // <-- your code: unlock premium, show download, etc.
    } else if (s.status === "expired") {
      clearInterval(timer);
      alert("Order expired — please try again.");
    }
  }, 15000);
}

function deliverProduct() {
  document.getElementById("pay-box").innerHTML =
    "✅ Payment verified! Your premium is unlocked 🎉";
  // ... give the customer what they bought
}
'''

MERCHANT_WEBHOOK_PY = '''"""
FamPay Gateway - webhook receiver example.
Instead of polling /api/order_status, give the gateway your webhook URL
(User Panel > Webhook URL) and verified payments will POST here automatically.

YOUR API KEY IS ALREADY FILLED IN BELOW (used to verify the signature). Just:
1) pip install flask
2) Run: python fampay_webhook_server.py   (expose it publicly, e.g. via Render)
3) Paste your public URL into the bot: User Panel > Webhook URL
"""
import hmac
import hashlib
import json
from flask import Flask, request

API_KEY = "{API_KEY}"  # your gateway API key (pre-filled ✅)

app = Flask(__name__)


@app.route("/fampay-webhook", methods=["POST"])
def webhook():
    payload = request.get_json(force=True)
    sig = request.headers.get("X-Signature", "")
    expected = hmac.new(
        API_KEY.encode(),
        json.dumps(payload, sort_keys=True).encode(),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return "bad signature", 403
    if payload.get("event") == "payment.verified":
        order_id = payload["order_id"]
        amount = payload["amount_paise"] / 100
        print(f"PAID: {order_id} Rs.{amount} — deliver the product now!")
        # TODO: mark this order as delivered in YOUR database
    return "ok"


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
'''
