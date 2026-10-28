from decimal import Decimal

import pytest

from maid_receipt_bot.config import Config, ConfigError

BASE = {"TELEGRAM_BOT_TOKEN": "123:abc", "MYSQL_USER": "u", "MYSQL_PASSWORD": "secretpw_long_enough",
        "MYSQL_DATABASE": "d", "ADMIN_TELEGRAM_ID": "1", "MAID_TELEGRAM_ID": "2"}


def test_defaults():
    c = Config.from_env(BASE)
    assert c.ocr_confidence_threshold == 70 and c.vision_model == "gpt-4o-mini"
    assert c.low_float_threshold == Decimal("100.00") and c.tz_name == "Asia/Hong_Kong"
    assert str(c.photo_dir) == "/var/lib/bots/maid-receipt/photos" and not c.vision_enabled
    assert "secretpw" not in repr(c) and "123:abc" not in repr(c)


def test_missing_and_invalid():
    with pytest.raises(ConfigError, match="TELEGRAM_BOT_TOKEN"):
        Config.from_env({k: v for k, v in BASE.items() if k != "TELEGRAM_BOT_TOKEN"})
    with pytest.raises(ConfigError):
        Config.from_env({**BASE, "MAID_TELEGRAM_ID": "1"})
    with pytest.raises(ConfigError):
        Config.from_env({**BASE, "OCR_CONFIDENCE_THRESHOLD": "150"})
    with pytest.raises(ConfigError):
        Config.from_env({**BASE, "BOT_TIMEZONE": "Mars/Base"})


def test_overrides():
    c = Config.from_env({**BASE, "OCR_CONFIDENCE_THRESHOLD": "65", "LOW_FLOAT_THRESHOLD": "200",
                         "VISION_API_KEY": "k", "BOT_TIMEZONE": "Asia/Hong_Kong"})
    assert c.ocr_confidence_threshold == 65 and c.low_float_threshold == Decimal("200.00") and c.vision_enabled


def test_process_tz_is_ignored_for_business_time():
    """Audit L5 / probe P09: TZ=:/etc/localtime or TZ=UTC must neither crash nor move HKT."""
    for tz in (":/etc/localtime", "UTC", "Europe/London", "garbage"):
        c = Config.from_env({**BASE, "TZ": tz})
        assert c.tz_name == "Asia/Hong_Kong"
    assert Config.from_env({**BASE, "BOT_TIMEZONE": "Asia/Singapore"}).tz_name == "Asia/Singapore"


def test_secrets_list_and_repr_hide_values():
    c = Config.from_env({**BASE, "VISION_API_KEY": "sk-fake-vision-key-000"})
    assert "sk-fake-vision-key-000" in c.secrets() and BASE["TELEGRAM_BOT_TOKEN"] in c.secrets()
    assert all(sec not in repr(c) for sec in c.secrets())
