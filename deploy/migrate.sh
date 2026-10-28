#!/usr/bin/env bash
# Apply pending schema migrations, once each (audit N11 / bot-deploy Phase A).
#
#   deploy/migrate.sh            # apply every migrations/NNN_*.sql newer than the DB
#   deploy/migrate.sh --status   # only print the current and the available versions
#
# On the office server prefer:  sudo bot-deploy maid-receipt migrate
# (creates DB + socket users, applies migrations as botmig-maid-receipt, reapplies db-grants).
# This script is the local / emergency runner (root via unix socket, or MYSQL_DEFAULTS).
#
# Run with the bot STOPPED and a fresh backup taken:
#   sudo bot-ctl stop maid-receipt && /opt/bots/maid-receipt/deploy/backup.sh
#   /opt/bots/maid-receipt/deploy/migrate.sh
#   sudo bot-ctl start maid-receipt
#
# Settings: DB_NAME (default bot_maid_receipt), MYSQL_DEFAULTS, MYSQL.
#
# App-facing version: schema_version.MAX(version). bot-deploy also records each file in
# schema_migrations (checksum). An empty database (no float_balance) starts at version 0 and
# gets migrations/001_initial.sql. Re-running is a no-op. DDL is not transactional in MySQL:
# if a migration fails part-way, restore the backup taken before (DEPLOY.md).
set -euo pipefail
cd "$(dirname "$0")/.."
DB_NAME=${DB_NAME:-bot_maid_receipt}
MYSQL=${MYSQL:-mysql}
M=("$MYSQL")
[ -n "${MYSQL_DEFAULTS:-}" ] && M=("$MYSQL" --defaults-extra-file="$MYSQL_DEFAULTS")
case "$DB_NAME" in *[!A-Za-z0-9_]*|"") echo "bad DB_NAME" >&2; exit 2;; esac

# Ensure the database exists (fresh install / local tests).
"${M[@]}" -e "CREATE DATABASE IF NOT EXISTS \`$DB_NAME\` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;"

q() { "${M[@]}" -N -B "$DB_NAME" -e "$1"; }
has_col() { [ "$(q "SELECT COUNT(*) FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE()
                    AND TABLE_NAME='$1' AND COLUMN_NAME='$2'")" = 1 ]; }

current=""
if [ "$(q "SELECT COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE()
           AND TABLE_NAME='schema_version'")" = 1 ]; then
    current=$(q "SELECT COALESCE(MAX(version),'') FROM schema_version")
fi
if [ -z "$current" ]; then
    if [ "$(q "SELECT COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE()
               AND TABLE_NAME='float_balance'")" = 1 ]; then
        # Pre-bot-deploy layout without schema_version: treat full current shape as version 1.
        if has_col float_balance low_alert_failed_date; then current=1
        else current=0; fi
        echo "no schema_version recorded; detected version $current from the table layout"
        detected=1
    else
        current=0
        detected=0
        echo "empty database $DB_NAME - will apply migrations from the start"
    fi
else
    detected=0
fi

latest=0
for f in migrations/[0-9][0-9][0-9]_*.sql; do
    [ -e "$f" ] || continue
    n=$((10#$(basename "$f" | cut -c1-3)))
    [ "$n" -gt "$latest" ] && latest=$n
done
echo "database $DB_NAME: schema version $current; code ships up to $latest"
[ "${1:-}" = "--status" ] && exit 0

record() {  # version name
    q "CREATE TABLE IF NOT EXISTS schema_version (version INT UNSIGNED NOT NULL, name VARCHAR(100) NOT NULL,
         applied_at DATETIME NOT NULL, PRIMARY KEY (version)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
         COLLATE=utf8mb4_unicode_ci;
       INSERT IGNORE INTO schema_version (version, name, applied_at) VALUES ($1, '$2', NOW())"
}

if [ "$detected" = 1 ] && [ "$current" -gt 0 ]; then
    for f in migrations/[0-9][0-9][0-9]_*.sql; do
        [ -e "$f" ] || continue
        n=$((10#$(basename "$f" | cut -c1-3)))
        [ "$n" -le "$current" ] && record "$n" "$(basename "$f" .sql) (detected)"
    done
fi

applied=0
for f in migrations/[0-9][0-9][0-9]_*.sql; do
    [ -e "$f" ] || continue
    n=$((10#$(basename "$f" | cut -c1-3)))
    [ "$n" -le "$current" ] && continue
    echo "applying $(basename "$f") ..."
    if ! "${M[@]}" "$DB_NAME" < "$f"; then
        echo "MIGRATION FAILED in $(basename "$f"); the database may be half-migrated." \
             "Restore the backup taken before this run (DEPLOY.md)." >&2
        exit 1
    fi
    record "$n" "$(basename "$f" .sql)"
    current=$n
    applied=$((applied + 1))
done
echo "schema version now $current ($applied migration(s) applied)"
