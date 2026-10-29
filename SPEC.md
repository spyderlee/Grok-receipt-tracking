# Maid Receipt Bot — Specification (v1.2)

> v1.1 = v1.0 plus every fix from the pre-deployment audit (H1-H2, M1-M8, L1-L12). The audit IDs
> are quoted next to each changed rule. Fresh schema: `migrations/001_initial.sql` (bot-deploy / migrate.sh).
>
> v1.2 = v1.1 plus the re-audit fixes N1-N13 (see §11). bot-deploy Phase A: single
> `migrations/001_initial.sql` (schema version 1); apply via `bot-deploy maid-receipt migrate`
> or `deploy/migrate.sh`.

## 1. Purpose
A private Telegram bot that lets Albert's maid photograph every shopping receipt. The bot reads each
receipt (amount, date, merchant, items), asks her to confirm, deducts the amount from a shared
household **float** (HKD), tells her the new balance, and keeps a tamper-resistant audit trail in a
local MySQL database on Albert's office server. Albert tops the float up with a one-line message and
is alerted when it runs low.

## 2. Users and roles
Exactly two Telegram users, identified by numeric user ID (env vars), one shared float.

| Role  | Env var             | Can do |
|-------|---------------------|--------|
| Admin (Albert) | `ADMIN_TELEGRAM_ID` | everything the maid can, plus top-ups, `/export`, `/adjust`, `/void`, `/reconcile`, confirm/edit/cancel any receipt, receives low-float, reconciliation and backup alerts |
| Maid  | `MAID_TELEGRAM_ID`  | send receipts, confirm/edit/cancel **her own** receipts, `/balance`, `/history` |
| Anyone else | — | refused politely ("Sorry, this is a private bot."); attempt is logged |

Only private (1:1) chats are handled; group messages are ignored.

## 3. Flows

### 3.1 Receipt (photo → confirm → deduct)
```
Maid            Bot                         Disk / Tesseract / Vision          MySQL
 |--photo------->|                                                              |
 |<-"reading…"---|                                                              |
 |               |--download--------------> RECEIPT_PHOTO_DIR/YYYY/MM/x.jpg     |
 |               |--Tesseract (local)-----> text + mean word confidence          |
 |               |  if conf < threshold OR no amount:                            |
 |               |--vision (low-cost)-----> {amount,date,merchant,items} JSON    |
 |               |  (on vision failure/no key -> keep Tesseract result)          |
 |               |--same file_unique_id already pending/confirmed? -> possible_duplicate
 |               |--INSERT receipts(status=pending) + audit receipt_created----->|
 |<-summary [✅Confirm][✏️Edit][❌Cancel]                                          |
 |--✏️Edit------->|  [Amount][Date][Merchant][Items][«Back]                       |
 |--Amount------->|--edit_sessions(user -> receipt, 'amount') (per user)------->|
 |<-"send amount"-|                                                              |
 |--"45.50"------>|--UPDATE receipts + audit receipt_edited(old,new)------------>|
 |<-new summary + buttons                                                        |
 |--✅Confirm---->|--BEGIN; SELECT receipt FOR UPDATE; SELECT float FOR UPDATE;  |
 |               |  balance -= amount (may go negative); status=confirmed;      |
 |               |  audit receipt_confirmed(before,after);                      |
 |               |  if after <= LOW_FLOAT_THRESHOLD and no alert sent today     |
 |               |  and no live claim: claim the alert (5-min lease); COMMIT--->|
 |               |--(if claimed) "⚠️ Low float…" FIRST ----------------> Albert  |
 |               |--record sent/failed (last_low_alert_date only if sent)------>|
 |<-"✅ confirmed … New float balance: HK$x"                                     |
```
* Nothing is deducted until **Confirm**. Pending receipts live in MySQL, so a restart loses nothing;
  the buttons on old summaries keep working.
* Confirm needs an amount. If none was read, Confirm shows "Please set the amount first" and the
  maid uses Edit → Amount. A receipt is **never rejected** for balance reasons.
* Edit state is kept **per user** (`edit_sessions`), so Albert and the maid editing at the same time
  don't clobber each other (L2), and an in-progress edit wins over text that looks like a top-up
  (L3: merchant "Top Up Mart" is saved as the merchant).
* Confirm is idempotent (double taps / two devices): the receipt row lock + status check
  guarantee one deduction. Once confirmed or cancelled a receipt is immutable (DB trigger, M5).
