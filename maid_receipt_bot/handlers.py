"""Telegram update handlers (python-telegram-bot v21, async)."""
from __future__ import annotations

import asyncio
import io
import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, Optional, Set, Tuple

from telegram import ForceReply, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, Conflict, NetworkError, TelegramError
from telegram.ext import ContextTypes

from . import formatting as fmt
from .config import Config
from .db import ADJUST_BALANCE_LIMIT, Database
from .images import ALLOWED_EXT, MIME_EXT
from .logsafe import scrub
from .ocr import Extraction, ReceiptReader
from .parsing import (MAX_AMOUNT, looks_like_topup, parse_amount_input, parse_date_input, parse_topup,
                      parse_year_month, to_decimal)

log = logging.getLogger(__name__)
HTML = ParseMode.HTML


@dataclass
class BotState:
    cfg: Config
    db: Database
    reader: ReceiptReader
    last_reconcile_problems: Optional[Tuple[str, ...]] = None
    last_conflict_log: float = 0.0


# Audit H2: a failed low-float alert is retried in the background after these delays (seconds);
# the DB claim lease (db.ALERT_CLAIM_LEASE, 5 min) covers the whole window. Tests shrink it.
ALERT_RETRY_DELAYS: Tuple[float, ...] = (5, 30, 120)
SWEEP_INTERVAL_SECONDS = 3600
MAX_RECEIPT_ID = 2 ** 63 - 1
_background: Set["asyncio.Task[Any]"] = set()


def spawn(coro) -> "asyncio.Task[Any]":
    """Fire-and-forget task that is kept referenced until done (tests can await `_background`)."""
    task = asyncio.get_running_loop().create_task(coro)
    _background.add(task)
    task.add_done_callback(_background.discard)
    return task


def _not_modified(exc: BaseException) -> bool:
    return isinstance(exc, BadRequest) and "not modified" in str(exc).lower()


async def safe_edit(fn, *args, **kwargs):
    """Audit L10: editing a message to identical content raises BadRequest 'message is not
    modified' - harmless, so don't let it reach on_error / the user."""
    try:
        return await fn(*args, **kwargs)
    except BadRequest as exc:
        if _not_modified(exc):
            return None
        raise


async def safe_answer(q, *args, **kwargs) -> None:
    """Answering a callback is cosmetic; an expired query ('query is too old') must not abort
    the ledger work that already committed (audit H2)."""
    try:
        await q.answer(*args, **kwargs)
    except TelegramError as exc:
        log.info("callback answer failed (ignored): %s", exc)


def _state(context: ContextTypes.DEFAULT_TYPE) -> BotState:
    return context.application.bot_data["state"]


async def run_db(fn, *args, **kwargs):
    """PyMySQL is blocking; keep the event loop free."""
    return await asyncio.to_thread(fn, *args, **kwargs)


def role_of(cfg: Config, update: Update) -> Optional[str]:
    user = update.effective_user
    if user is None:
        return None
    if user.id == cfg.admin_id:
        return "admin"
    if user.id == cfg.maid_id:
        return "maid"
    return None


async def _refuse(update: Update) -> None:
    user = update.effective_user
    log.warning("Refused update from unauthorised user id=%s username=%s",
                getattr(user, "id", None), getattr(user, "username", None))
    if update.callback_query:
        await update.callback_query.answer("Sorry, you're not authorised to use this bot.", show_alert=True)
    elif update.effective_message:
        await update.effective_message.reply_text("Sorry, this is a private bot.")


def authorised(*roles: str):
    def deco(fn):
        async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
            role = role_of(_state(context).cfg, update)
            if role is None:
                return await _refuse(update)
            if roles and role not in roles:
                if update.effective_message:
                    await update.effective_message.reply_text("Sorry, only the admin can do that.")
                return None
            return await fn(update, context, role)
        wrapper.__name__ = fn.__name__
        return wrapper
    return deco


def photo_path_for(base: Path, now: datetime, unique_id: str, ext: str) -> Path:
    safe = "".join(ch for ch in unique_id if ch.isalnum() or ch in "-_") or "photo"
    d = base / f"{now:%Y}" / f"{now:%m}"
    d.mkdir(parents=True, exist_ok=True, mode=0o750)
    return d / f"receipt_{now:%Y%m%d_%H%M%S}_{safe}{ext}"


