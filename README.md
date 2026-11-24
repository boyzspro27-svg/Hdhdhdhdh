# Lahar Digital WhatsApp Agent (standalone, no run limits)

Replies to every private message your customers send to your WhatsApp Business number, in their
language, and alerts you about real enquiries. Uses Meta's official WhatsApp Cloud API + Claude.

## 1. Meta setup (one time)
1. Go to developers.facebook.com, create an app (type: Business), add the **WhatsApp** product.
2. Add a phone number. It must NOT be active in the normal WhatsApp app. Either use a new number,
   or check Meta's "coexistence" option, which lets the Business app and the API share one number
   (available only in some countries, so check yours).
3. Copy the **Phone number ID**. Create a **System User** token (permanent) with
   `whatsapp_business_messaging` permission. Copy the app secret from App settings > Basic.

## 2. Host it
Any host that runs Python works (Render, Railway, a small VPS). Use a persistent disk for `agent.db`.
```
pip install -r requirements.txt
cp .env.example .env     # fill in the values, then export them as environment variables
uvicorn main:app --host 0.0.0.0 --port 8000
```
Needs HTTPS. Render and Railway give you that automatically.

## 3. Connect the webhook
In Meta: WhatsApp > Configuration > Webhook.
- Callback URL: `https://YOUR-HOST/webhook`
- Verify token: the same `VERIFY_TOKEN` you set
- Subscribe to the **messages** field

## 4. Test
Message your WhatsApp Business number from another phone. You should get a reply in seconds.

## Behaviour
- Private chats only; replies to every customer message, including first-time contacts
- Customer's language (Odia, Hindi, English, Roman-letter Odia/Hindi)
- Does not invent prices, hours or address; asks one question at a time
- STOP / START opt-out, duplicate and stale-message guards, 40 replies per customer per day
- Photos/files get a polite acknowledgement and an owner alert

## Know the limits
- **Owner alerts:** WhatsApp only lets the agent message you if you messaged the shop number in the
  last 24 hours. Send it a "Hi" daily, or ask me to add an approved message template for alerts.
- **24-hour window:** the agent can reply freely for 24 hours after a customer's message. Messaging
  customers first needs Meta-approved templates, and Meta charges per conversation.
- Edit prices, hours and services in `SYSTEM_PROMPT` inside `main.py`.
- Anthropic API usage is billed to your own Anthropic key.
