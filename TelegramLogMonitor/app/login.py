"""One-time interactive login. Creates the Telegram session file used by the server.

    python -m app.login

Asks for your phone number, the code Telegram sends you, and your 2FA password if set.
"""
from __future__ import annotations

import asyncio

from telethon import TelegramClient

from .config import load_rules, load_settings


async def main() -> None:
    s = load_settings()
    rules = load_rules(s.config_path)
    if not (s.api_id and s.api_hash):
        raise SystemExit("Set TG_API_ID and TG_API_HASH in .env first (https://my.telegram.org).")

    s.session_path.parent.mkdir(parents=True, exist_ok=True)
    client = TelegramClient(str(s.session_path), s.api_id, s.api_hash)
    await client.start()
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
