"""Scheduled-transfer regression checks using isolated databases and mocked timers.

Run from internet-banking-demo_2: python -m unittest tests.test_scheduled -v
No test opens a network port, starts a worker thread, or uses banking.sqlite3.
"""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

import server  # noqa: E402
from tests.test_bank import ARUZHAN, DIAS, KZT, invoke  # noqa: E402


class FrozenDateTime(datetime):
    current = datetime(2026, 11, 24, 8, 0, tzinfo=timezone.utc)

    @classmethod
    def now(cls, tz=None):
        return cls.current.astimezone(tz) if tz else cls.current.replace(tzinfo=None)


class ScheduledTransferChecks(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        patches = (
            patch.object(server, "DB_PATH", Path(temp.name) / "scheduled.sqlite3"),
            patch.object(server, "PBKDF2_ROUNDS", 1_000),
            patch.object(server, "MIN_TRANSFER_CENTS", 100 * KZT),
            patch.object(server, "MAX_TRANSFER_CENTS", 1_000_000 * KZT),
            patch.object(server, "DAILY_LIMIT_CENTS", 2_000_000 * KZT),
            patch.object(FrozenDateTime, "current", datetime(2026, 11, 24, 8, 0, tzinfo=timezone.utc)),
            patch.object(server, "datetime", FrozenDateTime),
            patch.object(server.threading, "Timer"),
        )
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        self.timer = server.threading.Timer
        server.init_db()
        with server.connect() as db:
            db.execute("DELETE FROM transfers")
            db.execute("UPDATE accounts SET balance_cents=10000000 WHERE id=1")
            db.execute("UPDATE accounts SET balance_cents=2500000 WHERE id=2")
            db.execute("UPDATE accounts SET balance_cents=7500000 WHERE id=3")
        self.owner = self.login(ARUZHAN)

    def login(self, email):
        status, headers, data = invoke("POST", "/api/login", {"email": email, "password": "DemoPass!23"})
        self.assertEqual(status, 200)
        return headers["Set-Cookie"].split(";", 1)[0], data["csrfToken"]

    @staticmethod
    def reference(number):
        return "TR-" + f"{number:032X}"

    def schedule(self, number=1, session=None, **fields):
        cookie, csrf = session or self.owner
        payload = {
            "requestId": self.reference(number), "fromAccountId": 1, "toAccountId": 2,
            "amount": "1000.00",
            "scheduledFor": (FrozenDateTime.current + timedelta(minutes=2)).isoformat(),
            "reminderNote": "Tuition",
            **fields,
        }
        return invoke("POST", "/api/scheduled", payload, cookie=cookie, csrf=csrf)

    def cancel(self, number=1, session=None):
        cookie, csrf = session or self.owner
        return invoke("POST", f"/api/scheduled/{self.reference(number)}/cancel", {}, cookie=cookie, csrf=csrf)

    def scheduled_row(self, number=1):
        with server.connect() as db:
            return db.execute("SELECT * FROM scheduled_transfers WHERE request_id=?",
                              (self.reference(number),)).fetchone()

    def ledger_rows(self, number=None):
        with server.connect() as db:
            if number is None:
                return db.execute("SELECT * FROM transfers").fetchall()
            return db.execute("SELECT * FROM transfers WHERE request_id=?", (self.reference(number),)).fetchall()

    def balances(self):
        with server.connect() as db:
            return dict(db.execute("SELECT id,balance_cents FROM accounts"))

    def limits(self):
        status, _, data = invoke("GET", "/api/limits", cookie=self.owner[0])
        self.assertEqual(status, 200)
        return data

    def process_due(self, number=1):
        due = datetime.fromisoformat(self.scheduled_row(number)["scheduled_for"])
        FrozenDateTime.current = max(FrozenDateTime.current, due)
        server.process_scheduled_transfer(self.reference(number))

    def seed_ledger(self, number, amount, status, created=None, sender=1):
        timestamp = created or server.now()
        with server.connect() as db:
            db.execute("""INSERT INTO transfers(request_id,sender_account_id,recipient_account_id,
                amount_cents,status,created_at,checked_at) VALUES(?,?,2,?,?,?,?)""",
                       (self.reference(number), sender, amount * KZT, status, timestamp, timestamp))

    def assert_failed_without_movement(self, number, before, reason):
        row = self.scheduled_row(number)
        self.assertEqual(row["status"], "Failed")
        self.assertIn(reason, row["failure_reason"])
        self.assertEqual(self.balances(), before)
        self.assertEqual(self.ledger_rows(number), [])

    def test_creation_stores_instruction_without_debit_or_quota_reservation(self):
        before = self.balances()
        # Together these exceed the current balance, but scheduling reserves no money.
        for number in (1, 2):
            status, _, data = self.schedule(number, amount="60000.25")
            self.assertEqual((status, data["status"]), (201, "Scheduled"))
            self.assertEqual(data["requestId"], self.reference(number))
        row = self.scheduled_row()
        self.assertEqual((row["user_id"], row["sender_account_id"], row["recipient_account_id"]), (1, 1, 2))
        self.assertEqual((row["amount_cents"], row["reminder_note"]), (6_000_025, "Tuition"))
        self.assertEqual(self.balances(), before)
        self.assertEqual(self.ledger_rows(), [])
        self.assertEqual((self.limits()["usedToday"], self.limits()["remainingToday"]), (0, 2_000_000))
        self.assertEqual(self.timer.call_count, 2)

    def test_scheduling_does_not_require_unused_quota_today(self):
        self.seed_ledger(90, 2_000_000, "Completed")
        self.assertEqual(self.limits()["remainingToday"], 0)
        self.assertEqual(self.schedule()[0], 201)
        self.assertEqual(self.limits()["usedToday"], 2_000_000)

    def test_scheduled_date_boundaries_and_invalid_dates(self):
        cases = (
            (timedelta(seconds=29), 400), (timedelta(seconds=30), 201),
            (timedelta(days=365), 201), (timedelta(days=365, seconds=1), 400),
        )
        for number, (offset, expected) in enumerate(cases, 1):
            with self.subTest(offset=offset):
                status, _, _ = self.schedule(number, scheduledFor=(FrozenDateTime.current + offset).isoformat())
                self.assertEqual(status, expected)
                self.assertEqual(self.scheduled_row(number) is not None, expected == 201)
        for number, bad in enumerate(("", "not-a-date", "2026-02-30T10:00:00+05:00"), 10):
            with self.subTest(value=bad):
                self.assertEqual(self.schedule(number, scheduledFor=bad)[0], 400)
                self.assertIsNone(self.scheduled_row(number))

    def test_local_schedule_time_is_interpreted_as_utc_plus_five(self):
        for number, value in enumerate(("2026-11-24T13:02", "2026-11-24T13:02:00+05:00"), 1):
            self.assertEqual(self.schedule(number, scheduledFor=value)[0], 201)
            self.assertEqual(self.scheduled_row(number)["scheduled_for"], "2026-11-24T08:02:00+00:00")

    def test_out_of_range_utc_dates_return_validation_errors(self):
        for number, value in enumerate(("0001-01-01T00:00:00+23:00", "9999-12-31T23:59:59-23:00"), 1):
            with self.subTest(value=value):
                code, _, data = self.schedule(number, scheduledFor=value)
                self.assertEqual(code, 400)
                self.assertIn("valid scheduled date", data["error"])
                self.assertIsNone(self.scheduled_row(number))
        self.timer.assert_not_called()

    def test_fractional_scheduled_time_is_not_truncated_to_an_earlier_instant(self):
        FrozenDateTime.current = FrozenDateTime.current.replace(microsecond=900000)
        due = FrozenDateTime.current + timedelta(seconds=30)
        code, _, _ = self.schedule(scheduledFor=due.isoformat())
        self.assertEqual(code, 201)
        self.assertEqual(datetime.fromisoformat(self.scheduled_row()["scheduled_for"]), due)
        self.timer.assert_called_once_with(30.0, server.process_scheduled_transfer, args=(self.reference(1),))

    def test_create_validates_amount_account_ownership_and_note_length(self):
        cases = ({"amount": "99.99"}, {"amount": "1000000.01"}, {"amount": "100.001"},
                 {"fromAccountId": 2}, {"toAccountId": 1}, {"toAccountId": 999},
                 {"reminderNote": "x" * 201})
        for number, fields in enumerate(cases, 1):
            with self.subTest(fields=fields):
                self.assertEqual(self.schedule(number, **fields)[0], 400)
                self.assertIsNone(self.scheduled_row(number))
        self.timer.assert_not_called()

    def test_owner_cancels_and_cancelled_worker_cannot_move_money(self):
        self.assertEqual(self.schedule()[0], 201)
        before = self.balances()
        status, _, data = self.cancel()
        self.assertEqual((status, data["status"]), (200, "Cancelled"))
        self.process_due()
        self.assertEqual(self.scheduled_row()["status"], "Cancelled")
        self.assertEqual(self.balances(), before)
        self.assertEqual(self.ledger_rows(), [])
        self.assertEqual(self.cancel()[0], 409)

    def test_other_user_cannot_cancel_or_view_private_schedule(self):
        self.assertEqual(self.schedule(toAccountId=3)[0], 201)
        stranger = self.login(DIAS)
        self.assertEqual(self.cancel(session=stranger)[0], 404)
        self.assertEqual(invoke("GET", f"/api/transfers/{self.reference(1)}", cookie=stranger[0])[0], 404)
        self.assertEqual(self.scheduled_row()["status"], "Scheduled")

    def test_cancel_requires_authentication_and_csrf(self):
        self.assertEqual(self.schedule()[0], 201)
        path = f"/api/scheduled/{self.reference(1)}/cancel"
        self.assertEqual(invoke("POST", path, {})[0], 401)
        for token in ("", "incorrect"):
            self.assertEqual(invoke("POST", path, {}, cookie=self.owner[0], csrf=token)[0], 403)
        self.assertEqual(self.scheduled_row()["status"], "Scheduled")

    def test_success_moves_balances_and_creates_one_completed_ledger_entry(self):
        self.assertEqual(self.schedule(amount="1000.25")[0], 201)
        self.process_due()
        row = self.scheduled_row()
        self.assertEqual((row["status"], row["failure_reason"]), ("Completed", None))
        self.assertEqual(self.balances(), {1: 9_899_975, 2: 2_600_025, 3: 7_500_000})
        ledger = self.ledger_rows(1)
        self.assertEqual(len(ledger), 1)
        self.assertEqual((ledger[0]["request_id"], ledger[0]["status"]), (row["request_id"], "Completed"))
        self.assertEqual((ledger[0]["amount_cents"], ledger[0]["description"]), (100_025, "Tuition"))
        self.assertEqual(ledger[0]["scheduled_for"], row["scheduled_for"])
        self.assertEqual(ledger[0]["created_at"], server.now())
        self.assertEqual(ledger[0]["completed_at"], server.now())
        status, _, details = invoke("GET", f"/api/transfers/{self.reference(1)}", cookie=self.owner[0])
        self.assertEqual((status, details["completedAt"]), (200, server.now()))
        self.assertEqual(self.limits()["usedToday"], 1000.25)
        self.assertEqual(self.cancel()[0], 409)

    def test_duplicate_worker_does_not_move_money_twice(self):
        self.assertEqual(self.schedule()[0], 201)
        self.process_due()
        before = self.balances()
        server.process_scheduled_transfer(self.reference(1))
        server.process_scheduled_transfer(self.reference(999))
        self.assertEqual(self.balances(), before)
        self.assertEqual(len(self.ledger_rows(1)), 1)

    def test_insufficient_funds_at_execution_fail_without_debit(self):
        self.assertEqual(self.schedule(amount="50000")[0], 201)
        # A user can spend the money between scheduling and execution.
        with server.connect() as db:
            db.execute("UPDATE accounts SET balance_cents=1000000 WHERE id=1")
        before = self.balances()
        self.process_due()
        self.assert_failed_without_movement(1, before, "Insufficient funds")
        self.assertEqual(self.cancel()[0], 409)
        server.process_scheduled_transfer(self.reference(1))
        self.assertEqual(self.balances(), before)

    def test_execution_rechecks_changed_minimum_and_maximum(self):
        for number, setting, value, reason in (
            (1, "MIN_TRANSFER_CENTS", 2000 * KZT, "below the minimum"),
            (2, "MAX_TRANSFER_CENTS", 500 * KZT, "maximum per-transfer"),
        ):
            with self.subTest(setting=setting):
                self.assertEqual(self.schedule(number)[0], 201)
                before = self.balances()
                with patch.object(server, setting, value):
                    self.process_due(number)
                self.assert_failed_without_movement(number, before, reason)

    def test_execution_rechecks_account_ownership(self):
        self.assertEqual(self.schedule()[0], 201)
        with server.connect() as db:
            db.execute("UPDATE accounts SET owner_user_id=2 WHERE id=1")
        before = self.balances()
        self.process_due()
        self.assert_failed_without_movement(1, before, "Account is no longer available")

    def test_execution_rechecks_daily_quota_across_all_owned_accounts(self):
        self.assertEqual(self.schedule()[0], 201)
        with server.connect() as db:
            db.execute("INSERT INTO accounts(id,customer_id,label,balance_cents,owner_user_id) VALUES(4,1,'Savings',0,1)")
        # This spending happened after the scheduled instruction was created.
        self.seed_ledger(90, 1_999_500, "Pending", sender=4)
        before = self.balances()
        self.process_due()
        self.assert_failed_without_movement(1, before, "Daily transfer limit")

    def test_execution_uses_actual_day_and_ignores_failed_transfers(self):
        self.assertEqual(self.schedule()[0], 201)
        self.seed_ledger(90, 2_000_000, "Completed")
        FrozenDateTime.current += timedelta(days=1)
        self.seed_ledger(91, 2_000_000, "Failed")
        self.seed_ledger(92, 1_999_000, "Pending")
        # Yesterday's completed spending and today's failed transfer do not count.
        # The new transfer can consume exactly the remaining 1,000 KZT.
        self.process_due()
        self.assertEqual(self.scheduled_row()["status"], "Completed")
        self.owner = self.login(ARUZHAN)  # The previous day's eight-hour session expired.
        self.assertEqual(self.limits()["usedToday"], 2_000_000)

    def test_duplicate_schedule_reference_returns_conflict_without_new_instruction(self):
        self.assertEqual(self.schedule()[0], 201)
        before = self.balances()
        for fields in ({}, {"amount": "2000"}):
            with self.subTest(fields=fields):
                self.assertEqual(self.schedule(**fields)[0], 409)
        with server.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM scheduled_transfers").fetchone()[0], 1)
        self.assertEqual(self.scheduled_row()["amount_cents"], 1000 * KZT)
        self.assertEqual(self.balances(), before)
        self.timer.assert_called_once()

    def test_reference_already_used_by_regular_transfer_cannot_be_scheduled(self):
        self.seed_ledger(1, 1000, "Completed")
        self.assertEqual(self.schedule()[0], 409)
        self.assertIsNone(self.scheduled_row())
        self.timer.assert_not_called()

    def test_worker_called_early_reschedules_without_processing(self):
        self.assertEqual(self.schedule()[0], 201)
        before = self.balances()
        self.timer.reset_mock()
        server.process_scheduled_transfer(self.reference(1))
        self.timer.assert_called_once_with(120.0, server.process_scheduled_transfer, args=(self.reference(1),))
        self.assertEqual(self.scheduled_row()["status"], "Scheduled")
        self.assertEqual(self.balances(), before)
        self.assertEqual(self.ledger_rows(), [])

    def test_restart_resumes_future_and_overdue_instructions_only(self):
        self.assertEqual(self.schedule(1, scheduledFor=(FrozenDateTime.current + timedelta(seconds=60)).isoformat())[0], 201)
        self.assertEqual(self.schedule(2)[0], 201)
        self.assertEqual(self.schedule(3)[0], 201)
        self.assertEqual(self.cancel(3)[0], 200)
        FrozenDateTime.current += timedelta(seconds=90)
        before = self.balances()
        server.init_db()  # Startup migration/initialization preserves saved instructions.
        self.timer.reset_mock()
        server.schedule_pending()
        calls = {call.kwargs["args"][0]: (call.args[0], call.args[1]) for call in self.timer.call_args_list}
        self.assertEqual(calls, {
            self.reference(1): (0, server.process_scheduled_transfer),
            self.reference(2): (30.0, server.process_scheduled_transfer),
        })
        self.assertEqual(self.balances(), before)
        self.process_due(1)
        self.assertEqual(self.scheduled_row(1)["status"], "Completed")
        self.assertEqual(self.scheduled_row(2)["status"], "Scheduled")
        self.assertEqual(self.scheduled_row(3)["status"], "Cancelled")
        self.assertEqual(len(self.ledger_rows()), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
