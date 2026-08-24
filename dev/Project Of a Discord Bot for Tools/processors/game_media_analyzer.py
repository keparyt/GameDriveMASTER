"""Evidence-first, production-oriented game identification pipeline."""

import asyncio
import base64
import json
import os
import re
import shutil
import tempfile
from pathlib import Path

import aiohttp

from processors.game_analyzer import (
    MAX_GAMES, MAX_SOCIAL_MEDIA_ITEMS, MAX_TRANSCRIPT_CHARS,
    _deepseek_correct_name, _dedupe_candidates, _download_attachment,
    _download_url, _extract_direct_text, _result, _run, _tool, _transcribe,
    _verify_and_enrich,
)
from processors.robust_ocr import ocr_image as _ocr_image
from utils.helper import log

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434/api/chat")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "deepseek-r1:7b")
OLLAMA_VISION_MODEL = os.getenv("OLLAMA_VISION_MODEL", "").strip()
VIDEO_FRAME_COUNT = max(4, int(os.getenv("GAME_OCR_FRAME_COUNT", "10")))
MAX_OCR_CHARS_PER_FRAME = 2500
DESCRIPTION_MAX_CHARS = 6000
MEDIA_CONCURRENCY = max(1, int(os.getenv("GAME_MEDIA_CONCURRENCY", "3")))
URL_CONCURRENCY = max(1, int(os.getenv("GAME_URL_CONCURRENCY", "3")))
VISION_IMAGE_LIMIT = max(1, int(os.getenv("GAME_VISION_IMAGE_LIMIT", "6")))


def _media_type(path: Path) -> str:
    return "image" if path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"} else "video"


def _video_duration(video: Path) -> float:
    if not _tool("ffprobe"):
        return 0.0
    try:
        proc = _run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(video)], 30)
        return float(proc.stdout.strip()) if proc.returncode == 0 else 0.0
    except Exception:
        return 0.0


async def _ocr_video_full(video: Path, workdir: Path, index: int) -> tuple[str, list[Path]]:
    if not _tool("ffmpeg"):
        return "", []
    frame_dir = workdir / f"frames_{index}"
    frame_dir.mkdir(parents=True, exist_ok=True)
    duration = await asyncio.to_thread(_video_duration, video)
    count = VIDEO_FRAME_COUNT
    if duration > 180:
        count = min(count, 8)
    elif duration < 20:
        count = min(count, 6)

    # One ffmpeg pass is much faster than spawning ffmpeg once per timestamp.
    output_pattern = frame_dir / "frame_%03d.jpg"
    if duration > 0:
        fps = count / max(duration, 1.0)
        args = ["ffmpeg", "-y", "-i", str(video), "-vf", f"fps={fps:.6f},scale=1600:-1", "-frames:v", str(count), str(output_pattern)]
    else:
        args = ["ffmpeg", "-y", "-i", str(video), "-vf", "fps=1/2,scale=1600:-1", "-frames:v", str(count), str(output_pattern)]
    proc = await asyncio.to_thread(_run, args, 120)
    frames = sorted(frame_dir.glob("*.jpg")) if proc.returncode == 0 else []
    log(f"Game detector video OCR frames | {video.name} | {len(frames)} frames")
    if not frames:
        return "", []

    sem = asyncio.Semaphore(MEDIA_CONCURRENCY)
    async def one(frame: Path):
        async with sem:
            return frame, await _ocr_image(frame)
    results = await asyncio.gather(*(one(frame) for frame in frames), return_exceptions=True)
    parts = []
    for item in results:
        if isinstance(item, Exception):
            continue
        frame, ocr = item
        if ocr.strip():
            parts.append(f"{frame.name}:\n{ocr[:MAX_OCR_CHARS_PER_FRAME]}")
    return "\n\n".join(parts), frames


