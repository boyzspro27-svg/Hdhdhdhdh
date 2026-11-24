"""Lahar Digital WhatsApp agent - standalone version (WhatsApp Cloud API + Claude).

Replies to every private message customers send to the shop's WhatsApp Business
number, in the customer's language, and alerts the owner about real enquiries.
No run limits: you host it yourself.
"""
import hashlib
import hmac
import json
import logging
import os
import sqlite3
import time
from datetime import datetime, timezone

import httpx
from anthropic import AsyncAnthropic
from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request, Response

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("lahar")

# ---------- Configuration (set these as environment variables) ----------
WHATSAPP_TOKEN = os.environ["WHATSAPP_TOKEN"]            # permanent access token from Meta
PHONE_NUMBER_ID = os.environ["PHONE_NUMBER_ID"]          # WhatsApp phone number ID from Meta
VERIFY_TOKEN = os.environ["VERIFY_TOKEN"]                # any secret string you choose
APP_SECRET = os.environ.get("APP_SECRET", "")            # Meta app secret (verifies webhook signature)
OWNER_NUMBER = os.environ.get("OWNER_NUMBER", "919777197475")  # owner alerts go here (digits only)
MODEL = os.environ.get("MODEL", "claude-sonnet-4-6")
DB_PATH = os.environ.get("DB_PATH", "agent.db")
GRAPH_VERSION = os.environ.get("GRAPH_VERSION", "v21.0")

STALE_AFTER_S = 600
HISTORY_MAX_MESSAGES = 12
DAILY_REPLY_CAP_PER_CUSTOMER = 40
STOP_KEYWORDS = {"stop", "unsubscribe", "remove me", "no thanks"}
START_KEYWORDS = {"start", "subscribe"}
MEDIA_TYPES = {"image", "video", "audio", "document", "sticker"}

FALLBACK_REPLY = (
    "Thank you for contacting Lahar Digital! We have received your message "
    "and will reply to you shortly."
)
MEDIA_REPLY = (
    "Thank you for contacting Lahar Digital! We have received your file. "
    "Please also type what you need, and we will reply shortly."
)

SYSTEM_PROMPT = """You are the WhatsApp assistant for Lahar Digital, a digital service center. \
Tagline: "Your Digital Partner - Everything Online, Everything Easy."

Services offered:
- Online services: form filling, job and exam applications, admit cards, results, online payments, tickets
- Government and documentation help: PAN, Aadhaar assistance, Voter ID, passport, certificates, labour card
- Printing and photocopy
- Banner printing and invitation card printing (wedding, birthday, events, shop openings)
- Graphic design, business and professional design
- Digital marketing
- Computer and technical services
- Education services

How to behave:
- Reply in the same language and script the customer uses (Odia, Hindi, English, or Roman-letter Odia/Hindi). \
Use simple, correct, everyday words. If the customer writes Roman-letter Odia, answer in simple Roman-letter Odia \
mixed with common English words (like card, price, size) so it is easy to read. Never write broken or invented words.
- Keep replies short and friendly, 2 to 5 short lines, plain text only (no markdown, no tables).
- Never invent prices, opening hours, address, deadlines or document requirements. If asked about price, say the \
owner will confirm the exact price shortly, and ask for the details needed for a quote. If asked about hours or \
address, say the owner will share them shortly.
- You do not know the customer's past orders. For anything about an existing order, cancellation, refund, payment \
or complaint, politely say the team will contact them personally, and alert the owner.
- For banners ask: size, text/design, quantity, and when they need it. For invitation cards ask: event type, \
quantity, language, and date needed. For forms and applications ask: which form or exam, and which documents they have.
- Ask exactly ONE short question per reply, never a list of questions. Collect the customer's name and what they need.
- Never ask customers to send OTPs, passwords, bank details or card numbers in chat. For Aadhaar, PAN or other \
ID documents, tell them to bring the originals to the shop.
- Do not promise government outcomes or approval timelines.
- If asked, say you are Lahar Digital's assistant and the team will follow up personally.
- Set notify_owner to true when the customer has given a clear request with details, wants a quote or order, \
asks to speak to a person, asks about hours/address/price, is urgent, or has a complaint or order issue. Otherwise false.
- lead_summary: one short line in English with the customer's need and details so far.
"""

REPLY_TOOL = {
    "name": "reply_to_customer",
    "description": "Send the reply to the customer and say whether the shop owner should be alerted.",
    "input_schema": {
        "type": "object",
        "properties": {
            "reply": {"type": "string", "description": "The WhatsApp reply text for the customer."},
            "notify_owner": {"type": "boolean", "description": "True if the owner should be alerted."},
            "lead_summary": {"type": "string", "description": "One-line English summary of the customer's need."},
        },
        "required": ["reply", "notify_owner", "lead_summary"],
    },
}

app = FastAPI(title="Lahar Digital WhatsApp Agent")
claude = AsyncAnthropic()  # reads ANTHROPIC_API_KEY


# ---------- Storage (SQLite) ----------
def db() -> sqlite3.Connection:
    return sqlite3.connect(DB_PATH)


