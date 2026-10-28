from datetime import date
from decimal import Decimal

import pytest

from maid_receipt_bot.parsing import (extract_amount, extract_date, extract_merchant, looks_like_topup,
                                      parse_amount_input, parse_date_input, parse_receipt_text,
                                      parse_topup, parse_year_month)

D = Decimal


@pytest.mark.parametrize("text,expected", [
    ("top up 50", D("50.00")),
    ("Top Up 50", D("50.00")),
    ("topup 50.5", D("50.50")),
    ("top-up $50", D("50.00")),
    ("top_up 50", D("50.00")),
    ("/topup 500", D("500.00")),
    ("/topup@MaidReceiptBot 75", D("75.00")),
    ("top up HK$1,000", D("1000.00")),
    ("top up 1,250.75 HKD", D("1250.75")),
    ("  TOP UP   20  ", D("20.00")),
    ("top up: 30", D("30.00")),
    ("top up by 40", D("40.00")),
])
def test_topup_valid(text, expected):
    assert parse_topup(text) == expected
    assert looks_like_topup(text)


@pytest.mark.parametrize("text", ["top up", "top up abc", "top up 0", "top up -50", "top up 50 please",
                                  "top up 9999999", "I'll top up 50 later", "topup 50.555", "stop up 50"])
def test_topup_invalid(text):
    assert parse_topup(text) is None


def test_looks_like_topup_only_prefix():
    assert looks_like_topup("top up abc")
    assert not looks_like_topup("receipt 50")


@pytest.mark.parametrize("text,expected", [
    ("45.5", D("45.50")), ("$45.50", D("45.50")), ("HK$1,200", D("1200.00")),
    ("hkd 88", D("88.00")), ("88 HKD", D("88.00")), (" 12 ", D("12.00")), ("30元", D("30.00")),
])
def test_amount_input(text, expected):
    assert parse_amount_input(text) == expected


@pytest.mark.parametrize("text", ["", "abc", "0", "-5", "12.345", "1 2", "2000000"])
def test_amount_input_invalid(text):
    assert parse_amount_input(text) is None


@pytest.mark.parametrize("text,expected", [
    ("Date: 26/10/2026 14:32", date(2026, 10, 26)),
    ("2026-10-26", date(2026, 10, 26)),
    ("2026/1/5", date(2026, 1, 5)),
    ("05.11.26", date(2026, 11, 5)),
    ("10/26/2026", date(2026, 10, 26)),        # MM/DD fallback when DD/MM impossible
    ("03/04/2026", date(2026, 4, 3)),          # HK convention DD/MM wins when ambiguous
    ("26 Oct 2026", date(2026, 10, 26)),
    ("Oct 26, 2026", date(2026, 10, 26)),
    ("2026年10月26日", date(2026, 10, 26)),
    ("Tel 2345-6789 no date here", None),
    ("31/02/2026", None),
])
def test_extract_date(text, expected):
    assert extract_date(text) == expected


def test_date_input_relative():
    today = date(2026, 10, 27)
    assert parse_date_input("today", today) == today
    assert parse_date_input("Yesterday", today) == date(2026, 10, 26)
    assert parse_date_input("27/10/2026", today) == today
    assert parse_date_input("sometime", today) is None


SAMPLE = """WELLCOME SUPERMARKET
Shop 12, Tai Koo Shing
Tel: 2345 6789
Date: 26/10/2026 Time: 14:32
Fresh Milk 2L 32.90
Free Range Eggs 10 41.50
Wholemeal Bread 18.00
Dish Soap 31.00
SUBTOTAL 123.40
TOTAL HK$ 123.40
CASH 200.00
CHANGE 76.60
"""


def test_receipt_text_full():
    p = parse_receipt_text(SAMPLE)
    assert p.amount == D("123.40")
    assert p.receipt_date == date(2026, 10, 26)
    assert p.merchant == "WELLCOME SUPERMARKET"
    assert "Fresh Milk" in p.items and "Dish Soap" in p.items
    assert "SUBTOTAL" not in p.items and "CASH" not in p.items


@pytest.mark.parametrize("text,expected", [
    ("Item A 10.00\nSubtotal 30.00\nGrand Total 27.00\nCash 50.00", D("27.00")),
    ("Total\n$ 88.50\nChange 11.50", D("88.50")),            # value wrapped to next line
    ("合計 $56.80\n現金 100.00", D("56.80")),
    ("Amount Due: HK$1,234.50", D("1234.50")),
    ("TOTAL $50", D("50.00")),                                 # integer total with $
    ("Bananas 12.00\nApples 20.50\nCash 50.00\nChange 17.50", D("20.50")),  # fallback = max non-cash
    ("no numbers here", None),
])
def test_extract_amount(text, expected):
    assert extract_amount(text) == expected


def test_merchant_skips_noise():
    assert extract_merchant("RECEIPT\nTel: 1234\nPARKnSHOP\n") == "PARKnSHOP"


def test_year_month():
    assert parse_year_month("2026-10") == (2026, 10)
    assert parse_year_month("202610") == (2026, 10)
    assert parse_year_month("2026-13") is None
