import asyncio
import json
import logging

import httpx

log = logging.getLogger(__name__)


class Model:
    def __init__(self, settings):
        self.settings = settings
        self.local_embedding = None
        self.embedding_lock = asyncio.Lock()
        self.http = httpx.AsyncClient(timeout=90)

    async def close(self):
        await self.http.aclose()

    async def post(self, url, key, endpoint, payload):
        if not url:
            raise ValueError(f'Service is not configured: {endpoint}')
        headers = {'Authorization': f'Bearer {key}'} if key else {}
        attempts = 1 if endpoint in ('/images/generations', '/sdapi/v1/txt2img') else 3
        for attempt in range(attempts):
            response = await self.http.post(url.rstrip('/') + endpoint, headers=headers, json=payload)
            if response.status_code not in (429, 502, 503, 504) or attempt == attempts - 1:
                response.raise_for_status()
                return response.json()
            await asyncio.sleep(2 ** attempt)

    async def chat(self, messages, tools=None):
        payload = {'model': self.settings.model, 'messages': messages}
        if tools:
            payload['tools'] = tools
        result = await self.post(self.settings.llm_url, self.settings.llm_key,
                                 '/chat/completions', payload)
        message = result['choices'][0]['message']
        return {key: message[key] for key in ('role', 'content', 'tool_calls') if key in message}

    async def json(self, instruction, data):
        reply = await self.chat([
            {'role': 'system', 'content': instruction + '\nReturn only a JSON object.'},
            {'role': 'user', 'content': json.dumps(data, ensure_ascii=False)},
        ])
        text = (reply.get('content') or '').strip()
        if text.startswith('```'):
            text = text.split('\n', 1)[1].rsplit('```', 1)[0]
        result = json.loads(text)
        if not isinstance(result, dict):
            raise ValueError('Expected a JSON object')
        return result

    async def embed(self, text):
        if not self.settings.embedding_model:
            return None
        if self.settings.embedding_model.startswith('local:'):
            async with self.embedding_lock:
                def encode():
                    if self.local_embedding is None:
                        from fastembed import TextEmbedding
                        self.local_embedding = TextEmbedding(
                            model_name=self.settings.embedding_model.removeprefix('local:'),
                            cache_dir=str(self.settings.root / 'embedding-model'), threads=2)
                    return next(self.local_embedding.embed([text])).tolist()
                return await asyncio.to_thread(encode)
        result = await self.post(self.settings.embedding_url, self.settings.embedding_key,
                                 '/embeddings', {'model': self.settings.embedding_model, 'input': text})
        vector = result['data'][0]['embedding']
        if not vector or not all(isinstance(x, (int, float)) for x in vector):
            raise ValueError('Invalid embedding')
        return vector
