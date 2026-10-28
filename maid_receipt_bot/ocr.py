"""Tesseract-first OCR pipeline with confidence-based escalation to a vision model."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytesseract
from PIL import Image, ImageOps

from .images import register_heif
from .parsing import parse_receipt_text
from .vision import VisionClient

log = logging.getLogger(__name__)
register_heif()

TESS_CONFIG = "--oem 1 --psm 4"


@dataclass
class TesseractResult:
    text: str
    confidence: float  # mean word confidence 0-100
    variant: str
    word_count: int
    words: List[Tuple[str, float]] = field(default_factory=list)  # (word, confidence)


@dataclass
class Extraction:
    amount: Optional[Decimal] = None
    receipt_date: Optional[date] = None
    merchant: Optional[str] = None
    items: Optional[str] = None
    engine: str = "none"                  # tesseract | vision | tesseract_fallback | none
    confidence: Optional[float] = None    # Tesseract mean word confidence
    raw_text: Optional[str] = None
    escalation_reason: Optional[str] = None
    vision_model: Optional[str] = None
    vision_raw: Optional[str] = None
    vision_error: Optional[str] = None

    def fields(self) -> Dict[str, Any]:
        return {"amount": self.amount, "receipt_date": self.receipt_date,
                "merchant": self.merchant, "items": self.items}


# --------------------------------------------------------------- helpers

def average_confidence(data: Dict[str, List[Any]]) -> tuple[float, int]:
    """Mean of Tesseract word confidences, ignoring conf == -1 and empty words."""
    confs: List[float] = []
    for txt, conf in zip(data.get("text", []), data.get("conf", [])):
        try:
            c = float(conf)
        except (TypeError, ValueError):
            continue
        if c < 0 or not str(txt).strip():
            continue
        confs.append(c)
    if not confs:
        return 0.0, 0
    return round(sum(confs) / len(confs), 2), len(confs)


def words_from_data(data: Dict[str, List[Any]]) -> List[Tuple[str, float]]:
    out: List[Tuple[str, float]] = []
    for txt, conf in zip(data.get("text", []), data.get("conf", [])):
        try:
            c = float(conf)
        except (TypeError, ValueError):
            continue
        if c >= 0 and str(txt).strip():
            out.append((str(txt).strip(), c))
    return out


def amount_token_confidence(words: List[Tuple[str, float]], amount: Optional[Decimal]) -> Optional[float]:
    """Tesseract's confidence in the word(s) that spell the chosen total (audit M7): a page can
    average 95% while the one number that matters was read at 40%."""
    if amount is None or not words:
        return None
    wanted = {f"{amount:.2f}", f"{amount:,.2f}"}
    if amount == amount.to_integral_value():
        wanted.add(str(int(amount)))
    confs = []
    for w, c in words:
        norm = "".join(ch for ch in w if ch.isdigit() or ch in ".,")
        if norm.strip(".,") and (norm in wanted or norm.rstrip(".,") in wanted):
            confs.append(c)
    return min(confs) if confs else None


def text_from_data(data: Dict[str, List[Any]]) -> str:
    lines: Dict[tuple, List[str]] = {}
    order: List[tuple] = []
    for i, word in enumerate(data.get("text", [])):
        if not str(word).strip():
            continue
        key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
        if key not in lines:
            lines[key] = []
            order.append(key)
        lines[key].append(str(word).strip())
    return "\n".join(" ".join(lines[k]) for k in order)


def otsu_threshold(gray: Image.Image) -> int:
    hist = gray.histogram()[:256]
    total = sum(hist)
    sum_all = sum(i * h for i, h in enumerate(hist))
    sum_b = w_b = 0
    best_t, best_var = 127, -1.0
    for t in range(256):
        w_b += hist[t]
        if w_b == 0:
            continue
        w_f = total - w_b
        if w_f == 0:
            break
        sum_b += t * hist[t]
        m_b, m_f = sum_b / w_b, (sum_all - sum_b) / w_f
        var = w_b * w_f * (m_b - m_f) ** 2
        if var > best_var:
            best_var, best_t = var, t
    return best_t


def prepare_gray(img: Image.Image, target_long_side: int = 1800, max_long_side: int = 3500) -> Image.Image:
    img = ImageOps.exif_transpose(img)
    gray = ImageOps.grayscale(img)
    long_side = max(gray.size)
    if long_side < target_long_side:
        scale = min(3.0, target_long_side / long_side)
        gray = gray.resize((int(gray.width * scale), int(gray.height * scale)), Image.LANCZOS)
    elif long_side > max_long_side:
        scale = max_long_side / long_side
        gray = gray.resize((int(gray.width * scale), int(gray.height * scale)), Image.LANCZOS)
    return ImageOps.autocontrast(gray, cutoff=1)


def binarize(gray: Image.Image) -> Image.Image:
    t = otsu_threshold(gray)
    return gray.point(lambda p: 255 if p > t else 0)


def estimate_skew(gray: Image.Image, max_angle: float = 12.0, step: float = 1.0) -> float:
    """Cheap projection-profile deskew: the angle whose row-sum profile is 'spikiest'."""
    small = gray.copy()
    small.thumbnail((600, 600))
    t = otsu_threshold(small)
    ink = small.point(lambda p: 255 if p <= t else 0)  # text = white on black
    best_angle, best_score = 0.0, -1.0
    a = -max_angle
    while a <= max_angle + 1e-9:
        rot = ink.rotate(a, expand=False, fillcolor=0)
        w = rot.width
        data = rot.tobytes()
        rows = [sum(data[i * w:(i + 1) * w]) for i in range(rot.height)]
        score = sum((rows[i + 1] - rows[i]) ** 2 for i in range(len(rows) - 1))
        if score > best_score:
            best_angle, best_score = a, score
        a += step
    return best_angle


def _run(img: Image.Image, lang: str, variant: str) -> TesseractResult:
    data = pytesseract.image_to_data(img, lang=lang, config=TESS_CONFIG, output_type=pytesseract.Output.DICT)
    conf, n = average_confidence(data)
    return TesseractResult(text=text_from_data(data), confidence=conf, variant=variant, word_count=n,
                           words=words_from_data(data))


def run_tesseract(path: Path, lang: str = "eng", good_enough: float = 70.0) -> TesseractResult:
    """Grayscale pass; if weak, also try Otsu-binarised and OSD-rotated variants. Keep the best."""
    with Image.open(path) as im:
        gray = prepare_gray(im)
    best = _run(gray, lang, "gray")
    if best.confidence >= good_enough:
        return best
    try:
        cand = _run(binarize(gray), lang, "binarized")
        if cand.confidence > best.confidence:
            best = cand
    except pytesseract.TesseractError as exc:
        log.debug("binarized pass failed: %s", exc)
    if best.confidence < good_enough:
        try:  # cheap deskew for photos taken at a slight angle
            angle = estimate_skew(gray)
            if abs(angle) >= 1.0:
                cand = _run(gray.rotate(angle, expand=True, fillcolor=255, resample=Image.BICUBIC),
                            lang, f"deskew{angle:+.0f}")
                if cand.confidence > best.confidence:
                    best = cand
        except pytesseract.TesseractError as exc:
            log.debug("deskew pass failed: %s", exc)
    if best.confidence < good_enough:
        try:  # cheap orientation fix for sideways/upside-down photos
            osd = pytesseract.image_to_osd(gray, output_type=pytesseract.Output.DICT)
            rotate = int(osd.get("rotate", 0))
            if rotate and float(osd.get("orientation_conf", 0)) > 1.0:
                cand = _run(gray.rotate(-rotate, expand=True), lang, f"rotated{rotate}")
                if cand.confidence > best.confidence:
                    best = cand
        except (pytesseract.TesseractError, ValueError) as exc:
            log.debug("OSD skipped: %s", exc)
    return best


# --------------------------------------------------------------- pipeline

class ReceiptReader:
    def __init__(self, confidence_threshold: float = 70.0, lang: str = "eng",
                 vision: Optional[VisionClient] = None):
        self.threshold = confidence_threshold
        self.lang = lang
        self.vision = vision

    async def extract(self, path: Path, today: date) -> Extraction:
        ext = Extraction()
        flags: List[str] = []
        token_conf: Optional[float] = None
        try:
            tess = await asyncio.to_thread(run_tesseract, path, self.lang, self.threshold)
            parsed = parse_receipt_text(tess.text, today)
            ext = Extraction(amount=parsed.amount, receipt_date=parsed.receipt_date,
                             merchant=parsed.merchant, items=parsed.items, engine="tesseract",
                             confidence=tess.confidence, raw_text=tess.text)
            flags = parsed.flags
            token_conf = amount_token_confidence(tess.words, parsed.amount)
            log.info("tesseract: conf=%.1f words=%d variant=%s amount=%s amount_conf=%s flags=%s",
                     tess.confidence, tess.word_count, tess.variant, parsed.amount, token_conf, flags)
        except Exception as exc:  # never lose the receipt because OCR crashed
            log.exception("tesseract failed")
            ext.escalation_reason = f"tesseract error: {exc}"[:255]

        reasons: List[str] = []
        if ext.engine == "none":
            reasons.append("tesseract failed")
        else:
            if (ext.confidence or 0) < self.threshold:
                reasons.append(f"confidence {ext.confidence:.1f} < {self.threshold:g}")
            if ext.amount is None:
                reasons.append("amount not found")
            if token_conf is not None and token_conf < self.threshold:
                reasons.append(f"total read at {token_conf:.0f}% confidence")
            # Audit M7: a confident page can still be misread - cross-checks escalate too.
            reasons.extend(flags)
        if not reasons:
            return ext
        ext.escalation_reason = "; ".join(reasons)[:255]
        fallback_engine = "tesseract_fallback" if ext.engine != "none" else "none"

        if self.vision is None:
            ext.engine = fallback_engine
            ext.vision_error = "vision fallback not configured (VISION_API_KEY unset)"
            return ext
        ext.vision_model = self.vision.model
        try:
            v = await self.vision.read(path, today)
        except Exception as exc:
            log.warning("vision fallback failed: %s", exc)
            ext.engine = fallback_engine
            ext.vision_error = str(exc)[:500]
            return ext
        ext.vision_raw = v.raw
        ext.engine = "vision"
        # Prefer vision values; keep Tesseract values only where vision returned null.
        ext.amount = v.amount if v.amount is not None else ext.amount
        ext.receipt_date = v.receipt_date or ext.receipt_date
        ext.merchant = v.merchant or ext.merchant
        ext.items = v.items or ext.items
        return ext
