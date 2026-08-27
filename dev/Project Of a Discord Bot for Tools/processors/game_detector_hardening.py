"""Hardened runtime layer for game detection.

Keeps the existing media/OCR/database pipeline but inserts reusable candidate
filtering before verification, consolidates duplicates, caches OCR cleanup,
and reuses a bounded Steam HTTP session.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time

import aiohttp
from bs4 import BeautifulSoup

from config import (
    MAX_EVIDENCE_CHARS, OLLAMA_MODEL, OLLAMA_TIMEOUT_SECONDS,
    OLLAMA_TEMPERATURE, OLLAMA_URL, OLLAMA_USER_AGENT, STEAM_COUNTRY,
    STEAM_LANGUAGE, STEAM_REQUEST_TIMEOUT_SECONDS, STEAM_SEARCH_URL,
    STEAM_USER_AGENT,
)
from processors import game_media_analyzer as _base
from processors.candidate_filter import (
    clean_title, confidence_percent, dedupe_candidates, dedupe_verified_games,
    is_plausible_title, normalize_title, rejection_reason,
)
from utils.helper import log

_ORIGINAL_IDENTIFY = _base._identify_from_evidence
_ORIGINAL_VERIFY = _base._verify_and_enrich

_FRAME_LABEL_RE = re.compile(r"^\s*(?:frame|image|video|scene|shot)\s*[_#-]?\s*\d+(?:\s*ocr)?\s*:?\s*$", re.I)
_INTERNAL_RE = re.compile(r"^\s*(?:===|primary evidence|secondary evidence|last[- ]resort evidence)\b", re.I)
_LABEL_PREFIXES = (
    "source title:", "source uploader/account:", "source description/caption:",
    "media item title:", "media item description:", "discord message text/context:",
)
_CLEANUP_CACHE: dict[str, tuple[float, str, list[dict]]] = {}
_CLEANUP_CACHE_TTL = 600.0
_STEAM_SESSION: aiohttp.ClientSession | None = None
_STEAM_SESSION_LOCK = asyncio.Lock()
_STEAM_REQUEST_SEM = asyncio.Semaphore(2)
_STEAM_CACHE: dict[str, tuple[float, dict | None]] = {}
_STEAM_CACHE_TTL = 900.0


def _norm(value: str) -> str:
    return normalize_title(value)


def _bad_label(value: str) -> bool:
    text = str(value or "").strip()
    if not text:
        return True
    if _FRAME_LABEL_RE.match(text) or _INTERNAL_RE.match(text):
        return True
    low = text.casefold().strip(" :;-=\t\r\n")
    return any(low.startswith(prefix) for prefix in _LABEL_PREFIXES)


def _clean(value: str) -> str:
    return clean_title(value)


def _looks_generic(value: str) -> bool:
    return not is_plausible_title(value)


def _looks_sentence(value: str) -> bool:
    words = re.findall(r"[a-z0-9']+", str(value or "").casefold())
    function_words = {"a", "an", "and", "are", "for", "from", "has", "have", "if", "in", "into", "is", "of", "on", "that", "the", "this", "to", "when", "while", "with", "you", "your"}
    return len(words) >= 5 and sum(w in function_words for w in words) >= 3


def _compact_evidence(evidence: str) -> str:
    text = str(evidence or "")
    blocks = re.split(r"(---\s*FRAME\s*---)", text, flags=re.I)
    frame_parts, other_parts, current = [], [], []
    in_frame = False
    for part in blocks:
        if re.fullmatch(r"---\s*FRAME\s*---", part.strip(), flags=re.I):
            if current:
                frame_parts.append("\n".join(current))
            current, in_frame = [], True
            continue
        (current if in_frame else other_parts).append(part)
    if current:
        frame_parts.append("\n".join(current))
    if not frame_parts:
        return text[:MAX_EVIDENCE_CHARS]
    budget = max(600, int(MAX_EVIDENCE_CHARS * 0.82))
    per_frame = max(500, min(2400, budget // max(1, len(frame_parts))))
    compact_frames = []
    for index, frame in enumerate(frame_parts, 1):
        frame = frame.strip()
        if len(frame) > per_frame:
            head = per_frame // 2
            frame = frame[:head] + "\n[... OCR middle omitted ...]\n" + frame[-(per_frame - head):]
        compact_frames.append(f"FRAME {index:03d}:\n{frame}")
    prefix = "\n\n".join(x.strip() for x in other_parts if x.strip())
    return ((prefix + "\n\n") if prefix else "") + "\n\n--- FRAME ---\n\n".join(compact_frames)


def _ocr_title_hints(evidence: str) -> list[dict]:
    hints, seen = [], set()
    for block in re.split(r"---\s*FRAME\s*---", str(evidence or ""), flags=re.I):
        for raw in block.splitlines():
            line = re.sub(r"\s+", " ", raw).strip()
            line = re.sub(r"^\s*(?:FRAME|IMAGE|VIDEO)\s*[_#-]?\s*\d+\s*:?\s*", "", line, flags=re.I).strip()
            if not line or _bad_label(line):
                continue
            words = re.findall(r"[a-z0-9']+", line.casefold())
            if not 1 <= len(words) <= 6 or len(line) > 80:
                continue
            if _looks_generic(line) or _looks_sentence(line):
                continue
            letters = [c for c in line if c.isalpha()]
            upper_ratio = sum(c.isupper() for c in letters) / len(letters) if letters else 0
            title_case = any(c.isupper() for c in line) and any(c.islower() for c in line)
            if not (upper_ratio >= 0.70 or title_case):
                continue
            name = _clean(line)
            key = _norm(name)
            if key and key not in seen:
                seen.add(key)
                hints.append({"name": name, "confidence": 0.96, "reason": "compact OCR title-shaped text in actual media", "evidence_type": "ocr_title_card"})
    return hints


async def _steam_session() -> aiohttp.ClientSession:
    global _STEAM_SESSION
    async with _STEAM_SESSION_LOCK:
        if _STEAM_SESSION is None or _STEAM_SESSION.closed:
            _STEAM_SESSION = aiohttp.ClientSession(headers={"User-Agent": STEAM_USER_AGENT})
        return _STEAM_SESSION


async def close_http_sessions() -> None:
    global _STEAM_SESSION
    session, _STEAM_SESSION = _STEAM_SESSION, None
    if session is not None and not session.closed:
        await session.close()


def _cache_get(cache, key, ttl):
    item = cache.get(key)
    if not item:
        return False, None
    timestamp, value = item
    if time.monotonic() - timestamp > ttl:
        cache.pop(key, None)
        return False, None
    return True, value


def _cache_set(cache, key, value):
    cache[key] = (time.monotonic(), value)


async def _steam_prefix_completion(query: str):
    query = _clean(query)
    normalized = _norm(query)
    if not normalized or len(normalized) < 3:
        return None
    key = normalized
    hit, cached = _cache_get(_STEAM_CACHE, key, _STEAM_CACHE_TTL)
    if hit:
        return cached
    session = await _steam_session()
    try:
        async with _STEAM_REQUEST_SEM:
            async with session.get(
                STEAM_SEARCH_URL,
                params={"term": query, "cc": STEAM_COUNTRY, "l": STEAM_LANGUAGE},
                timeout=STEAM_REQUEST_TIMEOUT_SECONDS,
            ) as response:
                if response.status == 429:
                    log(f"Steam title lookup rate limited | query={query!r} | retry_after={response.headers.get('Retry-After', 'unknown')}")
                    _cache_set(_STEAM_CACHE, key, None)
                    return None
                if response.status != 200:
                    log(f"Steam title lookup | HTTP {response.status} | query={query!r}")
                    _cache_set(_STEAM_CACHE, key, None)
                    return None
                html = await response.text()
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        log(f"Steam title lookup error | query={query!r} | {type(exc).__name__}: {exc}")
        _cache_set(_STEAM_CACHE, key, None)
        return None

    ranked = []
    for row in BeautifulSoup(html, "html.parser").select("a.search_result_row[data-ds-appid]")[:50]:
        node, appid = row.select_one(".title"), row.get("data-ds-appid")
        if not node or not appid or not str(appid).isdigit():
            continue
        title = node.get_text(" ", strip=True)
        title_norm = _norm(title)
        if title_norm == normalized:
            ranked.append((0, title, int(appid)))
        elif title_norm.startswith(normalized + " "):
            ranked.append((len(title_norm), title, int(appid)))
    result = None
    if ranked:
        _, title, appid = min(ranked)
        result = {
            "name": title, "steam_appid": appid,
            "steam_url": f"https://store.steampowered.com/app/{appid}/",
            "confidence": 1.0,
            "reason": "exact/prefix Steam title match",
            "evidence_type": "steam_search", "steam_verified": True,
        }
    _cache_set(_STEAM_CACHE, key, result)
    return result


async def _ai_clean_parsed_evidence(evidence: str):
    compact = _compact_evidence(evidence)
    if not compact.strip():
        return compact, []
    key = hashlib.sha256(compact.encode("utf-8", "ignore")).hexdigest()
    hit, cached = _cache_get(_CLEANUP_CACHE, key, _CLEANUP_CACHE_TTL)
    if hit:
        return cached

    prompt = f'''Clean noisy OCR/transcript evidence from a video-game identification scan.
Use ONLY information present in the supplied evidence. Do not identify games from general knowledge.
KEEP actual game titles, title-card text, distinctive proper names that clearly identify a game, repeated title spellings, and titles visible for only one frame.
CORRECT OCR spelling/spacing/capitalization only when surrounding evidence supports it.
REMOVE frame/debug labels, platform lists, prices, promotional copy, genre/feature descriptions, player counts, controls/UI, uploader labels, social captions, and ordinary sentences.
Never turn a generic phrase into a title. Never invent a title. Preserve frame context.
Return ONLY JSON: {{"cleaned_evidence":"FRAME 001: ...","game_hints":[{{"name":"Corrected Game Title","confidence":0.96,"reason":"brief evidence","evidence_type":"ocr_correction"}}]}}

EVIDENCE:
{compact}'''
    payload = {
        "model": OLLAMA_MODEL, "stream": False,
        "messages": [
            {"role": "system", "content": "You are a strict OCR cleanup engine for video-game titles. Never invent titles."},
            {"role": "user", "content": prompt},
        ],
        "options": {"temperature": min(float(OLLAMA_TEMPERATURE), 0.10)},
    }
    try:
        timeout = aiohttp.ClientTimeout(total=min(float(OLLAMA_TIMEOUT_SECONDS), 120.0))
        async with aiohttp.ClientSession(headers={"User-Agent": OLLAMA_USER_AGENT}, timeout=timeout) as session:
            async with session.post(OLLAMA_URL, json=payload) as response:
                response.raise_for_status()
                body = await response.json()
        content = str(body.get("message", {}).get("content", ""))
        match = re.search(r"\{.*\}", content, re.DOTALL)
        if not match:
            raise ValueError("cleanup model did not return JSON")
        parsed = json.loads(match.group(0))
        cleaned = str(parsed.get("cleaned_evidence", "")).strip()
        hints = parsed.get("game_hints", [])
        if not cleaned:
            raise ValueError("cleanup model returned empty evidence")
        valid_hints = [x for x in hints if isinstance(x, dict) and x.get("name")] if isinstance(hints, list) else []
        result = (_compact_evidence(cleaned), valid_hints)
        _cache_set(_CLEANUP_CACHE, key, result)
        log(f"Game detector AI evidence cleanup | input={len(compact)} chars | output={len(result[0])} chars | hints={len(valid_hints)}")
        return result
    except Exception as exc:
        log(f"Game detector AI evidence cleanup unavailable | {type(exc).__name__}: {exc} | using original OCR")
        result = (compact, [])
        _cache_set(_CLEANUP_CACHE, key, result)
        return result


def _filter_candidates(candidates, evidence=None):
    accepted = []
    for item in candidates or []:
        if not isinstance(item, dict):
            continue
        raw = str(item.get("name", "")).strip()
        name = _clean(raw)
        reason = rejection_reason(name)
        if _bad_label(name):
            reason = "frame/evidence label"
        elif _looks_sentence(name):
            reason = reason or "sentence/description text"
        if not name or reason:
            log(f"Game detector candidate rejected | raw={raw!r} | reason={reason or 'invalid candidate'}")
            continue
        copy = dict(item)
        copy["name"] = name
        copy["detected_name"] = copy.get("detected_name") or raw
        try:
            score = float(copy.get("confidence", 0) or 0)
            copy["confidence"] = confidence_percent(score) / 100.0 if score > 1.0 else max(0.0, min(1.0, score))
        except (TypeError, ValueError):
            copy["confidence"] = 0.0
        accepted.append(copy)
    return dedupe_candidates(accepted, max_items=20)


async def _identify_from_evidence(evidence: str, pass_name="primary"):
    cleaned, ai_hints = await _ai_clean_parsed_evidence(evidence)
    hints = _ocr_title_hints(cleaned) + ai_hints
    try:
        ai = await _ORIGINAL_IDENTIFY(cleaned, pass_name)
    except TypeError:
        ai = await _ORIGINAL_IDENTIFY(cleaned)
    except Exception as exc:
        log(f"Game detector base evidence extraction failed | pass={pass_name} | {type(exc).__name__}: {exc}")
        ai = {}
    ai_candidates = ai.get("candidates", []) if isinstance(ai, dict) else []
    candidates = _filter_candidates(hints + ai_candidates, cleaned)
    log(f"Game detector candidate gate | pass={pass_name} | accepted={len(candidates)}")
    return {"candidates": candidates}


async def _guarded_verify_and_enrich(candidates):
    gated = _filter_candidates(candidates)
    if not gated:
        log("Game detector verification skipped | reason=no plausible title candidates")
        return [], []
    log(f"Game detector verification gate | candidates={len(gated)}")
    verified, unresolved = await _ORIGINAL_VERIFY(gated)
    verified = dedupe_verified_games(verified, max_items=20)
    return verified, unresolved


_base._identify_from_evidence = _identify_from_evidence
_base._verify_and_enrich = _guarded_verify_and_enrich
_base._steam_prefix_completion = _steam_prefix_completion
