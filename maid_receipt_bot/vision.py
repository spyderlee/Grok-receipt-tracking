"""Low-cost vision-model fallback via any OpenAI-compatible /chat/completions API."""
from __future__ import annotations

import base64
import io
import json
import logging
import re
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

import httpx
from PIL import Image, ImageOps

from .images import register_heif
from .parsing import MAX_AMOUNT, parse_date_input, to_decimal

log = logging.getLogger(__name__)
register_heif()

SYSTEM_PROMPT = (
    "You read photos of shop receipts from Hong Kong and extract data. "
    "Respond with a single JSON object only, no prose, no markdown."
)
USER_PROMPT = (
    "Extract from this receipt and return strict JSON with exactly these keys:\n"
    '{"amount": number|null, "date": "YYYY-MM-DD"|null, "merchant": string|null, "items": string|null}\n'
    "- amount: the final total actually paid, in HKD, as a plain number (no currency symbol). "
    "Not the subtotal, not cash tendered, not change.\n"
    "- date: the purchase date printed on the receipt (receipts in HK are usually DD/MM/YYYY).\n"
    "- merchant: the shop / business name.\n"
    "- items: a short description of what was bought, e.g. \"milk, eggs, bread\".\n"
    "Use null for anything you cannot read."
)


class VisionError(RuntimeError):
    pass


@dataclass
class VisionResult:
    amount: Optional[Decimal]
    receipt_date: Optional[date]
    merchant: Optional[str]
    items: Optional[str]
    raw: str


def encode_image(path: Path, max_side: int = 1600, quality: int = 85) -> str:
    """Any Pillow-readable file (JPEG/PNG/WebP/GIF/HEIC...) re-encoded as JPEG, so the
    `data:image/jpeg` URL sent to the API is always truthful (audit L1)."""
    with Image.open(path) as im:
        im = ImageOps.exif_transpose(im).convert("RGB")
        im.thumbnail((max_side, max_side))
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=quality)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _clean_str(v: Any, limit: int) -> Optional[str]:
    if v is None:
        return None
    if isinstance(v, list):
        v = ", ".join(str(x.get("name", x)) if isinstance(x, dict) else str(x) for x in v)
    s = re.sub(r"\s+", " ", str(v)).strip()
    if not s or s.lower() in {"null", "none", "n/a", "unknown"}:
        return None
    return s[:limit]


def _first_json_object(text: str) -> tuple[Any, str]:
    """Decode the FIRST complete JSON object in the reply (audit L4: a greedy {.*} regex
    swallowed trailing prose such as '... {x}' and failed)."""
    dec = json.JSONDecoder()
    for m in re.finditer(r"\{", text):
        try:
            obj, end = dec.raw_decode(text, m.start())
        except json.JSONDecodeError:
            continue
        return obj, text[m.start():end]
    raise VisionError("vision reply contained no valid JSON object")


def parse_vision_amount(raw: Any) -> Optional[Decimal]:
    """'123.45', 123.45, 'HK$1,234.50', '1.234,50', '45,60' -> Decimal; junk -> None."""
    if raw is None or isinstance(raw, bool):
        return None
    s = re.sub(r"[^\d.,\-]", "", str(raw))
    if not s or s.count("-") > (1 if s.startswith("-") else 0):
        return None
    if "," in s and "." in s and s.rfind(",") > s.rfind("."):
        s = s.replace(".", "").replace(",", ".")          # 1.234,50 (European)
    elif re.fullmatch(r"-?\d+,\d{2}", s):
        s = s.replace(",", ".")                            # 45,60 (decimal comma)
    elif s.count(".") > 1:
        return None
    amount = to_decimal(s)
    if amount is None or not (0 < amount <= MAX_AMOUNT):
        return None
    return amount


def parse_vision_json(content: str, today: date) -> VisionResult:
    text = (content or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I)
    data, raw = _first_json_object(text)
    if not isinstance(data, dict):
        raise VisionError("vision reply JSON is not an object")

    amount = parse_vision_amount(data.get("amount"))
    d = None
    if data.get("date"):
        d = parse_date_input(str(data["date"]), today)
        if d is not None and d > today + timedelta(days=1):
            d = None  # a future "purchase date" is a misread
    return VisionResult(
        amount=amount,
        receipt_date=d,
        merchant=_clean_str(data.get("merchant"), 255),
        items=_clean_str(data.get("items"), 1000),
        raw=raw[:4000],
    )


class VisionClient:
    def __init__(self, api_key: str, model: str, base_url: str, timeout: float = 60.0,
                 transport: Optional[httpx.AsyncBaseTransport] = None):
        self.api_key = api_key
        self.model = model
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.timeout = timeout
        self._transport = transport

    def _payload(self, b64: str, json_mode: bool) -> dict:
        payload = {
            "model": self.model,
            "temperature": 0,
            "max_tokens": 400,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": [
                    {"type": "text", "text": USER_PROMPT},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}", "detail": "high"}},
                ]},
            ],
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        return payload

    async def read(self, path: Path, today: date) -> VisionResult:
        b64 = encode_image(path)
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        try:
            async with httpx.AsyncClient(timeout=self.timeout, transport=self._transport) as client:
                resp = await client.post(self.url, headers=headers, json=self._payload(b64, True))
                if resp.status_code == 400 and "response_format" in resp.text:
                    # some OpenAI-compatible servers don't support JSON mode
                    resp = await client.post(self.url, headers=headers, json=self._payload(b64, False))
                if resp.status_code >= 400:
                    raise VisionError(f"vision API HTTP {resp.status_code}: {resp.text[:200]}")
                content = resp.json()["choices"][0]["message"]["content"]
        except VisionError:
            raise
        except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as exc:
            raise VisionError(f"vision API call failed: {type(exc).__name__}: {exc}") from exc
        return parse_vision_json(content, today)
