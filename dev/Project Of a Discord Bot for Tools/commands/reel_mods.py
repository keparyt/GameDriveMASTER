import asyncio
import base64
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands


OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434")
OLLAMA_VISION_MODEL = os.getenv("OLLAMA_VISION_MODEL", "qwen2.5vl:3b")
OLLAMA_TEXT_MODEL = os.getenv("OLLAMA_TEXT_MODEL", "qwen2.5:3b")
CURSEFORGE_API_KEY = os.getenv("CURSEFORGE_API_KEY", "")

# Keep this intentionally small for GTX 1660 SUPER / 6 GB VRAM.
FRAME_COUNT = int(os.getenv("REEL_MOD_FRAME_COUNT", "4"))
WHISPER_MODEL = os.getenv("REEL_WHISPER_MODEL", "tiny")
MAX_CANDIDATES = 8


class ReelModAnalyzer:
    async def analyze(self, url: str) -> list[str]:
        with tempfile.TemporaryDirectory(prefix="reel-mod-") as temp:
            workdir = Path(temp)
            metadata = await self._download_reel(url, workdir)
            video = next(workdir.glob("video.*"), None)
            if video is None:
                raise RuntimeError("Instagram reel video could not be downloaded.")

            audio_text = await self._transcribe(video, workdir)
            frames = await self._extract_frames(video, workdir)
            visual_text = await self._analyze_frames(frames)

            candidates = await self._extract_candidates(
                metadata.get("description", ""),
                audio_text,
                visual_text,
            )

            return await self._verify_candidates(candidates)

    async def _download_reel(self, url: str, workdir: Path) -> dict[str, Any]:
        output = workdir / "video.%(ext)s"
        command = [
            "yt-dlp",
            "--no-playlist",
            "--no-warnings",
            "--quiet",
            "--print-json",
            "-f",
            "bv*+ba/b",
            "--merge-output-format",
            "mp4",
            "-o",
            str(output),
            url,
        ]

        def run() -> tuple[str, str]:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=180,
                check=False,
            )
            return result.stdout, result.stderr

        stdout, stderr = await asyncio.to_thread(run)
        if not stdout.strip():
            raise RuntimeError(stderr.strip() or "yt-dlp could not download this reel.")

        # yt-dlp may print progress/log lines around the JSON; use the last JSON line.
        info = None
        for line in reversed(stdout.splitlines()):
            try:
                info = json.loads(line)
                break
            except json.JSONDecodeError:
                continue

        return info or {}

    async def _transcribe(self, video: Path, workdir: Path) -> str:
        audio = workdir / "audio.wav"
        command = [
            "ffmpeg",
            "-y",
            "-i",
            str(video),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            str(audio),
        ]

        def extract() -> None:
            subprocess.run(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=120,
                check=True,
            )

        try:
            await asyncio.to_thread(extract)
        except Exception:
            return ""

        try:
            from faster_whisper import WhisperModel
        except ImportError:
            return ""

        def transcribe() -> str:
            model = WhisperModel(
                WHISPER_MODEL,
                device="cuda",
                compute_type="float16",
            )
            segments, _ = model.transcribe(str(audio), vad_filter=True)
            return " ".join(segment.text.strip() for segment in segments if segment.text.strip())

        try:
            return await asyncio.to_thread(transcribe)
        except Exception:
            # CUDA/driver incompatibility should not prevent visual/caption analysis.
            def cpu_transcribe() -> str:
                model = WhisperModel(
                    WHISPER_MODEL,
                    device="cpu",
                    compute_type="int8",
                )
                segments, _ = model.transcribe(str(audio), vad_filter=True)
                return " ".join(segment.text.strip() for segment in segments if segment.text.strip())

            try:
                return await asyncio.to_thread(cpu_transcribe)
            except Exception:
                return ""

    async def _extract_frames(self, video: Path, workdir: Path) -> list[Path]:
        pattern = workdir / "frame-%02d.jpg"
        command = [
            "ffmpeg",
            "-y",
            "-i",
            str(video),
            "-vf",
            f"fps={FRAME_COUNT}/max(1,\u003cDURATION\u003e),scale=960:-1".replace("max(1,<DURATION>)", "1"),
            "-frames:v",
            str(FRAME_COUNT),
            str(pattern),
        ]

        # Use evenly distributed timestamps instead of relying on a duration expression.
        duration_cmd = [
            "ffprobe",
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(video),
        ]
        try:
            duration = float((await asyncio.to_thread(
                lambda: subprocess.check_output(duration_cmd, text=True, timeout=20)
            )).strip())
        except Exception:
            duration = 30.0

        paths: list[Path] = []
        for index in range(FRAME_COUNT):
            timestamp = duration * (index + 0.5) / FRAME_COUNT
            frame = workdir / f"frame-{index:02d}.jpg"
            frame_cmd = [
                "ffmpeg", "-y", "-ss", f"{timestamp:.2f}", "-i", str(video),
                "-frames:v", "1", "-vf", "scale=960:-1", str(frame),
            ]
            try:
                await asyncio.to_thread(
                    lambda cmd=frame_cmd: subprocess.run(
                        cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        timeout=30, check=True,
                    )
                )
                paths.append(frame)
            except Exception:
                continue
        return paths

    async def _analyze_frames(self, frames: list[Path]) -> str:
        if not frames:
            return ""

        descriptions: list[str] = []
        timeout = aiohttp.ClientTimeout(total=90)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for frame in frames:
                image = base64.b64encode(frame.read_bytes()).decode("ascii")
                prompt = (
                    "Analyze this Minecraft screenshot from a short video. "
                    "Identify any visible Minecraft mod names, modded item/block/entity names, "
                    "GUI text, subtitles, or unmistakable mod-specific features. "
                    "Do not guess a mod from generic Minecraft content. Return concise clues only."
                )
                payload = {
                    "model": OLLAMA_VISION_MODEL,
                    "prompt": prompt,
                    "images": [image],
                    "stream": False,
                }
                try:
                    async with session.post(f"{OLLAMA_URL.rstrip('/')}/api/generate", json=payload) as response:
                        if response.status == 200:
                            data = await response.json()
                            text = str(data.get("response", "")).strip()
                            if text:
                                descriptions.append(text)
                except (aiohttp.ClientError, asyncio.TimeoutError):
                    continue

        return "\n".join(descriptions)

    async def _extract_candidates(self, description: str, audio: str, visual: str) -> list[str]:
        prompt = f"""
You identify Minecraft mods from an Instagram Reel.

Use ONLY the evidence below. Combine caption, spoken audio and visual clues.
Return ONLY valid JSON in this exact form:
{{"mods":["Mod Name 1","Mod Name 2"]}}

Rules:
- Return actual Minecraft mod/project names, not Minecraft features.
- Do not return shaders, resource packs, maps, datapacks, players, YouTubers or commands.
- Do not invent names from generic visuals.
- If uncertain, omit the name.
- Deduplicate names.
- Maximum {MAX_CANDIDATES} names.

CAPTION:
{description[:6000]}

AUDIO TRANSCRIPT:
{audio[:8000]}

VISUAL CLUES:
{visual[:10000]}
"""

        payload = {
            "model": OLLAMA_TEXT_MODEL,
            "prompt": prompt,
            "stream": False,
            "format": "json",
            "options": {"temperature": 0},
        }

        timeout = aiohttp.ClientTimeout(total=90)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(f"{OLLAMA_URL.rstrip('/')}/api/generate", json=payload) as response:
                if response.status != 200:
                    raise RuntimeError("Ollama is unavailable. Start Ollama and load the configured models.")
                data = await response.json()

        raw = str(data.get("response", "{}"))
        try:
            parsed = json.loads(raw)
            mods = parsed.get("mods", [])
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", raw, re.DOTALL)
            if not match:
                return []
            try:
                mods = json.loads(match.group(0)).get("mods", [])
            except json.JSONDecodeError:
                return []

        return [str(name).strip() for name in mods if str(name).strip()][:MAX_CANDIDATES]

    async def _verify_candidates(self, candidates: list[str]) -> list[str]:
        verified: list[str] = []
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for candidate in candidates:
                found = await self._search_modrinth(session, candidate)
                if not found and CURSEFORGE_API_KEY:
                    found = await self._search_curseforge(session, candidate)
                if found:
                    verified.append(found)

        return list(dict.fromkeys(verified))

    async def _search_modrinth(self, session: aiohttp.ClientSession, name: str) -> str | None:
        params = {"query": name, "limit": "5"}
        try:
            async with session.get("https://api.modrinth.com/v2/search", params=params) as response:
                if response.status != 200:
                    return None
                data = await response.json()
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return None

        normalized = re.sub(r"[^a-z0-9]", "", name.lower())
        for hit in data.get("hits", []):
            title = str(hit.get("title", "")).strip()
            if re.sub(r"[^a-z0-9]", "", title.lower()) == normalized:
                return title
        return None

    async def _search_curseforge(self, session: aiohttp.ClientSession, name: str) -> str | None:
        headers = {"x-api-key": CURSEFORGE_API_KEY}
        params = {
            "gameId": "432",
            "searchFilter": name,
            "classId": "6",
            "pageSize": "5",
        }
        try:
            async with session.get(
                "https://api.curseforge.com/v1/mods/search",
                params=params,
                headers=headers,
            ) as response:
                if response.status != 200:
                    return None
                data = await response.json()
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return None

        normalized = re.sub(r"[^a-z0-9]", "", name.lower())
        for hit in data.get("data", []):
            title = str(hit.get("name", "")).strip()
            if re.sub(r"[^a-z0-9]", "", title.lower()) == normalized:
                return title
        return None


class ReelMods(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.analyzer = ReelModAnalyzer()

    @app_commands.command(
        name="reel-mods",
        description="Find the Minecraft mod names shown or mentioned in an Instagram Reel.",
    )
    @app_commands.describe(url="Instagram Reel URL")
    async def reel_mods(self, interaction: discord.Interaction, url: str):
        if "instagram.com" not in url.lower() or "/reel/" not in url.lower():
            await interaction.response.send_message(
                "❌ Please provide an Instagram Reel URL.", ephemeral=True
            )
            return

        await interaction.response.defer(thinking=True)
        try:
            mods = await self.analyzer.analyze(url)
        except Exception as exc:
            await interaction.followup.send(f"❌ Reel analysis failed: `{exc}`")
            return

        if not mods:
            await interaction.followup.send("❌ No Minecraft mod names could be confidently identified.")
            return

        await interaction.followup.send("\n".join(f"**{name}**" for name in mods))


async def setup(bot: commands.Bot):
    await bot.add_cog(ReelMods(bot))
