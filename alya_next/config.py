from dataclasses import dataclass, field
from pathlib import Path
import os
import tomllib

from dotenv import load_dotenv


@dataclass
class Settings:
    root: Path
    api_id: int = 0
    api_hash: str = ''
    creator_id: int = 0
    llm_url: str = 'https://api.openai.com/v1'
    llm_key: str = ''
    model: str = ''
    vision_model: str = ''
    embedding_url: str = ''
    embedding_key: str = ''
    embedding_model: str = ''
    stt_url: str = ''
    stt_key: str = ''
    stt_model: str = 'whisper-1'
    tts_url: str = ''
    tts_key: str = ''
    tts_model: str = 'tts-1'
    tts_voice: str = 'alloy'
    sd_url: str = ''
    image_url: str = ''
    image_key: str = ''
    image_model: str = ''
    allowed_chats: list[int] = field(default_factory=list)
    proactive_chats: list[int] = field(default_factory=list)
    moderation_chats: list[int] = field(default_factory=list)
    debounce_seconds: float = 1.8
    spontaneous_chance: float = 0.07
    proactive_interval: int = 900
    cooldown_seconds: int = 300
    context_messages: int = 40
    tool_rounds: int = 6
    consolidation_interval: int = 21600
    read_delay: float = 1.0
    typing_speed: float = 12.0
    max_media_mb: int = 20
    max_concurrent_chats: int = 2

    @property
    def vault(self):
        return self.root / 'vault'

    def permits(self, chat_id):
        return not self.allowed_chats or chat_id in self.allowed_chats or chat_id == self.creator_id


def load_settings(path: Path) -> Settings:
    load_dotenv()
    data = tomllib.loads(path.read_text()) if path.exists() else {}
    root = (path.parent / data.pop('data_directory', 'data')).resolve()
    settings = Settings(root=root, **data)
    env = {
        'api_id': 'API_ID', 'api_hash': 'API_HASH', 'creator_id': 'CREATOR_ID',
        'vision_model': 'VISION_MODEL', 'llm_url': 'LLM_BASE_URL', 'llm_key': 'LLM_API_KEY', 'model': 'LLM_MODEL',
        'embedding_url': 'EMBEDDING_BASE_URL', 'embedding_key': 'EMBEDDING_API_KEY',
        'embedding_model': 'EMBEDDING_MODEL', 'stt_url': 'STT_BASE_URL',
        'stt_key': 'STT_API_KEY', 'stt_model': 'STT_MODEL', 'tts_url': 'TTS_BASE_URL',
        'tts_key': 'TTS_API_KEY', 'tts_model': 'TTS_MODEL', 'tts_voice': 'TTS_VOICE',
        'sd_url': 'SD_BASE_URL', 'image_url': 'IMAGE_BASE_URL',
        'image_key': 'IMAGE_API_KEY', 'image_model': 'IMAGE_MODEL',
    }
    for name, key in env.items():
        if os.getenv(key):
            value = os.environ[key]
            setattr(settings, name, int(value) if name in ('api_id', 'creator_id') else value)
    if not settings.image_url:
        settings.image_url = settings.llm_url
    if not settings.image_key:
        settings.image_key = settings.llm_key
    if not settings.embedding_url:
        settings.embedding_url = settings.llm_url
    if not settings.embedding_key:
        settings.embedding_key = settings.llm_key
    if settings.debounce_seconds < 0 or settings.typing_speed <= 0:
        raise ValueError('Invalid debounce_seconds or typing_speed')
    if not 0 <= settings.spontaneous_chance <= 1:
        raise ValueError('spontaneous_chance must be between 0 and 1')
    if min(settings.tool_rounds, settings.context_messages, settings.max_concurrent_chats,
           settings.proactive_interval, settings.consolidation_interval) < 1:
        raise ValueError('Limits and intervals must be positive')
    return settings
