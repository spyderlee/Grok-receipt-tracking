"""Regression tests for the audit's deployment findings: M6 (backups + restore drill),
L11 (migrations/001_initial + migrate.sh), L7 (systemd unit), M8 (vision staging tool), pinned deps."""
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import date
from decimal import Decimal as D
from pathlib import Path

import httpx
import pymysql
import pytest

from maid_receipt_bot.config import Config
from maid_receipt_bot.db import Database
from maid_receipt_bot.sqlscript import split_sql
from tests.helpers import make_receipt_image
from tests.test_db_integration import (ADMIN_ID, MAID_ID, MYSQL_OK, NOW, PORT, HOST, THR, DBNAME,  # noqa
                                       db, new_receipt)

ROOT = Path(__file__).resolve().parents[1]
ADMIN_USER, ADMIN_PW = "maidbot_test", "testpw_maidbot_admin"
needs_mysql = [pytest.mark.mysql, pytest.mark.skipif(not MYSQL_OK, reason="test MySQL not reachable")]
needs_dump = pytest.mark.skipif(shutil.which("mysqldump") is None or shutil.which("mysql") is None,
                                reason="mysql client tools not installed")


def conn(database):
    return pymysql.connect(host=HOST, port=PORT, user=ADMIN_USER, password=ADMIN_PW, database=database,
                           autocommit=True)


@pytest.fixture
def defaults(tmp_path):
    p = tmp_path / "my.cnf"
    p.write_text(f"[client]\nuser={ADMIN_USER}\npassword={ADMIN_PW}\nhost={HOST}\nport={PORT}\n")
    p.chmod(0o600)
    return p


def sh(script, env, *args):
    e = {**os.environ, **env}
    e.pop("TELEGRAM_BOT_TOKEN", None)
    return subprocess.run(["bash", str(ROOT / "deploy" / script), *args], env=e, capture_output=True, text=True,
                          timeout=120)


# ------------------------------------------------------------------ M6 backup + restore
@pytest.mark.usefixtures("db")
@needs_dump
@pytest.mark.mysql
@pytest.mark.skipif(not MYSQL_OK, reason="test MySQL not reachable")
def test_M6_backup_verify_offhost_restore_drill(db, tmp_path, defaults):  # noqa: F811
    db.topup(D("300"), ADMIN_ID, "t", NOW)
    db.confirm_receipt(new_receipt(db, "45.50"), MAID_ID, NOW, THR)
    db.adjust(D("-1.25"), ADMIN_ID, "count", NOW)
    photos = tmp_path / "photos" / "2026" / "10"
    photos.mkdir(parents=True)
    (photos / "r1.jpg").write_bytes(b"jpeg")
    dest, remote = tmp_path / "backups", tmp_path / "offhost"
    remote.mkdir()
    dest.mkdir()
    old = dest / "db-20200101-000000.sql.gz"
    old.write_bytes(b"old")
    os.utime(old, (time.time() - 90 * 86400,) * 2)
    unrelated = dest / "notes.txt"
    unrelated.write_text("keep me")
    os.utime(unrelated, (time.time() - 90 * 86400,) * 2)
    env = dict(BACKUP_CONF="/nonexistent", DB_NAME=DBNAME, MYSQL_DEFAULTS=str(defaults),
               PHOTO_DIR=str(tmp_path / "photos"), BACKUP_DIR=str(dest), BACKUP_REMOTE=str(remote), KEEP_DAYS="60")
    p = sh("backup.sh", env)
    assert p.returncode == 0, p.stdout + p.stderr
    assert "backup ok" in p.stdout
    dumps = [d for d in dest.glob("db-*.sql.gz") if d != old]
    assert len(dumps) == 1 and not old.exists() and unrelated.exists()   # pruned only our own old files
    assert len(list(dest.glob("photos-*.tar.gz"))) == 1 and len(list(dest.glob("SHA256SUMS-*"))) == 1
    assert not list(dest.glob(".*"))                                      # no temp files left
    assert oct(dest.stat().st_mode & 0o777) == "0o700" and oct(dumps[0].stat().st_mode & 0o777) == "0o600"
    assert sorted(x.name for x in remote.iterdir() if x.name != "photos") == \
        sorted(x.name for x in dest.iterdir() if x != unrelated)
    assert (remote / "photos" / "2026" / "10" / "r1.jpg").read_bytes() == b"jpeg"   # incremental mirror
    assert ADMIN_PW not in p.stdout + p.stderr

    r = sh("restore_test.sh", dict(BACKUP_DIR=str(dest), RESTORE_DB="maidbot_test_restore", DB_NAME=DBNAME,
                                   MYSQL_DEFAULTS=str(defaults)))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "checksum ok" in r.stdout and "restore drill OK" in r.stdout
    assert "stored balance=253.25 derived balance=253.25" in r.stdout
    assert re.search(r"triggers=([1-9]\d*)", r.stdout)


