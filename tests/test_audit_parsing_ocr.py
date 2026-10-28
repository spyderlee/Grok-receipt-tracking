"""Regression tests for the audit's parsing / OCR / CSV findings (probes/p_parsing.py,
probes/p_ocr.py). Each asserts the FIXED behaviour; the audit ID is in the test name."""
from tests.helpers import run_coro
from datetime import date, datetime
from decimal import Decimal as D
from pathlib import Path
from unittest import mock

import pytest

import maid_receipt_bot.ocr as o
from maid_receipt_bot.formatting import csv_text, receipt_summary, receipts_csv
from maid_receipt_bot.ocr import TesseractResult, amount_token_confidence, words_from_data
from maid_receipt_bot.parsing import (MAX_AMOUNT, extract_amount, extract_date, find_amount,
                                      looks_like_topup, parse_amount_input, parse_receipt_text, parse_topup)
from maid_receipt_bot.vision import parse_vision_json

TODAY = date(2026, 10, 27)


# ------------------------------------------------------------------ M1
@pytest.mark.parametrize("text", ["TOTAL 99999999999.99", "Grand Total 12345678901234.00", "TOTAL 1500000.00"])
def test_M1_total_line_amount_over_cap_is_not_returned(text):
    f = find_amount(text)
    assert f.amount is None
    assert "total out of range" in f.flags


def test_M1_cap_is_inclusive_and_fits_decimal_12_2():
    assert MAX_AMOUNT == D("1000000")
    assert extract_amount("TOTAL 1,000,000.00") == D("1000000.00")
    assert extract_amount("TOTAL 1,000,000.01") is None


# ------------------------------------------------------------------ M3
@pytest.mark.parametrize("text,flag", [
    ("REFUND\nTOTAL -12.50", "negative total"),
    ("TOTAL (12.50)", "negative total"),
    ("TOTAL 12.50-", "negative total"),
    ("退款\nTOTAL 12.50", "looks like a refund / void slip"),
    ("VOID\nTOTAL 12.50", "looks like a refund / void slip"),
])
def test_M3_refund_or_negative_total_is_never_prefilled(text, flag):
    r = parse_receipt_text(text, TODAY)
    assert r.amount is None
    assert flag in r.flags


# ------------------------------------------------------------------ L4
def test_L4_space_thousands():
    assert extract_amount("TOTAL HK$1 234.50") == D("1234.50")


def test_L4_integer_total_beats_payment_line():
    assert extract_amount("TOTAL 45\nVISA 50.00") == D("45.00")


def test_L4_date_prefers_label_and_rejects_future():
    assert extract_date("Valid 01/01/2030 Date 26/10/2026") == date(2026, 10, 26)
    r = parse_receipt_text("TOTAL 10.00\nValid until 01/01/2030", TODAY)
    assert r.receipt_date is None or r.receipt_date <= TODAY
    assert extract_date("Exp 01/27 date 26/10/26") == date(2026, 10, 26)


@pytest.mark.parametrize("content,amount", [
    ('{"amount": "1.234,50"}', D("1234.50")),
    ('{"amount": "HK$1,200.00"}', D("1200.00")),
    ('{"amount": "45.6", "date": "2026-10-27"} trailing {x}', D("45.60")),
    ('{"amount": -50}', None),
    ('{"amount": 1e9}', None),
    ('{"amount": true}', None),
    ('{"amount": "1200-1300"}', None),
])
def test_L4_vision_json_amounts(content, amount):
    assert parse_vision_json(content, TODAY).amount == amount


def test_L4_vision_future_date_rejected():
    assert parse_vision_json('{"amount": "5", "date": "2030-01-01"}', TODAY).receipt_date is None
    assert parse_vision_json('{"amount": "5", "date": "2026-10-27"}', TODAY).receipt_date == TODAY


# Behaviour the audit verified OK must stay OK.
@pytest.mark.parametrize("text,amount", [("top up 500", "500.00"), ("TOP   UP   500", "500.00"),
                                         ("Top-Up $50", "50.00"), ("top up\t500", "500.00"),
                                         ("top up ５００", "500.00"), ("top up 50,000", "50000.00"),
                                         ("top up 1000000", "1000000.00")])
def test_topup_accepted(text, amount):
    assert parse_topup(text) == D(amount)


@pytest.mark.parametrize("text", ["top up -500", "top up 1e9", "top up 500.555", "top up 0", "top up 0.001",
                                  "top up 1,000,000.01", "top up 500 600", "top up 1,00"])
