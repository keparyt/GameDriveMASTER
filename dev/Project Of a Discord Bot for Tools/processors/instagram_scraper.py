from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import aiohttp
from bs4 import BeautifulSoup
import instaloader

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


def _shortcode(url: str) -> str | None:
    path = urlparse(url).path.strip("/").split("/")
    if len(path) >= 2 and path[0].lower() in {"p", "reel", "reels", "tv"}:
        return path[1]
    return None


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
    return "cdninstagram.com" in lower or "fbcdn.net" in lower or any(ext in lower for ext in IMAGE_EXTENSIONS + VIDEO_EXTENSIONS)


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


async def _fetch_html(session: aiohttp.ClientSession, url: str) -> str:
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/151 Safari/537.36", "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8", "Accept-Language": "en-US,en;q=0.9"}
    async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=25), allow_redirects=True) as response:
        response.raise_for_status()
        return await response.text(errors="replace")


async def _download_media(session: aiohttp.ClientSession, item: InstagramMedia, target: Path) -> Path:
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/151 Safari/537.36", "Referer": "https://www.instagram.com/"}
    async with session.get(item.url, headers=headers, timeout=aiohttp.ClientTimeout(total=120)) as response:
        response.raise_for_status()
        content_type = (response.headers.get("Content-Type") or "").lower()
        if "text/html" in content_type or "application/json" in content_type:
            raise RuntimeError(f"Instagram CDN returned {content_type} instead of media")
        data = await response.read()
        if not data:
            raise RuntimeError("Instagram CDN returned an empty response")
        # Validate the actual bytes, not merely Content-Type.
        if item.media_type == "image":
            from PIL import Image
            import io
            try:
                with Image.open(io.BytesIO(data)) as image:
                    image.verify()
            except Exception as exc:
                raise RuntimeError(f"Instagram CDN response is not a valid image ({type(exc).__name__})") from exc
        target.write_bytes(data)
    return target


def _instaloader_post(url: str):
    shortcode = _shortcode(url)
    if not shortcode:
        raise RuntimeError("Could not determine Instagram shortcode")
    loader = instaloader.Instaloader(
        download_pictures=False,
        download_videos=False,
        download_video_thumbnails=False,
        download_geotags=False,
        save_metadata=False,
        compress_json=False,
        quiet=True,
    )
    post = instaloader.Post.from_shortcode(loader.context, shortcode)
    return loader, post


def _instaloader_media(url: str) -> tuple[str, list[InstagramMedia], str]:
    loader, post = _instaloader_post(url)
    media: list[InstagramMedia] = []
    seen: set[str] = set()
    if post.typename == "GraphSidecar":
        nodes = list(post.get_sidecar_nodes())
    else:
        nodes = [post]
    for node in nodes:
        media_url = node.video_url if getattr(node, "is_video", False) else node.display_url
        cleaned = _clean_url(media_url)
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        media.append(InstagramMedia(cleaned, "video" if getattr(node, "is_video", False) else "image", index=len(media) + 1))
    caption = post.caption or ""
    return post.owner_username or "", media, caption


async def scrape_post(url: str, workdir: Path | None = None):
    """Scrape Instagram using Instaloader first, with HTML extraction as fallback.

    Instaloader is important for carousels because it understands Instagram's
    GraphSidecar structure and gives us the actual display_url/video_url for
    each child instead of treating image children as videos.
    """
    author = None
    caption = ""
    media: list[InstagramMedia] = []
    errors: list[str] = []

    # Primary: Instagram-aware structured extraction.
    try:
        author, media, caption = await asyncio.to_thread(_instaloader_media, url)
    except Exception as exc:
        errors.append(f"Instaloader: {type(exc).__name__}: {exc}")

    # Fallback: public page embedded data. This is only used when the
    # structured Instagram request is unavailable/blocked.
    if not media:
        connector = aiohttp.TCPConnector(ssl=False)
        async with aiohttp.ClientSession(connector=connector) as session:
            try:
                html = await _fetch_html(session, url)
            except Exception as exc:
                raise RuntimeError("Instagram extraction failed. " + " | ".join(errors + [f"HTML: {type(exc).__name__}: {exc}"])) from exc
            soup = BeautifulSoup(html, "html.parser")
            seen: set[str] = set()
            caption_box: list[str] = []
            for script in soup.find_all("script"):
                raw = script.string or script.get_text() or ""
                if not raw.strip():
                    continue
                try:
                    _walk_json(json.loads(raw), media, caption_box, seen)
                except Exception:
                    pass
                for match in re.findall(r'https?:\\?/\\?/[^"\'\s<>]+', raw):
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
            if not caption and caption_box:
                caption = caption_box[0]

    if not media:
        raise RuntimeError("Instagram scraper found no public media. " + " | ".join(errors))

    # Normalize indexes after all extraction.
    deduped: list[InstagramMedia] = []
    seen_final: set[str] = set()
    for item in media:
        if item.url in seen_final:
            continue
        seen_final.add(item.url)
        item.index = len(deduped) + 1
        deduped.append(item)
    media = deduped[:20]

    if workdir is None:
        return InstagramPost(url=url, caption=caption, media=media, author=author)

    workdir.mkdir(parents=True, exist_ok=True)
    downloaded: list[Path] = []
    download_errors: list[str] = []

    # Use Instaloader's own downloader first for media it extracted. This
    # keeps the Instagram-specific session/request behavior instead of using
    # a generic CDN request that can sometimes return an HTML challenge page.
    try:
        loader, _post = await asyncio.to_thread(_instaloader_post, url)
    except Exception:
        loader = None

    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False)) as session:
        for item in media:
            suffix = ".mp4" if item.media_type == "video" else ".jpg"
            target = workdir / f"instagram_{item.index:03d}{suffix}"
            try:
                # Instaloader's download_pic uses its authenticated/session-aware
                # context. Run it in a thread because it is synchronous.
                if loader is not None:
                    def download_with_instaloader():
                        return loader.download_pic(str(target.with_suffix("")), item.url, None)
                    ok = await asyncio.to_thread(download_with_instaloader)
                    candidates = sorted(target.parent.glob(target.stem + ".*"), key=lambda p: p.stat().st_mtime, reverse=True)
                    actual = next((p for p in candidates if p.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp", ".mp4"}), None)
                    if ok and actual and actual.exists() and actual.stat().st_size > 0:
                        if actual != target:
                            actual.replace(target)
                        downloaded.append(target)
                        continue
                await _download_media(session, item, target)
                downloaded.append(target)
            except Exception as exc:
                download_errors.append(f"item {item.index}: {type(exc).__name__}: {exc}")

    if not downloaded:
        raise RuntimeError("Instagram media was discovered but none could be downloaded: " + "; ".join(download_errors[-5:]))

    metadata = {
        "title": f"Instagram post {urlparse(url).path.rstrip('/').split('/')[-1]}",
        "description": caption,
        "uploader": author,
        "instagram_url": url,
        "media_count": len(downloaded),
        "entries": [{"url": item.url, "media_type": item.media_type, "index": item.index} for item in media],
    }
    return metadata, downloaded


async def scrape_media_urls(url: str) -> list[str]:
    post = await scrape_post(url)
    return [item.url for item in post.media or []]
