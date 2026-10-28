"""Subprocess harness for the H1 regression tests (not collected by pytest).

Runs the real `maid_receipt_bot.main.main()` against a local fake Bot API server with a FAKE
token. Never talks to api.telegram.org.

Modes: 401 | 409 | refused (network down for good; stopped by a timer) | down_then_up (the fake
API only comes up after the bot has failed twice, audit N3) | db_down_then_up (MySQL unreachable twice, N3) |
schema_old (database not migrated, N11)."""
import json
import os
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
MODE = sys.argv[1]
FAKE = os.environ["FAKE_TOKEN"]


class H(BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("content-length") or 0)
        self.rfile.read(n)
        if MODE == "401":
            code, body = 401, {"ok": False, "error_code": 401, "description": "Unauthorized"}
        elif self.path.endswith("/getMe"):
            code, body = 200, {"ok": True, "result": {"id": 123456789, "is_bot": True, "first_name": "x",
                                                      "username": "x_bot", "can_join_groups": False,
                                                      "can_read_all_group_messages": False,
                                                      "supports_inline_queries": False}}
        elif "setMyCommands" in self.path or "deleteWebhook" in self.path:
            code, body = 200, {"ok": True, "result": True}
        elif MODE in ("down_then_up", "db_down_then_up") and self.path.endswith("/getUpdates"):
            print("HARNESS: getUpdates reached - polling started", flush=True)
            threading.Timer(0.3, lambda: os._exit(0)).start()
            code, body = 200, {"ok": True, "result": []}
        else:
            code, body = 409, {"ok": False, "error_code": 409,
                               "description": "Conflict: terminated by other getUpdates request"}
        b = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def log_message(self, *a):
        pass


def free_port():
    with socket.socket() as so:
        so.bind(("127.0.0.1", 0))
        return so.getsockname()[1]


if MODE == "down_then_up":
    port = free_port()

    import logging
    second_failure = threading.Event()

    class _Watch(logging.Handler):  # deterministic: come up only after the bot failed twice
        def emit(self, record):
            if "Telegram not reachable (attempt 2" in record.getMessage():
                second_failure.set()

    logging.getLogger("maid_receipt_bot").addHandler(_Watch(level=logging.WARNING))

    def late_start():
        second_failure.wait(60)
        srv = HTTPServer(("127.0.0.1", port), H)
        print("HARNESS: fake Telegram API is up now", flush=True)
        srv.serve_forever()

    threading.Thread(target=late_start, daemon=True).start()
else:
    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_port if MODE != "refused" else 1
os.environ.setdefault("MAIDBOT_STARTUP_BACKOFF_INITIAL", "0.2")
os.environ.setdefault("MAIDBOT_STARTUP_BACKOFF_MAX", "0.5")
os.environ.update(TELEGRAM_BOT_TOKEN=FAKE, MYSQL_USER="u", MYSQL_PASSWORD="SECRETPW_AUDIT", MYSQL_DATABASE="d",
                  ADMIN_TELEGRAM_ID="1", MAID_TELEGRAM_ID="2", LOG_LEVEL="DEBUG",
                  VISION_API_KEY="sk-AUDITFAKEKEY", VISION_API_BASE_URL="http://127.0.0.1:9",
                  MAIDBOT_ENV_FILE="/nonexistent")
import maid_receipt_bot.main as m  # noqa: E402
from telegram.ext import ApplicationBuilder  # noqa: E402

orig_build = ApplicationBuilder.build
base = f"http://127.0.0.1:{port}/bot"


def build(self):
    self.base_url(base)
    return orig_build(self)


ApplicationBuilder.build = build
if MODE == "db_down_then_up":
    import pymysql  # noqa: E402
    calls = {"n": 0}

    def flaky_checks(cfg, db):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise pymysql.err.OperationalError(2003, "Can't connect to MySQL server on '127.0.0.1' (111)")

    m.startup_checks = flaky_checks
elif MODE == "schema_old":
    m.Database.schema_version = lambda self: 0  # empty / unmigrated database
elif MODE == "db_denied":
    import pymysql  # noqa: E402

    def denied(cfg, db):
        raise pymysql.err.OperationalError(1045, "Access denied for user 'u'@'localhost' (using password: YES)")

    m.startup_checks = denied
else:
    m.startup_checks = lambda cfg, db: None
if MODE == "refused":
    threading.Timer(4, lambda: (print("HARNESS: still retrying after 4s - stopping", flush=True),
                                os._exit(7))).start()
if MODE in ("down_then_up", "db_down_then_up"):
    threading.Timer(40, lambda: os._exit(9)).start()  # safety net: never hang the suite
if MODE == "409":
    # stop after a few conflict rounds
    threading.Timer(6, lambda: os._exit(0)).start()
m.main([])