def test_topup_rejected_but_recognised(text):
    assert parse_topup(text) is None and looks_like_topup(text)


@pytest.mark.parametrize("text,amount", [("45.5", "45.50"), ("HK$1,200", "1200.00"), ("0", None), ("-5", None),
                                         ("45.555", None), ("1,000,001", None)])
def test_amount_input(text, amount):
    assert parse_amount_input(text) == (D(amount) if amount else None)


# ------------------------------------------------------------------ M7
class FakeVision:
    model = "m"

    def __init__(self):
        self.calls = 0

    async def read(self, p, t):
        self.calls += 1
        return mock.Mock(amount=D("45.60"), receipt_date=None, merchant=None, items=None, raw="{}")


def _extract(text, conf, words=None):
    v = FakeVision()
    tess = TesseractResult(text=text, confidence=conf, variant="x", word_count=5, words=words or [])
    with mock.patch.object(o, "run_tesseract", return_value=tess):
        e = run_coro(o.ReceiptReader(70.0, "eng", v).extract(Path("/x"), TODAY))
    return e, v.calls


def test_M7_threshold_boundary_matches_spec():
    e, calls = _extract("TOTAL 45.60", 70.0)
    assert calls == 0 and e.engine == "tesseract"
    e, calls = _extract("TOTAL 45.60", 69.99)
    assert calls == 1 and e.engine == "vision"


def test_M7_no_total_line_escalates_even_when_confident():
    e, calls = _extract("Milk 20.00", 95.0)
    assert calls == 1 and e.engine == "vision"
    assert "no TOTAL line" in e.escalation_reason


def test_M7_low_confidence_on_the_amount_token_escalates():
    words = [("TOTAL", 96.0), ("45.60", 31.0)] + [("x", 99.0)] * 10
    e, calls = _extract("TOTAL 45.60", 95.0, words)
    assert calls == 1
    assert "total read at 31% confidence" in e.escalation_reason


def test_M7_items_not_adding_up_is_flagged():
    e, calls = _extract("Total items: 3\nMilk 20.00\nEggs 30.00", 95.0)
    assert calls == 1 and "items add up to" in e.escalation_reason


def test_M7_reason_shown_to_maid_when_vision_off():
    tess = TesseractResult(text="Milk 20.00", confidence=95.0, variant="x", word_count=2)
    with mock.patch.object(o, "run_tesseract", return_value=tess):
        e = run_coro(o.ReceiptReader(70.0, "eng", None).extract(Path("/x"), TODAY))
    assert e.engine == "tesseract_fallback"
    r = dict(id=1, status="pending", amount=e.amount, receipt_date=None, merchant=None, items=None,
             ocr_engine=e.engine, ocr_confidence=e.confidence, ocr_escalation_reason=e.escalation_reason,
             duplicate_of=None, vision_error=e.vision_error)
    s = receipt_summary(r)
    assert "⚠️ <b>Check:</b>" in s and "no TOTAL line" in s


def test_amount_token_confidence_helpers():
    data = {"text": ["TOTAL", "HK$1,234.50", "", "x"], "conf": ["90", "40", "-1", "-1"]}
    words = words_from_data(data)
    assert words == [("TOTAL", 90.0), ("HK$1,234.50", 40.0)]
    assert amount_token_confidence(words, D("1234.50")) == 40.0
    assert amount_token_confidence(words, D("9.99")) is None
    assert amount_token_confidence(words, None) is None


# ------------------------------------------------------------------ L6
@pytest.mark.parametrize("v", ['=HYPERLINK("http://evil","x")', "+1+1", "-2", "@SUM(A1)", "\t=1", "\r=1",
                               "＝1", "＋1"])
def test_L6_csv_formula_injection_guard(v):
    assert csv_text(v).startswith("'")


def test_L6_csv_export_row_is_neutralised_but_numbers_are_not():
    row = dict(id=1, confirmed_at=datetime(2026, 10, 27), receipt_date=None, merchant='=HYPERLINK("http://evil","x")',
               items="+1+1", amount=D("1"), balance_before=D("2"), balance_after=D("-1"), ocr_engine="vision",
               ocr_confidence=None, submitted_by=2, confirmed_by=2, photo_path="/p")
    line = receipts_csv([row]).decode("utf-8-sig").splitlines()[1]
    assert "'=HYPERLINK" in line and "'+1+1" in line
    assert ",-1.00," in line or line.endswith(",-1.00") or "-1.00" in line  # negative balance stays numeric
    assert csv_text("Wellcome") == "Wellcome" and csv_text(None) == ""
