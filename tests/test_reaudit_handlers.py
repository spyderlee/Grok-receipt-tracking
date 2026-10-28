"""Re-audit round 2, handler side: N1 export, N4 quiet hours, N10 message, N12 expiry."""
import csv
import io
from datetime import datetime, timedelta
from decimal import Decimal as D

import pytest

from maid_receipt_bot import handlers as h
from maid_receipt_bot.config import Config
from tests.test_db_integration import ADMIN_ID, MAID_ID, NOW, THR, db, new_receipt  # noqa: F401
from tests.test_handlers import FakeMessage, cb, env, pytestmark, sent_to, upd  # noqa: F401


@pytest.fixture
def clock(monkeypatch):
    """Control Config.now() (the handlers' clock)."""
    t = {"now": NOW}
    monkeypatch.setattr(Config, "now", lambda self: t["now"])
    return t


@pytest.fixture(autouse=True)
def fast_retries(monkeypatch):
    monkeypatch.setattr(h, "ALERT_RETRY_DELAYS", (0, 0))


async def test_N1_export_lists_voided_receipts_but_leaves_them_out_of_the_total(env, clock):
    env.db.topup(D("500"), ADMIN_ID, "t", NOW)
    a, b = new_receipt(env.db, "40.00", merchant="A"), new_receipt(env.db, "2.50", merchant="B")
    for r in (a, b):
        env.db.confirm_receipt(r, MAID_ID, NOW, THR)
    env.ctx.args = [str(a), "entered", "twice"]
    await h.cmd_void(upd(ADMIN_ID, FakeMessage(ADMIN_ID, "/void")), env.ctx)
    env.ctx.args = ["2026-10"]
    m = FakeMessage(ADMIN_ID, "/export")
    await h.cmd_export(upd(ADMIN_ID, m), env.ctx)
    kw = m.reply_document.call_args.kwargs
    assert kw["caption"] == ("1 confirmed receipts in 2026-10, total HK$2.50 "
                             "(1 voided receipt listed but excluded)")
    rows = list(csv.DictReader(io.StringIO(kw["document"].getvalue().decode("utf-8-sig"))))
    by_id = {int(r["receipt_id"]): r for r in rows}
    assert by_id[a]["voided"] == "yes" and by_id[a]["void_reason"] == "entered twice" and by_id[a]["voided_at"]
    assert by_id[b]["voided"] == "no" and by_id[b]["voided_at"] == "" and by_id[b]["void_reason"] == ""


async def test_N1_export_without_voids_keeps_the_old_caption(env, clock):
    env.db.topup(D("500"), ADMIN_ID, "t", NOW)
    env.db.confirm_receipt(new_receipt(env.db, "40.00"), MAID_ID, NOW, THR)
    env.ctx.args = []
    m = FakeMessage(ADMIN_ID, "/export")
    await h.cmd_export(upd(ADMIN_ID, m), env.ctx)
    assert m.reply_document.call_args.kwargs["caption"] == "1 confirmed receipts in 2026-10, total HK$40.00"


async def test_N10_adjust_beyond_the_limit_is_refused_with_a_reason(env, clock):
    env.db.adjust(D("900000"), ADMIN_ID, "seed", NOW)
    env.ctx.args = ["+900000", "typo"]
    m = FakeMessage(ADMIN_ID, "/adjust")
    await h.cmd_adjust(upd(ADMIN_ID, m), env.ctx)
    text = m.replies[-1][0]
    assert text.startswith("Not adjusted") and "HK$1,800,000.00" in text and "±HK$1,000,000.00" in text
    assert env.db.get_balance()["balance"] == D("900000")
    assert not any("Float adjusted" in t for t in sent_to(env.bot, MAID_ID))


async def test_N12_expired_edit_does_not_capture_text(env, clock):
    rid = new_receipt(env.db, "45.50")
    u, _ = cb(MAID_ID, f"f:{rid}:amount", MAID_ID)
    await h.on_callback(u, env.ctx)
    clock["now"] = NOW + timedelta(minutes=31)
    m = FakeMessage(MAID_ID, "4.55")
    await h.on_text(upd(MAID_ID, m), env.ctx)
    assert "timed out" in m.replies[-1][0] and f"#{rid}" in m.replies[-1][0]
    assert env.db.get_receipt(rid)["amount"] == D("45.50")
    m = FakeMessage(MAID_ID, "4.55")                  # session is gone now: ordinary text
    await h.on_text(upd(MAID_ID, m), env.ctx)
    assert "Send me a photo" in m.replies[-1][0]


async def test_N12_live_edit_still_works_and_expired_one_lets_a_topup_through(env, clock):
    rid = new_receipt(env.db, "45.50")
    u, _ = cb(ADMIN_ID, f"f:{rid}:amount", ADMIN_ID)
    await h.on_callback(u, env.ctx)
    clock["now"] = NOW + timedelta(minutes=20)
    await h.on_text(upd(ADMIN_ID, FakeMessage(ADMIN_ID, "46.00")), env.ctx)
    assert env.db.get_receipt(rid)["amount"] == D("46.00")
    await h.on_callback(cb(ADMIN_ID, f"f:{rid}:merchant", ADMIN_ID)[0], env.ctx)
    clock["now"] = NOW + timedelta(hours=2)
    m = FakeMessage(ADMIN_ID, "top up 300")
    await h.on_text(upd(ADMIN_ID, m), env.ctx)
    assert "Topped up" in m.replies[-1][0]
    assert env.db.get_receipt(rid)["merchant"] != "top up 300"


async def _failed_alert(env, when):
    env.db.topup(D("120"), ADMIN_ID, "t", when)
    res = env.db.confirm_receipt(new_receipt(env.db, "50", now=when), MAID_ID, when, THR)
    env.db.finish_low_alert(when, False, res.balance_after, THR, claimed_at=res.alert_claim)


async def test_N4_sweep_holds_a_failed_alert_during_quiet_hours(env, clock):
    st = env.ctx.application.bot_data["state"]
    await _failed_alert(env, datetime(2026, 10, 27, 21, 50))
    clock["now"] = datetime(2026, 10, 27, 23, 0)
    out = await h.sweep_once(env.bot, st)
    assert out["alert"] == "quiet_hours" and not [t for t in sent_to(env.bot, ADMIN_ID) if "Low float" in t]
    clock["now"] = datetime(2026, 10, 28, 8, 5)
    out = await h.sweep_once(env.bot, st)
    assert [t for t in sent_to(env.bot, ADMIN_ID) if "Low float" in t]
    # delivered: the day it was claimed is consumed, the sweep has nothing more to send
    clock["now"] = datetime(2026, 10, 28, 9, 5)
    assert (await h.sweep_once(env.bot, st))["alert"] is None


async def test_N4_sweep_never_nags_about_a_delivered_alert(env, clock):
    st = env.ctx.application.bot_data["state"]
    env.db.topup(D("120"), ADMIN_ID, "t", NOW)
    res = env.db.confirm_receipt(new_receipt(env.db, "50"), MAID_ID, NOW, THR)
    env.db.finish_low_alert(NOW, True, res.balance_after, THR, claimed_at=res.alert_claim)
    for day in range(1, 4):
        clock["now"] = NOW + timedelta(days=day)
        assert (await h.sweep_once(env.bot, st))["alert"] is None
    assert not [t for t in sent_to(env.bot, ADMIN_ID) if "Low float" in t]
