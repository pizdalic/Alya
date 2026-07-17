"""bot.py — Alya v2: Pyrogram userbot, хэндлеры, Rolling Window, admin, точка входа."""
import asyncio
import tempfile
from collections import defaultdict, deque
from pathlib import Path
from typing import Optional

from pyrogram import Client, filters, idle
from pyrogram.enums import ChatType
from pyrogram.types import Message

import config
import memory
import brain
import actions
import humanizer

# ==========================================================================
#  ГЛОБАЛЬНОЕ СОСТОЯНИЕ
# ==========================================================================

app = Client(
    "alya",
    api_id=config.API_ID,
    api_hash=config.API_HASH,
    session_string=config.SESSION_STRING or None,
)

# Rolling Window: chat_id -> deque[{role, name, content, id}]
_windows: dict[int, deque] = defaultdict(lambda: deque(maxlen=config.CONTEXT_WINDOW))

# Счётчик молчаливых сообщений для фонового анализа
_silent_count: dict[int, int] = defaultdict(int)

# Debounce: chat_id -> задача отложенного ответа
_pending: dict[int, asyncio.Task] = {}

# Admin-режим (только Создатель в ЛС)
_admin_mode: bool = False

# Каталог стикеров: mood -> [file_id]
_sticker_catalog: dict[str, list[str]] = {}

# Кэш названий чатов (для отслеживания переименований)
_chat_titles: dict[int, str] = {}

# Данные аккаунта Али
_me_id: Optional[int] = None
_me_username: Optional[str] = None


# ==========================================================================
#  ВСПОМОГАТЕЛЬНОЕ
# ==========================================================================

def _display_name(user) -> str:
    if not user:
        return "Неизвестный"
    name = user.first_name or ""
    if user.last_name:
        name += f" {user.last_name}"
    return name.strip() or (user.username or f"id{user.id}")


async def _extract_text(message: Message) -> tuple[str, Optional[Path]]:
    """
    Возвращает (текст_для_LLM, путь_к_фото_или_None).
    Обрабатывает текст, подписи, голосовые, фото.
    """
    image_path = None
    text = message.text or message.caption or ""

    # голосовое
    if message.voice or message.audio:
        if config.STT_ENABLED:
            tmp = Path(tempfile.gettempdir()) / f"voice_{message.id}.ogg"
            await message.download(file_name=str(tmp))
            transcript = await brain.transcribe(tmp)
            tmp.unlink(missing_ok=True)
            if transcript:
                text = f'[Голосовое сообщение: "{transcript}"]'
            else:
                text = "[голосовое, не расслышала]"
        else:
            text = "[прислали голосовое]"

    # фото (для vision)
    if message.photo:
        tmp = Path(tempfile.gettempdir()) / f"img_{message.id}.jpg"
        await message.download(file_name=str(tmp))
        image_path = tmp

    return text, image_path


def _push_window(message: Message, role: str, text: str) -> None:
    """Добавляет сообщение в Rolling Window чата."""
    _windows[message.chat.id].append({
        "role": role,
        "name": _display_name(message.from_user) if role == "user" else "Аля",
        "content": text,
        "id": message.id,
    })


def _history_for_llm(chat_id: int) -> list[dict]:
    """Готовит историю в формате LLM (role/content)."""
    out = []
    for m in _windows[chat_id]:
        prefix = f"{m['name']}: " if m["role"] == "user" else ""
        out.append({"role": m["role"], "content": prefix + m["content"]})
    return out


# ==========================================================================
#  ОСНОВНОЙ ПАЙПЛАЙН ОТВЕТА
# ==========================================================================

