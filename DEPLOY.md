# Deploying Maid Receipt Bot (bot-deploy conventions)

The bot uses **long polling**: outbound HTTPS to `api.telegram.org` (and the vision API if
enabled). No inbound port, webhook, domain or TLS certificate is needed. Prefer long-polling so
we do **not** take Hermes port **3457** (`receipt-webhook`) on nlsai.

**Production layout (bot-deploy):**

| Item | Path / name |
|---|---|
| Code | `/opt/bots/maid-receipt` (root:bot-maid-receipt) |
| Unit | `bot-maid-receipt.service` (`deploy/bot-maid-receipt.service`) |
| Env | `/etc/bots/maid-receipt/env` (root:root 0600) |
| State / photos | `/var/lib/bots/maid-receipt` (`StateDirectory=bots/maid-receipt`), photos in `…/photos` |
| OS user | `bot-maid-receipt` |
| DB | `bot_maid_receipt` |
| Runtime DB user | `'bot-maid-receipt'@'localhost'` via **unix_socket** (no password) |
| Migrator | `botmig-maid-receipt` (ALL on the DB; applies `migrations/*.sql`) |

Do **not** introduce Hermes paths, ports, venv, or `HERMES_HOME`. Do not touch
`telegram-receipt-bot.service` or `receipt-webhook.service` unless Albert asks.

Albert creates secrets with the hidden-input script. **bot-deploy never reads or writes the env
file.** This package must not collect or print `TELEGRAM_BOT_TOKEN`.

## 0. Reach the server over Tailscale

Use the bot-deploy lane (`tag:bots` → office-server TCP 22 as OS user `deploy`). MySQL listens on
localhost only:

```bash
grep -R "^bind-address" /etc/mysql/        # expect 127.0.0.1
sudo ss -ltnp | grep 3306                    # expect 127.0.0.1:3306, NOT 0.0.0.0
```

MariaDB 11.8 on nlsai already has `innodb_snapshot_isolation=ON`.

## 1. Create the bot with BotFather

1. @BotFather → `/newbot` → name + username ending in `bot`.
2. Copy the token into `/etc/bots/maid-receipt/env` only (Albert / secrets script). If it leaks:
   BotFather → `/revoke`. **Own token** — do not share with Hermes `telegram-receipt-bot`.
3. `/setprivacy` → **Enable**; optional `/setjoingroups` → **Disable**.
4. `/setcommands` → paste:
   ```
   balance - Current float balance
   history - Recent receipts and top-ups
   topup - Top up the float (admin)
   export - Monthly CSV (admin)
   adjust - Correct the float, e.g. /adjust -20 counted cash (admin)
   void - Reverse a confirmed receipt, e.g. /void 12 entered twice (admin)
   reconcile - Check the float against the ledger (admin)
   help - How to use the bot
   ```
5. Albert and the maid must each press **Start** once.

## 2. Telegram user IDs

Each person messages @userinfobot (or @RawDataBot) and notes the numeric `Id`. After install,
`journalctl -u bot-maid-receipt | grep unauthorised` shows unknown IDs.

## 3. Install via bot-deploy (preferred)

Upload the release to `/home/deploy/releases/maid-receipt/` (or `.tar.gz`), then:

```bash
sudo /usr/local/sbin/bot-deploy maid-receipt deploy
sudo /usr/local/sbin/bot-deploy maid-receipt migrate   # DB + socket users + migrations + db-grants
```

bot-deploy creates the OS user, `/opt/bots/maid-receipt`, venv, unit, and state dir. It does
**not** create the env file.

**Manual / emergency install** (when bot-deploy is unavailable — same layout):

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip tesseract-ocr unzip
# optional Chinese OCR: tesseract-ocr-chi-tra  then OCR_LANG=eng+chi_tra
sudo useradd --system --home /opt/bots/maid-receipt --shell /usr/sbin/nologin bot-maid-receipt
sudo mkdir -p /opt/bots/maid-receipt
sudo unzip maid-receipt-bot.zip -d /tmp && sudo cp -r /tmp/maid-receipt-bot/. /opt/bots/maid-receipt/
sudo python3 -m venv /opt/bots/maid-receipt/venv
sudo /opt/bots/maid-receipt/venv/bin/pip install -r /opt/bots/maid-receipt/requirements.txt
sudo chown -R root:bot-maid-receipt /opt/bots/maid-receipt && sudo chmod -R o-rwx /opt/bots/maid-receipt
sudo install -d -o bot-maid-receipt -g bot-maid-receipt -m 750 \
  /var/lib/bots/maid-receipt /var/lib/bots/maid-receipt/photos
