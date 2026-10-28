-- =====================================================================
-- Maid Receipt Bot - MySQL schema  (MySQL 8.0+ / MariaDB 10.5+)
-- Engine InnoDB, charset utf8mb4. Money is DECIMAL(12,2) in HKD.
--
-- TIME CONVENTION: every DATETIME / DATE column below is written by the
-- application in LOCAL WALL-CLOCK TIME of the configured TZ
-- (default Asia/Hong_Kong, HKT = UTC+8, no DST). The application never
-- relies on NOW()/CURRENT_TIMESTAMP for business data, so the MySQL
-- server's own time_zone setting does not matter.
--
-- Load into an existing, empty database:
--     mysql -u root -p maid_receipts < schema.sql
-- =====================================================================

-- ---------------------------------------------------------------------
-- float_balance: exactly one row (id = 1) holding the running float.
-- Locked with SELECT ... FOR UPDATE on every receipt confirmation/top-up.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS float_balance (
    id                  TINYINT UNSIGNED NOT NULL DEFAULT 1,
    balance             DECIMAL(12,2)    NOT NULL DEFAULT 0.00,  -- may go negative
    currency            CHAR(3)          NOT NULL DEFAULT 'HKD',
    last_low_alert_date DATE             NULL,     -- HKT date of last low-float alert (1/day cap)
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

    edit_field              ENUM('amount','receipt_date','merchant','items') NULL, -- awaiting text reply
    edit_requested_by       BIGINT          NULL,
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
    KEY idx_receipts_edit (edit_requested_by, status),
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
-- audit_log: append-only trail of every receipt event and top-up.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS audit_log (
    id                BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    event_time        DATETIME(3)     NOT NULL,
    event_type        ENUM('receipt_created','receipt_edited','receipt_confirmed',
                           'receipt_cancelled','topup','low_float_alert') NOT NULL,
    actor_telegram_id BIGINT          NULL,
    receipt_id        BIGINT UNSIGNED NULL,
    topup_id          BIGINT UNSIGNED NULL,
    amount            DECIMAL(12,2)   NULL,
    balance_before    DECIMAL(12,2)   NULL,
    balance_after     DECIMAL(12,2)   NULL,
    details           JSON            NULL,       -- old/new values, OCR engine, etc.
    PRIMARY KEY (id),
    KEY idx_audit_time (event_time),
    KEY idx_audit_type (event_type, id),
    KEY idx_audit_receipt (receipt_id),
    KEY idx_audit_topup (topup_id),
    CONSTRAINT fk_audit_receipt FOREIGN KEY (receipt_id) REFERENCES receipts (id) ON DELETE RESTRICT,
    CONSTRAINT fk_audit_topup   FOREIGN KEY (topup_id)   REFERENCES topups (id)   ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Belt-and-braces append-only enforcement (the bot's DB user also has no
-- UPDATE/DELETE grant on audit_log or topups; see deploy/create_db_user.sql).
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
