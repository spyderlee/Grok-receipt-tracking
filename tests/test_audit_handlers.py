"""Regression tests for the audit's handler probes (tests/test_zz_handler_probes.py H1-H9) and the
new admin commands / background checks. Real MySQL + Tesseract, Telegram fully mocked."""
import asyncio
from decimal import Decimal as D
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from PIL import Image
from telegram.error import BadRequest, Conflict, NetworkError

from maid_receipt_bot import handlers as h
from maid_receipt_bot.ocr import Extraction
from tests.helpers import make_receipt_image
from tests.test_db_integration import ADMIN_ID, MAID_ID, NOW, THR, _admin_conn, audit, db, new_receipt  # noqa
from tests.test_handlers import FakeMessage, FakePhoto, STRANGER, cb, env, pytestmark, sent_to, upd  # noqa


@pytest.fixture(autouse=True)
def fast_retries(monkeypatch):
    monkeypatch.setattr(h, "ALERT_RETRY_DELAYS", (0, 0))


async def drain():
    while h._background:
        await asyncio.gather(*list(h._background), return_exceptions=True)


def admin_alerts(bot):
    return [t for t in sent_to(bot, ADMIN_ID) if "Low float" in t]


def last_alert_date(db):
    return db.get_balance()["last_low_alert_date"]


# ------------------------------------------------------------------ M2 (H1)
async def test_H1_M2_cancel_that_loses_the_race_reports_the_real_status(env):
    env.db.topup(D("500"), ADMIN_ID, "t", NOW)
    rid = new_receipt(env.db, "45.50")
    stale = env.db.get_receipt(rid)
    env.db.confirm_receipt(rid, MAID_ID, NOW, THR)          # confirmed on the other device
    orig, calls = env.db.get_receipt, []

    def get_receipt(r):
        calls.append(r)
        return stale if len(calls) == 1 else orig(r)        # first read is stale, re-read is fresh
    env.db.get_receipt = get_receipt
    u, q = cb(MAID_ID, f"xx:{rid}", MAID_ID)
    await h.on_callback(u, env.ctx)
    env.db.get_receipt = orig
    text = q.edit_message_text.call_args.args[0]
    assert "nothing was deducted" not in text
    assert "is confirmed" in text and "HK$45.50 was deducted" in text
    assert "already confirmed" in q.answer.call_args.args[0]
    assert env.db.get_receipt(rid)["status"] == "confirmed"


async def test_M2_tap_on_a_finished_receipt_shows_its_status(env):
    rid = new_receipt(env.db, "5")
    env.db.cancel_receipt(rid, MAID_ID, NOW)
    u, q = cb(MAID_ID, f"e:{rid}", MAID_ID)
    await h.on_callback(u, env.ctx)
    assert "already cancelled" in q.answer.call_args.args[0]
    assert "cancelled — nothing was deducted" in q.edit_message_text.call_args.args[0]


# ------------------------------------------------------------------ H2
async def test_H2_expired_callback_does_not_lose_the_alert(env):
    env.db.topup(D("120"), ADMIN_ID, "t", NOW)
    rid = new_receipt(env.db, "45.50")
    u, q = cb(MAID_ID, f"c:{rid}", MAID_ID)
    q.answer = AsyncMock(side_effect=BadRequest("Query is too old and response timeout expired"))
    await h.on_callback(u, env.ctx)                         # no exception escapes
    assert env.db.get_balance()["balance"] == D("74.50")
    assert len(admin_alerts(env.bot)) == 1 and "HK$74.50" in admin_alerts(env.bot)[0]
    assert last_alert_date(env.db) is not None
    assert any(a["event_type"] == "low_float_alert" for a in audit(env.db))
    assert any("New float balance" in t for t in sent_to(env.bot, MAID_ID))  # maid still told


