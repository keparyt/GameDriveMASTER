import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

from config import BLACKLIST_FILE, HISTORY_FILE, QUEUE_FILE
from processors.download_sources import find_download_source

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


async def _resolve_download_info(game: dict) -> dict:
    """Resolve queue download data exclusively from the local JSON sources."""
    name = str(game.get("name", "")).strip()
    match = await find_download_source(name) if name else None

    if match is None:
        return {
            "download_url": None,
            "download_uris": [],
            "download_source": None,
            "download_title": None,
            "file_size": None,
            "upload_date": None,
            "download_source_status": "not_found",
        }

    return {
        # download_url remains the backwards-compatible primary target.
        "download_url": match.primary_uri,
        # Preserve every valid URI from the source entry.
        "download_uris": list(match.uris),
        "download_source": match.source,
        "download_title": match.title,
        "file_size": match.file_size,
        "upload_date": match.upload_date,
        "download_source_status": "matched",
    }


async def _apply_download_info(item: dict) -> bool:
    """Refresh JSON-source metadata and report whether persisted data changed."""
    info = await _resolve_download_info(item)
    changed = any(item.get(key) != value for key, value in info.items())

    # Preserve the existing queue/history schema for consumers that still read
    # library_url/library_source, but make those fields aliases of the JSON
    # download target. They are never used as a fallback source.
    legacy = {
        "library_url": info["download_url"],
        "library_source": info["download_source"] or "none",
    }
    changed = changed or any(item.get(key) != value for key, value in legacy.items())
    item.update(info)
    item.update(legacy)
    return changed


async def list_queue() -> list[dict]:
    async with _lock:
        queue = _load(_path(QUEUE_FILE))
        changed = False
        for item in queue:
            # This is intentionally the only queue-level resolver. It never calls
            # the old SDB/Kepardb URL resolver and does not fall back to an input URL.
            if await _apply_download_info(item):
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

            download_info = await _resolve_download_info({"name": name})
            original_library_url = game.get("library_url")
            original_library_source = game.get("library_source")
            item = {
                "id": next_id,
                "name": name,
                **download_info,
                # Compatibility aliases now point only at the actual JSON
                # download target, never at SDB/Kepardb.
                "library_url": download_info["download_url"],
                "library_source": download_info["download_source"] or "none",
                # Keep original analysis metadata for history/debugging. These are
                # never used as a download-source fallback.
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
