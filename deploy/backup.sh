#!/usr/bin/env bash
# Nightly backup of the ledger DB + receipt photos (audit M6).
#
# Run as root from cron, e.g. (/etc/cron.d/bot-maid-receipt):
#   15 3 * * * root /opt/bots/maid-receipt/deploy/backup.sh >> /var/log/maid-receipt-bot-backup.log 2>&1
#
# Settings (environment, or /etc/bots/maid-receipt/backup.env which is sourced if present):
#   DB_NAME          database to dump                      (default bot_maid_receipt)
#   MYSQL_DEFAULTS   mysqldump --defaults-extra-file       (default: none = root via unix socket)
#   PHOTO_DIR        receipt photo directory               (default /var/lib/bots/maid-receipt/photos)
#                    A missing PHOTO_DIR fails the backup (audit N7c) unless ALLOW_MISSING_PHOTO_DIR=1
#                    (e.g. a brand-new install that has not received a photo yet).
#   BACKUP_DIR       local destination                     (default /var/backups/bots/maid-receipt)
#   KEEP_DAYS        local retention in days               (default 60)
#   PHOTO_ARCHIVE    "daily" (default): full photo tar every night, pruned after KEEP_DAYS.
#                    "none": no nightly tar - rely on the incremental off-host photo mirror
#                    below (photos are write-once, so the mirror only ever copies new files).
#   BACKUP_REMOTE    OFF-HOST copy target. "user@host:/path" uses rsync over ssh;
#                    "rclone:remote:path" uses rclone. Strongly recommended. The photo dir is
#                    also mirrored incrementally to <remote>/photos/ (new files only, never
#                    deletes on the remote).
#   ALERT_COMMAND    run (via sh -c) with the error message on stdin when a backup fails
#   ALERT_TELEGRAM   if "1" and no ALERT_COMMAND: send the failure to ADMIN_TELEGRAM_ID using
#                    TELEGRAM_BOT_TOKEN from /etc/bots/maid-receipt/env. The token is passed to
#                    curl through a config on stdin (curl -K -), never on the command line or
#                    in this script's output. Plain KEY=value and `export KEY=value` lines are
#                    both understood; if either key is missing a log line says so (audit N7f).
#
# A backup is only "done" after the dump and photo archive are complete, verified
# (gzip -t / tar -t) and renamed into place; old backups are pruned ONLY after that.
set -euo pipefail
umask 077

CONF=${BACKUP_CONF:-/etc/bots/maid-receipt/backup.env}
# shellcheck disable=SC1090
[ -r "$CONF" ] && . "$CONF"

DB_NAME=${DB_NAME:-bot_maid_receipt}
PHOTO_DIR=${PHOTO_DIR:-/var/lib/bots/maid-receipt/photos}
DEST=${BACKUP_DIR:-/var/backups/bots/maid-receipt}
KEEP_DAYS=${KEEP_DAYS:-60}
BOT_ENV=${BOT_ENV:-/etc/bots/maid-receipt/env}
MYSQLDUMP=${MYSQLDUMP:-mysqldump}
STAMP=$(TZ=Asia/Hong_Kong date +%Y%m%d-%H%M%S)

DB_OUT="$DEST/db-$STAMP.sql.gz"
PH_OUT="$DEST/photos-$STAMP.tar.gz"
TMP_FILES=()

log() { echo "$(date -Is) $*"; }

env_value() {  # KEY FILE - last "KEY=v" / "export KEY=v" line, quotes and CR stripped; never printed
    sed -n -E "s/^[[:space:]]*(export[[:space:]]+)?$1[[:space:]]*=[[:space:]]*//p" "$2" | tail -n1 \
        | tr -d '"'"'"'\r' | sed -E 's/[[:space:]]+$//'
}

alert() {
    local msg="maid-receipt backup FAILED on $(hostname): $1"
    if [ -n "${ALERT_COMMAND:-}" ]; then
        printf '%s\n' "$msg" | sh -c "$ALERT_COMMAND" || log "alert command failed"
    elif [ "${ALERT_TELEGRAM:-0}" = "1" ]; then
        if [ ! -r "$BOT_ENV" ]; then
            log "telegram alert not configured: $BOT_ENV not readable"
            return 0
        fi
        # Read only the two keys we need; never echo them.
        local token admin
        token=$(env_value TELEGRAM_BOT_TOKEN "$BOT_ENV")
        admin=$(env_value ADMIN_TELEGRAM_ID "$BOT_ENV")
        if [ -n "$token" ] && [ -n "$admin" ]; then
            printf 'url = "https://api.telegram.org/bot%s/sendMessage"\n' "$token" \
                | curl -sS -o /dev/null --max-time 20 -K - \
                       --data-urlencode "chat_id=$admin" --data-urlencode "text=$msg" \
                       2>/dev/null || log "telegram alert failed"
        else
            log "telegram alert not configured: TELEGRAM_BOT_TOKEN or ADMIN_TELEGRAM_ID missing in $BOT_ENV"
        fi
        unset token
    fi
}

