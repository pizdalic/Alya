"""Конфигурация Alya v2: env, константы, триггеры, синтаксис Ириса."""
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()


def _req(name: str) -> str:
    val = os.getenv(name)
    if not val:
        raise RuntimeError(f"Не задана обязательная переменная окружения: {name}")
    return val


# --- Telegram (my.telegram.org) ---
API_ID = int(_req("API_ID"))
API_HASH = _req("API_HASH")
SESSION_STRING = os.getenv("SESSION_STRING", "")

# --- Создатель ---
CREATOR_ID = int(os.getenv("CREATOR_ID", "4263059"))

# --- LLM (ClaudeHub, OpenAI-совместимый) ---
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://api.claudehub.fun/v1")
LLM_API_KEY = _req("LLM_API_KEY")
LLM_MODEL = os.getenv("LLM_MODEL", "qwen3.7-plus")
LLM_VISION_MODEL = os.getenv("LLM_VISION_MODEL", LLM_MODEL)  # qwen3.7-plus видит фото
LLM_REASONING_EFFORT = os.getenv("LLM_REASONING_EFFORT", "high")
LLM_TEMPERATURE = float(os.getenv("LLM_TEMPERATURE", "0.9"))

# --- STT (опционально, Groq whisper-large-v3) ---
STT_ENABLED = os.getenv("STT_ENABLED", "false").lower() == "true"
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_BASE_URL = os.getenv("GROQ_BASE_URL", "https://api.groq.com/openai/v1")
GROQ_STT_MODEL = os.getenv("GROQ_STT_MODEL", "whisper-large-v3")

# --- Vault ---
VAULT_PATH = Path(os.getenv("VAULT_PATH", "./alya_vault")).resolve()

# --- Iris (без префикса, команды уходят реплаем на нарушителя) ---
IRIS_BOT_USERNAME = os.getenv("IRIS_BOT_USERNAME", "iris_black_bot")
IRIS_COMMANDS = {
    "warn":     "варн",     # предупреждение
    "unwarn":   "-варн",    # снять варн
    "mute":     "мут",      # + время: "мут 2 часа"
    "unmute":   "-мут",     # снять мут
    "ban":      "бан",      # бан
    "delsms":   "-смс",     # удалить смс
    "lockchat": "-чат",     # запретить всем писать
    "openchat": "+чат",     # разрешить писать
}
# Допустимые варианты времени для мута/варна (то, что модель добавляет в args)
IRIS_TIME_UNITS = ["минута", "2 часа", "1 день", "3 дня"]

# --- Стикеры (short-name паков; можно пусто, тогда стикеров не будет) ---
STICKER_PACKS = [s.strip() for s in os.getenv("STICKER_PACKS", "").split(",") if s.strip()]

# --- Rolling Window ---
CONTEXT_WINDOW = 25
BACKGROUND_ANALYSIS_EVERY = 15   # каждые N молчаливых сообщений — фоновый анализ памяти

# --- Debounce (склейка быстрых сообщений) ---
DEBOUNCE_SECONDS = 1.8

# --- Триггеры имени в группах (любой регистр) ---
NAME_TRIGGERS = ["аля", "алечка", "аль", "алька", "alya", "alechka"]

# --- Шанс самой влезть в разговор без триггера ---
SPONTANEOUS_CHANCE = 0.07

# --- Тайминги гуманизатора (сек) ---
READ_DELAY_RANGE = (1.0, 3.0)
TYPING_CHARS_PER_SEC = 12.0
TYPING_MIN = 1.2
TYPING_MAX = 8.0
BETWEEN_MESSAGES_RANGE = (0.6, 1.8)

# --- Модерация ---
DM_WARN_BEFORE_BLOCK = 1          # сколько предупреждений в ЛС до блока
DM_BLOCK_DAYS = 14                # авто-разблок через N дней
CHAT_MAX_WARNS = 5                # максимум варнов на юзера в чате
# Логика: варны/муты — свободно; бан только после того, как были варны или муты.

# --- Настроение ---
MOOD_BLOCK_THRESHOLD = 8          # уровень злости (0-10), при котором Аля готова блокнуть
MOOD_DECAY_PER_HOUR = 1           # злость остывает со временем