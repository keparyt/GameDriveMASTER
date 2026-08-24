"""Evidence-first game identification with OCR-noise handling and title-quality filtering."""

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
}


def _media_type(path: Path) -> str:
    return "image" if path.suffix.lower() in IMAGE_EXTENSIONS else "video"


def _video_duration(video: Path) -> float:
    if not _tool(FFPROBE_BINARY):
        return 0.0
    try:
        proc = _run([FFPROBE_BINARY, "-v", "error", "-show_entries", "format=duration",
                     "-of", "default=noprint_wrappers=1:nokey=1", str(video)], 30)
        return float(proc.stdout.strip()) if proc.returncode == 0 else 0.0
    except Exception:
        return 0.0


async def _ocr_video_full(video: Path, workdir: Path, index: int):
    if not _tool(FFMPEG_BINARY):
        return "", []
    frame_dir = workdir / f"frames_{index}"
    frame_dir.mkdir(parents=True, exist_ok=True)
    duration = await asyncio.to_thread(_video_duration, video)
    count = max(1, VIDEO_FRAME_COUNT)
    if duration > 180:
        count = min(count, 8)
    elif duration < 20:
        count = min(count, 6)
    output = frame_dir / "frame_%03d.jpg"
    if duration > 0:
        fps = count / max(duration, 1.0)
        args = [FFMPEG_BINARY, "-y", "-i", str(video), "-vf",
                f"fps={fps:.6f},scale=1600:-1", "-frames:v", str(count), str(output)]
    else:
        args = [FFMPEG_BINARY, "-y", "-i", str(video), "-vf", "fps=1/2,scale=1600:-1",
                "-frames:v", str(count), str(output)]
    proc = await asyncio.to_thread(_run, args, 120)
    frames = sorted(frame_dir.glob("*.jpg")) if proc.returncode == 0 else []
    log(f"Game detector video OCR frames | {video.name} | {len(frames)} frames")
    if not frames:
        return "", []
    sem = asyncio.Semaphore(MEDIA_CONCURRENCY)

    async def one(frame):
        async with sem:
            return frame, await _ocr_image(frame)

    results = await asyncio.gather(*(one(f) for f in frames), return_exceptions=True)
    parts = []
    for item in results:
        if isinstance(item, Exception):
            continue
        frame, text = item
        if text.strip():
            parts.append(f"{frame.name}:\n{text[:MAX_OCR_CHARS_PER_FRAME]}")
    return "\n\n".join(parts), frames


