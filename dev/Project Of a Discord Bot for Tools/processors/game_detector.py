import re
from urllib.parse import urlparse

import discord

from utils.helper import log

URL_PATTERN = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)

SUPPORTED_HOSTS = {
    "instagram.com": "Instagram",
    "www.instagram.com": "Instagram",
    "m.instagram.com": "Instagram",
    "tiktok.com": "TikTok",
    "www.tiktok.com": "TikTok",
    "vm.tiktok.com": "TikTok",
    "vt.tiktok.com": "TikTok",
}


async def process_game_message(message: discord.Message) -> dict | None:
    """Detect supported sources in a Discord message.

    This is intentionally the first stage of the pipeline. It validates and
    classifies the source without attempting to bypass social-platform access
    controls. The next stages can process Discord-uploaded videos directly or
    use authorized platform/API access where available.
    """
    urls = URL_PATTERN.findall(message.content or "")
    supported_url = None
    source_name = None

    for url in urls:
        normalized = url.rstrip(".,!?;:)]}>")
        host = (urlparse(normalized).hostname or "").lower()
        if host in SUPPORTED_HOSTS:
            supported_url = normalized
            source_name = SUPPORTED_HOSTS[host]
            break

    video_attachments = [
        attachment
        for attachment in message.attachments
        if _is_video_attachment(attachment)
    ]

    if not supported_url and not video_attachments:
        return None

    if supported_url:
        log(
            f"Game detector | message={message.id} | "
            f"source={source_name} | url={supported_url}"
        )

        return {
            "status": "queued",
            "source": source_name,
            "url": supported_url,
            "attachment_count": len(video_attachments),
            "message": (
                "Source detected successfully. Social URL ingestion is the next "
                "pipeline stage; uploaded Discord videos can be processed directly "
                "once the media worker is added."
            ),
        }

    log(
        f"Game detector | message={message.id} | "
        f"source=Discord attachment | videos={len(video_attachments)}"
    )

    return {
        "status": "queued",
        "source": "Discord video attachment",
        "attachment_count": len(video_attachments),
        "message": (
            "Video detected and accepted for the game-identification pipeline. "
            "The next worker will extract audio, transcribe it, inspect frames, "
            "and verify game candidates."
        ),
    }


def _is_video_attachment(attachment: discord.Attachment) -> bool:
    content_type = (attachment.content_type or "").lower()
    if content_type.startswith("video/"):
        return True

    filename = attachment.filename.lower()
    return filename.endswith((".mp4", ".mov", ".mkv", ".webm", ".avi"))
