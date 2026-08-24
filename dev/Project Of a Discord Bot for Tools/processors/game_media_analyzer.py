"""Evidence-first game identification pipeline.

Social-media captions/descriptions are deliberately treated as the weakest signal.
Actual downloaded media is inspected first, including every image and video in a
carousel. OCR is sampled across the full video duration.
"""

import asyncio
import base64
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import aiohttp

from processors.game_analyzer import (
    MAX_GAMES, MAX_SOCIAL_MEDIA_ITEMS, MAX_TRANSCRIPT_CHARS,
    _clean_candidate_name, _dedupe_candidates, _deepseek_correct_name,
    _download_attachment, _download_url, _extract_direct_text, _ocr_image,
    _result, _transcribe, _verify_and_enrich, _tool, _run,
)
from utils.helper import log

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434/api/chat")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "deepseek-r1:7b")
OLLAMA_VISION_MODEL = os.getenv("OLLAMA_VISION_MODEL", "").strip()
VIDEO_FRAME_COUNT = max(8, int(os.getenv("GAME_OCR_FRAME_COUNT", "18")))
MAX_OCR_CHARS_PER_FRAME = 3500
DESCRIPTION_MAX_CHARS = 6000


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
        log(f"Game detector video OCR skipped | {video.name} | ffmpeg unavailable")
        return "", []
    frame_dir = workdir / f"full_frames_{index}"
    frame_dir.mkdir(parents=True, exist_ok=True)
    duration = await asyncio.to_thread(_video_duration, video)
    count = VIDEO_FRAME_COUNT
    frames: list[Path] = []
    if duration > 0:
        timestamps = [duration * i / max(1, count - 1) for i in range(count)]
        for frame_index, timestamp in enumerate(timestamps):
            output = frame_dir / f"frame_{frame_index:03d}.jpg"
            proc = await asyncio.to_thread(_run, ["ffmpeg", "-y", "-ss", f"{timestamp:.3f}", "-i", str(video), "-frames:v", "1", "-vf", "scale=1600:-1", str(output)], 30)
            if proc.returncode == 0 and output.exists():
                frames.append(output)
    else:
        proc = await asyncio.to_thread(_run, ["ffmpeg", "-y", "-i", str(video), "-vf", "fps=1/2,scale=1600:-1", "-frames:v", str(count), str(frame_dir / "frame_%03d.jpg")], 120)
        if proc.returncode == 0:
            frames = sorted(frame_dir.glob("*.jpg"))
    log(f"Game detector video OCR frames | {video.name} | {len(frames)} frames")
    parts = []
    for frame in frames:
        ocr = await _ocr_image(frame)
        if ocr.strip():
            parts.append(f"{frame.name}:\n{ocr[:MAX_OCR_CHARS_PER_FRAME]}")
    return "\n\n".join(parts), frames