async def _notify(context, chat_id: int, text: str) -> bool:
    try:
        await context.bot.send_message(chat_id=chat_id, text=text, parse_mode=HTML)
        return True
    except TelegramError as exc:
        log.error("Could not send message to %s: %s", chat_id, exc)
        return False


async def _show_summary(context, chat_id: int, receipt: Dict[str, Any], st: BotState, reply_to=None) -> None:
    """Send a fresh summary with buttons and retire the previous one (keeps buttons in view)."""
    old_chat, old_msg = receipt.get("summary_chat_id"), receipt.get("summary_message_id")
    if old_chat and old_msg:
        try:
            await context.bot.edit_message_text(chat_id=old_chat, message_id=old_msg,
                                                text=f"🧾 Receipt #{receipt['id']} — updated, see below ⬇️")
        except TelegramError:  # includes "message is not modified"
            pass
    target = reply_to.reply_text if reply_to is not None else (
        lambda **kw: context.bot.send_message(chat_id=chat_id, **kw))
    msg = await target(text=fmt.receipt_summary(receipt), reply_markup=fmt.summary_keyboard(receipt["id"]),
                       parse_mode=HTML)
    await run_db(st.db.set_summary_message, receipt["id"], msg.chat_id, msg.message_id)


# ------------------------------------------------------------------ commands

HELP_MAID = (
    "📷 <b>Send me a photo of each receipt.</b> I'll read it and show you the amount, date, "
    "shop and items. Check them, tap ✏️ Edit to fix anything, then ✅ Confirm.\n\n"
    "/balance — current float balance\n"
    "/history — recent receipts and top-ups (e.g. <code>/history 20</code>)\n"
    "/help — this message")
HELP_ADMIN = (
    HELP_MAID + "\n\n<b>Admin</b>\n"
    "<code>top up 500</code> (or /topup 500) — add money to the float\n"
    "/export 2026-10 — CSV of that month's confirmed receipts (default: this month)\n"
    "/void 12 reason — reverse confirmed receipt #12 (puts its amount back)\n"
    "/adjust -20 reason — correct the float by a signed amount\n"
    "/reconcile — check the float against the ledger and audit log")


@authorised()
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE, role: str) -> None:
    st = _state(context)
    name = st.cfg.admin_name if role == "admin" else (update.effective_user.first_name or "there")
    await update.effective_message.reply_text(
        f"Hi {name}! I track receipts against the household float (HKD).\n\n"
        + (HELP_ADMIN if role == "admin" else HELP_MAID), parse_mode=HTML)


@authorised()
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE, role: str) -> None:
    await update.effective_message.reply_text(HELP_ADMIN if role == "admin" else HELP_MAID, parse_mode=HTML)


@authorised()
async def cmd_balance(update: Update, context: ContextTypes.DEFAULT_TYPE, role: str) -> None:
    st = _state(context)
    row = await run_db(st.db.get_balance)
    text = f"💵 Float balance: <b>{fmt.money(row['balance'])}</b>"
    if row["balance"] < 0:
        text += "\n(negative — waiting for a top-up)"
    if role == "admin":
        pending = await run_db(st.db.pending_receipts)
        text += f"\nLow-float alert threshold: {fmt.money(st.cfg.low_float_threshold)}"
        text += f"\nLast low-float alert: {row['last_low_alert_date'] or 'never'}"
        if pending:
            text += f"\nPending receipts (not deducted yet): {len(pending)}"
    await update.effective_message.reply_text(text, parse_mode=HTML)


@authorised()
async def cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE, role: str) -> None:
    st = _state(context)
    n = 10
    if context.args:
        try:
            n = max(1, min(50, int(context.args[0])))
        except ValueError:
            await update.effective_message.reply_text("Usage: /history [number up to 50]")
            return
    rows = await run_db(st.db.history, n)
    pending = await run_db(st.db.pending_receipts)
    await update.effective_message.reply_text(fmt.history_text(rows, pending), parse_mode=HTML)


