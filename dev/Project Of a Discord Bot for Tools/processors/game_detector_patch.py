"""Runtime patch for the game media detector.

Adds an AI evidence-cleaning stage before title extraction. OCR is noisy: UI
labels, marketing text, player counts and descriptions can look like game
names. A local model is used first to normalize/correct the parsed evidence,
then the normal title extractor and database verification run on that cleaned
material. The original OCR remains available as a fallback when the cleanup
model is unavailable or times out.
"""

import difflib
import json
import re

import aiohttp

from config import (
    MAX_EVIDENCE_CHARS,
    OLLAMA_MODEL,
    OLLAMA_TIMEOUT_SECONDS,
    OLLAMA_TEMPERATURE,
    OLLAMA_URL,
    OLLAMA_USER_AGENT,
)
from processors import game_media_analyzer as _base
from utils.helper import log

_ORIGINAL_IDENTIFY = _base._identify_from_evidence

_FRAME_LABEL_RE = re.compile(r"^\s*(?:frame|image|video)\s*[_#-]?\s*\d+(?:\s*ocr)?\s*:?\s*$", re.I)
_INTERNAL_RE = re.compile(r"^\s*(?:===|primary evidence|secondary evidence|last[- ]resort evidence)\b", re.I)
_LABEL_PREFIXES = (
    "source title:", "source uploader/account:", "source description/caption:",
    "media item title:", "media item description:", "discord message text/context:",
)
_GENERIC = {
    "frame", "image", "video", "ocr", "words", "primary", "secondary", "evidence",
    "actual", "media", "source", "metadata", "description", "descriptions", "caption",
    "captions", "split", "screen", "puzzle", "adventure", "action", "romantic", "co",
    "coop", "cooperative", "multiplayer", "open", "world", "3d", "2d", "game", "games",
    "gamer", "highly", "fun", "these", "all", "are", "you", "your", "with", "into",
    "for", "the", "and", "or", "if", "this", "that", "when", "while", "very", "want",
    "can", "will", "have", "has", "from", "genre", "players", "player", "new", "best",
    "cheap", "couch", "free", "download", "sale", "steam", "summer", "part",
}
_SENTENCE_WORDS = {
    "if", "you", "your", "the", "this", "these", "those", "are", "is", "for", "with",
    "into", "when", "while", "all", "very", "highly", "best", "want", "can", "will",
    "have", "has", "from", "and", "or", "to", "a", "an", "of",
}


def _norm(value):
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).strip()


def _bad_label(value):
    text = str(value or "").strip()
    if not text:
        return True
    if _FRAME_LABEL_RE.match(text) or _INTERNAL_RE.match(text):
        return True
    low = text.casefold().strip(" :;-=")
    if low in {"primary evidence actual media", "secondary evidence source metadata", "last resort evidence descriptions captions"}:
        return True
    return any(low.startswith(prefix) for prefix in _LABEL_PREFIXES)


def _looks_generic(value):
    words = re.findall(r"[a-z0-9]+", str(value or "").casefold())
    if not words or len(words) > 8:
        return True
    generic = sum(w in _GENERIC for w in words)
    if len(words) == 1:
        return words[0] in _GENERIC
    return generic >= max(3, len(words) - 1)


def _looks_sentence(value):
    words = re.findall(r"[a-z0-9']+", str(value or "").casefold())
    return len(words) >= 5 and sum(w in _SENTENCE_WORDS for w in words) >= 3


def _clean(value):
    value = re.sub(r"\s+", " ", str(value or "")).strip(" -*•—–:;,\t\r\n")
    value = re.sub(r"^(?:title|game title|game)\s*[:=-]\s*", "", value, flags=re.I)
    # Common OCR/UI suffixes. These are metadata, not part of the title.
    value = re.sub(r"\s*\((?:\d+\s*)?(?:player|players)\)\s*$", "", value, flags=re.I)
    value = re.sub(r"\s*\[(?:\d+\s*)?(?:player|players)\]\s*$", "", value, flags=re.I)
    value = re.sub(r"\s+(?:free\s+)?download(?:\s+\([^)]*\))?\s*$", "", value, flags=re.I)
    return value.strip(" -*•—–:;,\t\r\n")


