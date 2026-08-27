"""Production hardening for the game detection pipeline.

This layer is installed by bot.py before the media analyzer runs. It keeps the
existing OCR/vision/database flow, but adds a strict candidate gate before any
expensive resolver, avoids running the cleanup LLM twice per video, consolidates
OCR fragments, and makes web search a bounded last resort.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import time

import aiohttp
from bs4 import BeautifulSoup

from config import (
    MAX_EVIDENCE_CHARS,
    OLLAMA_MODEL,
    OLLAMA_TIMEOUT_SECONDS,
    OLLAMA_TEMPERATURE,
    OLLAMA_URL,
    OLLAMA_USER_AGENT,
    STEAM_COUNTRY,
    STEAM_LANGUAGE,
    STEAM_REQUEST_TIMEOUT_SECONDS,
    STEAM_SEARCH_URL,
    STEAM_USER_AGENT,
)
from processors import game_analyzer as _analyzer
from processors import game_media_analyzer as _media
from processors.candidate_filter import (
    clean_title,
    confidence_percent,
    dedupe_candidates,
    dedupe_verified_games,
    is_plausible_title,
    normalize_title,
    rejection_reason,
)
from utils.helper import log

_ORIGINAL_IDENTIFY = _media._identify_from_evidence

# Production safety limits. They can be overridden through environment
# variables without requiring secrets/config.py changes.
MAX_VIDEO_CANDIDATES = max(4, int(os.getenv("GAME_DETECTOR_MAX_CANDIDATES", "12")))
MIN_VIDEO_CANDIDATE_CONFIDENCE = float(os.getenv("GAME_DETECTOR_MIN_CANDIDATE_CONFIDENCE", "0.70"))
WEB_SEARCH_MIN_CONFIDENCE = float(os.getenv("GAME_DETECTOR_WEB_MIN_CONFIDENCE", "0.90"))
MAX_WEB_SEARCHES_PER_JOB = max(0, int(os.getenv("GAME_DETECTOR_MAX_WEB_SEARCHES", "2")))
WEB_FAILURE_THRESHOLD = max(1, int(os.getenv("GAME_DETECTOR_WEB_FAILURE_THRESHOLD", "2")))

_FRAME_LABEL_RE = re.compile(
    r"^\s*(?:frame|image|video|scene|shot)\s*[_#-]?\s*\d+(?:\s*ocr)?\s*:?\s*$",
    re.I,
)
_INTERNAL_RE = re.compile(
    r"^\s*(?:===|primary evidence|secondary evidence|last[- ]resort evidence)\b",
    re.I,
)
_LABEL_PREFIXES = (
    "source title:",
    "source uploader/account:",
    "source description/caption:",
    "media item title:",
    "media item description:",
    "discord message text/context:",
)

# Deterministic phrases/categories that are overwhelmingly metadata, UI or
# marketing. These are phrase-oriented rather than exact OCR examples.
_METADATA_PHRASES = (
    re.compile(r"^(?:more|top|best|recommended|new)\s+(?:\w+\s+){0,3}games?$", re.I),
    re.compile(r"^(?:if|when|why|how|what)\s+you(?:'|\s)?(?:liked|like|enjoyed|enjoy)\b", re.I),
    re.compile(r"^(?:playstation|xbox|nintendo|switch|pc|steam)(?:\s*[/|&,]\s*(?:playstation|xbox|nintendo|switch|pc|steam|windows|mac|linux))+$", re.I),
    re.compile(r"^(?:couch|online|local|split[- ]?screen)\s*(?:&|and)?\s*(?:online|local|couch)?\s*co[- ]?op(?:erative)?$", re.I),
    re.compile(r"^(?:singleplayer|multiplayer|gameplay|gameplay footage|captured in[- ]game)$", re.I),
)

_OCR_GARBAGE_RE = re.compile(r"[^a-z0-9&'’.!?:,\-+ ]", re.I)

_CLEANUP_CACHE: dict[str, tuple[float, tuple[str, list[dict]]]] = {}
_CLEANUP_TTL = 600.0
_STEAM_SESSION: aiohttp.ClientSession | None = None
_STEAM_SESSION_LOCK = asyncio.Lock()
_STEAM_REQUEST_SEM = asyncio.Semaphore(2)
_STEAM_CACHE: dict[str, tuple[float, dict | None]] = {}
_STEAM_CACHE_TTL = 900.0


def _norm(value: str) -> str:
    return normalize_title(value)


def _clean(value: str) -> str:
    return clean_title(value)


def _confidence(value) -> float:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(score):
        return 0.0
    if score > 1.0:
        score /= 100.0
    return max(0.0, min(1.0, score))


def _bad_label(value: str) -> bool:
    text = str(value or "").strip()
    if not text or _FRAME_LABEL_RE.match(text) or _INTERNAL_RE.match(text):
        return True
    low = text.casefold().strip(" :;-=\t\r\n")
    return any(low.startswith(prefix) for prefix in _LABEL_PREFIXES)


def _looks_sentence(value: str) -> bool:
    words = re.findall(r"[a-z0-9']+", str(value or "").casefold())
    if len(words) < 5:
        return False
    function_words = {
        "a", "an", "and", "are", "as", "at", "for", "from", "has", "have", "if",
        "in", "into", "is", "it", "of", "on", "that", "the", "these", "this", "to",
        "was", "were", "when", "while", "with", "you", "your",
    }
    return sum(word in function_words for word in words) >= 3


def _looks_fragmented_ocr(value: str) -> bool:
    text = str(value or "").strip()
    words = re.findall(r"[A-Za-z0-9']+", text)
    if not words:
        return True
    if len(words) <= 3 and any(len(word) <= 1 for word in words):
        return True
    letters = sum(c.isalpha() for c in text)
    if letters < 3:
        return True
    nonspace = [c for c in text if not c.isspace()]
    if nonspace:
        strange = sum(not (c.isalnum() or c in "&'’.!?:,\-+") for c in nonspace)
        if strange / len(nonspace) > 0.15:
            return True
    return False


def _metadata_or_noise(value: str) -> str | None:
    text = _clean(value)
    if not text:
        return "empty candidate"
    if _bad_label(text):
        return "frame/evidence label"
    if _FRAME_LABEL_RE.match(text):
        return "frame/UI label"
    if any(pattern.fullmatch(text) for pattern in _METADATA_PHRASES):
        return "UI/marketing metadata"
    reason = rejection_reason(text)
    if reason:
        return reason
    words = re.findall(r"[a-z0-9']+", text.casefold())
    if len(words) > 6 or len(text) > 80:
        return "description-like candidate"
    if _looks_sentence(text):
        return "sentence/description text"
    if _looks_fragmented_ocr(text):
        return "fragmented OCR"
    # Candidates with only a platform token, control token, or generic phrase
    # should never be resolved remotely.
    if len(words) == 1 and words[0] in {
        "pc", "xbox", "playstation", "steam", "switch", "drop", "toggle", "press",
        "menu", "options", "loading", "game", "games", "gameplay", "pro", "source",
    }:
        return "generic/UI token"
    return None


def _validated_candidate(item: dict, media_mode: bool) -> dict | None:
    if not isinstance(item, dict):
        return None
    raw = str(item.get("name", "")).strip()
    name = _clean(raw)
    reason = _metadata_or_noise(name)
    if reason and not (not media_mode and reason in {"description-like candidate", "sentence/description text"}):
        log(f"Game detector candidate rejected | raw={raw!r} | reason={reason}")
        return None

    score = _confidence(item.get("confidence", 0.0))
    evidence_type = str(item.get("evidence_type", "")).casefold().strip()
    if media_mode and score < MIN_VIDEO_CANDIDATE_CONFIDENCE:
        log(f"Game detector candidate rejected | raw={raw!r} | reason=low candidate confidence | score={score:.3f}")
        return None
    if media_mode and evidence_type in {"description", "caption", "metadata"}:
        log(f"Game detector candidate rejected | raw={raw!r} | reason=metadata evidence")
        return None
    if media_mode and _OCR_GARBAGE_RE.search(name):
        # Preserve ordinary punctuation, reject unusual OCR control glyphs.
        cleaned_chars = sum(c.isalnum() or c.isspace() or c in "&'’.!?:,\-+" for c in name)
        if cleaned_chars / max(1, len(name)) < 0.97:
            log(f"Game detector candidate rejected | raw={raw!r} | reason=OCR garbage characters")
            return None

    result = dict(item)
    result["name"] = name
    result["detected_name"] = item.get("detected_name") or raw
    result["confidence"] = score
    return result


def _expand_combined_titles(candidates: list[dict]) -> list[dict]:
    expanded: list[dict] = []
    for item in candidates:
        name = str(item.get("name", "")).strip()
        # Handle list-style sequel notation such as "Overcooked 1 & 2".
        match = re.match(r"^(?P<base>.+?)\s+(?P<first>1|one)\s*&\s*(?P<second>2|two)$", name, re.I)
        if match:
            base = match.group("base").strip()
            first = dict(item)
            first["name"] = base
            first["reason"] = "split combined sequel list"
            second = dict(item)
            second["name"] = f"{base} 2"
            second["reason"] = "split combined sequel list"
            expanded.extend([first, second])
        else:
            expanded.append(item)
    return expanded


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


def _normalize_hint(item: dict) -> dict | None:
    if not isinstance(item, dict):
        return None
    name = _clean(item.get("name", ""))
    if not name or _metadata_or_noise(name):
        return None
    copy = dict(item)
    copy["name"] = name
    copy["confidence"] = _confidence(item.get("confidence", 0.0))
    return copy


def _ocr_title_hints(evidence: str) -> list[dict]:
    hints, seen = [], set()
    for block in re.split(r"---\s*FRAME\s*---", str(evidence or ""), flags=re.I):
        for raw in block.splitlines():
            line = re.sub(r"\s+", " ", raw).strip()
            line = re.sub(r"^\s*(?:FRAME|IMAGE|VIDEO|SCENE|SHOT)\s*[_#-]?\s*\d+\s*:?\s*", "", line, flags=re.I).strip()
            if not line or _bad_label(line) or _metadata_or_noise(line):
                continue
            words = re.findall(r"[a-z0-9']+", line.casefold())
            if not 1 <= len(words) <= 6 or len(line) > 80:
                continue
            letters = [c for c in line if c.isalpha()]
            upper_ratio = sum(c.isupper() for c in letters) / len(letters) if letters else 0
            if not (upper_ratio >= 0.70 or (any(c.isupper() for c in line) and any(c.islower() for c in line))):
                continue
            key = _norm(line)
            if key and key not in seen:
                seen.add(key)
                hints.append({
                    "name": _clean(line),
                    "confidence": 0.90,
                    "reason": "title-shaped OCR in actual media",
                    "evidence_type": "ocr_title_card",
                })
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
    hit, cached = _cache_get(_STEAM_CACHE, normalized, _STEAM_CACHE_TTL)
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
                    _cache_set(_STEAM_CACHE, normalized, None)
                    return None
                if response.status != 200:
                    log(f"Steam title lookup | HTTP {response.status} | query={query!r} | status={response.status}")
                    _cache_set(_STEAM_CACHE, normalized, None)
                    return None
                html = await response.text()
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        log(f"Steam title lookup error | query={query!r} | {type(exc).__name__}: {exc}")
        _cache_set(_STEAM_CACHE, normalized, None)
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
        result = {"name": title, "steam_appid": appid, "steam_url": f"https://store.steampowered.com/app/{appid}/", "confidence": 1.0, "reason": "exact/prefix Steam title match", "evidence_type": "steam_search", "steam_verified": True}
    _cache_set(_STEAM_CACHE, normalized, result)
    return result


async def _ai_clean_parsed_evidence(evidence: str):
    compact = _compact_evidence(evidence)
    if not compact.strip():
        return compact, []
    key = hashlib.sha256(compact.encode("utf-8", "ignore")).hexdigest()
    hit, cached = _cache_get(_CLEANUP_CACHE, key, _CLEANUP_TTL)
    if hit:
        log("Game detector AI evidence cleanup | cache hit")
        return cached

    prompt = f'''Clean noisy OCR/transcript evidence from a video-game identification scan.
Use ONLY information present in the supplied evidence. Do not identify games from general knowledge.
KEEP actual game titles supported by visible media, title cards, distinctive proper names, and repeated title spellings.
CORRECT OCR spelling/spacing/capitalization only when surrounding evidence supports it.
REMOVE frame/debug labels, platform lists, prices, promotional text, genre/feature descriptions, player counts, controls/UI, uploader labels, social captions, headings, list headers, and ordinary sentences.
Never turn a generic phrase into a title. Never invent a title.
Return ONLY JSON with a cleaned_evidence string and a game_hints array. game_hints must contain only titles actually supported by the supplied evidence.

EVIDENCE:
{compact}'''
    payload = {
        "model": OLLAMA_MODEL,
        "stream": False,
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
        valid_hints = []
        if isinstance(hints, list):
            for item in hints:
                normalized = _normalize_hint(item)
                if normalized:
                    valid_hints.append(normalized)
        result = (_compact_evidence(cleaned), valid_hints)
        _cache_set(_CLEANUP_CACHE, key, result)
        log(f"Game detector AI evidence cleanup | input={len(compact)} chars | output={len(result[0])} chars | hints={len(valid_hints)}")
        return result
    except Exception as exc:
        log(f"Game detector AI evidence cleanup unavailable | {type(exc).__name__}: {exc} | using deterministic OCR gate")
        result = (compact, [])
        _cache_set(_CLEANUP_CACHE, key, result)
        return result


def _prepare_media_candidates(items) -> list[dict]:
    prepared: list[dict] = []
    for item in items or []:
        normalized = _validated_candidate(item, media_mode=True)
        if normalized:
            prepared.append(normalized)
    prepared = _expand_combined_titles(prepared)
    prepared = dedupe_candidates(prepared, max_items=MAX_VIDEO_CANDIDATES)
    # Re-sort by evidence confidence so weak OCR fragments cannot consume the job
    # candidate budget before stronger titles.
    prepared.sort(key=lambda x: _confidence(x.get("confidence", 0.0)), reverse=True)
    return prepared[:MAX_VIDEO_CANDIDATES]


async def _identify_from_evidence(evidence: str, pass_name="primary"):
    # Only primary extraction invokes the LLM cleanup. Recovery reuses the exact
    # cleaned evidence produced above instead of spending another 30-60 seconds.
    if pass_name == "primary":
        cleaned, ai_hints = await _ai_clean_parsed_evidence(evidence)
    else:
        compact = _compact_evidence(evidence)
        key = hashlib.sha256(compact.encode("utf-8", "ignore")).hexdigest()
        hit, cached = _cache_get(_CLEANUP_CACHE, key, _CLEANUP_TTL)
        if hit:
            cleaned, ai_hints = cached
            log("Game detector recovery pass | reusing cached AI cleanup")
        else:
            cleaned, ai_hints = compact, []
            log("Game detector recovery pass | AI cleanup skipped because no cached primary cleanup exists")

    hints = _ocr_title_hints(cleaned) + list(ai_hints or [])
    try:
        ai = await _ORIGINAL_IDENTIFY(cleaned, pass_name)
    except TypeError:
        ai = await _ORIGINAL_IDENTIFY(cleaned)
    except Exception as exc:
        log(f"Game detector base evidence extraction failed | pass={pass_name} | {type(exc).__name__}: {exc}")
        ai = {}

    ai_candidates = ai.get("candidates", []) if isinstance(ai, dict) else []
    all_candidates = hints + (ai_candidates if isinstance(ai_candidates, list) else [])
    candidates = _prepare_media_candidates(all_candidates)
    log(f"Game detector candidate gate | pass={pass_name} | raw={len(all_candidates)} | accepted={len(candidates)}")
    return {"candidates": candidates}


async def _verify_candidate_without_web(candidate: dict) -> dict | None:
    """Resolve through Steam/local/TheGamesDB without invoking the DDG fallback."""
    try:
        completed = await _analyzer._complete_candidate_name(candidate)
    except Exception as exc:
        log(f"Game detector title completion error | query={candidate.get('name')!r} | {type(exc).__name__}: {exc}")
        completed = dict(candidate)

    name = _clean(completed.get("name", ""))
    if not name or _metadata_or_noise(name):
        return None

    steam_url = completed.get("steam_url")
    if steam_url:
        item = dict(completed)
        item.update({
            "name": name,
            "verified": True,
            "verification_source": "steam",
            "library_url": steam_url,
            "library_source": "steam",
            "pc_available": True,
            "has_console": False,
            "console_platforms": [],
            "console_names": [],
        })
        return item

    try:
        local = await _analyzer.find_local_game(name, min_fuzzy=1.0)
        if local and (
            _analyzer.normalize_name(local.name) == _analyzer.normalize_name(name)
            or _analyzer.normalize_name(local.name).startswith(_analyzer.normalize_name(name) + " ")
        ):
            item = dict(completed)
            item.update({
                "name": local.name,
                "verified": True,
                "verification_source": "kepardb",
                "library_url": local.url,
                "library_source": "kepardb",
                "pc_available": bool(getattr(local, "pc_available", True)),
                "console_platforms": list(getattr(local, "console_platforms", ()) or ()),
                "console_names": list(getattr(local, "console_platforms", ()) or ()),
                "has_console": bool(getattr(local, "console_platforms", ())),
            })
            return item
    except Exception as exc:
        log(f"Game detector local DB error | query={name!r} | {type(exc).__name__}: {exc}")

    try:
        platform = await _analyzer.verify_game(name)
        if platform:
            item = dict(completed)
            item.update({
                "name": getattr(platform, "game_title", None) or getattr(platform, "name", None) or name,
                "verified": True,
                "verification_source": "thegamesdb",
                "library_url": platform.url,
                "library_source": "thegamesdb",
                "pc_available": bool(getattr(platform, "pc_platform", None)),
                "console_platforms": list(getattr(platform, "console_names", ()) or ()),
                "console_names": list(getattr(platform, "console_names", ()) or ()),
                "has_console": bool(getattr(platform, "console_names", ())),
            })
            return item
    except Exception as exc:
        log(f"Game detector TheGamesDB error | query={name!r} | {type(exc).__name__}: {exc}")
    return None


async def _guarded_verify_and_enrich(candidates):
    unique = _prepare_media_candidates(candidates)
    if not unique:
        log("Game detector verification skipped | reason=no plausible title candidates")
        return [], []

    verified: list[dict] = []
    unresolved: list[dict] = []
    web_attempts = 0
    web_failures = 0

    for candidate in unique:
        result = await _verify_candidate_without_web(candidate)
        if result:
            verified.append(result)
            continue

        name = str(candidate.get("name", "")).strip()
        score = _confidence(candidate.get("confidence", 0.0))

        # Web search is strictly optional and only available for high-confidence
        # candidates. The existing store verifier handles DDG safely; the job
        # itself limits how many times it may be called and opens a circuit after
        # repeated failures.
        if (
            web_attempts < MAX_WEB_SEARCHES_PER_JOB
            and web_failures < WEB_FAILURE_THRESHOLD
            and score >= WEB_SEARCH_MIN_CONFIDENCE
        ):
            try:
                from processors.store_verifier import find_store
                web_attempts += 1
                store = await find_store(name)
            except Exception as exc:
                store = None
                log(f"Game detector web fallback error | query={name!r} | {type(exc).__name__}: {exc}")
            if store:
                item = dict(candidate)
                item.update({
                    "name": store.title,
                    "verified": True,
                    "verification_source": store.provider,
                    "library_url": store.url,
                    "library_source": store.provider,
                    "pc_available": True,
                    "has_console": False,
                    "console_platforms": [],
                    "console_names": [],
                    "confidence": max(score, float(getattr(store, "score", 0.0) or 0.0)),
                })
                verified.append(item)
                continue
            web_failures += 1
            if web_failures >= WEB_FAILURE_THRESHOLD:
                log(f"Game detector web circuit breaker opened | failures={web_failures}")

        unresolved.append({
            "name": name,
            "detected_name": candidate.get("detected_name") or name,
            "confidence": score,
            "reason": "Could not verify title.",
        })

    verified = dedupe_verified_games(verified, max_items=MAX_VIDEO_CANDIDATES)
    log(
        f"Game detector verification gate complete | candidates={len(unique)} | verified={len(verified)} "
        f"unresolved={len(unresolved)} | web_attempts={web_attempts} | web_failures={web_failures}"
    )
    return verified, unresolved


_media._identify_from_evidence = _identify_from_evidence
_media._verify_and_enrich = _guarded_verify_and_enrich
_media._steam_prefix_completion = _steam_prefix_completion
_analyzer._steam_prefix_completion = _steam_prefix_completion