@authorised("admin")
async def cmd_export(update: Update, context: ContextTypes.DEFAULT_TYPE, role: str) -> None:
    st = _state(context)
    now = st.cfg.now()
    if context.args:
        ym = parse_year_month(context.args[0])
        if not ym:
            await update.effective_message.reply_text("Usage: /export YYYY-MM  (e.g. /export 2026-10)")
            return
    else:
        ym = (now.year, now.month)
    rows = await run_db(st.db.confirmed_receipts_for_month, *ym)
    # Audit N1: a voided receipt was put back into the float, so it is not spending - it stays in
    # the CSV (marked) but is left out of the total.
    live = [r for r in rows if not r.get("void_adjustment_id")]
    total = sum((r["amount"] for r in live), start=Decimal("0"))
    data = fmt.receipts_csv(rows)
    name = f"receipts-{ym[0]}-{ym[1]:02d}.csv"
    voided = len(rows) - len(live)
    extra = f" ({voided} voided receipt{'s' if voided != 1 else ''} listed but excluded)" if voided else ""
    await update.effective_message.reply_document(
        document=io.BytesIO(data), filename=name,
        caption=f"{len(live)} confirmed receipts in {ym[0]}-{ym[1]:02d}, total {fmt.money(total)}{extra}")


@authorised()
async def cmd_topup(update: Update, context: ContextTypes.DEFAULT_TYPE, role: str) -> None:
    text = "top up " + " ".join(context.args or [])
    await _do_topup(update, context, role, text)


async def _do_topup(update: Update, context, role: str, text: str) -> None:
    st = _state(context)
    msg = update.effective_message
    if role != "admin":
        await msg.reply_text(f"Only {st.cfg.admin_name} can top up the float.")
        return
    amount = parse_topup(text)
    if amount is None:
        await msg.reply_text("I couldn't read that amount. Try <code>top up 500</code> or "
                             "<code>top up 250.50</code>.", parse_mode=HTML)
        return
    res = await run_db(st.db.topup, amount, update.effective_user.id, msg.text or text, st.cfg.now())
    await msg.reply_text(f"✅ Topped up {fmt.money(res.amount)}.\nFloat balance: <b>{fmt.money(res.balance_after)}</b>"
                         f" (was {fmt.money(res.balance_before)})", parse_mode=HTML)
    ok = await _notify(context, st.cfg.maid_id,
                       f"💰 {st.cfg.admin_name} topped up the float by {fmt.money(res.amount)}.\n"
                       f"New float balance: <b>{fmt.money(res.balance_after)}</b>")
    if not ok:
        await msg.reply_text("(I couldn't notify the maid — she may need to open the bot and press Start.)")


_SIGNED_RE = re.compile(r"^([+-])\s*(?:HK\$|\$)?\s*(\d[\d,]*(?:\.\d{1,2})?)$", re.I)


def parse_signed_amount(text: str) -> Optional[Decimal]:
    m = _SIGNED_RE.match((text or "").strip())
    if not m:
        return None
    val = to_decimal(m.group(2))
    if val is None or val <= 0 or val > MAX_AMOUNT:
        return None
    return -val if m.group(1) == "-" else val


@authorised("admin")
async def cmd_adjust(update: Update, context: ContextTypes.DEFAULT_TYPE, role: str) -> None:
    """Audit L11: /adjust <+/-amount> <reason> - append-only correction of the float."""
    st = _state(context)
    msg = update.effective_message
    args = context.args or []
    amount = parse_signed_amount(args[0]) if args else None
    reason = " ".join(args[1:]).strip()
    if amount is None or not reason:
        await msg.reply_text("Usage: <code>/adjust -20 counted cash, was short</code> or "
                             "<code>/adjust +15.50 reason</code> (sign required, reason required)", parse_mode=HTML)
        return
    res = await run_db(st.db.adjust, amount, update.effective_user.id, reason, st.cfg.now())
    if res.status == "out_of_range":  # audit N10
        await msg.reply_text(
            f"Not adjusted: that would make the float {fmt.money(res.balance_after)} (now "
            f"{fmt.money(res.balance_before)}), beyond the ±{fmt.money(ADJUST_BALANCE_LIMIT)} limit. "
            "Check for a typo; for a real large change use top up.")
        return
    line = (f"🛠 Float adjusted by {'+' if amount > 0 else '-'}{fmt.money(abs(amount))} ({fmt.h(reason)}).\n"
            f"Float balance: <b>{fmt.money(res.balance_after)}</b> (was {fmt.money(res.balance_before)})")
    await msg.reply_text(line, parse_mode=HTML)
    await _notify(context, st.cfg.maid_id, line)


