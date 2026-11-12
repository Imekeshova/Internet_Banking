"""
Internet Banking Demo - 20 automated system-check tests .

Run from the project folder:   python -m unittest tests.test_bank -v
(or)                           python tests/test_bank.py

Every test uses a fresh temporary SQLite DB, so banking.sqlite3 is never touched.
Handler is called in-memory (no network port needed).
"""
import io
import json
import re
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

import server  # noqa: E402

ARUZHAN = "aruzhan@example.test"   # user 1, account 1, 100,000 KZT
DIAS = "dias@example.test"         # user 2, account 2,  25,000 KZT
AMINA = "amina@example.test"       # user 3, account 3,  75,000 KZT
KZT = 100                          # cents per tenge


def setUpModule():
    # Fewer PBKDF2 rounds ONLY in tests: suite runs in ~1-2 s instead of ~10 s.
    server.PBKDF2_ROUNDS = 1_000


def invoke(method, path, body=None, cookie="", csrf=""):
    """Call Handler in memory. Returns (status, response_headers, json_body)."""
    raw = json.dumps(body).encode() if body is not None else b""
    headers = {"Content-Length": str(len(raw))}
    if cookie:
        headers["Cookie"] = cookie
    if csrf:
        headers["X-CSRF-Token"] = csrf
    h = server.Handler.__new__(server.Handler)
    h.requestline, h.request_version, h.command, h.path = f"{method} {path} HTTP/1.1", "HTTP/1.1", method, path
    h.headers, h.rfile, h.wfile = headers, io.BytesIO(raw), io.BytesIO()
    h.client_address, h.close_connection = ("127.0.0.1", 50000), True
    state, out_headers = {}, []
    h.send_response = lambda code, message=None: state.__setitem__("code", code)
    h.send_header = lambda k, v: out_headers.append((k, v))
    h.end_headers = lambda: None
    (h.do_GET if method == "GET" else h.do_POST)()
    text = h.wfile.getvalue().decode()
    return state.get("code", 0), dict(out_headers), (json.loads(text) if text else None)


