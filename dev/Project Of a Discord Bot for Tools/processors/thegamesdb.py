"""TheGamesDB verification with a rate-limit-safe Steam fallback."""
from __future__ import annotations

import asyncio
import difflib
import os
import re
from dataclasses import dataclass
from typing import Any

import aiohttp
from bs4 import BeautifulSoup
from utils.helper import log

BASE_URL = "https://api.thegamesdb.net"
try:
    from config import THEGAMESDB_API_KEY as CONFIG_API_KEY
except (ImportError, AttributeError):
    CONFIG_API_KEY = ""
API_KEY = os.getenv("THEGAMESDB_API_KEY", CONFIG_API_KEY).strip()
_GAME_CACHE: dict[str, "TGDBGameInfo | None"] = {}
_GAME_LOCK = asyncio.Lock()
_PLATFORM_CACHE: dict[int, "TGDBPlatform"] = {}
_PLATFORM_LOCK = asyncio.Lock()

PC_NAMES = {"pc", "windows", "microsoft windows", "windows pc", "pc compatible", "steam", "steam deck", "linux", "mac os", "macos", "macintosh", "dos", "ms dos"}
PLATFORM_ALIASES = {
    "pc": ("pc", "windows", "microsoft windows", "windows pc", "pc compatible", "steam", "steam deck", "linux", "mac os", "macos", "macintosh", "dos", "ms dos"),
    "ps1": ("ps1", "playstation 1", "sony playstation"), "ps2": ("ps2", "playstation 2"), "ps3": ("ps3", "playstation 3"),
    "ps4": ("ps4", "playstation 4"), "ps5": ("ps5", "playstation 5"), "xbox": ("xbox", "xbox original"),
    "xbox 360": ("xbox 360",), "xbox one": ("xbox one",), "xbox series": ("xbox series", "series x", "series s"),
    "switch": ("switch", "nintendo switch"), "wii": ("wii",), "wii u": ("wii u",), "gamecube": ("gamecube", "nintendo gamecube"),
    "n64": ("n64", "nintendo 64"), "nes": ("nes", "nintendo entertainment system"),
    "snes": ("snes", "super nintendo", "super nes", "super nintendo entertainment system"),
    "game boy": ("game boy", "gameboy", "gb"), "game boy color": ("game boy color", "gameboy color", "gbc"),
    "game boy advance": ("game boy advance", "gameboy advance", "gba"), "ds": ("ds", "nintendo ds"), "3ds": ("3ds", "nintendo 3ds"),
    "genesis": ("genesis", "sega genesis", "mega drive", "sega mega drive"), "dreamcast": ("dreamcast", "sega dreamcast"),
    "saturn": ("saturn", "sega saturn"), "game gear": ("game gear", "sega game gear"), "arcade": ("arcade",),
    "neo geo": ("neo geo", "neogeo"), "atari 2600": ("atari 2600",), "atari 5200": ("atari 5200",), "atari 7800": ("atari 7800",),
    "psp": ("psp", "playstation portable"), "vita": ("vita", "ps vita", "playstation vita"),
}
CONSOLE_HINTS = ("playstation", "xbox", "nintendo", "switch", "wii", "game boy", "gameboy", "game gear", "sega", "dreamcast", "saturn", "genesis", "mega drive", "nes", "snes", "super nintendo", "neo geo", "atari", "arcade")


@dataclass(frozen=True)
class TGDBPlatform:
    id: int
    name: str
    console: bool


@dataclass(frozen=True)
class TGDBGameInfo:
    game_id: int
    name: str
    pc_platform: TGDBPlatform | None
    console_platforms: tuple[TGDBPlatform, ...]
    platform_hint: str | None = None
    source: str = "thegamesdb"
    store_url: str | None = None

    @property
    def title(self) -> str:
        return self.name

    @property
    def has_console(self) -> bool:
        return bool(self.console_platforms)

    @property
    def selected_platform(self) -> TGDBPlatform:
        return self.pc_platform or self.console_platforms[0]

    @property
    def selected_platform_name(self) -> str:
        return self.selected_platform.name

    @property
    def console_names(self) -> list[str]:
        return [p.name for p in self.console_platforms]

    @property
    def url(self) -> str:
        return self.store_url or f"https://thegamesdb.net/game.php?id={self.game_id}"


def _norm(value: str) -> str:
    value = re.sub(r"[™®©]", "", str(value or "").casefold().replace("&", " and "))
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", value)).strip()


def _score(query: str, title: str) -> float:
    q, t = _norm(query), _norm(title)
    if not q or not t:
        return 0.0
    if q == t:
        return 1.0
    seq = difflib.SequenceMatcher(None, q, t).ratio()
    qt, tt = set(q.split()), set(t.split())
    union = len(qt | tt) or 1
    return seq * 0.55 + (len(qt & tt) / union) * 0.20 + (len(qt & tt) / (len(qt) or 1)) * 0.25


def _is_pc_platform(name: str) -> bool:
    n = _norm(name)
    return n in {_norm(x) for x in PC_NAMES} or n.startswith("pc ") or n.endswith(" pc")