* **Duplicates (M4):** a photo whose Telegram `file_unique_id` matches a pending or confirmed
  receipt is saved with a "⚠️ This looks like receipt #N" warning and the buttons
  [Confirm anyway][Cancel]; it is only deducted after that explicit confirm.
* **Check line (M7/M3/M1):** when the reading is doubtful (no TOTAL line, low amount-token
  confidence, items don't add up, refund/negative total, amount over HK$5,000) and vision didn't
  re-read it, the summary shows `⚠️ Check: <reason>`.
* If saving the receipt fails, the "reading…" message is replaced with "couldn't save, please
  resend" and the photo file is removed (M1). A cancel that loses a race with confirm shows the
  receipt's real status, never "nothing was deducted" (M2).
* Cancel asks "Yes, cancel it / No, go back"; cancelled receipts are never deducted but stay in the DB
  and audit log.
* Edit text replies: amount (`45.5`, `$45.50`, `HK$1,200`), date (`27/10/2026`, `2026-10-27`,
  `today`, `yesterday`), merchant/items free text (`-` clears). `cancel` leaves edit mode.
* Images sent as files (uncompressed documents) are accepted too, including HEIC/HEIF via
  `pillow-heif`; the saved extension and the vision MIME type follow the real format (L1).

### 3.2 Top-up (admin only)
```
Albert: "top up 500"  ->  BEGIN; SELECT float FOR UPDATE; balance += 500;
                          INSERT topups; audit topup(before,after); COMMIT
Bot -> Albert: "✅ Topped up HK$500.00. Float balance: HK$x (was HK$y)"
Bot -> Maid:   "💰 Albert topped up the float by HK$500.00. New float balance: HK$x"
```
Accepted forms (case-insensitive): `top up 50`, `topup 50.5`, `top-up $50`, `top_up 50`,
`top up HK$1,000`, `top up 50 HKD`, `/topup 50`. Amount must be > 0 and ≤ 1,000,000.
If the maid sends a top-up she's told only Albert can top up. Malformed amounts get a usage hint.

## 4. OCR pipeline and threshold logic
1. **Preprocess** (Pillow, cheap): EXIF-rotate → grayscale → upscale to ~1800 px long side
   (max 3×) or downscale above 3500 px → autocontrast.
2. **Tesseract** (`--oem 1 --psm 4`, `OCR_LANG`, default `eng`) → `image_to_data`.
   **Confidence** = arithmetic mean of word `conf` values, ignoring `conf == -1` and empty words.
3. If confidence < threshold, try progressively: Otsu-binarised image → projection-profile deskew
   (±12°) → OSD orientation fix (90/180/270°). Keep the variant with the best confidence.
4. **Parse** text: amount = value on the strongest "total" line (grand total / total due / 總計 >
   total / 合計 > amount), skipping subtotal, cash, change, tax, discount, Octopus balance, etc.;
   fallback = largest money value on a non-excluded line (flagged "no TOTAL line"). Amounts must be
   0 < x ≤ 1,000,000 on **every** path (M1); `HK$1 234.50` space-thousands are understood (L4).
   Negative totals (`-12.50`, `12.50-`, `(12.50)`) and refund/void/退款 slips are never pre-filled:
   amount = None, flagged, escalated (M3). Date = the date on a "Date/日期" line if any, else the
   first valid date that isn't an expiry/valid-until date and isn't in the future (L4) (Y-M-D,
   DD/MM/YYYY preferred over MM/DD, `26 Oct 2026`, `Oct 26, 2026`, `2026年10月26日`).
   Merchant = first plausible name line; items = priced lines that aren't totals/payments.
5. **Escalate** to the vision model when `confidence < OCR_CONFIDENCE_THRESHOLD` (default 70;
   exactly 70.0 does not escalate) **or** no amount was parsed **or** Tesseract crashed **or** (M7)
   no TOTAL-type line matched **or** the amount token's own confidence is below the threshold
   **or** the item prices don't add up to the total **or** the parse raised a flag (refund,
   negative, > HK$5,000).
   * OpenAI-compatible `POST {VISION_API_BASE_URL}/chat/completions`, `VISION_MODEL` (default
     `gpt-4o-mini`, a low-cost tier), temperature 0, JSON mode, image downscaled to 1600 px JPEG.
     Asks for strict JSON `{amount, date, merchant, items}`. If the server rejects JSON mode it retries
     once without it. Markdown fences / prose around the JSON are tolerated (first balanced `{…}`
     object, L4); `1.234,50` and `1,234.50` both read as 1234.50; vision amounts get the same cap.
   * Vision values win; Tesseract values fill any nulls.
   * No key, HTTP error, timeout (`VISION_TIMEOUT_SECONDS`, 60) or bad JSON → keep the Tesseract
     result (`ocr_engine = tesseract_fallback`), show the ⚠️ Check reason, and let the maid edit.
   * Vision is **off unless `VISION_API_KEY` is set** (M8). Before go-live either run
     `tools/vision_staging_test.py --live` on 10-20 real receipts or accept Tesseract-only.
