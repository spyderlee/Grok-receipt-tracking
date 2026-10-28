"""Integration tests against a REAL MySQL/MariaDB (see tests/setup_test_db.sh).

Schema is loaded with the admin test user; ledger operations run as an app user
holding exactly the production least-privilege grants from deploy/db-grants.
"""
import os
import re
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pymysql
import pytest

from maid_receipt_bot.config import Config
from maid_receipt_bot.db import Database
from maid_receipt_bot.ocr import Extraction
from maid_receipt_bot.sqlscript import split_sql

ROOT = Path(__file__).resolve().parents[1]
HOST = os.environ.get("TEST_MYSQL_HOST", "127.0.0.1")
PORT = int(os.environ.get("TEST_MYSQL_PORT", "3306"))
DBNAME = os.environ.get("TEST_MYSQL_DATABASE", "maidbot_test")
ADMIN_USER = os.environ.get("TEST_MYSQL_ADMIN_USER", "maidbot_test")
ADMIN_PW = os.environ.get("TEST_MYSQL_ADMIN_PASSWORD", "testpw_maidbot_admin")
APP_USER = os.environ.get("TEST_MYSQL_APP_USER", "maidbot_test_app")
APP_PW = os.environ.get("TEST_MYSQL_APP_PASSWORD", "apppw_maidbot_test")
ADMIN_ID, MAID_ID = 1001, 2002
D = Decimal


def _admin_conn():
    return pymysql.connect(host=HOST, port=PORT, user=ADMIN_USER, password=ADMIN_PW, database=DBNAME,
                           autocommit=True, charset="utf8mb4")


try:
    _admin_conn().close()
    MYSQL_OK = True
except Exception as exc:  # pragma: no cover
    MYSQL_OK = False
    MYSQL_ERR = exc

pytestmark = [pytest.mark.mysql,
              pytest.mark.skipif(not MYSQL_OK, reason="test MySQL not reachable; run tests/setup_test_db.sh")]


def sql_statements(path: Path):
    return split_sql(path.read_text())


ALL_TABLES = ("edit_sessions", "audit_log", "adjustments", "topups", "receipts", "float_balance", "schema_version")


def reset_schema(conn, schema: Path = ROOT / "migrations" / "001_initial.sql"):
    with conn.cursor() as cur:
        cur.execute("SET FOREIGN_KEY_CHECKS=0")
        for t in ALL_TABLES:
            cur.execute(f"DROP TABLE IF EXISTS {t}")
        cur.execute("SET FOREIGN_KEY_CHECKS=1")
        for stmt in sql_statements(schema):
            cur.execute(stmt)


@pytest.fixture
def db():
    conn = _admin_conn()
    reset_schema(conn)
    try:  # grants are applied once by tests/setup_test_db.sh and survive DROP/CREATE TABLE
        pymysql.connect(host=HOST, port=PORT, user=APP_USER, password=APP_PW, database=DBNAME).close()
        user, pw = APP_USER, APP_PW
    except pymysql.err.MySQLError:
        user, pw = ADMIN_USER, ADMIN_PW
    conn.close()
    cfg = Config.from_env({"MYSQL_HOST": HOST, "MYSQL_PORT": str(PORT), "MYSQL_USER": user,
                           "MYSQL_PASSWORD": pw, "MYSQL_DATABASE": DBNAME,
                           "ADMIN_TELEGRAM_ID": str(ADMIN_ID), "MAID_TELEGRAM_ID": str(MAID_ID)},
                          require_token=False)
    return Database(cfg)


NOW = datetime(2026, 10, 27, 9, 0, 0)
THR = D("100.00")


_fuid = iter(range(1, 10 ** 9))


def new_receipt(db, amount="45.50", now=NOW, merchant="Wellcome", file_unique_id=None,
                receipt_date=date(2026, 10, 26)):
    """Each call is a different photo unless file_unique_id is given (duplicate tests)."""
    ext = Extraction(amount=D(amount) if amount else None, receipt_date=receipt_date,
                     merchant=merchant, items="milk; eggs", engine="tesseract", confidence=91.2,
                     raw_text="TOTAL 45.50")
    return db.create_pending_receipt(MAID_ID, "/var/lib/x/r.jpg", ext, now, "fid",
                                     file_unique_id or f"fuid{next(_fuid)}")


def audit(db):
    with db.transaction() as cur:
        cur.execute("SELECT * FROM audit_log ORDER BY id")
        return cur.fetchall()


