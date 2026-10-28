"""End-to-end handler tests: real MySQL + real Tesseract, Telegram fully mocked."""
import csv
import io
import shutil
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from maid_receipt_bot import handlers as h
from maid_receipt_bot.config import Config
from maid_receipt_bot.ocr import ReceiptReader
from tests.helpers import make_receipt_image
from tests.test_db_integration import ADMIN_ID, MAID_ID, MYSQL_OK, db  # noqa: F401 (fixture)

pytestmark = [pytest.mark.mysql, pytest.mark.tesseract,
              pytest.mark.skipif(not MYSQL_OK, reason="test MySQL not reachable"),
              pytest.mark.skipif(shutil.which("tesseract") is None, reason="tesseract not installed")]
STRANGER = 999


class FakeMessage:
    _next_id = 100

    def __init__(self, chat_id, text=None, photo=None, document=None):
        FakeMessage._next_id += 1
        self.chat_id = chat_id
        self.message_id = FakeMessage._next_id
        self.text = text
        self.photo = photo or []
        self.document = document
        self.replies = []
        self.edits = []
        self.reply_document = AsyncMock()

    async def reply_text(self, text=None, **kw):
        m = FakeMessage(self.chat_id, text)
        self.replies.append((text, kw))
        return m

    async def edit_text(self, text, **kw):
        self.edits.append((text, kw))
        return self


class FakePhoto:
    def __init__(self, src):
        self.file_id, self.file_unique_id, self._src = "FILEID", "UNIQ123", src

    async def get_file(self):
        async def download_to_drive(custom_path):
            shutil.copy(self._src, custom_path)
        return SimpleNamespace(download_to_drive=download_to_drive)


@pytest.fixture
def env(db, tmp_path):  # noqa: F811
    cfg = Config.from_env({"MYSQL_USER": db.cfg.mysql_user, "MYSQL_PASSWORD": db.cfg.mysql_password,
                           "MYSQL_DATABASE": db.cfg.mysql_database, "MYSQL_HOST": db.cfg.mysql_host,
                           "ADMIN_TELEGRAM_ID": str(ADMIN_ID), "MAID_TELEGRAM_ID": str(MAID_ID),
                           "RECEIPT_PHOTO_DIR": str(tmp_path / "photos"), "LOW_FLOAT_THRESHOLD": "100"},
                          require_token=False)
    state = h.BotState(cfg=cfg, db=db, reader=ReceiptReader(70, "eng", None))
    bot = MagicMock()
    bot.send_message = AsyncMock()
    bot.edit_message_text = AsyncMock()
    ctx = SimpleNamespace(application=SimpleNamespace(bot_data={"state": state}), bot=bot, args=[])
    return SimpleNamespace(cfg=cfg, db=db, ctx=ctx, bot=bot, tmp=tmp_path)


def upd(user_id, message=None, callback=None):
    return SimpleNamespace(effective_user=SimpleNamespace(id=user_id, first_name="Ana", username="u"),
                           effective_message=message if message else (callback.message if callback else None),
                           callback_query=callback)


def cb(user_id, data, chat_id):
    q = SimpleNamespace(data=data, answer=AsyncMock(), edit_message_text=AsyncMock(),
                        edit_message_reply_markup=AsyncMock(), message=FakeMessage(chat_id))
    return upd(user_id, callback=q), q


def sent_to(bot, chat_id):
    return [c.kwargs["text"] for c in bot.send_message.call_args_list if c.kwargs["chat_id"] == chat_id]


