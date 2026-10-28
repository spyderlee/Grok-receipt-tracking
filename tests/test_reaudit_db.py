"""Re-audit round 2, database side: N1, N2/N2b (barrier concurrency), N4-N6, N10-N12."""
import threading
from datetime import date, datetime, timedelta
from decimal import Decimal as D

import pymysql
import pytest

from maid_receipt_bot import db as dbmod
from maid_receipt_bot.db import ADJUST_BALANCE_LIMIT, Database, retry_on_conflict
from tests.test_db_integration import ADMIN_ID, MAID_ID, MYSQL_OK, NOW, THR, audit, db, new_receipt  # noqa: F401

pytestmark = [pytest.mark.mysql, pytest.mark.skipif(not MYSQL_OK, reason="test MySQL not reachable")]


# ------------------------------------------------------------------ helpers
class SnapshotOff(Database):
    """MySQL-8-like behaviour on MariaDB: no snapshot-isolation errors, a stale read view
    simply reads stale data (audit N2b)."""
    def _connect(self):
        c = super()._connect()
        with c.cursor() as cur:
            cur.execute("SET SESSION innodb_snapshot_isolation=OFF")
        return c


class OldLockOrder(SnapshotOff):
    """Control for the barrier test: re-creates the pre-fix bug by opening the read view (a
    plain SELECT) before the float lock, as v1.1's duplicate check did."""
    from contextlib import contextmanager

    @contextmanager
    def transaction(self):
        with super().transaction() as cur:
            cur.execute("SELECT COUNT(*) n FROM receipts")
            cur.fetchall()
            yield cur


def race(fa, fb):
    """Run fa and fb in two threads released by one barrier; return their results / exceptions."""
    bar, out = threading.Barrier(2), [None, None]

    def run(i, f):
        bar.wait()
        try:
            out[i] = f()
        except BaseException as exc:  # noqa: BLE001 - collected and asserted on below
            out[i] = exc
    ts = [threading.Thread(target=run, args=(0, fa)), threading.Thread(target=run, args=(1, fb))]
    for t in ts:
        t.start()
    for t in ts:
        t.join(60)
    return out


def fb_row(d):
    with d.transaction() as cur:
        cur.execute("SELECT * FROM float_balance WHERE id=1")
        return cur.fetchone()


def balance(d):
    return d.get_balance()["balance"]


def no_errors(results):
    errs = [r for pair in results for r in pair if isinstance(r, BaseException)]
    assert not errs, errs[:3]


def run_mix(d, n_each, kinds=("topup", "dup", "adjust", "void")):
    """n_each racing pairs per kind against a fresh float; returns (expected_balance, stats)."""
    d.topup(D("100000"), ADMIN_ID, "seed", NOW)
    expected = D("100000")
    stats = {"double_deductions": 0}
    results = []
    k = 0
    for kind in kinds:
        for _ in range(n_each):
            k += 1
            amt = D(f"{k % 90 + 1}.{k % 100:02d}")
            if kind == "topup":
                rid = new_receipt(d, str(amt), merchant=f"shop{k}")
                res = race(lambda: d.confirm_receipt(rid, MAID_ID, NOW, THR),
                           lambda: d.topup(D("7"), ADMIN_ID, "top up 7", NOW))
                expected += D("7") - amt
            elif kind == "dup":   # the same photo sent twice, both copies confirmed at once
                fu = f"dupphoto-{k}"
                r1 = new_receipt(d, str(amt), merchant=f"shop{k}", file_unique_id=fu)
                r2 = new_receipt(d, str(amt), merchant=f"shop{k}", file_unique_id=fu)
                res = race(lambda: d.confirm_receipt(r1, MAID_ID, NOW, THR),
                           lambda: d.confirm_receipt(r2, MAID_ID, NOW, THR))
                ok = [r for r in res if not isinstance(r, BaseException) and r.status == "confirmed"]
                stats["double_deductions"] += len(ok) > 1
                expected -= amt * len(ok)
            elif kind == "adjust":
                rid = new_receipt(d, str(amt), merchant=f"shop{k}")
                res = race(lambda: d.confirm_receipt(rid, MAID_ID, NOW, THR),
                           lambda: d.adjust(D("-3"), ADMIN_ID, "cash count", NOW))
                expected += D("-3") - amt
            else:                 # void an older receipt while a new one is being confirmed
                old = new_receipt(d, "11.00", merchant=f"old{k}")
                assert d.confirm_receipt(old, MAID_ID, NOW, THR).status == "confirmed"
                rid = new_receipt(d, str(amt), merchant=f"shop{k}")
                res = race(lambda: d.confirm_receipt(rid, MAID_ID, NOW, THR),
                           lambda: d.adjust(None, ADMIN_ID, "wrong", NOW, receipt_id=old))
                expected -= amt     # -11 then +11 cancel out
            results.append(res)
    return expected, stats, results


