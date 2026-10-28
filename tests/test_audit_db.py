"""Regression tests for the audit's database probes (tests/test_zz_audit_probes.py P01-P09).
Run against a real MariaDB/MySQL as the least-privilege app user. Each asserts the FIXED behaviour."""
import concurrent.futures as cf
import threading
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D
from unittest import mock

import pymysql
import pytest

from maid_receipt_bot import db as dbmod
from maid_receipt_bot.config import Config, ConfigError
from maid_receipt_bot.handlers import _sane_amount
from maid_receipt_bot.ocr import Extraction
from tests.test_db_integration import (ADMIN_ID, APP_PW, APP_USER, DBNAME, HOST, MAID_ID, MYSQL_OK, NOW, PORT,  # noqa
                                       THR, _admin_conn, audit, db, new_receipt)

pytestmark = [pytest.mark.mysql, pytest.mark.skipif(not MYSQL_OK, reason="test MySQL not reachable")]


def app_conn():
    return pymysql.connect(host=HOST, port=PORT, user=APP_USER, password=APP_PW, database=DBNAME, autocommit=True)


def try_sql(sql):
    c = app_conn()
    try:
        with c.cursor() as cur:
            cur.execute(sql)
        return "ALLOWED"
    except pymysql.err.MySQLError as e:
        return f"blocked ({e.args[0]})"
    finally:
        c.close()


# ------------------------------------------------------------------ M1 (P01)
def test_P01_M1_huge_amount_is_sanitised_before_insert(db):
    ext = _sane_amount(Extraction(amount=D("99999999999.99"), engine="tesseract", confidence=95.0))
    assert ext.amount is None and "out of range" in ext.escalation_reason
    rid = db.create_pending_receipt(MAID_ID, "/x.jpg", ext, NOW, "fid", "u1")
    assert db.get_receipt(rid)["amount"] is None
    # the DB itself still refuses an unsanitised overflow, and leaves nothing half-written
    with pytest.raises(pymysql.err.MySQLError):
        db.create_pending_receipt(MAID_ID, "/y.jpg", Extraction(amount=D("99999999999.99")), NOW, "fid", "u2")
    assert len(db.pending_receipts()) == 1


# ------------------------------------------------------------------ M5 (P02, P03, P04)
def test_P02_M5_ledger_rows_cannot_be_rewritten_by_the_app_user(db):
    db.topup(D("100"), ADMIN_ID, "top up 100", NOW)
    rid = new_receipt(db, "10")
    db.confirm_receipt(rid, MAID_ID, NOW, THR)
    db.adjust(D("-1"), ADMIN_ID, "test", NOW)
    blocked = {
        "truncate_audit": "TRUNCATE audit_log",
        "truncate_receipts": "TRUNCATE receipts",
        "truncate_adjustments": "TRUNCATE adjustments",
        "drop_trigger": "DROP TRIGGER audit_log_no_update",
        "drop_receipts_trigger": "DROP TRIGGER receipts_final_no_update",
        "delete_receipts": "DELETE FROM receipts",
        "delete_topups": "DELETE FROM topups",
        "delete_adjustments": "DELETE FROM adjustments",
        "update_audit": "UPDATE audit_log SET amount=0",
        "update_topups": "UPDATE topups SET amount=1",
        "update_adjustments": "UPDATE adjustments SET amount=5",
        "update_confirmed_receipt": f"UPDATE receipts SET amount=1.00, balance_after=999 WHERE id={rid}",
        "unconfirm_receipt": f"UPDATE receipts SET status='pending' WHERE id={rid}",
        "alter_table": "ALTER TABLE receipts ADD COLUMN x INT",
    }
    out = {k: try_sql(v) for k, v in blocked.items()}
    assert all(v.startswith("blocked") for v in out.values()), out
    r = db.get_receipt(rid)
    assert r["status"] == "confirmed" and r["amount"] == D("10.00")


def test_P02_M5_float_tampering_and_forged_audit_rows_are_detected_by_reconcile(db):
    db.topup(D("100"), ADMIN_ID, "top up 100", NOW)
    db.confirm_receipt(new_receipt(db, "10"), MAID_ID, NOW, THR)
    assert db.reconcile()["ok"]
    # still allowed by grants (the bot must UPDATE the float / INSERT audit rows) ...
    assert try_sql("UPDATE float_balance SET balance=12345.67") == "ALLOWED"
    rec = db.reconcile()
    assert not rec["ok"] and any("float balance" in p for p in rec["problems"])
    assert try_sql("UPDATE float_balance SET balance=90.00") == "ALLOWED"
    assert db.reconcile()["ok"]
    # ... but a forged audit row no longer matches the ledger tables
    assert try_sql("INSERT INTO audit_log (event_time,event_type,actor_telegram_id,amount)"
                   " VALUES (NOW(),'topup',1001,5000)") == "ALLOWED"
    rec = db.reconcile()
    assert not rec["ok"] and any("audit top-ups" in p for p in rec["problems"])


