"""High-precision wrapper around the existing media analyzer.

Adds deterministic extraction for title-card layouts such as:
    DEGREES OF SEPARATION (2 PLAYER) Split-Screen Puzzle Adventure
    HAVEN (2 PLAYER) Romantic Co-Op Open World Adventure
    ONIRISM (4 PLAYER) Co-Op 3D Action Adventure

The existing analyzer remains responsible for media downloading, OCR, vision,
verification and UI result formatting. This wrapper only strengthens the title
extraction stage so descriptive text cannot replace the actual title.
"""

import re

from processors import game_media_analyzer as _base
from utils.helper import log


# Words that frequently appear immediately after a real title on game-list cards.
_DESCRIPTOR_START = re.compile(
    r"\b(?:co[ -]?op|coop|multiplayer|single[ -]?player|split[ -]?screen|"
    r"romantic|action|adventure|puzzle|open[ -]?world|3d|2d|platformer|"
    r"survival|strategy|shooter|rpg|horror|sandbox|simulation)\b",
    re.I,
)

_PLAYER_CARD = re.compile(
    r"^\s*(?:[-*•\d.)]+\s*)?(?P<title>[^\n:|]{2,100}?)\s*"
    r"\(\s*\d+\s*players?\s*\)\b",
    re.I,
)

_TITLE_DESCRIPTOR = re.compile(
    r"^\s*(?:[-*•\d.)]+\s*)?(?P<title>[^\n:|]{2,100}?)\s*"
    r"[:|\-–—]\s*(?P<descriptor>.+)$",
    re.I,
)


def _normalize_ocr_line(value: str) -> str:
    value = re.sub(r"\s+", " ", str(value or "")).strip()
    value = re.sub(r"^[\s\-–—•*]+", "", value)
    return value.strip()


def _clean_title(value: str) -> str:
    value = _normalize_ocr_line(value)
    value = re.sub(r"\s*\(\s*\d+\s*players?\s*\)\s*$", "", value, flags=re.I)
    value = re.sub(r"^(?:title|game title|game)\s*[:=-]\s*", "", value, flags=re.I)
    return value.strip(" :|\t\r\n-–—")


def _title_hints_from_evidence(evidence: str) -> list[dict]:
    """Extract title-card patterns before asking the LLM to reason about them."""
    hints: list[dict] = []
    seen: set[str] = set()

    # OCR is grouped by frame, so inspect individual lines as well as nearby text.
    for raw_line in str(evidence or "").splitlines():
        line = _normalize_ocr_line(raw_line)
        if not line or line.lower().startswith(("frame_", "video #", "image #")):
            continue

        title = None
        match = _PLAYER_CARD.match(line)
        if match:
            title = _clean_title(match.group("title"))
        else:
            match = _TITLE_DESCRIPTOR.match(line)
            if match:
                descriptor = match.group("descriptor")
                # Only split on ':'/'-' when the right side looks like a descriptor.
                if _DESCRIPTOR_START.search(descriptor):
                    title = _clean_title(match.group("title"))

        if not title:
            continue
        if len(title) < 2 or len(title) > 100:
            continue
        key = re.sub(r"[^a-z0-9]+", "", title.casefold())
        if not key or key in seen:
            continue
        seen.add(key)
        hints.append({
            "name": title,
            "confidence": 99,
            "reason": "title extracted from OCR title-card structure",
            "evidence_type": "ocr_title_card",
        })

    return hints


async def _identify_from_evidence(evidence: str) -> dict:
    hints = _title_hints_from_evidence(evidence)
    if hints:
        hint_text = "\n".join(f"- {item['name']} (OCR title-card match)" for item in hints)
        evidence = (
            "=== HIGH-PRIORITY OCR TITLE-CARD HINTS ===\n"
            + hint_text
            + "\n\nThese hints come from structured text visible in the actual media. "
              "Treat them as candidate titles and verify them; do not replace them "
              "with the descriptive text following the player-count marker.\n\n"
            + evidence
        )
        log("Game detector title-card hints | " + ", ".join(item["name"] for item in hints))

    ai = await _base._identify_from_evidence(evidence)
    raw = ai.get("candidates", []) if isinstance(ai, dict) else []

    # Hints are primary-media evidence. Merge them first so an LLM that returns
    # only one title cannot silently discard other distinct title cards.
    merged = []
    seen = set()
    for item in hints + list(raw):
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip()
        key = re.sub(r"[^a-z0-9]+", "", name.casefold())
        if not key or key in seen:
            continue
        seen.add(key)
        merged.append(item)

    return {"candidates": merged}


async def analyze_game_input(data: dict) -> dict:
    # Patch only the extraction callback for this invocation. The base analyzer
    # still performs all existing OCR, media, verification and result handling.
    original = _base._identify_from_evidence
    _base._identify_from_evidence = _identify_from_evidence
    try:
        return await _base.analyze_game_input(data)
    finally:
        _base._identify_from_evidence = original
