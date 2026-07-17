"""gen_session.py — одноразовая генерация SESSION_STRING.
Запусти локально: python gen_session.py
Введи номер телефона и код из Telegram. Скопируй строку в .env -> SESSION_STRING.
"""
import asyncio
from pyrogram import Client

import os
from dotenv import load_dotenv

load_dotenv()

API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")


async def main():
    if not API_ID or not API_HASH:
        api_id = int(input("API_ID: "))
        api_hash = input("API_HASH: ")
    else:
        api_id, api_hash = API_ID, API_HASH

    async with Client(":memory:", api_id=api_id, api_hash=api_hash) as app:
        session = await app.export_session_string()
        print("\n=== SESSION_STRING (вставь в .env) ===\n")
        print(session)
        print("\n======================================")


if __name__ == "__main__":
    asyncio.run(main())