# ------------------------------------------------------------------ N2 / N2b
def test_N2_barrier_200_pairs_snapshot_isolation_on(db):  # noqa: F811
    with db.transaction() as cur:
        cur.execute("SELECT @@innodb_snapshot_isolation v")
        assert cur.fetchone()["v"] == 1          # the MariaDB 11.8 default the audit tested on
    expected, stats, results = run_mix(db, 50)   # 4 kinds x 50 = 200 racing pairs
    no_errors(results)                            # no 1020 reaches the caller
    assert stats["double_deductions"] == 0
    assert balance(db) == expected
    rec = db.reconcile()
    assert rec["ok"], rec["problems"]


def test_N2b_barrier_pairs_mysql_like_no_snapshot_isolation(db):  # noqa: F811
    d = SnapshotOff(db.cfg)
    with d.transaction() as cur:
        cur.execute("SELECT @@innodb_snapshot_isolation v")
        assert cur.fetchone()["v"] == 0
    expected, stats, results = run_mix(d, 25)     # 100 pairs
    no_errors(results)
    assert stats["double_deductions"] == 0        # the stale-read-view double deduction is gone
    assert balance(d) == expected
    assert d.reconcile()["ok"]


def test_N2b_control_old_lock_order_does_double_deduct(db):  # noqa: F811
    """Proves the barrier test can see the bug: with the read view opened before the float
    lock (the v1.1 order) and no snapshot isolation, duplicate photos are deducted twice."""
    d = OldLockOrder(db.cfg)
    _, stats, results = run_mix(d, 40, kinds=("dup",))
    no_errors(results)
    assert stats["double_deductions"] > 0


def _err(errno):
    return pymysql.err.OperationalError(errno, "fake")


@pytest.mark.parametrize("errno", [1020, 1213])
def test_N2_retry_on_conflict_retries_once(monkeypatch, errno):
    monkeypatch.setattr(dbmod.time, "sleep", lambda s: None)
    calls = []

    @retry_on_conflict
    def f():
        calls.append(1)
        if len(calls) == 1:
            raise _err(errno)
        return "ok"
    assert f() == "ok" and len(calls) == 2

    @retry_on_conflict
    def always():
        calls.append(1)
        raise _err(errno)
    calls.clear()
    with pytest.raises(pymysql.err.OperationalError):
        always()
    assert len(calls) == 2


def test_N2_retry_on_conflict_does_not_retry_other_errors():
    calls = []

    @retry_on_conflict
    def f():
        calls.append(1)
        raise _err(1045)
    with pytest.raises(pymysql.err.OperationalError):
        f()
    assert len(calls) == 1


# ------------------------------------------------------------------ N1
def test_N1_resent_photo_after_void_is_not_a_duplicate(db):  # noqa: F811
    db.topup(D("500"), ADMIN_ID, "t", NOW)
    r1 = new_receipt(db, "45.50", file_unique_id="photoA")
    assert db.confirm_receipt(r1, MAID_ID, NOW, THR).status == "confirmed"
    r2 = new_receipt(db, "45.50", file_unique_id="photoA")
    assert db.confirm_receipt(r2, MAID_ID, NOW, THR).status == "possible_duplicate"
    db.cancel_receipt(r2, MAID_ID, NOW)
    assert db.adjust(None, ADMIN_ID, "wrong amount", NOW, receipt_id=r1).status == "adjusted"
    r3 = new_receipt(db, "45.50", file_unique_id="photoA")
    res = db.confirm_receipt(r3, MAID_ID, NOW, THR)
    assert res.status == "confirmed", res
    assert balance(db) == D("454.50")
    assert db.reconcile()["ok"]