class BankSystemChecks(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        server.DB_PATH = Path(self._tmp.name) / "test.sqlite3"
        server.init_db()

    def tearDown(self):
        self._tmp.cleanup()

    # ---------- helpers ----------
    def login(self, email, password="DemoPass!23"):
        status, headers, data = invoke("POST", "/api/login", {"email": email, "password": password})
        self.assertEqual(status, 200)
        return headers["Set-Cookie"].split(";", 1)[0], data["csrfToken"]

    def send(self, session, rid, src, dst, amount, **extra):
        """POST /api/transfers without starting the 3-second background timer."""
        cookie, csrf = session
        body = {"requestId": rid, "fromAccountId": src, "toAccountId": dst, "amount": amount, **extra}
        with patch.object(server, "schedule_transfer"):
            return invoke("POST", "/api/transfers", body, cookie=cookie, csrf=csrf)

    def balance(self, account_id):
        with server.connect() as db:
            return db.execute("SELECT balance_cents FROM accounts WHERE id=?", (account_id,)).fetchone()[0]

    def transfer_row(self, rid):
        with server.connect() as db:
            return db.execute("SELECT * FROM transfers WHERE request_id=?", (rid,)).fetchone()

    def seed_transfer(self, rid, src, dst, cents, status, created, checked=None, reason=None):
        with server.connect() as db:
            db.execute("""INSERT INTO transfers(request_id,sender_account_id,recipient_account_id,amount_cents,
                          status,created_at,checked_at,failure_reason) VALUES(?,?,?,?,?,?,?,?)""",
                       (rid, src, dst, cents, status, created, checked or created, reason))

    # =====================================================================
    # BLOCK 1 - Transfer lifecycle and atomicity
    # =====================================================================
    def test_01_transfer_starts_pending_and_moves_no_money(self):
        """New transfer: HTTP 202, status Pending, balances untouched until the worker runs."""
        s = self.login(ARUZHAN)
        rid = "TR-" + "A" * 32
        status, _, data = self.send(s, rid, 1, 2, 5000, description="Rent")
        self.assertEqual(status, 202)
        self.assertEqual(data["status"], "Pending")
        self.assertEqual(self.balance(1), 100_000 * KZT)
        self.assertEqual(self.balance(2), 25_000 * KZT)

    def test_02_worker_completes_transfer_atomically(self):
        """Worker: debit sender + credit recipient + status Completed in one step."""
        s = self.login(ARUZHAN)
        rid = "TR-" + "B" * 32
        self.send(s, rid, 1, 2, 10_000)
        server.process_transfer(rid)
        self.assertEqual(self.balance(1), 90_000 * KZT)
        self.assertEqual(self.balance(2), 35_000 * KZT)
        self.assertEqual(self.transfer_row(rid)["status"], "Completed")

    def test_03_insufficient_funds_fails_without_moving_money(self):
        """Dias (25,000) sends 900,000: Failed + 'Insufficient funds', both balances unchanged."""
        s = self.login(DIAS)
        rid = "TR-" + "C" * 32
        self.send(s, rid, 2, 1, 900_000)
        server.process_transfer(rid)
        row = self.transfer_row(rid)
        self.assertEqual((row["status"], row["failure_reason"]), ("Failed", "Insufficient funds"))
        self.assertEqual(self.balance(2), 25_000 * KZT)
        self.assertEqual(self.balance(1), 100_000 * KZT)

    def test_04_self_transfer_rejected(self):
        """Same account as sender and recipient is rejected and nothing is stored."""
        s = self.login(ARUZHAN)
        rid = "TR-" + "D" * 32
        status, _, data = self.send(s, rid, 1, 1, 1000)
        self.assertEqual(status, 400)
        self.assertIn("different recipient", data["error"])
        self.assertIsNone(self.transfer_row(rid))

    # =====================================================================
    # BLOCK 2 - Idempotency and request_id control
    # =====================================================================
    def test_05_invalid_request_id_format_rejected(self):
        """Reference must be TR- + 32 hex chars (upper-case); anything else -> 400."""
        s = self.login(ARUZHAN)
        for bad in ("INVALID-123", "TR-ABC", "tr-" + "a" * 32, "TR-" + "G" * 32, ""):
            with self.subTest(request_id=bad):
                status, _, data = self.send(s, bad, 1, 2, 1000)
                self.assertEqual(status, 400)
                self.assertIn("reference is invalid", data["error"])

    def test_06_identical_retry_is_idempotent(self):
        """Same reference + same details -> HTTP 200, existing transfer, only ONE debit."""
        s = self.login(ARUZHAN)
        rid = "TR-" + "E" * 32
        first, _, _ = self.send(s, rid, 1, 2, 5000, description="Books")
        server.process_transfer(rid)
        second, _, data = self.send(s, rid, 1, 2, 5000, description="Books")
        self.assertEqual((first, second, data["status"]), (202, 200, "Completed"))
        self.assertEqual(self.balance(1), 95_000 * KZT)
        with server.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM transfers").fetchone()[0], 1)

    def test_07_reusing_reference_with_other_details_returns_409(self):
        """Changed amount, recipient or description under an old reference -> 409."""
        s = self.login(ARUZHAN)
        rid = "TR-" + "F" * 32
        self.send(s, rid, 1, 2, 2000, description="a")
        for kwargs in ({"amount": 9000}, {"dst": 3}, {"description": "b"}):
            with self.subTest(change=kwargs):
                args = {"amount": 2000, "dst": 2, "description": "a", **kwargs}
                status, _, data = self.send(s, rid, 1, args["dst"], args["amount"], description=args["description"])
                self.assertEqual(status, 409)
                self.assertIn("different transfer details", data["error"])

    def test_08_worker_ignores_already_processed_transfer(self):
        """Running the worker twice (or on a Failed record) never double-moves money."""
        s = self.login(ARUZHAN)
        rid = "TR-" + "1" * 32
        self.send(s, rid, 1, 2, 3000)
        server.process_transfer(rid)
        server.process_transfer(rid)
        server.process_transfer("TR-" + "0" * 32)  # unknown id: must not raise
        self.assertEqual(self.balance(1), 97_000 * KZT)
        self.assertEqual(self.balance(2), 28_000 * KZT)

    # =====================================================================
    # BLOCK 3 - Security, sessions, access control
    # =====================================================================
    def test_09_passwords_are_salted_pbkdf2_and_never_plaintext(self):
        """PBKDF2-SHA256 verifies right/wrong password, salts differ, DB holds no plaintext."""
        h1, h2 = server.password_hash("StrongPass!2026"), server.password_hash("StrongPass!2026")
        self.assertTrue(h1.startswith("pbkdf2_sha256$"))
        self.assertNotEqual(h1, h2)  # random per-password salt
        self.assertTrue(server.verify_password("StrongPass!2026", h1))
        self.assertFalse(server.verify_password("WrongPassword!", h1))
        self.assertFalse(server.verify_password("x", "garbage-hash"))
        with server.connect() as db:
            stored = [r[0] for r in db.execute("SELECT password_hash FROM users")]
        self.assertTrue(all("DemoPass!23" not in v and v.startswith("pbkdf2_sha256$") for v in stored))

    def test_10_authentication_required_and_bad_login_rejected(self):
        """No cookie -> 401 on every protected GET; wrong password / unknown email -> 401."""
        for path in ("/api/accounts", "/api/transfers", "/api/limits", "/api/recipients"):
            with self.subTest(path=path):
                self.assertEqual(invoke("GET", path)[0], 401)
        self.assertEqual(invoke("POST", "/api/login", {"email": ARUZHAN, "password": "nope"})[0], 401)
        self.assertEqual(invoke("POST", "/api/login", {"email": "ghost@example.test", "password": "DemoPass!23"})[0], 401)

    def test_11_csrf_token_required_for_mutations(self):
        """Missing or wrong X-CSRF-Token -> 403 and no transfer is created."""
        cookie, csrf = self.login(ARUZHAN)
        body = {"requestId": "TR-" + "2" * 32, "fromAccountId": 1, "toAccountId": 2, "amount": 1000}
        for token in ("", "invalid-token"):
            with self.subTest(token=token):
                status, _, data = invoke("POST", "/api/transfers", body, cookie=cookie, csrf=token)
                self.assertEqual(status, 403)
                self.assertIn("Security token", data["error"])
        self.assertIsNone(self.transfer_row(body["requestId"]))

    def test_12_account_ownership_and_data_isolation(self):
        """Dias cannot debit Aruzhan's account, list it, or read her transfer (IDOR checks)."""
        aru, dias = self.login(ARUZHAN), self.login(DIAS)
        # debit a foreign account
        status, _, data = self.send(dias, "TR-" + "3" * 32, 1, 3, 1000)
        self.assertEqual(status, 400)
        self.assertIn("Choose one of your accounts", data["error"])
        # account list shows only own accounts
        _, _, accounts = invoke("GET", "/api/accounts", cookie=dias[0])
        self.assertEqual([a["id"] for a in accounts], [2])
        # recipients never include own accounts
        _, _, recips = invoke("GET", "/api/recipients", cookie=dias[0])
        self.assertNotIn(2, [r["id"] for r in recips])
        # a third party cannot read someone else's transfer
        rid = "TR-" + "4" * 32
        self.send(aru, rid, 1, 3, 1000)  # Aruzhan -> Amina
        self.assertEqual(invoke("GET", f"/api/transfers/{rid}", cookie=dias[0])[0], 404)
        self.assertEqual(invoke("GET", f"/api/transfers/{rid}", cookie=aru[0])[0], 200)

    # =====================================================================
    # BLOCK 4 - Limits and data-quality audit (E3 Validity, E5 Timeliness)
    # =====================================================================
    def test_13_min_max_limits_with_boundaries(self):
        """99.99 and 1,000,000.01 rejected; exactly 100.00 and 1,000,000.00 accepted."""
        s = self.login(ARUZHAN)
        cases = [("5", "99.99", 400), ("6", "100.00", 202), ("7", "1000000.00", 202), ("8", "1000000.01", 400)]
        for ch, amount, expected in cases:
            with self.subTest(amount=amount):
                self.assertEqual(self.send(s, "TR-" + ch * 32, 1, 2, amount)[0], expected)

    def test_14_daily_limit_counts_pending_completed_but_not_failed(self):
        """2,000,000 KZT/day (UTC+5): Failed ignored; Completed counts; over-limit rejected; exact limit allowed."""
        dias = self.login(DIAS)
        now = server.now()
        self.seed_transfer("TR-" + "9" * 32, 2, 1, 1_500_000 * KZT, "Failed", now, reason="Insufficient funds")
        _, _, limits = invoke("GET", "/api/limits", cookie=dias[0])
        self.assertEqual((limits["usedToday"], limits["remainingToday"]), (0.0, 2_000_000.0))

        aru = self.login(ARUZHAN)
        self.seed_transfer("TR-" + "a".upper() * 32, 1, 2, 1_950_000 * KZT, "Completed", now)
        _, _, limits = invoke("GET", "/api/limits", cookie=aru[0])
        self.assertEqual((limits["usedToday"], limits["remainingToday"]), (1_950_000.0, 50_000.0))
        status, _, data = self.send(aru, "TR-" + "B" * 32, 1, 2, 100_000)
        self.assertEqual(status, 400)
        self.assertIn("Daily transfer limit", data["error"])
        self.assertEqual(self.send(aru, "TR-" + "C" * 32, 1, 2, 50_000)[0], 202)  # exactly the limit

    def test_15_invalid_amounts_rejected_e3_validity(self):
        """E3: negative, zero, >2 decimals, NaN, text -> HTTP 400 (API) / ValueError (unit)."""
        s = self.login(ARUZHAN)
        for i, bad in enumerate((-500, 0, "10.123", "NaN", "abc", "", None)):
            with self.subTest(amount=bad):
                rid = "TR-" + f"{i:X}" * 32
                self.assertEqual(self.send(s, rid, 1, 2, bad)[0], 400)
                self.assertIsNone(self.transfer_row(rid))
        for bad in (-500, 0, "10.123"):
            with self.assertRaises(ValueError):
                server.cents_from(bad)
        self.assertEqual(server.cents_from("100.50"), 10_050)

    def test_16_no_stale_pending_transfers_e5_timeliness(self):
        """E5: audit query finds a Pending older than 5 min; the worker resolves it -> audit is clean."""
        stale = "TR-" + "7" * 32
        old = "2026-01-01T00:00:00+00:00"
        self.seed_transfer(stale, 1, 2, 10_000, "Pending", old)
        audit = ("SELECT request_id FROM transfers WHERE status='Pending' "
                 "AND datetime(checked_at) < datetime('now','-5 minutes')")
        with server.connect() as db:
            self.assertEqual([r[0] for r in db.execute(audit)], [stale])
        server.process_transfer(stale)
        with server.connect() as db:
            self.assertEqual(db.execute(audit).fetchall(), [])
        row = self.transfer_row(stale)
        self.assertEqual(row["status"], "Completed")
        self.assertGreater(row["checked_at"], old)  # checked_at moved forward when status was decided

    # =====================================================================
    # BLOCK 5 - DB integrity, recovery, reconciliation
    # =====================================================================
    def test_17_database_constraints_enforced(self):
        """FK on, CHECK(balance>=0), CHECK(amount>0), CHECK(status IN ...) all fire."""
        ts = "2026-11-12T00:00:00+00:00"
        ins = ("INSERT INTO transfers(request_id,sender_account_id,recipient_account_id,amount_cents,status,"
               "created_at,checked_at) VALUES(?,?,?,?,?,?,?)")
        with server.connect() as db:
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("UPDATE accounts SET balance_cents=-1 WHERE id=1")
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute(ins, ("TR-BAD-FK", 1, 9999, 10_000, "Pending", ts, ts))
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute(ins, ("TR-BAD-AMT", 1, 2, 0, "Pending", ts, ts))
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute(ins, ("TR-BAD-STATUS", 1, 2, 100, "Done", ts, ts))

    def test_18_pending_transfers_resume_after_restart(self):
        """schedule_pending() re-schedules every Pending transfer and skips finished ones."""
        pending = "TR-" + "7" * 32
        now = server.now()
        self.seed_transfer(pending, 1, 2, 25_000, "Pending", now)
        self.seed_transfer("TR-" + "8" * 32, 1, 2, 25_000, "Completed", now)
        with patch.object(server, "schedule_transfer") as sched:
            server.schedule_pending()
        sched.assert_called_once_with(pending)

    def test_19_status_lookup_on_pending_refreshes_checked_at(self):
        """GET /api/transfers/<id> on a Pending transfer updates checked_at; status stays Pending."""
        s = self.login(ARUZHAN)
        rid = "TR-" + "5" * 32
        old = "2026-11-05T09:30:00+00:00"
        self.seed_transfer(rid, 1, 2, 50_000, "Pending", old)
        status, _, data = invoke("GET", f"/api/transfers/{rid}", cookie=s[0])
        self.assertEqual(status, 200)
        self.assertEqual(data["status"], "Pending")
        self.assertGreater(data["checkedAt"], old)
        self.assertEqual(self.transfer_row(rid)["checked_at"], data["checkedAt"])

    def test_20_ledger_reconciliation_per_account_and_total(self):
        """balance == opening + sum(Completed in) - sum(Completed out) for every account; total conserved."""
        opening = {1: 100_000 * KZT, 2: 25_000 * KZT, 3: 75_000 * KZT}
        aru, dias = self.login(ARUZHAN), self.login(DIAS)
        plan = [(aru, "A1", 1, 3, 15_000), (aru, "A2", 1, 2, 4_000), (dias, "A3", 2, 1, 900_000),  # last one fails
                (dias, "A4", 2, 3, 1_000)]
        for session, tag, src, dst, amount in plan:
            rid = "TR-" + tag.ljust(32, "0")
            self.send(session, rid, src, dst, amount)
            server.process_transfer(rid)
        with server.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM transfers WHERE status='Failed'").fetchone()[0], 1)
            for acc, start in opening.items():
                inc = db.execute("SELECT COALESCE(SUM(amount_cents),0) FROM transfers WHERE status='Completed' "
                                 "AND recipient_account_id=?", (acc,)).fetchone()[0]
                out = db.execute("SELECT COALESCE(SUM(amount_cents),0) FROM transfers WHERE status='Completed' "
                                 "AND sender_account_id=?", (acc,)).fetchone()[0]
                self.assertEqual(self.balance(acc), start + inc - out, f"account {acc} does not reconcile")
            self.assertEqual(db.execute("SELECT SUM(balance_cents) FROM accounts").fetchone()[0], sum(opening.values()))

    # =====================================================================
    # BONUS - known bugs found in code review (expected to FAIL today).
    # After you fix server.py these will report "unexpected success":
    # then just delete the @unittest.expectedFailure line above the test.
    # =====================================================================
    @unittest.expectedFailure
    def test_21_BUG_next_id_matches_accepted_reference_format(self):
        """/api/next-id returns TR-<10 hex> but POST /api/transfers requires TR-<32 hex>."""
        cookie, csrf = self.login(ARUZHAN)
        _, _, data = invoke("POST", "/api/next-id", {}, cookie=cookie, csrf=csrf)
        self.assertRegex(data["requestId"], r"^TR-[A-F0-9]{32}$")

    @unittest.expectedFailure
    def test_22_BUG_lookup_must_not_overwrite_checked_at_of_finished_transfer(self):
        """GET on a Completed/Failed transfer rewrites checked_at (time of the decision is lost)."""
        s = self.login(ARUZHAN)
        rid = "TR-" + "6" * 32
        old = "2026-11-05T12:05:37+00:00"
        self.seed_transfer(rid, 1, 2, 10_000, "Completed", old)
        _, _, data = invoke("GET", f"/api/transfers/{rid}", cookie=s[0])
        self.assertEqual(data["checkedAt"], old)


if __name__ == "__main__":
    unittest.main(verbosity=2)