@needs_dump
@pytest.mark.mysql
@pytest.mark.skipif(not MYSQL_OK, reason="test MySQL not reachable")
def test_M6_photo_archive_none_mirrors_only_new_photos(db, tmp_path, defaults):  # noqa: F811
    """Audit M6 'photos get a full tar every night': PHOTO_ARCHIVE=none skips the nightly tar and the
    off-host mirror copies only new files (remote copies are never overwritten or deleted)."""
    ph = tmp_path / "photos"
    ph.mkdir()
    (ph / "a.jpg").write_bytes(b"A")
    dest, remote = tmp_path / "b", tmp_path / "r"
    remote.mkdir()
    env = dict(BACKUP_CONF="/nonexistent", DB_NAME=DBNAME, MYSQL_DEFAULTS=str(defaults), PHOTO_DIR=str(ph),
               BACKUP_DIR=str(dest), BACKUP_REMOTE=str(remote), PHOTO_ARCHIVE="none")
    p = sh("backup.sh", env)
    assert p.returncode == 0, p.stdout + p.stderr
    assert not list(dest.glob("photos-*")) and len(list(dest.glob("db-*.sql.gz"))) == 1
    sums = next(dest.glob("SHA256SUMS-*")).read_text()
    assert "db-" in sums and "photos-" not in sums
    (remote / "photos" / "a.jpg").write_bytes(b"remote copy")   # must not be overwritten
    (ph / "b.jpg").write_bytes(b"B")
    time.sleep(1.1)                                              # new STAMP
    assert sh("backup.sh", env).returncode == 0
    assert (remote / "photos" / "a.jpg").read_bytes() == b"remote copy"
    assert (remote / "photos" / "b.jpg").read_bytes() == b"B"
    bad = sh("backup.sh", {**env, "PHOTO_ARCHIVE": "weekly"})
    assert bad.returncode == 2


@needs_dump
def test_M6_restore_refuses_live_db(tmp_path):
    r = sh("restore_test.sh", dict(BACKUP_DIR=str(tmp_path), RESTORE_DB="bot_maid_receipt", DB_NAME="bot_maid_receipt"))
    assert r.returncode == 2 and "refusing" in r.stderr
    r = sh("restore_test.sh", dict(BACKUP_DIR=str(tmp_path), RESTORE_DB="x`; DROP DATABASE y; --"))
    assert r.returncode == 2 and "bad RESTORE_DB" in r.stderr


@needs_dump
@pytest.mark.mysql
@pytest.mark.skipif(not MYSQL_OK, reason="test MySQL not reachable")
def test_M6_restore_detects_tampered_dump(db, tmp_path, defaults):  # noqa: F811
    db.topup(D("100"), ADMIN_ID, "t", NOW)
    dest = tmp_path / "b"
    p = sh("backup.sh", dict(BACKUP_CONF="/nonexistent", DB_NAME=DBNAME, MYSQL_DEFAULTS=str(defaults),
                             PHOTO_DIR=str(tmp_path / "none"), BACKUP_DIR=str(dest),
                             ALLOW_MISSING_PHOTO_DIR="1"))
    assert p.returncode == 0, p.stderr
    assert "BACKUP_REMOTE not set" in p.stdout and "photo dir" in p.stdout
    dump = next(dest.glob("db-*.sql.gz"))
    dump.write_bytes(dump.read_bytes()[:-8] + b"\0" * 8)
    r = sh("restore_test.sh", dict(BACKUP_DIR=str(dest), RESTORE_DB="maidbot_test_restore", DB_NAME=DBNAME,
                                   MYSQL_DEFAULTS=str(defaults)))
    assert r.returncode != 0 and "restore drill OK" not in r.stdout


