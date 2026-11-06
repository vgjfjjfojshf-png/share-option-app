"""Run ONCE on any computer (or Termux on Android) to log in to Telegram and print a session string.
   TG_API_ID=... TG_API_HASH=... python make_session.py
Put the printed value in the TG_SESSION_STRING environment variable of the listener service."""
import asyncio
import os

from telethon import TelegramClient
from telethon.sessions import StringSession


async def main() -> None:
    async with TelegramClient(StringSession(), int(os.environ["TG_API_ID"]), os.environ["TG_API_HASH"]) as client:
        print("\nTG_SESSION_STRING=" + client.session.save())


asyncio.run(main())
