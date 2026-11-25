"""Transfer input and daily-limit boundaries, using only temporary databases."""
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tests.test_bank import ARUZHAN, KZT, invoke
from tests.test_scheduled import FrozenDateTime
import server


class TransferValidationChecks(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        for item in (
            patch.object(server, "DB_PATH", Path(temp.name) / "limits.sqlite3"),
            patch.object(server, "PBKDF2_ROUNDS", 1_000),
            patch.object(server, "MIN_TRANSFER_CENTS", 100 * KZT),
            patch.object(server, "MAX_TRANSFER_CENTS", 1_000_000 * KZT),
            patch.object(server, "DAILY_LIMIT_CENTS", 2_000_000 * KZT),
            patch.object(FrozenDateTime, "current", datetime(2026, 11, 24, 19, 0, tzinfo=timezone.utc)),
            patch.object(server, "datetime", FrozenDateTime),
            patch.object(server.threading, "Timer"),
        ):
            item.start()
            self.addCleanup(item.stop)
        server.init_db()
        with server.connect() as db:
            db.execute("DELETE FROM transfers")
        code, headers, data = invoke("POST", "/api/login", {
            "email": ARUZHAN, "password": "DemoPass!23",
        })
        self.assertEqual(code, 200)
        self.cookie = headers["Set-Cookie"].split(";", 1)[0]
        self.csrf = data["csrfToken"]
        self.sequence = 0

    def submit(self, path="/api/transfers", **fields):
        self.sequence += 1
        payload = {
            "requestId": f"TR-{self.sequence:032X}",
            "fromAccountId": 1, "toAccountId": 2, "amount": "1000.01",
            "scheduledFor": "2026-11-25T00:02:00+05:00", **fields,
        }
        return invoke("POST", path, payload, cookie=self.cookie, csrf=self.csrf)

    def test_account_identifiers_are_not_silently_rounded_or_coerced(self):
        for path in ("/api/transfers", "/api/scheduled"):
            for field in ("fromAccountId", "toAccountId"):
                for value in (True, False, 1.9, 2.9, None, [], {}, "1.0", "2.9", 2**63):
                    with self.subTest(path=path, field=field, value=value):
                        code, _, data = self.submit(path, **{field: value})
                        self.assertEqual(code, 400)
                        self.assertIn("error", data)
        with server.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM transfers").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM scheduled_transfers").fetchone()[0], 0)
        server.threading.Timer.assert_not_called()

    def test_integer_account_ids_and_integer_strings_remain_supported(self):
        for path in ("/api/transfers", "/api/scheduled"):
            for sender, recipient in ((1, 2), ("1", "2")):
                with self.subTest(path=path, sender=sender):
                    code, _, data = self.submit(path, fromAccountId=sender, toAccountId=recipient)
                    self.assertIn(code, (201, 202))
                    self.assertEqual((data["fromAccountId"], data["toAccountId"], data["amount"]),
                                     (1, 2, 1000.01))

    def test_extreme_amounts_return_validation_errors_without_crashing(self):
        for path in ("/api/transfers", "/api/scheduled"):
            for amount in ("1e1000", "1e999999", "92233720368547758.08", "Infinity", "sNaN"):
                with self.subTest(path=path, amount=amount):
                    code, _, data = self.submit(path, amount=amount)
                    self.assertEqual(code, 400)
                    self.assertIn("error", data)
        with server.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM transfers").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM scheduled_transfers").fetchone()[0], 0)
        server.threading.Timer.assert_not_called()

    def test_daily_limit_uses_inclusive_utc_plus_five_midnight_and_exclusive_next_day(self):
        with server.connect() as db:
            for number, amount, status, timestamp in (
                (90, 1000, "Completed", "2026-11-24T18:59:59+00:00"),
                (91, 2000, "Completed", "2026-11-25T00:00:00+05:00"),
                (92, 3000, "Pending", "2026-11-25T18:59:59+00:00"),
                (93, 4000, "Completed", "2026-11-25T19:00:00+00:00"),
                (94, 5000, "Failed", "2026-11-24T19:00:00+00:00"),
            ):
                db.execute("""INSERT INTO transfers(request_id,sender_account_id,recipient_account_id,
                    amount_cents,status,created_at,checked_at) VALUES(?,1,2,?,?,?,?)""",
                           (f"TR-{number:032X}", amount * KZT, status, timestamp, timestamp))
        code, _, data = invoke("GET", "/api/limits", cookie=self.cookie)
        self.assertEqual(code, 200)
        self.assertEqual((data["usedToday"], data["remainingToday"]), (5000, 1_995_000))


if __name__ == "__main__":
    unittest.main(verbosity=2)
