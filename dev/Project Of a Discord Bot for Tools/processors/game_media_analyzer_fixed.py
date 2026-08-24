"""High-precision game identification wrapper.

Strengthens the existing downloader/OCR/verification pipeline with conservative
OCR title extraction, evidence-backed candidate filtering, Ollama localhost
fallback, and tolerant Steam title correction for small OCR errors.
"""

import difflib
import json
import re
from urllib.parse import urlparse

import aiohttp
from bs4 import BeautifulSoup

from config import (
    MAX_EVIDENCE_CHARS, OLLAMA_MODEL, OLLAMA_TIMEOUT_SECONDS, OLLAMA_TEMPERATURE,
    OLLAMA_URL, OLLAMA_USER_AGENT, STEAM_COUNTRY, STEAM_LANGUAGE,
    STEAM_REQUEST_TIMEOUT_SECONDS, STEAM_SEARCH_URL, STEAM_USER_AGENT,
)
from processors import game_media_analyzer as _base
from utils.helper import log

_ORIGINAL_IDENTIFY_FROM_EVIDENCE = _base._identify_from_evidence
_ORIGINAL_VERIFY_AND_ENRICH = _base._verify_and_enrich

_GENERIC_WORDS = {
    "action", "adventure", "arcade", "battle", "brawler", "co", "coop", "cooperative",
    "competitive", "craft", "fighting", "fps", "game", "games", "genre", "horror", "indie",
    "multiplayer", "open", "online", "platformer", "puzzle", "rpg", "roguelike", "romantic",
    "sandbox", "screen", "shooter", "simulation", "single", "split", "strategy", "survival",
    "tactical", "third", "world", "players", "player", "3d", "2d", "free", "to", "play",
    "new", "best", "coming", "soon", "early", "access", "demo", "steam", "recurring", "ocr",
    "words", "gamer", "highly", "fun", "these", "all", "are", "you", "your", "with", "into",
    "for", "the", "and", "or", "if", "this", "that", "when", "while", "very", "want", "can",
    "will", "have", "has", "from",
}
_SECTION_HEADERS = {
    "primary evidence actual media",
    "secondary evidence source metadata",
    "last resort evidence descriptions captions",
}
_LABEL_PREFIXES = (
    "source title:", "source uploader/account:", "source description/caption:",
    "media item title:", "media item description:", "discord message text/context:",
)
_DESCRIPTOR_START = re.compile(r"\b(?:co[ -]?op|coop|multiplayer|single[ -]?player|split[ -]?screen|romantic|action|adventure|puzzle|open[ -]?world|3d|2d|platformer|survival|strategy|shooter|rpg|horror|sandbox|simulation)\b", re.I)
_PLAYER_CARD = re.compile(r"^\s*(?:[-*•\d.)]+\s*)?(?P<title>[^\n:|]{2,100}?)\s*\(\s*\d+\s*players?\s*\)\b", re.I)
_TITLE_DESCRIPTOR = re.compile(r"^\s*(?:[-*•\d.)]+\s*)?(?P<title>[^\n:|]{2,70}?)\s*[:|]\s*(?P<descriptor>.+)$", re.I)


def _normalize(value):
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).strip()


def _normalize_ocr_line(value: str) -> str:
    value = re.sub(r"\s+", " ", str(value or "")).strip()
    return re.sub(r"^[\s\-–—•*]+", "", value).strip()


def _is_section_or_label(value: str) -> bool:
    normalized = _normalize(value)
    if normalized in _SECTION_HEADERS:
        return True
    if str(value).strip().startswith("==="):
        return True
    return any(str(value).strip().casefold().startswith(prefix) for prefix in _LABEL_PREFIXES)


def _clean_title(value: str) -> str:
    value = _normalize_ocr_line(value)
    value = re.sub(r"\s*\(\s*\d+\s*players?\s*\)\s*$", "", value, flags=re.I)
    value = re.sub(r"^(?:title|game title|game)\s*[:=-]\s*", "", value, flags=re.I)
    return value.strip(" :|\t\r\n-–—")


def _is_generic_title(value: str) -> bool:
    words = re.findall(r"[a-z0-9]+", value.casefold())
    if not words or len(words) > 8:
        return True
    generic = sum(word in _GENERIC_WORDS for word in words)
    if len(words) == 1:
        return words[0] in _GENERIC_WORDS
    return generic >= max(3, len(words) - 1)