async def _vision_frame_analysis(frames):
    if not OLLAMA_VISION_MODEL or not frames:
        return ""
    step = max(1, len(frames) // VISION_IMAGE_LIMIT)
    images = []
    for frame in frames[::step][:VISION_IMAGE_LIMIT]:
        try:
            images.append(base64.b64encode(await asyncio.to_thread(frame.read_bytes)).decode("ascii"))
        except OSError:
            pass
    if not images:
        return ""
    payload = {
        "model": OLLAMA_VISION_MODEL, "stream": False,
        "messages": [{"role": "user", "content": (
            "Inspect these game-media frames. Extract ONLY concrete identifying evidence. "
            "If visible, report exact game title/logo text, title-screen text, HUD names, "
            "character names, map names or unmistakable UI. Do NOT describe genres such as "
            "'3D action adventure' as a title and do not guess a game from generic gameplay."
        ), "images": images}],
        "options": {"temperature": OLLAMA_TEMPERATURE},
    }
    try:
        timeout = aiohttp.ClientTimeout(total=OLLAMA_VISION_TIMEOUT_SECONDS)
        async with aiohttp.ClientSession(headers={"User-Agent": OLLAMA_USER_AGENT}, timeout=timeout) as session:
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
    if ":" in name:
        left, right = [x.strip() for x in name.split(":", 1)]
        words = re.findall(r"[a-z0-9]+", right.casefold())
        generic = sum(w in _GENERIC_WORDS for w in words)
        if len(words) >= 2 and generic >= max(2, len(words) // 2):
            name = left
    return name.strip(" :;,-")


def _is_generic_candidate(name: str) -> bool:
    words = re.findall(r"[a-z0-9]+", name.casefold())
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


async def _identify_from_evidence(evidence: str) -> dict:
    prompt = f'''Identify every DISTINCT VIDEO GAME TITLE actually supported by the evidence.

IMPORTANT:
- Return the game's proper title, NOT its genre, marketing copy, feature list, or sentence.
- NEVER return phrases like "Split Screen Puzzle Adventure", "Cheat Co-op", "3D Action Adventure", "Romantic Co-op Open World Adventure" or similar descriptive text.
- If OCR says "GameName: Co-op 3D Action Adventure", return only "GameName" unless the text after the colon is clearly an official subtitle.
- OCR may miss/repeat characters. Correct obvious OCR errors only when the surrounding evidence supports the correction.
- Prefer title/logo text visible in the actual media. Audio can support a title. Captions/descriptions are weak evidence and must never be the sole reason for a candidate.
- Do not invent sequels, remakes, editions or related games.
- If there is no real game title, return an empty candidates array.

Return ONLY JSON:
{{"candidates":[{{"name":"Exact game title","confidence":95,"reason":"brief concrete evidence","evidence_type":"ocr|audio|visual|metadata"}}]}}

EVIDENCE:\n{evidence[:MAX_EVIDENCE_CHARS]}'''
    payload = {
        "model": OLLAMA_MODEL, "stream": False,
        "messages": [
            {"role": "system", "content": "You are a strict game-title extraction engine. Output proper game names only."},
            {"role": "user", "content": prompt},
        ],
        "options": {"temperature": OLLAMA_TEMPERATURE},
    }
    try:
        timeout = aiohttp.ClientTimeout(total=OLLAMA_TIMEOUT_SECONDS)
        async with aiohttp.ClientSession(headers={"User-Agent": OLLAMA_USER_AGENT}, timeout=timeout) as session:
            async with session.post(OLLAMA_URL, json=payload) as response:
                response.raise_for_status()
                body = await response.json()
        content = body.get("message", {}).get("content", "")
        match = re.search(r"\{.*\}", content, re.DOTALL)
        return json.loads(match.group(0)) if match else {}
    except Exception as exc:
        log(f"Game detector evidence analysis error | {type(exc).__name__}: {exc}")
        return {}


def _filter_description_only_candidates(candidates):
    return [c for c in candidates if str(c.get("evidence_type", "")).casefold().strip() not in {"description", "caption"}]


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
            evidence.append(f"VIDEO #{index} OCR:\n{frame_ocr[:14000]}")
        visual = await _vision_frame_analysis(frames)
        if visual:
            evidence.append(f"VIDEO #{index} VISUAL ANALYSIS:\n{visual[:7000]}")
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
        log(f"Game detector sources | urls={len(urls)} image_attachments={len(data.get('image_attachments', []))} video_attachments={len(data.get('video_attachments', []))}")
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
        batches = await asyncio.gather(*(_analyze_one_media(i, media, workdir, sem) for i, media in enumerate(media_files, 1)), return_exceptions=True)
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
        ai = await _identify_from_evidence(evidence)
        raw_candidates = ai.get("candidates", []) if isinstance(ai, dict) else []
        candidates = _prepare_candidates(_filter_description_only_candidates(raw_candidates))
        log(f"Game detector candidates | raw={len(raw_candidates)} accepted={len(candidates)}")
        if not candidates:
            return {"status": "unknown", "message": "No game was supported strongly enough by the actual media."}
        games, unresolved = await _verify_and_enrich(candidates)
        log(f"Game detector verification complete | games={len(games)} unresolved={len(unresolved)}")
        return _result(games, unresolved)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
