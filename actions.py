"""actions.py — парсинг <action>...</action> и исполнение команд Али."""
import re
import json
import random
import asyncio
import tempfile
from pathlib import Path
from urllib.parse import quote

import aiohttp

from pyrogram import Client
from pyrogram.types import Message

import config
import memory

ACTION_RE = re.compile(r"<action>\s*(\{.*?\})\s*</action>", re.DOTALL)


# ==========================================================================
#  ПАРСИНГ
# ==========================================================================

def parse_actions(raw: str) -> tuple[str, list[dict]]:
    """
    Вырезает все теги <action> из ответа LLM.
    Возвращает (чистый_текст_для_юзера, [список_действий]).
    """
    actions = []
    for m in ACTION_RE.finditer(raw):
        try:
            actions.append(json.loads(m.group(1)))
        except json.JSONDecodeError:
            # кривой JSON — пробуем чуть починить одинарные кавычки
            try:
                actions.append(json.loads(m.group(1).replace("'", '"')))
            except json.JSONDecodeError:
                pass
    clean = ACTION_RE.sub("", raw).strip()
    return clean, actions


# ==========================================================================
#  КАРТИНКИ: генерация (Pollinations) и поиск (ddgs)
# ==========================================================================

async def _generate_image(prompt: str) -> Path | None:
    """Генерация картинки через Pollinations (без ключа)."""
    seed = random.randint(1, 10_000_000)
    url = (f"https://image.pollinations.ai/prompt/{quote(prompt)}"
           f"?width=768&height=768&seed={seed}&nologo=true")
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, timeout=aiohttp.ClientTimeout(total=90)) as r:
                if r.status != 200:
                    return None
                data = await r.read()
        tmp = Path(tempfile.gettempdir()) / f"alya_{seed}.jpg"
        tmp.write_bytes(data)
        return tmp
    except Exception as e:
        await memory.log_action(f"IMG GEN ERROR: {e}")
        return None


async def _search_image(query: str) -> Path | None:
    """Поиск реального фото через ddgs."""
    try:
        from ddgs import DDGS
        with DDGS() as ddgs:
            results = list(ddgs.images(query, max_results=5))
        if not results:
            return None
        url = random.choice(results[:3])["image"]
        async with aiohttp.ClientSession() as s:
            async with s.get(url, timeout=aiohttp.ClientTimeout(total=30)) as r:
                if r.status != 200:
                    return None
                data = await r.read()
        tmp = Path(tempfile.gettempdir()) / f"alya_search_{random.randint(1,9999)}.jpg"
        tmp.write_bytes(data)
        return tmp
    except Exception as e:
        await memory.log_action(f"IMG SEARCH ERROR: {e}")
        return None


# Селфи всегда генерим с добавкой её внешности
SELFIE_STYLE = ("anime girl, white hair, green eyes, cat ears, ponytail, "
                "18 years old, cute, high quality, ")


async def _get_image(mode: str, query: str, selfie: bool = False) -> Path | None:
    if selfie or "селфи" in query.lower() or "selfie" in query.lower():
        return await _generate_image(SELFIE_STYLE + query)
    if mode == "search":
        return await _search_image(query)
    return await _generate_image(query)


# ==========================================================================
#  ИСПОЛНЕНИЕ ДЕЙСТВИЙ
# ==========================================================================

