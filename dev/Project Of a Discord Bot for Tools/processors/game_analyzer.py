import asyncio
import difflib
import json
import re
import shutil
import subprocess
from pathlib import Path
from urllib.parse import unquote, urlparse

import aiohttp
from bs4 import BeautifulSoup

from config import (
    IMAGE_EXTENSIONS,
    KEPARDB_MATCH_THRESHOLD,
    MAX_GAMES,
    MAX_SOURCE_URLS,
    MAX_SOCIAL_MEDIA_ITEMS,
    MAX_TRANSCRIPT_CHARS,
    STEAM_COUNTRY,
    STEAM_HIGH_CONFIDENCE_SCORE,
    STEAM_HIGH_CONFIDENCE_SEQUENCE,
    STEAM_LANGUAGE,
    STEAM_MATCH_THRESHOLD,
    STEAM_REQUEST_TIMEOUT_SECONDS,
    STEAM_SEARCH_URL,
    STEAM_USER_AGENT,
    VIDEO_EXTENSIONS,
    OLLAMA_MODEL,
    OLLAMA_URL,
)
from processors.game_db import find_game, find_local_game, normalize_name
from processors.thegamesdb import verify_game
from processors.instagram_scraper import is_instagram_url, scrape_post
from utils.helper import log


def _run(command, timeout=180):
    return subprocess.run(command, capture_output=True, text=True, timeout=timeout)


def _tool(name):
    return shutil.which(name)


def _clean_candidate_name(name):
    return re.sub(r"\s+", " ", re.sub(r"^[\s*•\-–—\d.)]+", "", str(name)).strip()).strip(" \t\r\n-–—:;")


def _dedupe_candidates(candidates):
    seen = set()
    output = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        name = _clean_candidate_name(candidate.get("name", ""))
        key = re.sub(r"[^a-z0-9]+", "", name.casefold())
        if name and key and key not in seen:
            seen.add(key)
            item = dict(candidate)
            item["name"] = name
            output.append(item)
    return output[:MAX_GAMES]


def _similarity(a, b):
    return difflib.SequenceMatcher(None, normalize_name(a), normalize_name(b)).ratio()


def _word_similarity(a, b):
    words_a, words_b = normalize_name(a).split(), normalize_name(b).split()
    if not words_a or not words_b or abs(len(words_a) - len(words_b)) > 1:
        return 0.0
    return sum(max((difflib.SequenceMatcher(None, word, other).ratio() for other in words_b), default=0) for word in words_a) / len(words_a)


def _credible_name_match(query, title, threshold):
    qn, tn = normalize_name(query), normalize_name(title)
    if not qn or not tn:
        return False
    if qn == tn:
        return True
    similarity = _similarity(query, title)
    word_similarity = _word_similarity(query, title)
    if len(qn.split()) >= 2 and len(tn.split()) >= 2:
        return similarity >= threshold and word_similarity >= 0.82
    return similarity >= threshold and word_similarity >= 0.82


async def _deepseek_correct_name(name):
    # Kept as a compatibility hook. Title completion/verification is deliberately
    # deterministic so a small fragment can never be replaced by an unrelated game.
    return name


def _is_instagram_url(url):
    host = (urlparse(url).hostname or "").lower()
    return is_instagram_url(url) or (host in {"instagram.com", "www.instagram.com", "m.instagram.com"} and re.search(r"/(p|reel|reels|tv)/", url, re.I) is not None)


def _steam_candidates_from_text(text):
    output = []
    pattern = re.compile(r"https?://(?:store\.)?steampowered\.com/app/(\d+)(?:/([^/?#]+))?/?[^\s]*", re.I)
    for match in pattern.finditer(text):
        appid, slug = match.group(1), match.group(2)
        title = unquote(slug or "").replace("_", " ").strip()
        if title:
            output.append({
                "name": title,
                "confidence": 100,
                "reason": f"explicit Steam store URL (appid {appid})",
                "evidence_type": "steam_url",
                "steam_appid": appid,
                "steam_url": match.group(0),
            })
    return output


async def _extract_direct_text(text):
    steam = _steam_candidates_from_text(text)
    if steam:
        return steam
    lines = [
        re.sub(r"^\s*(?:[*•\-–—]|\d+[.)])\s*", "", line).strip()
        for line in text.splitlines()
        if line.strip()
    ]
    return [{"name": line, "confidence": 100, "reason": "explicitly present in direct text", "evidence_type": "direct_text"} for line in lines[:MAX_GAMES]]


