import argparse
import asyncio
import importlib.metadata
import json
import logging
from pathlib import Path
import shutil
import signal

from .config import load_settings
from .llm import Model
from .storage import Store
from .memory import Memory


async def doctor(settings, online=False):
    checks = {
        'tdlib': importlib.metadata.version('tdjson'),
        'ffmpeg': bool(shutil.which('ffmpeg')),
        'telegram_credentials': bool(settings.api_id and settings.api_hash),
        'creator_id': bool(settings.creator_id),
        'llm_configured': bool(settings.model and settings.llm_key),
        'embedding_configured': bool(settings.embedding_model),
        'stt_configured': bool(settings.stt_url),
        'tts_configured': bool(settings.tts_url),
        'stable_diffusion_configured': bool(settings.sd_url),
        'image_model': settings.image_model,
        'vault': str(settings.vault),
    }
    if online:
        model = Model(settings)
        try:
            try:
                reply = await model.chat([{'role': 'user', 'content': 'Reply with OK.'}])
                checks['llm_response'] = bool(reply.get('content'))
            except Exception as error:
                checks['llm_response'] = type(error).__name__
            try:
                vector = await model.embed('Проверка памяти')
                checks['embedding_dimensions'] = len(vector) if vector else 0
            except Exception as error:
                checks['embedding_response'] = type(error).__name__
        finally:
            await model.close()
    print(json.dumps(checks, ensure_ascii=False, indent=2))


async def run(settings, login_only=False):
    from .agent import Agent
    from .media import Media
    from .telegram import Telegram, message_text
    from .tools import Tools

    if not settings.api_id or not settings.api_hash or not settings.creator_id:
        raise ValueError('Set API_ID, API_HASH and CREATOR_ID in .env')
    if not login_only and (not settings.model or not settings.llm_key):
        raise ValueError('Set LLM_MODEL and LLM_API_KEY in .env')
    settings.root.mkdir(parents=True, exist_ok=True)
    # Prevent two processes using the same TDLib database and action queue.
    import fcntl
    with (settings.root / 'run.lock').open('w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('Alya is already using this data directory') from None
        telegram = Telegram(settings)
        model = Model(settings)
        store = Store(settings.root / 'state.sqlite3')
        memory = Memory(settings.vault, store, model)
        media = Media(settings, model, telegram)
        agent = Agent(settings, telegram, store, memory, model, media)
        agent.tools = Tools(settings, telegram, store, memory, media, agent.send)
        try:
            await telegram.start()
            print(f"Telegram подключён: {telegram.me['first_name']} (id {telegram.me['id']})")
            if login_only:
                return
            for row in store.pending():
                agent.schedule(row['chat'])
            agent.spawn(agent.periodic())
            print('Аля работает. Ctrl+C для остановки.')
            while not telegram.closed.is_set():
                try:
                    update = await asyncio.wait_for(telegram.updates.get(), 1)
                except TimeoutError:
                    continue
                kind = update.get('@type')
                try:
                    if kind == 'updateNewMessage':
                        await agent.incoming(update['message'])
                    elif kind == 'updateMessageSendSucceeded':
                        message = update['message']
                        with store.db:
                            store.db.execute('DELETE FROM messages WHERE chat=? AND id=?',
                                             (message['chat_id'], update['old_message_id']))
                        store.put_message(message, message_text(message))
                    elif kind == 'updateMessageSendFailed':
                        store.audit(update['message']['chat_id'], 'delivery', 'failed')
                        logging.error('Telegram delivery failed in chat %s', update['message']['chat_id'])
                except Exception:
                    logging.exception('Telegram update processing failed')
        finally:
            await agent.close()
            await telegram.close()
            await model.close()
            store.close()


def main():
    parser = argparse.ArgumentParser(description='Аля — Telegram-персонаж на TDLib')
    parser.add_argument('command', choices=('run', 'login', 'doctor', 'init'), nargs='?', default='run')
    parser.add_argument('--config', type=Path, default=Path(__file__).with_name('config.toml'))
    parser.add_argument('--online', action='store_true', help='Check LLM and embeddings using network requests')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
    logging.getLogger('httpx').setLevel(logging.WARNING)
    settings = load_settings(args.config)
    if args.command == 'init':
        store = Store(settings.root / 'state.sqlite3')
        Memory(settings.vault, store, None)
        store.close()
        print(f'Obsidian vault: {settings.vault}')
        return
    try:
        if args.command == 'doctor':
            asyncio.run(doctor(settings, args.online))
        else:
            asyncio.run(run(settings, args.command == 'login'))
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