@authorised("admin")
async def cmd_void(update: Update, context: ContextTypes.DEFAULT_TYPE, role: str) -> None:
    """Audit L11: /void <receipt id> [reason] - reverse a confirmed receipt (once)."""
    st = _state(context)
    msg = update.effective_message
    args = context.args or []
    try:
        rid = int(args[0].lstrip("#"))
        if not 1 <= rid <= MAX_RECEIPT_ID:
            raise ValueError
    except (IndexError, ValueError):
        await msg.reply_text("Usage: <code>/void 12 entered twice</code>", parse_mode=HTML)
        return
    reason = " ".join(args[1:]).strip() or "voided by admin"
    res = await run_db(st.db.adjust, None, update.effective_user.id, reason, st.cfg.now(), rid)
    if res.status != "adjusted":
        await msg.reply_text({"not_found": f"There is no receipt #{rid}.",
                              "not_confirmed": f"Receipt #{rid} isn't confirmed, so nothing was deducted"
                                               " (pending receipts can just be cancelled).",
                              "already_voided": f"Receipt #{rid} was already voided."}.get(
                                  res.status, f"Receipt #{rid} was not voided ({res.status})."))
        return
    line = (f"🛠 Receipt #{rid} voided — {fmt.money(res.amount)} put back ({fmt.h(reason)}).\n"
            f"Float balance: <b>{fmt.money(res.balance_after)}</b> (was {fmt.money(res.balance_before)})")
    await msg.reply_text(line, parse_mode=HTML)
    await _notify(context, st.cfg.maid_id, line)


def reconcile_text(rec: Dict[str, Any]) -> str:
    head = (f"Float {fmt.money(rec['balance'])}; expected {fmt.money(rec['expected'])} = top-ups "
            f"{fmt.money(rec['topups'])} − confirmed receipts {fmt.money(rec['receipts'])} "
            f"+ adjustments {fmt.money(rec['adjustments'])}")
    if rec["ok"]:
        return "✅ <b>Ledger reconciles.</b>\n" + fmt.h(head)
    return ("🚨 <b>Ledger does NOT reconcile</b>\n" + fmt.h(head) + "\n"
            + "\n".join("• " + fmt.h(p) for p in rec["problems"]))


@authorised("admin")
async def cmd_reconcile(update: Update, context: ContextTypes.DEFAULT_TYPE, role: str) -> None:
    st = _state(context)
    rec = await run_db(st.db.reconcile)
    await update.effective_message.reply_text(reconcile_text(rec), parse_mode=HTML)


# --------------------------------------------------------------- photos

def _photo_ext(msg) -> Optional[str]:
    if msg.photo:
        return ".jpg"  # Telegram re-encodes compressed photos as JPEG
    doc = msg.document
    if doc and (doc.mime_type or "").lower().startswith("image/"):
        mime = (doc.mime_type or "").lower()
        if mime in MIME_EXT:  # audit L1: trust the mime type (".gif" stays ".gif")
            return MIME_EXT[mime]
        ext = os.path.splitext(doc.file_name or "")[1].lower()
        return ext if ext in ALLOWED_EXT else ".img"
    return None


def _sane_amount(extraction: Extraction) -> Extraction:
    """Audit M1: never hand MySQL an amount that DECIMAL(12,2) can't hold."""
    a = extraction.amount
    if a is not None and not (Decimal("0") < a <= MAX_AMOUNT):
        reason = f"amount {a} out of range - please type it"
        extraction.escalation_reason = "; ".join(x for x in (extraction.escalation_reason, reason) if x)[:255]
        extraction.amount = None
    return extraction