async def _vision_frame_analysis(frames: list[Path]) -> str:
    if not OLLAMA_VISION_MODEL or not frames:
        return ""
    selected = frames[::max(1, len(frames) // 8)][:8]
    images = []
    for frame in selected:
        try:
            images.append(base64.b64encode(await asyncio.to_thread(frame.read_bytes)).decode("ascii"))
        except OSError:
            continue
    if not images:
        return ""
    payload = {"model": OLLAMA_VISION_MODEL, "stream": False, "messages": [{"role": "user", "content": "Analyze these game-media frames. Identify only concrete evidence such as logos, title screens, HUD, character names, menus, maps, items, characters, or unmistakable gameplay features. Do not guess a game name from generic graphics. Return concise evidence.", "images": images}], "options": {"temperature": 0}}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(OLLAMA_URL, json=payload, timeout=aiohttp.ClientTimeout(total=180)) as response:
                response.raise_for_status()
                body = await response.json()
        return str(body.get("message", {}).get("content", "")).strip()
    except Exception as exc:
        log(f"Game detector vision analysis error | {type(exc).__name__}: {exc}")
        return ""


def _build_evidence(media_evidence: list[str], metadata: list[str], descriptions: list[str]) -> str:
    sections = []
    if media_evidence:
        sections.append("=== PRIMARY EVIDENCE: ACTUAL MEDIA ===\n" + "\n\n---\n\n".join(media_evidence))
    if metadata:
        sections.append("=== SECONDARY EVIDENCE: SOURCE METADATA ===\n" + "\n\n---\n\n".join(metadata))
    if descriptions:
        sections.append("=== LAST-RESORT EVIDENCE: DESCRIPTIONS / CAPTIONS / HASHTAGS ===\n" + "\n\n---\n\n".join(descriptions))
    return "\n\n==============================\n\n".join(sections)


async def _identify_from_evidence(evidence: str) -> dict:
    prompt = f"""Identify every video game genuinely supported by the evidence.

STRICT RULES:
1. PRIMARY: actual media evidence: OCR from every image/video frame and concrete visual evidence.
2. SECONDARY: source metadata and audio transcript.
3. LAST: descriptions/captions/hashtags. They MUST NOT establish a game by themselves.
4. A title appearing only in a description is not enough.
5. Do not blindly trust AI-generated social-media descriptions.
6. Do not invent sequels, remakes, editions, or related games.
7. If evidence conflicts, prefer the strongest actual-media evidence.
8. Return every distinct game supported by the media. Maximum {MAX_GAMES}.

For each candidate return name, confidence 0-100, reason, evidence_type (ocr|visual|audio|metadata|description).
Return ONLY JSON: {{"candidates":[{{"name":"Game title","confidence":90,"reason":"...","evidence_type":"ocr"}}]}}

EVIDENCE:
{evidence[:65000]}"""
    payload = {"model": OLLAMA_MODEL, "stream": False, "messages": [{"role": "system", "content": "You are a strict game-identification engine. Media evidence outranks captions. Never hallucinate titles."}, {"role": "user", "content": prompt}], "options": {"temperature": 0}}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(OLLAMA_URL, json=payload, timeout=aiohttp.ClientTimeout(total=240)) as response:
                response.raise_for_status()
                body = await response.json()
        content = body.get("message", {}).get("content", "")
        match = re.search(r"\{.*\}", content, re.DOTALL)
        return json.loads(match.group(0)) if match else {}
    except Exception as exc:
        log(f"Game detector evidence analysis error | {type(exc).__name__}: {exc}")
        return {}


def _filter_description_only_candidates(candidates: list[dict]) -> list[dict]:
    return [c for c in candidates if str(c.get("evidence_type", "")).casefold().strip() != "description"]


async def analyze_game_input(data: dict) -> dict:
    workdir = Path(tempfile.mkdtemp(prefix="game-detector-evidence-"))
    log(f"Game detector analysis started | workdir={workdir.name}")
    try:
        text = str(data.get("text", "")).strip()
        has_media = bool(data.get("urls") or data.get("video_attachments") or data.get("image_attachments"))
        if text and not has_media and not data.get("source_types"):
            candidates = await _extract_direct_text(text)
            corrected = []
            for candidate in candidates[:MAX_GAMES]:
                original = str(candidate.get("name", "")).strip()
                name = await _deepseek_correct_name(original)
                item = dict(candidate)
                item["detected_name"] = original
                item["name"] = name
                item["correction"] = name if name.casefold() != original.casefold() else None
                corrected.append(item)
            games, unresolved = await _verify_and_enrich(corrected)
            return _result(games, unresolved)

        media_files: list[Path] = []
        metadata: list[str] = []
        descriptions: list[str] = []
        media_evidence: list[str] = []
        if text:
            metadata.append(f"Discord message text/context:\n{text[:10000]}")

        urls = list(dict.fromkeys(data.get("urls", [])))
        log(f"Game detector sources | urls={len(urls)} image_attachments={len(data.get('image_attachments', []))} video_attachments={len(data.get('video_attachments', []))}")
        for url in urls[:10]:
            log(f"Game detector extracting media | {url}")
            try:
                info, downloaded = await _download_url(url, workdir)
                media_files.extend(downloaded)
                log(f"Game detector media extracted | {url} | files={len(downloaded)}")
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
            except Exception as exc:
                log(f"Game detector media URL error | {url} | {type(exc).__name__}: {exc}")

        for item in data.get("video_attachments", []):
            target = workdir / item["filename"]
            await _download_attachment(item["url"], target)
            media_files.append(target)
        for item in data.get("image_attachments", []):
            target = workdir / item["filename"]
            await _download_attachment(item["url"], target)
            media_files.append(target)

        log(f"Game detector media ready | count={len(media_files)}")
        if not media_files:
            log("Game detector no media extracted | stopping before LLM")
            return {"status": "unknown", "message": "No readable media could be extracted from the supplied source."}

        for index, media in enumerate(media_files[:MAX_SOCIAL_MEDIA_ITEMS], 1):
            log(f"Game detector analyzing media | {index}/{min(len(media_files), MAX_SOCIAL_MEDIA_ITEMS)} | {media.name} | {_media_type(media)}")
            if _media_type(media) == "image":
                try:
                    ocr = await _ocr_image(media)
                    if ocr.strip():
                        media_evidence.append(f"IMAGE #{index} OCR:\n{ocr[:7000]}")
                        log(f"Game detector OCR complete | {media.name} | chars={len(ocr)}")
                    else:
                        log(f"Game detector OCR empty | {media.name}")
                except Exception as exc:
                    log(f"Game detector OCR error | {media.name} | {type(exc).__name__}: {exc}")
                visual = await _vision_frame_analysis([media])
                if visual:
                    media_evidence.append(f"IMAGE #{index} VISUAL ANALYSIS:\n{visual[:7000]}")
                continue

            transcript = await _transcribe(media, workdir, index)
            if transcript:
                media_evidence.append(f"VIDEO #{index} AUDIO TRANSCRIPT:\n{transcript[:MAX_TRANSCRIPT_CHARS]}")
            frame_ocr, frames = await _ocr_video_full(media, workdir, index)
            if frame_ocr:
                media_evidence.append(f"VIDEO #{index} OCR ACROSS FULL DURATION:\n{frame_ocr[:16000]}")
            visual = await _vision_frame_analysis(frames)
            if visual:
                media_evidence.append(f"VIDEO #{index} VISUAL ANALYSIS:\n{visual[:9000]}")

        log(f"Game detector evidence collected | sections={len(media_evidence)}")
        evidence = _build_evidence(media_evidence, metadata, descriptions)
        if not evidence:
            return {"status": "unknown", "message": "No readable media or source evidence was found."}
        log("Game detector sending evidence to identification model")
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
