"""
Share Option – Telegram listener (Telethon).

Env:  TG_API_ID, TG_API_HASH (from https://my.telegram.org), TG_CHANNEL_IDS (comma separated,
      e.g. -1001234567890,-1009876543210), ADMIN_SECRET, BACKEND_URL (default http://127.0.0.1:8000)
First run asks for your phone number / login code interactively and stores a session file.
"""
import asyncio
import logging
import os

import httpx
from telethon import TelegramClient, events
from telethon.sessions import StringSession

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("tg_listener")

API_ID = int(os.environ["TG_API_ID"])
API_HASH = os.environ["TG_API_HASH"]
CHANNELS = [int(c) for c in os.environ["TG_CHANNEL_IDS"].split(",") if c.strip()]
BACKEND_URL = os.getenv("BACKEND_URL", "http://127.0.0.1:8000").rstrip("/")
ADMIN_SECRET = os.environ["ADMIN_SECRET"]
SESSION = os.getenv("TG_SESSION", "share_option_listener")
SESSION_STRING = os.getenv("TG_SESSION_STRING")  # for hosts without a persistent disk: run make_session.py once


async def forward(http: httpx.AsyncClient, payload: dict, retries: int = 4) -> None:
    """POST to /api/signals/parse with exponential backoff (signals are time-critical, so keep it short)."""
    for attempt in range(1, retries + 1):
        try:
            r = await http.post(f"{BACKEND_URL}/api/signals/parse", json=payload,
                                headers={"X-Admin-Secret": ADMIN_SECRET})
            if r.status_code == 200:
                data = r.json()
                if data.get("status") == "OK":
                    log.info("Signal #%s %s %s | ready=%s blocked=%s", data["signal"]["id"],
                             data["signal"]["pair"], data["signal"]["direction"],
                             data["ready"], data["blocked"])
                else:
                    log.info("Message %s: %s (%s)", payload["message_id"], data.get("status"), data.get("reason"))
                return
            log.warning("Backend returned %s: %s", r.status_code, r.text[:200])
            if 400 <= r.status_code < 500:
                return  # client errors will not fix themselves
        except httpx.HTTPError as exc:
            log.warning("Attempt %d/%d failed: %s", attempt, retries, exc)
        await asyncio.sleep(min(0.5 * 2 ** (attempt - 1), 4))
    log.error("Dropped message %s after %d attempts", payload["message_id"], retries)


async def main() -> None:
    client = TelegramClient(StringSession(SESSION_STRING) if SESSION_STRING else SESSION, API_ID, API_HASH)
    async with httpx.AsyncClient(timeout=8.0) as http:

        @client.on(events.NewMessage(chats=CHANNELS))
        @client.on(events.MessageEdited(chats=CHANNELS))   # results are often appended by editing the signal
        async def on_message(event) -> None:
            try:
                text = (event.raw_text or "").strip()
                if not text:
                    return
                await forward(http, {"text": text[:2000], "channel_id": str(event.chat_id),
                                     "message_id": event.message.id})
            except Exception:
                log.exception("Handler error")

        await client.start()  # interactive login on first run
        log.info("Listening to %d channel(s): %s", len(CHANNELS), CHANNELS)
        await client.run_until_disconnected()  # Telethon auto-reconnects on network drops


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Stopped")