async def test_H2_alert_sent_before_any_other_telegram_call(env):
    env.db.topup(D("120"), ADMIN_ID, "t", NOW)
    rid = new_receipt(env.db, "45.50")
    order = []
    env.bot.send_message = AsyncMock(side_effect=lambda chat_id, text, **kw: order.append(("send", chat_id)))
    u, q = cb(MAID_ID, f"c:{rid}", MAID_ID)
    q.answer = AsyncMock(side_effect=lambda *a, **k: order.append(("answer",)))
    q.edit_message_text = AsyncMock(side_effect=lambda *a, **k: order.append(("edit",)))
    await h.on_callback(u, env.ctx)
    assert order[0] == ("send", ADMIN_ID)


async def _confirm_with_flaky_admin(env, failures):
    env.db.topup(D("120"), ADMIN_ID, "t", NOW)
    rid = new_receipt(env.db, "45.50")
    state = {"n": 0}

    async def send_message(chat_id, text, **kw):
        if chat_id == ADMIN_ID:
            state["n"] += 1
            if state["n"] <= failures:
                raise NetworkError("timed out")
        return FakeMessage(chat_id)
    env.bot.send_message = AsyncMock(side_effect=send_message)
    u, q = cb(MAID_ID, f"c:{rid}", MAID_ID)
    await h.on_callback(u, env.ctx)
    await drain()
    return state


async def test_H2b_failed_alert_is_retried_in_background(env):
    state = await _confirm_with_flaky_admin(env, failures=1)
    assert state["n"] == 2
    assert last_alert_date(env.db) is not None
    ev = [a for a in audit(env.db) if a["event_type"] == "low_float_alert"]
    assert len(ev) == 1 and '"attempts": 2' in ev[0]["details"]


async def test_H2b_alert_that_never_arrives_leaves_the_cap_open(env):
    state = await _confirm_with_flaky_admin(env, failures=99)
    assert state["n"] == 1 + len(h.ALERT_RETRY_DELAYS)
    assert last_alert_date(env.db) is None
    assert any(a["event_type"] == "low_float_alert_failed" for a in audit(env.db))
    # Telegram is back: the hourly sweep re-sends it, exactly once
    env.bot.send_message = AsyncMock()
    out = await h.sweep_once(env.bot, env.ctx.application.bot_data["state"])
    assert out["alert"] is True and len(admin_alerts(env.bot)) == 1
    assert last_alert_date(env.db) is not None
    out = await h.sweep_once(env.bot, env.ctx.application.bot_data["state"])
    assert out["alert"] is None and len(admin_alerts(env.bot)) == 1


async def test_H2b_next_receipt_resends_after_total_failure(env):
    await _confirm_with_flaky_admin(env, failures=99)
    env.bot.send_message = AsyncMock()
    rid2 = new_receipt(env.db, "1.00")
    u, q = cb(MAID_ID, f"c:{rid2}", MAID_ID)
    await h.on_callback(u, env.ctx)
    await drain()
    assert len(admin_alerts(env.bot)) == 1 and "HK$73.50" in admin_alerts(env.bot)[0]
    rid3 = new_receipt(env.db, "1.00")
    u, q = cb(MAID_ID, f"c:{rid3}", MAID_ID)
    await h.on_callback(u, env.ctx)
    await drain()
    assert len(admin_alerts(env.bot)) == 1                  # still one per day once delivered


# ------------------------------------------------------------------ L1 (H3)
def _doc(src, mime, name, uniq):
    d = FakePhoto(src)
    d.mime_type, d.file_name, d.file_unique_id = mime, name, uniq
    return d


async def test_H3_L1_heic_document_is_read(env, tmp_path):
    png = make_receipt_image(tmp_path / "r.png")
    heic = tmp_path / "r.heic"
    Image.open(png).save(heic, format="HEIF", quality=95)
    m = FakeMessage(MAID_ID, document=_doc(heic, "image/heic", "IMG_0001.HEIC", "Uheic"))
    await h.on_photo(upd(MAID_ID, m), env.ctx)
    r = env.db.get_receipt(env.db.pending_receipts()[-1]["id"])
    assert r["photo_path"].endswith(".heic")
    assert r["ocr_engine"] != "none"
    assert "cannot identify" not in (r["ocr_escalation_reason"] or "")
    assert r["amount"] == D("123.40")