async def _respond(message: Message, image_path: Optional[Path] = None) -> None:
    """Генерирует и отправляет ответ Али на сообщение."""
    chat = message.chat
    is_dm = chat.type == ChatType.PRIVATE
    user = message.from_user
    user_id = user.id if user else 0
    user_name = _display_name(user)
    chat_title = chat.title or user_name

    # синхронизация памяти чата
    if not is_dm:
        await memory.ensure_chat_context(chat_title)
        await memory.link_user_to_chat(user_name, chat_title)

    admin = _admin_mode and is_dm and user_id == config.CREATOR_ID

    # системный промпт
    sys_prompt = await brain.build_system_prompt(
        chat_title=None if is_dm else chat_title,
        user_name=user_name, user_id=user_id,
        is_dm=is_dm, admin_mode=admin,
    )

    history = _history_for_llm(chat.id)
    incoming_len = len(history[-1]["content"]) if history else 0

    # показываем "печатает", пока думаем
    await humanizer.show_busy(app, chat.id)

    raw = await brain.generate_reply(sys_prompt, history, image_path)
    if image_path:
        image_path.unlink(missing_ok=True)

    if not raw:
        return

    # --- цикл web_search: если попросила поиск, ищем и генерим заново ---
    clean, acts = actions.parse_actions(raw)
    search_acts = [a for a in acts if a.get("cmd") == "web_search"]
    if search_acts and not clean:
        results = []
        for sa in search_acts:
            res = await actions.web_search(sa.get("query", ""))
            results.append(f"Поиск '{sa.get('query')}':\n{res}")
        extra = "\n\n".join(results)
        sys_prompt2 = await brain.build_system_prompt(
            chat_title=None if is_dm else chat_title,
            user_name=user_name, user_id=user_id,
            is_dm=is_dm, admin_mode=admin,
            extra_context="Результаты поиска (используй, но отвечай своими словами):\n" + extra,
        )
        raw = await brain.generate_reply(sys_prompt2, history)
        clean, acts = actions.parse_actions(raw)

    # --- отправляем текст по-человечески ---
    parts = humanizer.split_message(clean)
    sent = await humanizer.send_humanized(app, message, parts, incoming_len)

    # запоминаем свой ответ в окне
    if clean:
        _windows[chat.id].append({
            "role": "assistant", "name": "Аля",
            "content": clean, "id": sent[0].id if sent else 0,
        })

    # --- исполняем действия (кроме web_search) ---
    for a in acts:
        if a.get("cmd") == "web_search":
            continue
        await actions.execute(app, message, a, _sticker_catalog, admin_mode=admin)


# ==========================================================================
#  DEBOUNCE (склейка быстрых сообщений)
# ==========================================================================

async def _debounced_respond(message: Message, image_path: Optional[Path]) -> None:
    """Ждём чуть-чуть — вдруг человек допишет ещё."""
    try:
        await asyncio.sleep(config.DEBOUNCE_SECONDS)
        await _respond(message, image_path)
    except asyncio.CancelledError:
        pass
    finally:
        _pending.pop(message.chat.id, None)


def _schedule_response(message: Message, image_path: Optional[Path]) -> None:
    """Отменяет предыдущий отложенный ответ и ставит новый."""
    old = _pending.get(message.chat.id)
    if old and not old.done():
        old.cancel()
    _pending[message.chat.id] = asyncio.create_task(
        _debounced_respond(message, image_path)
    )


# ==========================================================================
#  ХЭНДЛЕРЫ
# ==========================================================================

@app.on_message(filters.command(["admon", "admoff"], prefixes="/") & filters.private)
async def admin_toggle(client: Client, message: Message):
    """Включение/выключение admin-режима (только Создатель)."""
    global _admin_mode
    if not message.from_user or message.from_user.id != config.CREATOR_ID:
        return
    if message.command[0] == "admon":
        _admin_mode = True
        await message.reply("admin режим включён, слушаю тебя 🖤")
    else:
        _admin_mode = False
        await message.reply("окей, выключаюсь из режима настройки")
    await memory.log_action(f"Admin mode -> {_admin_mode}")


@app.on_message(filters.private & ~filters.me & ~filters.bot)
async def on_private(client: Client, message: Message):
    """Личка: отвечает всегда (если юзер не заблокирован)."""
    if not message.from_user:
        return
    user_id = message.from_user.id

    # заблокированного игнорим (авто-разблок отдельной задачей)
    if user_id != config.CREATOR_ID:
        st = await memory._load_state()
        if str(user_id) in st.get("blocks", {}):
            return

    text, image_path = await _extract_text(message)
    _push_window(message, "user", text)
    _schedule_response(message, image_path)


