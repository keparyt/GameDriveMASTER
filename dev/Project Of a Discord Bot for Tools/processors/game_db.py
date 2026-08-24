import asyncio
import difflib
import time
from dataclasses import dataclass

import aiohttp
from bs4 import BeautifulSoup

from utils.helper import log

GAMES_DB_URL = "https://kepargamedb.com/games-list-page/"
CACHE_TTL_SECONDS = 15 * 60


@dataclass(frozen=True)
class GameDBEntry:
    name: str
    url: str


_cache: list[GameDBEntry] = []
_cache_time = 0.0
_lock = asyncio.Lock()


def normalize_name(value: str) -> str:
    value = value.casefold()
    value = value.replace("&", " and ")
    # Keep the complete DB display name, but make matching tolerant of
    # punctuation, edition separators and version strings.
    import re
    value = re.sub(r"[™®©]", "", value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def _match_key(value: str) -> str:
    """Remove common release/version suffixes for game-title matching."""
    import re
    value = normalize_name(value)
    value = re.sub(
        r"\b(?:v|ver|version)\s*\d[\w.\-+]*.*$",
        "",
        value,
        flags=re.IGNORECASE,
    )
    return value.strip()


async def refresh_game_database(force: bool = False) -> list[GameDBEntry]:
    global _cache, _cache_time

    async with _lock:
        now = time.monotonic()
        if _cache and not force and now - _cache_time < CACHE_TTL_SECONDS:
            return _cache

        timeout = aiohttp.ClientTimeout(total=45)
        headers = {"User-Agent": "KeparGameDetector/1.0"}
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.get(GAMES_DB_URL, timeout=timeout) as response:
                response.raise_for_status()
                html = await response.text()

        soup = BeautifulSoup(html, "html.parser")
        entries: list[GameDBEntry] = []
        seen_urls: set[str] = set()

        # The site's game list uses class="az-list-item". Collect every
        # hyperlink inside each item and preserve the exact visible anchor text
        # (including versions/editions) as the database name.
        for item in soup.select(".az-list-item"):
            for anchor in item.find_all("a", href=True):
                name = " ".join(anchor.stripped_strings)
                url = anchor.get("href", "").strip()
                if not name or not url:
                    continue
                if url.startswith("/"):
                    url = "https://kepargamedb.com" + url
                elif url.startswith("//"):
                    url = "https:" + url
                if not url.startswith("http") or url in seen_urls:
                    continue
                seen_urls.add(url)
                entries.append(GameDBEntry(name=name, url=url))

        _cache = entries
        _cache_time = now
        log(f"GameDB | loaded {len(entries)} game links")
        return _cache


async def find_game(game_name: str) -> GameDBEntry | None:
    entries = await refresh_game_database()
    query = _match_key(game_name)
    if not query:
        return None

    # Prefer exact normalized title, then a title contained in the DB name.
    exact = [entry for entry in entries if _match_key(entry.name) == query]
    if exact:
        return exact[0]

    contained = [
        entry for entry in entries
        if query in _match_key(entry.name)
        or _match_key(entry.name) in query
    ]
    if contained:
        # Shortest matching title is generally the least surprising match.
        return min(contained, key=lambda entry: len(_match_key(entry.name)))

    # Conservative fuzzy fallback. Require a strong ratio so unrelated games
    # don't get a KeparGameDB link accidentally.
    best: tuple[float, GameDBEntry | None] = (0.0, None)
    for entry in entries:
        candidate = _match_key(entry.name)
        ratio = difflib.SequenceMatcher(None, query, candidate).ratio()
        if ratio > best[0]:
            best = (ratio, entry)

    return best[1] if best[0] >= 0.88 else None


async def enrich_games(games: list[dict]) -> list[dict]:
    """Prefer KeparGameDB URLs; fall back to Steam when no DB match exists."""
    result = []
    for game in games:
        item = dict(game)
        match = await find_game(str(item.get("name", "")))
        if match:
            item["kepargamedb_name"] = match.name
            item["kepargamedb_url"] = match.url
            item["library_url"] = match.url
            item["library_source"] = "kepargamedb"
        else:
            item["library_url"] = item.get("steam_url")
            item["library_source"] = "steam" if item.get("steam_url") else None
        result.append(item)
    return result
