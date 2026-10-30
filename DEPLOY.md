# FamPay Auto-Verification Gateway — VPS Deploy Guide

FamPay has **no official API**, so this gateway auto-verifies payments by reading
FamPay/FamApp payment confirmation emails from your Gmail.

## How it works
1. Merchant connects **Gmail** in the bot (via App Password, stored encrypted)
2. Sets their **UPI ID** (their FamPay UPI ID)
3. Creates an **API Key**
4. Website/bot calls `POST /api/create_order` → gets UPI QR + link
5. Customer pays from any UPI app → money goes straight to the merchant's FamPay account
6. When the email arrives (~5-30 sec) the order auto-marks **"paid"** → webhook fires + Telegram notification

## 1. Prepare the VPS
- Ubuntu 22.04/24.04 VPS

## 2. Upload files
```bash
sudo mkdir -p /opt/fampay-gateway
sudo unzip fampay-deploy.zip -d /opt/fampay-gateway
cd /opt/fampay-gateway
```

## 3. Python environment
```bash
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
```

## 4. Create .env (MOST IMPORTANT)
```bash
nano /opt/fampay-gateway/.env
```
```
PAYBOT_TOKEN=token_from_@BotFather
ADMIN_ID=your_telegram_numeric_id
PAY_EMAIL_SENDERS=fampay,famapp
ORDER_EXPIRY_MIN=15
POLL_INTERVAL_SEC=25
```

## 5. User + permissions
```bash
sudo useradd -r -s /bin/false fampay
sudo chown -R fampay:fampay /opt/fampay-gateway
sudo chmod 600 /opt/fampay-gateway/.env
```

## 6. Systemd services
```bash
sudo cp /opt/fampay-gateway/deploy/*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now fampay-bot fampay-api
```

## 7. Check
```bash
sudo systemctl status fampay-bot --no-pager
sudo systemctl status fampay-api --no-pager
curl http://127.0.0.1:5000/
```

## 8. How to create a Gmail App Password (for merchants)
1. Google Account > Security > turn **2-Step Verification ON**
2. **App passwords** > create new (name: "FamPay Gateway")
3. Copy the 16-character password into the bot's **Connect Gmail**
4. Never share your real Gmail password — an App Password can be revoked anytime

## 9. Make the API public
Nginx + certbot, or `ufw allow 5000/tcp` for testing.
Customers: `https://api.your-domain.com/api/create_order`

## 10. Email format note (IMPORTANT)
The parser looks for a **12-digit UTR** and a **₹ amount** in FamPay/FamApp emails.
If FamPay changes their email format, tweak `parse_payment_email()` in `common.py` and
`PAY_EMAIL_SENDERS` — test with one real payment first.

## Logs
```bash
sudo journalctl -u fampay-bot -f
sudo journalctl -u fampay-api -f
```