def test_P03_M5_confirmed_receipt_cannot_be_reconfirmed(db):
    db.topup(D("100"), ADMIN_ID, "t", NOW)
    rid = new_receipt(db, "10")
    db.confirm_receipt(rid, MAID_ID, NOW, THR)
    assert try_sql(f"UPDATE receipts SET status='pending' WHERE id={rid}").startswith("blocked")
    assert db.confirm_receipt(rid, MAID_ID, NOW, THR).status == "already_confirmed"
    assert db.get_balance()["balance"] == D("90.00")


def test_P03_M5_cancelled_receipt_is_final_and_pending_edits_still_work(db):
    rid = new_receipt(db, "10")
    assert db.update_field(rid, "amount", D("12.00"), MAID_ID, NOW)["amount"] == D("12.00")
    db.cancel_receipt(rid, MAID_ID, NOW)
    assert try_sql(f"UPDATE receipts SET status='pending' WHERE id={rid}").startswith("blocked")
    assert db.update_field(rid, "amount", D("13.00"), MAID_ID, NOW) is None


def test_P04_M5_truncate_only_possible_with_dba_rights(db):
    """Deferred part of M5: a DBA-level account can TRUNCATE (triggers don't fire). We can only
    make sure the bot's own account can't; DEPLOY.md documents the residual risk + off-host backups."""
    for t in ("audit_log", "receipts", "topups", "adjustments", "float_balance"):
        assert try_sql(f"TRUNCATE {t}").startswith("blocked"), t


def test_L11_void_and_adjust_are_append_only_and_reconcile(db):
    db.topup(D("100"), ADMIN_ID, "t", NOW)
    rid = new_receipt(db, "40")
    db.confirm_receipt(rid, MAID_ID, NOW, THR)
    res = db.adjust(None, ADMIN_ID, "entered twice", NOW, rid)
    assert res.status == "adjusted" and res.amount == D("40.00") and res.balance_after == D("100.00")
    assert db.adjust(None, ADMIN_ID, "again", NOW, rid).status == "already_voided"
    assert db.adjust(None, ADMIN_ID, "x", NOW, new_receipt(db, "5")).status == "not_confirmed"
    assert db.adjust(None, ADMIN_ID, "x", NOW, 999999).status == "not_found"
    assert db.adjust(D("-2.50"), ADMIN_ID, "cash count", NOW).balance_after == D("97.50")
    with pytest.raises(ValueError):
        db.adjust(D("0"), ADMIN_ID, "zero", NOW)
    rec = db.reconcile()
    assert rec["ok"], rec
    assert rec["adjustments"] == D("37.50")
    kinds = [a["event_type"] for a in audit(db)]
    assert kinds.count("adjustment") == 2 and "topup" in kinds  # corrections are not logged as top-ups


# ------------------------------------------------------------------ races (P05, P06) stay safe
def test_P05_double_tap_race(db):
    db.topup(D("100000"), ADMIN_ID, "t", NOW)
    rounds = 40
    for _ in range(rounds):
        rid = new_receipt(db, "1.10", receipt_date=None)
        b = threading.Barrier(3)

        def go():
            b.wait()
            return db.confirm_receipt(rid, MAID_ID, NOW, THR).status
        with cf.ThreadPoolExecutor(3) as ex:
            st = [f.result() for f in [ex.submit(go) for _ in range(3)]]
        assert st.count("confirmed") == 1, st
    assert db.get_balance()["balance"] == D("100000") - rounds * D("1.10")
    assert sum(1 for a in audit(db) if a["event_type"] == "receipt_confirmed") == rounds
    assert db.reconcile()["ok"]


def test_P06_confirm_vs_cancel_vs_edit_race(db):
    db.topup(D("10000"), ADMIN_ID, "t", NOW)
    for _ in range(40):
        rid = new_receipt(db, "5.00", receipt_date=None)
        b = threading.Barrier(3)

        def c():
            b.wait()
            return db.confirm_receipt(rid, MAID_ID, NOW, THR).status

        def x():
            b.wait()
            return db.cancel_receipt(rid, MAID_ID, NOW)

        def e():
            b.wait()
            return "edited" if db.update_field(rid, "amount", D("7.00"), ADMIN_ID, NOW) else "edit_rejected"
        with cf.ThreadPoolExecutor(3) as ex:
            res = tuple(f.result() for f in [ex.submit(c), ex.submit(x), ex.submit(e)])
        r = db.get_receipt(rid)
        assert not (r["status"] == "confirmed" and res[1] == "cancelled_now")
        assert (res[0] == "confirmed") == (r["status"] == "confirmed")
    assert db.reconcile()["ok"]


