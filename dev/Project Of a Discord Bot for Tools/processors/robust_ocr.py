from __future__ import annotations

import asyncio
import re
from pathlib import Path

from config import (
    OCR_PASSES,
    TESSERACT_ENABLED,
    TESSERACT_BINARY,
    OCR_SCALE_SMALL_IMAGES,
    OCR_THRESHOLD,
    OCR_MIN_SCORE,
)
from utils.helper import log

_ENGINE = None
_ENGINE_LOCK = asyncio.Lock()


async def _rapid_engine():
    global _ENGINE
    if _ENGINE is not None:
        return _ENGINE
    async with _ENGINE_LOCK:
        if _ENGINE is None:
            from rapidocr_onnxruntime import RapidOCR
            _ENGINE = await asyncio.to_thread(RapidOCR)
    return _ENGINE


def _make_variants(image: Path):
    from PIL import Image, ImageEnhance, ImageFilter, ImageOps

    with Image.open(image) as source:
        source.verify()
    with Image.open(image) as source:
        img = source.convert("RGB")

    max_dim = max(img.size)
    scale = OCR_SCALE_SMALL_IMAGES if max_dim < 1400 else 1
    if scale > 1:
        img = img.resize((img.width * scale, img.height * scale), Image.Resampling.LANCZOS)

    gray = ImageOps.grayscale(img)
    contrast = ImageEnhance.Contrast(gray).enhance(2.0)
    sharp = contrast.filter(ImageFilter.SHARPEN)
    auto = ImageOps.autocontrast(gray, cutoff=1)
    threshold = auto.point(lambda p: 255 if p >= OCR_THRESHOLD else 0)
    variants = [img, sharp, auto, threshold]
    return variants[:max(1, OCR_PASSES)]


async def ocr_image(image: Path) -> str:
    """Fast cached RapidOCR with configurable preprocessing passes.

    The default production profile uses only a couple of high-value passes. This
    avoids the previous 4x OCR cost while still handling stylized game artwork.
    """
    try:
        variants = await asyncio.to_thread(_make_variants, image)
        try:
            engine = await _rapid_engine()
        except Exception as exc:
            log(f"Game detector RapidOCR unavailable | {image.name} | {type(exc).__name__}: {exc}")
            return ""

        def run_variant(variant):
            result, _ = engine(variant)
            words = []
            if result:
                for item in result:
                    try:
                        text = re.sub(r"\s+", " ", str(item[1]).strip())
                        score = float(item[2]) if len(item) > 2 else 0.0
                    except (IndexError, TypeError, ValueError):
                        continue
                    if text and (score >= OCR_MIN_SCORE or len(text) >= 5):
                        words.append(text)
            return words

        blocks: list[str] = []
        votes: dict[str, int] = {}
        for pass_no, variant in enumerate(variants, 1):
            try:
                words = await asyncio.to_thread(run_variant, variant)
                if words:
                    block = "\n".join(dict.fromkeys(words))
                    blocks.append(block)
                    for word in re.findall(r"[A-Za-zÀ-ÿ0-9][A-Za-zÀ-ÿ0-9'&:.-]{2,}", block):
                        key = word.casefold()
                        votes[key] = votes.get(key, 0) + 1
            except Exception as exc:
                log(f"Game detector OCR pass error | {image.name} | pass={pass_no} | {type(exc).__name__}: {exc}")

        merged: list[str] = []
        seen: set[str] = set()
        for block in blocks:
            for line in block.splitlines():
                line = re.sub(r"\s+", " ", line).strip()
                key = re.sub(r"[^a-z0-9]+", "", line.casefold())
                if key and key not in seen:
                    seen.add(key)
                    merged.append(line)

        recurring = [word for word, count in sorted(votes.items(), key=lambda x: (-x[1], x[0])) if count >= 2 and len(word) >= 3]
        if recurring:
            merged.append("Recurring OCR words: " + " ".join(recurring[:80]))

        if TESSERACT_ENABLED:
            try:
                import pytesseract
                for variant in variants[1:3]:
                    text = (await asyncio.to_thread(
                        pytesseract.image_to_string,
                        variant,
                        config="--psm 11",
                    )).strip()
                    for line in text.splitlines():
                        line = re.sub(r"\s+", " ", line).strip()
                        key = re.sub(r"[^a-z0-9]+", "", line.casefold())
                        if key and key not in seen:
                            seen.add(key)
                            merged.append(line)
            except Exception as exc:
                if type(exc).__name__ != "TesseractNotFoundError":
                    log(f"Game detector optional Tesseract error | {image.name} | {type(exc).__name__}: {exc}")

        return "\n".join(merged)
    except Exception as exc:
        log(f"Game detector OCR error | {image.name} | {type(exc).__name__}: {exc}")
        return ""
