import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from alya_next.agent import Agent
from alya_next.config import Settings
from alya_next.memory import Memory, cosine
from alya_next.storage import Store
from alya_next.tools import Tools


@pytest.fixture
def setup(tmp_path):
    settings = Settings(root=tmp_path, creator_id=42, debounce_seconds=0.01, read_delay=0,
                        typing_speed=100000)
    store = Store(tmp_path / 'state.db')
    model = SimpleNamespace(embed=AsyncMock(return_value=[1., 0.]),
                            settings=SimpleNamespace(embedding_model='test', embedding_url='test'))
    memory = Memory(tmp_path / 'vault', store, model)
    yield settings, store, model, memory
    store.close()


def message(i, chat=-100, sender=7):
    return {'id': i, 'chat_id': chat, 'sender_id': {'user_id': sender},
            'content': {'@type': 'messageText', 'text': {'text': f'text {i}'}}, 'date': i}


def test_persistence_and_pending(setup):
    settings, store, _, _ = setup
    store.put_message(message(1), 'hello')
    store.mark_pending(-100, 1)
    store.put_message(message(1), 'edited')
    reopened = Store(settings.root / 'state.db')
    assert reopened.pending(-100)[0]['text'] == 'edited'
    reopened.finish(-100, [1])
    assert not store.pending(-100)
    reopened.close()


@pytest.mark.asyncio
async def test_embeddings_scope_and_external_edits(setup):
    _, store, model, memory = setup
    note = await memory.remember(-100, 'любит чай')
    await memory.remember(-200, 'другой чат')
    assert len(await memory.search(-100, 'чай')) == 1
    assert model.embed.call_count == 2
    await memory.search(-100, 'чай')
    assert model.embed.call_count == 3  # cached note embedding
    (memory.root / note).write_text('любит кофе')
    result = await memory.search(-100, 'кофе')
    assert 'кофе' in result[0]['text']
    assert model.embed.call_count == 5
    assert cosine([1, 0], [1]) == 0


@pytest.mark.asyncio
async def test_consolidation_keeps_originals(setup):
    _, _, model, memory = setup
    for i in range(4):
        await memory.remember(-100, f'факт {i}')
    model.json = AsyncMock(return_value={'summary': 'Все факты'})
    assert await memory.consolidate(-100)
    assert len(list(memory.chat_dir(-100).glob('Archive/*.md'))) == 4
    assert len(list(memory.chat_dir(-100).glob('Summaries/*.md'))) == 1
    assert not await memory.consolidate(-100)


@pytest.mark.asyncio
async def test_permissions_enforced_before_telegram(setup):
    settings, store, _, memory = setup
    tg = SimpleNamespace(me={'id': 99}, call=AsyncMock(), chats={})
    tools = Tools(settings, tg, store, memory, None, AsyncMock())
    with pytest.raises(PermissionError):
        await tools.execute('send_message', {'chat_id': -200, 'text': 'secret'}, -100, False)
    with pytest.raises(PermissionError):
        await tools.execute('ban_user', {'user_id': 42, 'days': 0, 'reason': 'x'}, -100, True)
    with pytest.raises(PermissionError):
        await tools.execute('ban_user', {'user_id': 7, 'days': 0, 'reason': 'x'}, -100, False)
    assert tg.call.call_count == 0
    tg.call.return_value = {'status': {'@type': 'chatMemberStatusMember'}}
    with pytest.raises(PermissionError):
        await tools.execute('delete_message', {'message_id': 1, 'reason': 'spam'}, -100, True)
    assert tg.call.call_count == 1


@pytest.mark.asyncio
async def test_debounce_and_arrival_during_reply(setup):
    settings, store, model, memory = setup
    agent = Agent(settings, None, store, memory, model, None)
    batches = []
    started = asyncio.Event()
    release = asyncio.Event()
    async def turn(chat, rows):
        batches.append([r['id'] for r in rows])
        started.set()
        if len(batches) == 1:
            await release.wait()
    agent.turn = turn
    for i in (1, 2):
        store.put_message(message(i), 'test')
        store.mark_pending(-100, i)
        agent.schedule(-100)
    await asyncio.wait_for(started.wait(), 1)
    store.put_message(message(3), 'later')
    store.mark_pending(-100, 3)
    agent.schedule(-100)
    release.set()
    await asyncio.wait_for(asyncio.gather(*agent.workers.values()), 1)
    assert batches == [[1, 2], [3]]
    assert not store.pending(-100)
    assert not agent.workers


@pytest.mark.asyncio
async def test_failure_not_replayed(setup):
    settings, store, model, memory = setup
    agent = Agent(settings, None, store, memory, model, None)
    agent.turn = AsyncMock(side_effect=RuntimeError('delivery uncertain'))
    store.put_message(message(1), 'test')
    store.mark_pending(-100, 1)
    agent.schedule(-100)
    await asyncio.gather(*agent.workers.values())
    assert not store.pending(-100)
    assert store.db.execute('SELECT pending FROM messages').fetchone()[0] == 2
    assert agent.turn.call_count == 1