@pytest.mark.parametrize("mime,name,ext", [("image/gif", "r.gif", ".gif"), ("image/png", "r.png", ".png"),
                                           ("image/webp", "x.webp", ".webp"), ("image/jpeg", "r.JPG", ".jpg"),
                                           ("image/heif", "r", ".heif"), ("image/x-unknown", "r.exe", ".img"),
                                           ("image/x-unknown", "r.tiff", ".tiff")])
def test_L1_extension_comes_from_mime_type(mime, name, ext):
    msg = SimpleNamespace(photo=[], document=SimpleNamespace(mime_type=mime, file_name=name))
    assert h._photo_ext(msg) == ext


def test_L1_non_image_document_refused():
    assert h._photo_ext(SimpleNamespace(photo=[], document=SimpleNamespace(mime_type="application/pdf",
                                                                           file_name="r.jpg"))) is None


# ------------------------------------------------------------------ L2 (H4)
async def test_H4_L2_edit_sessions_are_per_user(env):
    rid = new_receipt(env.db, None)
    u, q = cb(MAID_ID, f"f:{rid}:amount", MAID_ID)
    await h.on_callback(u, env.ctx)
    u, q = cb(ADMIN_ID, f"f:{rid}:merchant", ADMIN_ID)
    await h.on_callback(u, env.ctx)
    m = FakeMessage(MAID_ID, "45.50")
    await h.on_text(upd(MAID_ID, m), env.ctx)
    a = FakeMessage(ADMIN_ID, "ParknShop")
    await h.on_text(upd(ADMIN_ID, a), env.ctx)
    r = env.db.get_receipt(rid)
    assert r["amount"] == D("45.50") and r["merchant"] == "ParknShop"


# ------------------------------------------------------------------ M1 (H6)
async def test_H6_M1_huge_ocr_amount_still_saves_the_receipt(env, tmp_path):
    src = make_receipt_image(tmp_path / "a.png")

    async def fake_extract(path, today):
        return Extraction(amount=D("12345678901234.00"), engine="tesseract", confidence=92.0)
    env.ctx.application.bot_data["state"].reader.extract = fake_extract
    m = FakeMessage(MAID_ID, photo=[FakePhoto(src)])
    await h.on_photo(upd(MAID_ID, m), env.ctx)
    p = env.db.pending_receipts()
    assert len(p) == 1 and p[0]["amount"] is None
    # the "reading..." status message became the summary, asking her to type the amount
    status_edits = _status_edits(m)
    assert any("not found" in e and "out of range" in e for e in status_edits), status_edits


def _status_edits(m):
    # FakeMessage.reply_text returns a new FakeMessage whose edits we can't reach; record them instead
    return getattr(m, "_status_edits", [])


@pytest.fixture(autouse=True)
def record_status(monkeypatch):
    orig = FakeMessage.reply_text

    async def reply_text(self, text=None, **kw):
        child = await orig(self, text, **kw)
        self._status_edits = getattr(self, "_status_edits", [])
        parent = self

        async def edit_text(t, **k):
            parent._status_edits.append(t)
            return child
        child.edit_text = edit_text
        return child
    monkeypatch.setattr(FakeMessage, "reply_text", reply_text)


async def test_M1_db_failure_tells_the_maid_and_removes_the_photo(env, tmp_path):
    src = make_receipt_image(tmp_path / "a.png")

    def boom(*a, **k):
        raise RuntimeError("db down")
    env.db.create_pending_receipt = boom
    m = FakeMessage(MAID_ID, photo=[FakePhoto(src)])
    await h.on_photo(upd(MAID_ID, m), env.ctx)
    assert any("couldn't save that receipt" in e for e in _status_edits(m))
    assert not [p for p in (env.tmp / "photos").rglob("*") if p.is_file()]