cleanup() {
    local rc=$?
    for f in "${TMP_FILES[@]:-}"; do [ -n "$f" ] && rm -f -- "$f"; done
    if [ "$rc" -ne 0 ]; then
        log "backup FAILED (exit $rc) at: ${STEP:-?}"
        alert "step '${STEP:-?}' exited $rc"
    fi
    exit "$rc"
}
trap cleanup EXIT
# Audit N7a: a signal must not look like success - the EXIT trap then sees 143/130, logs, alerts.
trap 'exit 143' TERM
trap 'exit 130' INT
trap 'exit 129' HUP

# Audit N7 (cosmetic): validate settings before the (slow) dump.
PHOTO_ARCHIVE=${PHOTO_ARCHIVE:-daily}
case "$PHOTO_ARCHIVE" in daily|none) ;; *) STEP="config"; log "PHOTO_ARCHIVE must be daily or none"; exit 2;; esac
STEP="check photo dir $PHOTO_DIR"
if [ ! -d "$PHOTO_DIR" ]; then
    if [ "${ALLOW_MISSING_PHOTO_DIR:-0}" = "1" ]; then
        log "warning: photo dir $PHOTO_DIR missing (ALLOW_MISSING_PHOTO_DIR=1); no photos backed up"
    else
        log "photo dir $PHOTO_DIR is missing - wrong PHOTO_DIR? (set ALLOW_MISSING_PHOTO_DIR=1 on a new install)"
        exit 1
    fi
fi

STEP="prepare $DEST"
install -d -m 700 "$DEST"
# Audit N7b: temp files of a run that was SIGKILLed / lost power (no EXIT trap ran).
find "$DEST" -maxdepth 1 -type f \( -name '.db-*' -o -name '.photos-*' \) -mmin +1440 -delete

STEP="mysqldump $DB_NAME"
tmp_db=$(mktemp "$DEST/.db-$STAMP.XXXXXX")
TMP_FILES+=("$tmp_db")
DUMP_ARGS=(--single-transaction --triggers --hex-blob --no-tablespaces "$DB_NAME")
if [ -n "${MYSQL_DEFAULTS:-}" ]; then
    DUMP_ARGS=(--defaults-extra-file="$MYSQL_DEFAULTS" "${DUMP_ARGS[@]}")
fi
"$MYSQLDUMP" "${DUMP_ARGS[@]}" | gzip > "$tmp_db"
STEP="verify db dump"
gzip -t "$tmp_db"
# mysqldump writes this footer only when it finished cleanly.
zcat "$tmp_db" | tail -n 1 | grep -q -- '-- Dump completed'

if [ -n "${BACKUP_REMOTE:-}" ] || [ "$PHOTO_ARCHIVE" = daily ]; then :; else
    log "warning: PHOTO_ARCHIVE=none and no BACKUP_REMOTE - photos are NOT backed up"
fi
OUTS=("$DB_OUT")
if [ "$PHOTO_ARCHIVE" = daily ]; then
    STEP="archive photos"
    tmp_ph=$(mktemp "$DEST/.photos-$STAMP.XXXXXX")
    TMP_FILES+=("$tmp_ph")
    if [ -d "$PHOTO_DIR" ]; then
        tar -C "$(dirname "$PHOTO_DIR")" -czf "$tmp_ph" "$(basename "$PHOTO_DIR")"
    else
        log "warning: photo dir $PHOTO_DIR missing; archiving nothing"
        tar -czf "$tmp_ph" -T /dev/null
    fi
    STEP="verify photo archive"
    tar -tzf "$tmp_ph" > /dev/null
    OUTS+=("$PH_OUT")
fi

STEP="commit"
mv -- "$tmp_db" "$DB_OUT"
[ "$PHOTO_ARCHIVE" = daily ] && mv -- "$tmp_ph" "$PH_OUT"
TMP_FILES=()
( cd "$DEST" && for f in "${OUTS[@]}"; do basename "$f"; done | xargs sha256sum > "SHA256SUMS-$STAMP" )

if [ -n "${BACKUP_REMOTE:-}" ]; then
    STEP="off-host copy to $BACKUP_REMOTE"
    case "$BACKUP_REMOTE" in
        rclone:*)
            rclone copy --immutable "$DEST" "${BACKUP_REMOTE#rclone:}" \
                --include "*-$STAMP*" ;;
        *)
            rsync -a --ignore-existing "${OUTS[@]}" "$DEST/SHA256SUMS-$STAMP" "$BACKUP_REMOTE/" ;;
    esac
    if [ -d "$PHOTO_DIR" ]; then
        STEP="off-host photo mirror to $BACKUP_REMOTE/photos"
        case "$BACKUP_REMOTE" in
            rclone:*) rclone copy --immutable "$PHOTO_DIR" "${BACKUP_REMOTE#rclone:}/photos" ;;
            *)        rsync -a --ignore-existing "$PHOTO_DIR/" "$BACKUP_REMOTE/photos/" ;;
        esac
    fi
else
    log "warning: BACKUP_REMOTE not set - backups are on this host only"
fi

STEP="prune older than $KEEP_DAYS days"
find "$DEST" -maxdepth 1 -type f \( -name 'db-*.sql.gz' -o -name 'photos-*.tar.gz' -o -name 'SHA256SUMS-*' \) \
     -mtime +"$KEEP_DAYS" -delete
find "$DEST" -maxdepth 1 -type f \( -name '.db-*' -o -name '.photos-*' \) -mmin +1440 -delete

STEP="done"
log "backup ok: ${OUTS[*]}"
