"""FamBridge - Terms of Service & Privacy Policy.

Plain-text versions are shown inside the Telegram bot.
HTML versions are served by the API at /terms and /privacy.
"""

SERVICE_NAME = "FamBridge"
SUPPORT_CHANNEL = "https://t.me/magservicesbots"
SUPPORT_DM = "@magsupportchat_bot"
LAST_UPDATED = "30 October 2026"

# ---------------------------------------------------------------- terms ---
TERMS_TEXT = """📜 Terms of Service — FamBridge
Last updated: 30 October 2026

1. WHAT WE DO
FamBridge generates UPI payment requests (QR codes/links) for merchants and automatically verifies payments by reading payment-confirmation emails from the merchant's own Gmail inbox. We are a verification tool — NOT a payment processor. Money moves directly between your customer and your UPI ID; it never passes through us.

2. ELIGIBILITY
You must be legally able to enter contracts in your country and own a valid FamPay account to use this service.

3. YOUR RESPONSIBILITIES
• Connect the Gmail address that is linked to your FamPay account — verification only works with matching emails.
• Provide a correct UPI ID. Payments sent to a wrong UPI ID are your responsibility.
• Keep your API key and Gmail app password secret. Activity under your key is your responsibility.
• Use the service only for lawful goods and services. Fraud, scams, or illegal activity will get your access revoked.

4. PLANS & PAYMENTS
Free plan includes 60 payment verifications per month. Paid plans (Starter/Best/Premium) are billed monthly and activate automatically when your subscription payment is detected. Plan quotas reset on the 1st of each month. Fees are non-refundable except where required by law.

5. VERIFICATION DISCLAIMER
Verification depends on FamPay sending confirmation emails to your Gmail. Email delays or format changes may cause delayed or missed detections. We are not liable for business losses from delayed verification. Always confirm high-value payments manually.

6. ACCEPTABLE USE
No abuse of the API (rate limits apply), no attempts to breach other merchants' data, no reselling access without permission.

7. TERMINATION
We may suspend or terminate accounts for violations, abuse, or non-payment. You may stop using the service anytime by disconnecting your Gmail in the bot.

8. LIABILITY
The service is provided "as is". To the maximum extent allowed by law, our liability is limited to the fees you paid in the last 3 months.

9. CHANGES
We may update these terms; continued use after changes means acceptance. Major changes will be announced on our support channel: https://t.me/magservicesbots

Questions? DM @magsupportchat_bot"""

# --------------------------------------------------------------- privacy ---
PRIVACY_TEXT = """🔒 Privacy Policy — FamBridge
Last updated: 30 October 2026

1. DATA WE COLLECT
• Telegram user ID and username (account identity)
• Gmail address + app password (to read payment emails) — stored ENCRYPTED
• Your UPI ID (to generate payment QR codes)
• API keys, webhook URLs, your own bot token (if connected) — stored ENCRYPTED
• Order records: amount, status, UTR reference, timestamps
• Plan/subscription info

2. HOW WE USE IT
Only to operate the service: generating QR codes, detecting your payments, sending you notifications, and managing your plan. Nothing else.

3. GMAIL ACCESS
We connect via read-only IMAP and scan ONLY for FamPay/FamApp payment-confirmation emails. We do not read, store, or share your other emails.

4. SECURITY
Sensitive credentials (Gmail app passwords, bot tokens) are encrypted at rest and never logged in plain text. API keys are random tokens. Webhook URLs are validated to block unsafe destinations.

5. WHAT WE NEVER DO
We never sell your data. We never show ads. We never share your data with third parties — except sending webhooks to the URL YOU configured, and Telegram messages to YOU.

6. RETENTION & DELETION
Order history is kept for accounting while your account is active. Disconnecting your Gmail deletes the stored credentials immediately. For full account deletion, DM @magsupportchat_bot.

7. CONTACT
Support channel: https://t.me/magservicesbots
Direct: @magsupportchat_bot"""

# ------------------------------------------------------------------ html ---
_PAGE_STYLE = """
body{font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;max-width:760px;margin:0 auto;padding:32px 20px;color:#1a1a1a;line-height:1.65;background:#fafafa}
h1{font-size:28px;margin-bottom:4px}h2{font-size:19px;margin-top:28px;color:#0d47a1}
.meta{color:#666;font-size:14px;margin-bottom:24px}
.card{background:#fff;border:1px solid #e3e3e3;border-radius:12px;padding:24px 28px}
footer{margin-top:32px;font-size:13px;color:#888;text-align:center}
a{color:#0d47a1}
"""

def _html_page(title, body_text):
    paras = []
    for block in body_text.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        lines = block.split("\n")
        # numbered heading like "1. WHAT WE DO" -> h2, rest -> paragraphs
        first = lines[0].strip()
        import re
        if re.match(r"^\d+\.\s+[A-Z ]+$", first):
            paras.append(f"<h2>{first}</h2>")
            rest = "\n".join(lines[1:]).strip()
        else:
            rest = block
        if rest:
            rest = rest.replace("•", "<br>•")
            paras.append(f"<p>{rest}</p>")
    body = "\n".join(paras)
    return f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} — {SERVICE_NAME}</title><style>{_PAGE_STYLE}</style></head>
<body><div class="card"><h1>{title}</h1>
<div class="meta">{SERVICE_NAME} · Last updated: {LAST_UPDATED}</div>
{body}</div>
<footer>{SERVICE_NAME} · Support: <a href="{SUPPORT_CHANNEL}">Channel</a> · <a href="https://t.me/magsupportchat_bot">DM</a></footer>
</body></html>"""


def terms_html():
    return _html_page("Terms of Service", TERMS_TEXT.split("— FamBridge\n", 1)[-1])


def privacy_html():
    return _html_page("Privacy Policy", PRIVACY_TEXT.split("— FamBridge\n", 1)[-1])
