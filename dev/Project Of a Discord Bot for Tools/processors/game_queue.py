import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

QUEUE_FILE = Path("data/game_download_queue.json")
HISTORY_FILE = Path("data/game_download_history.json")
BLACKLIST_FILE = Path("data/game_download_blacklist.json")
_lock = asyncio.Lock()


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


async def list_queue() -> list[dict]:
    async with _lock:
        return _load(QUEUE_FILE)


async def check_blacklist(game_name: str) -> dict | None:
    wanted = _normalize(game_name)
    async with _lock:
        blacklist = _load(BLACKLIST_FILE)
        for item in blacklist:
            name = _normalize(item.get("name", ""))
            if name == wanted:
                return item
        return None


async def add_games(
    games: list[dict],
    requester_id: int | None = None,
    requester_name: str | None = None,
) -> tuple[list[dict], list[dict]]:
    """Add games while refusing blacklisted titles.

    Returns (added, blocked). Blocked entries include the blacklist reason so
    the caller can tell the requester exactly why they were refused.
    """
    async with _lock:
        queue = _load(QUEUE_FILE)
        history = _load(HISTORY_FILE)
        blacklist = _load(BLACKLIST_FILE)
        blacklisted = {_normalize(item.get("name", "")): item for item in blacklist}
        existing = {_normalize(item.get("name", "")) for item in queue}
        added = []
        blocked = []
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
            item = {
                "id": next_id,
                "name": game.get("name"),
                "library_url": game.get("library_url"),
                "library_source": game.get("library_source"),
                "kepargamedb_url": game.get("kepargamedb_url"),
                "steam_url": game.get("steam_url"),
                "steam_appid": game.get("steam_appid"),
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

        _save(QUEUE_FILE, queue)
        return added, blocked


async def resolve_queue_item(identifier: str) -> dict | None:
    value = identifier.strip()
    async with _lock:
        queue = _load(QUEUE_FILE)
        if value.isdigit():
            wanted_id = int(value)
            return next((item for item in queue if int(item.get("id", -1)) == wanted_id), None)

        wanted = _normalize(value)
        exact = next((item for item in queue if _normalize(item.get("name", "")) == wanted), None)
        if exact:
            return exact
        return next((item for item in queue if wanted in _normalize(item.get("name", ""))), None)


async def remove_queue_item(identifier: str, reason: str = "") -> dict | None:
    """Remove an item from the active queue without blacklisting it."""
    async with _lock:
        queue = _load(QUEUE_FILE)
        history = _load(HISTORY_FILE)
        target_index = None
        value = identifier.strip()

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
            if target_index is None:
                for index, item in enumerate(queue):
                    if wanted in _normalize(item.get("name", "")):
                        target_index = index
                        break

        if target_index is None:
            return None

        item = queue.pop(target_index)
        item["action"] = "removed"
        item["reason"] = reason
        item["resolved_at"] = datetime.now(timezone.utc).isoformat()
        history.append(item)
        _save(QUEUE_FILE, queue)
        _save(HISTORY_FILE, history)
        return item


async def blacklist_game(identifier: str, reason: str) -> dict | None:
    """Blacklist a game and remove any matching active queue entry."""
    async with _lock:
        queue = _load(QUEUE_FILE)
        blacklist = _load(BLACKLIST_FILE)
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

        if target_index is not None:
            item = queue.pop(target_index)
        else:
            item = {"name": identifier}

        name = str(item.get("name", identifier)).strip()
        record = {
            "name": name,
            "reason": reason,
            "blacklisted_at": datetime.now(timezone.utc).isoformat(),
            "blacklisted_by": item.get("blacklisted_by"),
        }

        blacklist = [x for x in blacklist if _normalize(x.get("name", "")) != _normalize(name)]
        blacklist.append(record)
        _save(BLACKLIST_FILE, blacklist)
        _save(QUEUE_FILE, queue)
        return record