async def _download_url(url, workdir):
    if _is_instagram_url(url):
        return await scrape_post(url, workdir)
    if not _tool("yt-dlp"):
        raise RuntimeError("yt-dlp is not installed")
    output = str(workdir / "source.%(ext)s")
    meta = await asyncio.to_thread(_run, ["yt-dlp", "--no-warnings", "--ignore-errors", "--dump-json", "--no-playlist", url], 180)
    entries = []
    for line in meta.stdout.splitlines():
        try:
            value = json.loads(line)
            if isinstance(value, dict):
                entries.append(value)
        except Exception:
            pass
    root = entries[0] if entries else {}
    download = await asyncio.to_thread(_run, ["yt-dlp", "--no-warnings", "--ignore-errors", "--restrict-filenames", "--no-playlist", "-o", output, url], 360)
    media = [p for p in workdir.glob("source.*") if p.is_file() and p.suffix.lower() not in {".json", ".part", ".ytdl"}]
    if not entries and not media:
        raise RuntimeError((download.stderr or meta.stderr or "unknown media extraction error")[-2000:])
    return {
        "title": root.get("title"),
        "description": root.get("description"),
        "uploader": root.get("uploader") or root.get("channel"),
        "entries": entries,
    }, sorted(media)[:MAX_SOCIAL_MEDIA_ITEMS]


async def _download_attachment(url, target):
    async with aiohttp.ClientSession() as session:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=180)) as response:
            response.raise_for_status()
            target.write_bytes(await response.read())


async def _transcribe(video, workdir, index=1):
    return ""


async def _steam_prefix_completion(query):
    """Complete a genuine title prefix; never return an unrelated fuzzy result."""
    query_normalized = normalize_name(query).strip()
    if not query_normalized or len(query_normalized) < 3:
        return None
    try:
        async with aiohttp.ClientSession(headers={"User-Agent": STEAM_USER_AGENT}) as session:
            async with session.get(
                STEAM_SEARCH_URL,
                params={"term": query, "cc": STEAM_COUNTRY, "l": STEAM_LANGUAGE},
                timeout=STEAM_REQUEST_TIMEOUT_SECONDS,
            ) as response:
                if response.status != 200:
                    return None
                html = await response.text()
        ranked = []
        for row in BeautifulSoup(html, "html.parser").select("a.search_result_row[data-ds-appid]")[:30]:
            node = row.select_one(".title")
            appid = row.get("data-ds-appid")
            if not node or not appid or not str(appid).isdigit():
                continue
            title = node.get_text(" ", strip=True)
            title_normalized = normalize_name(title)
            if title_normalized == query_normalized or title_normalized.startswith(query_normalized + " "):
                ranked.append((len(title_normalized), title, int(appid)))
        if not ranked:
            return None
        _, title, appid = min(ranked)
        return {
            "name": title,
            "steam_appid": appid,
            "steam_url": f"https://store.steampowered.com/app/{appid}/",
            "confidence": 100,
            "reason": f"title completion from prefix {query!r}",
            "evidence_type": "title_completion",
            "steam_verified": True,
        }
    except Exception as exc:
        log(f"Steam prefix completion error | {query} | {type(exc).__name__}: {exc}")
        return None


async def _complete_candidate_name(candidate):
    name = _clean_candidate_name(candidate.get("name", ""))
    if not name:
        return candidate

    try:
        local = await find_local_game(name, min_fuzzy=1.0)
        if local and normalize_name(local.name).startswith(normalize_name(name) + " "):
            item = dict(candidate)
            item["name"] = local.name
            item["completion_source"] = "kepardb"
            item["completion_url"] = local.url
            log(f"Game title completion | {name!r} -> {local.name!r} | source=KeparDB")
            return item
    except Exception as exc:
        log(f"Game title completion KeparDB error | {name} | {type(exc).__name__}: {exc}")

    steam = await _steam_prefix_completion(name)
    if steam:
        item = dict(candidate)
        item.update(steam)
        log(f"Game title completion | {name!r} -> {steam['name']!r} | source=Steam")
        return item
    return candidate