```

## 4. Database, schema, grants

**Preferred:** `sudo bot-deploy maid-receipt migrate` — creates `bot_maid_receipt`, socket users
`bot-maid-receipt` / `botmig-maid-receipt`, applies `migrations/001_initial.sql`, reapplies
`deploy/db-grants`.

**Local / emergency runner** (`deploy/migrate.sh`):

```bash
cd /opt/bots/maid-receipt
sudo /opt/bots/maid-receipt/deploy/migrate.sh --status   # empty → version 0; code ships up to 1
sudo /opt/bots/maid-receipt/deploy/migrate.sh            # applies 001_initial.sql
sudo mysql bot_maid_receipt -e "SHOW TRIGGERS\G" | grep -c Trigger:   # expect 8
```

Schema lives only in `migrations/001_*.sql` (no top-level `schema.sql` load; that file is a stub).
No `USE`, no `DEFINER`, no GRANTs in migrations. Runtime grants are in `deploy/db-grants`
(table-level SELECT/INSERT/UPDATE/DELETE only). The bot user has **no password** (unix_socket).
`MYSQL_PASSWORD` / `MYSQL_HOST` / `MYSQL_PORT` are not required on the office server.

**Triggers + binary logging (Albert decision if MySQL 8):** non-SUPER migrators need
`log_bin_trust_function_creators=1` when binlog is on. MariaDB on Ubuntu usually has binlog off —
likely not needed on nlsai.

Opening balance: start at 0 and send `top up 2000` (keeps the audit trail).

## 5. Environment file (secrets)

Albert writes `/etc/bots/maid-receipt/env` (root:root 0600) via the secrets script. Copy shape
from `.env.example`. The unit already sets `MYSQL_UNIX_SOCKET`, `MYSQL_USER`, `MYSQL_DATABASE`,
`RECEIPT_PHOTO_DIR`, `BOT_LOCK_FILE`. The env file needs the token, both Telegram IDs, and
optional `VISION_API_KEY`. **Do not set `MYSQL_PASSWORD` for socket auth.**

```bash
# illustrative only — prefer the secrets script
sudo install -d -m 750 -o root -g root /etc/bots/maid-receipt
# Albert fills TELEGRAM_BOT_TOKEN, ADMIN_TELEGRAM_ID, MAID_TELEGRAM_ID, …
```

systemd reads the file as root before dropping to `bot-maid-receipt`. Keep comments on their own
lines; don't quote values.

Dates and the once-a-day alert use **`BOT_TIMEZONE`** (default `Asia/Hong_Kong`). Process `TZ` is
ignored for business logic. `LOW_ALERT_QUIET_HOURS` (default `22-8`, or `off`) holds hourly-sweep
re-sends of undelivered low-float alerts overnight.

## 6. systemd service

```bash
# Preferred after bot-deploy:
sudo bot-ctl start maid-receipt
sudo bot-ctl status maid-receipt

