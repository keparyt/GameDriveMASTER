import asyncio
import json
from pathlib import Path

QUEUE_FILE = Path("data/game_download_queue.json")
_lock = asyncio.Lock()


def _load() -> list[dict]:
    try:
        return json.loads(QUEUE_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _save(items: list[dict]) -> None:
    QUEUE_FILE.parent.mkdir(parents=True, exist_ok=True)
    QUEUE_FILE.write_text(json.dumps(items, indent=2, ensure_ascii=False), encoding="utf-8")


async def list_queue() -> list[dict]:
    async with _lock:
        return _load()


async def add_games(games: list[dict]) -> list[dict]:
    """Add distinct games to the persistent download queue."""
    async with _lock:
        queue = _load()
        existing = {str(item.get("name", "")).casefold().strip() for item in queue}
        added = []
        for game in games:
            key = str(game.get("name", "")).casefold().strip()
            if not key or key in existing:
                continue
            item = {
                "name": game.get("name"),
                "steam_url": game.get("steam_url"),
                "steam_appid": game.get("steam_appid"),
                "confidence": game.get("confidence", 0),
                "reason": game.get("reason", ""),
            }
            queue.append(item)
            added.append(item)
            existing.add(key)
        _save(queue)
        return added
