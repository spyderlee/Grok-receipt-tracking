"""Shared test helpers: synthetic receipt images and fake Telegram objects."""
from __future__ import annotations

from pathlib import Path
from typing import List

from PIL import Image, ImageDraw, ImageFilter, ImageFont

FONT_CANDIDATES = ["/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
                   "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"]

RECEIPT_LINES: List[str] = [
    "WELLCOME SUPERMARKET",
    "Shop 12, Tai Koo Shing",
    "Tel: 2345 6789",
    "Date: 26/10/2026  Time: 14:32",
    "",
    "Fresh Milk 2L          32.90",
    "Free Range Eggs 10     41.50",
    "Wholemeal Bread        18.00",
    "Dish Soap              31.00",
    "",
    "SUBTOTAL              123.40",
    "TOTAL HK$             123.40",
    "CASH                  200.00",
    "CHANGE                 76.60",
    "Thank you!",
]


def _font(size: int):
    for p in FONT_CANDIDATES:
        if Path(p).exists():
            return ImageFont.truetype(p, size)
    return ImageFont.load_default()


def make_receipt_image(path: Path, lines: List[str] = RECEIPT_LINES, size: int = 28) -> Path:
    font = _font(size)
    w, lh = 760, int(size * 1.6)
    img = Image.new("RGB", (w, 60 + lh * len(lines)), "white")
    d = ImageDraw.Draw(img)
    for i, line in enumerate(lines):
        d.text((30, 30 + i * lh), line, fill="black", font=font)
    img.save(path)
    return path


def make_garbage_image(path: Path) -> Path:
    """A blurry, low-contrast scrawl Tesseract will not read confidently."""
    font = _font(14)
    img = Image.new("RGB", (300, 160), (150, 150, 150))
    d = ImageDraw.Draw(img)
    for i in range(6):
        d.text((10 + i * 3, 10 + i * 22), "~#@ lll|| 0O0 ;;", fill=(120, 120, 120), font=font)
    img = img.filter(ImageFilter.GaussianBlur(2.2))
    img.save(path)
    return path


def run_coro(coro):
    """Run a coroutine from a sync test on a private loop that is always closed (audit N13).
    asyncio.run() would also work, but it replaces - and drops unclosed - the current event loop
    that pytest-asyncio leaves set between tests, which surfaces as a ResourceWarning later."""
    import asyncio
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()
