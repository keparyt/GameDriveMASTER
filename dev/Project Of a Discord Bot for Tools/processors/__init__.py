"""Game and media processing helpers for the Discord tools bot.

The package installs compatibility hooks before the other processors import:
- typo-tolerant first-party PC storefront verification;
- storefront-aware GameDB results for PC-only games missing from TheGamesDB.
"""

from . import thegamesdb as _thegamesdb
from .store_verifier import verify_with_stores

_original_verify_game = _thegamesdb.verify_game


async def _verify_game_with_store_fallback(query: str):
    result = await _original_verify_game(query)
    if result is not None:
        return result
    store_result = await verify_with_stores(query)
    if store_result is not None:
        return store_result
    return None


_thegamesdb.verify_game = _verify_game_with_store_fallback

try:
    from . import game_db as _game_db
    from .game_db import GameDBEntry

    _original_find_game = _game_db.find_game

    async def _find_game_with_store(query: str, min_fuzzy: float = 0.88):
        existing = await _original_find_game(query, min_fuzzy)
        store_result = await verify_with_stores(query)
        if store_result is None:
            return existing
        return GameDBEntry(
            name=store_result.name,
            url=store_result.url,
            source=store_result.store_source,
            tgdb_game_id=existing.tgdb_game_id if existing else None,
            selected_platform=existing.selected_platform if existing else "PC",
            console_platforms=existing.console_platforms if existing else (),
            pc_available=True,
        )

    _game_db.find_game = _find_game_with_store
except Exception:
    pass
