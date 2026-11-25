"""Calendar analytics and completion-time regressions, using temporary databases only."""
import io
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import server
from tests.test_bank import invoke


class AnalyticsChecks(unittest.TestCase):
    AS_OF = "2026-11-24T10:00:00+00:00"

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.db_patch = patch.object(server, "DB_PATH", Path(temporary.name) / "analytics.sqlite3")
        self.db_patch.start()
        self.addCleanup(self.db_patch.stop)
        rounds = patch.object(server, "PBKDF2_ROUNDS", 1_000)
        rounds.start()
        self.addCleanup(rounds.stop)
        server.init_db()
        with server.connect() as db:
            db.execute("DELETE FROM transfers")
            db.execute("DELETE FROM audit_logs")
            db.execute("UPDATE accounts SET balance_cents=10000000 WHERE id=1")
        self.cookie, self.csrf = self.login("aruzhan@example.test")
        self.serial = 0

    def login(self, email):
        status, headers, data = invoke("POST", "/api/login", {"email": email, "password": "DemoPass!23"})
        self.assertEqual(status, 200)
        return headers["Set-Cookie"].split(";", 1)[0], data["csrfToken"]

    def seed(self, cents=10_000, completed=None, sender=1, recipient=2,
             status="Completed", created=None, request_id=None):
        self.serial += 1
        request_id = request_id or f"TR-{self.serial:032X}"
        completed = completed or self.AS_OF
        with server.connect() as db:
            db.execute("""INSERT INTO transfers(request_id,sender_account_id,recipient_account_id,
                amount_cents,status,created_at,checked_at,completed_at)
                VALUES(?,?,?,?,?,?,?,?)""",
                       (request_id, sender, recipient, cents, status, created or completed,
                        completed, completed if status == "Completed" else None))
        return request_id

    def analytics(self, as_of=None, cookie=None, path="/api/analytics"):
        with patch.object(server, "now", return_value=as_of or self.AS_OF):
            status, _, data = invoke("GET", path, cookie=cookie or self.cookie)
        self.assertEqual(status, 200)
        return data

    def test_empty_state_and_authentication(self):
        self.assertEqual(invoke("GET", "/api/analytics")[0], 401)
        data = self.analytics()
        self.assertEqual((data["currency"], data["timeZone"], data["asOf"]), ("KZT", "UTC+5", self.AS_OF))
        for period in (data["week"], data["month"]):
            self.assertEqual((period["totalAmount"], period["count"]), (0, 0))
        self.assertIsNone(data["month"]["largest"])
        self.assertIsNone(data["month"]["smallest"])
        self.assertEqual(data["month"]["frequentRecipients"], [])

    def test_public_assets_use_exact_allowlist_and_content_types(self):
        # Temporary assets keep this handler test independent of frontend builds.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            assets = {"/": ("index.html", "text/html; charset=utf-8", b"<!doctype html>"),
                      "/analytics.js": ("analytics.js", "text/javascript; charset=utf-8", b"window.analytics = {};"),
                      "/analytics.css": ("analytics.css", "text/css; charset=utf-8", b".analytics { display: grid; }")}
            for filename, _, content in assets.values():
                (root / filename).write_bytes(content)
            (root / "private.txt").write_text("Do not serve this file", encoding="utf-8")
            with patch.object(server, "ROOT", root):
                for path, (_, content_type, content) in assets.items():
                    with self.subTest(path=path):
                        handler = server.Handler.__new__(server.Handler)
                        handler.path, handler.headers = path, {}
                        handler.wfile = io.BytesIO()
                        response, headers = {}, {}
                        handler.send_response = lambda code: response.update(status=code)
                        handler.send_header = lambda key, value: headers.update({key: value})
                        handler.end_headers = lambda: None
                        handler.do_GET()
                        self.assertEqual(response["status"], 200)
                        self.assertEqual(headers["Content-Type"], content_type)
                        self.assertEqual(headers["Content-Length"], str(len(content)))
                        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
                        self.assertEqual(handler.wfile.getvalue(), content)
                for path in ("/private.txt", "/server.py", "/banking.sqlite3", "/../server.py",
                             "/%2e%2e/server.py", "/analytics.js/../private.txt"):
                    with self.subTest(path=path):
                        self.assertEqual(invoke("GET", path)[0], 401)
                        self.assertEqual(invoke("GET", path, cookie=self.cookie)[0], 404)

    def test_week_and_month_use_local_boundaries_and_exclude_future(self):
        self.seed(100_000, "2026-10-31T18:59:59+00:00")  # October in UTC+5
        self.seed(10_000, "2026-10-31T19:00:00+00:00")   # November starts
        self.seed(20_000, "2026-11-22T18:59:59+00:00")   # Sunday 23:59:59
        self.seed(30_000, "2026-11-22T19:00:00+00:00")   # Monday 00:00
        self.seed(40_000, self.AS_OF)
        self.seed(500_000, "2026-11-24T10:00:01+00:00")
        self.seed(600_000, "2026-11-30T19:00:00+00:00")  # December starts
        data = self.analytics()
        self.assertEqual(data["week"], {"start": "2026-11-22T19:00:00+00:00",
                                       "end": "2026-11-29T19:00:00+00:00",
                                       "totalAmount": 700, "count": 2})
        self.assertEqual((data["month"]["start"], data["month"]["end"]),
                         ("2026-10-31T19:00:00+00:00", "2026-11-30T19:00:00+00:00"))
        self.assertEqual((data["month"]["totalAmount"], data["month"]["count"]), (1000, 4))
        self.assertEqual(data["month"]["smallest"]["amount"], 100)
        self.assertEqual(data["month"]["largest"]["amount"], 400)

    def test_week_crosses_year_and_month_boundaries(self):
        self.seed(100_000, "2026-12-27T18:59:59+00:00")
        self.seed(10_000, "2026-12-27T19:00:00+00:00")  # Monday, December 28
        self.seed(20_000, "2026-12-31T18:59:59+00:00")
        self.seed(30_000, "2026-12-31T19:00:00+00:00")  # January 1 in UTC+5
        data = self.analytics("2026-12-31T20:00:00+00:00")
        self.assertEqual((data["week"]["start"], data["week"]["end"]),
                         ("2026-12-27T19:00:00+00:00", "2027-01-03T19:00:00+00:00"))
        self.assertEqual((data["month"]["start"], data["month"]["end"]),
                         ("2026-12-31T19:00:00+00:00", "2027-01-31T19:00:00+00:00"))
        self.assertEqual((data["week"]["totalAmount"], data["week"]["count"]), (600, 3))
        self.assertEqual((data["month"]["totalAmount"], data["month"]["count"]), (300, 1))

    def test_offset_timestamps_represent_instants_at_local_period_boundaries(self):
        # The database may contain timestamps with offsets from older imports.
        # Compare instants, not the visible date or the timestamp string.
        self.seed(10_000, "2026-11-01T00:00:00+05:00")
        self.seed(20_000, "2026-11-22T23:59:59+05:00")
        self.seed(30_000, "2026-11-23T00:00:00+05:00")
        self.seed(40_000, "2026-11-22T14:00:00-05:00")  # Same Monday boundary
        self.seed(50_000, "2026-11-24T15:00:00+05:00")  # Exactly asOf
        self.seed(900_000, "2026-11-24T15:00:01+05:00")  # Future instant
        data = self.analytics()
        self.assertEqual((data["week"]["totalAmount"], data["week"]["count"]), (1200, 3))
        self.assertEqual((data["month"]["totalAmount"], data["month"]["count"]), (1500, 5))
        self.assertEqual(data["month"]["largest"]["amount"], 500)
        self.assertEqual(data["month"]["smallest"]["amount"], 100)

    def test_month_end_handles_leap_year_and_exact_rollover(self):
        self.seed(10_000, "2028-02-29T18:59:59+00:00")
        self.seed(20_000, "2028-02-29T19:00:00+00:00")
        february = self.analytics("2028-02-29T18:59:59+00:00")
        self.assertEqual(february["month"]["end"], "2028-02-29T19:00:00+00:00")
        self.assertEqual(february["month"]["totalAmount"], 100)
        march = self.analytics("2028-02-29T19:00:00+00:00")
        self.assertEqual((march["month"]["count"], march["month"]["totalAmount"]), (1, 200))

    def test_only_completed_outgoing_owned_accounts_and_no_private_fields(self):
        self.seed(10_033)
        self.seed(20_067, sender=2, recipient=1)  # incoming
        self.seed(30_000, sender=2, recipient=3)  # another customer's transfer
        self.seed(40_000, status="Pending")
        self.seed(50_000, status="Failed")
        with server.connect() as db:
            db.execute("INSERT INTO accounts(id,customer_id,label,balance_cents,owner_user_id) VALUES(4,1,'Second',0,1)")
        self.seed(20_067, sender=4)
        data = self.analytics(path="/api/analytics?userId=2&accountId=2")
        self.assertEqual((data["month"]["count"], data["month"]["totalAmount"]), (2, 301))
        encoded = json.dumps(data)
        for private_value in ("password_hash", "csrfToken", "session_id", "balance", "@example.test"):
            self.assertNotIn(private_value, encoded)
        dias_cookie, _ = self.login("dias@example.test")
        dias = self.analytics(cookie=dias_cookie)
        self.assertEqual((dias["month"]["count"], dias["month"]["totalAmount"]), (2, 500.67))

    def test_entire_ledger_and_integer_tiyn_precision(self):
        for _ in range(55):
            self.seed(10_001)
        self.seed(20_045)
        history = invoke("GET", "/api/transfers", cookie=self.cookie)[2]
        self.assertEqual(len(history), 50)
        data = self.analytics()
        self.assertEqual((data["month"]["count"], data["week"]["count"]), (56, 56))
        self.assertEqual(data["month"]["totalAmount"], 5701)
        self.assertEqual(data["month"]["smallest"]["amount"], 100.01)
        self.assertEqual(data["month"]["largest"]["amount"], 200.45)

    def test_frequency_groups_recipient_person_and_returns_all_tied_leaders(self):
        with server.connect() as db:
            db.execute("INSERT INTO accounts(id,customer_id,label,balance_cents,owner_user_id) VALUES(4,2,'Second',0,2)")
        self.seed(10_000, recipient=2)
        self.seed(20_000, recipient=4)
        self.seed(900_000, recipient=3)
        month = self.analytics()["month"]
        self.assertEqual(month["largest"]["recipient"], "Amina K.")
        self.assertEqual(month["frequentRecipients"], [
            {"recipientId": 2, "name": "Dias T.", "count": 2, "totalAmount": 300}])
        self.seed(10_000, recipient=3)
        leaders = self.analytics()["month"]["frequentRecipients"]
        self.assertEqual(leaders, [
            {"recipientId": 3, "name": "Amina K.", "count": 2, "totalAmount": 9100},
            {"recipientId": 2, "name": "Dias T.", "count": 2, "totalAmount": 300}])

    def test_equal_amounts_have_stable_ties_and_completion_controls_period(self):
        early = "2026-11-22T20:00:00+00:00"
        self.seed(10_000, self.AS_OF, request_id="TR-" + "3" * 32)
        self.seed(10_000, early, request_id="TR-" + "2" * 32)
        first = self.seed(10_000, early, created="2026-10-30T10:00:00+00:00", request_id="TR-" + "1" * 32)
        month = self.analytics()["month"]
        self.assertEqual(month["count"], 3)
        self.assertEqual(month["largest"]["requestId"], first)
        self.assertEqual(month["smallest"]["requestId"], first)
        self.assertEqual(month["largest"]["completedAt"], early)

    def test_recipient_frequency_does_not_merge_people_with_the_same_name(self):
        with server.connect() as db:
            db.execute("UPDATE users SET full_name='Same Name' WHERE id IN (2,3)")
        self.seed(10_000, recipient=2)
        self.seed(20_000, recipient=3)
        self.assertEqual(self.analytics()["month"]["frequentRecipients"], [
            {"recipientId": 2, "name": "Same Name", "count": 1, "totalAmount": 100},
            {"recipientId": 3, "name": "Same Name", "count": 1, "totalAmount": 200}])
        self.seed(30_000, recipient=3)
        self.assertEqual(self.analytics()["month"]["frequentRecipients"], [
            {"recipientId": 3, "name": "Same Name", "count": 2, "totalAmount": 500}])

    def test_worker_records_completion_once_and_receipt_views_preserve_terminal_times(self):
        completed_at = self.AS_OF
        rid = self.seed(status="Pending", created="2026-10-30T10:00:00+00:00")
        with patch.object(server, "now", return_value=completed_at):
            server.process_transfer(rid)
        with server.connect() as db:
            before = dict(db.execute("SELECT * FROM transfers WHERE request_id=?", (rid,)).fetchone())
            balances = list(db.execute("SELECT id,balance_cents FROM accounts ORDER BY id"))
        self.assertEqual((before["status"], before["completed_at"], before["checked_at"]),
                         ("Completed", completed_at, completed_at))
        with patch.object(server, "now", return_value="2026-12-01T10:00:00+00:00"):
            server.process_transfer(rid)
            status, _, receipt = invoke("GET", f"/api/transfers/{rid}", cookie=self.cookie)
        self.assertEqual(status, 200)
        self.assertEqual((receipt["completedAt"], receipt["checkedAt"]), (completed_at, completed_at))
        with server.connect() as db:
            self.assertEqual(dict(db.execute("SELECT * FROM transfers WHERE request_id=?", (rid,)).fetchone()), before)
            self.assertEqual(list(db.execute("SELECT id,balance_cents FROM accounts ORDER BY id")), balances)
        self.assertEqual(self.analytics()["month"]["count"], 1)
        failed = self.seed(status="Failed")
        with patch.object(server, "now", return_value="2026-12-01T10:00:00+00:00"):
            receipt = invoke("GET", f"/api/transfers/{failed}", cookie=self.cookie)[2]
        self.assertEqual(receipt["checkedAt"], self.AS_OF)
        self.assertIsNone(receipt["completedAt"])

    def test_completed_schedule_counts_once_and_unexecuted_schedules_do_not_count(self):
        due = (datetime.now(timezone.utc) - timedelta(seconds=2)).isoformat(timespec="seconds")
        completed_at = server.now()
        for index, status in enumerate(("Scheduled", "Cancelled", "Failed"), start=100):
            rid = f"TR-{index:032X}"
            with server.connect() as db:
                db.execute("""INSERT INTO scheduled_transfers(request_id,user_id,sender_account_id,
                    recipient_account_id,amount_cents,scheduled_for,status,created_at,updated_at)
                    VALUES(?,1,1,2,10033,?,?,?,?)""", (rid, due, status, due, due))
        self.assertEqual(self.analytics(completed_at)["month"]["count"], 0)
        rid = f"TR-{100:032X}"
        with patch.object(server, "now", return_value=completed_at):
            server.process_scheduled_transfer(rid)
            server.process_scheduled_transfer(rid)
        data = self.analytics(completed_at)
        self.assertEqual((data["month"]["count"], data["month"]["totalAmount"]), (1, 100.33))
        with server.connect() as db:
            self.assertEqual(db.execute("SELECT status FROM scheduled_transfers WHERE request_id=?", (rid,)).fetchone()[0], "Completed")
            transfer = db.execute("SELECT * FROM transfers WHERE request_id=?", (rid,)).fetchone()
            self.assertEqual(transfer["completed_at"], completed_at)
        history = invoke("GET", "/api/transfers", cookie=self.cookie)[2]
        self.assertEqual(sum(item["requestId"] == rid for item in history), 1)

    def test_migration_recovers_trustworthy_times_and_is_idempotent(self):
        created = "2026-10-31T18:58:00+00:00"
        success = "2026-10-31T19:01:00+00:00"
        overwritten = "2026-12-01T12:00:00+00:00"
        ids = {name: self.seed(created=created) for name in ("audit", "schedule", "fallback", "mismatch", "pending", "failed")}
        with server.connect() as db:
            # Rebuild the pre-feature schema by removing only the new empty column.
            db.execute("DROP INDEX transfers_sender_completed")
            db.execute("ALTER TABLE transfers DROP COLUMN completed_at")
            db.execute("UPDATE transfers SET checked_at=?", (overwritten,))
            db.execute("UPDATE transfers SET status='Pending' WHERE request_id=?", (ids["pending"],))
            db.execute("UPDATE transfers SET status='Failed' WHERE request_id=?", (ids["failed"],))
            for action, timestamp in (("Transfer details viewed", overwritten),
                                      ("Transfer completed", "2026-10-31T19:05:00+00:00"),
                                      ("Transfer completed", success)):
                db.execute("""INSERT INTO audit_logs(actor_user_id,action,entity_type,entity_id,details,created_at)
                    VALUES(1,?,'transfer',?,'Historical event',?)""", (action, ids["audit"], timestamp))
            # A matching successful schedule is trustworthy; mismatched amounts are not.
            for name, cents in (("schedule", 10_000), ("mismatch", 99_999)):
                db.execute("UPDATE transfers SET scheduled_for=? WHERE request_id=?", (created, ids[name]))
                db.execute("""INSERT INTO scheduled_transfers(request_id,user_id,sender_account_id,
                    recipient_account_id,amount_cents,scheduled_for,status,created_at,updated_at)
                    VALUES(?,1,1,2,?,?,'Completed',?,?)""", (ids[name], cents, created, created, success))
            balances = [tuple(row) for row in db.execute("SELECT id,balance_cents FROM accounts ORDER BY id")]
            before = {row["request_id"]: dict(row) for row in db.execute("SELECT * FROM transfers")}
        server.init_db()
        with server.connect() as db:
            completed = {row["request_id"]: row["completed_at"] for row in db.execute("SELECT * FROM transfers")}
            self.assertEqual(completed, {ids["audit"]: success, ids["schedule"]: success,
                                        ids["fallback"]: created, ids["mismatch"]: created,
                                        ids["pending"]: None, ids["failed"]: None})
            for row in db.execute("SELECT * FROM transfers"):
                preserved = dict(row)
                preserved.pop("completed_at")
                self.assertEqual(preserved, before[row["request_id"]])
            self.assertEqual([tuple(row) for row in db.execute("SELECT id,balance_cents FROM accounts ORDER BY id")], balances)
            # Later discovery of an audit event must not move an already migrated entry.
            db.execute("""INSERT INTO audit_logs(actor_user_id,action,entity_type,entity_id,details,created_at)
                VALUES(1,'Transfer completed','transfer',?,'Late import',?)""", (ids["fallback"], success))
        server.init_db()
        with server.connect() as db:
            self.assertEqual({row["request_id"]: row["completed_at"] for row in db.execute("SELECT * FROM transfers")}, completed)
            self.assertEqual([tuple(row) for row in db.execute("SELECT id,balance_cents FROM accounts ORDER BY id")], balances)
        self.assertEqual(self.analytics()["month"]["count"], 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
