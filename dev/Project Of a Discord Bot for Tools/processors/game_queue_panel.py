import asyncio
import json
from pathlib import Path

from config import QUEUE_PANEL_STATE_FILE

PANEL_STATE_FILE = Path(QUEUE_PANEL_STATE_FILE)
_lock = asyncio.Lock()


def _load() -> dict:
    try:
        data = json.loads(PANEL_STATE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save(data: dict) -> None:
    PANEL_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp = PANEL_STATE_FILE.with_suffix(".tmp")
    temp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    temp.replace(PANEL_STATE_FILE)


async def get_panel_message_id() -> int | None:
    async with _lock:
        value = _load().get("message_id")
        try:
            return int(value) if value else None
        except (TypeError, ValueError):
            return None


async def set_panel_message_id(message_id: int) -> None:
    async with _lock:
        data = _load()
        data["message_id"] = int(message_id)
        _save(data)
