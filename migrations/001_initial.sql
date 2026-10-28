-- =====================================================================
-- Maid Receipt Bot - initial schema (migration 001)
-- MySQL 8.0+ / MariaDB 10.5+. Engine InnoDB, charset utf8mb4. Money DECIMAL(12,2) HKD.
--
-- Applied by bot-deploy migrate (as botmig-maid-receipt) or by deploy/migrate.sh.
-- No USE, no DEFINER, no GRANT (grants live in deploy/db-grants). DELIMITER $$ kept
-- for the multi-statement trigger.
--
-- TIME CONVENTION: every DATETIME / DATE column is written by the application in
-- LOCAL WALL-CLOCK TIME of BOT_TIMEZONE (default Asia/Hong_Kong, HKT = UTC+8, no DST).
-- The application never relies on NOW()/CURRENT_TIMESTAMP for business data.
-- =====================================================================

-- schema_version: app-facing version (bot-deploy also tracks schema_migrations by file).
-- The bot refuses to start when MAX(version) < REQUIRED_SCHEMA_VERSION in db.py.
CREATE TABLE IF NOT EXISTS schema_version (
    version    INT UNSIGNED NOT NULL,
    name       VARCHAR(100) NOT NULL,
    applied_at DATETIME     NOT NULL,
    PRIMARY KEY (version)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
INSERT IGNORE INTO schema_version (version, name, applied_at) VALUES
    (1, '001_initial', CURRENT_TIMESTAMP);

-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS float_balance (
    id                  TINYINT UNSIGNED NOT NULL DEFAULT 1,
    balance             DECIMAL(12,2)    NOT NULL DEFAULT 0.00,  -- may go negative
    currency            CHAR(3)          NOT NULL DEFAULT 'HKD',
    last_low_alert_date DATE             NULL,     -- HKT date of last low-float alert DELIVERED (1/day cap)
    low_alert_claim_date DATE            NULL,     -- set while one worker is sending today's alert
    low_alert_claimed_at DATETIME        NULL,     -- claim lease start (stale claims are re-taken)
    low_alert_failed_date DATE           NULL,     -- an alert claimed that day was NOT delivered (sweep retries)
    updated_at          DATETIME         NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT chk_float_single_row CHECK (id = 1)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Seed the single row (opening balance 0.00; Albert tops up to start).
INSERT IGNORE INTO float_balance (id, balance, currency, last_low_alert_date, updated_at)
VALUES (1, 0.00, 'HKD', NULL, CURRENT_TIMESTAMP);

-- ---------------------------------------------------------------------
-- receipts: one row per photo received. Lifecycle:
--   pending  -> confirmed  (float deducted, balance_before/after filled)
--   pending  -> cancelled  (never deducted)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS receipts (
    id                      BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    status                  ENUM('pending','confirmed','cancelled') NOT NULL DEFAULT 'pending',
    submitted_by            BIGINT          NOT NULL,          -- Telegram user id
    photo_path              VARCHAR(512)    NOT NULL,          -- file on server disk
    telegram_file_id        VARCHAR(255)    NULL,
    telegram_file_unique_id VARCHAR(64)     NULL,

    amount                  DECIMAL(12,2)   NULL,              -- NULL until known
    currency                CHAR(3)         NOT NULL DEFAULT 'HKD',
    receipt_date            DATE            NULL,              -- date printed on receipt
    merchant                VARCHAR(255)    NULL,
    items                   TEXT            NULL,              -- item description

    ocr_engine              ENUM('tesseract','vision','tesseract_fallback','none') NOT NULL,
    ocr_confidence          DECIMAL(5,2)    NULL,              -- Tesseract mean word confidence 0-100
    ocr_escalation_reason   VARCHAR(255)    NULL,              -- why vision was attempted
    ocr_raw_text            MEDIUMTEXT      NULL,
    vision_model            VARCHAR(100)    NULL,
    vision_raw_response     TEXT            NULL,
    vision_error            VARCHAR(500)    NULL,
    duplicate_of            BIGINT UNSIGNED NULL,              -- earlier receipt this looks like (M4)

    summary_chat_id         BIGINT          NULL,              -- message holding the buttons
    summary_message_id      BIGINT          NULL,

    balance_before          DECIMAL(12,2)   NULL,              -- filled on confirm
    balance_after           DECIMAL(12,2)   NULL,

    created_at              DATETIME        NOT NULL,
    updated_at              DATETIME        NOT NULL,
    confirmed_at            DATETIME        NULL,
    confirmed_by            BIGINT          NULL,
    cancelled_at            DATETIME        NULL,
    cancelled_by            BIGINT          NULL,

    PRIMARY KEY (id),
    KEY idx_receipts_status_confirmed (status, confirmed_at),
    KEY idx_receipts_file_unique (telegram_file_unique_id),
    KEY idx_receipts_dup (amount, receipt_date),
    KEY idx_receipts_created (created_at),
    CONSTRAINT chk_receipt_amount_positive CHECK (amount IS NULL OR amount > 0)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---------------------------------------------------------------------
-- topups: every float top-up made by the admin (append-only).
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS topups (
    id                BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    amount            DECIMAL(12,2)   NOT NULL,
    currency          CHAR(3)         NOT NULL DEFAULT 'HKD',
    admin_telegram_id BIGINT          NOT NULL,
    raw_message       VARCHAR(255)    NULL,       -- e.g. "top up 500"
    balance_before    DECIMAL(12,2)   NOT NULL,
    balance_after     DECIMAL(12,2)   NOT NULL,
    created_at        DATETIME        NOT NULL,
    PRIMARY KEY (id),
    KEY idx_topups_created (created_at),
    CONSTRAINT chk_topup_amount_positive CHECK (amount > 0)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---------------------------------------------------------------------
-- edit_sessions: which field each user is currently editing (one row per
-- user, so the admin and the maid can't clobber each other's edit).
-- Not a ledger table: rows are deleted when the edit finishes.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS edit_sessions (
    user_telegram_id BIGINT          NOT NULL,
    receipt_id       BIGINT UNSIGNED NOT NULL,
    field            ENUM('amount','receipt_date','merchant','items') NOT NULL,
    updated_at       DATETIME        NOT NULL,
    PRIMARY KEY (user_telegram_id),
    KEY idx_edit_receipt (receipt_id),
    CONSTRAINT fk_edit_receipt FOREIGN KEY (receipt_id) REFERENCES receipts (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---------------------------------------------------------------------
-- adjustments: admin corrections to the float after the fact (append-only),
-- e.g. voiding a receipt that was confirmed by mistake. Signed amount:
-- positive puts money back into the float, negative takes it out.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS adjustments (
    id                BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    amount            DECIMAL(12,2)   NOT NULL,
    currency          CHAR(3)         NOT NULL DEFAULT 'HKD',
    receipt_id        BIGINT UNSIGNED NULL,       -- receipt being corrected, if any
    reason            VARCHAR(255)    NOT NULL,
    admin_telegram_id BIGINT          NOT NULL,
    balance_before    DECIMAL(12,2)   NOT NULL,
    balance_after     DECIMAL(12,2)   NOT NULL,
    created_at        DATETIME        NOT NULL,
    PRIMARY KEY (id),
    KEY idx_adjust_receipt (receipt_id),
    CONSTRAINT chk_adjust_nonzero CHECK (amount <> 0),
    CONSTRAINT fk_adjust_receipt FOREIGN KEY (receipt_id) REFERENCES receipts (id) ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---------------------------------------------------------------------
-- audit_log: append-only trail of every receipt event and top-up.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS audit_log (
    id                BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    event_time        DATETIME(3)     NOT NULL,
    event_type        ENUM('receipt_created','receipt_edited','receipt_confirmed',
                           'receipt_cancelled','topup','low_float_alert',
                           'low_float_alert_failed','adjustment') NOT NULL,
    actor_telegram_id BIGINT          NULL,
    receipt_id        BIGINT UNSIGNED NULL,
    topup_id          BIGINT UNSIGNED NULL,
    adjustment_id     BIGINT UNSIGNED NULL,
    amount            DECIMAL(12,2)   NULL,
    balance_before    DECIMAL(12,2)   NULL,
    balance_after     DECIMAL(12,2)   NULL,
    details           JSON            NULL,       -- old/new values, OCR engine, etc.
    PRIMARY KEY (id),
    KEY idx_audit_time (event_time),
    KEY idx_audit_type (event_type, id),
    KEY idx_audit_receipt (receipt_id),
    KEY idx_audit_topup (topup_id),
    KEY idx_audit_adjust (adjustment_id),
    CONSTRAINT fk_audit_receipt FOREIGN KEY (receipt_id) REFERENCES receipts (id) ON DELETE RESTRICT,
    CONSTRAINT fk_audit_topup   FOREIGN KEY (topup_id)   REFERENCES topups (id)   ON DELETE RESTRICT,
    CONSTRAINT fk_audit_adjust  FOREIGN KEY (adjustment_id) REFERENCES adjustments (id) ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Belt-and-braces append-only enforcement (the bot's DB user also has no
-- UPDATE/DELETE grant on audit_log, topups or adjustments; runtime grants are in deploy/db-grants (bot-deploy)).
DROP TRIGGER IF EXISTS audit_log_no_update;
CREATE TRIGGER audit_log_no_update BEFORE UPDATE ON audit_log FOR EACH ROW
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'audit_log is append-only';
DROP TRIGGER IF EXISTS audit_log_no_delete;
CREATE TRIGGER audit_log_no_delete BEFORE DELETE ON audit_log FOR EACH ROW
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'audit_log is append-only';
DROP TRIGGER IF EXISTS topups_no_update;
CREATE TRIGGER topups_no_update BEFORE UPDATE ON topups FOR EACH ROW
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'topups is append-only';
DROP TRIGGER IF EXISTS topups_no_delete;
CREATE TRIGGER topups_no_delete BEFORE DELETE ON topups FOR EACH ROW
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'topups is append-only';
DROP TRIGGER IF EXISTS adjustments_no_update;
CREATE TRIGGER adjustments_no_update BEFORE UPDATE ON adjustments FOR EACH ROW
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'adjustments is append-only';
DROP TRIGGER IF EXISTS adjustments_no_delete;
CREATE TRIGGER adjustments_no_delete BEFORE DELETE ON adjustments FOR EACH ROW
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'adjustments is append-only';

-- A confirmed or cancelled receipt is final (audit M5): the bot user needs
-- UPDATE on receipts for pending edits, so the trigger - not the grant - is what
-- stops anyone holding the app credentials from rewriting or un-confirming a
-- receipt after it hit the ledger. Corrections go through `adjustments`.
DROP TRIGGER IF EXISTS receipts_final_no_update;
DELIMITER $$
CREATE TRIGGER receipts_final_no_update BEFORE UPDATE ON receipts FOR EACH ROW
BEGIN
    IF OLD.status <> 'pending' THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'confirmed/cancelled receipts are immutable';
    END IF;
END$$
DELIMITER ;
DROP TRIGGER IF EXISTS receipts_no_delete;
CREATE TRIGGER receipts_no_delete BEFORE DELETE ON receipts FOR EACH ROW
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'receipts cannot be deleted';
