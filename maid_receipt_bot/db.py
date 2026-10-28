"""MySQL access layer. All money movements are single InnoDB transactions that lock
the float_balance row with SELECT ... FOR UPDATE. Times passed in are naive local
(configured TZ, default HKT) datetimes, see migrations/001_initial.sql."""
from __future__ import annotations

import functools
import json
import logging
import random
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Dict, Iterator, List, Optional

import pymysql
from pymysql.cursors import DictCursor

from .config import Config
from .ocr import Extraction

log = logging.getLogger(__name__)

EDITABLE_FIELDS = {"amount": "amount", "receipt_date": "receipt_date",
                   "merchant": "merchant", "items": "items"}


def _json(obj: Any) -> str:
    return json.dumps(obj, default=str, ensure_ascii=False)


@dataclass
class ConfirmResult:
    # confirmed | already_confirmed | cancelled | missing_amount | not_found | possible_duplicate
    status: str
    receipt: Optional[Dict[str, Any]] = None
    balance_before: Optional[Decimal] = None
    balance_after: Optional[Decimal] = None
    # True = this caller holds today's low-float alert claim and must send the alert, then
    # call Database.finish_low_alert(). The once-per-day cap is only consumed on success.
    low_float_alert: bool = False
    duplicate_of: Optional[int] = None
    # Audit N6: the claim token (low_alert_claimed_at) to hand back to finish_low_alert().
    alert_claim: Optional[datetime] = None


@dataclass
class AlertClaim:
    balance: Decimal
    claimed_at: datetime


@dataclass
class TopupResult:
    topup_id: int
    amount: Decimal
    balance_before: Decimal
    balance_after: Decimal


@dataclass
class AdjustResult:
    status: str  # adjusted | not_found | not_confirmed | already_voided | out_of_range
    adjustment_id: Optional[int] = None
    amount: Optional[Decimal] = None
    balance_before: Optional[Decimal] = None
    balance_after: Optional[Decimal] = None


# How long one worker may hold the "I'm sending today's low-float alert" claim before
# another worker may take it over (covers a crash between claim and send).
ALERT_CLAIM_LEASE = timedelta(minutes=5)
# Audit N10: /adjust may not take the float beyond this (either sign) in one or many steps.
ADJUST_BALANCE_LIMIT = Decimal("1000000.00")
# Audit N12: an "I'm about to type the amount" edit session older than this is ignored.
EDIT_SESSION_TTL = timedelta(minutes=30)
# Audit N11: the schema this code needs (MAX(version) in schema_version; see deploy/migrate.sh).
REQUIRED_SCHEMA_VERSION = 1


# Audit N2: InnoDB errors that mean "another transaction got there first" - safe to re-run the
# whole transaction once. 1020 = MariaDB 11.6+ innodb_snapshot_isolation ("Record has changed
# since last read"), 1213 = deadlock. Neither has changed anything (the transaction rolled back).
RETRYABLE_ERRNOS = (1020, 1213)


def _errno(exc: BaseException) -> Optional[int]:
    args = getattr(exc, "args", ())
    return args[0] if args and isinstance(args[0], int) else None


