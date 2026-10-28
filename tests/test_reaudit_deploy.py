"""Re-audit round 2: backup / restore (N7) and the migration runner (N11)."""
import os
import signal
import subprocess
import time

import pymysql
import pytest

from tests.test_audit_deploy import (ROOT, _reload, _shape, conn, defaults, needs_dump, sh)  # noqa: F401
from tests.test_db_integration import ADMIN_ID, DBNAME, MAID_ID, MYSQL_OK, NOW, THR, db, new_receipt  # noqa: F401

def _fake_dump(tmp_path, sleep=0):
    f = tmp_path / "fakedump"
    f.write_text(f"#!/bin/sh\nsleep {sleep}\necho 'CREATE TABLE x (id int);'\necho '-- Dump completed on 2026'\n")
    f.chmod(0o755)
    return f


# ------------------------------------------------------------------ N7 backup.sh
def test_N7a_sigterm_mid_backup_fails_loudly_and_leaves_nothing(tmp_path):
    dest, ph = tmp_path / "b", tmp_path / "ph"
    ph.mkdir()
    alert = tmp_path / "alert.txt"
    env = {**os.environ, "BACKUP_CONF": "/nonexistent", "MYSQLDUMP": str(_fake_dump(tmp_path, 2)),
           "BACKUP_DIR": str(dest), "PHOTO_DIR": str(ph), "ALERT_COMMAND": f"cat > {alert}"}
    env.pop("TELEGRAM_BOT_TOKEN", None)
    p = subprocess.Popen(["bash", str(ROOT / "deploy" / "backup.sh")], env=env, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, text=True)
    time.sleep(0.8)
    p.send_signal(signal.SIGTERM)
    out, _ = p.communicate(timeout=30)
    assert p.returncode == 143, out
    assert "backup FAILED" in out and "backup FAILED" in alert.read_text()
    assert list(dest.iterdir()) == []          # no temp file, no "completed" dump


def test_N7b_stale_temp_files_from_a_killed_run_are_pruned(tmp_path):
    dest = tmp_path / "b"
    dest.mkdir()
    stale_db, stale_ph, fresh = dest / ".db-20260101.sql.gz.XXXX", dest / ".photos-20260101.XXXX", dest / ".db-now"
    for f in (stale_db, stale_ph, fresh):
        f.write_bytes(b"partial")
    for f in (stale_db, stale_ph):
        os.utime(f, (time.time() - 2 * 86400,) * 2)
    p = sh("backup.sh", dict(BACKUP_CONF="/nonexistent", MYSQLDUMP="false", BACKUP_DIR=str(dest),
                             PHOTO_DIR=str(tmp_path), ALERT_COMMAND="cat >/dev/null"))
    assert p.returncode != 0                     # the dump itself failed ...
    assert not stale_db.exists() and not stale_ph.exists()   # ... but old leftovers are gone
    assert fresh.exists()                        # a possibly-running backup's temp file is kept


def test_N7c_missing_photo_dir_fails_before_the_dump_unless_allowed(tmp_path):
    alert = tmp_path / "alert.txt"
    env = dict(BACKUP_CONF="/nonexistent", MYSQLDUMP=str(_fake_dump(tmp_path)), BACKUP_DIR=str(tmp_path / "b"),
               PHOTO_DIR=str(tmp_path / "typo"), ALERT_COMMAND=f"cat > {alert}")
    p = sh("backup.sh", env)
    assert p.returncode == 1 and "photo dir" in p.stdout and "ALLOW_MISSING_PHOTO_DIR" in p.stdout
    assert "backup FAILED" in alert.read_text()
    assert not list((tmp_path / "b").glob("db-*"))              # failed before dumping
    ok = sh("backup.sh", {**env, "ALLOW_MISSING_PHOTO_DIR": "1"})
    assert ok.returncode == 0, ok.stdout + ok.stderr
    assert "warning: photo dir" in ok.stdout


def test_N7_bad_photo_archive_is_rejected_before_the_dump(tmp_path):
    p = sh("backup.sh", dict(BACKUP_CONF="/nonexistent", MYSQLDUMP="false", BACKUP_DIR=str(tmp_path / "b"),
                             PHOTO_DIR=str(tmp_path), PHOTO_ARCHIVE="weekly", ALERT_COMMAND="cat >/dev/null"))
    assert p.returncode == 2 and "PHOTO_ARCHIVE must be" in p.stdout
    assert "mysqldump" not in p.stdout


