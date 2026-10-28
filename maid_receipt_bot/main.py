"""Entry point: python -m maid_receipt_bot"""
from __future__ import annotations

import argparse
import asyncio
import fcntl
import logging
import os
import sys
import time
from pathlib import Path
from typing import Optional

import pymysql
import pytesseract
from telegram import BotCommand, BotCommandScopeChat, BotCommandScopeDefault, Update
from telegram.error import InvalidToken, RetryAfter, TelegramError
from telegram.ext import (Application, ApplicationBuilder, CallbackQueryHandler, CommandHandler,
                          MessageHandler, filters)

from . import __version__
from . import handlers as h
from . import logsafe
from .config import Config, ConfigError
from .db import REQUIRED_SCHEMA_VERSION, Database
from .ocr import ReceiptReader
from .vision import VisionClient

log = logging.getLogger("maid_receipt_bot")

MAID_COMMANDS = [BotCommand("balance", "Current float balance"),
                 BotCommand("history", "Recent receipts and top-ups"),
                 BotCommand("help", "How to use this bot")]
ADMIN_COMMANDS = MAID_COMMANDS + [BotCommand("topup", "Add money to the float, e.g. /topup 500"),
                                  BotCommand("export", "Monthly CSV, e.g. /export 2026-10"),
                                  BotCommand("void", "Reverse a confirmed receipt, e.g. /void 12 typo"),
                                  BotCommand("adjust", "Correct the float, e.g. /adjust -20 reason"),
                                  BotCommand("reconcile", "Check the float against the ledger")]


def build_application(cfg: Config, db: Database, reader: ReceiptReader) -> Application:
    async def post_init(app: Application) -> None:
        try:
            await app.bot.set_my_commands([BotCommand("help", "How to use this bot")], scope=BotCommandScopeDefault())
            await app.bot.set_my_commands(MAID_COMMANDS, scope=BotCommandScopeChat(cfg.maid_id))
            await app.bot.set_my_commands(ADMIN_COMMANDS, scope=BotCommandScopeChat(cfg.admin_id))
        except TelegramError as exc:  # users who never pressed Start can't be scoped yet
            log.warning("Could not set per-user command menus (have both users pressed Start?): %s", exc)
        # Audit H2/M5: hourly retry of an undelivered low-float alert + ledger reconciliation.
        app.bot_data["sweep_task"] = asyncio.get_running_loop().create_task(
            h.sweep_forever(app.bot, app.bot_data["state"]))

    async def post_shutdown(app: Application) -> None:
        task = app.bot_data.pop("sweep_task", None)
        if task is not None:
            task.cancel()
        for t in list(h._background):  # pending alert retries
            t.cancel()

    app = (ApplicationBuilder().token(cfg.telegram_bot_token).concurrent_updates(4)
           .post_init(post_init).post_shutdown(post_shutdown).build())
    app.bot_data["state"] = h.BotState(cfg=cfg, db=db, reader=reader)

    private = filters.ChatType.PRIVATE
    app.add_handler(CommandHandler("start", h.cmd_start, filters=private))
    app.add_handler(CommandHandler("help", h.cmd_help, filters=private))
    app.add_handler(CommandHandler("balance", h.cmd_balance, filters=private))
    app.add_handler(CommandHandler("history", h.cmd_history, filters=private))
    app.add_handler(CommandHandler("export", h.cmd_export, filters=private))
    app.add_handler(CommandHandler("topup", h.cmd_topup, filters=private))
    app.add_handler(CommandHandler("adjust", h.cmd_adjust, filters=private))
    app.add_handler(CommandHandler("void", h.cmd_void, filters=private))
    app.add_handler(CommandHandler("reconcile", h.cmd_reconcile, filters=private))
    app.add_handler(MessageHandler(private & (filters.PHOTO | filters.Document.IMAGE), h.on_photo))
    app.add_handler(CallbackQueryHandler(h.on_callback))
    app.add_handler(MessageHandler(private & filters.TEXT & ~filters.COMMAND, h.on_text))
    app.add_handler(MessageHandler(private & ~filters.COMMAND, h.on_unsupported))
    app.add_error_handler(h.on_error)
    return app


class SchemaTooOld(RuntimeError):
    pass


