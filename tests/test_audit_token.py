"""Regression tests for audit H1 (token handling / exit codes, probes/p_token.py) and L9
(single instance). Uses only FAKE tokens and a local fake Bot API (tests/token_harness.py)."""
import fcntl
import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest

from maid_receipt_bot import logsafe, main as m
from tests.test_db_integration import APP_PW, APP_USER, DBNAME, HOST, MYSQL_OK, PORT

ROOT = Path(__file__).resolve().parents[1]
# Assembled at runtime so no token-shaped literal sits in the repo; it is not a real token.
FAKE_TOKEN = "123456789:" + "AAFAKE_TEST_TOKEN_" + "x" * 17
FAKE_SECRETS = (FAKE_TOKEN, "SECRETPW_AUDIT", "sk-AUDITFAKEKEY", FAKE_TOKEN.split(":")[1])


def run_harness(mode, tmp_path, timeout=60):
    env = {k: v for k, v in os.environ.items() if k not in ("TELEGRAM_BOT_TOKEN",)}
    env.update(FAKE_TOKEN=FAKE_TOKEN, BOT_LOCK_FILE=str(tmp_path / "bot.lock"), PYTHONUNBUFFERED="1")
    p = subprocess.run([sys.executable, str(ROOT / "tests" / "token_harness.py"), mode], cwd=ROOT, env=env,
                       capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout + p.stderr


def assert_no_secrets(out):
    for s in FAKE_SECRETS:
        assert s not in out, "a fake secret leaked into the output"


def test_H1_rejected_token_exits_2_without_traceback_or_token(tmp_path):
    code, out = run_harness("401", tmp_path)
    assert code == m.EXIT_CONFIG == 2, out[-2000:]
    assert "Telegram rejected TELEGRAM_BOT_TOKEN" in out
    assert "Traceback" not in out
    assert_no_secrets(out)


def test_N3_network_down_at_boot_keeps_retrying_and_is_redacted(tmp_path):
    """Audit N3: no exit (and so no systemd start-limit) while the network is down."""
    code, out = run_harness("refused", tmp_path)
    assert code == 7, out[-2000:]  # still alive when the harness timer fired
    assert "still retrying after 4s" in out
    assert out.count("Telegram not reachable (attempt") >= 3
    assert "Bot stopped" not in out and "Traceback" not in out
    assert_no_secrets(out)


def test_N3_network_comes_back_after_boot_and_polling_starts(tmp_path):
    code, out = run_harness("down_then_up", tmp_path)
    assert code == 0, out[-3000:]
    assert out.count("Telegram not reachable (attempt") >= 2
    assert "Telegram reachable again after" in out
    assert out.index("fake Telegram API is up now") < out.index("getUpdates reached")
    assert_no_secrets(out)


def test_N3_mysql_down_at_boot_is_waited_for(tmp_path):
    code, out = run_harness("db_down_then_up", tmp_path)
    assert code == 0, out[-3000:]
    assert out.count("MySQL not reachable (attempt") == 2
    assert "MySQL reachable again after 2 failed attempt(s)" in out
    assert "getUpdates reached" in out
    assert_no_secrets(out)


def test_N3_mysql_access_denied_still_fails_fast(tmp_path):
    code, out = run_harness("db_denied", tmp_path)
    assert code == m.EXIT_STARTUP == 3, out[-2000:]
    assert "not reachable" not in out
    assert_no_secrets(out)


def test_N11_unmigrated_database_exits_2(tmp_path):
    code, out = run_harness("schema_old", tmp_path)
    assert code == m.EXIT_CONFIG == 2, out[-2000:]
    assert "migrate" in out.lower()
    assert_no_secrets(out)


def test_L9_conflict_is_logged_rate_limited_and_redacted(tmp_path):
    code, out = run_harness("409", tmp_path)
    assert code == 0, out[-2000:]
    assert out.count("409 Conflict: another bot instance") == 1
    assert_no_secrets(out)


def test_H1_service_does_not_restart_on_config_or_lock_errors():
    unit = (ROOT / "deploy" / "bot-maid-receipt.service").read_text()
    assert "RestartPreventExitStatus=2 4" in unit
    assert "Restart=on-failure" in unit
    # bot-deploy example StartLimit (secondary backstop); in-process N3 backoff waits out boot outages.
    assert "StartLimitIntervalSec=600" in unit and "StartLimitBurst=5" in unit
    assert "RestartSec=15" in unit
    assert "AmbientCapabilities=" not in unit
    assert "Documentation=" not in unit
    assert "/opt/bots/maid-receipt" in unit
    assert "StateDirectory=bots/maid-receipt" in unit


# ------------------------------------------------------------------ Redactor unit tests
@pytest.fixture
def red():
    r = logsafe.Redactor()
    r.add(FAKE_TOKEN, "SECRETPW_AUDIT", "abc")  # "abc" is too short to be masked
    return r


def test_H1_redactor_masks_secrets_urls_and_token_shapes(red):
    assert FAKE_TOKEN not in red(f"POST https://api.telegram.org/bot{FAKE_TOKEN}/getMe")
    assert "SECRETPW_AUDIT" not in red("password=SECRETPW_AUDIT")
    assert "%3A" not in red(FAKE_TOKEN.replace(":", "%3A")) or FAKE_TOKEN.split(":")[1] not in \
        red(FAKE_TOKEN.replace(":", "%3A"))
    other = "987654321:" + "B" * 35  # any token-shaped string, even one we weren't told about
    assert other not in red(f"error {other}")
    assert red("/botSOMETHING/getUpdates") == "/bot[REDACTED]/getUpdates"
    assert red("abc def") == "abc def"
    assert red("receipt 12:30 total 45.50") == "receipt 12:30 total 45.50"


def test_H1_logging_filter_scrubs_args_and_tracebacks(tmp_path, capsys):
    logsafe.REDACTOR.add(FAKE_TOKEN)
    logger = logging.getLogger("test_redact")
    h = logging.StreamHandler(sys.stderr)
    h.addFilter(logsafe.RedactingFilter())
    h.setFormatter(logsafe.RedactingFormatter(logsafe.FORMAT))
    logger.addHandler(h)
    try:
        logger.error("token %s", FAKE_TOKEN)
        try:
            raise RuntimeError(f"bad url /bot{FAKE_TOKEN}/getMe")
        except RuntimeError:
            logger.exception("boom")
    finally:
        logger.removeHandler(h)
    err = capsys.readouterr().err
    assert "boom" in err and "Traceback" in err
    assert FAKE_TOKEN not in err and "[REDACTED]" in err


# ------------------------------------------------------------------ L9 lock / CLI exit codes
def _main_env(tmp_path, **extra):
    env = {k: v for k, v in os.environ.items() if k != "TELEGRAM_BOT_TOKEN"}
    env.update(TELEGRAM_BOT_TOKEN=FAKE_TOKEN, MYSQL_USER=APP_USER, MYSQL_PASSWORD=APP_PW, MYSQL_DATABASE=DBNAME,
               MYSQL_HOST=HOST, MYSQL_PORT=str(PORT), ADMIN_TELEGRAM_ID="1", MAID_TELEGRAM_ID="2",
               MAIDBOT_ENV_FILE="/nonexistent", BOT_LOCK_FILE=str(tmp_path / "bot.lock"),
               RECEIPT_PHOTO_DIR=str(tmp_path / "photos"))
    env.update(extra)
    return env


def run_main(args, env):
    p = subprocess.run([sys.executable, "main.py", *args], cwd=ROOT, env=env, capture_output=True, text=True,
                       timeout=60)
    return p.returncode, p.stdout + p.stderr


def test_L9_second_instance_exits_4(tmp_path):
    lock = tmp_path / "bot.lock"
    fd = os.open(lock, os.O_RDWR | os.O_CREAT)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        code, out = run_main([], _main_env(tmp_path))
    finally:
        os.close(fd)
    assert code == m.EXIT_ALREADY_RUNNING == 4, out[-2000:]
    assert "already running" in out
    assert_no_secrets(out)


def test_L9_lock_is_reusable_after_release(tmp_path):
    assert m.acquire_instance_lock(tmp_path / "a" / "bot.lock")
    assert (tmp_path / "a" / "bot.lock").read_text().strip() == str(os.getpid())
    assert m.acquire_instance_lock(None)


def test_config_error_exits_2(tmp_path):
    code, out = run_main([], _main_env(tmp_path, ADMIN_TELEGRAM_ID="not-a-number"))
    assert code == 2 and "Configuration error" in out
    assert_no_secrets(out)


@pytest.mark.mysql
@pytest.mark.skipif(not MYSQL_OK, reason="test MySQL not reachable")
def test_M5_reconcile_cli_exit_codes(tmp_path, db):  # noqa: F811
    code, out = run_main(["--reconcile"], _main_env(tmp_path))
    assert code == 0 and "Ledger reconciles" in out, out[-2000:]
    from tests.test_db_integration import _admin_conn
    c = _admin_conn()
    with c.cursor() as cur:
        cur.execute("UPDATE float_balance SET balance=1.23 WHERE id=1")
    c.commit()
    c.close()
    code, out = run_main(["--reconcile"], _main_env(tmp_path))
    assert code == 1 and "RECONCILE:" in out
    code, out = run_main(["--reconcile"], _main_env(tmp_path, MYSQL_PASSWORD="SECRETPW_AUDIT"))
    assert code == 3 and "Reconciliation could not run" in out
    assert_no_secrets(out)


from tests.test_db_integration import db  # noqa: E402,F401  (fixture)
