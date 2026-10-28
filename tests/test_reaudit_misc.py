"""Re-audit round 2, no database: N8 (logging / config), N9 (integer totals), N4 quiet hours."""
from tests.helpers import run_coro
import subprocess
import sys
import textwrap
from datetime import date, datetime
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace

import pytest

from maid_receipt_bot import ocr
from maid_receipt_bot.config import Config, ConfigError, parse_quiet_hours
from maid_receipt_bot.parsing import find_amount, parse_receipt_text

ROOT = Path(__file__).resolve().parents[1]
BASE = {"MYSQL_USER": "u", "MYSQL_PASSWORD": "secretpw_long_enough", "MYSQL_DATABASE": "d",
        "ADMIN_TELEGRAM_ID": "1", "MAID_TELEGRAM_ID": "2"}
NOFLAG = "total has no decimal point (check it is not missing, e.g. 4560 for 45.60)"


# ------------------------------------------------------------------ N8 config
@pytest.mark.parametrize("pw", ["short", "elevenchars"])
def test_N8_short_db_password_is_refused(pw):
    with pytest.raises(ConfigError, match="at least 12 characters"):
        Config.from_env({**BASE, "MYSQL_PASSWORD": pw}, require_token=False)


def test_N8_twelve_char_password_is_fine():
    assert Config.from_env({**BASE, "MYSQL_PASSWORD": "twelve_chars"}, require_token=False)


def test_N8_socket_auth_allows_missing_or_empty_password():
    base = {k: v for k, v in BASE.items() if k != "MYSQL_PASSWORD"}
    cfg = Config.from_env({**base, "MYSQL_UNIX_SOCKET": "/run/mysqld/mysqld.sock"}, require_token=False)
    assert cfg.mysql_password == "" and cfg.mysql_unix_socket.endswith("mysqld.sock")
    cfg = Config.from_env({**base, "MYSQL_UNIX_SOCKET": "/run/mysqld/mysqld.sock",
                           "MYSQL_PASSWORD": ""}, require_token=False)
    assert cfg.mysql_password == ""


def test_N8_short_password_still_refused_even_with_socket():
    with pytest.raises(ConfigError, match="at least 12 characters"):
        Config.from_env({**BASE, "MYSQL_PASSWORD": "short",
                         "MYSQL_UNIX_SOCKET": "/run/mysqld/mysqld.sock"}, require_token=False)


def test_N8_password_required_without_socket():
    base = {k: v for k, v in BASE.items() if k != "MYSQL_PASSWORD"}
    with pytest.raises(ConfigError, match="MYSQL_PASSWORD"):
        Config.from_env(base, require_token=False)


# ------------------------------------------------------------------ N4 quiet hours
@pytest.mark.parametrize("raw,val", [("22-8", (22, 8)), (" 23-7 ", (23, 7)), ("1-5", (1, 5)),
                                     ("off", None), ("none", None), ("", None), (None, None)])
def test_N4_parse_quiet_hours(raw, val):
    assert parse_quiet_hours(raw) == val


@pytest.mark.parametrize("raw", ["22", "8-8", "25-3", "a-b", "22-"])
def test_N4_bad_quiet_hours_is_a_config_error(raw):
    with pytest.raises(ConfigError, match="LOW_ALERT_QUIET_HOURS"):
        Config.from_env({**BASE, "LOW_ALERT_QUIET_HOURS": raw}, require_token=False)


@pytest.mark.parametrize("hour,quiet", [(21, False), (22, True), (23, True), (0, True), (7, True), (8, False),
                                        (12, False)])
def test_N4_default_quiet_hours_wrap_midnight(hour, quiet):
    cfg = Config.from_env(BASE, require_token=False)
    assert cfg.low_alert_quiet_hours == (22, 8)
    assert cfg.in_quiet_hours(datetime(2026, 10, 28, hour, 30)) is quiet


