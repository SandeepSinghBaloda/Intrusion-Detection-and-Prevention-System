"""One-time interactive login. Creates the Telegram session file used by the server.

    python -m app.login

Asks for your phone number, the code Telegram sends you, and your 2FA password if set.
"""
from __future__ import annotations

import asyncio
import getpass
import os
import re

from telethon import TelegramClient

from .config import load_rules, load_settings


def normalise_phone(raw: str) -> str | None:
    """'+91 98765-43210' -> '+919876543210'; None if it doesn't look like a phone number."""
    digits = re.sub(r"[\s()+.-]", "", raw.strip())
    return "+" + digits if digits.isdigit() and 8 <= len(digits) <= 15 else None


def ask_phone() -> str:
    preset = normalise_phone(os.getenv("TG_PHONE", ""))
    if preset:
        print(f"Using phone number from .env: {preset}")
        return preset
    print("\nStep 1/3 - Your Telegram phone number, including country code.")
    while True:
        phone = normalise_phone(input("  Phone (e.g. +919876543210): "))
        if phone:
            return phone
        print("  That doesn't look like a phone number. Type it in full, starting with + and the country code.")


def ask_code() -> str:
    print("\nStep 2/3 - Telegram has sent a login code as a chat message from 'Telegram' in your app")
    print("           (check your phone or Telegram Desktop; it is usually not an SMS).")
    return input("  Code: ").strip()


def ask_password() -> str:
    print("\nStep 3/3 - Your account has two-step verification. Enter your Telegram cloud password.")
    return getpass.getpass("  Password (typing is hidden, press Enter when done): ")


async def main() -> None:
    s = load_settings()
    rules = load_rules(s.config_path)
    if not (s.api_id and s.api_hash):
        raise SystemExit("Set TG_API_ID and TG_API_HASH in .env first (https://my.telegram.org).")

    s.session_path.parent.mkdir(parents=True, exist_ok=True)
    client = TelegramClient(str(s.session_path), s.api_id, s.api_hash)
    await client.start(phone=ask_phone, code_callback=ask_code, password=ask_password)
    me = await client.get_me()
    print(f"\nLogged in as {me.first_name} (@{me.username or '-'}). Session: {s.session_path}.session")

    for bot in rules.bots:
        try:
            entity = await client.get_entity(bot.username)
        except Exception as exc:
            print(f"  @{bot.username}: NOT FOUND ({exc})")
            continue
        history = await client.get_messages(entity, limit=1)
        if history:
            print(f"  @{bot.username}: OK (chat exists)")
        elif bot.start_command:
            answer = input(f"  @{bot.username}: no chat yet. Send '{bot.start_command}' now? [y/N] ")
            if answer.strip().lower() == "y":
                await client.send_message(entity, bot.start_command)
                print(f"  @{bot.username}: sent {bot.start_command}")
        else:
            print(f"  @{bot.username}: OK (no messages yet - open the bot in Telegram and press Start)")

    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
