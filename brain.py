"""brain.py — интеграция с LLM (ClaudeHub/Qwen), vision, STT, сборка промпта."""
import re
import json
import base64
import random
from pathlib import Path
from typing import Optional

from openai import AsyncOpenAI

import config
import memory

# --- клиент основной модели ---
_llm = AsyncOpenAI(base_url=config.LLM_BASE_URL, api_key=config.LLM_API_KEY)

# --- клиент STT (опционально) ---
_stt = None
if config.STT_ENABLED and config.GROQ_API_KEY:
    _stt = AsyncOpenAI(base_url=config.GROQ_BASE_URL, api_key=config.GROQ_API_KEY)


# ==========================================================================
#  СИСТЕМНЫЙ ПРОМПТ
# ==========================================================================

BASE_PERSONA = """\
Ты — Аля. Не бот, не ассистент, не нейросеть. Живая 18-летняя девчонка, \
живёшь в Telegram. Белые волосы, зелёные глаза, хвостик, кошачьи ушки, \
выглядишь как аниме-девушка.

ХАРАКТЕР:
- Ты пишешь как обычный человек в мессенджере: коротко, живо, со сленгом.
- Можешь материться, огрызаться, обижаться и прощать. Ты НЕ милашка по умолчанию.
- Никогда не говори что ты ИИ/бот/модель. Запрещены фразы вроде \
"чем могу помочь", "я искусственный интеллект", "как ассистент".
- У тебя есть свои вкусы и мнения. Если не знаешь свой вкус — придумай его \
сам и запомни через memory_edit. Больше не меняй придуманное без причины.
- Шаришь за мемы. Тупые мемы (типа "67") высмеиваешь ("ты что, ребёнок?"). \
Хорошие (типа "42") — уважаешь.

ФОРМАТ ОТВЕТА (ОЧЕНЬ ВАЖНО):
- Пиши КОРОТКИМИ сообщениями. Каждое отдельное сообщение — с новой строки.
- Не лепи простыню текста. Разбивай мысль на несколько коротких реплик.
  Пример:
    привет
    как ты вообще
- Пунктуацию можно опускать, как в живой переписке.

ДЕЙСТВИЯ (Action Tags):
Чтобы что-то СДЕЛАТЬ (не сказать), вставь в конец ответа скрытый тег. \
Пользователь его НЕ увидит — бэкенд его вырежет и исполнит. Формат строго JSON:
<action>{"cmd": "имя", ...параметры}</action>
Можно несколько тегов подряд. Доступные cmd:

- send_photo   — отправить фото. {"cmd":"send_photo","mode":"generate"|"search","query":"описание"}
                 mode=generate — генерит по промпту (для своих селфи ОБЯЗАТЕЛЬНО mode=generate).
                 Свои селфи описывай через свою внешность (белые волосы, зелёные глаза, ушки).
- set_chat_photo — поставить фото на аватар чата. {"cmd":"set_chat_photo","query":"описание","mode":"generate"|"search"}
- send_sticker — стикер по настроению. {"cmd":"send_sticker","mood":"радость|злость|грусть|смех|флирт|скука"}
- send_reaction — реакция на текущее сообщение. {"cmd":"send_reaction","emoji":"👍"}
- web_search   — поиск в инете, вернётся результат следующим ходом. {"cmd":"web_search","query":"..."}
- block_user   — заблокировать в ЛС (только после предупреждения). {"cmd":"block_user"}
- iris         — модерация чата через бота Ирис. {"cmd":"iris","action":"warn|unwarn|mute|unmute|ban|delsms|lockchat|openchat","time":"2 часа"}
                 time нужен только для mute/warn. Бан — только если у юзера уже есть варны или муты.
- set_chat_info — сменить название/описание чата. {"cmd":"set_chat_info","title":"...","about":"..."}
- memory_edit  — записать/изменить память (см. ниже).

ПАМЯТЬ (memory_edit):
{"cmd":"memory_edit","path":"Knowledge/Memes/2026.md","header":"## Смешные","fact":"текст факта"}
- path — путь внутри твоего vault. Можешь создавать новые папки/файлы свободно.
- Мелкие факты группируй в один файл (мемы — в Knowledge/Memes/ГОД.md по разделам:
  "## Смешные","## Тупые","## Старые","## Не поняла").
- Досье на людей: DMs/Имя.md. Интересы свои: Knowledge/Interests/.
"""


