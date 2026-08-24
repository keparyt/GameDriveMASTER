"""Evidence-first game identification with adaptive OCR/vision sampling."""

import asyncio
import base64
import json
import re
import shutil
import tempfile
from pathlib import Path

import aiohttp

from config import (
    DESCRIPTION_MAX_CHARS, FFMPEG_BINARY, FFPROBE_BINARY, IMAGE_EXTENSIONS,
    MAX_EVIDENCE_CHARS, MAX_GAMES, MAX_OCR_CHARS_PER_FRAME,
    MAX_SOCIAL_MEDIA_ITEMS, MAX_SOURCE_URLS, MAX_TRANSCRIPT_CHARS,
    MEDIA_CONCURRENCY, OLLAMA_MODEL, OLLAMA_TIMEOUT_SECONDS,
    OLLAMA_TEMPERATURE, OLLAMA_URL, OLLAMA_USER_AGENT, OLLAMA_VISION_MODEL,
    OLLAMA_VISION_TIMEOUT_SECONDS, VIDEO_FRAME_COUNT, VISION_IMAGE_LIMIT,
)
from processors.game_analyzer import (
    _dedupe_candidates, _download_attachment, _download_url, _extract_direct_text,
    _result, _run, _tool, _transcribe, _verify_and_enrich,
)
from processors.robust_ocr import ocr_image as _ocr_image
from utils.helper import log

_GENERIC_WORDS = {
    "action", "adventure", "arcade", "battle", "brawler", "co", "coop", "cooperative",
    "competitive", "craft", "fighting", "fps", "game", "games", "horror", "indie",
    "multiplayer", "open", "online", "platformer", "puzzle", "rpg", "roguelike",
    "romantic", "sandbox", "screen", "shooter", "simulation", "single", "split",
    "strategy", "survival", "tactical", "third", "world", "3d", "2d", "free", "to",
    "play", "new", "best", "coming", "soon", "early", "access", "demo", "steam",
    "primary", "secondary", "evidence", "actual", "media", "source", "metadata",
    "last", "resort", "description", "descriptions", "caption", "captions", "words",
}

_INTERNAL_EVIDENCE = {
    "primary evidence", "secondary evidence", "last-resort evidence",
    "actual media", "source metadata", "descriptions / captions",
}


def _media_type(path: Path) -> str:
    return "image" if path.suffix.lower() in IMAGE_EXTENSIONS else "video"


def _video_duration(video: Path) -> float:
    if not _tool(FFPROBE_BINARY):
        return 0.0
    try:
        proc = _run([
            FFPROBE_BINARY, "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", str(video)
        ], 30)
        return float(proc.stdout.strip()) if proc.returncode == 0 else 0.0
    except Exception:
        return 0.0


def _adaptive_frame_count(duration: float) -> int:
    """Sample enough of a video to catch short title cards and multiple games.

    The old implementation reduced a 1:49 video to six frames. That is much too
    sparse for compilation/reel content where a game may appear for only a few
    seconds. The configured value is treated as a minimum, not a hard maximum.
    """
    base = max(12, int(VIDEO_FRAME_COUNT or 0))
    if duration <= 0:
        return max(base, 18)
    if duration < 15:
        return max(base, 12)
    if duration < 30:
        return max(base, 16)
    if duration < 60:
        return max(base, 20)
    if duration < 120:
        return max(base, 28)
    if duration < 180:
        return max(base, 36)
    return max(base, min(48, int(duration / 4)))


