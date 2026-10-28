"""Message text, keyboards and CSV rendering."""
from __future__ import annotations

import csv
import io
from decimal import Decimal
from html import escape
from typing import Any, Dict, Iterable, List, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

ENGINE_LABEL = {
    "tesseract": "Tesseract",
    "vision": "vision model",
    "tesseract_fallback": "Tesseract (low confidence, vision unavailable)",
    "none": "not readable",
}
FIELD_LABEL = {"amount": "Amount", "receipt_date": "Date", "merchant": "Merchant", "items": "Items"}


def money(v: Optional[Decimal]) -> str:
    if v is None:
        return "—"
    sign = "-" if v < 0 else ""
    return f"{sign}HK${abs(v):,.2f}"


def h(v: Any) -> str:
    return escape(str(v)) if v not in (None, "") else "—"


def receipt_summary(r: Dict[str, Any], header: str = "please check") -> str:
    amount = money(r["amount"]) if r.get("amount") is not None else "❓ not found — tap ✏️ Edit"
    conf = r.get("ocr_confidence")
    read_by = ENGINE_LABEL.get(r.get("ocr_engine"), r.get("ocr_engine"))
    if conf is not None:
        read_by += f", OCR confidence {float(conf):.0f}%"
    warn = ""
    reason = r.get("ocr_escalation_reason")
    # Audit M7: when no vision model double-checked a doubtful read, tell the maid why.
    if reason and r.get("ocr_engine") != "vision":
        warn += f"⚠️ <b>Check:</b> {escape(str(reason)[:300])}\n"
    if r.get("duplicate_of"):
        warn += f"⚠️ <b>Looks like a duplicate of receipt #{int(r['duplicate_of'])}</b> — check before confirming.\n"
    return (f"🧾 <b>Receipt #{r['id']}</b> — {escape(header)}\n"
            f"<b>Amount:</b> {escape(amount)}\n"
            f"<b>Date:</b> {h(r.get('receipt_date'))}\n"
            f"<b>Merchant:</b> {h(r.get('merchant'))}\n"
            f"<b>Items:</b> {h(r.get('items'))}\n"
            f"<i>Read by {escape(read_by)}</i>\n"
            f"{warn}\n"
            "Tap ✅ <b>Confirm</b> to deduct it from the float, ✏️ <b>Edit</b> to fix anything, "
            "or ❌ <b>Cancel</b> if this isn't a receipt.")


def summary_keyboard(rid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Confirm", callback_data=f"c:{rid}"),
        InlineKeyboardButton("✏️ Edit", callback_data=f"e:{rid}"),
        InlineKeyboardButton("❌ Cancel", callback_data=f"x:{rid}"),
    ]])


def duplicate_keyboard(rid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Confirm anyway", callback_data=f"cc:{rid}"),
        InlineKeyboardButton("❌ Cancel it", callback_data=f"x:{rid}"),
    ], [InlineKeyboardButton("« Back", callback_data=f"b:{rid}")]])


def final_status_text(r: Dict[str, Any]) -> str:
    """The receipt's real, committed state (audit M2: never assume what a race did)."""
    if r["status"] == "confirmed":
        return (f"✅ Receipt #{r['id']} is confirmed — {money(r['amount'])} was deducted"
                f" (float after: {money(r.get('balance_after'))}).")
    if r["status"] == "cancelled":
        return f"❌ Receipt #{r['id']} cancelled — nothing was deducted."
    return receipt_summary(r)


def edit_keyboard(rid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💲 Amount", callback_data=f"f:{rid}:amount"),
         InlineKeyboardButton("📅 Date", callback_data=f"f:{rid}:receipt_date")],
        [InlineKeyboardButton("🏪 Merchant", callback_data=f"f:{rid}:merchant"),
         InlineKeyboardButton("🛒 Items", callback_data=f"f:{rid}:items")],
        [InlineKeyboardButton("« Back", callback_data=f"b:{rid}")],
    ])


def cancel_keyboard(rid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("Yes, cancel it", callback_data=f"xx:{rid}"),
        InlineKeyboardButton("No, go back", callback_data=f"b:{rid}"),
    ]])


EDIT_PROMPT = {
    "amount": "Send the correct total amount in HKD, e.g. <code>45.50</code>",
    "receipt_date": "Send the receipt date, e.g. <code>27/10/2026</code>, <code>2026-10-27</code> or <code>today</code>",
    "merchant": "Send the shop / merchant name (or <code>-</code> to clear it)",
    "items": "Send a short description of the items, e.g. <code>milk, eggs, bread</code> (or <code>-</code> to clear)",
}


def history_text(rows: List[Dict[str, Any]], pending: List[Dict[str, Any]]) -> str:
    if not rows and not pending:
        return "No receipts or top-ups yet."
    lines = [f"<b>Last {len(rows)} transactions</b> (newest first)"]
    for r in rows:
        ts = r["event_time"].strftime("%d/%m %H:%M")
        if r["event_type"] == "topup":
            lines.append(f"{ts}  💰 Top-up  +{money(r['amount'])}  → {money(r['balance_after'])}")
        elif r["event_type"] == "adjustment":
            what = f"void #{r['receipt_id']}" if r.get("receipt_id") else "adjustment"
            sign = "+" if r["amount"] > 0 else "-"
            lines.append(f"{ts}  🛠 {what}  {sign}{money(abs(r['amount']))}  → {money(r['balance_after'])}")
        else:
            lines.append(f"{ts}  🧾 #{r['receipt_id']} {h(r.get('merchant'))}  -{money(r['amount'])}"
                         f"  → {money(r['balance_after'])}")
    if pending:
        ids = ", ".join(f"#{p['id']}" for p in pending)
        lines.append(f"\n⏳ Awaiting confirmation (not deducted yet): {ids}")
    return "\n".join(lines)


CSV_COLUMNS = ["receipt_id", "confirmed_at", "receipt_date", "merchant", "items", "amount_hkd",
               "balance_before", "balance_after", "ocr_engine", "ocr_confidence",
               "submitted_by", "confirmed_by", "photo_path",
               # audit N1: a voided receipt was refunded to the float - listed, but not spending
               "voided", "voided_at", "void_reason"]


_FORMULA_START = ("=", "+", "-", "@", "\t", "\r", "\n", "\uff1d", "\uff0b", "\uff0d", "\uff20")


def csv_text(v: Any) -> str:
    """Audit L6: neutralise spreadsheet formulas in free-text cells (OWASP CSV injection).
    Numeric columns are written separately and never pass through here."""
    s = "" if v is None else str(v)
    return "'" + s if s.startswith(_FORMULA_START) else s


def receipts_csv(rows: Iterable[Dict[str, Any]]) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(CSV_COLUMNS)
    for r in rows:
        w.writerow([r["id"], r["confirmed_at"].strftime("%Y-%m-%d %H:%M:%S") if r["confirmed_at"] else "",
                    r["receipt_date"] or "", csv_text(r["merchant"]), csv_text(r["items"]), f"{r['amount']:.2f}",
                    f"{r['balance_before']:.2f}", f"{r['balance_after']:.2f}", csv_text(r["ocr_engine"]),
                    "" if r["ocr_confidence"] is None else f"{r['ocr_confidence']:.2f}",
                    r["submitted_by"], r["confirmed_by"] or "", csv_text(r["photo_path"]),
                    "yes" if r.get("void_adjustment_id") else "no",
                    r["voided_at"].strftime("%Y-%m-%d %H:%M:%S") if r.get("voided_at") else "",
                    csv_text(r.get("void_reason"))])
    return buf.getvalue().encode("utf-8-sig")  # BOM so Excel shows Chinese text correctly
