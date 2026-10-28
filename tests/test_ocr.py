import json
import shutil
from datetime import date
from decimal import Decimal

import httpx
import pytest

from maid_receipt_bot.ocr import ReceiptReader, average_confidence, run_tesseract
from maid_receipt_bot.vision import VisionClient, VisionError, parse_vision_json
from tests.helpers import make_garbage_image, make_receipt_image

_skip_no_tesseract = pytest.mark.skipif(shutil.which("tesseract") is None, reason="tesseract not installed")


def needs_tesseract(fn):
    return pytest.mark.tesseract(_skip_no_tesseract(fn))
TODAY = date(2026, 10, 27)


def test_average_confidence_ignores_minus_one_and_empty():
    data = {"text": ["", "TOTAL", "  ", "12.00", "x"], "conf": ["-1", "90", "50", 70.0, -1]}
    # only TOTAL (90) and 12.00 (70) count; "  " is empty and "x" has conf -1
    assert average_confidence(data) == (80.0, 2)
    assert average_confidence({"text": [], "conf": []}) == (0.0, 0)


def vision_transport(content: str, status: int = 200, calls=None):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if calls is not None:
            calls.append(body)
        assert request.headers["authorization"] == "Bearer test-key"
        assert body["model"] == "gpt-4o-mini"
        assert body["messages"][1]["content"][1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
        if status != 200:
            return httpx.Response(status, text="upstream error")
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})
    return httpx.MockTransport(handler)


def vision_client(**kw):
    return VisionClient("test-key", "gpt-4o-mini", "https://vision.example/v1", transport=vision_transport(**kw))


@needs_tesseract
def test_tesseract_reads_synthetic_receipt(tmp_path):
    p = make_receipt_image(tmp_path / "r.png")
    res = run_tesseract(p, "eng", 70)
    assert res.confidence >= 70, res
    assert "TOTAL" in res.text.upper()


@needs_tesseract
async def test_pipeline_clear_receipt_uses_tesseract_only(tmp_path):
    calls = []
    p = make_receipt_image(tmp_path / "r.png")
    reader = ReceiptReader(70, "eng", VisionClient("test-key", "gpt-4o-mini", "https://v/v1",
                                                   transport=vision_transport("{}", calls=calls)))
    ext = await reader.extract(p, TODAY)
    assert ext.engine == "tesseract"
    assert ext.confidence >= 70
    assert ext.amount == Decimal("123.40")
    assert ext.receipt_date == date(2026, 10, 26)
    assert ext.merchant and "WELLCOME" in ext.merchant.upper()
    assert ext.items and "milk" in ext.items.lower()
    assert calls == []  # vision never called


@needs_tesseract
async def test_pipeline_threshold_forces_vision(tmp_path):
    """Same clear receipt, but threshold 99.9 => escalate; vision values win."""
    calls = []
    p = make_receipt_image(tmp_path / "r.png")
    content = json.dumps({"amount": 123.4, "date": "2026-10-26", "merchant": "Wellcome", "items": ["milk", "eggs"]})
    reader = ReceiptReader(99.9, "eng", VisionClient("test-key", "gpt-4o-mini", "https://v/v1",
                                                     transport=vision_transport(content, calls=calls)))
    ext = await reader.extract(p, TODAY)
    assert ext.engine == "vision"
    assert "confidence" in ext.escalation_reason
    assert ext.amount == Decimal("123.40") and ext.merchant == "Wellcome" and ext.items == "milk, eggs"
    assert len(calls) == 1 and calls[0]["response_format"] == {"type": "json_object"}


@needs_tesseract
async def test_pipeline_garbage_escalates_to_vision(tmp_path):
    p = make_garbage_image(tmp_path / "g.png")
    content = '```json\n{"amount": "HK$45.50", "date": "26/10/2026", "merchant": "7-Eleven", "items": null}\n```'
    reader = ReceiptReader(70, "eng", vision_client(content=content))
    ext = await reader.extract(p, TODAY)
    assert ext.engine == "vision"
    assert ext.confidence is not None and (ext.confidence < 70 or "amount not found" in ext.escalation_reason)
    assert ext.amount == Decimal("45.50")
    assert ext.receipt_date == date(2026, 10, 26)
    assert ext.merchant == "7-Eleven"


@needs_tesseract
async def test_pipeline_vision_failure_falls_back(tmp_path):
    p = make_garbage_image(tmp_path / "g.png")
    reader = ReceiptReader(70, "eng", vision_client(content="", status=500))
    ext = await reader.extract(p, TODAY)
    assert ext.engine == "tesseract_fallback"
    assert "HTTP 500" in ext.vision_error


@needs_tesseract
async def test_pipeline_no_vision_key(tmp_path):
    p = make_garbage_image(tmp_path / "g.png")
    ext = await ReceiptReader(70, "eng", None).extract(p, TODAY)
    assert ext.engine == "tesseract_fallback"
    assert "not configured" in ext.vision_error


async def test_pipeline_tesseract_crash_still_returns(tmp_path, monkeypatch):
    import maid_receipt_bot.ocr as ocr
    monkeypatch.setattr(ocr, "run_tesseract", lambda *a: (_ for _ in ()).throw(RuntimeError("boom")))
    p = make_receipt_image(tmp_path / "r.png")
    ext = await ReceiptReader(70, "eng", None).extract(p, TODAY)
    assert ext.engine == "none" and ext.amount is None


async def test_vision_retries_without_json_mode(tmp_path):
    seen = []

    def handler(request):
        body = json.loads(request.content)
        seen.append("response_format" in body)
        if "response_format" in body:
            return httpx.Response(400, text="unsupported parameter: response_format")
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"amount": 12}'}}]})

    p = make_receipt_image(tmp_path / "r.png")
    v = VisionClient("k", "m", "https://x/v1", transport=httpx.MockTransport(handler))
    res = await v.read(p, TODAY)
    assert res.amount == Decimal("12.00") and seen == [True, False]


def test_parse_vision_json_rejects_garbage():
    with pytest.raises(VisionError):
        parse_vision_json("sorry, I can't read that", TODAY)
    r = parse_vision_json('{"amount": -5, "date": "not a date", "merchant": "null", "items": ""}', TODAY)
    assert r.amount is None and r.receipt_date is None and r.merchant is None and r.items is None


@needs_tesseract
def test_tesseract_deskews_tilted_photo(tmp_path):
    from PIL import Image, ImageFilter
    from maid_receipt_bot.parsing import parse_receipt_text
    p = make_receipt_image(tmp_path / "r.png")
    im = Image.open(p).convert("RGB").rotate(8, expand=True, fillcolor=(200, 200, 190))
    im.filter(ImageFilter.GaussianBlur(0.8)).save(tmp_path / "tilt.jpg", quality=70)
    res = run_tesseract(tmp_path / "tilt.jpg", "eng", 70)
    assert res.variant.startswith("deskew") and res.confidence >= 70
    assert parse_receipt_text(res.text).amount == Decimal("123.40")
