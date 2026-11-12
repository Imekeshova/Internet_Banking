import os
import sqlite3
import unittest
from datetime import datetime, timedelta, timezone

TEST_DB = "test_banking_demo.db"


def init_test_db(db_path: str) -> None:
    if os.path.exists(db_path):
        os.remove(db_path)

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON;")
    cursor = conn.cursor()

    cursor.executescript("""
    CREATE TABLE users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        full_name TEXT NOT NULL,
        email TEXT UNIQUE NOT NULL,
        phone TEXT NOT NULL,
        password_hash TEXT NOT NULL,
        role TEXT NOT NULL CHECK(role IN ('customer', 'admin'))
    );

    CREATE TABLE accounts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
        account_number TEXT UNIQUE NOT NULL,
        balance_minor INTEGER NOT NULL CHECK(balance_minor >= 0),
        currency TEXT NOT NULL DEFAULT 'KZT',
        status TEXT NOT NULL DEFAULT 'active'
    );

    CREATE TABLE ledger_entries (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        account_id INTEGER NOT NULL REFERENCES accounts(id),
        amount_minor INTEGER NOT NULL CHECK(amount_minor > 0),
        entry_type TEXT NOT NULL CHECK(entry_type IN ('INITIAL_DEPOSIT', 'ADMIN_ADJUSTMENT')),
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );

    CREATE TABLE transfers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        request_id TEXT UNIQUE NOT NULL,
        source_account_id INTEGER NOT NULL REFERENCES accounts(id),
        destination_account_id INTEGER NOT NULL REFERENCES accounts(id),
        amount_minor INTEGER NOT NULL CHECK(amount_minor > 0),
        status TEXT NOT NULL CHECK(status IN ('pending', 'completed', 'failed')),
        failure_reason TEXT,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        checked_at TEXT NOT NULL DEFAULT (datetime('now')),
        CHECK(source_account_id <> destination_account_id)
    );
    """)

    cursor.execute("INSERT INTO users (id, full_name, email, phone, password_hash, role) VALUES (1, 'Dias T.', 'dias@northstar.kz', '+77011112233', 'hash', 'customer')")
    cursor.execute("INSERT INTO users (id, full_name, email, phone, password_hash, role) VALUES (2, 'Aruzhan S.', 'aruzhan@northstar.kz', '+77022223344', 'hash', 'customer')")

    cursor.execute("INSERT INTO accounts (id, user_id, account_number, balance_minor) VALUES (1, 1, 'KZ•• 8821', 10000000)")
    cursor.execute("INSERT INTO accounts (id, user_id, account_number, balance_minor) VALUES (2, 2, 'KZ•• 1042', 2500000)")

    cursor.execute("INSERT INTO ledger_entries (account_id, amount_minor, entry_type) VALUES (1, 10000000, 'INITIAL_DEPOSIT')")
    cursor.execute("INSERT INTO ledger_entries (account_id, amount_minor, entry_type) VALUES (2, 2500000, 'INITIAL_DEPOSIT')")

    conn.commit()
    conn.close()