def startup_checks(cfg: Config, db: Database) -> None:
    # Audit N11: refuse to run new code on an un-migrated database.
    version = db.schema_version()
    if version is None or version < REQUIRED_SCHEMA_VERSION:
        raise SchemaTooOld(f"database schema version {version or 'unknown'} is older than the "
                           f"{REQUIRED_SCHEMA_VERSION} this code needs - stop the bot, back up, then run "
                           "bot-deploy maid-receipt migrate (or deploy/migrate.sh)")
    row = db.health_check()
    log.info("MySQL OK - float balance HK$%s", row["balance"])
    log.info("Tesseract %s", pytesseract.get_tesseract_version())
    cfg.photo_dir.mkdir(parents=True, exist_ok=True, mode=0o750)
    if not os.access(cfg.photo_dir, os.W_OK):
        raise RuntimeError(f"RECEIPT_PHOTO_DIR {cfg.photo_dir} is not writable")
    if not cfg.vision_enabled:
        log.warning("VISION_API_KEY not set - low-confidence receipts will use Tesseract output only")


EXIT_CONFIG = 2        # bad config / token rejected - systemd: RestartPreventExitStatus
EXIT_STARTUP = 3       # DB / Tesseract / photo dir broken (not just unreachable) - systemd retries
EXIT_ALREADY_RUNNING = 4

# Audit N3: a network or DB that is down at boot is waited for IN-PROCESS (exponential
# backoff). systemd StartLimit (600s/5) is only a secondary backstop.
# Overridable for tests.
STARTUP_BACKOFF_INITIAL = float(os.environ.get("MAIDBOT_STARTUP_BACKOFF_INITIAL", "5"))
STARTUP_BACKOFF_MAX = float(os.environ.get("MAIDBOT_STARTUP_BACKOFF_MAX", "300"))
# MySQL client errors that mean "server not reachable (yet)", not "misconfigured".
TRANSIENT_DB_ERRNOS = {2002, 2003, 2005, 2006, 2013, 1040, 1053, 1205, 2055}


class Backoff:
    def __init__(self, what: str) -> None:
        self.what, self.delay, self.attempt, self.started = what, STARTUP_BACKOFF_INITIAL, 0, time.monotonic()

    def failed(self, exc: BaseException, wait: Optional[float] = None) -> float:
        self.attempt += 1
        delay = self.delay if wait is None else max(wait, 1.0)
        log.warning("%s not reachable (attempt %d, %s: %s) - retrying in %.0fs",
                    self.what, self.attempt, type(exc).__name__, exc, delay)
        self.delay = min(self.delay * 2, STARTUP_BACKOFF_MAX)
        return delay

    def succeeded(self) -> None:
        if self.attempt:
            log.warning("%s reachable again after %d failed attempt(s) / %.0fs - continuing startup",
                        self.what, self.attempt, time.monotonic() - self.started)


def _transient_db_error(exc: BaseException) -> bool:
    if isinstance(exc, pymysql.err.OperationalError):
        return bool(exc.args) and exc.args[0] in TRANSIENT_DB_ERRNOS
    return isinstance(exc, (ConnectionError, TimeoutError))


def wait_for_database(cfg: Config, db: Database) -> None:
    """Run startup_checks, retrying while MySQL is merely unreachable. Real misconfiguration
    (wrong password 1045, unknown DB 1049, missing table, unwritable photo dir) still fails fast."""
    backoff = Backoff("MySQL")
    while True:
        try:
            startup_checks(cfg, db)
        except Exception as exc:
            if not _transient_db_error(exc):
                raise
            time.sleep(backoff.failed(exc))
            continue
        backoff.succeeded()
        return


async def wait_for_telegram(app: Application) -> None:
    """getMe in a backoff loop: a dead network at boot is waited out; a rejected token is not."""
    backoff = Backoff("Telegram")
    while True:
        try:
            await app.bot.initialize()
        except InvalidToken:
            raise
        except RetryAfter as exc:
            ra = exc.retry_after
            await asyncio.sleep(backoff.failed(exc, ra.total_seconds() if hasattr(ra, "total_seconds") else ra))
            continue
        except TelegramError as exc:  # NetworkError, TimedOut, 5xx ...
            await asyncio.sleep(backoff.failed(exc))
            continue
        backoff.succeeded()
        return


def _invalid_token_exit() -> None:
    # Audit H1: fixed message, no traceback (PTB's exception text can contain the token),
    # and a distinct exit code so systemd stops restarting.
    log.critical("Telegram rejected TELEGRAM_BOT_TOKEN (HTTP 401/404). Put a valid token from "
                 "@BotFather in /etc/bots/maid-receipt/env, then: systemctl restart bot-maid-receipt")
    sys.exit(EXIT_CONFIG)

