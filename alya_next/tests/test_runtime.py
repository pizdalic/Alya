import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from alya_next.agent import Agent
from alya_next.config import Settings
from alya_next.media import Media
from alya_next.memory import Memory
from alya_next.storage import Store
from alya_next.tools import Tools


@pytest.mark.asyncio
async def test_normal_entrypoint_to_reply_and_memory(tmp_path):
    settings = Settings(root=tmp_path, creator_id=42, debounce_seconds=0, read_delay=0, typing_speed=100000)
    store = Store(tmp_path / 'state.db')
    model = SimpleNamespace(embed=AsyncMock(return_value=None),
        chat=AsyncMock(return_value={'role': 'assistant', 'content': 'привет'}),
        json=AsyncMock(return_value={'diary': 'Создатель поздоровался.', 'working': '', 'emotions': 'рада'}))
    tg = SimpleNamespace(is_muted=lambda c: False, history=AsyncMock(return_value={'messages': []}), me={'id': 99}, users={}, chats={42: {'id': 42, 'type': {'@type': 'chatTypePrivate'}}},
        call=AsyncMock(return_value={}), send=AsyncMock(return_value={'id': 20, 'chat_id': 42, 'is_outgoing': True,
            'sender_id': {'user_id': 99}, 'content': {'@type': 'messageText', 'text': {'text': 'привет'}}}))
    memory = Memory(tmp_path / 'vault', store, model)
    agent = Agent(settings, tg, store, memory, model, SimpleNamespace(inspect=AsyncMock()))
    agent.tools = Tools(settings, tg, store, memory, agent.media, agent.send)
    try:
        await agent.incoming({'id': 1, 'chat_id': 42, 'sender_id': {'user_id': 42},
                              'content': {'@type': 'messageText', 'text': {'text': 'привет'}}})
        await asyncio.wait_for(asyncio.gather(*agent.workers.values()), 3)
        tg.send.assert_awaited_once_with(42, 'привет', 0)
        assert not store.pending(42)
        assert memory.state(42)['emotions'] == 'рада'
        assert list(memory.chat_dir(42).glob('Diary/*.md'))
        assert len(store.history(42)) == 2
    finally:
        await agent.close()
        store.close()


@pytest.mark.asyncio
async def test_generated_file_survives_queued_upload(tmp_path):
    import base64
    settings = Settings(root=tmp_path, image_model='test')
    model = SimpleNamespace(post=AsyncMock(return_value={'data': [{'b64_json': base64.b64encode(b'image').decode()}]}))
    tg = SimpleNamespace(send_content=AsyncMock(return_value={'id': 1}))
    media = Media(settings, model, tg)
    await media.generate_image(42, 'tea', '')
    path = tg.send_content.call_args.args[1]['photo']['photo']['path']
    from pathlib import Path
    assert Path(path).read_bytes() == b'image'