6. Stored on the receipt row: `ocr_engine` (`tesseract` | `vision` | `tesseract_fallback` | `none`),
   `ocr_confidence`, `ocr_escalation_reason`, `ocr_raw_text`, `vision_model`, `vision_raw_response`,
   `vision_error`. The summary shows which engine read it.

## 5. Low-float alert rules (H2)
* Checked on every confirmed receipt and by an **hourly sweep**.
* Due when `balance <= LOW_FLOAT_THRESHOLD` (default HKD 100; "at or below") **and** no alert was
  successfully sent today (`float_balance.last_low_alert_date` < today in `BOT_TIMEZONE`, HKT).
* Whoever is due first **claims** it inside the confirm transaction (`low_alert_claim_date`,
  `low_alert_claimed_at`, 5-minute lease), so two confirms can't both send it.
* The alert is **sent first**, before the maid's buttons are answered/edited, so a stale callback or
  edit error can't swallow it. Only a successful send sets `last_low_alert_date` and writes the
  audit row `low_float_alert` (`details.sent=true`); a failure writes `low_float_alert` with
  `sent=false` + the error and leaves today's cap open.
* Failed sends retry in the background after 5 s, 30 s and 2 min; after that the next receipt or the
  hourly sweep tries again (after the lease expires). Result: at most one *delivered* alert per HKT
  day; a duplicate is possible only if the DB write after a successful send fails (preferred over
  losing the alert).
* A top-up doesn't reset the day's cap. Negative balances keep triggering (once a day) until topped up.
* Sent to Albert's chat (`ADMIN_TELEGRAM_ID`).
* v1.2 (N4): the sweep only (re-)sends an alert that is due and **not yet delivered** today; it never
  re-alerts after a delivered one, and it holds the re-send during `LOW_ALERT_QUIET_HOURS` (default
  22-8). Receipt-triggered alerts ignore quiet hours.
* v1.2 (N5/N6): the day an alert counts for is the **claim** date (a 23:58 claim delivered at 00:01
  counts for the earlier day). Finishing an alert checks the claim is still this worker's; a late
  success by a worker whose lease expired records the day but does not clear another worker's claim.
* The hourly sweep also runs the **reconciliation** (M5, §6) and alerts Albert once per new mismatch.

## 6. Data model (see `migrations/001_initial.sql`)
* `float_balance`: single row `id = 1` (CHECK), `balance DECIMAL(12,2)` (may be negative),
  `last_low_alert_date DATE` (last *delivered* alert), `low_alert_claim_date/claimed_at` (lease), `updated_at`.
* `receipts`: one row per photo; status `pending|confirmed|cancelled`; photo **path only**;
  extracted fields; OCR metadata; `duplicate_of`; `balance_before/after` on confirm; who/when
  for submit/confirm/cancel. Indexed on `telegram_file_unique_id`. Immutable once not pending.
* `edit_sessions`: one row per user → (receipt, field) being edited; deleted when the edit ends.
* `adjustments`: append-only signed corrections (`/adjust`, `/void`), reason, admin, balance before/after,
  optional `receipt_id` (a receipt can be voided once).
* `topups`: append-only; amount, admin id, raw message, balance before/after, time.
* `audit_log`: append-only; `receipt_created | receipt_edited | receipt_confirmed |
  receipt_cancelled | topup | adjustment | low_float_alert`, actor Telegram ID, amount, balance before/after,
  JSON details (old/new values on edits, OCR engine/confidence on create).