def _is_console_platform(name: str, raw: dict[str, Any] | None = None) -> bool:
    raw = raw or {}
    return bool(raw.get("console")) or any(h in _norm(name) for h in CONSOLE_HINTS)


def _extract_platform_hint(value: str) -> tuple[str, str | None]:
    original = str(value or "").strip()
    normalized = _norm(original)
    for canonical, aliases in sorted(PLATFORM_ALIASES.items(), key=lambda x: max(map(len, x[1])), reverse=True):
        for alias in aliases:
            a = _norm(alias)
            if normalized.endswith(" " + a):
                words, alias_words = original.split(), alias.split()
                if len(words) > len(alias_words):
                    return " ".join(words[:-len(alias_words)]), canonical
    return original, None


def _platform_matches_hint(platform: TGDBPlatform, hint: str | None) -> bool:
    if not hint:
        return True
    if hint == "pc":
        return _is_pc_platform(platform.name)
    n = _norm(platform.name)
    return any(n == _norm(a) or _norm(a) in n or n in _norm(a) for a in PLATFORM_ALIASES.get(hint, (hint,)))


async def _get_json(path: str, params: dict[str, Any]) -> dict[str, Any] | None:
    if not API_KEY:
        return None
    try:
        async with aiohttp.ClientSession(headers={"User-Agent": "KeparGameDetector/1.0"}) as session:
            async with session.get(f"{BASE_URL}{path}", params={"apikey": API_KEY, **params}, timeout=aiohttp.ClientTimeout(total=25)) as response:
                if response.status != 200:
                    log(f"TheGamesDB | HTTP {response.status} | path={path}")
                    return None
                return await response.json()
    except Exception as exc:
        log(f"TheGamesDB | request error | {path} | {type(exc).__name__}: {exc}")
        return None


async def _platforms_by_id(ids: list[int]) -> dict[int, TGDBPlatform]:
    wanted = [int(x) for x in ids if str(x).isdigit()]
    missing = [x for x in wanted if x not in _PLATFORM_CACHE]
    if missing:
        async with _PLATFORM_LOCK:
            body = await _get_json("/v1/Platforms/ByPlatformID", {"id": ",".join(map(str, missing)), "fields": "console"})
            for key, raw in (((body or {}).get("data") or {}).get("platforms") or {}).items():
                try:
                    pid = int(raw.get("id", key))
                except (TypeError, ValueError):
                    continue
                name = str(raw.get("name") or "").strip()
                if name:
                    _PLATFORM_CACHE[pid] = TGDBPlatform(pid, name, _is_console_platform(name, raw))
    return {pid: _PLATFORM_CACHE[pid] for pid in wanted if pid in _PLATFORM_CACHE}


async def _search_games(query: str) -> list[tuple[float, dict[str, Any], TGDBPlatform]]:
    body = await _get_json("/v1.1/Games/ByGameName", {"name": query, "mode": "natural", "fields": "platform,alternates,overview", "include": "platform", "page": 1})
    games = ((body or {}).get("data") or {}).get("games") or []
    included = (((body or {}).get("include") or {}).get("platform") or {})
    included = included.get("data") or included or {}
    if not games:
        return []
    ids = [int(g["platform"]) for g in games if str(g.get("platform", "")).isdigit()]
    platform_map = await _platforms_by_id(ids)
    for raw_id, raw in included.items():
        try:
            pid = int(raw.get("id", raw_id))
        except (TypeError, ValueError):
            continue
        pname = str(raw.get("name") or "").strip()
        if pname:
            platform_map[pid] = TGDBPlatform(pid, pname, _is_console_platform(pname, raw))
    ranked = []
    for game in games:
        try:
            pid = int(game.get("platform"))
        except (TypeError, ValueError):
            continue
        platform = platform_map.get(pid)
        title = str(game.get("game_title") or "").strip()
        if platform and title:
            ranked.append((_score(query, title), game, platform))
    return sorted(ranked, key=lambda x: x[0], reverse=True)


