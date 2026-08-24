from __future__ import annotations

import asyncio
import re
from pathlib import Path

from utils.helper import log


async def ocr_image(image: Path) -> str:
    """Multi-pass OCR tuned for stylized game covers, screenshots and social posts."""
    try:
        from PIL import Image, ImageEnhance, ImageFilter, ImageOps

        def make_variants():
            with Image.open(image) as source:
                source.verify()
            img = Image.open(image).convert("RGB")
            max_dim = max(img.size)
            scale = 3 if max_dim < 1200 else (2 if max_dim < 2400 else 1)
            if scale > 1:
                img = img.resize((img.width * scale, img.height * scale), Image.Resampling.LANCZOS)

            gray = ImageOps.grayscale(img)
            contrast = ImageEnhance.Contrast(gray).enhance(2.0)
            sharp = contrast.filter(ImageFilter.SHARPEN).filter(ImageFilter.SHARPEN)
            auto = ImageOps.autocontrast(gray, cutoff=1)
            bright = ImageEnhance.Brightness(contrast).enhance(1.15)
            threshold = auto.point(lambda p: 255 if p >= 150 else 0)
            threshold_low = auto.point(lambda p: 255 if p >= 110 else 0)
            inverted = ImageOps.invert(threshold)
            return [img, gray, sharp, auto, bright, threshold, threshold_low, inverted]

        variants = await asyncio.to_thread(make_variants)
        blocks: list[str] = []
        votes: dict[str, int] = {}

        try:
            from rapidocr_onnxruntime import RapidOCR
            engine = RapidOCR()
            for pass_no, variant in enumerate(variants, 1):
                try:
                    result, _ = await asyncio.to_thread(engine, variant)
                    words = []
                    if result:
                        for item in result:
                            try:
                                text = re.sub(r"\s+", " ", str(item[1]).strip())
                                score = float(item[2]) if len(item) > 2 else 0.0
                            except (IndexError, TypeError, ValueError):
                                continue
                            if not text:
                                continue
                            # Keep moderately confident OCR and longer strings even
                            # when the detector is conservative on stylized fonts.
                            if score >= 0.30 or len(text) >= 5:
                                words.append(text)
                    if words:
                        block = "\n".join(dict.fromkeys(words))
                        blocks.append(block)
                        for word in re.findall(r"[A-Za-zÀ-ÿ0-9][A-Za-zÀ-ÿ0-9'&:.-]{2,}", block):
                            key = word.casefold()
                            votes[key] = votes.get(key, 0) + 1
                except Exception as exc:
                    log(f"Game detector OCR pass error | {image.name} | pass={pass_no} | {type(exc).__name__}: {exc}")
        except Exception as exc:
            log(f"Game detector RapidOCR unavailable | {image.name} | {type(exc).__name__}: {exc}")

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
            merged.append("Recurring OCR words: " + " ".join(recurring[:100]))

        # Tesseract is a supplementary pass only when installed. It can recover
        # decorative text that RapidOCR misses, but is never required.
        try:
            import pytesseract
            tess_blocks = []
            for variant in variants[2:5]:
                for psm in (6, 11, 12, 13):
                    text = (await asyncio.to_thread(pytesseract.image_to_string, variant, config=f"--psm {psm}")).strip()
                    if text:
                        tess_blocks.append(text)
            for block in tess_blocks:
                for line in block.splitlines():
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
