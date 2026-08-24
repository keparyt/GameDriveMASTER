from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import aiohttp
from bs4 import BeautifulSoup

try:
    import instaloader
except ImportError:
    instaloader = None

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
    return value if value.startswith("https://") else None


def _looks_like_media(url: str) -> bool:
    lower = url.lower()
    return "cdninstagram.com" in lower or "fbcdn.net" in lower or any(x in lower for x in (".jpg", ".jpeg", ".png", ".webp", ".mp4", ".mov", ".m4v"))


def _walk_json(value: Any, media: list[InstagramMedia], caption_box: list[str], seen: set[str]) -> None:
    if isinstance(value, dict):
        for key, media_type in (("display_url", "image"), ("thumbnail_src", "image"), ("image_url", "image"), ("src", "image"), ("video_url", "video"), ("video_versions", "video")):
            if key not in value:
                continue
            candidates = value[key] if isinstance(value[key], list) else [value[key]]
            for item in candidates:
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


def _post_shortcode(url: str) -> str:
    parts = [x for x in urlparse(url).path.split("/") if x]
    if len(parts) >= 2 and parts[0] in {"p", "reel", "reels", "tv"}:
        return parts[1]
    raise ValueError(f"Unsupported Instagram URL: {url}")


def _instaloader_media(url: str) -> tuple[list[InstagramMedia], str]:
    """Use Instaloader's post/sidecar model. Never use its filename timestamp logic."""
    if instaloader is None:
        return [], ""
    loader = instaloader.Instaloader(download_pictures=False, download_videos=False, save_metadata=False, quiet=True)
    post = instaloader.Post.from_shortcode(loader.context, _post_shortcode(url))
    caption = post.caption or ""
    media: list[InstagramMedia] = []
    seen: set[str] = set()

    def add(image_url: str | None, media_type: str = "image") -> None:
        cleaned = _clean_url(image_url)
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            media.append(InstagramMedia(cleaned, media_type=media_type, index=len(media) + 1))

    if post.typename == "GraphSidecar":
        for child in post.get_sidecar_nodes():
            add(child.video_url if child.is_video else child.display_url, "video" if child.is_video else "image")
    else:
        add(post.video_url if post.is_video else post.url, "video" if post.is_video else "image")

    return media, caption


async def _fetch_html(session: aiohttp.ClientSession, url: str) -> str:
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/151 Safari/537.36", "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8", "Accept-Language": "en-US,en;q=0.9"}
    async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=25), allow_redirects=True) as response:
        response.raise_for_status()
        return await response.text(errors="replace")


async def _download_media(session: aiohttp.ClientSession, item: InstagramMedia, target: Path) -> Path:
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/151 Safari/537.36", "Referer": "https://www.instagram.com/"}
    async with session.get(item.url, headers=headers, timeout=aiohttp.ClientTimeout(total=120), allow_redirects=True) as response:
        response.raise_for_status()
        data = await response.read()
        content_type = (response.headers.get("Content-Type") or "").lower()
        # Instagram occasionally returns a challenge/error page with HTTP 200.
        if not data or "text/html" in content_type or "application/json" in content_type or data.lstrip().startswith((b"<!DOCTYPE", b"<html", b"{")):
            raise RuntimeError(f"Instagram CDN returned non-media response ({content_type or 'unknown content-type'})")
        if item.media_type == "image" and not (data.startswith(b"\xff\xd8\xff") or data.startswith(b"\x89PNG") or data.startswith(b"RIFF")):
            raise RuntimeError("Instagram CDN response is not a recognized image")
        if item.media_type == "video" and not (b"ftyp" in data[:64]):
            raise RuntimeError("Instagram CDN response is not a recognized MP4")
        target.write_bytes(data)
    return target


async def scrape_post(url: str, workdir: Path | None = None):
    """Scrape Instagram. Instaloader is primary; HTML JSON is fallback."""
    media: list[InstagramMedia] = []
    caption = ""
    seen: set[str] = set()

    try:
        media, caption = _instaloader_media(url)
    except Exception as exc:
        # Instaloader can fail on public posts because Instagram changes its
        # private endpoints. Keep the browser-like HTML fallback available.
        media = []
        caption = ""

    connector = aiohttp.TCPConnector(ssl=False)
    async with aiohttp.ClientSession(connector=connector) as session:
        if not media:
            html = await _fetch_html(session, url)
            soup = BeautifulSoup(html, "html.parser")
            for script in soup.find_all("script"):
                raw = script.string or script.get_text() or ""
                if not raw.strip():
                    continue
                try:
                    _walk_json(json.loads(raw), media, [caption] if caption else [], seen)
                except Exception:
                    pass
                for match in re.findall(r'https?:\\?/\\?/[^"\'\s<>]+', raw):
                    cleaned = _clean_url(match)
                    if cleaned and _looks_like_media(cleaned) and cleaned not in seen:
                        seen.add(cleaned)
                        media.append(InstagramMedia(cleaned, index=len(media) + 1))
            for selector in ({"property": "og:image"}, {"name": "twitter:image"}):
                tag = soup.find("meta", attrs=selector)
                if tag:
                    cleaned = _clean_url(tag.get("content"))
                    if cleaned and cleaned not in seen:
                        seen.add(cleaned)
                        media.insert(0, InstagramMedia(cleaned, index=1))

        if not media:
            raise RuntimeError("Instagram scraper found no public media. The post may require authentication or Instagram may have blocked the request.")

        for i, item in enumerate(media, 1):
            item.index = i

        if workdir is None:
            return InstagramPost(url=url, caption=caption, media=media)

        workdir.mkdir(parents=True, exist_ok=True)
        downloaded: list[Path] = []
        errors: list[str] = []
        for item in media[:20]:
            suffix = ".mp4" if item.media_type == "video" else ".jpg"
            target = workdir / f"instagram_{item.index:03d}{suffix}"
            try:
                await _download_media(session, item, target)
                downloaded.append(target)
            except Exception as exc:
                errors.append(f"item {item.index}: {type(exc).__name__}: {exc}")

        if not downloaded:
            raise RuntimeError("Instagram media was discovered but none could be downloaded: " + "; ".join(errors[-5:]))

        metadata = {"title": f"Instagram post {_post_shortcode(url)}", "description": caption, "uploader": None, "instagram_url": url, "media_count": len(downloaded), "entries": [{"url": x.url, "media_type": x.media_type, "index": x.index} for x in media]}
        return metadata, downloaded


async def scrape_media_urls(url: str) -> list[str]:
    post = await scrape_post(url)
    return [item.url for item in post.media or []]
