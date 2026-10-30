# FamPay Auto-Verification Gateway

Telegram bot + API that lets anyone **auto-verify FamPay UPI payments** on their website or their own bot.

> Note: FamPay has no official merchant API. This gateway verifies payments by reading
> FamPay/FamApp payment confirmation emails from the merchant's Gmail via IMAP.

## Flow
1. Merchant connects **Gmail** in the bot (App Password, stored encrypted)
2. Sets their **UPI ID** (their FamPay UPI ID)
3. Creates an **API Key**
4. Website/bot calls `POST /api/create_order` → UPI QR + link
5. Customer pays (any UPI app) → money goes straight to the merchant's FamPay
6. When the email arrives, the order auto-marks **paid** → webhook + Telegram notification

## Files
- `bot.py` — Telegram bot: User Panel (Gmail, UPI ID, API key, webhook, guide, test order, stats) + Admin Panel (stats, merchants, orders, broadcast)
- `api.py` — Flask API + background IMAP poller thread (rate limiting, SSRF-safe webhooks)
- `common.py` — storage, Fernet encryption, UPI QR, email parser, order logic
- `deploy/` — systemd units, `DEPLOY.md` — VPS guide

## API
**Create order** — `POST /api/create_order`
```json
{"api_key": "fp_...", "amount": 99, "note": "premium"}
```
→ `{"order_id": "ord_...", "upi_link": "upi://pay?...", "qr_base64": "...", "expires_in_sec": 900}`

**Order status** — `GET /api/order_status?api_key=fp_...&order_id=ord_...`
→ `{"status": "paid|pending|expired", "amount_paise": 9900, "utr": "..."}`

**Webhook** (optional): on verified payment we POST to your URL:
```json
{"event": "payment.verified", "order_id": "...", "amount_paise": 9900, "utr": "..."}
```
Header `X-Signature`: HMAC-SHA256 of body, secret = your API key.

**Rate limits**: 60 order creations/min per API key, 600 status checks/min per key, 300 req/min per IP (HTTP 429 when exceeded).

## Plans (monthly quotas, reset on the 1st)
- **FREE** — 60 payment requests/month — ₹0
- **STARTER** — 150/month — ₹80
- **BEST** — 250/month — ₹150
- **PREMIUM** — 500/month — ₹250

**Auto-buy:** merchants upgrade from the bot (My Plan → tap a plan → pay the UPI QR).
The payment is auto-detected from the admin's Gmail (same email-polling engine) and the
plan activates automatically — no manual step. Requires: admin sets **Sub. UPI**
(Admin Panel) and connects their own Gmail in User Panel.

Admin can also upgrade/downgrade manually: Admin Panel > Subscriptions > tap a merchant
(plan cycles free → starter → best → premium → free). Est. MRR shown on the dashboard.

## Support
- Channel: https://t.me/magservicesbots
- DM: @magsupportchat_bot

## Security
- Gmail App Passwords encrypted with Fernet (AES), file permissions 600
- Real passwords are never asked for; App Passwords can be revoked in Google anytime
- Per-merchant API keys with regenerate/revoke
- Webhook URLs must be public http(s) — private/loopback/internal addresses are blocked (SSRF guard)
- Admin panel locked to a single Telegram ID via `ADMIN_ID`
