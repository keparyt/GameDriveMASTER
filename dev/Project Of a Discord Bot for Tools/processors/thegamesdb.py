"""TheGamesDB platform verification and PC/console selection.

TheGamesDB is preferred for platform metadata, but it is NOT the only source of
truth. Modern/indie PC games can be absent from TGDB, so Steam is used as a
fallback identity source when TGDB has no usable result.
"""

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
_PLATFORM_CACHE: dict[int, dict[str, Any]] = {}
_PLATFORM_LOCK = asyncio.Lock()
_GAME_CACHE: dict[str, "TGDBGameInfo | None"] = {}
_GAME_LOCK = asyncio.Lock()
PC_NAMES = {"pc","windows","microsoft windows","windows pc","pc compatible","steam","steam deck","linux","mac os","macos","macintosh","dos","ms dos","epic","epic games","battle net","battlenet"}
PLATFORM_ALIASES = {
 "pc": ("pc","windows","microsoft windows","windows pc","pc compatible","steam","steam deck","linux","mac os","macos","macintosh","dos","ms dos"),
 "ps1": ("ps1","playstation 1","sony playstation"), "ps2": ("ps2","playstation 2","sony playstation 2"),
 "ps3": ("ps3","playstation 3","sony playstation 3"), "ps4": ("ps4","playstation 4","sony playstation 4"),
 "ps5": ("ps5","playstation 5","sony playstation 5"), "xbox": ("xbox","xbox original","original xbox"),
 "xbox 360": ("xbox 360","360"), "xbox one": ("xbox one",), "xbox series": ("xbox series","series x","series s"),
 "switch": ("switch","nintendo switch"), "wii": ("wii",), "wii u": ("wii u",), "gamecube": ("gamecube","nintendo gamecube"),
 "n64": ("n64","nintendo 64"), "nes": ("nes","nintendo entertainment system"),
 "snes": ("snes","super nintendo","super nes","super nintendo entertainment system"),
 "game boy": ("game boy","gameboy","gb"), "game boy color": ("game boy color","gameboy color","gbc"),
 "game boy advance": ("game boy advance","gameboy advance","gba"), "ds": ("ds","nintendo ds"), "3ds": ("3ds","nintendo 3ds"),
 "genesis": ("genesis","sega genesis","mega drive","sega mega drive"), "dreamcast": ("dreamcast","sega dreamcast"),
 "saturn": ("saturn","sega saturn"), "game gear": ("game gear","sega game gear"), "arcade": ("arcade",), "neo geo": ("neo geo","neogeo"),
 "atari 2600": ("atari 2600",), "atari 5200": ("atari 5200",), "atari 7800": ("atari 7800",),
 "psp": ("psp","playstation portable"), "vita": ("vita","ps vita","playstation vita")}
CONSOLE_HINTS = ("playstation","xbox","nintendo","switch","wii","game boy","gameboy","game gear","sega","dreamcast","saturn","genesis","mega drive","nes","snes","super nintendo","neo geo","atari","commodore","3do","turbo grafx","pc engine","virtual boy","ouya","intellivision","colecovision","arcade")

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
    def title(self) -> str: return self.name
    @property
    def has_console(self) -> bool: return bool(self.console_platforms)
    @property
    def selected_platform(self) -> TGDBPlatform: return self.pc_platform or self.console_platforms[0]
    @property
    def selected_platform_name(self) -> str: return self.selected_platform.name
    @property
    def console_names(self) -> list[str]: return [p.name for p in self.console_platforms]
    @property
    def url(self) -> str:
        return self.store_url or f"https://thegamesdb.net/game.php?id={self.game_id}"

def _norm(value: str) -> str:
    value = re.sub(r"[™®©]", "", str(value or "").casefold().replace("&", " and "))
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", value)).strip()

def _score(query: str, title: str) -> float:
    q,t = _norm(query),_norm(title)
    if not q or not t: return 0.0
    if q == t: return 1.0
    seq = difflib.SequenceMatcher(None,q,t).ratio()
    qt,tt=set(q.split()),set(t.split())
    union=len(qt|tt) or 1
    token=len(qt&tt)/union
    coverage=len(qt&tt)/(len(qt) or 1)
    # Character similarity handles Whisper phonetic errors such as Frogin -> Froggin.
    return seq*0.45+token*0.20+coverage*0.35

def _is_pc_platform(name: str) -> bool:
    n=_norm(name)
    return n in {_norm(x) for x in PC_NAMES} or n.startswith("pc ") or n.endswith(" pc")

def _is_console_platform(name: str, raw: dict[str,Any]|None=None) -> bool:
    raw=raw or {}
    return bool(raw.get("console")) or any(h in _norm(name) for h in CONSOLE_HINTS)

def _extract_platform_hint(value: str) -> tuple[str,str|None]:
    original=str(value or "").strip()
    if not original: return "",None
    normalized=_norm(original)
    aliases=sorted(((_norm(a),c) for c,vals in PLATFORM_ALIASES.items() for a in vals),key=lambda x:len(x[0]),reverse=True)
    for alias,canonical in aliases:
        if normalized.endswith(" "+alias):
            words,alias_words=original.split(),alias.split()
            if len(words)>len(alias_words): return " ".join(words[:-len(alias_words)]),canonical
        if normalized.startswith(alias+" "):
            base=normalized[len(alias)+1:].strip()
            if base: return base,canonical
    return original,None