async def _ocr_video_full(video: Path, workdir: Path, index: int):
    if not _tool(FFMPEG_BINARY):
        return "", []
    frame_dir = workdir / f"frames_{index}"
    frame_dir.mkdir(parents=True, exist_ok=True)
    duration = await asyncio.to_thread(_video_duration, video)
    count = _adaptive_frame_count(duration)
    output = frame_dir / "frame_%03d.jpg"

    if duration > 0:
        # Include the end of the video as well; title cards are often shown there.
        fps = count / max(duration, 1.0)
        args = [
            FFMPEG_BINARY, "-y", "-i", str(video), "-vf",
            f"fps={fps:.6f},scale=1600:-1", "-frames:v", str(count), str(output)
        ]
    else:
        args = [
            FFMPEG_BINARY, "-y", "-i", str(video), "-vf",
            "fps=1/2,scale=1600:-1", "-frames:v", str(count), str(output)
        ]

    proc = await asyncio.to_thread(_run, args, 180)
    frames = sorted(frame_dir.glob("*.jpg")) if proc.returncode == 0 else []
    log(
        f"Game detector video OCR frames | {video.name} | {len(frames)} frames"
        f" | duration={duration:.1f}s | target={count}"
    )
    if not frames:
        return "", []

    sem = asyncio.Semaphore(MEDIA_CONCURRENCY)

    async def one(frame):
        async with sem:
            try:
                return frame, await _ocr_image(frame)
            except Exception as exc:
                return frame, f"[OCR ERROR: {type(exc).__name__}]"

    results = await asyncio.gather(*(one(f) for f in frames), return_exceptions=True)
    parts = []
    for item in results:
        if isinstance(item, Exception):
            continue
        frame, text = item
        text = str(text or "").strip()
        if text:
            # Keep every frame boundary. This is important: the LLM must be able
            # to see that a title appeared repeatedly rather than receiving a bag
            # of OCR words with no temporal context.
            parts.append(f"FRAME {frame.stem.replace('frame_', '')}:\n{text[:MAX_OCR_CHARS_PER_FRAME]}")
    return "\n\n--- FRAME ---\n\n".join(parts), frames


async def _vision_frame_analysis(frames):
    if not OLLAMA_VISION_MODEL or not frames:
        return ""
    limit = max(1, int(VISION_IMAGE_LIMIT or 1))
    # Spread images over the complete video rather than taking only the first N.
    indexes = []
    if len(frames) <= limit:
        indexes = list(range(len(frames)))
    else:
        indexes = [round(i * (len(frames) - 1) / (limit - 1)) for i in range(limit)] if limit > 1 else [0]
    selected = []
    seen = set()
    for idx in indexes:
        if 0 <= idx < len(frames) and idx not in seen:
            seen.add(idx)
            selected.append(frames[idx])

    images = []
    for frame in selected:
        try:
            images.append(base64.b64encode(await asyncio.to_thread(frame.read_bytes)).decode("ascii"))
        except OSError:
            pass
    if not images:
        return ""

    payload = {
        "model": OLLAMA_VISION_MODEL,
        "stream": False,
        "messages": [{
            "role": "user",
            "content": (
                "You are identifying video games from screenshots. Inspect EVERY supplied frame. "
                "Look specifically for title/logo text, title screens, game UI, distinctive proper "
                "names, character names, locations, and other concrete identifiers. "
                "Return each possible game title separately and explain which frame supports it. "
                "Do not turn genres or marketing phrases into titles. "
                "Never use phrases like 'Split-Screen Puzzle Adventure' or 'Co-op 3D Action Adventure' "
                "as a game title. If a title is partly obscured, preserve the visible spelling and "
                "mark it as uncertain rather than inventing a different game."
            ),
            "images": images,
        }],
        "options": {"temperature": OLLAMA_TEMPERATURE},
    }
    try:
        timeout = aiohttp.ClientTimeout(total=OLLAMA_VISION_TIMEOUT_SECONDS)
        async with aiohttp.ClientSession(
            headers={"User-Agent": OLLAMA_USER_AGENT}, timeout=timeout
        ) as session:
            async with session.post(OLLAMA_URL, json=payload) as response:
                response.raise_for_status()
                body = await response.json()
        return str(body.get("message", {}).get("content", "")).strip()
    except Exception as exc:
        log(f"Game detector vision analysis error | {type(exc).__name__}: {exc}")
        return ""


def _build_evidence(media_evidence, metadata, descriptions):
    sections = []
    if media_evidence:
        sections.append("=== PRIMARY EVIDENCE: ACTUAL MEDIA ===\n" + "\n\n---\n\n".join(media_evidence))
    if metadata:
        sections.append("=== SECONDARY EVIDENCE: SOURCE METADATA ===\n" + "\n\n---\n\n".join(metadata))
    if descriptions:
        sections.append("=== LAST-RESORT EVIDENCE: DESCRIPTIONS / CAPTIONS ===\n" + "\n\n---\n\n".join(descriptions))
    return "\n\n==============================\n\n".join(sections)


