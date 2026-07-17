"""humanizer.py — очеловечивание: тайминги, статусы 'печатает', разбивка ответа."""
import re
import random
import asyncio

from pyrogram import Client
from pyrogram.enums import ChatAction
from pyrogram.types import Message

import config


# ==========================================================================
#  РАЗБИВКА ОТВЕТА НА КОРОТКИЕ СООБЩЕНИЯ
# ==========================================================================

def split_message(text: str) -> list[str]:
    """
    Бьёт ответ LLM на отдельные короткие реплики (как живая переписка).
    Приоритет — переносы строк (модель их сама ставит).
    Слишком длинные строки дополнительно режем по предложениям.
    """
    text = text.strip()
    if not text:
        return []

    # 1) по переносам строк — основной разделитель
    chunks = [c.strip() for c in text.split("\n") if c.strip()]

    result = []
    for chunk in chunks:
        # 2) если реплика длинная — режем по границам предложений
        if len(chunk) > 180:
            sentences = re.split(r"(?<=[.!?…])\s+", chunk)
            buf = ""
            for s in sentences:
                if len(buf) + len(s) < 180:
                    buf = f"{buf} {s}".strip()
                else:
                    if buf:
                        result.append(buf)
                    buf = s
            if buf:
                result.append(buf)
        else:
            result.append(chunk)

    # не больше 6 сообщений подряд, чтобы не выглядело как спам-простыня
    return result[:6]


# ==========================================================================
#  ТАЙМИНГИ
# ==========================================================================

def _read_delay(incoming_len: int) -> float:
    """Пауза 'прочитала' — чуть зависит от длины входящего сообщения."""
    base = random.uniform(*config.READ_DELAY_RANGE)
    return base + min(incoming_len / 200.0, 2.0)


def _typing_delay(text_len: int) -> float:
    """Сколько 'печатать' реплику — по скорости печати."""
    secs = text_len / config.TYPING_CHARS_PER_SEC
    return max(config.TYPING_MIN, min(secs, config.TYPING_MAX))


# ==========================================================================
#  ОТПРАВКА С ИМИТАЦИЕЙ ЖИВОГО ЧЕЛОВЕКА
# ==========================================================================

async def send_humanized(client: Client, message: Message, messages: list[str],
                         incoming_len: int = 0) -> list[Message]:
    """
    Полный цикл: прочитала -> пауза -> [печатает -> пауза -> отправка] x N.
    messages — список коротких реплик (из split_message).
    Возвращает список отправленных Message (например, для реакций).
    """
    if not messages:
        return []

    chat_id = message.chat.id
    sent = []

    # 1) помечаем прочитанным (Аля "увидела" сообщение)
    try:
        await client.read_chat_history(chat_id)
    except Exception:
        pass

    # 2) пауза "прочитала, думает"
    await asyncio.sleep(_read_delay(incoming_len))

    for i, part in enumerate(messages):
        # 3) статус "печатает..."
        typing = _typing_delay(len(part))
        try:
            await client.send_chat_action(chat_id, ChatAction.TYPING)
        except Exception:
            pass
        await asyncio.sleep(typing)

        # 4) отправка (реплаем — только первая реплика в группе)
        try:
            reply_to = message.id if (i == 0 and message.chat.type.name != "PRIVATE") else None
            m = await client.send_message(chat_id, part, reply_to_message_id=reply_to)
            sent.append(m)
        except Exception:
            pass

        # 5) пауза между репликами
        if i < len(messages) - 1:
            await asyncio.sleep(random.uniform(*config.BETWEEN_MESSAGES_RANGE))

    # сбрасываем статус
    try:
        await client.send_chat_action(chat_id, ChatAction.CANCEL)
    except Exception:
        pass

    return sent


async def show_busy(client: Client, chat_id: int,
                    action: ChatAction = ChatAction.TYPING) -> None:
    """Разовый статус — для долгих операций (генерация фото, поиск)."""
    try:
        await client.send_chat_action(chat_id, action)
    except Exception:
        pass