def test_M6_failed_dump_leaves_no_partial_files_no_prune_and_alerts(tmp_path):
    dest = tmp_path / "b"
    dest.mkdir()
    old = dest / "db-20200101-000000.sql.gz"
    old.write_bytes(b"old")
    os.utime(old, (time.time() - 90 * 86400,) * 2)
    alert_file = tmp_path / "alert.txt"
    p = sh("backup.sh", dict(BACKUP_CONF="/nonexistent", MYSQLDUMP="false", BACKUP_DIR=str(dest),
                             PHOTO_DIR=str(tmp_path), KEEP_DAYS="1", ALERT_COMMAND=f"cat > {alert_file}"))
    assert p.returncode != 0
    assert "backup FAILED" in p.stdout and "mysqldump" in p.stdout
    assert old.exists()                                      # nothing pruned after a failure
    assert sorted(x.name for x in dest.iterdir()) == [old.name]  # no temp / partial files
    assert "backup FAILED" in alert_file.read_text() and "mysqldump" in alert_file.read_text()


def test_M6_truncated_dump_is_rejected(tmp_path):
    fake = tmp_path / "fakedump"
    fake.write_text("#!/bin/sh\necho 'CREATE TABLE x (id int);'\n")   # no '-- Dump completed' footer
    fake.chmod(0o755)
    dest = tmp_path / "b"
    p = sh("backup.sh", dict(BACKUP_CONF="/nonexistent", MYSQLDUMP=str(fake), BACKUP_DIR=str(dest),
                             PHOTO_DIR=str(tmp_path), ALERT_COMMAND="cat >/dev/null"))
    assert p.returncode != 0 and "verify db dump" in p.stdout
    assert not list(dest.iterdir())


def test_M6_telegram_alert_never_puts_the_token_on_a_command_line(tmp_path):
    """The failure alert reads the bot env file and passes the token to curl on stdin."""
    fake_token = "123456789:" + "AAFAKE_BACKUP_TEST_" + "y" * 16
    envf = tmp_path / "env"
    envf.write_text(f"TELEGRAM_BOT_TOKEN={fake_token}\nADMIN_TELEGRAM_ID=1001\n")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    rec = tmp_path / "curl.txt"
    (bindir / "curl").write_text(f"#!/bin/sh\necho \"ARGS: $*\" > {rec}\necho 'STDIN:' >> {rec}\ncat >> {rec}\n")
    (bindir / "curl").chmod(0o755)
    p = sh("backup.sh", dict(BACKUP_CONF="/nonexistent", MYSQLDUMP="false", BACKUP_DIR=str(tmp_path / "b"),
                             PHOTO_DIR=str(tmp_path), ALERT_TELEGRAM="1", BOT_ENV=str(envf),
                             PATH=f"{bindir}:{os.environ['PATH']}"))
    assert p.returncode != 0
    got = rec.read_text()
    args_line = got.split("STDIN:")[0]
    assert fake_token not in args_line and "chat_id=1001" in args_line and "-K -" in args_line
    assert fake_token in got.split("STDIN:")[1]               # only via the stdin config
    assert fake_token not in p.stdout + p.stderr


# ------------------------------------------------------------------ L11 migration
def _shape(c):
    with c.cursor() as cur:
        cur.execute("SELECT TABLE_NAME, COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, COLUMN_DEFAULT, EXTRA "
                    "FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE()")
        cols = set(cur.fetchall())
        cur.execute("SELECT TABLE_NAME, INDEX_NAME, SEQ_IN_INDEX, COLUMN_NAME, NON_UNIQUE "
                    "FROM information_schema.STATISTICS WHERE TABLE_SCHEMA=DATABASE()")
        idx = set(cur.fetchall())
        cur.execute("SELECT TRIGGER_NAME, EVENT_MANIPULATION, EVENT_OBJECT_TABLE, ACTION_TIMING, ACTION_STATEMENT "
                    "FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA=DATABASE()")
        trg = {(a, b, c_, d, " ".join(e.split())) for a, b, c_, d, e in cur.fetchall()}
        cur.execute("SELECT TABLE_NAME, CONSTRAINT_NAME, REFERENCED_TABLE_NAME FROM "
                    "information_schema.REFERENTIAL_CONSTRAINTS WHERE CONSTRAINT_SCHEMA=DATABASE()")
        fks = set(cur.fetchall())
    return cols, idx, trg, fks


