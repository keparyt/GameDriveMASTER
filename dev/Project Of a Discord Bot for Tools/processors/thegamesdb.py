"""TheGamesDB platform verification and PC/console selection.

Games must have at least one console release. If a PC release exists, PC is
selected as the canonical/download platform. A platform suffix in user input
(e.g. ``Zelda SNES`` or ``Skyrim PC``) is treated as an explicit platform
constraint and only releases on that platform are considered.
"""

from __future__ import annotations

import asyncio
import difflib
import os
import re
from dataclasses import dataclass
from typing import Any

import aiohttp

from utils.helper import log

BASE_URL = "https://api.thegamesdb.net"

try:
    from config import THEGAMESDB_API_KEY as CONFIG_API_KEY
except (ImportError, AttributeError):
    CONFIG_API_KEY = ""

API_KEY = os.getenv("THEGAMESDB_API_KEY", CONFIG_API_KEY).strip()

_PLATFORM_CACHE: dict[int, dict[str, Any]] = {}
_PLATFORM_LOCK = asyncio.Lock()
_GAME_CACHE: dict[str, "TGDBGameInfo | None"] = {}
_GAME_LOCK = asyncio.Lock()

PC_NAMES = {
    "pc", "windows", "microsoft windows", "windows pc", "steam", "linux",
    "mac os", "macos", "macintosh", "dos", "ms dos", "pc compatible",
    "steam deck", "epic", "epic games", "battle net", "battlenet",
}

# User-facing aliases. TheGamesDB's actual platform names are still used for
# the final comparison and display.
PLATFORM_ALIASES = {
    "pc": ("pc", "windows", "microsoft windows", "windows pc", "pc compatible", "steam", "steam deck", "linux", "mac os", "macos", "macintosh", "dos", "ms dos"),
    "ps1": ("ps1", "playstation 1", "sony playstation", "playstation"),
    "ps2": ("ps2", "playstation 2", "sony playstation 2"),
    "ps3": ("ps3", "playstation 3", "sony playstation 3"),
    "ps4": ("ps4", "playstation 4", "sony playstation 4"),
    "ps5": ("ps5", "playstation 5", "sony playstation 5"),
    "xbox": ("xbox", "xbox original", "original xbox"),
    "xbox 360": ("xbox 360", "360"),
    "xbox one": ("xbox one",),
    "xbox series": ("xbox series", "series x", "series s", "series x s"),
    "switch": ("switch", "nintendo switch"),
    "wii": ("wii",),
    "wii u": ("wii u",),
    "gamecube": ("gamecube", "nintendo gamecube"),
    "n64": ("n64", "nintendo 64"),
    "nes": ("nes", "nintendo entertainment system"),
    "snes": ("snes", "super nintendo", "super nes", "super nintendo entertainment system"),
    "game boy": ("game boy", "gameboy", "gb"),
    "game boy color": ("game boy color", "gameboy color", "gbc"),
    "game boy advance": ("game boy advance", "gameboy advance", "gba"),
    "ds": ("ds", "nintendo ds"),
    "3ds": ("3ds", "nintendo 3ds"),
    "genesis": ("genesis", "sega genesis", "mega drive", "sega mega drive"),
    "dreamcast": ("dreamcast", "sega dreamcast"),
    "saturn": ("saturn", "sega saturn"),
    "game gear": ("game gear", "sega game gear"),
    "arcade": ("arcade",),
    "neo geo": ("neo geo", "neogeo"),
    "atari 2600": ("atari 2600",),
    "atari 5200": ("atari 5200",),
    "atari 7800": ("atari 7800",),
    "psp": ("psp", "playstation portable"),
    "vita": ("vita", "ps vita", "playstation vita"),
}

CONSOLE_HINTS = (
    "playstation", "xbox", "nintendo", "switch", "wii", "game boy", "gameboy",
    "game gear", "sega", "dreamcast", "saturn", "genesis", "mega drive", "nes",
    "snes", "super nintendo", "neo geo", "atari", "commodore", "3do", "turbo grafx",
    "pc engine", "virtual boy", "ouya", "intellivision", "colecovision", "arcade",
)


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
        return [platform.name for platform in self.console_platforms]

    @property
    def url(self) -> str:
        return f"https://thegamesdb.net/game.php?id={self.game_id}"


