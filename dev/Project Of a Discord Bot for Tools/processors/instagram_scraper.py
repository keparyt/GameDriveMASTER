"""Instagram media scraper used by the game-identification pipeline.

Instagram posts are handled separately from yt-dlp because a carousel can contain
image-only children. yt-dlp's video extractor legitimately reports "No video
formats found" for those children, but that must never prevent us from getting
the actual images for OCR.

The scraper uses Instaloader's post/sidecar model first, then a lightweight public
HTML/JSON fallback. It returns every carousel child as media and never requires a
video format for an image child.
"""

import asyncio
import json
import re
from pathlib import Path
from urllib.parse import urlparse

import aiohttp
from bs4 import BeautifulSoup

from utils.helper import log

MAX_ITEMS = 20
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0 Safari/537.36"


def is_instagram_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host in {"instagram.com", "www.instagram.com", "m.instagram.com"} and re.search(r"/(p|reel|reels|tv)/", url, re.I) is not None


def shortcode_from_url(url: str) -> str | None:
    match = re.search(r"/(?:p|reel|reels|tv)/([A-Za-z0-9_-]+)", url)
    return match.group(1) if match else None


def _ext_from_content_type(content_type: str, fallback: str = ".jpg") -> str:
    content_type = content_type.lower().split(";", 1)[0].strip()
    return {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "video/mp4": ".mp4",
        "video/quicktime": ".mov",
    }.get(content_type, fallback)


async def _download(session: aiohttp.ClientSession, url: str, target: Path) -> bool:
    try:
        async with session.get(url, allow_redirects=True, timeout=aiohttp.ClientTimeout(total=60)) as response:
            response.raise_for_status()
            content_type = response.headers.get("Content-Type", "")
            data = await response.read()
            if not data:
                return False
            suffix = target.suffix or _ext_from_content_type(content_type)
            if target.suffix != suffix:
                target = target.with_suffix(suffix)
            target.write_bytes(data)
            return True
    except Exception as exc:
        log(f"Game detector Instagram download error | {type(exc).__name__}: {exc}")
        return False


def _instaloader_entries(url: str) -> dict | None:
    """Use Instaloader's structured post model, including sidecar children."""
    try:
        import instaloader

        shortcode = shortcode_from_url(url)
        if not shortcode:
            return None
        context = instaloader.Instaloader(
            download_pictures=False,
            download_videos=False,
            download_video_thumbnails=False,
            save_metadata=False,
            compress_json=False,
            quiet=True,
        ).context
        post = instaloader.Post.from_shortcode(context, shortcode)
        entries = []
        if post.typename == "GraphSidecar":
            nodes = list(post.get_sidecar_nodes())[:MAX_ITEMS]
        else:
            nodes = [post]
        for index, node in enumerate(nodes, 1):
            video = getattr(node, "video_url", None) if getattr(node, "is_video", False) else None
            image = getattr(node, "display_url", None)
            entries.append({
                "index": index,
                "is_video": bool(video),
                "video_url": str(video) if video else None,
                "image_url": str(image) if image else None,
                "thumbnail": str(image) if image else None,
                "title": None,
                "description": None,
            })
        return {
            "title": f"Instagram @{post.owner_username}",
            "description": post.caption or "",
            "uploader": post.owner_username,
            "shortcode": shortcode,
            "entries": entries,
            "source": "instaloader",
        }
    except Exception as exc:
        log(f"Game detector Instagram structured scraper fallback | {type(exc).__name__}: {exc}")
        return None


def _walk_json(value, urls: list[str]) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"display_url", "image_versions2", "video_url", "thumbnail_src"}:
                if isinstance(item, str) and item.startswith("http"):
                    urls.append(item)
                elif isinstance(item, dict):
                    _walk_json(item, urls)
                elif isinstance(item, list):
                    _walk_json(item, urls)
            else:
                _walk_json(item, urls)
    elif isinstance(value, list):
        for item in value:
            _walk_json(item, urls)
    elif isinstance(value, str) and ("cdninstagram.com" in value or "fbcdn.net" in value):
        urls.append(value.replace("\\u0026", "&").replace("\\/", "/"))


async def _html_fallback(url: str) -> dict | None:
    """Extract public Instagram embedded JSON when structured scraping is unavailable."""
    try:
        headers = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.get(url, allow_redirects=True, timeout=aiohttp.ClientTimeout(total=45)) as response:
                response.raise_for_status()
                html = await response.text()
        soup = BeautifulSoup(html, "html.parser")
        description = ""
        og = soup.find("meta", attrs={"property": "og:description"})
        if og:
            description = og.get("content", "")
        urls: list[str] = []
        for script in soup.find_all("script"):
            raw = script.string or script.get_text()
            if not raw:
                continue
            # Try JSON script blocks first.
            try:
                obj = json.loads(raw)
                _walk_json(obj, urls)
                continue
            except Exception:
                pass
            for match in re.findall(r"https?:\\?/\\?/[^"]+", raw):
                if "cdninstagram.com" in match or "fbcdn.net" in match:
                    urls.append(match.replace("\\/", "/").replace("\\u0026", "&"))
        # og:image is useful even when Instagram does not expose the complete
        # carousel in the public HTML.
        og_image = soup.find("meta", attrs={"property": "og:image"})
        if og_image and og_image.get("content"):
            urls.append(og_image["content"])
        deduped = []
        seen = set()
        for value in urls:
            value = value.strip().strip('"').replace("&amp;", "&")
            if value and value not in seen:
                seen.add(value)
                deduped.append(value)
        entries = []
        for index, value in enumerate(deduped[:MAX_ITEMS], 1):
            is_video = ".mp4" in value.lower() or "video" in value.lower()
            entries.append({"index": index, "is_video": is_video, "video_url": value if is_video else None, "image_url": None if is_video else value, "thumbnail": None if is_video else value})
        return {"title": "Instagram post", "description": description, "uploader": None, "entries": entries, "source": "instagram_html"} if entries else None
    except Exception as exc:
        log(f"Game detector Instagram HTML scraper error | {type(exc).__name__}: {exc}")
        return None


async def scrape_post(url: str, workdir: Path) -> tuple[dict, list[Path]]:
    if not is_instagram_url(url):
        raise ValueError("Not an Instagram post/reel URL")

    info = await asyncio.to_thread(_instaloader_entries, url)
    if not info:
        info = await _html_fallback(url)
    if not info:
        raise RuntimeError("Instagram could not expose public media for this post. It may require login or be unavailable.")

    entries = info.get("entries") or []
    media: list[Path] = []
    headers = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}
    async with aiohttp.ClientSession(headers=headers) as session:
        for index, entry in enumerate(entries[:MAX_ITEMS], 1):
            media_url = entry.get("video_url") or entry.get("image_url") or entry.get("thumbnail")
            if not media_url:
                continue
            suffix = ".mp4" if entry.get("video_url") else ".jpg"
            target = workdir / f"instagram_{index:03d}{suffix}"
            if await _download(session, media_url, target):
                # The downloader may adjust extension from Content-Type.
                actual = target if target.exists() else next(iter(workdir.glob(target.stem + ".*")), None)
                if actual and actual.is_file():
                    media.append(actual)
                    log(f"Game detector Instagram scraper | item={index} | type={'video' if entry.get('video_url') else 'image'}")
    if not media:
        raise RuntimeError("Instagram post was found, but no public media could be downloaded.")
    info["entries"] = entries
    return info, media