@pytest.mark.parametrize("line_fmt", ["export TELEGRAM_BOT_TOKEN={t}\nexport ADMIN_TELEGRAM_ID=1001\n",
                                      'TELEGRAM_BOT_TOKEN="{t}"\r\nADMIN_TELEGRAM_ID=1001\r\n'])
def test_N7f_telegram_alert_understands_export_and_quoted_lines(tmp_path, line_fmt):
    fake_token = "123456789:" + "AAFAKE_BACKUP_TEST_" + "z" * 16
    envf = tmp_path / "env"
    envf.write_text(line_fmt.format(t=fake_token))
    bindir = tmp_path / "bin"
    bindir.mkdir()
    rec = tmp_path / "curl.txt"
    (bindir / "curl").write_text(f"#!/bin/sh\necho \"ARGS: $*\" > {rec}\necho 'STDIN:' >> {rec}\ncat >> {rec}\n")
    (bindir / "curl").chmod(0o755)
    p = sh("backup.sh", dict(BACKUP_CONF="/nonexistent", MYSQLDUMP="false", BACKUP_DIR=str(tmp_path / "b"),
                             PHOTO_DIR=str(tmp_path), ALERT_TELEGRAM="1", BOT_ENV=str(envf),
                             PATH=f"{bindir}:{os.environ['PATH']}"))
    got = rec.read_text()
    assert "chat_id=1001" in got.split("STDIN:")[0] and fake_token not in got.split("STDIN:")[0]
    assert f'bot{fake_token}/sendMessage"' in got.split("STDIN:")[1]   # no quote / CR residue
    assert fake_token not in p.stdout + p.stderr


def test_N7f_unconfigured_telegram_alert_is_logged(tmp_path):
    envf = tmp_path / "env"
    envf.write_text("ADMIN_TELEGRAM_ID=1001\n")
    p = sh("backup.sh", dict(BACKUP_CONF="/nonexistent", MYSQLDUMP="false", BACKUP_DIR=str(tmp_path / "b"),
                             PHOTO_DIR=str(tmp_path), ALERT_TELEGRAM="1", BOT_ENV=str(envf)))
    assert p.returncode != 0 and "telegram alert not configured" in p.stdout
    p = sh("backup.sh", dict(BACKUP_CONF="/nonexistent", MYSQLDUMP="false", BACKUP_DIR=str(tmp_path / "b"),
                             PHOTO_DIR=str(tmp_path), ALERT_TELEGRAM="1", BOT_ENV=str(tmp_path / "missing")))
    assert "telegram alert not configured" in p.stdout and "not readable" in p.stdout


# ------------------------------------------------------------------ N7 restore_test.sh
@pytest.mark.mysql
@needs_dump
@pytest.mark.skipif(not MYSQL_OK, reason="test MySQL not reachable")
def test_N7d_restore_requires_the_checksum_file(db, tmp_path, defaults):  # noqa: F811
    db.topup(__import__("decimal").Decimal("100"), ADMIN_ID, "t", NOW)
    dest = tmp_path / "b"
    p = sh("backup.sh", dict(BACKUP_CONF="/nonexistent", DB_NAME=DBNAME, MYSQL_DEFAULTS=str(defaults),
                             PHOTO_DIR=str(tmp_path), BACKUP_DIR=str(dest)))
    assert p.returncode == 0, p.stdout + p.stderr
    for f in dest.glob("SHA256SUMS-*"):
        f.unlink()
    env = dict(BACKUP_DIR=str(dest), RESTORE_DB="maidbot_test_restore", DB_NAME=DBNAME, MYSQL_DEFAULTS=str(defaults))
    r = sh("restore_test.sh", env)
    assert r.returncode == 1 and "checksum file" in r.stderr and "restore drill OK" not in r.stdout
    r = sh("restore_test.sh", env, "--no-checksum")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "integrity NOT verified" in r.stderr and "restore drill OK" in r.stdout
    assert "schema_version=1" in r.stdout