@authorised()
async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE, role: str) -> None:
    st = _state(context)
    msg = update.effective_message
    user = update.effective_user
    ext = _photo_ext(msg)
    if ext is None:
        await msg.reply_text("Please send the receipt as a photo.")
        return
    tg_obj = msg.photo[-1] if msg.photo else msg.document

    status = await msg.reply_text("📷 Got it — reading the receipt…")
    now = st.cfg.now()
    path = photo_path_for(st.cfg.photo_dir, now, tg_obj.file_unique_id, ext)
    try:
        tg_file = await tg_obj.get_file()
        await tg_file.download_to_drive(custom_path=path)
    except Exception:
        log.exception("photo download failed")
        path.unlink(missing_ok=True)
        await safe_edit(status.edit_text, "❌ I couldn't download that photo from Telegram. Please send it again.")
        return

    try:
        extraction = await st.reader.extract(path, now.date())
    except Exception as exc:  # pragma: no cover - reader already guards; belt and braces
        log.exception("OCR pipeline crashed")
        extraction = Extraction(engine="none", escalation_reason=str(exc)[:255])
    extraction = _sane_amount(extraction)

    try:
        rid = await run_db(st.db.create_pending_receipt, user.id, str(path), extraction, now,
                           tg_obj.file_id, tg_obj.file_unique_id)
    except Exception:
        # Audit M1: the maid must never be left on "reading…" with nothing saved.
        log.exception("could not save receipt")
        path.unlink(missing_ok=True)
        await safe_edit(status.edit_text, "❌ Sorry, I couldn't save that receipt (nothing was recorded or "
                                          "deducted). Please send the photo again in a minute.")
        return
    receipt = await run_db(st.db.get_receipt, rid)
    await safe_edit(status.edit_text, fmt.receipt_summary(receipt), reply_markup=fmt.summary_keyboard(rid),
                    parse_mode=HTML)
    await run_db(st.db.set_summary_message, rid, status.chat_id, status.message_id)


# ------------------------------------------------------------- callbacks

@authorised()
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, role: str) -> None:
    st = _state(context)
    q = update.callback_query
    user = update.effective_user
    parts = (q.data or "").split(":")
    try:
        action, rid = parts[0], int(parts[1])
        if not 1 <= rid <= MAX_RECEIPT_ID:
            raise ValueError
    except (IndexError, ValueError):
        await safe_answer(q)
        return
    receipt = await run_db(st.db.get_receipt, rid)
    if receipt is None:
        await safe_answer(q, "Receipt not found.", show_alert=True)
        return
    if role != "admin" and receipt["submitted_by"] != user.id:
        await safe_answer(q, "That isn't your receipt.", show_alert=True)
        return
    if receipt["status"] != "pending" and action not in ("c", "cc"):
        await safe_answer(q, f"Receipt #{rid} is already {receipt['status']}.", show_alert=True)
        try:
            await q.edit_message_text(fmt.final_status_text(receipt), parse_mode=HTML)
        except TelegramError:
            pass
        return

    now = st.cfg.now()
    if action in ("c", "cc"):
        await _confirm(update, context, st, rid, allow_duplicate=(action == "cc"))
    elif action == "e":
        await safe_answer(q)
        await safe_edit(q.edit_message_text, fmt.receipt_summary(receipt, "what needs fixing?"),
                        reply_markup=fmt.edit_keyboard(rid), parse_mode=HTML)
    elif action == "f" and len(parts) == 3 and parts[2] in fmt.FIELD_LABEL:
        field_name = parts[2]
        updated = await run_db(st.db.set_edit_field, rid, field_name, user.id, now)
        if updated is None:
            await safe_answer(q, "This receipt is no longer pending.", show_alert=True)
            return
        await safe_answer(q)
        await context.bot.send_message(
            chat_id=q.message.chat_id,
            text=f"✏️ Receipt #{rid} — {fmt.EDIT_PROMPT[field_name]}\n(or send <code>cancel</code> to leave it as is)",
            parse_mode=HTML,
            reply_markup=ForceReply(selective=True, input_field_placeholder=fmt.FIELD_LABEL[field_name]))
    elif action == "b":
        await run_db(st.db.set_edit_field, rid, None, user.id, now)
        await safe_answer(q)
        await safe_edit(q.edit_message_text, fmt.receipt_summary(receipt), reply_markup=fmt.summary_keyboard(rid),
                        parse_mode=HTML)
    elif action == "x":
        await safe_answer(q)
        await safe_edit(q.edit_message_text, fmt.receipt_summary(receipt, "cancel this receipt?")
                        + "\n\n<b>Cancel it?</b> Nothing will be deducted.",
                        reply_markup=fmt.cancel_keyboard(rid), parse_mode=HTML)
    elif action == "xx":
        result = await run_db(st.db.cancel_receipt, rid, user.id, now)
        if result == "cancelled_now":
            await safe_answer(q, "Cancelled.")
            await safe_edit(q.edit_message_text, f"❌ Receipt #{rid} cancelled — nothing was deducted.")
            return
        # Audit M2: someone else got there first (e.g. confirmed on another device) - report
        # what actually happened, from the database, not what this tap asked for.
        fresh = await run_db(st.db.get_receipt, rid) or receipt
        await safe_answer(q, f"Not cancelled — receipt #{rid} is already {fresh['status']}.", show_alert=True)
        await safe_edit(q.edit_message_text, fmt.final_status_text(fresh), parse_mode=HTML)
    else:
        await safe_answer(q)