def _ocr_title_hints(evidence):
    """Extract conservative OCR candidates without treating FRAME 001 as a title."""
    hints = []
    seen = set()
    frame_blocks = re.split(r"---\s*FRAME\s*---", str(evidence or ""), flags=re.I)
    for block in frame_blocks:
        for raw in block.splitlines():
            line = re.sub(r"\s+", " ", raw).strip()
            if not line or _bad_label(line):
                continue
            line = re.sub(r"^\s*(?:FRAME|IMAGE|VIDEO)\s*[_#-]?\s*\d+\s*:?\s*", "", line, flags=re.I).strip()
            if not line or _bad_label(line):
                continue
            words = re.findall(r"[a-z0-9']+", line.casefold())
            if not 1 <= len(words) <= 6 or len(line) > 80:
                continue
            letters = [c for c in line if c.isalpha()]
            upper_ratio = sum(c.isupper() for c in letters) / len(letters) if letters else 0
            title_case = any(c.isupper() for c in line) and any(c.islower() for c in line)
            if _looks_generic(line) or _looks_sentence(line):
                continue
            if not (upper_ratio >= 0.70 or title_case):
                continue
            name = _clean(line)
            key = _norm(name).replace(" ", "")
            if key and key not in seen:
                seen.add(key)
                hints.append({
                    "name": name,
                    "confidence": 96,
                    "reason": "compact OCR title-shaped text in actual media",
                    "evidence_type": "ocr_title_card",
                })
    return hints