def test_N1_month_export_marks_voided_receipts(db):  # noqa: F811
    db.topup(D("500"), ADMIN_ID, "t", NOW)
    a = new_receipt(db, "10.00", merchant="A")
    b = new_receipt(db, "20.00", merchant="B")
    for r in (a, b):
        db.confirm_receipt(r, MAID_ID, NOW, THR)
    db.adjust(None, ADMIN_ID, "duplicate", NOW + timedelta(hours=1), receipt_id=a)
    rows = {r["id"]: r for r in db.confirmed_receipts_for_month(2026, 10)}
    assert rows[a]["void_adjustment_id"] and rows[a]["void_reason"] == "duplicate"
    assert rows[a]["voided_at"] == NOW + timedelta(hours=1)
    assert rows[b]["void_adjustment_id"] is None


# ------------------------------------------------------------------ N4 / N5 / N6
def test_N4_sweep_does_not_realert_a_delivered_low_float_next_day(db):  # noqa: F811
    db.topup(D("120"), ADMIN_ID, "t", NOW)
    res = db.confirm_receipt(new_receipt(db, "50"), MAID_ID, NOW, THR)
    assert res.low_float_alert
    db.finish_low_alert(NOW, True, res.balance_after, THR, claimed_at=res.alert_claim)
    next_morning = NOW + timedelta(days=1)
    assert db.claim_low_alert(next_morning, THR) is None       # delivered yesterday: nothing owed
    assert db.claim_low_alert(next_morning + timedelta(days=5), THR) is None


def test_N4_sweep_retries_a_failed_alert(db):  # noqa: F811
    db.topup(D("120"), ADMIN_ID, "t", NOW)
    res = db.confirm_receipt(new_receipt(db, "50"), MAID_ID, NOW, THR)
    db.finish_low_alert(NOW, False, res.balance_after, THR, error="net down", claimed_at=res.alert_claim)
    claim = db.claim_low_alert(NOW + timedelta(hours=1), THR)
    assert claim is not None and claim.balance == D("70.00")
    assert db.finish_low_alert(NOW + timedelta(hours=1), True, claim.balance, THR,
                               claimed_at=claim.claimed_at) == "owner"
    assert db.claim_low_alert(NOW + timedelta(hours=2), THR) is None


def test_N4_failed_alert_is_dropped_after_a_topup(db):  # noqa: F811
    db.topup(D("120"), ADMIN_ID, "t", NOW)
    res = db.confirm_receipt(new_receipt(db, "50"), MAID_ID, NOW, THR)
    db.finish_low_alert(NOW, False, res.balance_after, THR, claimed_at=res.alert_claim)
    db.topup(D("500"), ADMIN_ID, "t", NOW)
    assert db.claim_low_alert(NOW + timedelta(hours=1), THR) is None
    assert fb_row(db)["low_alert_failed_date"] is None


def test_N5_alert_claimed_2358_delivered_0001_counts_for_the_claim_day(db):  # noqa: F811
    db.topup(D("120"), ADMIN_ID, "t", NOW)
    late = datetime(2026, 10, 27, 23, 58, 30)
    res = db.confirm_receipt(new_receipt(db, "50", now=late), MAID_ID, late, THR)
    assert res.low_float_alert and res.alert_claim == datetime(2026, 10, 27, 23, 58, 30)
    db.finish_low_alert(datetime(2026, 10, 28, 0, 1), True, res.balance_after, THR, claimed_at=res.alert_claim)
    assert db.get_balance()["last_low_alert_date"] == date(2026, 10, 27)
    # so the next receipt on the 28th that keeps the float low still alerts that day
    nxt = datetime(2026, 10, 28, 9, 0)
    assert db.confirm_receipt(new_receipt(db, "5", now=nxt), MAID_ID, nxt, THR).low_float_alert is True


def test_N6_stale_sender_does_not_release_the_new_owners_claim(db):  # noqa: F811
    db.topup(D("120"), ADMIN_ID, "t", NOW)
    a = db.confirm_receipt(new_receipt(db, "50"), MAID_ID, NOW, THR)
    assert a.low_float_alert
    # A hangs past the lease; the sweep (worker B) re-claims
    b = db.claim_low_alert(NOW + timedelta(minutes=6), THR)
    assert b is not None and b.claimed_at != a.alert_claim
    # A finally fails: it must not clear B's claim
    assert db.finish_low_alert(NOW + timedelta(minutes=7), False, a.balance_after, THR,
                               claimed_at=a.alert_claim) == "not_owner"
    fb = fb_row(db)
    assert fb["low_alert_claimed_at"] == b.claimed_at and fb["low_alert_failed_date"] is None
    # nobody else can claim while B's lease runs
    assert db.claim_low_alert(NOW + timedelta(minutes=8), THR) is None
    assert db.finish_low_alert(NOW + timedelta(minutes=8), True, b.balance, THR,
                               claimed_at=b.claimed_at) == "owner"
    fb = fb_row(db)
    assert fb["low_alert_claimed_at"] is None and fb["last_low_alert_date"] == NOW.date()