def low_alert_text(st: BotState, balance: Decimal, receipt: Optional[Dict[str, Any]] = None) -> str:
    after = ""
    if receipt is not None:
        after = (f" after receipt #{receipt['id']} ({fmt.h(receipt.get('merchant'))}, "
                 f"{fmt.money(receipt.get('amount'))})")
    return (f"⚠️ <b>Low float</b>: balance is now <b>{fmt.money(balance)}</b> "
            f"(alert level {fmt.money(st.cfg.low_float_threshold)}){after}.\n"
            f"Reply <code>top up 500</code> to add funds. (Max one alert per day.)")


async def _finish_alert(st: BotState, sent: bool, balance: Decimal, rid: Optional[int],
                        error: Optional[str], attempts: int, claimed_at: Optional[datetime]) -> None:
    # Audit N5/N6: the claim token decides which day the alert counts for and whether this
    # sender still owns the claim (a slow retry must not release somebody else's).
    try:
        await run_db(st.db.finish_low_alert, st.cfg.now(), sent, balance, st.cfg.low_float_threshold,
                     rid, scrub(error) if error else None, attempts, claimed_at)
    except Exception:  # the claim lease expires by itself; the sweep will pick it up
        log.exception("could not record the low-float alert outcome")


async def send_low_alert(bot, st: BotState, balance: Decimal, receipt: Optional[Dict[str, Any]] = None,
                         claimed_at: Optional[datetime] = None) -> bool:
    """Audit H2: the caller holds today's alert claim. Try now; on failure keep retrying in the
    background and only then release the claim, so the daily cap is consumed by a *delivered*
    alert only."""
    text = low_alert_text(st, balance, receipt)
    rid = receipt["id"] if receipt else None
    try:
        await bot.send_message(chat_id=st.cfg.admin_id, text=text, parse_mode=HTML)
    except Exception as exc:
        log.warning("Low-float alert to admin failed (attempt 1), retrying in background: %s", exc)
        spawn(_retry_low_alert(bot, st, text, balance, rid, exc, claimed_at))
        return False
    await _finish_alert(st, True, balance, rid, None, 1, claimed_at)
    return True


async def _retry_low_alert(bot, st: BotState, text: str, balance: Decimal, rid: Optional[int],
                           err: BaseException, claimed_at: Optional[datetime] = None) -> bool:
    attempts = 1
    for delay in ALERT_RETRY_DELAYS:
        await asyncio.sleep(delay)
        attempts += 1
        try:
            await bot.send_message(chat_id=st.cfg.admin_id, text=text, parse_mode=HTML)
        except Exception as exc:
            err = exc
            log.warning("Low-float alert attempt %d failed: %s", attempts, exc)
            continue
        await _finish_alert(st, True, balance, rid, None, attempts, claimed_at)
        return True
    log.error("Low-float alert not delivered after %d attempts; the next receipt or the hourly check "
              "will try again", attempts)
    await _finish_alert(st, False, balance, rid, f"{type(err).__name__}: {err}", attempts, claimed_at)
    return False