# ------------------------------------------------------------------ L3 (H8)
@pytest.mark.parametrize("text", ["Top Up Mart", "top up 500"])
async def test_H8_L3_edit_in_progress_beats_topup_parsing(env, text):
    rid = new_receipt(env.db, "10")
    u, q = cb(ADMIN_ID, f"f:{rid}:merchant", ADMIN_ID)
    await h.on_callback(u, env.ctx)
    m = FakeMessage(ADMIN_ID, text)
    await h.on_text(upd(ADMIN_ID, m), env.ctx)
    assert env.db.get_receipt(rid)["merchant"] == text
    assert env.db.get_balance()["balance"] == D("0.00")


async def test_L3_topup_typed_into_amount_edit_gets_a_hint(env):
    rid = new_receipt(env.db, "10")
    u, q = cb(ADMIN_ID, f"f:{rid}:amount", ADMIN_ID)
    await h.on_callback(u, env.ctx)
    m = FakeMessage(ADMIN_ID, "top up 500")
    await h.on_text(upd(ADMIN_ID, m), env.ctx)
    assert "still editing receipt" in m.replies[-1][0]
    assert env.db.get_balance()["balance"] == D("0.00")
    m = FakeMessage(ADMIN_ID, "cancel")
    await h.on_text(upd(ADMIN_ID, m), env.ctx)
    m = FakeMessage(ADMIN_ID, "top up 500")
    await h.on_text(upd(ADMIN_ID, m), env.ctx)
    assert env.db.get_balance()["balance"] == D("500.00")


# ------------------------------------------------------------------ H9 / L10
async def test_H9_spoofed_and_malformed_callbacks_are_harmless(env):
    rid = env.db.create_pending_receipt(ADMIN_ID, "/x.jpg", Extraction(amount=D("5"), engine="tesseract",
                                                                        confidence=90), NOW)
    for data in [f"c:{rid}", f"xx:{rid}", f"f:{rid}:amount", "c:abc", "c:-1", "c:0", f"zz:{rid}",
                 "c:99999999999999999999", "", "c", f"f:{rid}:status", f"cc:{rid}"]:
        u, q = cb(MAID_ID, data, MAID_ID)
        await h.on_callback(u, env.ctx)                     # never raises
        q.answer.assert_awaited()
    assert env.db.get_receipt(rid)["status"] == "pending"
    u, q = cb(STRANGER, f"c:{rid}", STRANGER)
    await h.on_callback(u, env.ctx)
    assert "not authorised" in q.answer.call_args.args[0]


async def test_L10_safe_edit_swallows_not_modified_only():
    fn = AsyncMock(side_effect=BadRequest("Message is not modified: specified new message content ..."))
    assert await h.safe_edit(fn, "x") is None
    fn = AsyncMock(side_effect=BadRequest("Chat not found"))
    with pytest.raises(BadRequest):
        await h.safe_edit(fn, "x")


async def test_L9_L10_on_error_quiet_cases(env, caplog):
    st = env.ctx.application.bot_data["state"]
    ctx = SimpleNamespace(application=env.ctx.application, error=BadRequest("Message is not modified"))
    await h.on_error(None, ctx)
    ctx.error = Conflict("terminated by other getUpdates request")
    await h.on_error(None, ctx)
    await h.on_error(None, ctx)
    assert sum("409 Conflict" in r.message for r in caplog.records) == 1  # rate-limited
    assert st.last_conflict_log > 0
    ctx.error = NetworkError("connection reset")
    await h.on_error(None, ctx)
    assert not any(r.exc_info for r in caplog.records)