_lock_fd: Optional[int] = None


def acquire_instance_lock(path: Optional[Path]) -> bool:
    """Audit L9: refuse to start a second copy on this host (two pollers = endless 409s).
    Returns False only if another live process holds the lock."""
    global _lock_fd
    if path is None:
        return True
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o640)
    except OSError as exc:
        log.warning("Single-instance lock %s unavailable (%s); continuing without it", path, exc)
        return True
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return False
    os.ftruncate(fd, 0)
    os.write(fd, f"{os.getpid()}\n".encode())
    _lock_fd = fd
    return True


def run_reconcile_cli(cfg: Config) -> int:
    """`python main.py --reconcile` for cron/monitoring: exit 0 = ledger consistent, 1 = not."""
    rec = Database(cfg).reconcile()
    if rec["ok"]:
        log.info("Ledger reconciles: balance %s == expected %s", rec["balance"], rec["expected"])
        return 0
    for p in rec["problems"]:
        log.error("RECONCILE: %s", p)
    return 1


def main(argv: Optional[list] = None) -> None:
    parser = argparse.ArgumentParser(prog="maid-receipt-bot")
    parser.add_argument("--reconcile", action="store_true",
                        help="check the float against the ledger + audit log, print problems, exit 0/1")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    level = os.environ.get("LOG_LEVEL", "INFO")
    # Redaction is installed before anything can log; the env token is masked even if the
    # config turns out to be invalid.
    logsafe.install([os.environ.get("TELEGRAM_BOT_TOKEN")], level)
    try:
        from dotenv import load_dotenv  # optional, for local development only
        load_dotenv(os.environ.get("MAIDBOT_ENV_FILE", ".env"), override=False)
    except ImportError:
        pass
    logsafe.REDACTOR.add(os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("MYSQL_PASSWORD"),
                         os.environ.get("VISION_API_KEY"))
    try:
        cfg = Config.from_env()
    except ConfigError as exc:
        log.error("Configuration error: %s", exc)
        sys.exit(EXIT_CONFIG)
    logsafe.REDACTOR.add(*cfg.secrets())
    if args.reconcile:
        try:
            sys.exit(run_reconcile_cli(cfg))
        except SystemExit:
            raise
        except Exception as exc:
            log.error("Reconciliation could not run: %s", exc)
            sys.exit(EXIT_STARTUP)
    log.info("maid-receipt-bot %s starting: %r", __version__, cfg)
    if os.environ.get("TZ") and os.environ.get("TZ") != cfg.tz_name:
        log.info("Process TZ=%s is ignored for business dates; using BOT_TIMEZONE=%s",
                 os.environ.get("TZ"), cfg.tz_name)

    if not acquire_instance_lock(cfg.lock_file):
        log.error("Another bot-maid-receipt instance is already running on this host (lock %s held). Exiting.",
                  cfg.lock_file)
        sys.exit(EXIT_ALREADY_RUNNING)

    db = Database(cfg)
    try:
        wait_for_database(cfg, db)
    except SchemaTooOld as exc:
        log.critical("%s", exc)
        sys.exit(EXIT_CONFIG)
    except Exception as exc:
        log.error("Startup check failed: %s", exc)
        sys.exit(EXIT_STARTUP)

    vision = (VisionClient(cfg.vision_api_key, cfg.vision_model, cfg.vision_api_base_url, cfg.vision_timeout)
              if cfg.vision_enabled else None)
    reader = ReceiptReader(cfg.ocr_confidence_threshold, cfg.ocr_lang, vision)
    app = build_application(cfg, db, reader)
    # One event loop for the probe and for run_polling (which uses get_event_loop()). It is not
    # closed here: run_polling closes it on shutdown.
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(wait_for_telegram(app))
    except InvalidToken:
        _invalid_token_exit()
    # Long polling: outbound HTTPS only, no inbound port / webhook needed (works behind Tailscale/NAT).
    try:
        app.run_polling(allowed_updates=[Update.MESSAGE, Update.CALLBACK_QUERY], drop_pending_updates=False)
    except InvalidToken:
        _invalid_token_exit()
    except Exception as exc:  # network down at boot etc. - redacted by the logging filter
        log.critical("Bot stopped: %s: %s", type(exc).__name__, exc, exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