def _looks_like_sentence(value: str) -> bool:
    words = re.findall(r"[a-z0-9']+", value.casefold())
    if len(words) < 4:
        return False
    sentence_words = {"if", "you", "your", "the", "this", "these", "those", "are", "is", "for", "with", "into", "when", "while", "all", "very", "highly", "best", "want", "can", "will", "have", "has", "from", "and", "or", "to"}
    return sum(w in sentence_words for w in words) >= 2


def _title_hints_from_evidence(evidence: str) -> list[dict]:
    hints, seen = [], set()
    for raw_line in str(evidence or "").splitlines():
        line = _normalize_ocr_line(raw_line)
        if not line or _is_section_or_label(line) or line.lower().startswith(("frame_", "video #", "image #")):
            continue
        candidates = []
        match = _PLAYER_CARD.match(line)
        if match:
            candidates.append(_clean_title(match.group("title")))
        else:
            match = _TITLE_DESCRIPTOR.match(line)
            if match:
                left = _clean_title(match.group("title"))
                words = re.findall(r"[a-z0-9']+", left.casefold())
                if 1 <= len(words) <= 6 and not _is_generic_title(left) and not _looks_like_sentence(left) and _DESCRIPTOR_START.search(match.group("descriptor")):
                    candidates.append(left)
            letters = [c for c in line if c.isalpha()]
            upper_ratio = sum(c.isupper() for c in letters) / len(letters) if letters else 0
            if 2 <= len(re.findall(r"[a-z0-9']+", line.casefold())) <= 6 and upper_ratio >= 0.80 and not _is_generic_title(line) and not _looks_like_sentence(line):
                candidates.append(_clean_title(line))
        for title in candidates:
            if len(title) < 2 or len(title) > 80 or _is_generic_title(title) or _is_section_or_label(title):
                continue
            key = _normalize(title).replace(" ", "")
            if key and key not in seen:
                seen.add(key)
                hints.append({"name": title, "confidence": 99, "reason": "strong OCR title-card structure", "evidence_type": "ocr_title_card"})
    return hints


def _candidate_supported_by_evidence(name: str, evidence: str) -> bool:
    if _is_section_or_label(name):
        return False
    query = _normalize(name)
    normalized = _normalize(evidence)
    if not query:
        return False
    if query in normalized:
        return True
    q_words = query.split()
    for raw_line in evidence.splitlines():
        if _is_section_or_label(raw_line):
            continue
        line = _normalize(raw_line)
        words = line.split()
        if len(words) < len(q_words):
            continue
        for i in range(len(words) - len(q_words) + 1):
            window = words[i:i + len(q_words)]
            ratios = [difflib.SequenceMatcher(None, a, b).ratio() for a, b in zip(q_words, window)]
            if ratios and sum(ratios) / len(ratios) >= 0.78 and all(r >= 0.65 for r in ratios):
                return True
    return False


async def _local_ollama_extract(evidence: str) -> dict:
    parsed = urlparse(OLLAMA_URL)
    urls = [OLLAMA_URL]
    if parsed.hostname in {"localhost", "127.0.0.1", "0.0.0.0", "192.168.28.3"}:
        urls.append("http://127.0.0.1:11434/api/chat")
    prompt = f'''Identify every DISTINCT video game title actually supported by this evidence.
Return ONLY JSON: {{"candidates":[{{"name":"Exact title","confidence":95,"reason":"concrete evidence","evidence_type":"ocr|audio|visual|metadata"}}]}}
Never output section headings, evidence labels, genres, marketing sentences, creator commentary, or generic words. Correct obvious OCR mistakes only when the evidence supports the correction. Prefer exact visible title/logo text. Do not invent a title absent from the evidence.
EVIDENCE:\n{evidence[:MAX_EVIDENCE_CHARS]}'''
    payload = {"model": OLLAMA_MODEL, "stream": False, "messages": [{"role": "system", "content": "You are a strict game-title extraction engine."}, {"role": "user", "content": prompt}], "options": {"temperature": OLLAMA_TEMPERATURE}}
    for url in dict.fromkeys(urls):
        try:
            timeout = aiohttp.ClientTimeout(total=OLLAMA_TIMEOUT_SECONDS)
            async with aiohttp.ClientSession(headers={"User-Agent": OLLAMA_USER_AGENT}, timeout=timeout) as session:
                async with session.post(url, json=payload) as response:
                    response.raise_for_status()
                    body = await response.json()
            match = re.search(r"\{.*\}", str(body.get("message", {}).get("content", "")), re.DOTALL)
            if match:
                log(f"Game detector Ollama | endpoint={url}")
                result = json.loads(match.group(0))
                return result if isinstance(result, dict) else {}
        except Exception as exc:
            log(f"Game detector Ollama endpoint failed | {url} | {type(exc).__name__}: {exc}")
    return {}