def test_runs_with_least_privilege_user(db):
    assert db.cfg.mysql_user == APP_USER, "grants from deploy/db-grants were not applied"
    assert db.health_check()["balance"] == D("0.00")


def test_topup_adds_and_audits(db):
    r = db.topup(D("500"), ADMIN_ID, "top up 500", NOW)
    assert (r.balance_before, r.balance_after) == (D("0.00"), D("500.00"))
    r2 = db.topup(D("20.50"), ADMIN_ID, "topup 20.5", NOW)
    assert r2.balance_after == D("520.50")
    rows = [a for a in audit(db) if a["event_type"] == "topup"]
    assert len(rows) == 2 and rows[0]["actor_telegram_id"] == ADMIN_ID
    assert rows[1]["balance_before"] == D("500.00") and rows[1]["balance_after"] == D("520.50")
    with db.transaction() as cur:
        cur.execute("SELECT COUNT(*) n FROM topups")
        assert cur.fetchone()["n"] == 2


def test_pending_does_not_deduct_then_confirm_deducts(db):
    db.topup(D("500"), ADMIN_ID, "top up 500", NOW)
    rid = new_receipt(db, "45.50")
    assert db.get_balance()["balance"] == D("500.00")  # nothing deducted while pending
    res = db.confirm_receipt(rid, MAID_ID, NOW, THR)
    assert res.status == "confirmed"
    assert (res.balance_before, res.balance_after) == (D("500.00"), D("454.50"))
    assert db.get_balance()["balance"] == D("454.50")
    r = db.get_receipt(rid)
    assert r["status"] == "confirmed" and r["balance_after"] == D("454.50") and r["confirmed_by"] == MAID_ID
    # double-tap Confirm is idempotent
    again = db.confirm_receipt(rid, MAID_ID, NOW, THR)
    assert again.status == "already_confirmed" and db.get_balance()["balance"] == D("454.50")
    types = [a["event_type"] for a in audit(db)]
    assert types == ["topup", "receipt_created", "receipt_confirmed"]


def test_negative_balance_allowed(db):
    db.topup(D("30"), ADMIN_ID, "top up 30", NOW)
    rid = new_receipt(db, "80.00")
    res = db.confirm_receipt(rid, MAID_ID, NOW, THR)
    assert res.status == "confirmed" and res.balance_after == D("-50.00")
    rid2 = new_receipt(db, "25.00")
    assert db.confirm_receipt(rid2, MAID_ID, NOW, THR).balance_after == D("-75.00")
    assert db.topup(D("100"), ADMIN_ID, "top up 100", NOW).balance_after == D("25.00")


def test_edit_and_cancel_are_audited(db):
    rid = new_receipt(db, None)
    assert db.confirm_receipt(rid, MAID_ID, NOW, THR).status == "missing_amount"
    assert db.set_edit_field(rid, "amount", MAID_ID, NOW)["edit_field"] == "amount"
    assert db.get_awaiting_edit(MAID_ID)["id"] == rid
    upd = db.update_field(rid, "amount", D("12.30"), MAID_ID, NOW)
    assert upd["amount"] == D("12.30") and db.get_awaiting_edit(MAID_ID) is None
    assert db.get_awaiting_edit(MAID_ID) is None
    db.update_field(rid, "merchant", "7-Eleven", MAID_ID, NOW)
    rid2 = new_receipt(db, "9.90")
    assert db.cancel_receipt(rid2, MAID_ID, NOW) == "cancelled_now"
    assert db.cancel_receipt(rid2, MAID_ID, NOW) == "cancelled"
    assert db.confirm_receipt(rid2, MAID_ID, NOW, THR).status == "cancelled"
    assert db.update_field(rid2, "amount", D("1"), MAID_ID, NOW) is None
    edits = [a for a in audit(db) if a["event_type"] == "receipt_edited"]
    assert len(edits) == 2 and '"old": null' in edits[0]["details"] and '"new": "12.30"' in edits[0]["details"]
    assert [a["event_type"] for a in audit(db)].count("receipt_cancelled") == 1
    assert db.get_balance()["balance"] == D("0.00")


