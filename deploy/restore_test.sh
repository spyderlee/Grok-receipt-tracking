#!/usr/bin/env bash
# Restore drill (audit M6): load the newest (or a given) backup into a SCRATCH database,
# check it is consistent, and report. Never touches the live database.
#
#   deploy/restore_test.sh                       # newest backup in $BACKUP_DIR
#   deploy/restore_test.sh /path/db-....sql.gz   # a specific dump
#   deploy/restore_test.sh --no-checksum [dump]  # a dump without its SHA256SUMS-<stamp> file
#
# The checksum file written by backup.sh is REQUIRED (audit N7d) unless --no-checksum is given.
# Dumps taken before migration 001 (no adjustments table) are checked without that term (N7e).
#
# Settings: BACKUP_DIR (default /var/backups/bots/maid-receipt), RESTORE_DB (default
# bot_maid_receipt_restore_test; must differ from the live DB_NAME), MYSQL_DEFAULTS
# (--defaults-extra-file for mysql), KEEP_RESTORE_DB=1 to leave the scratch DB for inspection.
# Run as root (or a user with CREATE/DROP on RESTORE_DB and TRIGGER/SUPER for the triggers).
set -euo pipefail
DEST=${BACKUP_DIR:-/var/backups/bots/maid-receipt}
RESTORE_DB=${RESTORE_DB:-bot_maid_receipt_restore_test}
LIVE_DB=${DB_NAME:-bot_maid_receipt}
MYSQL=${MYSQL:-mysql}

if [ "$RESTORE_DB" = "$LIVE_DB" ]; then
    echo "refusing: RESTORE_DB equals the live database ($LIVE_DB)" >&2; exit 2
fi
case "$RESTORE_DB" in *[!A-Za-z0-9_]*|"") echo "bad RESTORE_DB name" >&2; exit 2;; esac

NO_CHECKSUM=0
if [ "${1:-}" = "--no-checksum" ]; then NO_CHECKSUM=1; shift; fi

DUMP=${1:-$(ls -1t "$DEST"/db-*.sql.gz 2>/dev/null | head -n1 || true)}
[ -n "$DUMP" ] && [ -r "$DUMP" ] || { echo "no dump found in $DEST" >&2; exit 1; }

M=("$MYSQL")
[ -n "${MYSQL_DEFAULTS:-}" ] && M=("$MYSQL" --defaults-extra-file="$MYSQL_DEFAULTS")

SUMS=$(dirname "$DUMP")/SHA256SUMS-$(basename "$DUMP" | sed 's/^db-//; s/\.sql\.gz$//')
if [ -r "$SUMS" ]; then
    ( cd "$(dirname "$DUMP")" && grep " $(basename "$DUMP")\$" "$(basename "$SUMS")" | sha256sum -c --quiet - )
    echo "checksum ok"
elif [ "$NO_CHECKSUM" = 1 ]; then
    echo "WARNING: no checksum file $(basename "$SUMS") - integrity NOT verified (--no-checksum)" >&2
else
    echo "RESTORE CHECK FAILED: checksum file $SUMS missing (use --no-checksum to restore anyway)" >&2
    exit 1
fi

cleanup() { [ "${KEEP_RESTORE_DB:-0}" = "1" ] || "${M[@]}" -e "DROP DATABASE IF EXISTS \`$RESTORE_DB\`" || true; }
trap cleanup EXIT

"${M[@]}" -e "DROP DATABASE IF EXISTS \`$RESTORE_DB\`; CREATE DATABASE \`$RESTORE_DB\` CHARACTER SET utf8mb4"
zcat "$DUMP" | "${M[@]}" "$RESTORE_DB"

q() { "${M[@]}" -N -B "$RESTORE_DB" -e "$1"; }
receipts=$(q "SELECT COUNT(*) FROM receipts")
topups=$(q "SELECT COUNT(*) FROM topups")
audit=$(q "SELECT COUNT(*) FROM audit_log")
stored=$(q "SELECT balance FROM float_balance WHERE id=1")
adj_term="+ COALESCE((SELECT SUM(amount) FROM adjustments),0)"
if [ "$(q "SELECT COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE()
           AND TABLE_NAME='adjustments'")" != 1 ]; then
    echo "note: dump predates migration 001 (no adjustments table); checking without adjustments"
    adj_term=""
fi
version=$(q "SELECT COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE()
             AND TABLE_NAME='schema_version'")
if [ "$version" = 1 ]; then version=$(q "SELECT COALESCE(MAX(version),'?') FROM schema_version"); else version="none"; fi
derived=$(q "SELECT COALESCE((SELECT SUM(amount) FROM topups),0)
                  - COALESCE((SELECT SUM(amount) FROM receipts WHERE status='confirmed'),0)
                  $adj_term")
triggers=$(q "SELECT COUNT(*) FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA=DATABASE()")

echo "restored $(basename "$DUMP") into $RESTORE_DB: receipts=$receipts topups=$topups audit_rows=$audit triggers=$triggers"
echo "schema_version=$version stored balance=$stored derived balance=$derived"
if [ "$(printf '%.2f' "$stored")" != "$(printf '%.2f' "$derived")" ]; then
    echo "RESTORE CHECK FAILED: balance does not match the ledger" >&2; exit 1
fi
if [ "$triggers" -lt 1 ]; then
    echo "RESTORE CHECK FAILED: append-only triggers missing from the dump" >&2; exit 1
fi
echo "restore drill OK"