async def _confirm(update: Update, context, st: BotState, rid: int, allow_duplicate: bool = False) -> None:
    q = update.callback_query
    user = update.effective_user
    res = await run_db(st.db.confirm_receipt, rid, user.id, st.cfg.now(), st.cfg.low_float_threshold,
                       allow_duplicate)
    if res.status == "missing_amount":
        await safe_answer(q, "Please set the amount first: tap ✏️ Edit → Amount.", show_alert=True)
        return
    if res.status in ("not_found", "cancelled"):
        await safe_answer(q, f"Receipt #{rid} can't be confirmed ({res.status}).", show_alert=True)
        return
    if res.status == "possible_duplicate":
        # Audit M4: same photo / same amount+date as an earlier receipt - ask again.
        await safe_answer(q, f"This looks like receipt #{res.duplicate_of}.", show_alert=True)
        await safe_edit(q.edit_message_text,
                        fmt.receipt_summary(res.receipt, "possible duplicate").split("\n\nTap")[0]
                        + f"\n\n⚠️ <b>This looks like receipt #{res.duplicate_of}</b>, which is already recorded. "
                          "Only confirm if it really is a second, separate purchase.",
                        reply_markup=fmt.duplicate_keyboard(rid), parse_mode=HTML)
        return
    r = res.receipt
    line = (f"✅ <b>Receipt #{rid} confirmed</b>\n{fmt.h(r['merchant'])} — {fmt.money(r['amount'])}"
            f"\nNew float balance: <b>{fmt.money(res.balance_after)}</b>")
    if res.status == "already_confirmed":
        await safe_answer(q, "Already confirmed.")
        try:
            await q.edit_message_text(line, parse_mode=HTML)
        except TelegramError:
            pass
        return
    # Audit H2: the low-float alert is sent FIRST - it is the one message with a daily cap, and
    # nothing below (an expired callback, a failed edit) may stop it.
    if res.low_float_alert:
        await send_low_alert(context.bot, st, res.balance_after, r, claimed_at=res.alert_claim)
    await safe_answer(q, "Confirmed ✅")
    try:
        await safe_edit(q.edit_message_text, fmt.receipt_summary(r, "confirmed").split("\n\n")[0], parse_mode=HTML)
    except TelegramError as exc:
        log.warning("could not update the summary message: %s", exc)
    # Reply to the person who confirmed (normally the maid) with the new balance ...
    await _notify(context, q.message.chat_id, line)
    # ... and to the maid too if the admin confirmed on her behalf.
    if r["submitted_by"] != user.id:
        await _notify(context, r["submitted_by"], line)


# ------------------------------------------------------------------ text

