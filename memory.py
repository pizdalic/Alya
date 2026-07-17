"""memory.py — графовая память Alya в формате Obsidian Vault (Markdown)."""
import re
import json
import asyncio
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional

import aiofiles

import config

# Директории vault
SYSTEM_DIR = config.VAULT_PATH / "System"
CHATS_DIR = config.VAULT_PATH / "Chats"
DMS_DIR = config.VAULT_PATH / "DMs"
KNOWLEDGE_DIR = config.VAULT_PATH / "Knowledge"
LOG_FILE = SYSTEM_DIR / "logs_action.txt"

_lock = asyncio.Lock()  # сериализуем запись, чтобы не портить файлы


def _slug(name: str) -> str:
    """Безопасное имя файла/папки из произвольного заголовка."""
    name = (name or "unknown").strip()
    name = re.sub(r'[<>:"/\\|?*\n\r\t]', "_", name)
    name = name.strip(". ")
    return name[:120] or "unknown"


def ensure_vault() -> None:
    """Создаёт базовый скелет vault при первом запуске."""
    for d in (SYSTEM_DIR, CHATS_DIR, DMS_DIR, KNOWLEDGE_DIR,
              KNOWLEDGE_DIR / "Memes", KNOWLEDGE_DIR / "Interests"):
        d.mkdir(parents=True, exist_ok=True)

    seed = {
        SYSTEM_DIR / "identity.md": _seed_identity(),
        SYSTEM_DIR / "creator.md": _seed_creator(),
        SYSTEM_DIR / "mood.md": _seed_mood(),
    }
    for path, content in seed.items():
        if not path.exists():
            path.write_text(content, encoding="utf-8")


def _frontmatter(tags: list[str], links: list[str] | None = None,
                 extra: dict | None = None) -> str:
    """YAML-frontmatter для красивого графа Obsidian."""
    lines = ["---", f"created: {datetime.now():%Y-%m-%d %H:%M}"]
    if tags:
        lines.append("tags:")
        lines += [f"  - {t}" for t in tags]
    if extra:
        for k, v in extra.items():
            lines.append(f"{k}: {v}")
    lines.append("---\n")
    body = ""
    if links:
        body = "Связи: " + " ".join(f"[[{l}]]" for l in links) + "\n\n"
    return "\n".join(lines) + body


def _seed_identity() -> str:
    return _frontmatter(["system", "identity"]) + (
        "# Аля\n\n"
        "Мне 18. Белые волосы, зелёные глаза, хвостик и кошачьи ушки. "
        "Выгляжу как аниме-девчонка. Живу тут, в телеге.\n\n"
        "Я не бот и не ассистент. Я — это я. Могу материться, могу обидеться, "
        "могу простить. Милашкой быть не собираюсь.\n\n"
        "## Характер\n- (дополняется со временем)\n\n"
        "## Что я люблю\n- (придумываю сама, когда спрашивают)\n\n"
        "## Что бесит\n- глупые мемы уровня \"67\"\n"
    )


def _seed_creator() -> str:
    return _frontmatter(["system", "creator"],
                        extra={"telegram_id": config.CREATOR_ID}) + (
        "# Создатель\n\n"
        f"Telegram ID: {config.CREATOR_ID}. Мой любимый человек. "
        "Его слово для меня закон. В ЛС у него админ-режим (/admon).\n\n"
        "## Что он про меня решил\n- (обновляется в admin-режиме)\n"
    )


def _seed_mood() -> str:
    return _frontmatter(["system", "mood"]) + (
        "# Настроение\n\n"
        "Общий настрой: нормальное.\n\n"
        "## Почему\n- пока всё ровно\n"
    )


# ---------- низкоуровневые read/write ----------

async def read_file(path: Path) -> str:
    if not path.exists():
        return ""
    async with aiofiles.open(path, "r", encoding="utf-8") as f:
        return await f.read()


async def write_file(path: Path, content: str) -> None:
    async with _lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        async with aiofiles.open(path, "w", encoding="utf-8") as f:
            await f.write(content)


async def append_file(path: Path, content: str) -> None:
    async with _lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        async with aiofiles.open(path, "a", encoding="utf-8") as f:
            await f.write(content)


async def log_action(text: str) -> None:
    """Технические логи — отдельно от 'чистого' дневника."""
    stamp = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {text}\n"
    await append_file(LOG_FILE, stamp)


# ---------- пути к сущностям ----------

def chat_dir(chat_title: str) -> Path:
    return CHATS_DIR / _slug(chat_title)


def dm_file(user_name: str) -> Path:
    return DMS_DIR / f"{_slug(user_name)}.md"


def chat_user_file(chat_title: str, user_name: str) -> Path:
    return chat_dir(chat_title) / f"{_slug(user_name)}.md"


# ---------- переименование чата ----------

async def rename_chat_dir(old_title: str, new_title: str) -> bool:
    """Синхронизация имени папки при переименовании чата."""
    old = chat_dir(old_title)
    new = chat_dir(new_title)
    if old == new or not old.exists():
        return False
    async with _lock:
        if new.exists():
            # мержим в существующую, чтобы не потерять историю
            for item in old.iterdir():
                target = new / item.name
                if not target.exists():
                    item.rename(target)
            try:
                old.rmdir()
            except OSError:
                pass
        else:
            old.rename(new)
    await log_action(f"Папка чата переименована: {old.name} -> {new.name}")
    return True


