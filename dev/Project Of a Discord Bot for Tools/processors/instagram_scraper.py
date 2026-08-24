from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import aiohttp
from bs4 import BeautifulSoup


@dataclass
class InstagramMedia:
    url: str
    media_type: str = "image"
    thumbnail_url: str | None = None
    index: int = 0


@dataclass
class InstagramPost:
    url: str
    caption: str = ""
    media: list[InstagramMedia] | None = None
    author: str | None = None


INSTAGRAM_HOSTS = {"instagram.com", "www.instagram.com", "m.instagram.com"}
IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp")
VIDEO_EXTENSIONS = (".mp4", ".mov", ".m4v")


def is_instagram_url(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").lower()
        return host in INSTAGRAM_HOSTS and bool(re.match(r"^/(p|reel|reels|tv)/[^/]+", urlparse(url).path))
    except Exception:
        return False


def _clean_url(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip().strip('"').strip("'")
    value = value.replace("\\/", "/").replace("\\u0026", "&").replace("&amp;", "&")
    if value.startswith("//"):
        value = "https:" + value
    if value.startswith("http://"):
        value = "https://" + value[7:]
    if not value.startswith("https://"):
        return None
    return value


def _looks_like_media(url: str) -> bool:
    lower = url.lower()
    return "cdninstagram.com" in lower or "fbcdn.net" in lower or any(ext in lower for ext in IMAGE_EXTENSIONS + VIDEO_EXTENSIONS)


def _walk_json(value: Any, media: list[InstagramMedia], caption_box: list[str], seen: set[str]) -> None:
    if isinstance(value, dict):
        candidates: list[tuple[Any, str]] = []
        for key in ("display_url", "thumbnail_src", "image_url", "src"):
            if key in value:
                candidates.append((value[key], "image"))
        for key in ("video_url", "video_versions"):
            if key in value:
                candidates.append((value[key], "video"))
        for candidate, media_type in candidates:
            values = candidate if isinstance(candidate, list) else [candidate]
            for item in values:
                if isinstance(item, dict):
                    item = item.get("url") or item.get("src")
                cleaned = _clean_url(item)
                if cleaned and _looks_like_media(cleaned) and cleaned not in seen:
                    seen.add(cleaned)
                    media.append(InstagramMedia(cleaned, media_type=media_type, index=len(media) + 1))
        for key in ("caption", "title", "description"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip() and not caption_box:
                caption_box.append(candidate.strip())
        for child in value.values():
            _walk_json(child, media, caption_box, seen)
    elif isinstance(value, list):
        for child in value:
            _walk_json(child, media, caption_box, seen)


async def _fetch_html(session: aiohttp.ClientSession, url: str) -> str:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/151 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "no-cache",
    }
    async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=25), allow_redirects=True) as response:
        response.raise_for_status()
        return await response.text(errors="replace")


async def _download_media(session: aiohttp.ClientSession, item: InstagramMedia, target: Path) -> Path:
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/151 Safari/537.36", "Referer": "https://www.instagram.com/"}
    async with session.get(item.url, headers=headers, timeout=aiohttp.ClientTimeout(total=120)) as response:
        response.raise_for_status()
        with target.open("wb") as file:
            async for chunk in response.content.iter_chunked(1024 * 1024):
                file.write(chunk)
    return target


async def scrape_post(url: str, workdir: Path | None = None):
    """Scrape public Instagram media and optionally download it for the analyzer.

    With no workdir, returns InstagramPost for callers that only need URLs.
    With workdir, returns (metadata, downloaded_paths), matching the analyzer's
    media downloader contract.
    """
    connector = aiohttp.TCPConnector(ssl=False)
    async with aiohttp.ClientSession(connector=connector) as session:
        html = await _fetch_html(session, url)
        soup = BeautifulSoup(html, "html.parser")
        media: list[InstagramMedia] = []
        seen: set[str] = set()
        caption_box: list[str] = []

        for script in soup.find_all("script"):
            raw = script.string or script.get_text() or ""
            if not raw.strip():
                continue
            try:
                obj = json.loads(raw)
                _walk_json(obj, media, caption_box, seen)
            except Exception:
                pass

            for match in re.findall(r'https?:\\?/\\?/[^"\\\'\s<>]+', raw):
                cleaned = _clean_url(match)
                if cleaned and _looks_like_media(cleaned) and cleaned not in seen:
                    seen.add(cleaned)
                    media.append(InstagramMedia(cleaned, index=len(media) + 1))

        for selector in ({"property": "og:image"}, {"name": "twitter:image"}):
            tag = soup.find("meta", attrs=selector)
            if tag and tag.get("content"):
                cleaned = _clean_url(tag["content"])
                if cleaned and cleaned not in seen:
                    seen.add(cleaned)
                    media.insert(0, InstagramMedia(cleaned, index=1))

        for tag in soup.find_all(["img", "video"]):
            candidate = tag.get("src") or tag.get("data-src") or tag.get("poster")
            cleaned = _clean_url(candidate)
            if cleaned and _looks_like_media(cleaned) and cleaned not in seen:
                seen.add(cleaned)
                media.append(InstagramMedia(cleaned, "video" if tag.name == "video" else "image", index=len(media) + 1))

        deduped: list[InstagramMedia] = []
        seen_final: set[str] = set()
        for item in media:
            if item.url in seen_final:
                continue
            seen_final.add(item.url)
            item.index = len(deduped) + 1
            deduped.append(item)

        if not deduped:
            raise RuntimeError("Instagram scraper found no public media. The post may require authentication or Instagram may have blocked the request.")

        caption = caption_box[0] if caption_box else ""
        post = InstagramPost(url=url, caption=caption, media=deduped)

        if workdir is None:
            return post

        workdir.mkdir(parents=True, exist_ok=True)
        downloaded: list[Path] = []
        errors: list[str] = []
        for item in deduped:
            suffix = ".mp4" if item.media_type == "video" else ".jpg"
            target = workdir / f"instagram_{item.index:03d}{suffix}"
            try:
                await _download_media(session, item, target)
                downloaded.append(target)
            except Exception as exc:
                errors.append(f"item {item.index}: {type(exc).__name__}: {exc}")

        if not downloaded:
            detail = "; ".join(errors[-3:])
            raise RuntimeError(f"Instagram media was discovered but could not be downloaded. {detail}")

        metadata = {
            "title": f"Instagram post {urlparse(url).path.rstrip('/').split('/')[-1]}",
            "description": caption,
            "uploader": post.author,
            "instagram_url": url,
            "media_count": len(downloaded),
            "entries": [
                {"url": item.url, "media_type": item.media_type, "index": item.index}
                for item in deduped
            ],
        }
        return metadata, downloaded[:20]


async def scrape_media_urls(url: str) -> list[str]:
    post = await scrape_post(url)
    return [item.url for item in post.media or []]