def _clean_ai_title(name: str):
    name = re.sub(r"\s+", " ", str(name or "")).strip(" \t\r\n-*•—–")
    name = re.sub(r"^(?:title|game title|game)\s*[:=-]\s*", "", name, flags=re.I)
    name = re.sub(r"^['\"]?(?:primary|secondary|last[- ]resort)\s+evidence['\"]?\s*[:=-]?\s*", "", name, flags=re.I)
    # Strip accidental evidence labels, not real game subtitles.
    low = name.casefold().strip(" :;,-")
    if low in _INTERNAL_EVIDENCE or low.startswith("==="):
        return ""
    if ":" in name:
        left, right = [x.strip() for x in name.split(":", 1)]
        words = re.findall(r"[a-z0-9]+", right.casefold())
        generic = sum(w in _GENERIC_WORDS for w in words)
        if len(words) >= 2 and generic >= max(2, len(words) // 2):
            name = left
    return name.strip(" :;,-")


def _is_generic_candidate(name: str) -> bool:
    low = name.casefold().strip(" =:-")
    if low in _INTERNAL_EVIDENCE or low.startswith("==="):
        return True
    words = re.findall(r"[a-z0-9]+", low)
    if not words:
        return True
    generic = sum(w in _GENERIC_WORDS for w in words)
    if len(words) == 1:
        return words[0] in _GENERIC_WORDS
    return generic >= len(words) - 1 and generic >= 2


def _prepare_candidates(raw):
    prepared = []
    for candidate in raw:
        if not isinstance(candidate, dict):
            continue
        item = dict(candidate)
        original = str(item.get("name", "")).strip()
        cleaned = _clean_ai_title(original)
        if not cleaned or _is_generic_candidate(cleaned):
            log(f"Game detector candidate rejected | raw={original!r} | reason=generic/non-title")
            continue
        item["name"] = cleaned
        item["detected_name"] = original
        item["correction"] = cleaned if cleaned != original else None
        prepared.append(item)
    return _dedupe_candidates(prepared)


def _parse_candidates(content):
    """Parse strict JSON even when a local model adds a small amount of prose."""
    if not content:
        return []
    match = re.search(r"\{.*\}", content, re.DOTALL)
    if not match:
        return []
    try:
        value = json.loads(match.group(0))
    except json.JSONDecodeError:
        return []
    return value.get("candidates", []) if isinstance(value, dict) else []


async def _identify_from_evidence(evidence: str, pass_name="primary") -> dict:
    focus = (
        "FIRST inspect frame-by-frame OCR and visual evidence. Extract titles that appear in the actual media. "
        "Treat every frame as independent evidence and do not assume the video contains only one game. "
        "Look for short title cards that occur in only one or two frames."
        if pass_name == "primary" else
        "Perform an independent second pass. Search for game titles that the first pass could have missed, "
        "especially short-lived title cards, OCR misspellings, logos, and names visible in screenshots. "
        "Do not copy generic genre phrases. Return multiple games when multiple distinct games are present."
    )
    prompt = f'''Identify EVERY DISTINCT VIDEO GAME TITLE actually supported by the supplied evidence.

{focus}

RULES:
- Return proper game titles only, not genres, feature lists, descriptions, captions, or sentences.
- NEVER return phrases such as "Split-Screen Puzzle Adventure" or "Co-op 3D Action Adventure" as titles.
- If OCR says "GameName: Co-op 3D Action Adventure", return GameName unless the suffix is clearly an official subtitle.
- OCR may contain one-character errors. Correct them only when another frame, audio, or visual evidence supports the correction.
- A title visible in actual media is stronger than metadata. Metadata is only corroboration.
- Do not invent sequels/remakes/editions.
- It is valid to return several games from one compilation video.
- If there is no defensible game title, return an empty array.

Return ONLY JSON:
{{"candidates":[{{"name":"Exact game title","confidence":95,"reason":"brief concrete evidence","evidence_type":"ocr|audio|visual|metadata"}}]}}

EVIDENCE:
{evidence[:MAX_EVIDENCE_CHARS]}'''
    payload = {
        "model": OLLAMA_MODEL,
        "stream": False,
        "messages": [
            {"role": "system", "content": "You are a strict video-game title extraction engine. Never invent titles."},
            {"role": "user", "content": prompt},
        ],
        "options": {"temperature": min(float(OLLAMA_TEMPERATURE), 0.15)},
    }
    try:
        timeout = aiohttp.ClientTimeout(total=OLLAMA_TIMEOUT_SECONDS)
        async with aiohttp.ClientSession(
            headers={"User-Agent": OLLAMA_USER_AGENT}, timeout=timeout
        ) as session:
            async with session.post(OLLAMA_URL, json=payload) as response:
                response.raise_for_status()
                body = await response.json()
        return {"candidates": _parse_candidates(str(body.get("message", {}).get("content", "")))}
    except Exception as exc:
        log(f"Game detector evidence analysis error | pass={pass_name} | {type(exc).__name__}: {exc}")
        return {}


def _filter_description_only_candidates(candidates):
    return [
        c for c in candidates
        if str(c.get("evidence_type", "")).casefold().strip() not in {"description", "caption"}
    ]


def _merge_candidates(*candidate_lists):
    merged = []
    for candidates in candidate_lists:
        for candidate in candidates or []:
            if not isinstance(candidate, dict):
                continue
            # Prefer stronger evidence/confidence when the two passes find the same title.
            name = str(candidate.get("name", "")).strip()
            key = re.sub(r"[^a-z0-9]+", "", name.casefold())
            found = next((x for x in merged if re.sub(r"[^a-z0-9]+", "", str(x.get("name", "")).casefold()) == key), None)
            if found is None:
                merged.append(dict(candidate))
            elif float(candidate.get("confidence", 0) or 0) > float(found.get("confidence", 0) or 0):
                found.update(candidate)
    return merged


async def _analyze_one_media(index, media, workdir, sem):
    async with sem:
        kind = _media_type(media)
        log(f"Game detector analyzing media | {index} | {media.name} | {kind}")
        evidence = []
        if kind == "image":
            ocr = await _ocr_image(media)
            if ocr.strip():
                evidence.append(f"IMAGE #{index} OCR:\n{ocr[:7000]}")
                log(f"Game detector OCR complete | {media.name} | chars={len(ocr)}")
            visual = await _vision_frame_analysis([media])
            if visual:
                evidence.append(f"IMAGE #{index} VISUAL ANALYSIS:\n{visual[:5000]}")
            return evidence

        transcript_task = asyncio.create_task(_transcribe(media, workdir, index))
        frame_task = asyncio.create_task(_ocr_video_full(media, workdir, index))
        transcript, frame_result = await asyncio.gather(transcript_task, frame_task)
        frame_ocr, frames = frame_result
        if transcript:
            evidence.append(f"VIDEO #{index} AUDIO TRANSCRIPT:\n{transcript[:MAX_TRANSCRIPT_CHARS]}")
        if frame_ocr:
            evidence.append(f"VIDEO #{index} FRAME-BY-FRAME OCR:\n{frame_ocr[:22000]}")
        visual = await _vision_frame_analysis(frames)
        if visual:
            evidence.append(f"VIDEO #{index} VISUAL ANALYSIS:\n{visual[:9000]}")
        return evidence


async def analyze_game_input(data: dict) -> dict:
    workdir = Path(tempfile.mkdtemp(prefix="game-detector-evidence-"))
    log(f"Game detector analysis started | workdir={workdir.name}")
    try:
        text = str(data.get("text", "")).strip()
        has_media = bool(data.get("urls") or data.get("video_attachments") or data.get("image_attachments"))
        if text and not has_media and not data.get("source_types"):
            candidates = await _extract_direct_text(text)
            prepared = []
            for candidate in candidates[:MAX_GAMES]:
                original = str(candidate.get("name", "")).strip()
                item = dict(candidate)
                item.update({"detected_name": original, "name": original, "correction": None})
                prepared.append(item)
            games, unresolved = await _verify_and_enrich(prepared)
            return _result(games, unresolved)

        media_files, metadata, descriptions = [], [], []
        if text:
            metadata.append(f"Discord message text/context:\n{text[:10000]}")
        urls = list(dict.fromkeys(data.get("urls", [])))[:MAX_SOURCE_URLS]
        log(
            f"Game detector sources | urls={len(urls)} "
            f"image_attachments={len(data.get('image_attachments', []))} "
            f"video_attachments={len(data.get('video_attachments', []))}"
        )
        url_sem = asyncio.Semaphore(max(1, MEDIA_CONCURRENCY))

        async def extract(url):
            async with url_sem:
                try:
                    log(f"Game detector extracting media | {url}")
                    return url, await _download_url(url, workdir)
                except Exception as exc:
                    log(f"Game detector media URL error | {url} | {type(exc).__name__}: {exc}")
                    return url, ({}, [])

        for _, result in await asyncio.gather(*(extract(url) for url in urls)):
            info, downloaded = result
            media_files.extend(downloaded)
            if info.get("title"):
                metadata.append(f"Source title:\n{str(info['title'])[:6000]}")
            if info.get("uploader"):
                metadata.append(f"Source uploader/account:\n{str(info['uploader'])[:3000]}")
            if info.get("description"):
                descriptions.append(f"Source description/caption:\n{str(info['description'])[:DESCRIPTION_MAX_CHARS]}")
            for entry in info.get("entries", [])[:MAX_SOCIAL_MEDIA_ITEMS]:
                if isinstance(entry, dict):
                    if entry.get("title"):
                        metadata.append(f"Media item title:\n{str(entry['title'])[:4000]}")
                    if entry.get("description"):
                        descriptions.append(f"Media item description:\n{str(entry['description'])[:DESCRIPTION_MAX_CHARS]}")

        async def attachment(item):
            target = workdir / item["filename"]
            await _download_attachment(item["url"], target)
            return target

        attachments = list(data.get("video_attachments", [])) + list(data.get("image_attachments", []))
        if attachments:
            results = await asyncio.gather(*(attachment(item) for item in attachments), return_exceptions=True)
            media_files.extend(x for x in results if isinstance(x, Path))
        media_files = list(dict.fromkeys(media_files))[:MAX_SOCIAL_MEDIA_ITEMS]
        log(f"Game detector media ready | count={len(media_files)}")
        if not media_files:
            return {"status": "unknown", "message": "No readable media could be extracted from the supplied source."}

        sem = asyncio.Semaphore(MEDIA_CONCURRENCY)
        batches = await asyncio.gather(
            *(_analyze_one_media(i, media, workdir, sem) for i, media in enumerate(media_files, 1)),
            return_exceptions=True,
        )
        media_evidence = []
        for batch in batches:
            if isinstance(batch, Exception):
                log(f"Game detector media analysis error | {type(batch).__name__}: {batch}")
            else:
                media_evidence.extend(batch)
        log(f"Game detector evidence collected | sections={len(media_evidence)}")
        evidence = _build_evidence(media_evidence, metadata, descriptions)
        if not evidence:
            return {"status": "unknown", "message": "No readable media or source evidence was found."}

        # Two independent extraction passes greatly reduce the chance that a local
        # model notices one game and silently ignores the others in a compilation.
        first = await _identify_from_evidence(evidence, "primary")
        second = await _identify_from_evidence(evidence, "recovery")
        raw_candidates = _merge_candidates(
            first.get("candidates", []) if isinstance(first, dict) else [],
            second.get("candidates", []) if isinstance(second, dict) else [],
        )
        candidates = _prepare_candidates(_filter_description_only_candidates(raw_candidates))
        log(
            f"Game detector candidates | pass1={len(first.get('candidates', [])) if isinstance(first, dict) else 0} "
            f"pass2={len(second.get('candidates', [])) if isinstance(second, dict) else 0} "
            f"raw={len(raw_candidates)} accepted={len(candidates)}"
        )
        if not candidates:
            return {"status": "unknown", "message": "No game was supported strongly enough by the actual media."}

        games, unresolved = await _verify_and_enrich(candidates)
        log(f"Game detector verification complete | games={len(games)} unresolved={len(unresolved)}")
        return _result(games, unresolved)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