@app.on_message(filters.group & ~filters.me)
async def on_group(client: Client, message: Message):
    """Группы: тихо читает, отвечает по триггеру."""
    user = message.from_user
    if not user:
        return

    # отслеживаем переименование чата
    await _check_rename(message.chat)

    text, image_path = await _extract_text(message)
    _push_window(message, "user", text)

    # --- триггеры ---
    triggered = False
    # 1) реплай Але
    if message.reply_to_message and message.reply_to_message.from_user:
        if message.reply_to_message.from_user.id == _me_id:
            triggered = True
    # 2) имя
    if not triggered and brain.name_triggered(text):
        triggered = True
    # 3) @username
    if not triggered and _me_username and f"@{_me_username}".lower() in text.lower():
        triggered = True

    if triggered:
        _schedule_response(message, image_path)
        _silent_count[message.chat.id] = 0
        return

    # --- тихое чтение ---
    if image_path:
        image_path.unlink(missing_ok=True)

    # иногда сама влезает
    if brain.spontaneous_urge():
        _schedule_response(message, None)
        return

    # фоновый анализ памяти каждые N сообщений
    _silent_count[message.chat.id] += 1
    if _silent_count[message.chat.id] >= config.BACKGROUND_ANALYSIS_EVERY:
        _silent_count[message.chat.id] = 0
        asyncio.create_task(_background(message.chat.id, message.chat.title or "чат"))


async def _background(chat_id: int, chat_title: str):
    """Фоновый анализ окна -> сохранение фактов."""
    recent = list(_windows[chat_id])
    edits = await brain.background_analyze(chat_title, recent)
    for e in edits:
        if e.get("cmd") == "memory_edit":
            await actions._memory_edit(e)


async def _check_rename(chat):
    """Ловит переименование чата и синхронит папку в vault."""
    old = _chat_titles.get(chat.id)
    new = chat.title
    if new and old and old != new:
        await memory.rename_chat_dir(old, new)
    if new:
        _chat_titles[chat.id] = new


# ==========================================================================
#  ФОНОВЫЕ ЗАДАЧИ
# ==========================================================================

async def _auto_unblock_loop():
    """Раз в час снимает истёкшие блокировки в ЛС."""
    while True:
        try:
            for uid in await memory.get_expired_blocks():
                try:
                    await app.unblock_user(uid)
                except Exception:
                    pass
                await memory.clear_block(uid)
                await memory.log_action(f"Авто-разблок {uid}")
        except Exception as e:
            await memory.log_action(f"UNBLOCK LOOP ERROR: {e}")
        await asyncio.sleep(3600)


async def _load_stickers():
    """Выкачивает стикеры из паков и строит каталог по эмодзи-настроению."""
    mood_map = {
        "😀": "радость", "😄": "радость", "🙂": "радость", "😊": "радость",
        "😡": "злость", "😠": "злость", "🤬": "злость",
        "😢": "грусть", "😭": "грусть", "🥺": "грусть",
        "😂": "смех", "🤣": "смех", "😆": "смех",
        "😏": "флирт", "😉": "флирт", "😳": "флирт", "❤️": "флирт",
        "😐": "скука", "🙄": "скука", "😴": "скука",
    }
    for pack in config.STICKER_PACKS:
        try:
            stickers = await app.get_stickers(pack)
            for st in stickers:
                mood = mood_map.get(st.emoji or "", "радость")
                _sticker_catalog.setdefault(mood, []).append(st.file_id)
        except Exception as e:
            await memory.log_action(f"STICKER LOAD ERROR ({pack}): {e}")
    total = sum(len(v) for v in _sticker_catalog.values())
    await memory.log_action(f"Загружено стикеров: {total}")


# ==========================================================================
#  ТОЧКА ВХОДА
# ==========================================================================

async def main():
    global _me_id, _me_username

    memory.ensure_vault()
    await app.start()

    me = await app.get_me()
    _me_id = me.id
    _me_username = me.username
    print(f"Аля запущена как {me.first_name} (@{me.username}, id={me.id})")

    await _load_stickers()
    asyncio.create_task(_auto_unblock_loop())
    await memory.log_action("=== Аля запущена ===")

    await idle()
    await app.stop()


if __name__ == "__main__":
    asyncio.run(main())