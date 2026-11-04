import json
import sqlite3
import time
from pathlib import Path


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS messages (
                chat INTEGER, id INTEGER, sender INTEGER, outgoing INTEGER,
                text TEXT, raw TEXT, date INTEGER, pending INTEGER DEFAULT 0,
                PRIMARY KEY(chat, id));
            CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS embeddings (
                path TEXT PRIMARY KEY, digest TEXT, model TEXT, vector TEXT);
            CREATE TABLE IF NOT EXISTS audit (
                id INTEGER PRIMARY KEY, timestamp REAL, chat INTEGER, tool TEXT, status TEXT);
        ''')

    def close(self):
        self.db.close()

    def put_message(self, message, text):
        sender = message.get('sender_id', {}).get('user_id', 0)
        with self.db:
            self.db.execute('''INSERT INTO messages(chat,id,sender,outgoing,text,raw,date)
                VALUES(?,?,?,?,?,?,?) ON CONFLICT(chat,id) DO UPDATE SET
                text=excluded.text,raw=excluded.raw''',
                (message['chat_id'], message['id'], sender, message.get('is_outgoing', False),
                 text, json.dumps(message), message.get('date', int(time.time()))))

    def mark_pending(self, chat, message):
        with self.db:
            self.db.execute('UPDATE messages SET pending=1 WHERE chat=? AND id=?', (chat, message))

    def pending(self, chat=None):
        if chat is None:
            return self.db.execute('SELECT DISTINCT chat FROM messages WHERE pending=1').fetchall()
        return self.db.execute('SELECT * FROM messages WHERE chat=? AND pending=1 ORDER BY date,id', (chat,)).fetchall()

    def finish(self, chat, ids):
        with self.db:
            self.db.executemany('UPDATE messages SET pending=0 WHERE chat=? AND id=?', [(chat, i) for i in ids])

    def history(self, chat, limit=40):
        rows = self.db.execute('SELECT * FROM messages WHERE chat=? ORDER BY date DESC,id DESC LIMIT ?',
                               (chat, limit)).fetchall()
        return list(reversed(rows))

    def get(self, key, default=None):
        row = self.db.execute('SELECT value FROM kv WHERE key=?', (key,)).fetchone()
        return json.loads(row['value']) if row else default

    def set(self, key, value):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO kv VALUES(?,?)', (key, json.dumps(value)))

    def audit(self, chat, tool, status):
        with self.db:
            self.db.execute('INSERT INTO audit(timestamp,chat,tool,status) VALUES(?,?,?,?)',
                            (time.time(), chat, tool, status))