@pytest.mark.mysql
@needs_dump
@pytest.mark.skipif(not MYSQL_OK, reason="test MySQL not reachable")
def test_N7e_restore_drill_handles_a_pre_migration_dump(tmp_path, defaults):  # noqa: F811
    mc = conn("maidbot_test_migrate")
    _reload(mc, (ROOT / "tests" / "fixtures" / "schema_v1_0.sql").read_text())
    with mc.cursor() as cur:
        cur.execute("INSERT INTO topups (amount, admin_telegram_id, balance_before, balance_after, created_at) "
                    "VALUES (500, 1001, 0, 500, '2026-10-01 09:00')")
        cur.execute("UPDATE float_balance SET balance=500 WHERE id=1")
        cur.execute("INSERT INTO audit_log (event_time, event_type, actor_telegram_id, topup_id, amount) "
                    "VALUES ('2026-10-01 09:00','topup',1001,1,500)")
    mc.close()
    dest = tmp_path / "b"
    p = sh("backup.sh", dict(BACKUP_CONF="/nonexistent", DB_NAME="maidbot_test_migrate",
                             MYSQL_DEFAULTS=str(defaults), PHOTO_DIR=str(tmp_path), BACKUP_DIR=str(dest)))
    assert p.returncode == 0, p.stdout + p.stderr
    r = sh("restore_test.sh", dict(BACKUP_DIR=str(dest), RESTORE_DB="maidbot_test_restore",
                                   DB_NAME="maidbot_test_migrate", MYSQL_DEFAULTS=str(defaults)))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "predates migration 001" in r.stdout and "restore drill OK" in r.stdout
    assert "schema_version=none stored balance=500.00 derived balance=500.00" in r.stdout


# ------------------------------------------------------------------ N11 migrate.sh
def _versions(c):
    with c.cursor() as cur:
        cur.execute("SELECT version, name FROM schema_version ORDER BY version")
        return cur.fetchall()


def _wipe(c):
    with c.cursor() as cur:
        cur.execute("SET FOREIGN_KEY_CHECKS=0")
        cur.execute("SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE()")
        for (t,) in cur.fetchall():
            cur.execute(f"DROP TABLE `{t}`")
        cur.execute("SET FOREIGN_KEY_CHECKS=1")


@pytest.mark.mysql
@needs_dump
@pytest.mark.skipif(not MYSQL_OK, reason="test MySQL not reachable")
def test_N11_migrate_is_idempotent_and_records_version(tmp_path, defaults):  # noqa: F811
    mc = conn("maidbot_test_migrate")
    _wipe(mc)
    env = dict(DB_NAME="maidbot_test_migrate", MYSQL_DEFAULTS=str(defaults))
    st = sh("migrate.sh", env, "--status")
    assert st.returncode == 0 and "schema version 0; code ships up to 1" in st.stdout
    p = sh("migrate.sh", env)
    assert p.returncode == 0, p.stdout + p.stderr
    assert [v for v, _ in _versions(mc)] == [1]
    assert any("001_initial" in n for _, n in _versions(mc))
    shape = _shape(mc)
    again = sh("migrate.sh", env)
    assert again.returncode == 0, again.stdout + again.stderr
    assert "schema version 1" in again.stdout and "0 migration(s) applied" in again.stdout
    assert _shape(mc) == shape and [v for v, _ in _versions(mc)] == [1]
    mc.close()


@pytest.mark.mysql
@needs_dump
@pytest.mark.skipif(not MYSQL_OK, reason="test MySQL not reachable")
def test_N11_detected_full_schema_without_version_table_is_recorded(tmp_path, defaults):  # noqa: F811
    """A DB that already has the current columns but no schema_version is treated as version 1."""
    mc = conn("maidbot_test_migrate")
    _reload(mc, (ROOT / "migrations" / "001_initial.sql").read_text())
    with mc.cursor() as cur:
        cur.execute("DROP TABLE schema_version")
    p = sh("migrate.sh", dict(DB_NAME="maidbot_test_migrate", MYSQL_DEFAULTS=str(defaults)))
    assert p.returncode == 0, p.stdout + p.stderr
    assert "detected version 1" in p.stdout
    assert "0 migration(s) applied" in p.stdout or "schema version now 1" in p.stdout
    assert [v for v, _ in _versions(mc)] == [1]
    mc.close()


@pytest.mark.mysql
@needs_dump
@pytest.mark.skipif(not MYSQL_OK, reason="test MySQL not reachable")
def test_N11_fresh_install_needs_no_migration_and_bot_sees_version_1(db, defaults):  # noqa: F811
    p = sh("migrate.sh", dict(DB_NAME=DBNAME, MYSQL_DEFAULTS=str(defaults)))
    assert p.returncode == 0, p.stdout + p.stderr
    assert "schema version 1; code ships up to 1" in p.stdout and "0 migration(s) applied" in p.stdout
    assert db.schema_version() == 1                    # as the least-privilege bot user


def test_N11_migrate_refuses_bad_db_name():
    p = sh("migrate.sh", dict(DB_NAME="x; DROP DATABASE y"))
    assert p.returncode == 2 and "bad DB_NAME" in p.stderr