def retry_on_conflict(fn):
    """Re-run a whole write transaction once on a serialization conflict (audit N2). Every
    decorated method opens its own transaction, so a retry starts from a clean slate."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except pymysql.err.MySQLError as exc:
            if _errno(exc) not in RETRYABLE_ERRNOS:
                raise
            log.warning("%s: transaction conflict (%s), retrying once", fn.__name__, _errno(exc))
            time.sleep(random.uniform(0.01, 0.1))
            return fn(*args, **kwargs)
    return wrapper


# Lock order for every write transaction (audit N2): float_balance row FIRST - as the very first
# statement, before any plain SELECT creates the InnoDB read view - then the receipts row, then
# edit_sessions. Taking the float lock first also serialises the duplicate check: the second of
# two simultaneous confirms of the same photo only builds its snapshot after the first committed.
LOCK_FLOAT = ("SELECT balance, last_low_alert_date, low_alert_claim_date, low_alert_claimed_at,"
              " low_alert_failed_date FROM float_balance WHERE id=1 FOR UPDATE")


# A receipt is voided when an adjustment references it (one at most, see adjust()).
_NOT_VOIDED = "NOT EXISTS (SELECT 1 FROM adjustments ad WHERE ad.receipt_id = receipts.id)"


class Database:
    def __init__(self, cfg: Config):
        self.cfg = cfg

    # ------------------------------------------------------------ plumbing
    def _connect(self) -> pymysql.connections.Connection:
        kw: Dict[str, Any] = dict(user=self.cfg.mysql_user, password=self.cfg.mysql_password,
                                  database=self.cfg.mysql_database, charset="utf8mb4",
                                  cursorclass=DictCursor, autocommit=False,
                                  connect_timeout=10, read_timeout=60, write_timeout=60)
        if self.cfg.mysql_unix_socket:
            kw["unix_socket"] = self.cfg.mysql_unix_socket
        else:
            kw["host"], kw["port"] = self.cfg.mysql_host, self.cfg.mysql_port
        last: Optional[Exception] = None
        for attempt in range(3):
            try:
                return pymysql.connect(**kw)
            except pymysql.err.OperationalError as exc:
                last = exc
                log.warning("MySQL connect failed (attempt %d/3): %s", attempt + 1, exc)
                time.sleep(1 + attempt * 2)
        assert last is not None
        raise last

    @contextmanager
    def transaction(self) -> Iterator[DictCursor]:
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                yield cur
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _audit(self, cur, now: datetime, event_type: str, actor: Optional[int], *,
               receipt_id: Optional[int] = None, topup_id: Optional[int] = None,
               adjustment_id: Optional[int] = None, amount: Optional[Decimal] = None, before: Optional[Decimal] = None,
               after: Optional[Decimal] = None, details: Optional[Dict[str, Any]] = None) -> None:
        cur.execute(
            "INSERT INTO audit_log (event_time, event_type, actor_telegram_id, receipt_id, topup_id,"
            " adjustment_id, amount, balance_before, balance_after, details)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (now, event_type, actor, receipt_id, topup_id, adjustment_id, amount, before, after,
             _json(details) if details is not None else None))

    # -------------------------------------------------------------- health
    def health_check(self) -> Dict[str, Any]:
        with self.transaction() as cur:
            cur.execute("SELECT balance, last_low_alert_date FROM float_balance WHERE id = 1")
            row = cur.fetchone()
        if row is None:
            raise RuntimeError("float_balance row id=1 missing - was migrations/001_initial.sql applied?")
        return row

    # ------------------------------------------------------------ receipts
    @retry_on_conflict
    def create_pending_receipt(self, submitted_by: int, photo_path: str, ext: Extraction, now: datetime,
                               file_id: Optional[str] = None, file_unique_id: Optional[str] = None) -> int:
        with self.transaction() as cur:
            dup = self._find_duplicate(cur, None, file_unique_id, ext.amount, ext.receipt_date, ext.merchant)
            cur.execute(
                "INSERT INTO receipts (status, submitted_by, photo_path, telegram_file_id,"
                " telegram_file_unique_id, amount, receipt_date, merchant, items, ocr_engine,"
                " ocr_confidence, ocr_escalation_reason, ocr_raw_text, vision_model,"
                " vision_raw_response, vision_error, duplicate_of, created_at, updated_at)"
                " VALUES ('pending',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (submitted_by, photo_path, file_id, file_unique_id, ext.amount, ext.receipt_date,
                 ext.merchant, ext.items, ext.engine, ext.confidence, ext.escalation_reason,
                 ext.raw_text, ext.vision_model, ext.vision_raw, ext.vision_error, dup, now, now))
            rid = cur.lastrowid
            self._audit(cur, now, "receipt_created", submitted_by, receipt_id=rid, amount=ext.amount,
                        details={"photo_path": photo_path, "ocr_engine": ext.engine,
                                 "ocr_confidence": ext.confidence,
                                 "escalation_reason": ext.escalation_reason,
                                 "vision_error": ext.vision_error, "duplicate_of": dup,
                                 "extracted": ext.fields()})
        return rid

    @staticmethod
    def _find_duplicate(cur, rid: Optional[int], file_unique_id: Optional[str], amount: Optional[Decimal],
                        receipt_date: Optional[date], merchant: Optional[str]) -> Optional[int]:
        """Audit M4: an earlier live receipt with the same Telegram photo, or a confirmed one with
        the same amount + receipt date (+ merchant when both are known). A receipt that was
        voided (/void) no longer counts - re-sending its photo is the normal way to redo it (N1)."""
        exclude = rid or 0
        if file_unique_id:
            # On upload, warn about any live copy; on confirm, only a *confirmed* copy blocks
            # (otherwise two pending copies would each block the other).
            live = "('pending','confirmed')" if rid is None else "('confirmed')"
            cur.execute("SELECT id FROM receipts WHERE telegram_file_unique_id=%s AND id<>%s"
                        f" AND status IN {live} AND {_NOT_VOIDED} ORDER BY id LIMIT 1", (file_unique_id, exclude))
            row = cur.fetchone()
            if row:
                return row["id"]
        if amount is not None and receipt_date is not None:
            cur.execute("SELECT id, merchant FROM receipts WHERE status='confirmed' AND amount=%s"
                        f" AND receipt_date=%s AND id<>%s AND {_NOT_VOIDED} ORDER BY id",
                        (amount, receipt_date, exclude))
            for row in cur.fetchall():
                a, b = (row["merchant"] or "").strip().lower(), (merchant or "").strip().lower()
                if not a or not b or a == b:
                    return row["id"]
        return None

    def get_receipt(self, rid: int) -> Optional[Dict[str, Any]]:
        with self.transaction() as cur:
            cur.execute("SELECT * FROM receipts WHERE id = %s", (rid,))
            return cur.fetchone()

    @retry_on_conflict
    def set_summary_message(self, rid: int, chat_id: int, message_id: int) -> None:
        with self.transaction() as cur:
            cur.execute("UPDATE receipts SET summary_chat_id=%s, summary_message_id=%s"
                        " WHERE id=%s AND status='pending'", (chat_id, message_id, rid))

    @retry_on_conflict
    def set_edit_field(self, rid: int, field: Optional[str], user_id: int, now: datetime) -> Optional[Dict[str, Any]]:
        """Mark which field `user_id` is about to type for receipt `rid` (None clears).
        Edit state is per user (edit_sessions), so the admin starting an edit no longer
        cancels the maid's half-finished one (audit L2)."""
        if field is not None and field not in EDITABLE_FIELDS:
            raise ValueError(f"not an editable field: {field}")
        with self.transaction() as cur:
            cur.execute("SELECT * FROM receipts WHERE id=%s FOR UPDATE", (rid,))
            r = cur.fetchone()
            if not r or r["status"] != "pending":
                return None
            if field:
                cur.execute("INSERT INTO edit_sessions (user_telegram_id, receipt_id, field, updated_at)"
                            " VALUES (%s,%s,%s,%s) ON DUPLICATE KEY UPDATE receipt_id=VALUES(receipt_id),"
                            " field=VALUES(field), updated_at=VALUES(updated_at)", (user_id, rid, field, now))
            else:
                cur.execute("DELETE FROM edit_sessions WHERE user_telegram_id=%s", (user_id,))
            r.update(edit_field=field, edit_requested_by=user_id if field else None)
            return r

    def get_awaiting_edit(self, user_id: int, now: Optional[datetime] = None) -> Optional[Dict[str, Any]]:
        """The receipt + field `user_id` is typing a new value for, or None.
        Audit N12: a session older than EDIT_SESSION_TTL is expired - it is deleted and returned
        with edit_expired=True so the caller treats the text normally (and can say so)."""
        with self.transaction() as cur:
            cur.execute("SELECT r.*, s.field AS edit_field, s.user_telegram_id AS edit_requested_by,"
                        " s.updated_at AS edit_started_at"
                        " FROM edit_sessions s JOIN receipts r ON r.id = s.receipt_id"
                        " WHERE s.user_telegram_id=%s AND r.status='pending'", (user_id,))
            row = cur.fetchone()
            if row is None:
                return None
            row["edit_expired"] = bool(now is not None and row["edit_started_at"] is not None
                                       and row["edit_started_at"] < now - EDIT_SESSION_TTL)
            if row["edit_expired"]:
                cur.execute("DELETE FROM edit_sessions WHERE user_telegram_id=%s AND updated_at=%s",
                            (user_id, row["edit_started_at"]))
            return row

    @retry_on_conflict
    def update_field(self, rid: int, field: str, value: Any, actor: int, now: datetime) -> Optional[Dict[str, Any]]:
        col = EDITABLE_FIELDS[field]  # KeyError for anything not whitelisted
        with self.transaction() as cur:
            cur.execute("SELECT * FROM receipts WHERE id=%s FOR UPDATE", (rid,))
            r = cur.fetchone()
            if not r or r["status"] != "pending":
                return None
            old = r[col]
            cur.execute(f"UPDATE receipts SET {col}=%s, updated_at=%s WHERE id=%s", (value, now, rid))
            cur.execute("DELETE FROM edit_sessions WHERE user_telegram_id=%s AND receipt_id=%s", (actor, rid))
            self._audit(cur, now, "receipt_edited", actor, receipt_id=rid,
                        amount=value if field == "amount" else r["amount"],
                        details={"field": field, "old": old, "new": value})
            cur.execute("SELECT * FROM receipts WHERE id=%s", (rid,))
            return cur.fetchone()

    @staticmethod
    def _claim_low_alert(fb: Dict[str, Any], balance: Decimal, now: datetime, low_threshold: Decimal,
                         sweep: bool = False) -> bool:
        """Decide (under the float_balance row lock) whether this caller gets today's alert.
        sweep=True (the hourly background check, audit N4) only re-sends an alert that is known
        to be undelivered: a failed send (low_alert_failed_date) or a claim whose sender died
        (low_alert_claim_date still set after the lease). A plain new day with a still-low float
        is NOT a reason to alert - the next receipt that keeps it low does that, in waking hours."""
        today = now.date()
        last: Optional[date] = fb["last_low_alert_date"]
        if balance > low_threshold or (last is not None and last >= today):
            return False
        claimed_at = fb.get("low_alert_claimed_at")
        if claimed_at and claimed_at > now - ALERT_CLAIM_LEASE:
            return False  # someone else is sending it right now
        if sweep and fb.get("low_alert_failed_date") is None and fb.get("low_alert_claim_date") is None:
            return False
        return True

    @staticmethod
    def _claim_token(now: datetime) -> datetime:
        # Stored in a DATETIME (seconds) column and compared back by finish_low_alert (audit N6);
        # MySQL rounds and MariaDB truncates fractions, so drop them ourselves.
        return now.replace(microsecond=0)

    @retry_on_conflict
    def confirm_receipt(self, rid: int, actor: int, now: datetime, low_threshold: Decimal,
                        allow_duplicate: bool = False) -> ConfirmResult:
        with self.transaction() as cur:
            # Audit N2: float lock is the FIRST statement (see LOCK_FLOAT); only then the receipt
            # row, and only then the plain-SELECT duplicate check.
            cur.execute(LOCK_FLOAT)
            fb = cur.fetchone()
            if fb is None:
                raise RuntimeError("float_balance row missing")
            cur.execute("SELECT * FROM receipts WHERE id=%s FOR UPDATE", (rid,))
            r = cur.fetchone()
            if not r:
                return ConfirmResult("not_found")
            if r["status"] == "confirmed":
                return ConfirmResult("already_confirmed", r, r["balance_before"], r["balance_after"])
            if r["status"] == "cancelled":
                return ConfirmResult("cancelled", r)
            if r["amount"] is None or r["amount"] <= 0:
                return ConfirmResult("missing_amount", r)
            if not allow_duplicate:
                dup = self._find_duplicate(cur, rid, r["telegram_file_unique_id"], r["amount"],
                                           r["receipt_date"], r["merchant"])
                if dup is not None:
                    return ConfirmResult("possible_duplicate", r, duplicate_of=dup)

            before: Decimal = fb["balance"]
            after: Decimal = before - r["amount"]  # negative allowed, never blocked
            # Audit H2: only *claim* the alert here. last_low_alert_date is written by
            # finish_low_alert() after Telegram accepted the message.
            alert = self._claim_low_alert(fb, after, now, low_threshold)
            token = self._claim_token(now) if alert else None
            if alert:
                cur.execute("UPDATE float_balance SET balance=%s, low_alert_claim_date=%s,"
                            " low_alert_claimed_at=%s, updated_at=%s WHERE id=1", (after, token.date(), token, now))
            else:
                cur.execute("UPDATE float_balance SET balance=%s, updated_at=%s WHERE id=1", (after, now))
            cur.execute("UPDATE receipts SET status='confirmed', confirmed_at=%s, confirmed_by=%s,"
                        " balance_before=%s, balance_after=%s, updated_at=%s WHERE id=%s",
                        (now, actor, before, after, now, rid))
            cur.execute("DELETE FROM edit_sessions WHERE receipt_id=%s", (rid,))
            self._audit(cur, now, "receipt_confirmed", actor, receipt_id=rid, amount=r["amount"],
                        before=before, after=after,
                        details={"merchant": r["merchant"], "receipt_date": r["receipt_date"],
                                 "items": r["items"], "ocr_engine": r["ocr_engine"],
                                 "duplicate_acknowledged": bool(allow_duplicate)})
            r.update(status="confirmed", confirmed_at=now, confirmed_by=actor,
                     balance_before=before, balance_after=after)
        return ConfirmResult("confirmed", r, before, after, alert, alert_claim=token)

    @retry_on_conflict
    def claim_low_alert(self, now: datetime, low_threshold: Decimal) -> Optional[AlertClaim]:
        """Periodic sweep: re-claim a low-float alert that is known to be undelivered (failed
        send, or a sender that crashed holding the claim) - audit H2/N4. Returns the claim, or
        None if nothing is owed / someone else is sending."""
        with self.transaction() as cur:
            cur.execute(LOCK_FLOAT)
            fb = cur.fetchone()
            if fb is None:
                return None
            if fb["balance"] > low_threshold and fb.get("low_alert_failed_date") is not None:
                # topped up since the failure: nothing is owed any more
                cur.execute("UPDATE float_balance SET low_alert_failed_date=NULL WHERE id=1")
                return None
            if not self._claim_low_alert(fb, fb["balance"], now, low_threshold, sweep=True):
                return None
            token = self._claim_token(now)
            cur.execute("UPDATE float_balance SET low_alert_claim_date=%s, low_alert_claimed_at=%s WHERE id=1",
                        (token.date(), token))
            return AlertClaim(fb["balance"], token)

    @retry_on_conflict
    def finish_low_alert(self, now: datetime, sent: bool, balance: Decimal, low_threshold: Decimal,
                         receipt_id: Optional[int] = None, error: Optional[str] = None,
                         attempts: int = 1, claimed_at: Optional[datetime] = None) -> str:
        """Release the alert claim. Only a successful send consumes the once-per-HKT-day cap;
        a failed send leaves the cap open and marks the alert as owed, so the next receipt or the
        sweep retries (audit H2/N4).

        Audit N5: the day that is consumed is the day the alert was CLAIMED, not the day the
        retries finished (a 23:58 claim delivered at 00:01 counts for the 23:58 day).
        Audit N6: with claimed_at, only the owner of that claim releases it; if the lease expired
        and another worker re-claimed meanwhile, its claim is left alone.
        Returns 'owner' or 'not_owner' (for tests/logging)."""
        with self.transaction() as cur:
            cur.execute(LOCK_FLOAT)
            fb = cur.fetchone() or {}
            if claimed_at is not None:
                token = self._claim_token(claimed_at)
                owner = fb.get("low_alert_claimed_at") == token
                alert_day = token.date()
            else:  # legacy callers: whatever claim is recorded
                owner = True
                alert_day = fb.get("low_alert_claim_date") or now.date()
            last = fb.get("last_low_alert_date")
            details = {"threshold": low_threshold, "alert_date": alert_day, "notify": self.cfg.admin_id,
                       "attempts": attempts, "claim_owner": owner}
            if sent:
                new_last = alert_day if last is None or alert_day > last else last
                if owner:
                    cur.execute("UPDATE float_balance SET last_low_alert_date=%s, low_alert_claim_date=NULL,"
                                " low_alert_claimed_at=NULL, low_alert_failed_date=NULL WHERE id=1", (new_last,))
                else:  # the message did go out - record it, but don't touch the other worker's claim
                    cur.execute("UPDATE float_balance SET last_low_alert_date=%s, low_alert_failed_date=NULL"
                                " WHERE id=1", (new_last,))
                self._audit(cur, now, "low_float_alert", None, receipt_id=receipt_id, after=balance,
                            details={"status": "sent", **details})
            else:
                if owner:
                    cur.execute("UPDATE float_balance SET low_alert_claim_date=NULL, low_alert_claimed_at=NULL,"
                                " low_alert_failed_date=%s WHERE id=1", (alert_day,))
                self._audit(cur, now, "low_float_alert_failed", None, receipt_id=receipt_id, after=balance,
                            details={"status": "failed", **details, "error": (error or "")[:300]})
        return "owner" if owner else "not_owner"

    @retry_on_conflict
    def cancel_receipt(self, rid: int, actor: int, now: datetime) -> str:
        with self.transaction() as cur:
            cur.execute("SELECT * FROM receipts WHERE id=%s FOR UPDATE", (rid,))
            r = cur.fetchone()
            if not r:
                return "not_found"
            if r["status"] != "pending":
                return r["status"]
            cur.execute("UPDATE receipts SET status='cancelled', cancelled_at=%s, cancelled_by=%s,"
                        " updated_at=%s WHERE id=%s", (now, actor, now, rid))
            cur.execute("DELETE FROM edit_sessions WHERE receipt_id=%s", (rid,))
            self._audit(cur, now, "receipt_cancelled", actor, receipt_id=rid, amount=r["amount"],
                        details={"merchant": r["merchant"]})
        return "cancelled_now"

    # -------------------------------------------------------------- float
    @retry_on_conflict
    def topup(self, amount: Decimal, actor: int, raw_message: str, now: datetime) -> TopupResult:
        if amount is None or amount <= 0:
            raise ValueError("top-up amount must be positive")
        with self.transaction() as cur:
            cur.execute(LOCK_FLOAT)
            fb = cur.fetchone()
            if fb is None:
                raise RuntimeError("float_balance row missing")
            before: Decimal = fb["balance"]
            after = before + amount
            cur.execute("UPDATE float_balance SET balance=%s, updated_at=%s WHERE id=1", (after, now))
            cur.execute("INSERT INTO topups (amount, admin_telegram_id, raw_message, balance_before,"
                        " balance_after, created_at) VALUES (%s,%s,%s,%s,%s,%s)",
                        (amount, actor, (raw_message or "")[:255], before, after, now))
            tid = cur.lastrowid
            self._audit(cur, now, "topup", actor, topup_id=tid, amount=amount, before=before, after=after,
                        details={"raw_message": (raw_message or "")[:255]})
        return TopupResult(tid, amount, before, after)

    @retry_on_conflict
    def adjust(self, amount: Optional[Decimal], actor: int, reason: str, now: datetime,
               receipt_id: Optional[int] = None) -> AdjustResult:
        """Admin correction (audit L11). Confirmed receipts are immutable, so mistakes are fixed
        with a signed, append-only adjustment. With receipt_id this voids that receipt: the
        amount is ignored and the receipt's amount is put back into the float, once.
        Audit N10: a plain adjustment may not push the balance beyond +/-ADJUST_BALANCE_LIMIT
        (moving back towards zero is always allowed)."""
        with self.transaction() as cur:
            # Audit N2b: float lock first, before the plain SELECT on adjustments.
            cur.execute(LOCK_FLOAT)
            fb = cur.fetchone()
            if fb is None:
                raise RuntimeError("float_balance row missing")
            if receipt_id is not None:
                cur.execute("SELECT id, status, amount FROM receipts WHERE id=%s FOR UPDATE", (receipt_id,))
                r = cur.fetchone()
                if not r:
                    return AdjustResult("not_found")
                if r["status"] != "confirmed":
                    return AdjustResult("not_confirmed")
                cur.execute("SELECT id FROM adjustments WHERE receipt_id=%s LIMIT 1", (receipt_id,))
                if cur.fetchone():
                    return AdjustResult("already_voided")
                amount = r["amount"]
            if amount is None or amount == 0:
                raise ValueError("adjustment amount must be non-zero")
            before: Decimal = fb["balance"]
            after = before + amount
            if receipt_id is None and abs(after) > ADJUST_BALANCE_LIMIT and abs(after) > abs(before):
                return AdjustResult("out_of_range", amount=amount, balance_before=before, balance_after=after)
            cur.execute("UPDATE float_balance SET balance=%s, updated_at=%s WHERE id=1", (after, now))
            cur.execute("INSERT INTO adjustments (amount, receipt_id, reason, admin_telegram_id, balance_before,"
                        " balance_after, created_at) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                        (amount, receipt_id, (reason or "")[:255] or "-", actor, before, after, now))
            aid = cur.lastrowid
            self._audit(cur, now, "adjustment", actor, receipt_id=receipt_id, adjustment_id=aid, amount=amount,
                        before=before, after=after, details={"reason": (reason or "")[:255]})
        return AdjustResult("adjusted", aid, amount, before, after)

    def get_balance(self) -> Dict[str, Any]:
        return self.health_check()

    # ------------------------------------------------------- reconciliation
    def reconcile(self) -> Dict[str, Any]:
        """Audit M5: cross-check the running float against the ledger tables and the audit log.
        Catches a direct `UPDATE float_balance`, forged/missing audit rows, and receipts whose
        confirmed amount no longer matches what was audited. Read-only."""
        q = lambda cur, sql: (cur.execute(sql), cur.fetchone())[1]  # noqa: E731
        z = Decimal("0.00")
        with self.transaction() as cur:
            bal = q(cur, "SELECT balance FROM float_balance WHERE id=1")
            balance = bal["balance"] if bal else None
            t = q(cur, "SELECT COALESCE(SUM(amount),0) s, COUNT(*) n FROM topups")
            r = q(cur, "SELECT COALESCE(SUM(amount),0) s, COUNT(*) n FROM receipts WHERE status='confirmed'")
            a = q(cur, "SELECT COALESCE(SUM(amount),0) s, COUNT(*) n FROM adjustments")
            at = q(cur, "SELECT COALESCE(SUM(amount),0) s, COUNT(*) n FROM audit_log WHERE event_type='topup'")
            ar = q(cur, "SELECT COALESCE(SUM(amount),0) s, COUNT(*) n FROM audit_log"
                        " WHERE event_type='receipt_confirmed'")
            aa = q(cur, "SELECT COALESCE(SUM(amount),0) s, COUNT(*) n FROM audit_log WHERE event_type='adjustment'")
            orphan_t = q(cur, "SELECT COUNT(*) n FROM audit_log a LEFT JOIN topups t ON t.id=a.topup_id"
                              " WHERE a.event_type='topup' AND (t.id IS NULL OR t.amount<>a.amount)")["n"]
            cur.execute("SELECT r.id FROM receipts r LEFT JOIN audit_log a ON a.receipt_id=r.id"
                        " AND a.event_type='receipt_confirmed' WHERE r.status='confirmed'"
                        " AND (a.id IS NULL OR a.amount<>r.amount) ORDER BY r.id LIMIT 20")
            bad_receipts = [row["id"] for row in cur.fetchall()]
            cur.execute("SELECT a.id FROM audit_log a LEFT JOIN receipts r ON r.id=a.receipt_id"
                        " WHERE a.event_type='receipt_confirmed' AND (r.id IS NULL OR r.status<>'confirmed')"
                        " ORDER BY a.id LIMIT 20")
            forged_confirms = [row["id"] for row in cur.fetchall()]
        expected = (t["s"] or z) - (r["s"] or z) + (a["s"] or z)
        problems: List[str] = []
        if balance is None:
            problems.append("float_balance row missing")
        elif balance != expected:
            problems.append(f"float balance {balance} != top-ups {t['s']} - confirmed receipts {r['s']}"
                            f" + adjustments {a['s']} = {expected}")
        if (at["s"], at["n"]) != (t["s"], t["n"]) or orphan_t:
            problems.append(f"audit top-ups ({at['n']} rows, {at['s']}) != topups table ({t['n']} rows, {t['s']})")
        if (ar["s"], ar["n"]) != (r["s"], r["n"]):
            problems.append(f"audit confirmations ({ar['n']} rows, {ar['s']}) != confirmed receipts"
                            f" ({r['n']} rows, {r['s']})")
        if (aa["s"], aa["n"]) != (a["s"], a["n"]):
            problems.append(f"audit adjustments ({aa['n']} rows, {aa['s']}) != adjustments table"
                            f" ({a['n']} rows, {a['s']})")
        if bad_receipts:
            problems.append("confirmed receipts without a matching audit row: "
                            + ", ".join(f"#{i}" for i in bad_receipts))
        if forged_confirms:
            problems.append("audit confirmations for receipts that are not confirmed: audit ids "
                            + ", ".join(str(i) for i in forged_confirms))
        return {"ok": not problems, "balance": balance, "expected": expected, "topups": t["s"],
                "receipts": r["s"], "adjustments": a["s"], "problems": problems}

    # ------------------------------------------------------------ reports
    def history(self, n: int = 10) -> List[Dict[str, Any]]:
        with self.transaction() as cur:
            cur.execute(
                "SELECT a.id, a.event_time, a.event_type, a.amount, a.balance_after, a.receipt_id,"
                " a.topup_id, r.merchant, r.receipt_date FROM audit_log a"
                " LEFT JOIN receipts r ON r.id = a.receipt_id"
                " WHERE a.event_type IN ('receipt_confirmed','topup','adjustment')"
                " ORDER BY a.id DESC LIMIT %s", (int(n),))
            return list(cur.fetchall())

    def pending_receipts(self) -> List[Dict[str, Any]]:
        with self.transaction() as cur:
            cur.execute("SELECT id, amount, merchant, created_at FROM receipts WHERE status='pending' ORDER BY id")
            return list(cur.fetchall())

    def confirmed_receipts_for_month(self, year: int, month: int) -> List[Dict[str, Any]]:
        start = datetime(year, month, 1)
        end = datetime(year + (month == 12), month % 12 + 1, 1)
        with self.transaction() as cur:
            # Audit N1: voided receipts are listed (marked) but are not money spent.
            cur.execute(
                "SELECT r.id, r.confirmed_at, r.receipt_date, r.merchant, r.items, r.amount, r.currency,"
                " r.balance_before, r.balance_after, r.ocr_engine, r.ocr_confidence, r.submitted_by,"
                " r.confirmed_by, r.photo_path, ad.id AS void_adjustment_id, ad.created_at AS voided_at,"
                " ad.reason AS void_reason"
                " FROM receipts r LEFT JOIN adjustments ad ON ad.receipt_id = r.id"
                " WHERE r.status='confirmed' AND r.confirmed_at >= %s AND r.confirmed_at < %s"
                " ORDER BY r.confirmed_at, r.id", (start, end))
            return list(cur.fetchall())

    # ------------------------------------------------------------- schema
    def schema_version(self) -> Optional[int]:
        """Audit N11: MAX(version) from schema_version, or None if the table is missing."""
        try:
            with self.transaction() as cur:
                cur.execute("SELECT MAX(version) v FROM schema_version")
                row = cur.fetchone()
        except pymysql.err.ProgrammingError as exc:
            if _errno(exc) == 1146:  # table doesn't exist
                return None
            raise
        return row["v"] if row else None
