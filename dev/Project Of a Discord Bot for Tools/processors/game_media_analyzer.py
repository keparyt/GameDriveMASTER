"""Evidence-first game identification pipeline.

Social-media captions/descriptions are deliberately treated as the weakest signal.
The actual downloaded media is inspected first, with OCR sampled across the full
video duration. Storefront databases are used only after media evidence produces
candidate names.
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
    MAX_GAMES,
    MAX_SOCIAL_MEDIA_ITEMS,
    MAX_TRANSCRIPT_CHARS,
    _clean_candidate_name,
    _dedupe_candidates,
    _deepseek_correct_name,
    _download_attachment,
    _download_url,
    _extract_direct_text,
    _ocr_image,
    _result,
    _transcribe,
    _verify_and_enrich,
    _tool,
    _run,
)
from utils.helper import log

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434/api/chat")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "deepseek-r1:7b")
OLLAMA_VISION_MODEL = os.getenv("OLLAMA_VISION_MODEL", "").strip()
VIDEO_FRAME_COUNT = max(8, int(os.getenv("GAME_OCR_FRAME_COUNT", "18")))
MAX_OCR_CHARS_PER_FRAME = 3500
DESCRIPTION_MAX_CHARS = 6000


def _media_type(path: Path) -> str:
    if path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}:
        return "image"
    return "video"


def _video_duration(video: Path) -> float:
    if not _tool("ffprobe"):
        return 0.0
    try:
        proc = _run([
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", str(video)
        ], 30)
        return float(proc.stdout.strip()) if proc.returncode == 0 else 0.0
    except Exception:
        return 0.0


async def _ocr_video_full(video: Path, workdir: Path, index: int) -> tuple[str, list[Path]]:
    """OCR frames distributed across the ENTIRE video, not only its first seconds."""
    if not _tool("ffmpeg"):
        return "", []

    frame_dir = workdir / f"full_frames_{index}"
    frame_dir.mkdir(parents=True, exist_ok=True)
    duration = await asyncio.to_thread(_video_duration, video)
    count = VIDEO_FRAME_COUNT

    if duration > 0:
        # Explicit timestamps guarantee coverage from beginning to end.
        timestamps = [duration * i / max(1, count - 1) for i in range(count)]
        frames = []
        for frame_index, timestamp in enumerate(timestamps):
            output = frame_dir / f"frame_{frame_index:03d}.jpg"
            proc = await asyncio.to_thread(_run, [
                "ffmpeg", "-y", "-ss", f"{timestamp:.3f}", "-i", str(video),
                "-frames:v", "1", "-vf", "scale=1600:-1", str(output)
            ], 30)
            if proc.returncode == 0 and output.exists():
                frames.append(output)
    else:
        # Fallback for media where ffprobe cannot read duration.
        proc = await asyncio.to_thread(_run, [
            "ffmpeg", "-y", "-i", str(video), "-vf", "fps=1/2,scale=1600:-1",
            "-frames:v", str(count), str(frame_dir / "frame_%03d.jpg")
        ], 120)
        frames = sorted(frame_dir.glob("*.jpg")) if proc.returncode == 0 else []

    parts = []
    for frame in frames:
        ocr = await _ocr_image(frame)
        if ocr.strip():
            parts.append(f"{frame.name}:\n{ocr[:MAX_OCR_CHARS_PER_FRAME]}")

    return "\n\n".join(parts), frames


async def _vision_frame_analysis(frames: list[Path]) -> str:
    """Optional visual reasoning through an Ollama vision model.

    Disabled unless OLLAMA_VISION_MODEL is configured, so the normal text model
    is never accidentally sent image payloads it cannot understand.
    """
    if not OLLAMA_VISION_MODEL or not frames:
        return ""

    selected = frames[::max(1, len(frames) // 8)][:8]
    images = []
    for frame in selected:
        try:
            data = await asyncio.to_thread(frame.read_bytes)
            images.append(base64.b64encode(data).decode("ascii"))
        except OSError:
            continue
    if not images:
        return ""

    payload = {
        "model": OLLAMA_VISION_MODEL,
        "stream": False,
        "messages": [{
            "role": "user",
            "content": (
                "Analyze these game-media frames for identification. Identify only concrete visual evidence: "
                "game logos, title screens, HUD layouts, character names, menus, distinctive UI, maps, items, "
                "characters, or unmistakable visual/gameplay features. Do not guess from generic graphics. "
                "If uncertain, say uncertain. Return concise evidence, not a game-name guess."
            ),
            "images": images,
        }],
        "options": {"temperature": 0},
    }
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
    """Build an explicitly ranked evidence document for the LLM."""
    sections = []
    if media_evidence:
        sections.append("=== PRIMARY EVIDENCE: ACTUAL MEDIA ===\n" + "\n\n---\n\n".join(media_evidence))
    if metadata:
        sections.append("=== SECONDARY EVIDENCE: SOURCE METADATA ===\n" + "\n\n---\n\n".join(metadata))
    if descriptions:
        sections.append(
            "=== LAST-RESORT EVIDENCE: DESCRIPTIONS / CAPTIONS / HASHTAGS ===\n"
            + "\n\n---\n\n".join(descriptions)
        )
    return "\n\n==============================\n\n".join(sections)


async def _identify_from_evidence(evidence: str) -> dict:
    prompt = f"""Identify every video game that is genuinely supported by the supplied evidence.

