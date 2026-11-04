import asyncio
import getpass
import json
import logging
import platform
import uuid

log = logging.getLogger(__name__)


class TelegramError(RuntimeError):
    pass


class Telegram:
    def __init__(self, settings):
        import tdjson
        self.native = tdjson
        self.settings = settings
        tdjson.td_execute(json.dumps({'@type': 'setLogVerbosityLevel', 'new_verbosity_level': 0}).encode())
        self.client = tdjson.td_create_client_id()
        self.waiters = {}
        self.updates = asyncio.Queue()
        self.chats = {}
        self.users = {}
        self.scope_notifications = {}
        self.closed = asyncio.Event()
        self.receiver = None
        self.me = None

    async def start(self):
        self.receiver = asyncio.create_task(self._receive())
        await self.call('getOption', name='version')
        while True:
            update = await self.updates.get()
            if update.get('@type') != 'updateAuthorizationState':
                continue
            state = update['authorization_state']['@type']
            if state == 'authorizationStateWaitTdlibParameters':
                await self.call('setTdlibParameters',
                    database_directory=str(self.settings.root / 'telegram'),
                    files_directory=str(self.settings.root / 'downloads'),
                    database_encryption_key='', use_file_database=True,
                    use_chat_info_database=True, use_message_database=True,
                    use_secret_chats=False, api_id=self.settings.api_id,
                    api_hash=self.settings.api_hash, system_language_code='ru',
                    device_model='Alya', system_version=platform.system(), application_version='3.0')
            elif state == 'authorizationStateWaitPhoneNumber':
                phone = await asyncio.to_thread(input, 'Номер Telegram (+...): ')
                await self.call('setAuthenticationPhoneNumber', phone_number=phone.strip())
            elif state == 'authorizationStateWaitCode':
                code = await asyncio.to_thread(input, 'Код Telegram: ')
                await self.call('checkAuthenticationCode', code=code.strip())
            elif state == 'authorizationStateWaitPassword':
                password = await asyncio.to_thread(getpass.getpass, 'Пароль 2FA: ')
                await self.call('checkAuthenticationPassword', password=password)
            elif state == 'authorizationStateWaitOtherDeviceConfirmation':
                print('Подтвердите вход по ссылке: ' + update['authorization_state']['link'])
            elif state == 'authorizationStateReady':
                self.me = await self.call('getMe')
                for scope in ('notificationSettingsScopePrivateChats', 'notificationSettingsScopeGroupChats', 'notificationSettingsScopeChannelChats'):
                    self.scope_notifications[scope] = await self.call('getScopeNotificationSettings', scope={'@type': scope})
                try:
                    await self.call('loadChats', chat_list={'@type': 'chatListMain'}, limit=100)
                except TelegramError as error:
                    if '404' not in str(error):
                        raise
                return
            elif state in ('authorizationStateClosed', 'authorizationStateWaitRegistration'):
                raise TelegramError('Use an existing Telegram account')
            else:
                raise TelegramError(f'Unsupported authorization state: {state}')

    async def _receive(self):
        try:
            while not self.closed.is_set():
                raw = await asyncio.to_thread(self.native.td_receive, 0.25)
                if not raw:
                    continue
                event = json.loads(raw)
                if event.get('@client_id') != self.client:
                    continue
                extra = event.get('@extra')
                if extra:
                    waiter = self.waiters.get(extra)
                    if waiter and not waiter.done():
                        waiter.set_result(event)
                    continue
                kind = event.get('@type')
                if kind == 'updateNewChat':
                    self.chats[event['chat']['id']] = event['chat']
                elif kind == 'updateScopeNotificationSettings':
                    self.scope_notifications[event['scope']['@type']] = event['notification_settings']
                elif kind == 'updateUser':
                    self.users[event['user']['id']] = event['user']
                elif kind == 'updateChatPosition' and event['chat_id'] in self.chats:
                    chat = self.chats[event['chat_id']]
                    position = event['position']
                    chat['positions'] = [p for p in chat.get('positions', []) if p['list'] != position['list']]
                    chat['positions'].append(position)
                elif kind in ('updateChatNotificationSettings', 'updateChatReadInbox', 'updateChatTitle'):
                    chat = self.chats.get(event['chat_id'], {})
                    for field in ('notification_settings', 'unread_count', 'title'):
                        if field in event:
                            chat[field] = event[field]
                elif kind == 'updateAuthorizationState' and event['authorization_state']['@type'] == 'authorizationStateClosed':
                    self.closed.set()
                await self.updates.put(event)
        except Exception:
            log.exception('TDLib receive loop stopped')
            self.closed.set()
            for future in self.waiters.values():
                if not future.done():
                    future.set_exception(TelegramError('TDLib receive loop stopped'))

    async def call(self, method, **kwargs):
        if self.closed.is_set():
            raise TelegramError('TDLib is closed')
        key = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self.waiters[key] = future
        self.native.td_send(self.client, json.dumps({'@type': method, '@extra': key, **kwargs}).encode())
        try:
            result = await asyncio.wait_for(future, timeout=90)
        finally:
            self.waiters.pop(key, None)
        if result.get('@type') == 'error':
            raise TelegramError(f"{result.get('code')}: {result.get('message')}")
        return result

    async def close(self):
        if not self.closed.is_set():
            try:
                await self.call('close')
                await asyncio.wait_for(self.closed.wait(), 10)
            except (TelegramError, TimeoutError):
                log.warning('TDLib close timeout')
        self.closed.set()
        if self.receiver:
            await self.receiver

    def is_muted(self, chat):
        settings = chat.get('notification_settings', {})
        if not settings.get('use_default_mute_for', False):
            return settings.get('mute_for', 0) > 0
        kind = chat.get('type', {})
        if kind.get('@type') == 'chatTypePrivate':
            scope = 'notificationSettingsScopePrivateChats'
        elif kind.get('is_channel'):
            scope = 'notificationSettingsScopeChannelChats'
        else:
            scope = 'notificationSettingsScopeGroupChats'
        return self.scope_notifications.get(scope, {}).get('mute_for', 0) > 0

    async def get_chat(self, chat_id):
        chat = await self.call('getChat', chat_id=chat_id)
        self.chats[chat_id] = chat
        return chat

    async def send(self, chat, text, reply_to=0):
        content = {'@type': 'inputMessageText', 'text': {'@type': 'formattedText', 'text': text}}
        return await self.send_content(chat, content, reply_to)

    async def send_content(self, chat, content, reply_to=0):
        args = {'chat_id': chat, 'input_message_content': content}
        if reply_to:
            args['reply_to'] = {'@type': 'inputMessageReplyToMessage', 'message_id': reply_to}
        return await self.call('sendMessage', **args)

    async def history(self, chat, around=0, limit=30):
        return await self.call('getChatHistory', chat_id=chat, from_message_id=around,
                               offset=-limit//2 if around else 0, limit=limit, only_local=False)


def message_text(message):
    content = message.get('content', {})
    kind = content.get('@type', 'unknown')
    text = content.get('text', {}).get('text', '') or content.get('caption', {}).get('text', '')
    if kind == 'messageSticker':
        text += ' [стикер ' + content.get('sticker', {}).get('emoji', '') + ']'
    elif kind != 'messageText':
        text += f' [{kind}]'
    return text.strip()


def compact_message(message):
    return {'id': message['id'], 'chat_id': message['chat_id'], 'date': message.get('date'),
            'sender': message.get('sender_id'), 'text': message_text(message),
            'outgoing': message.get('is_outgoing', False),
            'delivery': 'queued' if message.get('sending_state') else 'confirmed', 'reply_to': message.get('reply_to')}
