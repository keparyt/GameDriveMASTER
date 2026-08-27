"""Reusable heuristics for filtering noisy OCR before expensive verification."""
from __future__ import annotations

import difflib
import math
import re
from typing import Iterable

NOISE_WORDS = {
    "all", "are", "best", "button", "caption", "captured", "click", "coming", "continue", "control", "controls",
    "co", "coop", "cooperative", "couch", "creature", "data", "demo", "description", "descriptions", "download",
    "drop", "early", "evidence", "free", "game", "games", "gameplay", "highly", "ingame", "liked", "loading",
    "menu", "more", "new", "online", "options", "pc", "play", "players", "player", "playstation", "press",
    "price", "promo", "promotional", "recommended", "sale", "screen", "select", "sent", "singleplayer", "steam",
    "summer", "terminal", "toggle", "trailer", "try", "tutorial", "ui", "unavailable", "watch", "xbox", "youtube",
}
PLATFORM_WORDS = {"pc", "playstation", "ps4", "ps5", "ps3", "xbox", "switch", "nintendo", "steam", "windows", "mac", "linux"}
SENTENCE_WORDS = {"a", "an", "and", "are", "as", "at", "captured", "for", "from", "has", "have", "if", "in", "into", "is", "it", "liked", "new", "of", "on", "that", "the", "these", "this", "to", "was", "were", "when", "while", "with", "you", "your"}
CONTROL_WORDS = {"press", "hold", "click", "select", "toggle", "drop", "open", "close", "menu", "options", "settings", "inventory", "continue", "loading", "pause", "back", "start", "confirm"}
GENRE_WORDS = {"action", "adventure", "arcade", "battle", "brawler", "fighting", "fps", "horror", "indie", "multiplayer", "open", "online", "platformer", "puzzle", "rpg", "roguelike", "sandbox", "shooter", "simulation", "singleplayer", "split", "strategy", "survival", "tactical", "world", "co", "coop", "cooperative", "couch"}
CAPTION_PREFIXES = {"if", "when", "why", "how", "what", "watch", "more", "best", "try", "you", "your"}