* **Append-only enforcement**: the bot's MySQL user has only `SELECT, INSERT` on `topups`,
  `adjustments` and `audit_log`, `SELECT, INSERT, UPDATE` on `receipts`, `SELECT, UPDATE` on
  `float_balance`, DELETE only on `edit_sessions`, no DDL/TRUNCATE. Triggers reject UPDATE/DELETE on
  `topups`, `adjustments`, `audit_log`, any UPDATE of a confirmed/cancelled receipt, and any receipt
  DELETE, even for other users.
* **Reconciliation (M5)**: `balance == Σtopups − Σconfirmed receipts + Σadjustments`, and the
  audit rows must match those tables one-for-one (no forged/missing confirm or top-up rows).
  Run by `/reconcile`, the hourly sweep and `python main.py --reconcile` (exit 0/1/3). Direct
  `UPDATE float_balance` or forged audit INSERTs remain *possible* with the bot's credentials but are
  *detected*.
* **Money**: `DECIMAL(12,2)`, Python `Decimal` end to end (no floats).
* **Time**: every DATETIME/DATE is **local wall-clock time in `BOT_TIMEZONE` (default Asia/Hong_Kong, UTC+8,
  no DST)**, generated by the app (never `NOW()`), so the MySQL server timezone doesn't matter.
  The process `TZ` variable is ignored (L5).
  Only the `float_balance` seed row's `updated_at` uses the server's clock.
* Photos: `RECEIPT_PHOTO_DIR/YYYY/MM/receipt_YYYYMMDD_HHMMSS_<telegram-unique-id>.<ext>`, dirs 0750.

## 7. Commands
| Command | Who | Description |
|---|---|---|
| `/start`, `/help` | both | greeting and role-specific help |
| *(photo)* | both | submit a receipt |
| `/balance` | both | current float; admin also sees alert threshold, last alert date, pending count |
| `/history [n]` | both | last n (default 10, max 50) confirmed receipts and top-ups with running balance, plus IDs of pending receipts |
| `top up 50` / `/topup 50` | admin | add to the float |
| `/export [YYYY-MM]` | admin | CSV (UTF-8 with BOM, Excel-friendly) of that month's **confirmed** receipts by confirmation time; default current month; caption shows count and total. v1.2 (N1): voided receipts are listed with `voided`, `voided_at`, `void_reason` columns but **excluded from the total** ("… (1 voided receipt listed but excluded)"). Cells starting with `= + - @` (or tab/CR) are prefixed with `'` (L6) |
| `/adjust ±amount reason` | admin | signed append-only correction, e.g. `/adjust -20 counted cash, was short`; sign and reason required; the maid is notified (L11). v1.2 (N10): refused when the resulting balance would be beyond ±HK$1,000,000, unless it moves the balance toward 0; `/void` is exempt |
| `/void <receipt#> reason` | admin | reverses a confirmed receipt by putting its amount back via an adjustment; once per receipt; not_found / not confirmed / already voided are reported (L11) |
| `/reconcile` | admin | ledger consistency check (M5) |

Per-user command menus are registered at startup (admin menu, maid menu).

## 8. Security
* No secrets in code; everything comes from environment variables (`/etc/bots/maid-receipt/env`,
  root-owned, `chmod 600`, read by systemd). `Config.__repr__` never prints the token, password
  or API key; `httpx` request logging is turned down, and a logging filter on every handler
  redacts the token, the DB password, the vision key and any `bot<id>:<secret>` shape from every
  record **including tracebacks** (H1).
* Telegram user-ID whitelist on every handler and callback; the maid can only act on her own receipts.
* Least-privilege runtime MySQL user `bot-maid-receipt` via unix_socket (no `MYSQL_PASSWORD` when
  `MYSQL_UNIX_SOCKET` is set). Migrator `botmig-maid-receipt` applies `migrations/001_initial.sql`;
  runtime grants are `deploy/db-grants`. MySQL listens on 127.0.0.1 only (L8).
* **Long polling** – outbound HTTPS only; no webhook, no inbound port. The server is administered
  over Tailscale SSH.
* systemd sandboxing (L7): dedicated `bot-maid-receipt` user, `ProtectSystem=strict`, `ProtectHome`,
  `NoNewPrivileges`, empty `CapabilityBoundingSet`, `SystemCallFilter=@system-service`,
  `ProtectClock/Hostname/KernelLogs`, `ProtectProc=invisible`, `RestrictNamespaces/Realtime`,
  `MemoryDenyWriteExecute`, `PrivateUsers`, write access limited to `/var/lib/bots/maid-receipt`
  (`StateDirectory=bots/maid-receipt`; photos in `…/photos`);
  `systemd-analyze security` ≈ 1.x-2.0.