STRICT EVIDENCE RULES:
1. PRIMARY: actual media evidence (OCR from images/video frames and concrete visual evidence).
2. SECONDARY: source metadata such as the post title, uploader metadata, URL metadata, and audio transcript.
3. LAST: descriptions, captions and hashtags. They are unreliable and MUST NOT by themselves establish a game.
4. A game name appearing only in a description/caption/hashtag is NOT enough to identify it.
5. Prefer a game name that is supported by OCR/visual evidence, even if the description claims another game.
6. Do not blindly trust AI-generated social-media descriptions.
7. Do not invent sequels, remakes, editions, or related games.
8. If evidence conflicts, report the conflict in the reason and prefer the strongest media evidence.
9. Preserve subtitles/entries when the media supports them.
10. Return distinct games only. Maximum {MAX_GAMES}.

For each candidate return:
- name
- confidence 0-100
- reason describing the strongest evidence
- evidence_type: ocr|visual|audio|metadata|description

Return ONLY JSON:
{{"candidates":[{{"name":"Game title","confidence":90,"reason":"...","evidence_type":"ocr"}}]}}

EVIDENCE:
{evidence[:65000]}"""

    payload = {
        "model": OLLAMA_MODEL,
        "stream": False,
        "messages": [
            {"role": "system", "content": "You are a strict game-identification engine. Media evidence outranks captions. Never hallucinate titles."},
            {"role": "user", "content": prompt},
        ],
        "options": {"temperature": 0},
    }
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
    """Reject candidates whose only claimed evidence is a social description."""
    result = []
    for candidate in candidates:
        evidence_type = str(candidate.get("evidence_type", "")).casefold().strip()
        if evidence_type == "description":
            continue
        result.append(candidate)
    return result


async def analyze_game_input(data: dict) -> dict:
    workdir = Path(tempfile.mkdtemp(prefix="game-detector-evidence-"))
    try:
        text = str(data.get("text", "")).strip()
        has_media = bool(data.get("urls") or data.get("video_attachments") or data.get("image_attachments"))

        # Direct game-name input remains literal and is verified through Steam/DB.
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
            # Discord text is useful context, but is intentionally weaker than media.
            metadata.append(f"Discord message text/context:\n{text[:10000]}")

        for url in data.get("urls", [])[:10]:
            try:
                info, downloaded = await _download_url(url, workdir)
                media_files.extend(downloaded)
                if info.get("title"):
                    metadata.append(f"Source title:\n{str(info['title'])[:6000]}")
                if info.get("uploader"):
                    metadata.append(f"Source uploader/account:\n{str(info['uploader'])[:3000]}")
                if info.get("description"):
                    descriptions.append(f"Source description/caption:\n{str(info['description'])[:DESCRIPTION_MAX_CHARS]}")
                for entry in info.get("entries", [])[:MAX_SOCIAL_MEDIA_ITEMS]:
                    if not isinstance(entry, dict):
                        continue
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

        # Every media item is inspected. No first-frame-only shortcut.
        for index, media in enumerate(media_files[:MAX_SOCIAL_MEDIA_ITEMS], 1):
            if _media_type(media) == "image":
                ocr = await _ocr_image(media)
                if ocr.strip():
                    media_evidence.append(f"IMAGE #{index} OCR:\n{ocr[:7000]}")
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

        evidence = _build_evidence(media_evidence, metadata, descriptions)
        if not evidence:
            return {"status": "unknown", "message": "No readable media or source evidence was found."}

        ai = await _identify_from_evidence(evidence)
        candidates = _filter_description_only_candidates(_dedupe_candidates(ai.get("candidates", [])))
        if not candidates:
            return {"status": "unknown", "message": "No game was supported strongly enough by the actual media."}

        games, unresolved = await _verify_and_enrich(candidates)
        return _result(games, unresolved)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