def _norm(value: str) -> str:
    value = value.casefold().replace("&", " and ")
    value = re.sub(r"[™®©]", "", value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def _score(query: str, title: str) -> float:
    q = _norm(query)
    t = _norm(title)
    if not q or not t:
        return 0.0
    if q == t:
        return 1.0
    seq = difflib.SequenceMatcher(None, q, t).ratio()
    qt = set(q.split())
    tt = set(t.split())
    token = len(qt & tt) / len(qt | tt) if qt and tt else 0.0
    return seq * 0.7 + token * 0.3


def _is_pc_platform(name: str) -> bool:
    normalized = _norm(name)
    return normalized in {_norm(x) for x in PC_NAMES} or normalized.startswith("pc ") or normalized.endswith(" pc")


def _is_console_platform(name: str, raw: dict[str, Any] | None = None) -> bool:
    raw = raw or {}
    if raw.get("console"):
        return True
    normalized = _norm(name)
    return any(hint in normalized for hint in CONSOLE_HINTS)


def _extract_platform_hint(value: str) -> tuple[str, str | None]:
    """Return (game query without suffix, canonical platform hint).

    Only a platform expression at the beginning/end of the input is removed.
    This avoids corrupting legitimate game titles containing words such as
    'PC' in the middle.
    """
    original = str(value or "").strip()
    if not original:
        return "", None

    normalized = _norm(original)
    # Longest aliases first so 'playstation 3' wins over 'playstation'.
    aliases: list[tuple[str, str]] = []
    for canonical, values in PLATFORM_ALIASES.items():
        for alias in values:
            aliases.append((_norm(alias), canonical))
    aliases.sort(key=lambda item: len(item[0]), reverse=True)

    for alias, canonical in aliases:
        if not alias:
            continue
        # Suffix form: "zelda snes", "skyrim pc", "zelda super nintendo".
        if normalized == alias:
            return "", canonical
        if normalized.endswith(" " + alias):
            base = normalized[: -(len(alias) + 1)].strip()
            if base:
                # Keep the original spelling for the actual TGDB query where
                # possible; normalized text is safer for deterministic parsing.
                words = original.split()
                alias_words = alias.split()
                if len(words) >= len(alias_words):
                    return " ".join(words[: len(words) - len(alias_words)]).strip(), canonical
                return base, canonical
        # Prefix form is also accepted: "SNES Zelda".
        if normalized.startswith(alias + " "):
            base = normalized[len(alias) + 1 :].strip()
            if base:
                return base, canonical

    return original, None


def _platform_matches_hint(platform: TGDBPlatform, hint: str | None) -> bool:
    if not hint:
        return True
    name = _norm(platform.name)
    aliases = PLATFORM_ALIASES.get(hint, (hint,))
    normalized_aliases = [_norm(alias) for alias in aliases]
    if hint == "pc":
        return _is_pc_platform(platform.name)
    return any(name == alias or alias in name or name in alias for alias in normalized_aliases)


async def _get_json(path: str, params: dict[str, Any]) -> dict[str, Any] | None:
    if not API_KEY:
        return None
    request_params = {"apikey": API_KEY, **params}
    try:
        async with aiohttp.ClientSession(headers={"User-Agent": "KeparGameDetector/1.0"}) as session:
            async with session.get(f"{BASE_URL}{path}", params=request_params, timeout=aiohttp.ClientTimeout(total=25)) as response:
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
            missing = [x for x in wanted if x not in _PLATFORM_CACHE]
            if missing:
                body = await _get_json(
                    "/v1/Platforms/ByPlatformID",
                    {"id": ",".join(map(str, missing)), "fields": "console,controller,developer,manufacturer"},
                )
                platforms = ((body or {}).get("data") or {}).get("platforms") or {}
                for key, raw in platforms.items():
                    try:
                        platform_id = int(raw.get("id", key))
                    except (TypeError, ValueError):
                        continue
                    name = str(raw.get("name") or "").strip()
                    if not name:
                        continue
                    _PLATFORM_CACHE[platform_id] = {
                        "id": platform_id,
                        "name": name,
                        "console": _is_console_platform(name, raw),
                    }
    return {
        platform_id: TGDBPlatform(
            id=platform_id,
            name=str(_PLATFORM_CACHE[platform_id]["name"]),
            console=bool(_PLATFORM_CACHE[platform_id]["console"]),
        )
        for platform_id in wanted
        if platform_id in _PLATFORM_CACHE
    }


async def verify_game(name: str) -> TGDBGameInfo | None:
    """Resolve a game, optionally constrained to an explicit platform."""
    original_query = str(name or "").strip()
    query, platform_hint = _extract_platform_hint(original_query)
    if not query:
        query = original_query
    key = f"{_norm(query)}|platform={platform_hint or '*'}"
    if not query or not API_KEY:
        return None
    if key in _GAME_CACHE:
        return _GAME_CACHE[key]

    async with _GAME_LOCK:
        if key in _GAME_CACHE:
            return _GAME_CACHE[key]

        body = await _get_json(
            "/v1.1/Games/ByGameName",
            {
                "name": query,
                "mode": "natural",
                "fields": "platform,alternates,overview",
                "include": "platform",
                "page": 1,
            },
        )
        games = ((body or {}).get("data") or {}).get("games") or []
        included = ((body or {}).get("include") or {}).get("platform") or {}
        included = included.get("data") or included or {}
        if not games:
            _GAME_CACHE[key] = None
            return None

        platform_ids = []
        for game in games:
            try:
                platform_ids.append(int(game.get("platform")))
            except (TypeError, ValueError):
                pass
        platform_map = await _platforms_by_id(platform_ids)
        for raw_id, raw in included.items():
            try:
                platform_id = int(raw.get("id", raw_id))
            except (TypeError, ValueError):
                continue
            name_value = str(raw.get("name") or "").strip()
            if name_value:
                platform_map[platform_id] = TGDBPlatform(
                    id=platform_id,
                    name=name_value,
                    console=_is_console_platform(name_value, raw),
                )

        ranked: list[tuple[float, dict[str, Any], TGDBPlatform]] = []
        for game in games:
            try:
                platform_id = int(game.get("platform"))
            except (TypeError, ValueError):
                continue
            platform = platform_map.get(platform_id)
            title = str(game.get("game_title") or "").strip()
            if not platform or not title:
                continue
            if platform_hint and not _platform_matches_hint(platform, platform_hint):
                continue
            ranked.append((_score(query, title), game, platform))

        if not ranked:
            log(f"TheGamesDB | no platform match | query={original_query!r} | hint={platform_hint!r}")
            _GAME_CACHE[key] = None
            return None

        best_score = max(score for score, _, _ in ranked)
        title_rows = [(game, platform) for score, game, platform in ranked if score >= max(0.62, best_score - 0.08)]
        if not title_rows:
            _GAME_CACHE[key] = None
            return None

        canonical_game, _ = max(
            title_rows,
            key=lambda pair: _score(query, str(pair[0].get("game_title") or "")),
        )
        canonical_name = str(canonical_game.get("game_title") or query).strip()

        same_title = []
        for game, platform in title_rows:
            title = str(game.get("game_title") or "").strip()
            if _norm(title) == _norm(canonical_name) or _score(canonical_name, title) >= 0.92:
                same_title.append((game, platform))

        # For an explicit platform, every retained row is already constrained.
        # For normal input, preserve all console releases and prefer PC.
        if platform_hint:
            matching = [(game, platform) for game, platform in same_title if _platform_matches_hint(platform, platform_hint)]
            same_title = matching

        pc_platform = next((platform for _, platform in same_title if _is_pc_platform(platform.name)), None)
        consoles: dict[int, TGDBPlatform] = {}
        for _, platform in same_title:
            if platform.console and not _is_pc_platform(platform.name):
                consoles[platform.id] = platform

        # Mandatory console support remains in force even when the user asked
        # for PC. A PC-only TGDB entry is therefore not accepted.
        if not consoles:
            _GAME_CACHE[key] = None
            log(
                f"TheGamesDB | rejected (no console) | query={original_query!r} | "
                f"title={canonical_name!r} | hint={platform_hint!r}"
            )
            return None

        selected_game_id = next(
            (
                int(game.get("id"))
                for game, platform in same_title
                if pc_platform and platform.id == pc_platform.id and str(game.get("id", "")).isdigit()
            ),
            None,
        )
        if selected_game_id is None:
            selected_game_id = next(
                (int(game.get("id")) for game, _ in same_title if str(game.get("id", "")).isdigit()),
                0,
            )
        if not selected_game_id:
            _GAME_CACHE[key] = None
            return None

        result = TGDBGameInfo(
            game_id=selected_game_id,
            name=canonical_name,
            pc_platform=pc_platform,
            console_platforms=tuple(sorted(consoles.values(), key=lambda p: p.name.casefold())),
            platform_hint=platform_hint,
        )
        _GAME_CACHE[key] = result
        log(
            f"TheGamesDB | verified | query={original_query!r} | title={result.name!r} | "
            f"hint={platform_hint or 'any'} | selected={result.selected_platform_name!r} | "
            f"pc={result.pc_platform.name if result.pc_platform else 'no'} | "
            f"consoles={', '.join(result.console_names)}"
        )
        return result


async def annotate_game(game: dict) -> dict | None:
    """Attach TGDB platform metadata to a game dict, or return None if invalid."""
    info = await verify_game(str(game.get("name") or ""))
    if not info:
        return None
    item = dict(game)
    item["name"] = info.name
    item["tgdb_game_id"] = info.game_id
    item["tgdb_url"] = info.url
    item["pc_available"] = bool(info.pc_platform)
    item["selected_platform"] = info.selected_platform_name
    item["console_platforms"] = info.console_names
    item["console_names"] = info.console_names
    item["has_console"] = True
    item["platform_hint"] = info.platform_hint
    return item