def test_N4_quiet_hours_off():
    cfg = Config.from_env({**BASE, "LOW_ALERT_QUIET_HOURS": "off"}, require_token=False)
    assert not any(cfg.in_quiet_hours(datetime(2026, 10, 28, h)) for h in range(24))


# ------------------------------------------------------------------ N9 integer totals
@pytest.mark.parametrize("text,amount,flagged", [
    ("TOTAL 4560", "4560", True),                         # 45.60 with the point dropped
    ("Milk 12.50\nEggs 33.10\nTOTAL 4560", "4560", True),
    ("Milk 12.50\nTOTAL 45", "45", True),                 # items have cents, total doesn't
    ("TOTAL 45", "45", False),                            # small, plausible whole-dollar total
    ("TOTAL HK$1200", "1200", False),                     # currency-marked integer
    ("TOTAL 45.60", "45.60", False),
    ("TOTAL 999", "999", False),
])
def test_N9_integer_total_flag(text, amount, flagged):
    f = find_amount(text)
    assert f.amount == D(amount)
    assert (NOFLAG in f.flags) is flagged, f.flags


def test_N9_flag_reaches_parse_receipt_text():
    assert NOFLAG in parse_receipt_text("WELLCOME\n27/10/2026\nTOTAL 4560", date(2026, 10, 28)).flags


def test_N9_flag_escalates_ocr_even_at_high_confidence(monkeypatch, tmp_path):
    fake = SimpleNamespace(text="WELLCOME\n27/10/2026\nTOTAL 4560", confidence=96.0, word_count=4,
                           variant="gray", words=[])
    monkeypatch.setattr(ocr, "run_tesseract", lambda *a, **k: fake)
    reader = ocr.ReceiptReader(70, "eng", None)
    ext = run_coro(reader.extract(tmp_path / "x.jpg", date(2026, 10, 28)))
    assert "no decimal point" in (ext.escalation_reason or "")
    assert ext.amount == D("4560") and ext.engine == "tesseract_fallback"


# ------------------------------------------------------------------ N8 logging (own process)
def _run(code):
    return subprocess.run([sys.executable, "-c", textwrap.dedent(code)], cwd=ROOT, capture_output=True,
                          text=True, timeout=60)


SECRET = "123456789:" + "AAFAKE_REAUDIT_" + "q" * 20     # fake, token-shaped


def test_N8_logger_with_propagate_false_and_its_own_handler_is_redacted():
    p = _run(f"""
        import logging, sys
        lg = logging.getLogger("thirdparty.lib")
        h = logging.StreamHandler(sys.stdout)
        h.setFormatter(logging.Formatter("%(message)s"))
        lg.addHandler(h); lg.propagate = False; lg.setLevel(logging.INFO)
        from maid_receipt_bot import logsafe
        logsafe.install(["{SECRET}"], "INFO")
        lg.info("url https://api.telegram.org/bot%s/getMe", "{SECRET}")
        late = logging.getLogger("thirdparty.late")       # created AFTER install
        h2 = logging.StreamHandler(sys.stdout); h2.setFormatter(logging.Formatter("%(message)s"))
        late.addHandler(h2); late.propagate = False
        late.warning("token={SECRET}")
        try:
            raise RuntimeError("boom {SECRET}")
        except RuntimeError:
            late.exception("failed")
        print("DONE")
    """)
    assert p.returncode == 0, p.stderr
    out = p.stdout + p.stderr
    assert "DONE" in out and SECRET not in out
    assert out.count("REDACTED") >= 3, out


def test_N8_unraisable_exception_is_logged_redacted():
    p = _run(f"""
        import gc
        from maid_receipt_bot import logsafe
        logsafe.install(["{SECRET}"], "INFO")
        class Bad:
            def __del__(self):
                raise ValueError("in __del__ {SECRET}")
        Bad()
        gc.collect()
        print("DONE")
    """)
    assert p.returncode == 0
    out = p.stdout + p.stderr
    assert "Unraisable exception" in out and "ValueError" in out and SECRET not in out
    assert "Exception ignored in" not in out      # Python's default (unredacted) printer was replaced