def test_N6_late_success_of_a_non_owner_still_records_the_day(db):  # noqa: F811
    db.topup(D("120"), ADMIN_ID, "t", NOW)
    a = db.confirm_receipt(new_receipt(db, "50"), MAID_ID, NOW, THR)
    b = db.claim_low_alert(NOW + timedelta(minutes=6), THR)
    assert db.finish_low_alert(NOW + timedelta(minutes=7), True, a.balance_after, THR,
                               claimed_at=a.alert_claim) == "not_owner"
    fb = fb_row(db)
    assert fb["last_low_alert_date"] == NOW.date() and fb["low_alert_claimed_at"] == b.claimed_at


# ------------------------------------------------------------------ N10
def test_N10_adjust_cannot_walk_the_balance_past_the_limit(db):  # noqa: F811
    big = D("900000")
    assert db.adjust(big, ADMIN_ID, "a", NOW).status == "adjusted"
    res = db.adjust(big, ADMIN_ID, "b", NOW)            # each step in range, the sum is not
    assert res.status == "out_of_range" and res.balance_after == D("1800000.00")
    assert balance(db) == big
    assert db.adjust(ADJUST_BALANCE_LIMIT - big, ADMIN_ID, "exactly at limit", NOW).status == "adjusted"
    assert db.adjust(D("0.01"), ADMIN_ID, "one cent over", NOW).status == "out_of_range"
    assert db.adjust(D("-5"), ADMIN_ID, "towards zero", NOW).status == "adjusted"
    assert db.adjust(-D("2100000"), ADMIN_ID, "too negative", NOW).status == "out_of_range"
    assert db.reconcile()["ok"]


def test_N10_moving_back_towards_zero_is_always_allowed(db):  # noqa: F811
    # a balance already beyond the limit (top-ups are not capped)
    db.topup(D("1500000"), ADMIN_ID, "t", NOW)
    assert db.adjust(D("-100000"), ADMIN_ID, "fix", NOW).status == "adjusted"
    assert db.adjust(D("1"), ADMIN_ID, "further away", NOW).status == "out_of_range"


def test_N10_void_is_exempt_from_the_limit(db):  # noqa: F811
    db.adjust(-D("999990"), ADMIN_ID, "deep negative", NOW)
    r = new_receipt(db, "5.00")
    db.confirm_receipt(r, MAID_ID, NOW, THR)   # -999995
    assert db.adjust(None, ADMIN_ID, "void", NOW, receipt_id=r).status == "adjusted"


# ------------------------------------------------------------------ N12
def test_N12_edit_session_expires_after_30_minutes(db):  # noqa: F811
    rid = new_receipt(db, "45.50")
    db.set_edit_field(rid, "amount", MAID_ID, NOW)
    live = db.get_awaiting_edit(MAID_ID, NOW + timedelta(minutes=29))
    assert live["id"] == rid and live["edit_expired"] is False
    gone = db.get_awaiting_edit(MAID_ID, NOW + timedelta(minutes=31))
    assert gone["id"] == rid and gone["edit_expired"] is True
    assert db.get_awaiting_edit(MAID_ID, NOW + timedelta(minutes=32)) is None   # deleted
    assert db.get_receipt(rid)["amount"] == D("45.50")


def test_N12_restarting_an_edit_refreshes_the_timer(db):  # noqa: F811
    rid = new_receipt(db, "45.50")
    db.set_edit_field(rid, "amount", MAID_ID, NOW)
    db.set_edit_field(rid, "merchant", MAID_ID, NOW + timedelta(minutes=25))
    s = db.get_awaiting_edit(MAID_ID, NOW + timedelta(minutes=50))
    assert s["edit_expired"] is False and s["edit_field"] == "merchant"


# ------------------------------------------------------------------ N11
def test_N11_schema_version_is_1_and_none_when_table_missing(db):  # noqa: F811
    assert db.schema_version() == dbmod.REQUIRED_SCHEMA_VERSION == 1
    from tests.test_db_integration import _admin_conn
    c = _admin_conn()
    try:
        with c.cursor() as cur:
            cur.execute("DROP TABLE schema_version")
    finally:
        c.close()
    assert db.schema_version() is None