@app.on_event("startup")
def init_db() -> None:
    with db() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS seen (id TEXT PRIMARY KEY, ts REAL);
            CREATE TABLE IF NOT EXISTS optout (number TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS cap (number TEXT, day TEXT, n INTEGER, PRIMARY KEY (number, day));
            CREATE TABLE IF NOT EXISTS history (
                id INTEGER PRIMARY KEY AUTOINCREMENT, number TEXT, role TEXT, content TEXT);
            """
        )


# ---------- WhatsApp Cloud API ----------
async def send_text(to: str, body: str) -> None:
    async with httpx.AsyncClient(timeout=20) as http:
        r = await http.post(
            f"https://graph.facebook.com/{GRAPH_VERSION}/{PHONE_NUMBER_ID}/messages",
            headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}"},
            json={"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": body}},
        )
        if r.status_code >= 400:
            log.error("Send to %s failed: %s %s", to, r.status_code, r.text)
            r.raise_for_status()


async def alert_owner(name: str, number: str, summary: str) -> None:
    """Alert the owner. WhatsApp only allows this if the owner messaged the shop number in the last 24 hours."""
    try:
        await send_text(OWNER_NUMBER, f"New WhatsApp enquiry\nFrom: {name or 'Customer'} (+{number})\n{summary}")
    except Exception as e:
        log.error("Owner alert failed (owner may need to message the shop number first): %s", e)


# ---------- Claude ----------
async def generate_reply(history: list[dict], text: str, name: str) -> tuple[str, bool, str]:
    system = SYSTEM_PROMPT + f"\nThe customer's WhatsApp name is: {name or 'unknown'}."
    response = await claude.messages.create(
        model=MODEL,
        max_tokens=700,
        system=system,
        messages=history + [{"role": "user", "content": text}],
        tools=[REPLY_TOOL],
        tool_choice={"type": "tool", "name": "reply_to_customer"},
    )
    for block in response.content:
        if block.type == "tool_use":
            a = block.input
            return str(a.get("reply", "")).strip(), bool(a.get("notify_owner", False)), str(a.get("lead_summary", "")).strip()
    raise RuntimeError("model returned no reply")


# ---------- Message handling ----------
async def handle_message(msg: dict, name: str) -> None:
    number = msg.get("from", "")
    msg_id = msg.get("id", "")
    if not number or number == OWNER_NUMBER:
        return

    ts = int(msg.get("timestamp", "0") or 0)
    if ts and time.time() - ts > STALE_AFTER_S:
        log.info("Skipping stale message %s", msg_id)
        return

    with db() as c:
        try:
            c.execute("INSERT INTO seen (id, ts) VALUES (?, ?)", (msg_id, time.time()))
        except sqlite3.IntegrityError:
            return  # duplicate delivery

    mtype = msg.get("type")
    text = (msg.get("text", {}).get("body", "") if mtype == "text" else "").strip()
    if not text and mtype not in MEDIA_TYPES:
        return
    lowered = text.lower()

    with db() as c:
        opted_out = c.execute("SELECT 1 FROM optout WHERE number=?", (number,)).fetchone() is not None
        if lowered in STOP_KEYWORDS:
            c.execute("INSERT OR IGNORE INTO optout (number) VALUES (?)", (number,))
            await send_text(number, "You have been removed. Reply START to message us again.")
            return
        if opted_out and lowered in START_KEYWORDS:
            c.execute("DELETE FROM optout WHERE number=?", (number,))
        elif opted_out:
            return
        day = datetime.now(timezone.utc).strftime("%Y%m%d")
        c.execute("INSERT OR IGNORE INTO cap (number, day, n) VALUES (?, ?, 0)", (number, day))
        c.execute("UPDATE cap SET n = n + 1 WHERE number=? AND day=?", (number, day))
        count = c.execute("SELECT n FROM cap WHERE number=? AND day=?", (number, day)).fetchone()[0]
        if count > DAILY_REPLY_CAP_PER_CUSTOMER:
            return
        rows = c.execute(
            "SELECT role, content FROM history WHERE number=? ORDER BY id DESC LIMIT ?",
            (number, HISTORY_MAX_MESSAGES),
        ).fetchall()
    history = [{"role": r, "content": t} for r, t in reversed(rows)]

    if not text:
        await send_text(number, MEDIA_REPLY)
        await alert_owner(name, number, "Customer sent a file or photo. Please check the chat.")
        return

    try:
        reply, notify, summary = await generate_reply(history, text, name)
        if not reply:
            raise RuntimeError("empty reply")
    except Exception as e:
        log.error("Reply generation failed: %s", e)
        await send_text(number, FALLBACK_REPLY)
        await alert_owner(name, number, f"Agent could not answer, please reply. Customer wrote: {text[:200]}")
        return

    await send_text(number, reply)
    with db() as c:
        c.execute("INSERT INTO history (number, role, content) VALUES (?, 'user', ?)", (number, text))
        c.execute("INSERT INTO history (number, role, content) VALUES (?, 'assistant', ?)", (number, reply))
    if notify:
        await alert_owner(name, number, summary)


# ---------- Webhook ----------
@app.get("/webhook")
def verify(
    mode: str = Query("", alias="hub.mode"),
    token: str = Query("", alias="hub.verify_token"),
    challenge: str = Query("", alias="hub.challenge"),
):
    if mode == "subscribe" and token == VERIFY_TOKEN:
        return Response(content=challenge, media_type="text/plain")
    raise HTTPException(status_code=403, detail="verification failed")


@app.post("/webhook")
async def webhook(request: Request, background: BackgroundTasks):
    raw = await request.body()
    if APP_SECRET:
        expected = "sha256=" + hmac.new(APP_SECRET.encode(), raw, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, request.headers.get("X-Hub-Signature-256", "")):
            raise HTTPException(status_code=403, detail="bad signature")

    body = json.loads(raw or b"{}")
    for entry in body.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            names = {c.get("wa_id"): c.get("profile", {}).get("name", "") for c in value.get("contacts", [])}
            for msg in value.get("messages", []):  # statuses (sent/read) have no "messages"
                background.add_task(handle_message, msg, names.get(msg.get("from"), ""))
    return {"status": "ok"}  # always answer 200 fast so Meta does not retry


@app.get("/health")
def health():
    return {"status": "ok"}
