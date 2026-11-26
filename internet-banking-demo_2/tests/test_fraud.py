"""Suspicious-transfer detector (risk_score / risk_reason), using only temporary databases.

Run from the project folder:   python -m unittest tests.test_fraud -v
"""
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tests.test_bank import ARUZHAN, KZT, invoke
from tests.test_scheduled import FrozenDateTime
import server

NOW = datetime(2026, 11, 24, 8, 0, tzinfo=timezone.utc)


class FraudDetectorChecks(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        for item in (
            patch.object(server, "DB_PATH", Path(temp.name) / "fraud.sqlite3"),
            patch.object(server, "PBKDF2_ROUNDS", 1_000),
            patch.object(server, "MIN_TRANSFER_CENTS", 100 * KZT),
            patch.object(server, "MAX_TRANSFER_CENTS", 1_000_000 * KZT),
            patch.object(server, "DAILY_LIMIT_CENTS", 2_000_000 * KZT),
            patch.object(server, "RISK_NEW_RECIPIENT_CENTS", 50_000 * KZT),
            patch.object(FrozenDateTime, "current", NOW),
            patch.object(server, "datetime", FrozenDateTime),
            patch.object(server, "schedule_transfer"),
        ):
            item.start()
            self.addCleanup(item.stop)
        server.init_db()
        with server.connect() as db:
            db.execute("DELETE FROM transfers")
            db.execute("DELETE FROM audit_logs")
            db.execute("UPDATE accounts SET balance_cents=10000000 WHERE id=1")
        code, headers, data = invoke("POST", "/api/login", {"email": ARUZHAN, "password": "DemoPass!23"})
        self.assertEqual(code, 200)
        self.cookie = headers["Set-Cookie"].split(";", 1)[0]
        self.csrf = data["csrfToken"]
        self.sequence = 0

    def seed(self, amount_kzt, recipient=2, minutes_ago=600, status="Completed"):
        """Insert a historical transfer from Aruzhan (account 1)."""
        self.sequence += 1
        created = datetime.fromtimestamp(NOW.timestamp() - minutes_ago * 60, timezone.utc).isoformat()
        with server.connect() as db:
            db.execute("""INSERT INTO transfers(request_id,sender_account_id,recipient_account_id,amount_cents,
                status,created_at,checked_at) VALUES(?,1,?,?,?,?,?)""",
                       (f"TR-{0xF0000 + self.sequence:032X}", recipient, amount_kzt * KZT, status, created, created))

    def submit(self, amount, recipient=2):
        self.sequence += 1
        rid = f"TR-{self.sequence:032X}"
        code, _, _ = invoke("POST", "/api/transfers",
                            {"requestId": rid, "fromAccountId": 1, "toAccountId": recipient, "amount": amount},
                            cookie=self.cookie, csrf=self.csrf)
        self.assertEqual(code, 202)
        with server.connect() as db:
            row = db.execute("SELECT risk_score,risk_reason FROM transfers WHERE request_id=?", (rid,)).fetchone()
            flags = db.execute("""SELECT details FROM audit_logs WHERE entity_id=?
                AND action='Transfer flagged as suspicious'""", (rid,)).fetchall()
        return row["risk_score"], row["risk_reason"], [f["details"] for f in flags]

    def test_normal_transfer_is_not_flagged(self):
        self.seed(1_000)
        self.seed(1_200)
        score, reason, flags = self.submit("1500")
        self.assertEqual((score, reason, flags), (0, "", []))

    def test_amount_above_three_times_average_is_flagged(self):
        self.seed(1_000)
        self.seed(1_000)
        score, reason, flags = self.submit("3500")
        self.assertEqual(score, 40)
        self.assertIn("3.5x the customer's average", reason)
        self.assertEqual(len(flags), 1)
        self.assertIn("Risk score 40/100", flags[0])

    def test_exactly_three_times_average_is_not_flagged(self):
        self.seed(1_000)
        self.assertEqual(self.submit("3000")[0], 0)

    def test_failed_transfers_do_not_lower_the_average(self):
        self.seed(10_000)
        self.seed(10, status="Failed")
        self.assertEqual(self.submit("20000")[0], 0)

    def test_large_amount_to_new_recipient_is_flagged(self):
        self.seed(40_000, recipient=2)
        score, reason, flags = self.submit("50000", recipient=3)
        self.assertEqual(score, 35)
        self.assertIn("to a new recipient", reason)
        self.assertEqual(len(flags), 1)

    def test_large_amount_to_known_recipient_is_not_flagged(self):
        self.seed(40_000, recipient=3)
        self.assertEqual(self.submit("50000", recipient=3)[0], 0)

    def test_sixth_transfer_within_ten_minutes_is_flagged(self):
        for minutes in (1, 2, 3, 4, 9):
            self.seed(1_000, minutes_ago=minutes)
        score, reason, _ = self.submit("1000")
        self.assertEqual(score, 25)
        self.assertIn("More than 5 transfers in 10 minutes", reason)

    def test_transfers_older_than_ten_minutes_do_not_count_as_burst(self):
        for minutes in (1, 2, 3, 4, 11):
            self.seed(1_000, minutes_ago=minutes)
        self.assertEqual(self.submit("1000")[0], 0)

    def test_rules_combine_and_flag_is_visible_in_audit_log(self):
        for minutes in range(1, 6):
            self.seed(1_000, minutes_ago=minutes)
        score, reason, _ = self.submit("60000", recipient=3)
        self.assertEqual(score, 100)
        self.assertEqual(reason.count(";"), 2)
        code, _, logs = invoke("GET", "/api/audit-logs", cookie=self.cookie)
        self.assertEqual(code, 200)
        flagged = [log for log in logs if log["action"] == "Transfer flagged as suspicious"]
        self.assertEqual(len(flagged), 1)
        self.assertIn("Risk score 100/100", flagged[0]["details"])

    def test_flagged_transfer_is_still_accepted_and_processed(self):
        self.seed(1_000)
        self.submit("5000")
        rid = f"TR-{self.sequence:032X}"
        server.process_transfer(rid)
        with server.connect() as db:
            self.assertEqual(db.execute("SELECT status FROM transfers WHERE request_id=?", (rid,)).fetchone()[0],
                             "Completed")


if __name__ == "__main__":
    unittest.main(verbosity=2)
