import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

from config import BLACKLIST_FILE, HISTORY_FILE, QUEUE_FILE
from processors.game_db import find_local_game

_lock = asyncio.Lock()


def _path(value: str) -> Path:
    return Path(value)


def _load(path: Path) -> list[dict]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, list) else []
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _save(path: Path, items: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(items, indent=2, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def _normalize(name: str) -> str:
    return " ".join(str(name).casefold().strip().split())


def _next_id(queue: list[dict], history: list[dict]) -> int:
    ids = []
    for item in queue + history:
        try:
            ids.append(int(item.get("id", 0)))
        except (TypeError, ValueError):
            pass
    return max(ids, default=0) + 1


async def _resolve_queue_url(game: dict) -> tuple[str | None, str | None]:
    """Prefer sdb.html, otherwise preserve the analyzer's original URL."""
    name = str(game.get("name", "")).strip()
    if name:
        try:
            sdb_match = await find_local_game(name)
            if sdb_match and sdb_match.url:
                return sdb_match.url, "kepardb"
        except Exception:
            pass

    original_url = (
        game.get("original_library_url")
        or game.get("library_url")
        or game.get("kepargamedb_url")
        or game.get("steam_url")
        or game.get("tgdb_url")
    )
    original_source = game.get("original_library_source") or game.get("library_source")
    return original_url, original_source


async def list_queue() -> list[dict]:
    async with _lock:
        queue = _load(_path(QUEUE_FILE))
        changed = False
        for item in queue:
            queue_url, queue_source = await _resolve_queue_url(item)
            if queue_url and item.get("queue_url") != queue_url:
                item["queue_url"] = queue_url
                item["queue_url_source"] = queue_source or "original"
                item["library_url"] = queue_url
                item["library_source"] = queue_source or item.get("library_source") or "original"
                changed = True
            elif queue_url and item.get("library_url") != queue_url:
                item["library_url"] = queue_url
                item["library_source"] = queue_source or item.get("library_source") or "original"
                changed = True
        if changed:
            _save(_path(QUEUE_FILE), queue)
        return queue


async def check_blacklist(game_name: str) -> dict | None:
    wanted = _normalize(game_name)
    async with _lock:
        for item in _load(_path(BLACKLIST_FILE)):
            if _normalize(item.get("name", "")) == wanted:
                return item
    return None


async def add_games(games: list[dict], requester_id: int | None = None, requester_name: str | None = None):
    async with _lock:
        queue = _load(_path(QUEUE_FILE))
        history = _load(_path(HISTORY_FILE))
        blacklist = _load(_path(BLACKLIST_FILE))
        blacklisted = {_normalize(item.get("name", "")): item for item in blacklist}
        existing = {_normalize(item.get("name", "")) for item in queue}
        added, blocked = [], []
        next_id = _next_id(queue, history)
        requested_at = datetime.now(timezone.utc).isoformat()

        for game in games:
            name = str(game.get("name", "")).strip()
            key = _normalize(name)
            if not key:
                continue
            if key in blacklisted:
                blocked_item = dict(blacklisted[key])
                blocked_item["attempted_name"] = name
                blocked.append(blocked_item)
                continue
            if key in existing:
                continue

            original_library_url = game.get("library_url")
            original_library_source = game.get("library_source")
            queue_url, queue_url_source = await _resolve_queue_url(game)
            item = {
                "id": next_id,
                "name": name,
                "queue_url": queue_url,
                "queue_url_source": queue_url_source or "original",
                "library_url": queue_url,
                "library_source": queue_url_source or original_library_source or "original",
                "original_library_url": original_library_url,
                "original_library_source": original_library_source,
                "kepargamedb_url": game.get("kepargamedb_url"),
                "steam_url": game.get("steam_url"),
                "steam_appid": game.get("steam_appid"),
                "tgdb_url": game.get("tgdb_url"),
                "tgdb_game_id": game.get("tgdb_game_id"),
                "selected_platform": game.get("selected_platform"),
                "console_platforms": list(game.get("console_platforms") or game.get("console_names") or []),
                "console_names": list(game.get("console_names") or game.get("console_platforms") or []),
                "pc_available": bool(game.get("pc_available", False)),
                "has_console": bool(game.get("has_console", bool(game.get("console_platforms") or game.get("console_names")))),
                "confidence": game.get("confidence", 0),
                "reason": game.get("reason", ""),
                "requester_id": requester_id,
                "requester_name": requester_name or (str(requester_id) if requester_id else "Unknown"),
                "requested_at": requested_at,
            }
            queue.append(item)
            added.append(item)
            existing.add(key)
            next_id += 1

        _save(_path(QUEUE_FILE), queue)
        return added, blocked


async def resolve_queue_item(identifier: str) -> dict | None:
    value = identifier.strip()
    async with _lock:
        queue = _load(_path(QUEUE_FILE))
        if value.isdigit():
            wanted_id = int(value)
            return next((item for item in queue if int(item.get("id", -1)) == wanted_id), None)
        wanted = _normalize(value)
        exact = next((item for item in queue if _normalize(item.get("name", "")) == wanted), None)
        return exact or next((item for item in queue if wanted in _normalize(item.get("name", ""))), None)


async def remove_queue_item(identifier: str, reason: str = "") -> dict | None:
    async with _lock:
        queue = _load(_path(QUEUE_FILE))
        history = _load(_path(HISTORY_FILE))
        value = identifier.strip()
        target_index = None
        if value.isdigit():
            wanted_id = int(value)
            for index, item in enumerate(queue):
                if int(item.get("id", -1)) == wanted_id:
                    target_index = index
                    break
        else:
            wanted = _normalize(value)
            for index, item in enumerate(queue):
                if _normalize(item.get("name", "")) == wanted or wanted in _normalize(item.get("name", "")):
                    target_index = index
                    break
        if target_index is None:
            return None
        item = queue.pop(target_index)
        item["action"] = "removed"
        item["reason"] = reason
        item["resolved_at"] = datetime.now(timezone.utc).isoformat()
        history.append(item)
        _save(_path(QUEUE_FILE), queue)
        _save(_path(HISTORY_FILE), history)
        return item


async def blacklist_game(identifier: str, reason: str) -> dict | None:
    async with _lock:
        queue = _load(_path(QUEUE_FILE))
        blacklist = _load(_path(BLACKLIST_FILE))
        value = identifier.strip()
        target_index = None
        if value.isdigit():
            wanted_id = int(value)
            for index, item in enumerate(queue):
                if int(item.get("id", -1)) == wanted_id:
                    target_index = index
                    break
        else:
            wanted = _normalize(value)
            for index, item in enumerate(queue):
                if _normalize(item.get("name", "")) == wanted:
                    target_index = index
                    break
        item = queue.pop(target_index) if target_index is not None else {"name": identifier}
        name = str(item.get("name", identifier)).strip()
        record = {
            "name": name,
            "reason": reason,
            "blacklisted_at": datetime.now(timezone.utc).isoformat(),
            "blacklisted_by": item.get("blacklisted_by"),
        }
        blacklist = [x for x in blacklist if _normalize(x.get("name", "")) != _normalize(name)]
        blacklist.append(record)
        _save(_path(BLACKLIST_FILE), blacklist)
        _save(_path(QUEUE_FILE), queue)
        return record