def _platform_matches_hint(platform:TGDBPlatform,hint:str|None)->bool:
    if not hint:return True
    if hint=="pc":return _is_pc_platform(platform.name)
    n=_norm(platform.name)
    return any(n==_norm(a) or _norm(a) in n or n in _norm(a) for a in PLATFORM_ALIASES.get(hint,(hint,)))

async def _get_json(path:str,params:dict[str,Any])->dict[str,Any]|None:
    if not API_KEY:return None
    try:
        async with aiohttp.ClientSession(headers={"User-Agent":"KeparGameDetector/1.0"}) as session:
            async with session.get(f"{BASE_URL}{path}",params={"apikey":API_KEY,**params},timeout=aiohttp.ClientTimeout(total=25)) as response:
                if response.status!=200:
                    log(f"TheGamesDB | HTTP {response.status} | path={path}");return None
                return await response.json()
    except Exception as exc:
        log(f"TheGamesDB | request error | {path} | {type(exc).__name__}: {exc}");return None

async def _platforms_by_id(ids:list[int])->dict[int,TGDBPlatform]:
    wanted=[int(x) for x in ids if str(x).isdigit()]
    missing=[x for x in wanted if x not in _PLATFORM_CACHE]
    if missing:
        async with _PLATFORM_LOCK:
            missing=[x for x in wanted if x not in _PLATFORM_CACHE]
            if missing:
                body=await _get_json("/v1/Platforms/ByPlatformID",{"id":",".join(map(str,missing)),"fields":"console,controller,developer,manufacturer"})
                for key,raw in (((body or {}).get("data") or {}).get("platforms") or {}).items():
                    try:pid=int(raw.get("id",key))
                    except (TypeError,ValueError):continue
                    pname=str(raw.get("name") or "").strip()
                    if pname:_PLATFORM_CACHE[pid]={"id":pid,"name":pname,"console":_is_console_platform(pname,raw)}
    return {pid:TGDBPlatform(pid,str(_PLATFORM_CACHE[pid]["name"]),bool(_PLATFORM_CACHE[pid]["console"])) for pid in wanted if pid in _PLATFORM_CACHE}

async def _search_games(query:str)->list[tuple[float,dict[str,Any],TGDBPlatform]]:
    body=await _get_json("/v1.1/Games/ByGameName",{"name":query,"mode":"natural","fields":"platform,alternates,overview","include":"platform","page":1})
    games=((body or {}).get("data") or {}).get("games") or []
    included=(((body or {}).get("include") or {}).get("platform") or {})
    included=included.get("data") or included or {}
    if not games:return []
    ids=[]
    for game in games:
        try:ids.append(int(game.get("platform")))
        except (TypeError,ValueError):pass
    platform_map=await _platforms_by_id(ids)
    for raw_id,raw in included.items():
        try:pid=int(raw.get("id",raw_id))
        except (TypeError,ValueError):continue
        pname=str(raw.get("name") or "").strip()
        if pname:platform_map[pid]=TGDBPlatform(pid,pname,_is_console_platform(pname,raw))
    ranked=[]
    for game in games:
        try:pid=int(game.get("platform"))
        except (TypeError,ValueError):continue
        platform,title=platform_map.get(pid),str(game.get("game_title") or "").strip()
        if platform and title:ranked.append((_score(query,title),game,platform))
    return sorted(ranked,key=lambda x:x[0],reverse=True)

async def _steam_fallback(query: str) -> TGDBGameInfo | None:
    """Find a real Steam title when TGDB has no entry.

    This is deliberately identity-only. It does not fabricate console releases.
    The returned PC platform is synthetic and marked by source='steam'.
    """
    try:
        async with aiohttp.ClientSession(headers={"User-Agent":"KeparGameDetector/1.0"}) as session:
            async with session.get("https://store.steampowered.com/search/", params={"term":query,"cc":"ca","l":"english"}, timeout=15) as response:
                if response.status != 200:
                    return None
                html=await response.text()
        soup=BeautifulSoup(html,"html.parser")
        ranked=[]
        for row in soup.select("a.search_result_row[data-ds-appid]")[:30]:
            title_node=row.select_one(".title")
            appid=row.get("data-ds-appid")
            if not title_node or not appid or not str(appid).isdigit():
                continue
            title=" ".join(title_node.stripped_strings).strip()
            if not title:
                continue
            score=_score(query,title)
            seq=difflib.SequenceMatcher(None,_norm(query),_norm(title)).ratio()
            if _norm(query)==_norm(title):
                score=1.0
            ranked.append((score,seq,title,int(appid)))
        if not ranked:
            return None
        score,seq,title,appid=max(ranked,key=lambda x:(x[0],x[1]))
        # Strong typo tolerance for short/one-token Whisper errors, but do not
        # accept vaguely similar unrelated titles.
        if _norm(query)!=_norm(title) and not (score>=0.84 and seq>=0.84):
            log(f"Steam fallback | rejected low title confidence | query={query!r} | best={title!r} | score={score:.3f} | seq={seq:.3f}")
            return None
        platform=TGDBPlatform(-appid,"PC",False)
        result=TGDBGameInfo(-appid,title,platform,tuple(),"pc","steam",f"https://store.steampowered.com/app/{appid}/")
        log(f"Steam fallback | verified | query={query!r} | title={title!r} | appid={appid} | score={score:.3f}")
        return result
    except Exception as exc:
        log(f"Steam fallback error | {query!r} | {type(exc).__name__}: {exc}")
        return None