def _reload(c, schema_text):
    with c.cursor() as cur:
        cur.execute("SET FOREIGN_KEY_CHECKS=0")
        cur.execute("SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE()")
        for (t,) in cur.fetchall():
            cur.execute(f"DROP TABLE `{t}`")
        cur.execute("SET FOREIGN_KEY_CHECKS=1")
        for s in split_sql(schema_text):
            cur.execute(s)


@needs_dump
@pytest.mark.mysql
@pytest.mark.skipif(not MYSQL_OK, reason="test MySQL not reachable")
def test_L11_migrate_applies_001_initial_on_empty_db_matching_fresh_load(tmp_path, defaults):
    """bot-deploy Phase A: a single 001_initial.sql is the whole schema. Empty DB + migrate.sh
    must match loading that file directly, and the ledger must work afterwards."""
    try:
        mc = conn("maidbot_test_migrate")
    except pymysql.err.MySQLError as exc:
        pytest.skip(f"maidbot_test_migrate not set up (run tests/setup_test_db.sh): {exc}")
    with mc.cursor() as cur:
        cur.execute("SET FOREIGN_KEY_CHECKS=0")
        cur.execute("SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE()")
        for (t,) in cur.fetchall():
            cur.execute(f"DROP TABLE `{t}`")
        cur.execute("SET FOREIGN_KEY_CHECKS=1")
    p = sh("migrate.sh", dict(DB_NAME="maidbot_test_migrate", MYSQL_DEFAULTS=str(defaults)))
    assert p.returncode == 0, p.stdout + p.stderr
    assert "empty database" in p.stdout and "applying 001_initial.sql" in p.stdout
    assert "schema version now 1 (1 migration(s) applied)" in p.stdout
    migrated = _shape(mc)
    fc = conn(DBNAME)
    _reload(fc, (ROOT / "migrations" / "001_initial.sql").read_text())
    fresh = _shape(fc)
    fc.close()
    for name, a, b in zip(("columns", "indexes", "triggers", "foreign keys"), migrated, fresh):
        assert a == b, f"{name} differ: only-migrated={a - b} only-fresh={b - a}"

    cfg = Config.from_env({"MYSQL_HOST": HOST, "MYSQL_PORT": str(PORT), "MYSQL_USER": ADMIN_USER,
                           "MYSQL_PASSWORD": ADMIN_PW, "MYSQL_DATABASE": "maidbot_test_migrate",
                           "ADMIN_TELEGRAM_ID": str(ADMIN_ID), "MAID_TELEGRAM_ID": str(MAID_ID)},
                          require_token=False)
    mdb = Database(cfg)
    assert mdb.schema_version() == 1
    assert mdb.reconcile()["ok"], mdb.reconcile()
    mdb.topup(D("500"), ADMIN_ID, "open", NOW)
    rid = new_receipt(mdb, "45.50")
    res = mdb.confirm_receipt(rid, MAID_ID, NOW, THR)
    assert res.status == "confirmed" and res.balance_after == D("454.50")
    assert mdb.reconcile()["ok"]
    with mc.cursor() as cur:
        with pytest.raises(pymysql.err.MySQLError):
            cur.execute("UPDATE receipts SET amount=1 WHERE status='confirmed'")
    mc.close()





# ------------------------------------------------------------------ L7 systemd unit / requirements
UNIT = ROOT / "deploy" / "bot-maid-receipt.service"


def test_L7_unit_hardening_directives():
    text = UNIT.read_text()
    for d in ("NoNewPrivileges=true", "ProtectSystem=strict", "ProtectHome=true", "PrivateTmp=true",
              "CapabilityBoundingSet=\n", "SystemCallFilter=@system-service", "RestrictAddressFamilies=",
              "MemoryDenyWriteExecute=true", "ProtectProc=invisible", "UMask=0027",
              "Environment=BOT_TIMEZONE=Asia/Hong_Kong", "Environment=BOT_LOCK_FILE=", "EnvironmentFile="):
        assert d in text, d
    assert "TELEGRAM_BOT_TOKEN" not in text  # secrets only via EnvironmentFile