async def _identify_from_evidence(evidence: str) -> dict:
    hints = _title_hints_from_evidence(evidence)
    if hints:
        log("Game detector title-card hints | " + ", ".join(x["name"] for x in hints))
    try:
        ai = await _ORIGINAL_IDENTIFY_FROM_EVIDENCE(evidence)
    except Exception:
        ai = {}
    if not isinstance(ai, dict) or not ai.get("candidates"):
        ai = await _local_ollama_extract(evidence)

    merged, seen = [], set()
    for item in hints + list(ai.get("candidates", []) if isinstance(ai, dict) else []):
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip()
        if not name:
            continue
        if _is_section_or_label(name):
            log(f"Game detector candidate rejected | raw={name!r} | reason=evidence section/label")
            continue
        evidence_type = str(item.get("evidence_type", "")).casefold()
        if evidence_type != "ocr_title_card" and not _candidate_supported_by_evidence(name, evidence):
            log(f"Game detector candidate rejected | raw={name!r} | reason=not supported by evidence")
            continue
        cleaned = re.sub(r"\s+", " ", name).strip(" -*•—–:;,.\t\r\n")
        if _is_generic_title(cleaned) or _looks_like_sentence(cleaned):
            log(f"Game detector candidate rejected | raw={name!r} | reason=generic/non-title")
            continue
        key = _normalize(cleaned).replace(" ", "")
        if key and key not in seen:
            seen.add(key)
            item = dict(item)
            item["name"] = cleaned
            merged.append(item)
    return {"candidates": merged}


async def _steam_ocr_correction(candidate):
    name = str(candidate.get("name", "")).strip()
    if len(name) < 3:
        return candidate
    try:
        async with aiohttp.ClientSession(headers={"User-Agent": STEAM_USER_AGENT}) as session:
            async with session.get(STEAM_SEARCH_URL, params={"term": name, "cc": STEAM_COUNTRY, "l": STEAM_LANGUAGE}, timeout=STEAM_REQUEST_TIMEOUT_SECONDS) as response:
                if response.status != 200:
                    return candidate
                html = await response.text()
        ranked = []
        for row in BeautifulSoup(html, "html.parser").select("a.search_result_row[data-ds-appid]")[:30]:
            node = row.select_one(".title")
            appid = row.get("data-ds-appid")
            if not node or not appid or not str(appid).isdigit():
                continue
            title = node.get_text(" ", strip=True)
            seq = difflib.SequenceMatcher(None, _normalize(name), _normalize(title)).ratio()
            word_values = []
            for q in _normalize(name).split():
                word_values.append(max((difflib.SequenceMatcher(None, q, t).ratio() for t in _normalize(title).split()), default=0))
            word = sum(word_values) / len(word_values) if word_values else 0
            score = (seq * 0.65) + (word * 0.35)
            ranked.append((score, seq, word, title, int(appid)))
        if not ranked:
            return candidate
        score, seq, word, title, appid = max(ranked)
        if seq >= 0.90 and word >= 0.82 and score >= 0.64:
            item = dict(candidate)
            item.update({"name": title, "steam_appid": appid, "steam_url": f"https://store.steampowered.com/app/{appid}/", "steam_verified": True, "confidence": max(float(item.get("confidence", 0)), score * 100), "correction": title if title != name else item.get("correction")})
            log(f"Steam OCR correction | query={name!r} | best={title!r} | score={score:.3f} | seq={seq:.3f} | word={word:.3f} | accepted")
            return item
        log(f"Steam OCR correction | query={name!r} | best={title!r} | score={score:.3f} | seq={seq:.3f} | word={word:.3f} | rejected")
    except Exception as exc:
        log(f"Steam OCR correction error | {name} | {type(exc).__name__}: {exc}")
    return candidate


async def _verify_and_enrich(candidates):
    repaired = []
    for candidate in candidates:
        repaired.append(await _steam_ocr_correction(candidate))
    return await _ORIGINAL_VERIFY_AND_ENRICH(repaired)


async def analyze_game_input(data: dict) -> dict:
    original_identify = _base._identify_from_evidence
    original_verify = _base._verify_and_enrich
    _base._identify_from_evidence = _identify_from_evidence
    _base._verify_and_enrich = _verify_and_enrich
    try:
        return await _base.analyze_game_input(data)
    finally:
        _base._identify_from_evidence = original_identify
        _base._verify_and_enrich = original_verify
