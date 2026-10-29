# Maid Receipt Bot

Private Telegram bot that tracks a household HKD float: the maid photographs receipts, the bot
OCRs them (local Tesseract first, low-cost vision model only when needed), she confirms, the
float is deducted in MySQL with a full audit trail, and Albert tops up with `top up 500`.

* Spec: [SPEC.md](SPEC.md) · Deployment: [DEPLOY.md](DEPLOY.md) · Schema: [migrations/001_initial.sql](migrations/001_initial.sql)
* Entry point: `main.py` (`python main.py`, or `python -m maid_receipt_bot`);
  `python main.py --reconcile` checks the ledger and exits 0 (ok) / 1 (mismatch) / 3 (DB down)
* Commands: `/balance`, `/history`, `top up 500`, `/export`, `/adjust ±amt reason`, `/void <#> reason`,
  `/reconcile`, `/help`
* Ops tools: `deploy/backup.sh`, `deploy/restore_test.sh`, `migrations/001_initial.sql`,
  `tools/vision_staging_test.py` (vision fallback check on real photos; `--live` only on the server)

## Run locally
```bash
sudo apt install -y python3-venv tesseract-ocr        # plus a local MySQL/MariaDB
python3 -m venv venv && venv/bin/pip install -r requirements.txt
sudo mysql -e "CREATE DATABASE bot_maid_receipt CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
sudo mysql bot_maid_receipt < migrations/001_initial.sql
cp .env.example .env && chmod 600 .env && $EDITOR .env  # token and both Telegram IDs
RECEIPT_PHOTO_DIR=./photos venv/bin/python main.py
```
`.env` in the working directory is loaded automatically (override path with `MAIDBOT_ENV_FILE`);
real environment variables take precedence. On the office server the runtime MySQL user is
`bot-maid-receipt` on database `bot_maid_receipt`, via unix_socket (`MYSQL_UNIX_SOCKET` set, no
`MYSQL_PASSWORD`). `deploy/db-grants` is the privilege list `bot-deploy` applies (table
privileges only). Schema entry is `migrations/001_initial.sql`. For a local TCP
login, comment out `MYSQL_UNIX_SOCKET` and set `MYSQL_HOST`, `MYSQL_PORT`, `MYSQL_USER`, and
`MYSQL_PASSWORD` (at least 12 characters).

## Tests
```bash
sudo apt install -y tesseract-ocr mariadb-server fonts-dejavu-core
tests/setup_test_db.sh                                # creates maidbot_test DB + 2 test users
venv/bin/pip install -r requirements-dev.txt
venv/bin/python -m pytest -q                          # -m "not mysql" / "not tesseract" to skip
```
Telegram and the vision API are mocked; MySQL, Tesseract, mysqldump and bash are real. The
`tests/test_audit_*.py` files turn each finding of the pre-deployment audit into a regression test
(the backup/migration tests need the `maidbot_test_restore` / `maidbot_test_migrate` DBs created by
`setup_test_db.sh`; the systemd test needs `systemd-analyze`). Override the test DB with
`TEST_MYSQL_HOST/PORT/DATABASE`, `TEST_MYSQL_ADMIN_USER/PASSWORD`, `TEST_MYSQL_APP_USER/PASSWORD`.
