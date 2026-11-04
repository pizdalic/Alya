import json
import time

from .telegram import compact_message, message_text


def tool(name, description, properties, required=None):
    return {'type': 'function', 'function': {'name': name, 'description': description,
            'parameters': {'type': 'object', 'properties': properties,
                           'required': required if required is not None else list(properties),
                           'additionalProperties': False}}}


INTEGER = {'type': 'integer'}
STRING = {'type': 'string'}
SCHEMAS = [
    tool('list_chats', 'List accessible chats with unread, pinned and muted state.', {}),
    tool('search_chats', 'Search accessible chats by title.', {'query': STRING}),
    tool('search_messages', 'Search message text in one accessible chat.', {'chat_id': INTEGER, 'query': STRING}),
    tool('view_messages', 'Read history around message_id, or latest messages with message_id=0.',
         {'chat_id': INTEGER, 'message_id': INTEGER}),
    tool('send_message', 'Send a short message. Cross-chat messages require owner request or configured proactive chat.',
         {'chat_id': INTEGER, 'text': STRING}),
    tool('edit_message', 'Edit your own message in the current chat.', {'message_id': INTEGER, 'text': STRING}),
    tool('forward_message', 'Forward a message. Only owner can move content between chats.',
         {'from_chat_id': INTEGER, 'message_id': INTEGER, 'to_chat_id': INTEGER}),
    tool('react', 'Add one emoji reaction to a message in the current chat.', {'message_id': INTEGER, 'emoji': STRING}),
    tool('ban_user', 'Ban a member using Telegram permissions. Give a reason. days=0 is permanent.',
         {'user_id': INTEGER, 'days': {'type': 'integer', 'minimum': 0, 'maximum': 366}, 'reason': STRING}),
    tool('delete_message', 'Delete a message in the current chat for everyone. Give a reason.',
         {'message_id': INTEGER, 'reason': STRING}),
    tool('set_member_tag', 'Set a member tag, max 16 characters. Empty clears it.',
         {'user_id': INTEGER, 'tag': {'type': 'string', 'maxLength': 16}}),
    tool('remember', 'Save a grounded fact/event to the current chat diary.', {'text': STRING}),
    tool('recall', 'Find relevant diary notes for the current chat.', {'query': STRING}),
    tool('search_stickers', 'Find installed stickers by emoji or query. Returns file IDs.', {'query': STRING}),
    tool('send_sticker', 'Send a sticker returned by search_stickers.', {'file_id': INTEGER}),
    tool('generate_image', 'Generate an image using the configured image provider, preserving your appearance for selfies.',
         {'prompt': STRING, 'selfie': {'type': 'boolean'}}),
    tool('speak', 'Send a voice message via configured TTS.', {'text': STRING}),
    tool('wait', 'Finish without sending a text reply when there is nothing to say.', {}),
]