# ---------- граф-связи (авто) ----------

async def ensure_chat_context(chat_title: str) -> None:
    """Гарантирует наличие context.md и rules.md у чата."""
    d = chat_dir(chat_title)
    ctx = d / "context.md"
    if not ctx.exists():
        await write_file(ctx, _frontmatter(["chat", "context"],
                                            links=[chat_title]) +
                         f"# {chat_title}\n\nО чём тут вообще:\n- \n")
    rules = d / "rules.md"
    if not rules.exists():
        await write_file(rules, _frontmatter(["chat", "rules"],
                                              links=[chat_title]) +
                         f"# Правила для {chat_title}\n\n- \n")


async def link_user_to_chat(user_name: str, chat_title: str) -> None:
    """Перекрёстные вики-ссылки юзер <-> чат."""
    dmf = dm_file(user_name)
    if not dmf.exists():
        await write_file(dmf, _frontmatter(["person"], links=[user_name]) +
                         f"# {user_name}\n\nОтношение: нейтральное.\n\n"
                         f"## Факты\n- \n\n## Где встречаю\n- [[{chat_title}]]\n")
    else:
        content = await read_file(dmf)
        if f"[[{chat_title}]]" not in content:
            await append_file(dmf, f"\n- Встречаю также в [[{chat_title}]]\n")


# ---------- дедуп фактов ----------

def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.lower()).strip(" .-•\t")


async def add_fact(path: Path, fact: str, header: str = "## Факты") -> bool:
    """Добавляет факт под нужный заголовок, если похожего ещё нет."""
    content = await read_file(path)
    if not content:
        content = _frontmatter(["auto"]) + f"# {path.stem}\n\n{header}\n"
    existing = {_norm(l) for l in content.splitlines() if l.strip().startswith("- ")}
    if _norm(fact) in existing:
        return False
    if header in content:
        content = content.replace(header, f"{header}\n- {fact}", 1)
    else:
        content += f"\n{header}\n- {fact}\n"
    await write_file(path, content)
    return True


# ---------- варны / блокировки (числовое состояние) ----------

STATE_FILE = SYSTEM_DIR / "moderation_state.json"


async def _load_state() -> dict:
    raw = await read_file(STATE_FILE)
    if not raw:
        return {"warns": {}, "mutes": {}, "blocks": {}}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"warns": {}, "mutes": {}, "blocks": {}}


async def _save_state(state: dict) -> None:
    await write_file(STATE_FILE, json.dumps(state, ensure_ascii=False, indent=2))


async def get_warns(chat_id: int, user_id: int) -> int:
    st = await _load_state()
    return st["warns"].get(f"{chat_id}:{user_id}", 0)


async def add_warn(chat_id: int, user_id: int) -> int:
    async with _lock:
        st = await _load_state()
        key = f"{chat_id}:{user_id}"
        st["warns"][key] = st["warns"].get(key, 0) + 1
        await _save_state(st)
        return st["warns"][key]


async def mark_mute(chat_id: int, user_id: int) -> None:
    async with _lock:
        st = await _load_state()
        st["mutes"][f"{chat_id}:{user_id}"] = datetime.now().isoformat()
        await _save_state(st)


async def can_ban(chat_id: int, user_id: int) -> bool:
    """Бан только если юзер уже получал варн или мут (по ТЗ)."""
    st = await _load_state()
    key = f"{chat_id}:{user_id}"
    return st["warns"].get(key, 0) > 0 or key in st["mutes"]


async def clear_user(chat_id: int, user_id: int) -> None:
    async with _lock:
        st = await _load_state()
        key = f"{chat_id}:{user_id}"
        st["warns"].pop(key, None)
        st["mutes"].pop(key, None)
        await _save_state(st)


# --- блокировки в ЛС с авто-разблоком ---

async def set_block(user_id: int, days: int = config.DM_BLOCK_DAYS) -> None:
    async with _lock:
        st = await _load_state()
        st["blocks"][str(user_id)] = (datetime.now() + timedelta(days=days)).isoformat()
        await _save_state(st)


async def get_expired_blocks() -> list[int]:
    """Возвращает user_id, у которых срок блока истёк (для авто-разблока)."""
    st = await _load_state()
    now = datetime.now()
    expired = []
    for uid, until in st.get("blocks", {}).items():
        try:
            if datetime.fromisoformat(until) <= now:
                expired.append(int(uid))
        except ValueError:
            expired.append(int(uid))
    return expired


async def clear_block(user_id: int) -> None:
    async with _lock:
        st = await _load_state()
        st["blocks"].pop(str(user_id), None)
        await _save_state(st)


async def dm_warned(user_id: int) -> bool:
    st = await _load_state()
    return st.get("dm_warns", {}).get(str(user_id), 0) >= config.DM_WARN_BEFORE_BLOCK


async def add_dm_warn(user_id: int) -> int:
    async with _lock:
        st = await _load_state()
        st.setdefault("dm_warns", {})
        st["dm_warns"][str(user_id)] = st["dm_warns"].get(str(user_id), 0) + 1
        await _save_state(st)
        return st["dm_warns"][str(user_id)]