def _compact_evidence(evidence):
    """Keep every video frame represented before the base model's global cap."""
    text = str(evidence or "")
    blocks = re.split(r"(---\s*FRAME\s*---)", text, flags=re.I)
    frame_parts = []
    other_parts = []
    current = []
    in_frame = False
    for part in blocks:
        if re.fullmatch(r"---\s*FRAME\s*---", part.strip(), flags=re.I):
            if current:
                frame_parts.append("\n".join(current))
            current = []
            in_frame = True
            continue
        if in_frame:
            current.append(part)
        else:
            other_parts.append(part)
    if current:
        frame_parts.append("\n".join(current))

    if not frame_parts:
        return text[:MAX_EVIDENCE_CHARS]

    budget = max(600, int(MAX_EVIDENCE_CHARS * 0.82))
    per_frame = max(500, min(2400, budget // max(1, len(frame_parts))))
    compact_frames = []
    for i, frame in enumerate(frame_parts, 1):
        frame = frame.strip()
        if len(frame) > per_frame:
            head = per_frame // 2
            tail = per_frame - head
            frame = frame[:head] + "\n[... OCR middle omitted ...]\n" + frame[-tail:]
        compact_frames.append(f"FRAME {i:03d}:\n{frame}")

    prefix = "\n\n".join(x.strip() for x in other_parts if x.strip())
    compact = (prefix + "\n\n" if prefix else "") + "\n\n--- FRAME ---\n\n".join(compact_frames)
    return compact[:MAX_EVIDENCE_CHARS]


async def _ai_clean_parsed_evidence(evidence):
    """Use the local LLM as an OCR correction/filtering pass.

    This is intentionally BEFORE game database verification. The model is not
    asked to identify arbitrary games from memory; it is asked to repair noisy
    text that is already present in the supplied evidence. It may remove UI,
    marketing and genre text, normalize player-count suffixes, merge repeated
    OCR, and correct obvious OCR spelling errors when supported by the frames.
    """
    compact = _compact_evidence(evidence)
    if not compact.strip():
        return compact, []

    prompt = f'''Clean noisy OCR/transcript evidence from a video-game identification scan.

Your job is NOT to guess games from general knowledge. Your job is to repair the parsed text using ONLY information present in the evidence.

KEEP:
- actual game titles visible in frames
- title-card text
- distinctive proper names when they clearly identify a game
- repeated/consistent title spellings
- a title even if it appears in only one frame

CORRECT:
- OCR spelling errors when the surrounding evidence makes the intended title clear
- missing spaces/punctuation/capitalization
- examples: "Degrees ofseperation" -> "Degrees of Separation"; "Haven(2Player)" -> "Haven"; "Onirism (4 Player)" -> "Onirism"

REMOVE:
- FRAME 001 / FRAME 002 labels
- OCR/debug labels
- "Steam Summer Sale", "Free Download", prices and promotional text
- genres such as "Split-Screen Puzzle Adventure"
- feature descriptions such as "Romantic Co-op openworld Adventure"
- player counts such as "(2 Player)" / "(4 Player)"
- uploader/source labels, Instagram IDs, captions and ordinary sentences
- words created only because OCR split a title or UI element apart

IMPORTANT:
- Do NOT invent a game title that is not supported by the supplied text.
- Do NOT turn a generic phrase into a game title.
- If an OCR spelling is uncertain, keep the original spelling instead of hallucinating.
- Preserve frame numbers so later verification knows where the evidence came from.
- Multiple games are expected in compilation/recommendation videos.

Return ONLY JSON in this form:
{{
  "cleaned_evidence": "FRAME 001: ...\\nFRAME 002: ...",
  "game_hints": [
    {{"name":"Corrected Game Title","confidence":96,"reason":"why the evidence supports the correction","evidence_type":"ocr_correction"}}
  ]
}}

EVIDENCE:
{compact}'''

    payload = {
        "model": OLLAMA_MODEL,
        "stream": False,
        "messages": [
            {"role": "system", "content": "You are an OCR cleanup engine for video-game titles. Never invent titles."},
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
        log(f"Game detector AI evidence cleanup | input={len(compact)} chars | output={len(cleaned)} chars | hints={len(hints) if isinstance(hints, list) else 0}")
        return _compact_evidence(cleaned), hints if isinstance(hints, list) else []
    except Exception as exc:
        log(f"Game detector AI evidence cleanup unavailable | {type(exc).__name__}: {exc} | using original OCR")
        return compact, []


def _filter_candidates(candidates, evidence):
    result = []
    seen = set()
    for item in candidates or []:
        if not isinstance(item, dict):
            continue
        raw = str(item.get("name", "")).strip()
        if not raw:
            continue
        if _bad_label(raw):
            log(f"Game detector candidate rejected | raw={raw!r} | reason=frame/evidence label")
            continue
        name = _clean(raw)
        if _bad_label(name) or _looks_generic(name) or _looks_sentence(name):
            log(f"Game detector candidate rejected | raw={raw!r} | reason=generic/non-title")
            continue
        if _FRAME_LABEL_RE.match(name):
            log(f"Game detector candidate rejected | raw={raw!r} | reason=frame label")
            continue
        key = _norm(name).replace(" ", "")
        if key in seen:
            continue
        seen.add(key)
        copy = dict(item)
        copy["name"] = name
        result.append(copy)
    return result


async def _identify_from_evidence(evidence: str, pass_name="primary"):
    # AI cleanup happens BEFORE title extraction. This prevents database search
    # from ever seeing most OCR junk and gives the extractor corrected spellings.
    cleaned, ai_hints = await _ai_clean_parsed_evidence(evidence)
    hints = _ocr_title_hints(cleaned)
    if ai_hints:
        for item in ai_hints:
            if isinstance(item, dict) and item.get("name"):
                hints.append(item)
    if hints:
        log(f"Game detector cleaned OCR title hints | pass={pass_name} | " + ", ".join(x["name"] for x in hints[:20]))

    try:
        ai = await _ORIGINAL_IDENTIFY(cleaned, pass_name)
    except TypeError:
        ai = await _ORIGINAL_IDENTIFY(cleaned)
    except Exception as exc:
        log(f"Game detector base evidence extraction failed | pass={pass_name} | {type(exc).__name__}: {exc}")
        ai = {}

    ai_candidates = ai.get("candidates", []) if isinstance(ai, dict) else []
    merged = []
    seen = set()
    for item in list(hints) + list(ai_candidates):
        if not isinstance(item, dict):
            continue
        name = _clean(item.get("name", ""))
        if not name or _bad_label(name) or _looks_generic(name) or _looks_sentence(name):
            continue
        key = _norm(name).replace(" ", "")
        if key in seen:
            continue
        seen.add(key)
        copy = dict(item)
        copy["name"] = name
        merged.append(copy)
    return {"candidates": merged}
