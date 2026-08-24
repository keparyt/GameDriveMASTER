import asyncio
import difflib
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from config import KEPAR_DB_URL_PREFIX
from processors.thegamesdb import verify_game
from utils.helper import log

DB_FILE = Path(__file__).resolve().parent.parent / "sdb.html"


@dataclass(frozen=True)
class GameDBEntry:
    name: str
    url: str
    source: str = "kepardb"
    tgdb_game_id: int | None = None
    selected_platform: str | None = None
    console_platforms: tuple[str, ...] = ()
    pc_available: bool = False


_cache: list[GameDBEntry] = []
_cache_signature: tuple[int, int] | None = None
_lock = asyncio.Lock()


# Explicit, conservative aliases for common spoken/transcribed forms.
_TITLE_ALIASES = {
    "arc 2": "ark 2",
    "ark ii": "ark 2",
    "civilization 6": "sid meier s civilization vi",
    "civilisation 6": "sid meier s civilization vi",
    "civilization vi": "sid meier s civilization vi",
    "civilisation vi": "sid meier s civilization vi",
    "civ 6": "sid meier s civilization vi",
    "civ vi": "sid meier s civilization vi",
    "civilization 7": "sid meier s civilization vii",
    "civilisation 7": "sid meier s civilization vii",
    "civilization vii": "sid meier s civilization vii",
    "civilisation vii": "sid meier s civilization vii",
    "civ 7": "sid meier s civilization vii",
    "civ vii": "sid meier s civilization vii",
    "stalker 2": "s t a l k e r 2 heart of chornobyl",
    "stalker 2 heart of chornobyl": "s t a l k e r 2 heart of chornobyl",
    "hollow knight silk song": "hollow knight silksong",
    "hollow knight silk songs": "hollow knight silksong",
    "hollow knight silksong": "hollow knight silksong",
    "rocket league": "rocket league",
    "overwatch 2": "overwatch 2",
    "garys mod": "garrys mod",
    "gary s mod": "garrys mod",
    "gmod": "garrys mod",
    "skyrim": "the elder scrolls v skyrim",
}


def normalize_name(value: str) -> str:
    value = value.casefold().replace("&", " and ")
    value = re.sub(r"[™®©]", "", value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return _TITLE_ALIASES.get(value, value)


def _match_key(value: str) -> str:
    value = normalize_name(value)
    return re.sub(r"\b(?:v|ver|version)\s*\d[\w.\-+]*.*$", "", value, flags=re.IGNORECASE).strip()


def _resolve_db_url(href: str) -> str:
    href = href.strip()
    prefix = KEPAR_DB_URL_PREFIX.rstrip("/")
    if not href:
        return ""
    if href.startswith("//"):
        href = "https:" + href
    if re.match(r"^https?://", href, re.IGNORECASE):
        from urllib.parse import urlsplit, urlunsplit
        parts = urlsplit(href)
        db = urlsplit(prefix)
        return urlunsplit((db.scheme, db.netloc, parts.path, parts.query, parts.fragment))
    return urljoin(prefix + "/", href.lstrip("/"))


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
        for item in soup.select(".az-list-item"):
            for anchor in item.find_all("a", href=True):
                name = " ".join(anchor.stripped_strings)
                url = _resolve_db_url(anchor.get("href", ""))
                if not name or not url or url in seen_urls:
                    continue
                seen_urls.add(url)
                entries.append(GameDBEntry(name=name, url=url))
        _cache = entries
        _cache_signature = signature
        log(f"GameDB | loaded {len(entries)} game links from {DB_FILE.name}")
        log(f"GameDB | URL prefix: {KEPAR_DB_URL_PREFIX}")
        return _cache


async def find_local_game(game_name: str, min_fuzzy: float = 0.88) -> GameDBEntry | None:
    """Look up a game in the local sdb.html only.

    This deliberately has NO external fallback. It is used by the public queue
    panel when choosing the URL to display, so the panel can prefer the canonical
    SDB/KeparGameDB URL while still falling back to the URL already stored on the
    queue item when the local database has no match.
    """
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
    if best[1] is not None and best[0] >= min_fuzzy:
        return best[1]
    return None


async def find_game(game_name: str, min_fuzzy: float = 0.88) -> GameDBEntry | None:
    """Find a local KeparGameDB entry, then fall back to TheGamesDB.

    The external fallback is deliberately strict: a title is accepted only when
    TheGamesDB has a console release. When a PC release also exists, its PC
    platform is selected as the canonical download target.
    """
    local = await find_local_game(game_name, min_fuzzy)
    if local:
        return await _with_platform_metadata(local, game_name)

    # Local DB miss: use TGDB as the cross-platform authority. This is what lets
    # non-Steam PC games such as Epic/Battle.net titles remain detectable.
    tgdb = await verify_game(game_name)
    if tgdb is None:
        return None
    return GameDBEntry(
        name=tgdb.name,
        url=tgdb.url,
        source="thegamesdb",
        tgdb_game_id=tgdb.game_id,
        selected_platform=tgdb.selected_platform_name,
        console_platforms=tuple(tgdb.console_names),
        pc_available=bool(tgdb.pc_platform),
    )


async def _with_platform_metadata(entry: GameDBEntry, original_name: str) -> GameDBEntry | None:
    """Require console support for local DB matches too, while preserving the local URL."""
    tgdb = await verify_game(entry.name or original_name)
    if tgdb is None:
        return entry
    return GameDBEntry(
        name=entry.name,
        url=entry.url,
        source=entry.source,
        tgdb_game_id=tgdb.game_id,
        selected_platform=tgdb.selected_platform_name,
        console_platforms=tuple(tgdb.console_names),
        pc_available=bool(tgdb.pc_platform),
    )


async def enrich_games(games: list[dict]) -> list[dict]:
    result = []
    for game in games:
        item = dict(game)
        original_name = str(item.get("name", "")).strip()
        match = await find_game(original_name)
        if match:
            item["detected_name"] = original_name
            item["name"] = match.name
            item["kepargamedb_name"] = match.name if match.source == "kepardb" else None
            item["kepargamedb_url"] = match.url if match.source == "kepardb" else None
            item["library_url"] = match.url
            item["library_source"] = match.source
            item["verified"] = True
            item["verification_source"] = match.source
            item["tgdb_game_id"] = match.tgdb_game_id
            item["selected_platform"] = match.selected_platform
            item["console_platforms"] = list(match.console_platforms)
            item["console_names"] = list(match.console_platforms)
            item["pc_available"] = match.pc_available
            item["has_console"] = bool(match.console_platforms)
        else:
            item["verified"] = False
            item["verification_source"] = None
        result.append(item)
    return result