class Tools:
    def __init__(self, settings, telegram, store, memory, media, send):
        self.settings, self.telegram, self.store = settings, telegram, store
        self.memory, self.media, self.send = memory, media, send

    def check_chat(self, chat, current, owner, write=False):
        if not self.settings.permits(chat):
            raise PermissionError('Chat is outside configured access')
        if chat != current and not owner:
            if not write or chat not in self.settings.proactive_chats:
                raise PermissionError('Cross-chat access requires a creator request')

    async def moderate(self, chat, owner, right):
        if not owner and chat not in self.settings.moderation_chats:
            raise PermissionError('Autonomous moderation is not enabled in this chat')
        member = await self.telegram.call('getChatMember', chat_id=chat,
            member_id={'@type': 'messageSenderUser', 'user_id': self.telegram.me['id']})
        status = member['status']
        if status['@type'] == 'chatMemberStatusCreator':
            return
        if status['@type'] != 'chatMemberStatusAdministrator' or not status.get('rights', {}).get(right):
            raise PermissionError('Telegram account lacks the required administrator right')

    async def execute(self, name, args, current, owner):
        schema = next((s['function']['parameters'] for s in SCHEMAS if s['function']['name'] == name), None)
        if schema is None or not isinstance(args, dict):
            raise ValueError('Unknown tool or invalid arguments')
        if set(args) != set(schema['required']):
            raise ValueError('Missing or extra tool arguments')
        for key, spec in schema['properties'].items():
            expected = {'string': str, 'integer': int, 'boolean': bool}[spec['type']]
            if type(args[key]) is not expected:
                raise ValueError('Invalid type for ' + key)
            if expected is str and len(args[key]) > spec.get('maxLength', 12000):
                raise ValueError('Argument too long: ' + key)
            if expected is int and not spec.get('minimum', -(2**63)) <= args[key] <= spec.get('maximum', 2**63-1):
                raise ValueError('Argument out of range: ' + key)
        self.check_chat(current, current, owner)
        self.store.audit(current, name, 'started')
        try:
            result = await self._execute(name, args, current, owner)
        except Exception:
            self.store.audit(current, name, 'failed')
            raise
        self.store.audit(current, name, 'ok')
        return result

    async def _execute(self, name, a, current, owner):
        tg = self.telegram
        if name in ('list_chats', 'search_chats'):
            result = []
            for chat in tg.chats.values():
                if not self.settings.permits(chat['id']) or (not owner and chat['id'] != current):
                    continue
                if name == 'search_chats' and a['query'].lower() not in chat.get('title', '').lower():
                    continue
                result.append({'id': chat['id'], 'title': chat.get('title'),
                               'unread': chat.get('unread_count', 0),
                               'pinned': any(p.get('is_pinned') for p in chat.get('positions', [])),
                               'muted': tg.is_muted(chat)})
            return result[:100]
        if name in ('view_messages', 'search_messages'):
            chat = a['chat_id']
            self.check_chat(chat, current, owner)
            if name == 'view_messages':
                result = await tg.history(chat, a['message_id'])
            else:
                result = await tg.call('searchChatMessages', chat_id=chat, query=a['query'],
                                       from_message_id=0, offset=0, limit=30)
            return [compact_message(m) for m in result.get('messages', [])]
        if name == 'send_message':
            self.check_chat(a['chat_id'], current, owner, write=True)
            return await self.send(a['chat_id'], a['text'])
        if name == 'edit_message':
            message = await tg.call('getMessage', chat_id=current, message_id=a['message_id'])
            if not message.get('is_outgoing'):
                raise PermissionError('Only own messages can be edited')
            result = await tg.call('editMessageText', chat_id=current, message_id=a['message_id'],
                                  input_message_content={'@type': 'inputMessageText',
                                  'text': {'@type': 'formattedText', 'text': a['text']}})
            self.store.put_message(result, message_text(result))
            return compact_message(result)
        if name == 'forward_message':
            for chat in (a['from_chat_id'], a['to_chat_id']):
                self.check_chat(chat, current, owner)
            result = await tg.call('forwardMessages', chat_id=a['to_chat_id'], from_chat_id=a['from_chat_id'],
                                   message_ids=[a['message_id']], send_copy=False, remove_caption=False)
            return [compact_message(m) for m in result.get('messages', []) if m]
        if name == 'react':
            return await tg.call('addMessageReaction', chat_id=current, message_id=a['message_id'],
                                 reaction_type={'@type': 'reactionTypeEmoji', 'emoji': a['emoji']},
                                 is_big=False, update_recent_reactions=True)
        if name in ('ban_user', 'set_member_tag'):
            if a['user_id'] in (self.settings.creator_id, tg.me['id']):
                raise PermissionError('Cannot moderate creator or self')
            right = 'can_restrict_members' if name == 'ban_user' else 'can_manage_tags'
            await self.moderate(current, owner, right)
            if name == 'set_member_tag':
                return await tg.call('setChatMemberTag', chat_id=current, user_id=a['user_id'], tag=a['tag'])
            return await tg.call('setChatMemberStatus', chat_id=current,
                member_id={'@type': 'messageSenderUser', 'user_id': a['user_id']},
                status={'@type': 'chatMemberStatusBanned',
                        'banned_until_date': int(time.time()) + a['days'] * 86400 if a['days'] else 0})
        if name == 'delete_message':
            await self.moderate(current, owner, 'can_delete_messages')
            return await tg.call('deleteMessages', chat_id=current, message_ids=[a['message_id']], revoke=True)
        if name == 'remember':
            return await self.memory.remember(current, a['text'])
        if name == 'recall':
            return await self.memory.search(current, a['query'])
        if name == 'search_stickers':
            result = await tg.call('getStickers', sticker_type={'@type': 'stickerTypeRegular'},
                                   query=a['query'], limit=12, chat_id=current)
            return [{'file_id': s['sticker']['id'], 'emoji': s.get('emoji')} for s in result.get('stickers', [])]
        if name == 'send_sticker':
            return compact_message(await tg.send_content(current, {'@type': 'inputMessageSticker',
                'sticker': {'@type': 'inputSticker', 'sticker': {'@type': 'inputFileId', 'id': a['file_id']}}}))
        if name == 'generate_image':
            appearance = (self.memory.root / 'Character' / 'appearance.md').read_text() if a['selfie'] else ''
            return compact_message(await self.media.generate_image(current, a['prompt'], appearance))
        if name == 'speak':
            return compact_message(await self.media.speak(current, a['text']))
        if name == 'wait':
            return {'finished': True}
        raise ValueError('Unknown tool')