@pytest.mark.skipif(shutil.which("systemd-analyze") is None, reason="systemd-analyze not installed")
def test_L7_unit_security_score(tmp_path):
    p = subprocess.run(["systemd-analyze", "security", "--offline=true", str(UNIT)], capture_output=True,
                       text=True, timeout=60)
    m = re.search(r"Overall exposure level for \S+: (\d+\.\d)", p.stdout + p.stderr)
    assert m, p.stdout + p.stderr
    assert float(m.group(1)) <= 2.0


def test_requirements_are_pinned():
    lines = [ln.split("#")[0].strip() for ln in (ROOT / "requirements.txt").read_text().splitlines()]
    reqs = [ln for ln in lines if ln and not ln.startswith("-")]
    assert reqs and all(re.fullmatch(r"[A-Za-z0-9_.\-\[\]]+==[\w.]+", r) for r in reqs), reqs
    assert any(r.startswith("pillow-heif==") for r in reqs)


# ------------------------------------------------------------------ M8 vision staging tool
from tools import vision_staging_test as vst  # noqa: E402
from maid_receipt_bot.ocr import ReceiptReader  # noqa: E402
from maid_receipt_bot.vision import VisionClient  # noqa: E402

needs_tess = pytest.mark.skipif(shutil.which("tesseract") is None, reason="tesseract not installed")


def _staging_folder(tmp_path):
    folder = tmp_path / "receipts"
    folder.mkdir()
    make_receipt_image(folder / "good.png")
    make_receipt_image(folder / "nototal.png", ["CORNER SHOP", "Milk           20.00", "Eggs   30.00"])
    (folder / "notes.txt").write_text("ignored")
    exp = tmp_path / "expected.csv"
    exp.write_text("file,amount,date\ngood.png,123.40,2026-10-26\nnototal.png,50.00,\n")
    return folder, exp


@needs_tess
def test_M8_staging_tool_offline_reports_and_exit_code(tmp_path, capsys):
    folder, exp = _staging_folder(tmp_path)
    env_before = dict(os.environ)
    code = vst.main([str(folder), "--expected", str(exp)])
    out = capsys.readouterr()
    assert code == 1                                  # no vision offline -> no-TOTAL receipt is wrong
    rows = {r.split(",")[0]: r for r in out.out.splitlines()[1:] if r}
    assert set(rows) == {"good.png", "nototal.png"}
    assert ",123.40,123.40,True," in rows["good.png"]
    assert "no TOTAL line" in rows["nototal.png"]
    assert "escalated by the bot's rules" in out.err
    assert dict(os.environ) == env_before


def test_M8_staging_tool_usage_errors(tmp_path, monkeypatch):
    monkeypatch.delenv("VISION_API_KEY", raising=False)
    assert vst.main([str(tmp_path / "missing")]) == 2
    assert vst.main([str(tmp_path), "--live"]) == 2            # needs a key
    assert vst.main([str(tmp_path), "--force-vision"]) == 2    # needs --live
    assert vst.main([str(tmp_path)]) == 2                      # no images


@needs_tess
async def test_M8_staging_tool_with_mocked_vision(tmp_path):
    folder, exp = _staging_folder(tmp_path)
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(
            {"amount": "50.00", "date": None, "merchant": "Corner Shop", "items": "Milk, Eggs"})}}]})
    v = VisionClient("fake-key", "m", "https://vision.invalid/v1", transport=httpx.MockTransport(handler))
    rows = await vst.run(vst.find_images(folder), vst.load_expected(exp), ReceiptReader(70, "eng", v),
                         date(2026, 10, 27))
    assert len(calls) == 1                                     # only the receipt the bot would escalate
    by = {r.file: r for r in rows}
    assert by["nototal.png"].final.engine == "vision" and by["nototal.png"].amount_ok
    assert by["good.png"].amount_ok and by["good.png"].date_ok
    assert vst.report(rows, io.StringIO()) == 0
    rows = await vst.run(vst.find_images(folder), vst.load_expected(exp), ReceiptReader(70, "eng", v),
                         date(2026, 10, 27), force_vision=True)
    assert len(calls) == 3 and rows[0].forced_vision is not None