@authorised()
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE, role: str) -> None:
    st = _state(context)
    msg = update.effective_message
    text = (msg.text or "").strip()
    user = update.effective_user

    # Audit L3: an edit in progress wins - "Top Up Mart" is a merchant name, not a top-up.
    pending = await run_db(st.db.get_awaiting_edit, user.id, st.cfg.now())
    expired = None
    if pending is not None and pending.get("edit_expired"):
        # Audit N12: an edit left open for more than 30 minutes no longer captures text.
        expired, pending = pending, None
    if pending is None:
        if looks_like_topup(text):
            await _do_topup(update, context, role, text)
            return
        if expired is not None:
            await msg.reply_text(f"Your edit of receipt #{expired['id']} timed out (more than 30 minutes), so I "
                                 "didn't use that text. Tap ✏️ Edit on the receipt again to change it.")
            return
        await msg.reply_text("Send me a photo of a receipt 📷, or /help to see what I can do.")
        return

    field_name, rid = pending["edit_field"], pending["id"]
    now = st.cfg.now()
    if text.lower() in {"cancel", "/cancel", "skip", "back"}:
        await run_db(st.db.set_edit_field, rid, None, user.id, now)
        await _show_summary(context, msg.chat_id, pending, st, reply_to=msg)
        return

    value: Any
    if field_name == "amount":
        value = parse_amount_input(text)
        if value is None:
            hint = ""
            if looks_like_topup(text):
                hint = (f"\n(You're still editing receipt #{rid}. Send <code>cancel</code> first, "
                        "then the top-up.)")
            await msg.reply_text("That doesn't look like an amount. Send just the number, e.g. 45.50" + hint,
                                 parse_mode=HTML)
            return
    elif field_name == "receipt_date":
        value = parse_date_input(text, now.date())
        if value is None:
            await msg.reply_text("I couldn't read that date. Try 27/10/2026, 2026-10-27, 'today' or 'yesterday'.")
            return
    elif field_name == "merchant":
        value = None if text == "-" else text[:255]
    else:
        value = None if text == "-" else text[:1000]

    updated = await run_db(st.db.update_field, rid, field_name, value, user.id, now)
    if updated is None:
        await msg.reply_text(f"Receipt #{rid} is no longer pending.")
        return
    await _show_summary(context, msg.chat_id, updated, st, reply_to=msg)


async def on_unsupported(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if role_of(_state(context).cfg, update) is None:
        await _refuse(update)
        return
    if update.effective_message:
        await update.effective_message.reply_text("Please send receipts as photos 📷.")


# --------------------------------------------------------- background checks

async def sweep_once(bot, st: BotState) -> Dict[str, Any]:
    """Hourly + at startup: (1) a low-float alert that failed earlier, or whose sender crashed,
    is re-sent (audit H2) - only such undelivered alerts, never a fresh daily nag (audit N4), and
    not during LOW_ALERT_QUIET_HOURS; (2) the ledger is reconciled and the admin is told about a
    new mismatch (audit M5)."""
    out: Dict[str, Any] = {"alert": None, "reconcile": None}
    now = st.cfg.now()
    if st.cfg.in_quiet_hours(now):
        out["alert"] = "quiet_hours"
    else:
        claim = await run_db(st.db.claim_low_alert, now, st.cfg.low_float_threshold)
        if claim is not None:
            out["alert"] = await send_low_alert(bot, st, claim.balance, claimed_at=claim.claimed_at)
    rec = await run_db(st.db.reconcile)
    out["reconcile"] = rec
    if rec["ok"]:
        st.last_reconcile_problems = None
    else:
        key = tuple(rec["problems"])
        log.error("Ledger reconciliation failed: %s", "; ".join(key))
        if key != st.last_reconcile_problems:
            try:
                await bot.send_message(chat_id=st.cfg.admin_id, text=reconcile_text(rec), parse_mode=HTML)
                st.last_reconcile_problems = key
            except TelegramError as exc:
                log.error("could not send the reconciliation alert: %s", exc)
    return out


async def sweep_forever(bot, st: BotState, interval: float = SWEEP_INTERVAL_SECONDS) -> None:
    while True:
        try:
            await sweep_once(bot, st)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("periodic low-float / reconciliation check failed")
        await asyncio.sleep(interval)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    err = context.error
    if _not_modified(err):  # audit L10
        return
    if isinstance(err, Conflict):
        # Audit L9: another process is polling with this token (a second host, or a dev laptop).
        st = context.application.bot_data.get("state")
        now = time.monotonic()
        if st is None or now - st.last_conflict_log > 300:
            log.error("Telegram 409 Conflict: another bot instance is polling with this token. Stop the "
                      "other copy (see DEPLOY.md 'Only one instance'). Will keep retrying.")
            if st is not None:
                st.last_conflict_log = now
        return
    if update is None and isinstance(err, NetworkError):
        log.warning("Telegram network problem while polling (will retry): %s", err)
        return
    log.error("Unhandled error while processing update", exc_info=err)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "⚠️ Something went wrong on my side. Please try again in a minute "
                "(check /history first if you were confirming a receipt).")
        except TelegramError:
            pass