async def _verify_and_enrich(candidates):
    verified, unresolved = [], []
    for original in _dedupe_candidates(candidates):
        candidate = await _complete_candidate_name(original)
        name = candidate["name"]
        steam_url = candidate.get("steam_url")
        if steam_url:
            item = dict(candidate)
            item.update({
                "verified": True,
                "verification_source": "steam_url",
                "library_url": steam_url,
                "library_source": "steam",
                "pc_available": True,
                "has_console": False,
                "console_platforms": [],
                "console_names": [],
            })
            verified.append(item)
            continue

        platform = await verify_game(name)
        if platform:
            item = dict(candidate)
            item.update({
                "name": getattr(platform, "game_title", None) or name,
                "verified": True,
                "verification_source": "thegamesdb",
                "library_url": platform.url,
                "library_source": "thegamesdb",
                "pc_available": bool(platform.pc_platform),
                "console_platforms": platform.console_names,
                "console_names": platform.console_names,
                "has_console": bool(platform.console_names),
            })
            verified.append(item)
            continue

        db = await find_game(name)
        if db and _credible_name_match(name, db.name, KEPARDB_MATCH_THRESHOLD):
            item = dict(candidate)
            item.update({
                "name": db.name,
                "verified": True,
                "verification_source": db.source,
                "library_url": db.url,
                "library_source": db.source,
                "pc_available": db.pc_available,
                "console_platforms": list(db.console_platforms),
                "console_names": list(db.console_platforms),
                "has_console": bool(db.console_platforms),
            })
            verified.append(item)
            continue

        unresolved.append({
            "name": name,
            "detected_name": name,
            "confidence": float(candidate.get("confidence", 0)),
            "reason": "Could not verify title.",
            "requires_store_link": True,
        })
    return verified[:MAX_GAMES], unresolved


async def _find_steam_match(query):
    try:
        async with aiohttp.ClientSession(headers={"User-Agent": STEAM_USER_AGENT}) as session:
            async with session.get(
                STEAM_SEARCH_URL,
                params={"term": query, "cc": STEAM_COUNTRY, "l": STEAM_LANGUAGE},
                timeout=STEAM_REQUEST_TIMEOUT_SECONDS,
            ) as response:
                if response.status != 200:
                    return None
                html = await response.text()
        ranked = []
        for row in BeautifulSoup(html, "html.parser").select("a.search_result_row[data-ds-appid]")[:30]:
            node = row.select_one(".title")
            appid = row.get("data-ds-appid")
            if node and appid and str(appid).isdigit():
                title = node.get_text(strip=True)
                ranked.append((_similarity(query, title), title, int(appid)))
        if not ranked:
            return None
        score, title, appid = max(ranked)
        sequence = difflib.SequenceMatcher(None, normalize_name(query), normalize_name(title)).ratio()
        if not (sequence >= STEAM_HIGH_CONFIDENCE_SEQUENCE and score >= STEAM_HIGH_CONFIDENCE_SCORE) and not _credible_name_match(query, title, STEAM_MATCH_THRESHOLD):
            log(f"Steam fallback | rejected unrelated result | query={query!r} | best={title!r} | score={score:.3f} | seq={sequence:.3f}")
            return None
        return {
            "name": title,
            "steam_appid": appid,
            "steam_url": f"https://store.steampowered.com/app/{appid}/",
            "steam_verified": True,
            "confidence": score * 100,
        }
    except Exception as exc:
        log(f"Steam verification error | {query} | {type(exc).__name__}: {exc}")
        return None


def _result(games, unresolved=None):
    unresolved = unresolved or []
    if not games:
        return {
            "status": "needs_store_link" if unresolved else "unknown",
            "message": "No identified game could be verified.",
            "game_count": 0,
            "games": [],
            "unresolved_games": unresolved,
            "requires_store_link": bool(unresolved),
        }
    first = games[0]
    return {
        "status": "identified" if not unresolved else "partial",
        "game_count": len(games),
        "games": games,
        "unresolved_games": unresolved,
        "requires_store_link": bool(unresolved),
        "game_name": first.get("name"),
        "confidence": float(first.get("confidence", 0)),
        "steam_url": first.get("steam_url"),
        "reason": first.get("reason", "Identification from supplied content."),
        "candidates": games,
        "message": "",
    }


async def analyze_game_input(data):
    from processors.game_media_analyzer import analyze_game_input as evidence_analyzer
    return await evidence_analyzer(data)