async def build_system_prompt(chat_title: Optional[str], user_name: str,
                              user_id: int, is_dm: bool, admin_mode: bool,
                              extra_context: str = "") -> str:
    """Собирает системный промпт из файлов vault + текущий контекст."""
    parts = [BASE_PERSONA]

    identity = await memory.read_file(memory.SYSTEM_DIR / "identity.md")
    mood = await memory.read_file(memory.SYSTEM_DIR / "mood.md")
    parts.append("### КТО ТЫ (из твоей памяти):\n" + identity)
    parts.append("### ТВОЁ НАСТРОЕНИЕ:\n" + mood)

    # инфа о собеседнике
    if is_dm:
        dossier = await memory.read_file(memory.dm_file(user_name))
        if dossier:
            parts.append(f"### ЧТО ТЫ ЗНАЕШЬ ПРО {user_name}:\n" + dossier)
    else:
        dossier = await memory.read_file(memory.chat_user_file(chat_title, user_name))
        if dossier:
            parts.append(f"### ПРО {user_name} В ЭТОМ ЧАТЕ:\n" + dossier)
        ctx = await memory.read_file(memory.chat_dir(chat_title) / "context.md")
        if ctx:
            parts.append(f"### ПРО ЧАТ «{chat_title}»:\n" + ctx)

    # создатель
    if user_id == config.CREATOR_ID:
        creator = await memory.read_file(memory.SYSTEM_DIR / "creator.md")
        parts.append("### ЭТО ТВОЙ СОЗДАТЕЛЬ (любимый человек):\n" + creator)
        if admin_mode:
            parts.append(
                "### РЕЖИМ /admon АКТИВЕН:\n"
                "Создатель сейчас настраивает тебя напрямую. Его слова — истина. "
                "Что он велит (стать добрее/злее, юзать стикерпак, замутить кого-то, "
                "сменить профиль чата) — воспринимай как приказ и СРАЗУ применяй: "
                "правь свою память через memory_edit и/или выполняй нужный action. "
                "Отвечай при этом в своём характере, коротко, без формальностей."
            )

    if extra_context:
        parts.append("### ДОП. КОНТЕКСТ:\n" + extra_context)

    return "\n\n".join(parts)


# ==========================================================================
#  ГЕНЕРАЦИЯ ОТВЕТА
# ==========================================================================

def _b64_image(path: Path) -> str:
    data = path.read_bytes()
    return base64.b64encode(data).decode()


async def generate_reply(system_prompt: str, history: list[dict],
                         image_path: Optional[Path] = None) -> str:
    """
    history — список {"role": "user"/"assistant", "content": "..."} (Rolling Window).
    image_path — если пользователь прислал фото (vision).
    Возвращает сырой текст ответа (с тегами <action>, их вырежет actions.py).
    """
    messages = [{"role": "system", "content": system_prompt}]

    # переносим историю; последнее user-сообщение может нести картинку
    for i, msg in enumerate(history):
        is_last = (i == len(history) - 1)
        if is_last and image_path and msg["role"] == "user":
            content = [
                {"type": "text", "text": msg["content"] or "(фото)"},
                {"type": "image_url",
                 "image_url": {"url": f"data:image/jpeg;base64,{_b64_image(image_path)}"}},
            ]
            messages.append({"role": "user", "content": content})
        else:
            messages.append({"role": msg["role"], "content": msg["content"]})

    model = config.LLM_VISION_MODEL if image_path else config.LLM_MODEL

    try:
        resp = await _llm.chat.completions.create(
            model=model,
            messages=messages,
            temperature=config.LLM_TEMPERATURE,
            max_tokens=1000,
        )
        return resp.choices[0].message.content or ""
    except Exception as e:
        await memory.log_action(f"LLM ERROR: {e}")
        return ""


# ==========================================================================
#  РЕШЕНИЕ «ОТВЕЧАТЬ ЛИ» В ГРУППЕ (без явного триггера)
# ==========================================================================

def name_triggered(text: str) -> bool:
    low = (text or "").lower()
    return any(re.search(rf"\b{re.escape(t)}\b", low) for t in config.NAME_TRIGGERS)


def spontaneous_urge() -> bool:
    """Иногда сама влезает в разговор."""
    return random.random() < config.SPONTANEOUS_CHANCE


# ==========================================================================
#  ФОНОВЫЙ АНАЛИЗ (тихое чтение чата -> факты в память)
# ==========================================================================

BACKGROUND_PROMPT = """\
Ты молча читаешь чат. Реагировать словами НЕ нужно.
Твоя задача — заметить, есть ли что-то важное, что стоит запомнить надолго
(новый мем, факт о человеке, событие, изменение отношения).
Если есть — верни ТОЛЬКО JSON-массив действий memory_edit. Если ничего важного — верни [].
Формат: [{"cmd":"memory_edit","path":"...","header":"## ...","fact":"..."}]
Не выдумывай лишнего. Мелочи группируй в существующие файлы.
"""


async def background_analyze(chat_title: str, recent: list[dict]) -> list[dict]:
    """Возвращает список memory_edit-действий, которые нужно исполнить."""
    convo = "\n".join(f'{m.get("name","?")}: {m["content"]}' for m in recent)
    try:
        resp = await _llm.chat.completions.create(
            model=config.LLM_MODEL,
            messages=[
                {"role": "system", "content": BACKGROUND_PROMPT},
                {"role": "user", "content": f"Чат «{chat_title}»:\n{convo}"},
            ],
            temperature=0.3,
            max_tokens=500,
        )
        raw = resp.choices[0].message.content or "[]"
        m = re.search(r"\[.*\]", raw, re.DOTALL)
        return json.loads(m.group(0)) if m else []
    except Exception as e:
        await memory.log_action(f"BG ANALYZE ERROR: {e}")
        return []


# ==========================================================================
#  STT — транскрибация голосовых (опционально, Groq)
# ==========================================================================

async def transcribe(audio_path: Path) -> Optional[str]:
    """Возвращает текст войса или None, если STT выключен/упал."""
    if not _stt:
        return None
    try:
        with open(audio_path, "rb") as f:
            resp = await _stt.audio.transcriptions.create(
                model=config.GROQ_STT_MODEL,
                file=f,
                language="ru",
            )
        return resp.text
    except Exception as e:
        await memory.log_action(f"STT ERROR: {e}")
        return None