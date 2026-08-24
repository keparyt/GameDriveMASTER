import asyncio
import difflib
import re
from dataclasses import dataclass
from pathlib import Path

from bs4 import BeautifulSoup

from utils.helper import log

DB_FILE = Path(__file__).resolve().parent.parent / "sdb.html"


@dataclass(frozen=True)
class GameDBEntry:
    name: str
    url: str


_cache: list[GameDBEntry] = []
_cache_signature: tuple[int, int] | None = None
_lock = asyncio.Lock()


def normalize_name(value: str) -> str:
    value = value.casefold().replace("&", " and ")
    value = re.sub(r"[™®©]", "", value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def _match_key(value: str) -> str:
    value = normalize_name(value)
    return re.sub(
        r"\b(?:v|ver|version)\s*\d[\w.\-+]*.*$",
        "",
        value,
        flags=re.IGNORECASE,
    ).strip()


async def refresh_game_database(force: bool = False) -> list[GameDBEntry]:
    global _cache, _cache_signature

    async with _lock:
        if not DB_FILE.exists():
            log(f"GameDB | ERROR: local database file missing: {DB_FILE}")
            return []

        stat = DB_FILE.stat()
        signature = (stat.st_mtime_ns, stat.st_size)
        if _cache and not force and signature == _cache_signature:
            return _cache

        html = DB_FILE.read_text(encoding="utf-8", errors="replace")
        soup = BeautifulSoup(html, "html.parser")
        entries: list[GameDBEntry] = []
        seen_urls: set[str] = set()

        # Parse EVERY hyperlink contained by .az-list-item.
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
                if not url.startswith(("http://", "https://")) or url in seen_urls:
                    continue
                seen_urls.add(url)
                entries.append(GameDBEntry(name=name, url=url))

        _cache = entries
        _cache_signature = signature
        log(f"GameDB | loaded {len(entries)} game links from {DB_FILE.name}")
        return _cache


async def find_game(game_name: str) -> GameDBEntry | None:
    entries = await refresh_game_database()
    query = _match_key(game_name)
    if not query:
        return None

    exact = [entry for entry in entries if _match_key(entry.name) == query]
    if exact:
        return exact[0]

    contained = [
        entry for entry in entries
        if query in _match_key(entry.name) or _match_key(entry.name) in query
    ]
    if contained:
        return min(contained, key=lambda entry: len(_match_key(entry.name)))

    best: tuple[float, GameDBEntry | None] = (0.0, None)
    for entry in entries:
        candidate = _match_key(entry.name)
        ratio = difflib.SequenceMatcher(None, query, candidate).ratio()
        if ratio > best[0]:
            best = (ratio, entry)

    return best[1] if best[0] >= 0.88 else None


async def enrich_games(games: list[dict]) -> list[dict]:
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