# ------------------------------------------------------------------ H2 / P07
def test_P07_H2_alert_boundary_rollover_and_delivery_based_cap(db):
    cfg = db.cfg
    db.topup(D("200.01"), ADMIN_ID, "t", NOW)
    assert db.confirm_receipt(new_receipt(db, "100.00"), MAID_ID, NOW, THR).low_float_alert is False  # 100.01
    res = db.confirm_receipt(new_receipt(db, "0.01"), MAID_ID, NOW, THR)                             # exactly 100
    assert res.low_float_alert is True
    db.finish_low_alert(NOW, True, res.balance_after, THR, res.receipt["id"])

    class FakeDT(datetime):
        _utc = None

        @classmethod
        def now(cls, tz=None):
            return cls._utc.astimezone(tz)
    import maid_receipt_bot.config as cmod
    with mock.patch.object(cmod, "datetime", FakeDT):
        FakeDT._utc = datetime(2026, 10, 27, 15, 59, 59, tzinfo=timezone.utc)
        t1 = cfg.now()
        assert db.confirm_receipt(new_receipt(db, "1"), MAID_ID, t1, THR).low_float_alert is False
        FakeDT._utc = datetime(2026, 10, 27, 16, 0, 0, tzinfo=timezone.utc)
        t2 = cfg.now()
        assert t2 == datetime(2026, 10, 28, 0, 0, 0)
        r2 = db.confirm_receipt(new_receipt(db, "2"), MAID_ID, t2, THR)
        assert r2.low_float_alert is True
        db.finish_low_alert(t2, True, r2.balance_after, THR)
        db.topup(D("500"), ADMIN_ID, "t", t2)
        assert db.confirm_receipt(new_receipt(db, "450"), MAID_ID, t2 + timedelta(hours=5), THR).low_float_alert is False


def test_H2_failed_alert_does_not_consume_the_daily_cap(db):
    db.topup(D("120"), ADMIN_ID, "t", NOW)
    res = db.confirm_receipt(new_receipt(db, "45.50"), MAID_ID, NOW, THR)
    assert res.low_float_alert is True
    assert db.get_balance()["last_low_alert_date"] is None
    db.finish_low_alert(NOW, False, res.balance_after, THR, res.receipt["id"], "NetworkError: timed out", 4)
    b = db.get_balance()
    assert b["last_low_alert_date"] is None
    c = _admin_conn()
    with c.cursor() as cur:
        cur.execute("SELECT low_alert_claim_date, low_alert_claimed_at FROM float_balance WHERE id=1")
        assert cur.fetchone() == (None, None)
    c.close()
    kinds = [a["event_type"] for a in audit(db)]
    assert "low_float_alert_failed" in kinds and "low_float_alert" not in kinds
    # the next receipt (or the hourly sweep) gets the alert again
    assert db.confirm_receipt(new_receipt(db, "1.00"), MAID_ID, NOW, THR).low_float_alert is True


def test_H2_claim_lease_blocks_parallel_senders_and_expires(db):
    """Updated for audit N4/N6: the sweep only re-claims an alert that is owed (failed or
    abandoned), and returns an AlertClaim carrying the claim token."""
    db.topup(D("150"), ADMIN_ID, "t", NOW)
    res = db.confirm_receipt(new_receipt(db, "100.00"), MAID_ID, NOW, THR)   # 50 left -> alert claimed
    assert res.low_float_alert and res.alert_claim is not None
    assert db.claim_low_alert(NOW + timedelta(minutes=1), THR) is None       # the confirmer is sending
    later = NOW + dbmod.ALERT_CLAIM_LEASE + timedelta(seconds=1)
    c = db.claim_low_alert(later, THR)                                          # sender crashed -> retake
    assert c is not None and c.balance == D("50.00") and c.claimed_at == later
    assert db.finish_low_alert(later, True, D("50.00"), THR, claimed_at=c.claimed_at) == "owner"
    assert db.claim_low_alert(later + timedelta(hours=1), THR) is None           # delivered today
    # N4: a new HKT day with an unchanged low balance is NOT a reason for the sweep to nag again
    assert db.claim_low_alert(datetime(2026, 10, 28, 0, 0, 1), THR) is None


def test_H2_no_claim_when_balance_is_fine(db):
    db.topup(D("500"), ADMIN_ID, "t", NOW)
    assert db.claim_low_alert(NOW, THR) is None