def normalize_title(value: str) -> str:
    value = str(value or "").casefold().replace("&", " and ")
    value = re.sub(r"[™®©]", "", value)
    value = re.sub(r"\b(?:\d+\s*)?(?:player|players)\b", "", value)
    value = re.sub(r"\b(?:demo|trial)\b", "", value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def compact_key(value: str) -> str:
    return normalize_title(value).replace(" ", "")


def clean_title(value: str) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip(" -*•—–:;,\t\r\n")
    text = re.sub(r"^(?:title|game title|game)\s*[:=-]\s*", "", text, flags=re.I)
    text = re.sub(r"\s*\((?:\d+\s*)?players?\)\s*$", "", text, flags=re.I)
    text = re.sub(r"\s*\[(?:\d+\s*)?players?\]\s*$", "", text, flags=re.I)
    text = re.sub(r"\s+(?:free\s+)?download(?:\s+\([^)]*\))?\s*$", "", text, flags=re.I)
    return text.strip(" -*•—–:;,\t\r\n")


def _words(value: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", str(value or "").casefold())


def rejection_reason(value: str) -> str | None:
    text = clean_title(value)
    words = _words(text)
    if not words:
        return "empty candidate"
    if len(text) > 90:
        return "too long for a game title"
    if len(words) > 8:
        return "sentence/description-like candidate"
    if re.fullmatch(r"(?:frame|scene|shot|image|video)[ _#-]*\d+(?:\s*ocr)?", text, re.I):
        return "frame/UI label"
    low = normalize_title(text)
    if low in {"primary evidence", "secondary evidence", "last resort evidence", "actual media", "source metadata"}:
        return "internal evidence label"
    noise = sum(word in NOISE_WORDS for word in words)
    platforms = sum(word in PLATFORM_WORDS for word in words)
    sentence = sum(word in SENTENCE_WORDS for word in words)
    controls = sum(word in CONTROL_WORDS for word in words)
    genres = sum(word in GENRE_WORDS for word in words)
    if platforms >= 2:
        return "platform list/UI text"
    if controls and (len(words) <= 3 or noise >= 2):
        return "control/UI text"
    # Caption lead-ins are rejected only when the following words also look
    # like recommendation/promotion copy. This keeps legitimate titles such as
    # "If Found..." from being discarded.
    if len(words) >= 2 and words[0] in CAPTION_PREFIXES:
        tail = words[1:]
        if any(word in NOISE_WORDS for word in tail) or any(word.startswith("you") for word in tail):
            return "caption/marketing lead-in"
    if any(len(word) == 1 for word in words) and len(words) <= 3 and re.search(r"[-–—]", text):
        return "fragmented OCR token"
    if len(words) >= 3 and noise >= max(2, len(words) // 2):
        return "OCR/UI/marketing noise"
    if len(words) >= 5 and sentence >= 3:
        return "sentence/description text"
    if len(words) >= 3 and genres >= 2 and noise >= 2:
        return "genre/feature description"
    if len(words) == 1 and (words[0] in NOISE_WORDS or words[0] in GENRE_WORDS):
        return "generic single-word candidate"
    return None


def is_plausible_title(value: str) -> bool:
    return rejection_reason(value) is None


def _token_set(value: str) -> set[str]:
    return set(_words(value))


def similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, normalize_title(a), normalize_title(b)).ratio()


def _is_partial_duplicate(a: str, b: str) -> bool:
    na, nb = normalize_title(a), normalize_title(b)
    if not na or not nb or na == nb:
        return True
    wa, wb = _token_set(na), _token_set(nb)
    if len(wa) >= len(wb):
        short, long = wb, wa
        short_text, long_text = nb, na
    else:
        short, long = wa, wb
        short_text, long_text = na, nb
    if short and short.issubset(long) and len(long) >= 3 and len(long - short) <= 5:
        return True
    if len(short) >= 2 and short.issubset(long) and len(long - short) <= 2:
        return True
    return similarity(short_text, long_text) >= 0.92


def _merge_duplicate_into(accepted: list[dict], index: int, item: dict) -> None:
    existing = accepted[index]
    existing_score = float(existing.get("confidence", 0) or 0)
    new_score = float(item.get("confidence", 0) or 0)
    if len(_token_set(item["name"])) > len(_token_set(existing["name"])) or new_score > existing_score:
        merged = dict(existing)
        merged.update(item)
        accepted[index] = merged
    canonical = accepted[index]
    accepted[:] = [record for i, record in enumerate(accepted) if i == index or not _is_partial_duplicate(record["name"], canonical["name"])]


def dedupe_candidates(candidates: Iterable[dict], max_items: int = 20) -> list[dict]:
    accepted: list[dict] = []
    for original in candidates:
        if not isinstance(original, dict):
            continue
        raw = str(original.get("name", "")).strip()
        name = clean_title(raw)
        reason = rejection_reason(name)
        if not name or reason:
            continue
        item = dict(original)
        item["name"] = name
        item.setdefault("detected_name", raw)
        duplicate_index = next((i for i, existing in enumerate(accepted) if _is_partial_duplicate(name, existing["name"])), None)
        if duplicate_index is None:
            accepted.append(item)
        else:
            _merge_duplicate_into(accepted, duplicate_index, item)
    return accepted[:max_items]


def dedupe_verified_games(games: Iterable[dict], max_items: int = 20) -> list[dict]:
    output: list[dict] = []
    for game in games:
        if not isinstance(game, dict):
            continue
        name = clean_title(game.get("name", ""))
        if not name:
            continue
        item = dict(game)
        item["name"] = name
        duplicate_index = next((i for i, existing in enumerate(output) if _is_partial_duplicate(name, existing.get("name", ""))), None)
        if duplicate_index is None:
            output.append(item)
        else:
            _merge_duplicate_into(output, duplicate_index, item)
    return output[:max_items]


def confidence_percent(value) -> int:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return 0
    if not math.isfinite(score):
        return 0
    if score <= 1.0:
        score *= 100.0
    return max(0, min(100, round(score)))