async def execute(client: Client, message: Message, action: dict,
                  sticker_catalog: dict, admin_mode: bool = False) -> None:
    """
    Исполняет одно действие. message — исходное сообщение-триггер
    (нужно для реплаев Ирису, реакций, определения чата/юзера).
    sticker_catalog — {mood: [file_id, ...]} из bot.py.
    """
    cmd = action.get("cmd")
    chat_id = message.chat.id
    try:
        # ---------- ФОТО ----------
        if cmd == "send_photo":
            path = await _get_image(action.get("mode", "generate"),
                                    action.get("query", ""),
                                    selfie=action.get("selfie", False))
            if path:
                await client.send_photo(chat_id, str(path))
                path.unlink(missing_ok=True)

        # ---------- АВАТАР ЧАТА ----------
        elif cmd == "set_chat_photo":
            path = await _get_image(action.get("mode", "search"),
                                    action.get("query", ""))
            if path:
                await client.set_chat_photo(chat_id, photo=str(path))
                path.unlink(missing_ok=True)
                await memory.log_action(f"Сменила аву чата {chat_id}")

        # ---------- НАЗВАНИЕ / ОПИСАНИЕ ----------
        elif cmd == "set_chat_info":
            if action.get("title"):
                await client.set_chat_title(chat_id, action["title"])
            if action.get("about"):
                await client.set_chat_description(chat_id, action["about"])
            await memory.log_action(f"Сменила инфо чата {chat_id}")

        # ---------- СТИКЕР ----------
        elif cmd == "send_sticker":
            mood = action.get("mood", "").lower()
            pool = sticker_catalog.get(mood) or sum(sticker_catalog.values(), [])
            if pool:
                await client.send_sticker(chat_id, random.choice(pool))

        # ---------- РЕАКЦИЯ ----------
        elif cmd == "send_reaction":
            emoji = action.get("emoji", "👍")
            try:
                await client.send_reaction(chat_id, message.id, emoji)
            except Exception:
                pass  # реакция может быть недоступна в чате

        # ---------- ВЕБ-ПОИСК (результат вернём в bot.py) ----------
        elif cmd == "web_search":
            return  # обрабатывается отдельно (см. web_search())

        # ---------- БЛОК В ЛС ----------
        elif cmd == "block_user":
            uid = message.from_user.id
            if uid == config.CREATOR_ID:
                return  # Создателя не блокируем никогда
            if await memory.dm_warned(uid):
                await client.block_user(uid)
                await memory.set_block(uid, config.DM_BLOCK_DAYS)
                await memory.log_action(f"Заблокировала {uid} на {config.DM_BLOCK_DAYS} дн")
            else:
                await memory.add_dm_warn(uid)
                await client.send_message(uid, "я предупреждаю. ещё раз и в бан")

        # ---------- ИРИС (модерация) ----------
        elif cmd == "iris":
            await _iris(client, message, action)

        # ---------- ПАМЯТЬ ----------
        elif cmd == "memory_edit":
            await _memory_edit(action)

    except Exception as e:
        await memory.log_action(f"ACTION ERROR ({cmd}): {e}")


async def _iris(client: Client, message: Message, action: dict) -> None:
    """Отправляет реплаем на нарушителя команду Ирису."""
    act = action.get("action")
    key = act
    if key not in config.IRIS_COMMANDS:
        await memory.log_action(f"Неизвестная iris-команда: {act}")
        return

    chat_id = message.chat.id
    target = message.reply_to_message or message
    target_user = target.from_user
    target_id = target_user.id if target_user else None

    # Создателя не трогаем
    if target_id == config.CREATOR_ID:
        return

    # Бан — только если уже были варны/муты
    if act == "ban" and target_id is not None:
        if not await memory.can_ban(chat_id, target_id):
            await memory.log_action(f"Бан {target_id} отклонён: не было варнов/мутов")
            return

    cmd_text = config.IRIS_COMMANDS[act]
    time = action.get("time")
    if act in ("mute", "warn") and time:
        cmd_text = f"{cmd_text} {time}"

    # отправляем реплаем на сообщение нарушителя
    await client.send_message(chat_id, cmd_text, reply_to_message_id=target.id)

    # обновляем числовое состояние
    if target_id is not None:
        if act == "warn":
            await memory.add_warn(chat_id, target_id)
        elif act == "mute":
            await memory.mark_mute(chat_id, target_id)
        elif act == "ban":
            await memory.clear_user(chat_id, target_id)
    await memory.log_action(f"Iris '{cmd_text}' -> {target_id} в {chat_id}")


async def _memory_edit(action: dict) -> None:
    """Запись факта в vault. Путь относительный внутри vault."""
    rel = (action.get("path") or "").lstrip("/")
    if not rel or ".." in rel:
        return  # защита от выхода за пределы vault
    path = config.VAULT_PATH / rel
    fact = action.get("fact", "").strip()
    header = action.get("header", "## Факты")
    if not fact:
        return
    added = await memory.add_fact(path, fact, header)
    await memory.log_action(f"memory_edit {rel}: {'+' if added else 'dup'} {fact[:40]}")


# ==========================================================================
#  ВЕБ-ПОИСК (вызывается из bot.py, результат идёт следующим ходом в LLM)
# ==========================================================================

async def web_search(query: str, max_results: int = 5) -> str:
    """Возвращает текстовую выжимку результатов для подстановки в промпт."""
    try:
        from ddgs import DDGS
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=max_results))
        if not results:
            return "(ничего не нашла)"
        out = []
        for r in results:
            out.append(f"- {r.get('title','')}: {r.get('body','')}")
        return "\n".join(out)
    except Exception as e:
        await memory.log_action(f"WEB SEARCH ERROR: {e}")
        return "(поиск не сработал)"