def test_low_float_alert_once_per_day_and_rearms(db):
    db.topup(D("300"), ADMIN_ID, "top up 300", NOW)
    day1 = NOW
    # 300 -> 150: above threshold, no alert
    assert db.confirm_receipt(new_receipt(db, "150"), MAID_ID, day1, THR).low_float_alert is False
    # 150 -> 100: exactly at threshold (<=) -> this caller gets the alert claim
    res = db.confirm_receipt(new_receipt(db, "50"), MAID_ID, day1, THR)
    assert res.low_float_alert is True
    assert db.get_balance()["last_low_alert_date"] is None      # not "sent" until it is (H2)
    db.finish_low_alert(day1, True, res.balance_after, THR, res.receipt["id"])
    # more receipts same HKT day -> capped
    assert db.confirm_receipt(new_receipt(db, "10"), MAID_ID, day1 + timedelta(hours=14, minutes=59),
                              THR).low_float_alert is False
    assert db.get_balance()["last_low_alert_date"] == date(2026, 10, 27)
    # top-up doesn't reset the daily cap (balance 90 -> 100, still <= threshold)
    db.topup(D("10"), ADMIN_ID, "top up 10", day1)
    # next HKT day (00:00:01 on the 28th) -> re-armed
    day2 = datetime(2026, 10, 28, 0, 0, 1)
    res2 = db.confirm_receipt(new_receipt(db, "5"), MAID_ID, day2, THR)
    assert res2.low_float_alert is True
    # while the first sender holds the claim, nobody else may send (no double alert)
    assert db.confirm_receipt(new_receipt(db, "5"), MAID_ID, day2, THR, allow_duplicate=True).low_float_alert is False
    db.finish_low_alert(day2, True, res2.balance_after, THR)
    assert db.confirm_receipt(new_receipt(db, "5"), MAID_ID, day2, THR, allow_duplicate=True).low_float_alert is False
    alerts = [a for a in audit(db) if a["event_type"] == "low_float_alert"]
    assert [a["event_time"].date() for a in alerts] == [date(2026, 10, 27), date(2026, 10, 28)]


def test_no_alert_when_above_threshold(db):
    db.topup(D("1000"), ADMIN_ID, "top up 1000", NOW)
    assert db.confirm_receipt(new_receipt(db, "100"), MAID_ID, NOW, THR).low_float_alert is False
    assert db.get_balance()["last_low_alert_date"] is None


def test_audit_log_is_append_only(db):
    db.topup(D("10"), ADMIN_ID, "top up 10", NOW)
    with pytest.raises(pymysql.err.MySQLError):
        with db.transaction() as cur:
            cur.execute("UPDATE audit_log SET amount = 0")
    with pytest.raises(pymysql.err.MySQLError):
        with db.transaction() as cur:
            cur.execute("DELETE FROM topups")
    assert len(audit(db)) == 1


def test_history_and_export(db):
    db.topup(D("500"), ADMIN_ID, "top up 500", datetime(2026, 9, 30, 23, 0))
    sept = new_receipt(db, "10", now=datetime(2026, 9, 30, 23, 30))
    db.confirm_receipt(sept, MAID_ID, datetime(2026, 9, 30, 23, 59, 59), THR)
    oct1 = new_receipt(db, "20")
    db.confirm_receipt(oct1, MAID_ID, datetime(2026, 10, 1, 0, 0, 0), THR)
    new_receipt(db, "30")  # pending: excluded from export/history
    h = db.history(10)
    assert [x["event_type"] for x in h] == ["receipt_confirmed", "receipt_confirmed", "topup"]
    assert h[0]["receipt_id"] == oct1 and h[0]["merchant"] == "Wellcome"
    assert [r["id"] for r in db.confirmed_receipts_for_month(2026, 10)] == [oct1]
    assert [r["id"] for r in db.confirmed_receipts_for_month(2026, 9)] == [sept]
    assert len(db.pending_receipts()) == 1


def test_concurrent_confirms_and_topups_are_consistent(db):
    import concurrent.futures as cf
    db.topup(D("1000"), ADMIN_ID, "top up 1000", NOW)
    rids = [new_receipt(db, "7.25") for _ in range(20)]
    with cf.ThreadPoolExecutor(8) as ex:
        # same amount+date on purpose: allow_duplicate so only the locking is under test
        futs = [ex.submit(db.confirm_receipt, r, MAID_ID, NOW, THR, True) for r in rids]
        futs += [ex.submit(db.confirm_receipt, r, MAID_ID, NOW, THR, True) for r in rids]  # double taps
        futs += [ex.submit(db.topup, D("1"), ADMIN_ID, "top up 1", NOW) for _ in range(10)]
        results = [f.result() for f in futs]
    assert sum(1 for r in results if getattr(r, "status", None) == "confirmed") == 20
    assert db.get_balance()["balance"] == D("1000") - 20 * D("7.25") + 10
