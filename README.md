# buyDips 📉

Tracks major stock indexes and sends you a WhatsApp message when one falls 1% or more from its previous close.

## Setup

```bash
cd ~/Documents/buyDips
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env   # then edit .env
.venv/bin/python app.py
```

Open http://127.0.0.1:5000.

## WhatsApp (Meta Cloud API, default)

1. At https://developers.facebook.com create an app (use case: connect with customers through WhatsApp).
2. In **WhatsApp → API Setup**, copy the **Phone number ID** and **access token**, and add your phone as a recipient.
3. Fill `META_ACCESS_TOKEN`, `META_PHONE_NUMBER_ID`, `META_TO` in `.env` and send a test (uses the `hello_world` template).
4. In WhatsApp Manager, create a **Utility** template named `buydips_alert` (English) with body
   `📉 buyDips alert: {{1}}. Open your buyDips dashboard for details.`
   Once approved, set `META_TEMPLATE_NAME=buydips_alert` and `META_TEMPLATE_LANG=en`.
5. The API Setup token expires in 24h. For a permanent one: Business Settings → System users → add an admin system user → assign the app → generate a token with `whatsapp_business_messaging` and `whatsapp_business_management`.

## WhatsApp (CallMeBot, optional)

1. Go to https://www.callmebot.com/blog/free-api-whatsapp-messages/ and save the CallMeBot WhatsApp number shown there in your contacts.
2. From WhatsApp, send it: `I allow callmebot to send me messages`
3. It replies with your API key. Put it in `.env` as `CALLMEBOT_APIKEY`, and set `CALLMEBOT_PHONE` to your number with country code.
4. Click **Send test WhatsApp** on the dashboard.

## WhatsApp (Twilio, optional)

Set `WHATSAPP_PROVIDER=twilio`. Note: Twilio trial accounts can only send fixed demo templates, so you need a paid account and an approved template for real alerts.


1. Create a free account at https://www.twilio.com.
2. In the console, go to **Messaging → Try it out → Send a WhatsApp message** and join the sandbox by sending the code (e.g. `join something-word`) from your phone to the sandbox number.
3. Put your Account SID, Auth Token, and phone number into `.env`.
4. Click **Send test WhatsApp** on the dashboard.

## Email (Gmail, free)

1. Turn on 2-Step Verification for your Google account.
2. Create an app password at https://myaccount.google.com/apppasswords (name it "buyDips").
3. In `.env` set `SMTP_USER` to your Gmail address and `SMTP_PASSWORD` to the 16-character app password.

Alerts go to every configured channel (WhatsApp and/or email).

## How it works

- Every `CHECK_INTERVAL_SECONDS` (default 5 min), it fetches prices from Yahoo Finance.
- If an index is down `DIP_THRESHOLD_PCT` (default 1%) or more vs. the previous close, it sends a WhatsApp alert.
- You get at most one alert per index per day (tracked in `alerts_state.json`).
- Edit `INDEXES` in `.env` to change which indexes are tracked (uses Yahoo Finance symbols).