# ------------------------------------------------------------------ L11 commands
async def test_L11_void_and_adjust_commands(env):
    env.db.topup(D("200"), ADMIN_ID, "t", NOW)
    rid = new_receipt(env.db, "40")
    env.db.confirm_receipt(rid, MAID_ID, NOW, THR)
    env.ctx.args = [str(rid), "entered", "twice"]
    m = FakeMessage(ADMIN_ID, "/void")
    await h.cmd_void(upd(ADMIN_ID, m), env.ctx)
    assert "voided" in m.replies[-1][0] and "HK$200.00" in m.replies[-1][0]
    m = FakeMessage(ADMIN_ID, "/void")
    await h.cmd_void(upd(ADMIN_ID, m), env.ctx)
    assert "already voided" in m.replies[-1][0]
    for args, expect in [(["abc"], "Usage"), ([], "Usage"), (["99999999999999999999"], "Usage"),
                         (["424242"], "no receipt")]:
        env.ctx.args = args
        m = FakeMessage(ADMIN_ID, "/void")
        await h.cmd_void(upd(ADMIN_ID, m), env.ctx)
        assert expect in m.replies[-1][0], args
    env.ctx.args = ["-20.50", "cash", "count"]
    m = FakeMessage(ADMIN_ID, "/adjust")
    await h.cmd_adjust(upd(ADMIN_ID, m), env.ctx)
    assert "HK$179.50" in m.replies[-1][0]
    assert any("Float adjusted" in t for t in sent_to(env.bot, MAID_ID))
    for args in (["20", "no sign"], ["-20"], ["-0"], ["+1e9", "x"], ["-1000000.01", "x"]):
        env.ctx.args = args
        m = FakeMessage(ADMIN_ID, "/adjust")
        await h.cmd_adjust(upd(ADMIN_ID, m), env.ctx)
        assert "Usage" in m.replies[-1][0], args
    env.ctx.args = []
    m = FakeMessage(ADMIN_ID, "/reconcile")
    await h.cmd_reconcile(upd(ADMIN_ID, m), env.ctx)
    assert "Ledger reconciles" in m.replies[-1][0]
    # the maid can't use admin commands
    env.ctx.args = ["-5", "x"]
    m = FakeMessage(MAID_ID, "/adjust")
    await h.cmd_adjust(upd(MAID_ID, m), env.ctx)
    assert "only the admin" in m.replies[-1][0]
    assert env.db.get_balance()["balance"] == D("179.50")


@pytest.mark.parametrize("text,val", [("-20", "-20"), ("+15.50", "15.50"), ("-HK$1,000", "-1000"),
                                      ("20", None), ("-0", None), ("--5", None), ("-1.234", None)])
def test_L11_parse_signed_amount(text, val):
    assert h.parse_signed_amount(text) == (D(val) if val else None)


# ------------------------------------------------------------------ M5 sweep alert
async def test_M5_sweep_reports_a_ledger_mismatch_once(env):
    st = env.ctx.application.bot_data["state"]
    env.db.topup(D("500"), ADMIN_ID, "t", NOW)
    out = await h.sweep_once(env.bot, st)
    assert out["reconcile"]["ok"] and not sent_to(env.bot, ADMIN_ID)
    c = _admin_conn()
    with c.cursor() as cur:
        cur.execute("UPDATE float_balance SET balance=12345.67 WHERE id=1")
    c.commit()
    c.close()
    await h.sweep_once(env.bot, st)
    await h.sweep_once(env.bot, st)
    msgs = [t for t in sent_to(env.bot, ADMIN_ID) if "does NOT reconcile" in t]
    assert len(msgs) == 1 and "HK$12,345.67" in msgs[0]


# ------------------------------------------------------------------ M4 in the UI
async def test_M4_duplicate_photo_asks_before_deducting(env, tmp_path):
    env.db.topup(D("500"), ADMIN_ID, "t", NOW)
    src = make_receipt_image(tmp_path / "s.png")
    for _ in range(2):
        await h.on_photo(upd(MAID_ID, FakeMessage(MAID_ID, photo=[FakePhoto(src)])), env.ctx)
    first, second = sorted(r["id"] for r in env.db.pending_receipts())
    u, q = cb(MAID_ID, f"c:{first}", MAID_ID)
    await h.on_callback(u, env.ctx)
    u, q = cb(MAID_ID, f"c:{second}", MAID_ID)
    await h.on_callback(u, env.ctx)
    assert f"looks like receipt #{first}" in q.edit_message_text.call_args.args[0]
    assert env.db.get_balance()["balance"] == D("376.60")
    u, q = cb(MAID_ID, f"cc:{second}", MAID_ID)
    await h.on_callback(u, env.ctx)
    assert env.db.get_balance()["balance"] == D("253.20")
