import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

QUEUE_FILE = Path("data/game_download_queue.json")
HISTORY_FILE = Path("data/game_download_history.json")
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


async def add_games(
    games: list[dict],
    requester_id: int | None = None,
    requester_name: str | None = None,
) -> list[dict]:
    async with _lock:
        queue = _load(QUEUE_FILE)
        history = _load(HISTORY_FILE)
        existing = {str(item.get("name", "")).casefold().strip() for item in queue}
        added = []
        next_id = _next_id(queue, history)
        requested_at = datetime.now(timezone.utc).isoformat()

        for game in games:
            key = str(game.get("name", "")).casefold().strip()
            if not key or key in existing:
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
        return added


async def resolve_queue_item(identifier: str) -> dict | None:
    value = identifier.strip()
    async with _lock:
        queue = _load(QUEUE_FILE)
        if value.isdigit():
            wanted_id = int(value)
            return next((item for item in queue if int(item.get("id", -1)) == wanted_id), None)

        wanted = value.casefold()
        exact = next((item for item in queue if str(item.get("name", "")).casefold().strip() == wanted), None)
        if exact:
            return exact
        return next((item for item in queue if wanted in str(item.get("name", "")).casefold()), None)


async def complete_queue_item(identifier: str, action: str, reason: str = "") -> dict | None:
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
            wanted = value.casefold()
            for index, item in enumerate(queue):
                if str(item.get("name", "")).casefold().strip() == wanted:
                    target_index = index
                    break
            if target_index is None:
                for index, item in enumerate(queue):
                    if wanted in str(item.get("name", "")).casefold():
                        target_index = index
                        break

        if target_index is None:
            return None

        item = queue.pop(target_index)
        item["action"] = action
        item["reason"] = reason
        item["resolved_at"] = datetime.now(timezone.utc).isoformat()
        history.append(item)
        _save(QUEUE_FILE, queue)
        _save(HISTORY_FILE, history)
        return item
