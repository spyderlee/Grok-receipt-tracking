"""Configuration loaded exclusively from environment variables (no secrets in code)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Mapping, Optional, Tuple
from zoneinfo import ZoneInfo


class ConfigError(RuntimeError):
    pass


# Audit N8: the log redactor can only mask secrets of 6+ characters without mangling ordinary
# text, and a short DB password is weak anyway - require a real one.
MIN_DB_PASSWORD_LENGTH = 12


def parse_quiet_hours(raw: Optional[str]) -> Optional[Tuple[int, int]]:
    """'22-8' -> (22, 8): no background low-float alert from 22:00 to 07:59 (audit N4).
    'off' / 'none' / '' disables. Raises ValueError on anything else."""
    if raw is None or raw.strip().lower() in ("", "off", "none", "0"):
        return None
    start, _, end = raw.strip().partition("-")
    a, b = int(start), int(end)
    if not (0 <= a <= 23 and 0 <= b <= 23) or a == b:
        raise ValueError(raw)
    return a, b


def _get(env: Mapping[str, str], key: str, default: Optional[str] = None) -> Optional[str]:
    val = env.get(key)
    if val is None or str(val).strip() == "":
        return default
    return str(val).strip()


@dataclass(frozen=True)
class Config:
    telegram_bot_token: str
    mysql_user: str
    mysql_password: str
    mysql_database: str
    admin_id: int
    maid_id: int
    mysql_host: str = "127.0.0.1"
    mysql_port: int = 3306
    mysql_unix_socket: Optional[str] = None
    ocr_confidence_threshold: float = 70.0
    ocr_lang: str = "eng"
    vision_api_key: Optional[str] = field(default=None, repr=False)
    vision_model: str = "gpt-4o-mini"
    vision_api_base_url: str = "https://api.openai.com/v1"
    vision_timeout: float = 60.0
    low_float_threshold: Decimal = Decimal("100.00")
    photo_dir: Path = Path("/var/lib/bots/maid-receipt/photos")
    tz_name: str = "Asia/Hong_Kong"
    admin_name: str = "Albert"
    lock_file: Optional[Path] = Path("/var/lib/bots/maid-receipt/bot.lock")
    low_alert_quiet_hours: Optional[Tuple[int, int]] = (22, 8)

    # ------------------------------------------------------------------ time
    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.tz_name)

    def now(self) -> datetime:
        """Current wall-clock time in the configured TZ, naive (as stored in MySQL)."""
        return datetime.now(self.tz).replace(tzinfo=None, microsecond=0)

    def in_quiet_hours(self, now: datetime) -> bool:
        """True while background (sweep) low-float alerts are held back (audit N4)."""
        if not self.low_alert_quiet_hours:
            return False
        a, b = self.low_alert_quiet_hours
        h = now.hour
        return (a <= h or h < b) if a > b else (a <= h < b)

    def secrets(self) -> list:
        """Values that must never reach a log line (audit H1)."""
        return [v for v in (self.telegram_bot_token, self.mysql_password, self.vision_api_key) if v]

    @property
    def vision_enabled(self) -> bool:
        return bool(self.vision_api_key)

    def __repr__(self) -> str:  # never leak secrets into logs
        return (f"Config(db={self.mysql_user}@{self.mysql_host}:{self.mysql_port}/{self.mysql_database}, "
                f"admin={self.admin_id}, maid={self.maid_id}, ocr_threshold={self.ocr_confidence_threshold}, "
                f"vision={'on' if self.vision_enabled else 'off'}:{self.vision_model}, "
                f"low_float={self.low_float_threshold}, photos={self.photo_dir}, tz={self.tz_name})")

    # ------------------------------------------------------------- loading
    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None, require_token: bool = True) -> "Config":
        env = os.environ if env is None else env
        # Socket auth (bot-deploy): MYSQL_UNIX_SOCKET set => MYSQL_PASSWORD may be empty/missing.
        socket = _get(env, "MYSQL_UNIX_SOCKET")
        required = ["MYSQL_USER", "MYSQL_DATABASE", "ADMIN_TELEGRAM_ID", "MAID_TELEGRAM_ID"]
        if require_token:
            required.insert(0, "TELEGRAM_BOT_TOKEN")
        if not socket:
            required.append("MYSQL_PASSWORD")
        missing = [k for k in required if _get(env, k) is None]
        if missing:
            raise ConfigError("Missing required environment variables: " + ", ".join(missing))

        def as_int(key: str, default: Optional[int] = None) -> int:
            raw = _get(env, key)
            if raw is None:
                if default is None:
                    raise ConfigError(f"{key} is required")
                return default
            try:
                return int(raw)
            except ValueError as exc:
                raise ConfigError(f"{key} must be an integer, got {raw!r}") from exc

        def as_float(key: str, default: float) -> float:
            raw = _get(env, key)
            try:
                return float(raw) if raw is not None else default
            except ValueError as exc:
                raise ConfigError(f"{key} must be a number, got {raw!r}") from exc

        try:
            low = Decimal(_get(env, "LOW_FLOAT_THRESHOLD", "100")).quantize(Decimal("0.01"))
        except InvalidOperation as exc:
            raise ConfigError("LOW_FLOAT_THRESHOLD must be a number") from exc

        # Audit L5: business time comes from BOT_TIMEZONE only. The process-level TZ variable is
        # left to the OS/libc (it may legitimately be ":/etc/localtime" or "UTC" on a server) and
        # no longer changes which HKT day a receipt or the low-float cap belongs to.
        tz_name = _get(env, "BOT_TIMEZONE", "Asia/Hong_Kong")
        try:
            ZoneInfo(tz_name)
        except Exception as exc:
            raise ConfigError(f"BOT_TIMEZONE {tz_name!r} is not a valid IANA timezone") from exc

        thr = as_float("OCR_CONFIDENCE_THRESHOLD", 70.0)
        if not 0 <= thr <= 100:
            raise ConfigError("OCR_CONFIDENCE_THRESHOLD must be between 0 and 100")

        password = _get(env, "MYSQL_PASSWORD") or ""
        # Length rule only when a password is actually used (TCP/password auth). Socket auth
        # uses an empty password and the OS user identity instead (bot-deploy convention).
        if password and len(password) < MIN_DB_PASSWORD_LENGTH:
            raise ConfigError(f"MYSQL_PASSWORD must be at least {MIN_DB_PASSWORD_LENGTH} characters "
                              "(generate one with: openssl rand -base64 24)")
        try:
            quiet = parse_quiet_hours(_get(env, "LOW_ALERT_QUIET_HOURS", "22-8"))
        except ValueError as exc:
            raise ConfigError("LOW_ALERT_QUIET_HOURS must look like 22-8 (start-end hour) or 'off'") from exc

        cfg = cls(
            telegram_bot_token=_get(env, "TELEGRAM_BOT_TOKEN", "") or "",
            mysql_host=_get(env, "MYSQL_HOST", "127.0.0.1"),
            mysql_port=as_int("MYSQL_PORT", 3306),
            mysql_unix_socket=_get(env, "MYSQL_UNIX_SOCKET"),
            mysql_user=_get(env, "MYSQL_USER"),
            mysql_password=password,
            mysql_database=_get(env, "MYSQL_DATABASE"),
            admin_id=as_int("ADMIN_TELEGRAM_ID"),
            maid_id=as_int("MAID_TELEGRAM_ID"),
            ocr_confidence_threshold=thr,
            ocr_lang=_get(env, "OCR_LANG", "eng"),
            vision_api_key=_get(env, "VISION_API_KEY"),
            vision_model=_get(env, "VISION_MODEL", "gpt-4o-mini"),
            vision_api_base_url=_get(env, "VISION_API_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
            vision_timeout=as_float("VISION_TIMEOUT_SECONDS", 60.0),
            low_float_threshold=low,
            photo_dir=Path(_get(env, "RECEIPT_PHOTO_DIR", "/var/lib/bots/maid-receipt/photos")),
            tz_name=tz_name,
            admin_name=_get(env, "ADMIN_DISPLAY_NAME", "Albert"),
            lock_file=Path(_get(env, "BOT_LOCK_FILE", "/var/lib/bots/maid-receipt/bot.lock")),
            low_alert_quiet_hours=quiet,
        )
        if cfg.admin_id == cfg.maid_id:
            raise ConfigError("ADMIN_TELEGRAM_ID and MAID_TELEGRAM_ID must be different users")
        return cfg