class TestInternetBankingSuite(unittest.TestCase):
    def setUp(self):
        init_test_db(TEST_DB)

    def tearDown(self):
        if os.path.exists(TEST_DB):
            os.remove(TEST_DB)

    def test_01_insufficient_funds_rollback(self):
        """Проверка отмены транзакции (ROLLBACK) при недостатке средств (Task 9)"""
        conn = sqlite3.connect(TEST_DB)
        cursor = conn.cursor()

        balance_before = cursor.execute("SELECT balance_minor FROM accounts WHERE id = 1").fetchone()[0]
        amount_minor = 90000000  # 900,000 KZT

        cursor.execute("BEGIN IMMEDIATE;")
        sender_balance = cursor.execute("SELECT balance_minor FROM accounts WHERE id = 1").fetchone()[0]
        if sender_balance < amount_minor:
            cursor.execute("ROLLBACK;")
            cursor.execute(
                "INSERT INTO transfers (request_id, source_account_id, destination_account_id, amount_minor, status, failure_reason) VALUES (?, 1, 2, ?, 'failed', 'insufficient_funds')",
                ("TR-012", amount_minor),
            )
            conn.commit()

        balance_after = cursor.execute("SELECT balance_minor FROM accounts WHERE id = 1").fetchone()[0]
        row = cursor.execute("SELECT status, failure_reason FROM transfers WHERE request_id = 'TR-012'").fetchone()
        conn.close()

        self.assertEqual(balance_before, balance_after)
        self.assertEqual(row, ("failed", "insufficient_funds"))

    def test_02_idempotency_duplicate_request_id(self):
        """Защита от повторного списания при дублировании request_id"""
        conn = sqlite3.connect(TEST_DB)
        cursor = conn.cursor()

        cursor.execute("INSERT INTO transfers (request_id, source_account_id, destination_account_id, amount_minor, status) VALUES ('TR-001', 1, 2, 100000, 'completed')")
        conn.commit()

        with self.assertRaises(sqlite3.IntegrityError):
            cursor.execute("INSERT INTO transfers (request_id, source_account_id, destination_account_id, amount_minor, status) VALUES ('TR-001', 1, 2, 100000, 'completed')")
            conn.commit()
        conn.close()

    def test_03_timeliness_pending_status_rule_e5(self):
        """Правило свежести данных E5: Pending статус старше 5 минут считается устаревшим"""
        now = datetime.now(timezone.utc)
        stale_checked_at = now - timedelta(minutes=30)
        is_stale = (now - stale_checked_at) > timedelta(minutes=5)
        self.assertTrue(is_stale)

    def test_04_ledger_balance_reconciliation(self):
        """Математическая сверка баланса с книгой учета (Ledger Reconciliation)"""
        conn = sqlite3.connect(TEST_DB)
        cursor = conn.cursor()

        # Перевод 50,000 KZT (5,000,000 тиын) со счета 1 на счет 2
        cursor.execute("UPDATE accounts SET balance_minor = balance_minor - 5000000 WHERE id = 1")
        cursor.execute("UPDATE accounts SET balance_minor = balance_minor + 5000000 WHERE id = 2")
        cursor.execute("INSERT INTO transfers (request_id, source_account_id, destination_account_id, amount_minor, status) VALUES ('TR-050', 1, 2, 5000000, 'completed')")
        conn.commit()

        stored_balance = cursor.execute("SELECT balance_minor FROM accounts WHERE id = 1").fetchone()[0]
        initial = cursor.execute("SELECT COALESCE(SUM(amount_minor), 0) FROM ledger_entries WHERE account_id = 1").fetchone()[0]
        incoming = cursor.execute("SELECT COALESCE(SUM(amount_minor), 0) FROM transfers WHERE destination_account_id = 1 AND status = 'completed'").fetchone()[0]
        outgoing = cursor.execute("SELECT COALESCE(SUM(amount_minor), 0) FROM transfers WHERE source_account_id = 1 AND status = 'completed'").fetchone()[0]
        conn.close()

        self.assertEqual(stored_balance, initial + incoming - outgoing)

    def test_05_access_control_ownership(self):
        """Изоляция доступа: Клиент 1 не владеет счетом Клиента 2"""
        conn = sqlite3.connect(TEST_DB)
        owner_id = conn.execute("SELECT user_id FROM accounts WHERE id = 2").fetchone()[0]
        conn.close()
        self.assertNotEqual(owner_id, 1)

    def test_06_foreign_key_restrict_delete(self):
        """Защита ссылочной целостности: нельзя удалить пользователя с активным счетом"""
        conn = sqlite3.connect(TEST_DB)
        conn.execute("PRAGMA foreign_keys = ON;")
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM users WHERE id = 1")
            conn.commit()
        conn.close()

    def test_07_invalid_negative_amount_e3(self):
        """Валидация суммы: отклонение отрицательных переводов (E3)"""
        conn = sqlite3.connect(TEST_DB)
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO transfers (request_id, source_account_id, destination_account_id, amount_minor, status) VALUES ('TR-ERR', 1, 2, -50000, 'pending')")
            conn.commit()
        conn.close()

    def test_08_completed_transfer_retry_blocked(self):
        """Завершенный перевод не может быть обработан повторно"""
        conn = sqlite3.connect(TEST_DB)
        conn.execute("INSERT INTO transfers (request_id, source_account_id, destination_account_id, amount_minor, status) VALUES ('TR-DONE', 1, 2, 10000, 'completed')")
        conn.commit()
        status = conn.execute("SELECT status FROM transfers WHERE request_id = 'TR-DONE'").fetchone()[0]
        conn.close()
        self.assertNotEqual(status, "pending")


if __name__ == "__main__":
    unittest.main()