async def _vision_frame_analysis(frames: list[Path]) -> str:
    if not OLLAMA_VISION_MODEL or not frames:
        return ""
    step = max(1, len(frames) // VISION_IMAGE_LIMIT)
    selected = frames[::step][:VISION_IMAGE_LIMIT]
    images = []
    for frame in selected:
        try:
            images.append(base64.b64encode(await asyncio.to_thread(frame.read_bytes)).decode("ascii"))
        except OSError:
            pass
    if not images:
        return ""
    payload = {"model": OLLAMA_VISION_MODEL, "stream": False, "messages": [{"role": "user", "content": "Identify only concrete game evidence: logos, title screens, HUD, menus, named characters, maps or unmistakable gameplay. Do not guess from generic visuals. Return concise evidence.", "images": images}], "options": {"temperature": 0}}
    try:
        timeout = aiohttp.ClientTimeout(total=120)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(OLLAMA_URL, json=payload) as response:
                response.raise_for_status()
                body = await response.json()
        return str(body.get("message", {}).get("content", "")).strip()
    except Exception as exc:
        log(f"Game detector vision analysis error | {type(exc).__name__}: {exc}")
        return ""


def _build_evidence(media_evidence, metadata, descriptions) -> str:
    sections = []
    if media_evidence:
        sections.append("=== PRIMARY EVIDENCE: ACTUAL MEDIA ===\n" + "\n\n---\n\n".join(media_evidence))
    if metadata:
        sections.append("=== SECONDARY EVIDENCE: SOURCE METADATA ===\n" + "\n\n---\n\n".join(metadata))
    if descriptions:
        sections.append("=== LAST-RESORT EVIDENCE: DESCRIPTIONS / CAPTIONS ===\n" + "\n\n---\n\n".join(descriptions))
    return "\n\n==============================\n\n".join(sections)


async def _identify_from_evidence(evidence: str) -> dict:
    prompt = f'''Identify every distinct video game genuinely supported by the evidence.

Priority: actual media OCR/visual evidence > source metadata/audio > captions/descriptions.
A title appearing only in a description is not enough. OCR is noisy, so normalize obvious character mistakes only when supported by multiple evidence signals. Never invent a related title, sequel, remake or edition. Maximum {MAX_GAMES} games.

Return ONLY JSON: {{"candidates":[{{"name":"Game title","confidence":90,"reason":"short evidence","evidence_type":"ocr"}}]}}

EVIDENCE:\n{evidence[:50000]}'''
    payload = {"model": OLLAMA_MODEL, "stream": False, "messages": [{"role": "system", "content": "You are a strict game-identification engine. Prefer actual media over captions and never hallucinate titles."}, {"role": "user", "content": prompt}], "options": {"temperature": 0}}
    try:
        timeout = aiohttp.ClientTimeout(total=180)
        async with aiohttp.ClientSession(timeout=timeout) as session:
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
    return [c for c in candidates if str(c.get("evidence_type", "")).casefold().strip() != "description"]


async def _analyze_one_media(index: int, media: Path, workdir: Path, sem: asyncio.Semaphore):
    async with sem:
        kind = _media_type(media)
        log(f"Game detector analyzing media | {index} | {media.name} | {kind}")
        evidence = []
        if kind == "image":
            ocr = await _ocr_image(media)
            if ocr.strip():
                evidence.append(f"IMAGE #{index} OCR:\n{ocr[:7000]}")
                log(f"Game detector OCR complete | {media.name} | chars={len(ocr)}")
            else:
                log(f"Game detector OCR empty | {media.name}")
            visual = await _vision_frame_analysis([media])
            if visual:
                evidence.append(f"IMAGE #{index} VISUAL ANALYSIS:\n{visual[:5000]}")
            return evidence

        # Audio and frame extraction can overlap.
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
            corrected = await asyncio.gather(*(_deepseek_correct_name(str(c.get("name", "")).strip()) for c in candidates[:MAX_GAMES]))
            prepared = []
            for candidate, name in zip(candidates[:MAX_GAMES], corrected):
                original = str(candidate.get("name", "")).strip()
                item = dict(candidate)
                item.update({"detected_name": original, "name": name, "correction": name if name.casefold() != original.casefold() else None})
                prepared.append(item)
            games, unresolved = await _verify_and_enrich(prepared)
            return _result(games, unresolved)

        media_files, metadata, descriptions = [], [], []
        if text:
            metadata.append(f"Discord message text/context:\n{text[:10000]}")
        urls = list(dict.fromkeys(data.get("urls", [])))[:10]
        log(f"Game detector sources | urls={len(urls)} image_attachments={len(data.get('image_attachments', []))} video_attachments={len(data.get('video_attachments', []))}")
        url_sem = asyncio.Semaphore(URL_CONCURRENCY)
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
            if info.get("title"): metadata.append(f"Source title:\n{str(info['title'])[:6000]}")
            if info.get("uploader"): metadata.append(f"Source uploader/account:\n{str(info['uploader'])[:3000]}")
            if info.get("description"): descriptions.append(f"Source description/caption:\n{str(info['description'])[:DESCRIPTION_MAX_CHARS]}")
            for entry in info.get("entries", [])[:MAX_SOCIAL_MEDIA_ITEMS]:
                if isinstance(entry, dict):
                    if entry.get("title"): metadata.append(f"Media item title:\n{str(entry['title'])[:4000]}")
                    if entry.get("description"): descriptions.append(f"Media item description:\n{str(entry['description'])[:DESCRIPTION_MAX_CHARS]}")

        async def attachment(item):
            target = workdir / item["filename"]
            await _download_attachment(item["url"], target)
            return target
        attachments = list(data.get("video_attachments", [])) + list(data.get("image_attachments", []))
        if attachments:
            media_files.extend(await asyncio.gather(*(attachment(item) for item in attachments)))

        # Deduplicate paths before expensive processing.
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
        candidates = _filter_description_only_candidates(_dedupe_candidates(raw_candidates))
        log(f"Game detector candidates | raw={len(raw_candidates)} accepted={len(candidates)}")
        if not candidates:
            return {"status": "unknown", "message": "No game was supported strongly enough by the actual media."}
        games, unresolved = await _verify_and_enrich(candidates)
        log(f"Game detector verification complete | games={len(games)} unresolved={len(unresolved)}")
        return _result(games, unresolved)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