async def _steam_fallback(query: str) -> TGDBGameInfo | None:
    """Single exact Steam search first, with one conservative retry.

    The old implementation generated up to 24 character mutations. That was
    the reason logs showed repeated HTTP 429 responses and caused real Steam
    games such as Blight: Survival to become unresolved.
    """
    queries = [query]
    stripped = re.sub(r"['’`\"]", "", query).strip()
    normalized = _norm(query)
    if stripped and _norm(stripped) != normalized:
        queries.append(stripped)
    if normalized and normalized not in {_norm(x) for x in queries}:
        queries.append(normalized)

    try:
        async with aiohttp.ClientSession(headers={"User-Agent": "KeparGameDetector/1.0"}) as session:
            best = None
            for attempt, variant in enumerate(queries[:3], 1):
                try:
                    async with session.get("https://store.steampowered.com/search/", params={"term": variant, "cc": "ca", "l": "english"}, timeout=15) as response:
                        if response.status == 429:
                            log(f"Steam fallback | HTTP 429 | query={variant!r} | attempt={attempt}")
                            continue
                        if response.status != 200:
                            log(f"Steam fallback | HTTP {response.status} | query={variant!r}")
                            continue
                        html = await response.text()
                except Exception as exc:
                    log(f"Steam fallback request error | query={variant!r} | {type(exc).__name__}: {exc}")
                    continue
                soup = BeautifulSoup(html, "html.parser")
                for row in soup.select("a.search_result_row[data-ds-appid]")[:30]:
                    node, appid = row.select_one(".title"), row.get("data-ds-appid")
                    if not node or not str(appid).isdigit():
                        continue
                    title = " ".join(node.stripped_strings).strip()
                    seq = difflib.SequenceMatcher(None, _norm(query), _norm(title)).ratio()
                    score = _score(query, title)
                    exact = _norm(query) == _norm(title) or _norm(variant) == _norm(title)
                    candidate = (exact, seq, score, title, int(appid), variant)
                    if best is None or candidate[:3] > best[:3]:
                        best = candidate
                if best and best[0]:
                    break
            if not best:
                log(f"Steam fallback | no search results | query={query!r}")
                return None
            exact, seq, score, title, appid, matched = best
            if not exact and not (seq >= 0.93 and score >= 0.62):
                log(f"Steam fallback | rejected low title confidence | query={query!r} | best={title!r} | score={score:.3f} | seq={seq:.3f}")
                return None
            reason = "exact" if exact else "fuzzy-high-confidence"
            log(f"Steam fallback | verified | query={query!r} | title={title!r} | appid={appid} | score={score:.3f} | seq={seq:.3f} | matched_query={matched!r} | reason={reason}")
            return TGDBGameInfo(-appid, title, TGDBPlatform(-appid, "PC", False), tuple(), "pc", "steam", f"https://store.steampowered.com/app/{appid}/")
    except Exception as exc:
        log(f"Steam fallback error | {query!r} | {type(exc).__name__}: {exc}")
        return None


async def verify_game(name: str) -> TGDBGameInfo | None:
    original = str(name or "").strip()
    query, platform_hint = _extract_platform_hint(original)
    if not query:
        return None
    key = f"{_norm(query)}|platform={platform_hint or '*'}"
    if key in _GAME_CACHE:
        return _GAME_CACHE[key]
    async with _GAME_LOCK:
        if key in _GAME_CACHE:
            return _GAME_CACHE[key]
        ranked = await _search_games(query) if API_KEY else []
        if platform_hint:
            ranked = [r for r in ranked if _platform_matches_hint(r[2], platform_hint)]
        if ranked:
            best_score = ranked[0][0]
            min_score = 0.48 if len(_norm(query).split()) <= 2 else 0.58
            if best_score >= min_score:
                canonical_name = str(ranked[0][1].get("game_title") or query).strip()
                same = [(g, p) for _, g, p in ranked if _norm(str(g.get("game_title") or "")) == _norm(canonical_name) or _score(canonical_name, str(g.get("game_title") or "")) >= 0.90]
                pc = next((p for _, p in same if _is_pc_platform(p.name)), None)
                consoles = {p.id: p for _, p in same if p.console and not _is_pc_platform(p.name)}
                if pc or consoles:
                    game_id = next((int(g.get("id")) for g, p in same if pc and p.id == pc.id and str(g.get("id", "")).isdigit()), None)
                    if game_id is None:
                        game_id = next((int(g.get("id")) for g, _ in same if str(g.get("id", "")).isdigit()), 0)
                    result = TGDBGameInfo(game_id, canonical_name, pc, tuple(sorted(consoles.values(), key=lambda p: p.name.casefold())), platform_hint)
                    _GAME_CACHE[key] = result
                    log(f"TheGamesDB | verified | query={original!r} | title={result.name!r} | selected={result.selected_platform_name!r}")
                    return result
            else:
                log(f"TheGamesDB | rejected (low title confidence) | query={original!r} | best={str(ranked[0][1].get('game_title') or '')!r} | score={best_score:.3f}")
        elif API_KEY:
            log(f"TheGamesDB | no usable result | query={original!r} | hint={platform_hint!r}")
        if platform_hint in (None, "pc"):
            steam = await _steam_fallback(query)
            if steam:
                _GAME_CACHE[key] = steam
                return steam
        _GAME_CACHE[key] = None
        return None


async def annotate_game(game: dict) -> dict | None:
    info = await verify_game(str(game.get("name") or ""))
    if not info:
        return None
    item = dict(game)
    item.update({"name": info.name, "tgdb_game_id": info.game_id if info.source == "thegamesdb" else None, "tgdb_url": info.url if info.source == "thegamesdb" else None, "pc_available": bool(info.pc_platform), "selected_platform": info.selected_platform_name, "console_platforms": info.console_names, "console_names": info.console_names, "has_console": info.has_console, "platform_hint": info.platform_hint, "verification_source": info.source, "steam_url": info.store_url if info.source == "steam" else None})
    return item