async def test_full_receipt_flow(env):
    ctx, bot = env.ctx, env.bot
    # Albert tops up 150; maid is notified
    m = FakeMessage(ADMIN_ID, "top up 150")
    await h.on_text(upd(ADMIN_ID, m), ctx)
    assert "HK$150.00" in m.replies[0][0]
    assert any("topped up the float by HK$150.00" in t for t in sent_to(bot, MAID_ID))

    # maid sends a photo -> pending summary with buttons, nothing deducted
    src = make_receipt_image(env.tmp / "src.png")
    pm = FakeMessage(MAID_ID, photo=[FakePhoto(src)])
    await h.on_photo(upd(MAID_ID, pm), ctx)
    rid = env.db.pending_receipts()[0]["id"]
    r = env.db.get_receipt(rid)
    assert r["amount"] == Decimal("123.40") and r["ocr_engine"] == "tesseract"
    now = env.cfg.now()
    assert r["photo_path"].startswith(str(env.tmp / "photos" / f"{now:%Y}" / f"{now:%m}"))
    assert (env.tmp / "photos").exists() and __import__("os").path.exists(r["photo_path"])
    assert env.db.get_balance()["balance"] == Decimal("150.00")

    # Edit -> Amount -> "60" -> re-shown summary
    u, q = cb(MAID_ID, f"e:{rid}", MAID_ID)
    await h.on_callback(u, ctx)
    q.edit_message_text.assert_awaited()
    u, q = cb(MAID_ID, f"f:{rid}:amount", MAID_ID)
    await h.on_callback(u, ctx)
    assert "total amount" in bot.send_message.call_args.kwargs["text"]
    tm = FakeMessage(MAID_ID, "HK$60")
    await h.on_text(upd(MAID_ID, tm), ctx)
    assert "HK$60.00" in tm.replies[-1][0] and tm.replies[-1][1]["reply_markup"] is not None
    assert env.db.get_receipt(rid)["amount"] == Decimal("60.00")

    # Confirm -> 150 - 60 = 90 -> maid gets balance, Albert gets low-float alert
    u, q = cb(MAID_ID, f"c:{rid}", MAID_ID)
    await h.on_callback(u, ctx)
    assert any("New float balance: <b>HK$90.00</b>" in t for t in sent_to(bot, MAID_ID))
    alerts = [t for t in sent_to(bot, ADMIN_ID) if "Low float" in t]
    assert len(alerts) == 1 and "HK$90.00" in alerts[0]

    # second receipt same day -> negative balance, no second alert
    photo2 = FakePhoto(src)
    photo2.file_unique_id = "UNIQ456"  # a different photo (the same one twice = duplicate warning)
    pm2 = FakeMessage(MAID_ID, photo=[photo2])
    await h.on_photo(upd(MAID_ID, pm2), ctx)
    rid2 = env.db.pending_receipts()[0]["id"]
    u, q = cb(MAID_ID, f"c:{rid2}", MAID_ID)
    await h.on_callback(u, ctx)
    assert any("-HK$33.40" in t for t in sent_to(bot, MAID_ID))
    assert len([t for t in sent_to(bot, ADMIN_ID) if "Low float" in t]) == 1

    # double tap on Confirm doesn't deduct twice
    u, q = cb(MAID_ID, f"c:{rid2}", MAID_ID)
    await h.on_callback(u, ctx)
    assert env.db.get_balance()["balance"] == Decimal("-33.40")

    # /balance and /history for the maid
    bm = FakeMessage(MAID_ID, "/balance")
    await h.cmd_balance(upd(MAID_ID, bm), ctx)
    assert "-HK$33.40" in bm.replies[0][0]
    hm = FakeMessage(MAID_ID, "/history")
    await h.cmd_history(upd(MAID_ID, hm), ctx)
    assert hm.replies[0][0].count("🧾") == 2 and "Top-up" in hm.replies[0][0]

    # /export (admin) -> CSV document with 2 rows
    em = FakeMessage(ADMIN_ID, "/export")
    await h.cmd_export(upd(ADMIN_ID, em), ctx)
    kw = em.reply_document.call_args.kwargs
    rows = list(csv.reader(io.StringIO(kw["document"].getvalue().decode("utf-8-sig"))))
    assert rows[0][0] == "receipt_id" and len(rows) == 3 and "total HK$183.40" in kw["caption"]


async def test_cancel_flow(env):
    src = make_receipt_image(env.tmp / "src.png")
    pm = FakeMessage(MAID_ID, photo=[FakePhoto(src)])
    await h.on_photo(upd(MAID_ID, pm), env.ctx)
    rid = env.db.pending_receipts()[0]["id"]
    u, q = cb(MAID_ID, f"x:{rid}", MAID_ID)
    await h.on_callback(u, env.ctx)
    u, q = cb(MAID_ID, f"xx:{rid}", MAID_ID)
    await h.on_callback(u, env.ctx)
    assert env.db.get_receipt(rid)["status"] == "cancelled"
    assert env.db.get_balance()["balance"] == Decimal("0.00")


async def test_permissions(env):
    ctx = env.ctx
    sm = FakeMessage(STRANGER, "top up 1000")
    await h.on_text(upd(STRANGER, sm), ctx)
    assert "private bot" in sm.replies[0][0]
    bm = FakeMessage(STRANGER, "/balance")
    await h.cmd_balance(upd(STRANGER, bm), ctx)
    assert "private bot" in bm.replies[0][0]
    mm = FakeMessage(MAID_ID, "top up 50")
    await h.on_text(upd(MAID_ID, mm), ctx)
    assert "Only Albert" in mm.replies[0][0]
    xm = FakeMessage(MAID_ID, "/export")
    await h.cmd_export(upd(MAID_ID, xm), ctx)
    assert "only the admin" in xm.replies[0][0]
    assert env.db.get_balance()["balance"] == Decimal("0.00")
    u, q = cb(STRANGER, "c:1", STRANGER)
    await h.on_callback(u, ctx)
    q.answer.assert_awaited()


async def test_topup_command_and_bad_amount(env):
    ctx = env.ctx
    ctx.args = ["250.50"]
    m = FakeMessage(ADMIN_ID, "/topup 250.50")
    await h.cmd_topup(upd(ADMIN_ID, m), ctx)
    assert env.db.get_balance()["balance"] == Decimal("250.50")
    m2 = FakeMessage(ADMIN_ID, "top up lots")
    await h.on_text(upd(ADMIN_ID, m2), ctx)
    assert "couldn't read" in m2.replies[0][0]


def test_application_builds_without_network(env):
    from maid_receipt_bot.main import build_application
    from dataclasses import replace
    cfg = replace(env.cfg, telegram_bot_token="123456:TEST")
    app = build_application(cfg, env.db, ReceiptReader(70, "eng", None))
    assert app.bot_data["state"].cfg.admin_id == ADMIN_ID
    assert sum(len(v) for v in app.handlers.values()) == 13