async def verify_game(name:str)->TGDBGameInfo|None:
    original_query=str(name or "").strip()
    query,platform_hint=_extract_platform_hint(original_query)
    if not query:query=original_query
    key=f"{_norm(query)}|platform={platform_hint or '*'}"
    if not query:return None
    if key in _GAME_CACHE:return _GAME_CACHE[key]
    async with _GAME_LOCK:
        if key in _GAME_CACHE:return _GAME_CACHE[key]

        ranked=await _search_games(query) if API_KEY else []
        if platform_hint:
            ranked=[r for r in ranked if _platform_matches_hint(r[2],platform_hint)]
        if ranked:
            best_score=ranked[0][0]
            min_score=0.48 if len(_norm(query).split())<=2 else 0.58
            if best_score>=min_score:
                canonical_game,canonical_platform=ranked[0][1],ranked[0][2]
                canonical_name=str(canonical_game.get("game_title") or query).strip()
                qtokens=set(_norm(query).split());ttokens=set(_norm(canonical_name).split())
                if len(qtokens)<3 or len(qtokens&ttokens)/len(qtokens)>=0.50:
                    same_title_all=[]
                    for _,game,platform in ranked:
                        title=str(game.get("game_title") or "").strip()
                        if _norm(title)==_norm(canonical_name) or _score(canonical_name,title)>=0.90:
                            same_title_all.append((game,platform))
                    if platform_hint:
                        unfiltered=await _search_games(canonical_name)
                        for _,game,platform in unfiltered:
                            title=str(game.get("game_title") or "").strip()
                            if (_norm(title)==_norm(canonical_name) or _score(canonical_name,title)>=0.90) and not any(int(game.get("id",0))==int(g.get("id",-1)) and platform.id==p.id for g,p in same_title_all):
                                same_title_all.append((game,platform))
                    selected_rows=[(g,p) for g,p in same_title_all if _platform_matches_hint(p,platform_hint)] if platform_hint else same_title_all
                    if selected_rows:
                        pc_platform=next((p for _,p in selected_rows if _is_pc_platform(p.name)),None)
                        consoles={p.id:p for _,p in same_title_all if p.console and not _is_pc_platform(p.name)}
                        if consoles:
                            selected_game_id=next((int(g.get("id")) for g,p in selected_rows if pc_platform and p.id==pc_platform.id and str(g.get("id","")).isdigit()),None)
                            if selected_game_id is None:selected_game_id=next((int(g.get("id")) for g,_ in selected_rows if str(g.get("id","")).isdigit()),0)
                            if selected_game_id:
                                result=TGDBGameInfo(selected_game_id,canonical_name,pc_platform,tuple(sorted(consoles.values(),key=lambda p:p.name.casefold())),platform_hint)
                                _GAME_CACHE[key]=result
                                log(f"TheGamesDB | verified | query={original_query!r} | title={result.name!r} | hint={platform_hint or 'any'} | selected={result.selected_platform_name!r} | pc={result.pc_platform.name if result.pc_platform else 'no'} | consoles={', '.join(result.console_names)}")
                                return result
            else:
                log(f"TheGamesDB | rejected (low title confidence) | query={original_query!r} | best={str(ranked[0][1].get('game_title') or '')!r} | score={best_score:.3f}")
        elif API_KEY:
            log(f"TheGamesDB | no usable result | query={original_query!r} | hint={platform_hint!r}")

        # TGDB is not exhaustive. Fall back to Steam for PC games, including
        # modern indie games that have no TGDB/sdb.html record.
        if platform_hint in (None,"pc"):
            steam=await _steam_fallback(query)
            if steam:
                _GAME_CACHE[key]=steam
                return steam

        _GAME_CACHE[key]=None
        return None

async def annotate_game(game:dict)->dict|None:
    info=await verify_game(str(game.get("name") or ""))
    if not info:return None
    item=dict(game)
    item.update({"name":info.name,"tgdb_game_id":info.game_id if info.source=="thegamesdb" else None,"tgdb_url":info.url if info.source=="thegamesdb" else None,"pc_available":bool(info.pc_platform),"selected_platform":info.selected_platform_name,"console_platforms":info.console_names,"console_names":info.console_names,"has_console":info.has_console,"platform_hint":info.platform_hint,"verification_source":info.source,"steam_url":info.store_url if info.source=="steam" else None})
    return item
