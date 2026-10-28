#!/usr/bin/env bash
# Creates a throwaway test database plus two users on a local MySQL/MariaDB:
#   maidbot_test      - full rights on maidbot_test (re-loads migrations/001_initial.sql for every test)
#   maidbot_test_app  - the SAME least-privilege table grants as production (deploy/db-grants)
# Usage: sudo -v && tests/setup_test_db.sh
set -euo pipefail
cd "$(dirname "$0")/.."
sudo mysql <<'SQL'
CREATE DATABASE IF NOT EXISTS maidbot_test CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER IF NOT EXISTS 'maidbot_test'@'localhost' IDENTIFIED BY 'testpw_maidbot_admin';
GRANT ALL ON maidbot_test.* TO 'maidbot_test'@'localhost';
CREATE USER IF NOT EXISTS 'maidbot_test_app'@'localhost' IDENTIFIED BY 'apppw_maidbot_test';
CREATE USER IF NOT EXISTS 'maidbot_test_app'@'127.0.0.1' IDENTIFIED BY 'apppw_maidbot_test';
-- scratch DB for the backup -> restore test (tests/test_audit_deploy.py)
CREATE DATABASE IF NOT EXISTS maidbot_test_restore CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
GRANT ALL ON maidbot_test_restore.* TO 'maidbot_test'@'localhost';
-- migration test (tests/test_*_deploy.py)
CREATE DATABASE IF NOT EXISTS maidbot_test_migrate CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
GRANT ALL ON maidbot_test_migrate.* TO 'maidbot_test'@'localhost';
-- mysqldump --single-transaction needs these on MySQL 8 / MariaDB
GRANT PROCESS ON *.* TO 'maidbot_test'@'localhost';
SQL
# Tables must exist before table-level GRANTs.
sudo mysql maidbot_test -e "SET FOREIGN_KEY_CHECKS=0; DROP TABLE IF EXISTS edit_sessions, audit_log, adjustments, topups, receipts, float_balance, schema_version, schema_migrations;"
sudo mysql maidbot_test < migrations/001_initial.sql
# Translate deploy/db-grants into GRANTs for the test app user (both hosts).
while IFS= read -r line || [ -n "$line" ]; do
  line="${line%%#*}"
  read -r tbl privs _ <<<"$line" || true
  [ -z "${tbl:-}" ] && continue
  for host in localhost 127.0.0.1; do
    echo "GRANT ${privs} ON maidbot_test.${tbl} TO 'maidbot_test_app'@'${host}';"
  done
done < deploy/db-grants | sudo mysql
sudo mysql -e "FLUSH PRIVILEGES;"
echo "Test DB ready: maidbot_test (users maidbot_test / maidbot_test_app)."