* Backups (M6): verified dump + photos, checksums, off-host copy, failure alert, restore drill.
* All SQL is parameterised; the only dynamic column name comes from a fixed whitelist.

## 9. Error handling
* Config errors, or Telegram rejecting the token (`InvalidToken`) → one fixed log line without the
  token, exit code 2; systemd does not restart on 2 (H1).
* Startup checks: MySQL reachable and `float_balance` row present, Tesseract installed, photo dir
  writable → otherwise exit 3. v1.2 (N3): MySQL or Telegram being *unreachable* at boot is waited
  out inside the process (backoff 5 s → 5 min, retried indefinitely, logged per attempt); wrong
  credentials (1045) / unknown DB still exit 3; a schema older than version 2 exits 2. systemd
  restarts other failures every 30 s with no start limit (`StartLimitIntervalSec=0`); 2 and 4 are
  never restarted.
* A second instance on the same host → exit 4 (lock file, not restarted). A 409 Conflict from an
  instance elsewhere is logged at most every 5 min without a traceback (L9). Other errors → exit 1.
* Telegram "message is not modified" and expired-callback errors are ignored quietly (L10).
* MySQL connect retries 3× with backoff; each operation is its own transaction, rolled back on error.
* OCR crash or vision failure never loses a receipt: it's saved as pending with whatever was read
  (possibly nothing) for the maid to fill in.
* Photo download failure → the maid is asked to resend (nothing stored).
* Unexpected exceptions → logged with traceback; user gets "Something went wrong, please try again".
* Notifications that can't be delivered (e.g. maid never pressed Start) are logged, and Albert is
  told when the maid couldn't be notified of a top-up.

## 10. Non-goals (v1)
* Multiple maids, multiple floats, or currencies other than HKD.
* Webhooks, web dashboard, or public endpoints.
* Obsidian or any storage other than MySQL + photo files.
* Item-level line parsing / categorisation, or VAT handling (HK has none).
* Multi-page or multi-receipt photos (one receipt per photo).

## 11. v1.2 re-audit changes (N1-N13)
* **N1** `/export` lists voided receipts marked, excluded from the total; the duplicate-photo check
  ignores receipts that were voided, so re-sending the photo of a voided receipt is not a duplicate.
* **N2** every ledger transaction locks `float_balance` (`SELECT … FOR UPDATE`) **before** any other
  read, so its read view sees all committed data (no double deduction / missed duplicate under
  MariaDB snapshot isolation or MySQL REPEATABLE READ); errors 1020 and 1213 are retried
  (whole transaction, bounded). Lock order: float → receipt → edit_sessions.
* **N3** see §9.
* **N4-N6** see §5.
* **N7** backup/restore: TERM/INT traps, stale temp pruning, missing photo dir fails
  (`ALLOW_MISSING_PHOTO_DIR=1` opt-out), `PHOTO_ARCHIVE` validated, restore requires SHA256SUMS
  unless `--no-checksum`, drill handles pre-migration dumps, Telegram alert config is logged.
* **N8** logging redaction also covers loggers with `propagate=False` and "unraisable" exceptions
  (`sys.unraisablehook`); `MYSQL_PASSWORD` ≥ 12 characters when set; optional/empty when `MYSQL_UNIX_SOCKET` is set.
* **N9** a total without a decimal point is flagged "⚠️ Check" (and escalated to vision when
  enabled) if it is ≥ 1000 with no currency mark, or the other lines carry cents.
* **N10** cumulative `/adjust` bound, see §7.
* **N11** `schema_version` table (version, name, applied_at); `migrations/001_initial.sql`;
  `deploy/migrate.sh` applies pending migrations once each (idempotent; detects hand-applied 001).
  The bot refuses to start on an older schema (exit 2).
* **N12** an edit session older than 30 min expires: the next text is not taken as that field and
  the maid is told the edit timed out and to tap ✏️ Edit again; a top-up message is processed normally.
* **N13** event loops / sockets are always closed; the test suite runs with ResourceWarning as an
  error and collects garbage after every test.
