"""Pure text parsing: top-up commands, user edits, and receipt OCR text."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import List, Optional, Tuple

MAX_AMOUNT = Decimal("1000000.00")  # sanity cap for typed amounts (HKD)
CENT = Decimal("0.01")

# A number: 1,234.56 | 1234.5 | 50 | .50
_NUM = r"(\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?|\.\d{1,2})"

# ---------------------------------------------------------------- amounts

def to_decimal(raw: str) -> Optional[Decimal]:
    try:
        val = Decimal(raw.replace(",", "").strip())
    except (InvalidOperation, AttributeError):
        return None
    if not val.is_finite():
        return None
    return val.quantize(CENT)


_AMOUNT_INPUT_RE = re.compile(
    r"^\s*(?:hk\s*\$|hkd|\$)?\s*" + _NUM + r"\s*(?:hkd|hk\$|dollars?|元|蚊)?\s*$", re.I)


def parse_amount_input(text: str) -> Optional[Decimal]:
    """Parse an amount typed by a human ("45.5", "$45.50", "HK$1,200"). >0 and <= MAX_AMOUNT."""
    m = _AMOUNT_INPUT_RE.match(text or "")
    if not m:
        return None
    val = to_decimal(m.group(1))
    if val is None or val <= 0 or val > MAX_AMOUNT:
        return None
    return val


# ----------------------------------------------------------------- top-up

_TOPUP_PREFIX_RE = re.compile(r"^\s*/?top[\s_\-]*up\b", re.I)
_TOPUP_RE = re.compile(
    r"^\s*/?top[\s_\-]*up(?:@\w+)?\s*[:=]?\s*(?:by\s+)?(?:hk\s*\$|hkd|\$)?\s*" + _NUM
    + r"\s*(?:hkd|hk\$|dollars?)?\s*$", re.I)


def looks_like_topup(text: str) -> bool:
    return bool(_TOPUP_PREFIX_RE.match(text or ""))


def parse_topup(text: str) -> Optional[Decimal]:
    """'top up 50', 'topup 50.5', 'top-up $50', '/topup 50', 'Top up HK$1,000' -> Decimal."""
    m = _TOPUP_RE.match(text or "")
    if not m:
        return None
    val = to_decimal(m.group(1))
    if val is None or val <= 0 or val > MAX_AMOUNT:
        return None
    return val


# ------------------------------------------------------------------ dates

_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}
_MON = r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?"

_DATE_PATTERNS: List[Tuple[re.Pattern, str]] = [
    (re.compile(r"(?<!\d)(20\d{2})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})\s*日?(?!\d)"), "ymd"),
    (re.compile(r"(?<!\d)(\d{1,2})[-/.](\d{1,2})[-/.](20\d{2}|\d{2})(?!\d)"), "dmy"),
    (re.compile(r"(?<!\d)(\d{1,2})(?:st|nd|rd|th)?[\s\-]*" + _MON + r"[\s,\-]*(20\d{2}|\d{2})(?!\d)", re.I), "d_mon_y"),
    (re.compile(_MON + r"\s*(\d{1,2})(?:st|nd|rd|th)?,?\s+(20\d{2})(?!\d)", re.I), "mon_d_y"),
]


def _mk_date(y: int, m: int, d: int) -> Optional[date]:
    if y < 100:
        y += 2000
    if not 2000 <= y <= 2099:
        return None
    try:
        return date(y, m, d)
    except ValueError:
        return None


def _date_from_match(m: re.Match, kind: str) -> Optional[date]:
    g = m.groups()
    if kind == "ymd":
        return _mk_date(int(g[0]), int(g[1]), int(g[2]))
    if kind == "dmy":  # Hong Kong convention is DD/MM/YYYY; fall back to MM/DD if invalid
        a, b, y = int(g[0]), int(g[1]), int(g[2])
        return _mk_date(y, b, a) or _mk_date(y, a, b)
    if kind == "d_mon_y":
        return _mk_date(int(g[2]), _MONTHS[g[1].lower()[:3]], int(g[0]))
    if kind == "mon_d_y":
        return _mk_date(int(g[2]), _MONTHS[g[0].lower()[:3]], int(g[1]))
    return None


_DATE_LABEL_RE = re.compile(r"\bdate\b|日期|交易日|\bdt\b", re.I)
_NOT_PURCHASE_DATE_RE = re.compile(r"\bexp|expir|valid|best\s*before|use\s*by|有效|到期", re.I)


def _dates_in(text: str) -> List[Tuple[int, date]]:
    found: List[Tuple[int, date]] = []
    for rx, kind in _DATE_PATTERNS:
        for m in rx.finditer(text or ""):
            d = _date_from_match(m, kind)
            if d:
                found.append((m.start(), d))
    return sorted(found)


def extract_date(text: str, today: Optional[date] = None) -> Optional[date]:
    """Purchase date printed on a receipt (audit L4).

    Preference: a date right after a "Date/日期" label, then the earliest date on a line that
    isn't an expiry/validity line. With `today`, dates more than a day in the future
    (vouchers, "valid until", OCR garbage) are rejected."""
    def ok(d: date) -> bool:
        return today is None or d <= today + timedelta(days=1)

    for line in (text or "").splitlines():
        for lm in _DATE_LABEL_RE.finditer(line):
            after = [(pos, d) for pos, d in _dates_in(line[lm.end():]) if ok(d)]
            if after:
                return after[0][1]
    for line in (text or "").splitlines():
        if _NOT_PURCHASE_DATE_RE.search(line) and not _DATE_LABEL_RE.search(line):
            continue
        cands = [d for _, d in _dates_in(line) if ok(d)]
        if cands:
            return cands[0]
    return None


def parse_date_input(text: str, today: date) -> Optional[date]:
    t = (text or "").strip().lower()
    if t in {"today", "今日", "今天"}:
        return today
    if t in {"yesterday", "尋日", "昨天", "昨日"}:
        return today - timedelta(days=1)
    return extract_date(text)  # typed by a human: future dates allowed (they can fix typos)


# ----------------------------------------------------- receipt text parse

_MONEY_RE = re.compile(r"(?<![\d.,])(?:HK\s*\$|HKD|\$)?\s*(\d{1,3}(?:,\d{3})+|\d+)\s?[.](\d{2})(?![\d])", re.I)
# "HK$1 234.50": space as thousands separator - only trusted right after a currency marker,
# otherwise "Eggs 10 41.50" would become 1041.50 (audit L4).
_MONEY_SPACE_RE = re.compile(r"(?:HK\s*\$|HKD|\$)\s*(\d{1,3}(?: \d{3})+)[.](\d{2})(?![\d])", re.I)
_MONEY_INT_RE = re.compile(r"(?:HK\s*\$|HKD|\$)\s*(\d{1,6})(?![\d.,])", re.I)
# "TOTAL 45" / "總計 45" / "TOTAL 45,60": label + bare number and nothing else on the line.
_BARE_TOTAL_RE = re.compile(r"^[\s:：=]*(?:HK\s*\$|HKD|\$)?\s*(\d{1,6})(?:,(\d{2}))?\s*(?:HKD|元)?\s*$", re.I)
_NEG_RE = re.compile(r"(?:^|[\s:：$])-\s*(?:HK\s*\$|HKD|\$)?\s*\d|\d\s*-\s*$|\(\s*(?:HK\s*\$|\$)?\s*\d[\d,]*\.\d{2}\s*\)", re.I)
_REFUND_RE = re.compile(r"refund|退款|退貨|退款單|\bvoid\b|credit\s*note|return(?:ed)?\s+(?:slip|receipt)", re.I)
_PAYMENT_LINE_RE = re.compile(r"\bvisa\b|master\s*card|\bamex\b|unionpay|銀聯|octopus|八達通|\beps\b|alipay|"
                              r"wechat|payme|\bfps\b|轉數快|\bcard\b", re.I)
_CASH_RE = re.compile(r"\bcash\b|現金", re.I)
_CHANGE_RE = re.compile(r"\bchange\b|找贖|找續|找零", re.I)
LARGE_AMOUNT = Decimal("5000.00")  # household float: anything bigger is worth a second look

_TOTAL_PRIORITY = [
    (3, re.compile(r"grand\s*total|total\s*(?:due|amount|payable|paid)|amount\s*(?:due|payable)|"
                   r"net\s*(?:total|amount)|總計|總額|總金額|應付|實付|實收", re.I)),
    (2, re.compile(r"\btotal\b|合計|總數|共計", re.I)),
    (1, re.compile(r"\bamount\b|\bamt\b|金額|\bnet\b", re.I)),
]
_EXCLUDE_RE = re.compile(
    r"sub\s*-?\s*total|小計|\bchange\b|找贖|找續|找零|\bcash\b|現金|tender|received|"
    r"remain|餘額|(?<!\w)balance(?!\s*due)|discount|折扣|saving|\btax\b|service\s*charge|"
    r"total\s*(?:items?|qty|quantity|pcs|件)|"
    r"\bqty\b|quantity|數量|\btel\b|phone|電話|\bfax\b|points?|積分|card\s*no", re.I)
_SKIP_MERCHANT_RE = re.compile(
    r"receipt|invoice|收據|發票|\btel\b|phone|電話|date|time|日期|www\.|http|@|"
    r"cashier|收銀|order\s*no|table|\bno\.\s*\d|thank", re.I)


def _money_values(line: str) -> List[Decimal]:
    vals: List[Tuple[int, Decimal]] = []
    for m in _MONEY_SPACE_RE.finditer(line):
        v = to_decimal(m.group(1).replace(" ", "") + "." + m.group(2))
        if v is not None:
            vals.append((m.start(), v))
    rest = _MONEY_SPACE_RE.sub(lambda m: " " * len(m.group(0)), line)
    for m in _MONEY_RE.finditer(rest):
        v = to_decimal(f"{m.group(1)}.{m.group(2)}")
        if v is not None:
            vals.append((m.start(), v))
    return [v for _, v in sorted(vals)]


@dataclass
class AmountFind:
    amount: Optional[Decimal]
    source: str                      # total_line | fallback | none
    flags: List[str]
    line: Optional[str] = None       # the OCR line the amount came from


INTEGER_TOTAL_CHECK = Decimal("1000")  # audit N9: "TOTAL 4560" may be 45.60 without its point


def _total_line_values(line: str, keyword_end: int) -> Tuple[List[Decimal], str]:
    """Values on a TOTAL line and how they were written: 'decimal' (45.60), 'currency_int'
    (HK$45) or 'bare_int' (TOTAL 45 - no currency marker, no decimals)."""
    vals = _money_values(line)
    if vals:
        return vals, "decimal"
    vals = [v for v in (to_decimal(x) for x in _MONEY_INT_RE.findall(line)) if v is not None]
    if vals:
        return vals, "currency_int"
    m = _BARE_TOTAL_RE.match(line[keyword_end:])
    if m:
        v = to_decimal(m.group(1) + ("." + m.group(2) if m.group(2) else ""))
        return ([v] if v is not None else []), ("decimal" if m.group(2) else "bare_int")
    return [], "none"


def find_amount(text: str) -> AmountFind:
    """Locate the receipt total and say how sure we are (audit M1, M3, M7)."""
    lines = [ln.strip() for ln in (text or "").splitlines()]
    flags: List[str] = []
    if _REFUND_RE.search(text or ""):
        flags.append("looks like a refund / void slip")
    best: Optional[Tuple[int, Decimal, str]] = None
    best_kind, best_index = "none", -1
    negative = False
    for i, line in enumerate(lines):
        if not line or _EXCLUDE_RE.search(line):
            continue
        hit = next(((p, rx.search(line)) for p, rx in _TOTAL_PRIORITY if rx.search(line)), None)
        if not hit:
            continue
        prio, km = hit
        vals, kind = _total_line_values(line, km.end())
        src_line, src_index = line, i
        if not vals and i + 1 < len(lines) and not _EXCLUDE_RE.search(lines[i + 1]) \
                and not _PAYMENT_LINE_RE.search(lines[i + 1]):
            vals, kind = _money_values(lines[i + 1]), "decimal"  # amount wrapped onto the next line
            src_line, src_index = lines[i + 1], i + 1
        if not vals:
            continue
        if _NEG_RE.search(src_line[km.end():] if src_line is line else src_line):
            negative = True
        cand = (prio, vals[-1], src_line)
        if best is None or cand[:2] > best[:2]:
            best, best_kind, best_index = cand, kind, src_index
    if negative:
        flags.append("negative total")
    if best and best_kind in ("bare_int", "currency_int"):
        # Audit N9: an integer total is plausible ("TOTAL 45") but is also what OCR produces when
        # it drops the decimal point ("TOTAL 4560" for 45.60). Ask for a second look when it is
        # big without a currency marker, or when the item lines carry cents.
        other_decimals = any(_money_values(ln) for j, ln in enumerate(lines) if j != best_index and ln)
        if (best_kind == "bare_int" and best[1] >= INTEGER_TOTAL_CHECK) or other_decimals:
            flags.append("total has no decimal point (check it is not missing, e.g. 4560 for 45.60)")
    if best:
        amount = best[1]
        if not (0 < amount <= MAX_AMOUNT):
            flags.append("total out of range" if amount > MAX_AMOUNT else "zero total")
            amount = None
        if flags and amount is not None and ("negative total" in flags or "looks like a refund / void slip" in flags):
            amount = None  # never pre-fill a refund as an expense
        return AmountFind(amount, "total_line", flags, best[2])
    # Fallback: largest decimal money value on a non-excluded line.
    vals = [v for ln in lines if ln and not _EXCLUDE_RE.search(ln) for v in _money_values(ln)]
    vals = [v for v in vals if 0 < v <= MAX_AMOUNT]
    if not vals:
        return AmountFind(None, "none", flags)
    flags.append("no TOTAL line (guessed the largest number)")
    amount = None if any("refund" in f for f in flags) else max(vals)
    return AmountFind(amount, "fallback", flags)


def extract_amount(text: str) -> Optional[Decimal]:
    return find_amount(text).amount


def _item_values(text: str) -> List[Decimal]:
    out: List[Decimal] = []
    for line in [ln.strip() for ln in (text or "").splitlines() if ln.strip()]:
        if _EXCLUDE_RE.search(line) or _dates_in(line) or _PAYMENT_LINE_RE.search(line):
            continue
        if any(rx.search(line) for _, rx in _TOTAL_PRIORITY):
            continue
        vals = _money_values(line)
        if vals:
            out.append(vals[-1])
    return out


def _labelled_value(text: str, rx: re.Pattern) -> Optional[Decimal]:
    for line in (text or "").splitlines():
        if rx.search(line):
            vals = _money_values(line)
            if vals:
                return vals[-1]
    return None


def sanity_flags(text: str, amount: Optional[Decimal], receipt_date: Optional[date],
                 today: Optional[date] = None) -> List[str]:
    """Audit M7: cross-checks that catch confident-but-wrong reads."""
    flags: List[str] = []
    if amount is None:
        return flags
    items = _item_values(text)
    if len(items) >= 2:
        total_items = sum(items, Decimal("0.00"))
        if abs(total_items - amount) > Decimal("0.05"):
            flags.append(f"items add up to {total_items} not {amount}")
    if items and max(items) > amount + Decimal("0.05") and len(items) < 2:
        flags.append(f"an item ({max(items)}) costs more than the total")
    cash, change = _labelled_value(text, _CASH_RE), _labelled_value(text, _CHANGE_RE)
    if cash is not None and change is not None and abs((cash - change) - amount) > Decimal("0.05"):
        flags.append(f"cash {cash} - change {change} != total {amount}")
    if cash is not None and cash + Decimal("0.05") < amount:
        flags.append(f"cash paid {cash} is less than the total")
    if amount > LARGE_AMOUNT:
        flags.append(f"unusually large amount (> {LARGE_AMOUNT:.0f})")
    if today is not None and receipt_date is not None and receipt_date > today + timedelta(days=1):
        flags.append("receipt date is in the future")
    return flags


def _alpha_ratio(s: str) -> float:
    s2 = s.replace(" ", "")
    return sum(ch.isalpha() for ch in s2) / len(s2) if s2 else 0.0


def extract_merchant(text: str) -> Optional[str]:
    for line in [ln.strip() for ln in (text or "").splitlines() if ln.strip()][:6]:
        if _SKIP_MERCHANT_RE.search(line) or _dates_in(line):
            continue
        if sum(ch.isalpha() for ch in line) >= 3 and _alpha_ratio(line) >= 0.6:
            return re.sub(r"\s{2,}", " ", line)[:120]
    return None


_QTY_RE = re.compile(r"^\s*\d+\s*[x×@]\s*|\s+\d+\s*[x×@]\s*\d*\s*$|\s+[x×@]\s*\d+\s*$", re.I)


def extract_items(text: str, merchant: Optional[str] = None, limit: int = 300) -> Optional[str]:
    items: List[str] = []
    for line in [ln.strip() for ln in (text or "").splitlines() if ln.strip()]:
        if merchant and line.startswith(merchant):
            continue
        if not _money_values(line) or _EXCLUDE_RE.search(line) or _dates_in(line):
            continue
        if any(rx.search(line) for _, rx in _TOTAL_PRIORITY):
            continue
        name = _MONEY_RE.sub(" ", _MONEY_SPACE_RE.sub(" ", line))
        name = _QTY_RE.sub(" ", name)
        name = re.sub(r"[^\w\s&'\-/().\u4e00-\u9fff]", " ", name)
        name = re.sub(r"\s{2,}", " ", name).strip(" .-")
        if sum(ch.isalpha() for ch in name) >= 2:
            items.append(name)
    if not items:
        return None
    out = "; ".join(items)
    return out if len(out) <= limit else out[: limit - 1] + "…"


@dataclass
class ParsedReceipt:
    amount: Optional[Decimal]
    receipt_date: Optional[date]
    merchant: Optional[str]
    items: Optional[str]
    amount_source: str = "none"
    flags: List[str] = field(default_factory=list)   # reasons a human / vision should double-check
    amount_line: Optional[str] = None


def parse_receipt_text(text: str, today: Optional[date] = None) -> ParsedReceipt:
    merchant = extract_merchant(text)
    found = find_amount(text)
    rdate = extract_date(text, today)
    flags = list(found.flags) + sanity_flags(text, found.amount, rdate, today)
    return ParsedReceipt(
        amount=found.amount,
        receipt_date=rdate,
        merchant=merchant,
        items=extract_items(text, merchant),
        amount_source=found.source,
        flags=flags,
        amount_line=found.line,
    )


_YM_RE = re.compile(r"^\s*(20\d{2})[-/.]?(0[1-9]|1[0-2])\s*$")


def parse_year_month(text: str) -> Optional[Tuple[int, int]]:
    m = _YM_RE.match(text or "")
    return (int(m.group(1)), int(m.group(2))) if m else None