# Manual:
sudo cp /opt/bots/maid-receipt/deploy/bot-maid-receipt.service /etc/systemd/system/
# Offline allowlist check (no SSH): bot-deploy maid-receipt validate-unit deploy/bot-maid-receipt.service
sudo systemd-analyze verify /etc/systemd/system/bot-maid-receipt.service
sudo systemctl daemon-reload
sudo systemctl enable --now bot-maid-receipt
```

**One instance per token.** Same host → exit 4 (`BOT_LOCK_FILE`). Another host → Telegram 409;
bot logs at most every 5 minutes and keeps retrying.

**Restart policy.** `Restart=on-failure`, `RestartSec=15`, `StartLimitIntervalSec=600` /
`StartLimitBurst=5` (bot-deploy example). Exit 2 (bad config / bad token / schema too old) and
exit 4 (lock) are never restarted. In-process exponential backoff (N3) waits out network/MySQL at
boot so StartLimit is only a secondary backstop. After fixing config:

```bash
sudo systemctl reset-failed bot-maid-receipt && sudo systemctl start bot-maid-receipt
# or: sudo bot-ctl start maid-receipt
```

**Hardening (L7).** `MemoryDenyWriteExecute` / `PrivateUsers` were validated statically. If start
fails with EPERM/SIGSYS, temporarily comment those two, `daemon-reload`, and report which one.

## 7. Logs

```bash
sudo bot-ctl logs maid-receipt
# or: journalctl -u bot-maid-receipt -f
```

| Exit | Meaning | systemd |
|---|---|---|
| 0 | clean stop | - |
| 1 | unexpected error (token redacted) | restarts (rate-limited) |
| 2 | config / rejected token / schema too old | **not** restarted |
| 3 | startup check failed (MySQL broken, Tesseract missing, photo dir) | restarts |
| 4 | lock held | **not** restarted |

## 8. Backups, restore drill, reconciliation

`deploy/backup.sh` dumps **`bot_maid_receipt`** (`mysqldump --single-transaction --triggers`),
verifies gzip + "Dump completed", archives photos, writes `SHA256SUMS-<stamp>`, copies off-host,
then prunes local copies older than `KEEP_DAYS` (60).

1. Settings in `/etc/bots/maid-receipt/backup.env` (root, `chmod 600`):
   ```bash
   BACKUP_REMOTE=albert@nas:/volume1/backups/maid-receipt
   ALERT_TELEGRAM=1
   PHOTO_ARCHIVE=daily
   # ALLOW_MISSING_PHOTO_DIR=1   # brand-new install only
   ```
2. Install and schedule:
   ```bash
   sudo install -m 700 /opt/bots/maid-receipt/deploy/backup.sh /usr/local/sbin/maid-receipt-backup
   sudo install -m 700 /opt/bots/maid-receipt/deploy/restore_test.sh /usr/local/sbin/maid-receipt-restore-test
   sudo tee /etc/cron.d/bot-maid-receipt >/dev/null <<'CRON'
   15 3 * * * root /usr/local/sbin/maid-receipt-backup >> /var/log/bot-maid-receipt-backup.log 2>&1
   45 3 * * * root sh -c 'set -a; . /etc/bots/maid-receipt/env; exec /opt/bots/maid-receipt/venv/bin/python /opt/bots/maid-receipt/main.py --reconcile' >> /var/log/bot-maid-receipt-reconcile.log 2>&1
   30 4 1 * * root /usr/local/sbin/maid-receipt-restore-test >> /var/log/bot-maid-receipt-backup.log 2>&1
   CRON
   ```
3. Run once by hand: expect "backup ok" and "restore drill OK". Scratch DB is
   `bot_maid_receipt_restore_test` (refuses the live name).
4. Real restore: stop the bot, `gunzip -c db-….sql.gz | sudo mysql bot_maid_receipt`, re-run
   `bot-deploy maid-receipt migrate` (or reapply `deploy/db-grants`), restore photos under
   `/var/lib/bots/maid-receipt`.

## 9. Updating

```bash
sudo bot-ctl stop maid-receipt
sudo /usr/local/sbin/maid-receipt-backup
# upload new release, then:
sudo bot-deploy maid-receipt deploy
sudo bot-deploy maid-receipt migrate
sudo bot-ctl start maid-receipt
```

If schema is behind, the bot logs `database schema version … is older than …` and exits 2:
migrate, then `bot-ctl start` / `reset-failed` + start.

**Migrations.** One initial file: `migrations/001_initial.sql`. `migrate.sh` / bot-deploy migrate
record `schema_version` (app) and `schema_migrations` (bot-deploy checksums). Re-run is a no-op.
DDL is not transactional — restore the pre-migrate backup on failure.

## 10. Troubleshooting

| Symptom | Fix |
|---|---|
| Missing required env (exit 2) | typo in `/etc/bots/maid-receipt/env`; `reset-failed` + start |
| Telegram rejected token (exit 2) | fix token; never share with Hermes receipt bot |
| 409 Conflict | second poller somewhere |
| Access denied for `bot-maid-receipt` | socket plugin / grants; re-run migrate |
| schema version too old (exit 2) | `bot-deploy maid-receipt migrate` |
| MySQL / Telegram not reachable at boot | in-process backoff; fix network, no restart needed |
| `/reconcile` mismatch | compare off-host backup; correct with `/adjust` |

## 11. Server-only steps before go-live

1. Run the test suite on the server once (`tests/setup_test_db.sh`, pytest). Never point tests at
   `bot_maid_receipt`.
2. Real systemd start + `systemd-analyze security bot-maid-receipt`.
3. Vision staging (`tools/vision_staging_test.py`) with or without `--live --env-file /etc/bots/maid-receipt/env`.
4. Backup + restore drill; confirm files on `BACKUP_REMOTE`.
5. Failure alert: `MYSQLDUMP=false` backup must alert Albert.
6. One instance only (no laptop with the production token).
7. Network-down boot test (N3 in-process backoff).
8. Record `@@version`, `@@innodb_snapshot_isolation`, `@@transaction_isolation`.
9. Under real hardening, send one JPEG and one HEIC and confirm both read.