# ------------------------------------------------------------------ M4 (P08)
def test_P08_M4_same_photo_twice_needs_explicit_confirm(db):
    db.topup(D("500"), ADMIN_ID, "t", NOW)
    a = new_receipt(db, "45.50", file_unique_id="SAMEUNIQ")
    b = new_receipt(db, "45.50", file_unique_id="SAMEUNIQ")
    assert db.get_receipt(b)["duplicate_of"] == a
    assert db.confirm_receipt(a, MAID_ID, NOW, THR).status == "confirmed"
    res = db.confirm_receipt(b, MAID_ID, NOW, THR)
    assert res.status == "possible_duplicate" and res.duplicate_of == a
    assert db.get_balance()["balance"] == D("454.50")
    assert db.confirm_receipt(b, MAID_ID, NOW, THR, allow_duplicate=True).status == "confirmed"
    assert db.get_balance()["balance"] == D("409.00")
    confirms = [a_ for a_ in audit(db) if a_["event_type"] == "receipt_confirmed"]
    assert '"duplicate_acknowledged": true' in confirms[-1]["details"]


def test_M4_two_pending_copies_do_not_block_each_other(db):
    db.topup(D("500"), ADMIN_ID, "t", NOW)
    a = new_receipt(db, "45.50", file_unique_id="SAME2")
    b = new_receipt(db, "45.50", file_unique_id="SAME2")
    db.cancel_receipt(a, MAID_ID, NOW)
    assert db.confirm_receipt(b, MAID_ID, NOW, THR).status == "confirmed"


def test_M4_same_amount_and_date_different_photo_is_flagged(db):
    db.topup(D("500"), ADMIN_ID, "t", NOW)
    a = new_receipt(db, "88.00", merchant="Wellcome")
    db.confirm_receipt(a, MAID_ID, NOW, THR)
    assert db.confirm_receipt(new_receipt(db, "88.00", merchant="Wellcome"), MAID_ID, NOW, THR).status \
        == "possible_duplicate"
    # different merchant, same amount/date -> a genuinely separate purchase
    assert db.confirm_receipt(new_receipt(db, "88.00", merchant="ParknShop"), MAID_ID, NOW, THR).status \
        == "confirmed"
    # different date -> fine
    assert db.confirm_receipt(new_receipt(db, "88.00", receipt_date=date(2026, 10, 20)), MAID_ID, NOW, THR).status \
        == "confirmed"


# ------------------------------------------------------------------ L5 (P09)
BASE = {"MYSQL_USER": "u", "MYSQL_PASSWORD": "p_long_enough_pw", "MYSQL_DATABASE": "d", "ADMIN_TELEGRAM_ID": "1",
        "MAID_TELEGRAM_ID": "2"}


def test_P09_L5_tz_env_no_longer_changes_business_time():
    assert Config.from_env({**BASE, "TZ": "UTC"}, require_token=False).tz_name == "Asia/Hong_Kong"
    assert Config.from_env({**BASE, "TZ": ":/etc/localtime"}, require_token=False).tz_name == "Asia/Hong_Kong"
    assert Config.from_env({**BASE, "BOT_TIMEZONE": "Europe/London", "TZ": "UTC"},
                           require_token=False).tz_name == "Europe/London"
    with pytest.raises(ConfigError):
        Config.from_env({**BASE, "BOT_TIMEZONE": "Not/AZone"}, require_token=False)


# ------------------------------------------------------------------ L8
def test_L8_app_user_exists_for_tcp_127_0_0_1():
    app_conn().close()  # connects over TCP 127.0.0.1 as the dedicated @'127.0.0.1' account


def test_L8_production_db_grants_are_least_privilege():
    """bot-deploy reads deploy/db-grants; socket auth has no CREATE USER / password here."""
    from pathlib import Path
    text = (Path(__file__).resolve().parents[1] / "deploy" / "db-grants").read_text()
    lines = []
    for ln in text.splitlines():
        ln = ln.split("#", 1)[0].strip()
        if not ln:
            continue
        tbl, privs = ln.split(None, 1)
        lines.append((tbl, {p.strip() for p in privs.split(",")}))
    by = dict(lines)
    assert by["receipts"] == {"SELECT", "INSERT", "UPDATE"}
    assert by["float_balance"] == {"SELECT", "UPDATE"}
    assert by["topups"] == {"SELECT", "INSERT"}
    assert by["adjustments"] == {"SELECT", "INSERT"}
    assert by["audit_log"] == {"SELECT", "INSERT"}
    assert by["edit_sessions"] == {"SELECT", "INSERT", "UPDATE", "DELETE"}
    assert by["schema_version"] == {"SELECT"}
    assert "DELETE" not in by["receipts"] and "DELETE" not in by["audit_log"]
    assert not any(w in text.upper() for w in ("DROP", "ALTER", "TRIGGER", "SUPER", "CREATE USER", "GRANT OPTION"))
