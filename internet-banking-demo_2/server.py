from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("BANK_DB", ROOT / "banking.sqlite3"))
HOST = os.environ.get("BANK_HOST", "127.0.0.1")
PORT = int(os.environ.get("BANK_PORT", "8000"))
SESSION_SECONDS = 8 * 60 * 60
PBKDF2_ROUNDS = 310_000
CENT = 100
MIN_TRANSFER_CENTS = int(os.environ.get("BANK_MIN_TRANSFER_KZT", "100")) * CENT
MAX_TRANSFER_CENTS = int(os.environ.get("BANK_MAX_TRANSFER_KZT", "1000000")) * CENT
DAILY_LIMIT_CENTS = int(os.environ.get("BANK_DAILY_LIMIT_KZT", "2000000")) * CENT
TRANSFER_DELAY_SECONDS = int(os.environ.get("BANK_PENDING_SECONDS", "3"))
DB_LOCK = threading.Lock()


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def connect():
    db = sqlite3.connect(DB_PATH, timeout=20, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA journal_mode=WAL")
    try:
        yield db
    finally:
        db.close()


def password_hash(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ROUNDS)
    return "pbkdf2_sha256${}${}${}".format(
        PBKDF2_ROUNDS, base64.urlsafe_b64encode(salt).decode(), base64.urlsafe_b64encode(digest).decode()
    )


def verify_password(password: str, stored: str) -> bool:
    try:
        method, rounds, salt_text, digest_text = stored.split("$", 3)
        if method != "pbkdf2_sha256":
            return False
        salt = base64.urlsafe_b64decode(salt_text.encode())
        expected = base64.urlsafe_b64decode(digest_text.encode())
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, int(rounds))
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def table_columns(db: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}


def init_db() -> None:
    with connect() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS customers (
            id INTEGER PRIMARY KEY, name TEXT NOT NULL, account_number TEXT UNIQUE NOT NULL
        );
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            full_name TEXT NOT NULL,
            email TEXT NOT NULL COLLATE NOCASE UNIQUE,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'customer' CHECK(role IN ('customer','admin')),
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS accounts (
            id INTEGER PRIMARY KEY,
            customer_id INTEGER NOT NULL REFERENCES customers(id),
            label TEXT NOT NULL,
            balance_cents INTEGER NOT NULL CHECK(balance_cents >= 0),
            owner_user_id INTEGER REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS transfers (
            request_id TEXT PRIMARY KEY,
            sender_account_id INTEGER NOT NULL REFERENCES accounts(id),
            recipient_account_id INTEGER NOT NULL REFERENCES accounts(id),
            amount_cents INTEGER NOT NULL CHECK(amount_cents > 0),
            status TEXT NOT NULL CHECK(status IN ('Pending','Completed','Failed')),
            created_at TEXT NOT NULL,
            checked_at TEXT NOT NULL,
            failure_reason TEXT,
            description TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS request_ids (request_id TEXT PRIMARY KEY, reserved_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS sessions (
            session_id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            csrf_token TEXT NOT NULL,
            expires_at INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS transfers_sender_created ON transfers(sender_account_id, created_at DESC);
        CREATE INDEX IF NOT EXISTS transfers_recipient_created ON transfers(recipient_account_id, created_at DESC);
        CREATE INDEX IF NOT EXISTS sessions_expiry ON sessions(expires_at);
        """)

        # Upgrade databases created by the earlier demo without discarding account or transfer data.
        account_columns = table_columns(db, "accounts")
        if "owner_user_id" not in account_columns:
            db.execute("ALTER TABLE accounts ADD COLUMN owner_user_id INTEGER REFERENCES users(id)")
        transfer_columns = table_columns(db, "transfers")
        if "description" not in transfer_columns:
            db.execute("ALTER TABLE transfers ADD COLUMN description TEXT NOT NULL DEFAULT ''")

        if db.execute("SELECT COUNT(*) FROM customers").fetchone()[0] == 0:
            db.executemany("INSERT INTO customers(id,name,account_number) VALUES(?,?,?)", [
                (1, "Aruzhan S.", "KZ•• 1042"), (2, "Dias T.", "KZ•• 8821"), (3, "Amina K.", "KZ•• 3307")
            ])
            db.executemany("INSERT INTO accounts(id,customer_id,label,balance_cents) VALUES(?,?,?,?)", [
                (1, 1, "Everyday account", 10_000_000),
                (2, 2, "Everyday account", 2_500_000),
                (3, 3, "Savings account", 7_500_000)
            ])

        # Give the three original sample customers persistent local-demo sign-in credentials.
        demo_users = {
            1: ("Aruzhan S.", "aruzhan@example.test", "DemoPass!23"),
            2: ("Dias T.", "dias@example.test", "DemoPass!23"),
            3: ("Amina K.", "amina@example.test", "DemoPass!23")
        }
        for customer in db.execute("SELECT id,name FROM customers").fetchall():
            cid = customer["id"]
            display, email, demo_password = demo_users.get(
                cid, (customer["name"], f"demo{cid}@example.test", "DemoPass!23")
            )
            if not db.execute("SELECT 1 FROM users WHERE email=? COLLATE NOCASE", (email,)).fetchone():
                db.execute("INSERT INTO users(full_name,email,password_hash,role,created_at) VALUES(?,?,?,?,?)",
                           (display, email, password_hash(demo_password), "customer", now()))
            user_id = db.execute("SELECT id FROM users WHERE email=? COLLATE NOCASE", (email,)).fetchone()[0]
            db.execute("UPDATE accounts SET owner_user_id=? WHERE customer_id=? AND owner_user_id IS NULL", (user_id, cid))

        db.execute("DELETE FROM sessions WHERE expires_at <= ?", (int(datetime.now(timezone.utc).timestamp()),))


def create_session(db: sqlite3.Connection, user_id: int) -> tuple[str, str]:
    sid, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    expires = int(datetime.now(timezone.utc).timestamp()) + SESSION_SECONDS
    db.execute("INSERT INTO sessions(session_id,user_id,csrf_token,expires_at) VALUES(?,?,?,?)", (sid, user_id, csrf, expires))
    return sid, csrf


def schedule_pending() -> None:
    with connect() as db:
        ids = [row[0] for row in db.execute("SELECT request_id FROM transfers WHERE status='Pending'")]
    for request_id in ids:
        schedule_transfer(request_id)


def schedule_transfer(request_id: str) -> None:
    timer = threading.Timer(TRANSFER_DELAY_SECONDS, process_transfer, args=(request_id,))
    timer.daemon = True
    timer.start()


def process_transfer(request_id: str) -> None:
    try:
        with DB_LOCK, connect() as db:
            db.execute("BEGIN IMMEDIATE")
            transfer = db.execute("SELECT * FROM transfers WHERE request_id=?", (request_id,)).fetchone()
            if not transfer or transfer["status"] != "Pending":
                db.rollback()
                return
            sender = db.execute("SELECT balance_cents FROM accounts WHERE id=?", (transfer["sender_account_id"],)).fetchone()
            if not sender or sender["balance_cents"] < transfer["amount_cents"]:
                db.execute("UPDATE transfers SET status='Failed',failure_reason='Insufficient funds',checked_at=? WHERE request_id=?",
                           (now(), request_id))
            else:
                db.execute("UPDATE accounts SET balance_cents=balance_cents-? WHERE id=?",
                           (transfer["amount_cents"], transfer["sender_account_id"]))
                db.execute("UPDATE accounts SET balance_cents=balance_cents+? WHERE id=?",
                           (transfer["amount_cents"], transfer["recipient_account_id"]))
                db.execute("UPDATE transfers SET status='Completed',checked_at=? WHERE request_id=?", (now(), request_id))
            db.commit()
    except sqlite3.Error as exc:
        print(f"Could not process pending transfer {request_id}: {exc}")


def cents_from(value) -> int:
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or amount <= 0 or amount.as_tuple().exponent < -2:
            raise ValueError("Enter a positive amount with up to 2 decimal places")
        cents = int(amount * CENT)
    except (InvalidOperation, TypeError, ValueError) as exc:
        if isinstance(exc, ValueError) and str(exc):
            raise
        raise ValueError("Enter a valid transfer amount") from exc
    return cents


def local_day_bounds_utc() -> tuple[str, str]:
    local = datetime.now(timezone(timedelta(hours=5)))
    start_local = local.replace(hour=0, minute=0, second=0, microsecond=0)
    end_local = start_local + timedelta(days=1)
    return (start_local.astimezone(timezone.utc).isoformat(timespec="seconds"),
            end_local.astimezone(timezone.utc).isoformat(timespec="seconds"))


class Handler(BaseHTTPRequestHandler):
    server_version = "BankingDemo/2.0"

    def log_message(self, fmt: str, *args) -> None:
        print(f"{self.log_date_time_string()} {self.client_address[0]} {fmt % args}")

    def send_json(self, status: int, data: dict | list, extra_headers: list[tuple[str, str]] | None = None) -> None:
        raw = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        for key, value in extra_headers or []:
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(raw)

    def read_json(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length > 16_384:
                raise ValueError("Request is too large")
            payload = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(payload, dict):
                raise ValueError("Invalid request")
            return payload
        except (json.JSONDecodeError, ValueError) as exc:
            raise ValueError("Invalid request data") from exc

    def cookie_session(self):
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
            sid = cookie["bank_session"].value
        except Exception:
            return None
        with connect() as db:
            row = db.execute("""SELECT s.session_id,s.user_id,s.csrf_token,u.full_name,u.email,u.role
                FROM sessions s JOIN users u ON u.id=s.user_id
                WHERE s.session_id=? AND s.expires_at>?""",
                (sid, int(datetime.now(timezone.utc).timestamp()))).fetchone()
            return row

    def require_user(self, csrf: bool = False):
        user = self.cookie_session()
        if not user:
            self.send_json(401, {"error": "Please sign in to continue"})
            return None
        if csrf and not hmac.compare_digest(self.headers.get("X-CSRF-Token", ""), user["csrf_token"]):
            self.send_json(403, {"error": "Security token expired. Refresh the page and try again"})
            return None
        return user

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/":
            body = (ROOT / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "same-origin")
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/api/session":
            user = self.cookie_session()
            if not user:
                self.send_json(200, {"authenticated": False})
            else:
                self.send_json(200, {"authenticated": True, "csrfToken": user["csrf_token"],
                                     "user": {"name": user["full_name"], "email": user["email"]}})
            return
        user = self.require_user()
        if not user:
            return
        if path == "/api/accounts":
            with connect() as db:
                rows = db.execute("""SELECT a.id,a.label,a.balance_cents,c.account_number,u.full_name
                    FROM accounts a JOIN customers c ON c.id=a.customer_id
                    LEFT JOIN users u ON u.id=a.owner_user_id
                    WHERE a.owner_user_id=? ORDER BY a.id""", (user["user_id"],)).fetchall()
                self.send_json(200, [{"id": r["id"], "label": r["label"], "balance": r["balance_cents"] / CENT,
                                     "number": r["account_number"], "name": r["full_name"]} for r in rows])
            return
        if path == "/api/recipients":
            with connect() as db:
                rows = db.execute("""SELECT a.id,c.account_number,u.full_name FROM accounts a
                    JOIN customers c ON c.id=a.customer_id JOIN users u ON u.id=a.owner_user_id
                    WHERE a.owner_user_id<>? ORDER BY u.full_name""",
                                  (user["user_id"],)).fetchall()
                self.send_json(200, [{"id": r["id"], "name": r["full_name"], "number": r["account_number"]} for r in rows])
            return
        if path == "/api/limits":
            start_utc, end_utc = local_day_bounds_utc()
            with connect() as db:
                used = db.execute("""SELECT COALESCE(SUM(amount_cents),0) FROM transfers
                    WHERE sender_account_id IN (SELECT id FROM accounts WHERE owner_user_id=?)
                    AND status IN ('Pending','Completed') AND datetime(created_at)>=datetime(?) AND datetime(created_at)<datetime(?)""",
                                  (user["user_id"], start_utc, end_utc)).fetchone()[0]
            self.send_json(200, {"minimum": MIN_TRANSFER_CENTS / CENT, "maximum": MAX_TRANSFER_CENTS / CENT,
                                 "daily": DAILY_LIMIT_CENTS / CENT, "usedToday": used / CENT,
                                 "remainingToday": max(DAILY_LIMIT_CENTS - used, 0) / CENT})
            return
        if path == "/api/transfers":
            ids = self.user_account_ids(user["user_id"])
            if not ids:
                self.send_json(200, [])
                return
            placeholders = ",".join("?" for _ in ids)
            with connect() as db:
                rows = db.execute(f"""SELECT t.*,su.full_name sender_name,ru.full_name recipient_name,
                    sc.account_number sender_number,rc.account_number recipient_number
                    FROM transfers t JOIN accounts sa ON sa.id=t.sender_account_id
                    JOIN accounts ra ON ra.id=t.recipient_account_id
                    JOIN customers sc ON sc.id=sa.customer_id JOIN customers rc ON rc.id=ra.customer_id
                    LEFT JOIN users su ON su.id=sa.owner_user_id LEFT JOIN users ru ON ru.id=ra.owner_user_id
                    WHERE t.sender_account_id IN ({placeholders}) OR t.recipient_account_id IN ({placeholders})
                    ORDER BY t.created_at DESC LIMIT 50""", (*ids, *ids)).fetchall()
                self.send_json(200, [self.transfer_payload(r, ids) for r in rows])
            return
        match = re.fullmatch(r"/api/transfers/([^/]+)", path)
        if match:
            ids = self.user_account_ids(user["user_id"])
            with connect() as db:
                row = self.transfer_row(db, match.group(1))
                if not row or (row["sender_account_id"] not in ids and row["recipient_account_id"] not in ids):
                    self.send_json(404, {"error": "Transfer not found"})
                    return
                checked = now()
                db.execute("UPDATE transfers SET checked_at=? WHERE request_id=?", (checked, row["request_id"]))
                row = self.transfer_row(db, row["request_id"])
                self.send_json(200, self.transfer_payload(row, ids))
            return
        self.send_json(404, {"error": "Not found"})

    @staticmethod
    def user_account_ids(user_id: int) -> list[int]:
        with connect() as db:
            return [r[0] for r in db.execute("SELECT id FROM accounts WHERE owner_user_id=?", (user_id,))]

    @staticmethod
    def transfer_row(db: sqlite3.Connection, request_id: str):
        return db.execute("""SELECT t.*,su.full_name sender_name,ru.full_name recipient_name,
            sc.account_number sender_number,rc.account_number recipient_number
            FROM transfers t JOIN accounts sa ON sa.id=t.sender_account_id
            JOIN accounts ra ON ra.id=t.recipient_account_id
            JOIN customers sc ON sc.id=sa.customer_id JOIN customers rc ON rc.id=ra.customer_id
            LEFT JOIN users su ON su.id=sa.owner_user_id LEFT JOIN users ru ON ru.id=ra.owner_user_id
            WHERE t.request_id=?""", (request_id,)).fetchone()

    @staticmethod
    def transfer_payload(row: sqlite3.Row, account_ids: list[int]) -> dict:
        outgoing = row["sender_account_id"] in account_ids
        return {"requestId": row["request_id"], "fromAccountId": row["sender_account_id"],
                "toAccountId": row["recipient_account_id"], "from": row["sender_name"],
                "to": row["recipient_name"], "counterparty": row["recipient_name"] if outgoing else row["sender_name"],
                "recipientNumber": row["recipient_number"], "amount": row["amount_cents"] / CENT,
                "direction": "outgoing" if outgoing else "incoming", "status": row["status"],
                "description": row["description"], "createdAt": row["created_at"],
                "checkedAt": row["checked_at"], "failureReason": row["failure_reason"]}

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path in ("/api/register", "/api/login"):
            self.auth_post(path)
            return
        if path == "/api/logout":
            user = self.require_user(csrf=True)
            if not user:
                return
            with connect() as db:
                db.execute("DELETE FROM sessions WHERE session_id=?", (user["session_id"],))
            self.send_json(200, {"ok": True}, [("Set-Cookie", "bank_session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0")])
            return
        user = self.require_user(csrf=True)
        if not user:
            return
        if path == "/api/next-id":
            self.send_json(200, {"requestId": "TR-" + secrets.token_hex(5).upper()})
            return
        if path != "/api/transfers":
            self.send_json(404, {"error": "Not found"})
            return
        try:
            payload = self.read_json()
            request_id = str(payload.get("requestId", "")).strip()
            if not re.fullmatch(r"TR-[A-F0-9]{32}", request_id):
                raise ValueError("Transfer reference is invalid. Please start a new transfer")
            sender_id = int(payload.get("fromAccountId", 0))
            recipient_id = int(payload.get("toAccountId", 0))
            cents = cents_from(payload.get("amount", 0))
            description = str(payload.get("description", "")).strip()
            if len(description) > 200:
                raise ValueError("Description must be 200 characters or fewer")
        except (ValueError, TypeError) as exc:
            self.send_json(400, {"error": str(exc) or "Invalid transfer"})
            return

        with DB_LOCK, connect() as db:
            db.execute("BEGIN IMMEDIATE")
            sender = db.execute("SELECT * FROM accounts WHERE id=? AND owner_user_id=?", (sender_id, user["user_id"])).fetchone()
            recipient = db.execute("SELECT * FROM accounts WHERE id=?", (recipient_id,)).fetchone()
            if not sender or not recipient or sender_id == recipient_id:
                db.rollback()
                self.send_json(400, {"error": "Choose one of your accounts and a different recipient"})
                return

            existing = db.execute("SELECT * FROM transfers WHERE request_id=?", (request_id,)).fetchone()
            if existing:
                matches = (existing["sender_account_id"] == sender_id and existing["recipient_account_id"] == recipient_id
                           and existing["amount_cents"] == cents and existing["description"] == description)
                if not matches:
                    db.rollback()
                    self.send_json(409, {"error": "This reference is already attached to different transfer details"})
                    return
                row = self.transfer_row(db, request_id)
                db.commit()
                self.send_json(200, self.transfer_payload(row, [sender_id]))
                return

            if cents < MIN_TRANSFER_CENTS:
                db.rollback()
                self.send_json(400, {"error": f"Minimum transfer is {MIN_TRANSFER_CENTS / CENT:,.2f} KZT"})
                return
            if cents > MAX_TRANSFER_CENTS:
                db.rollback()
                self.send_json(400, {"error": f"Maximum per transfer is {MAX_TRANSFER_CENTS / CENT:,.2f} KZT"})
                return

            start_utc, end_utc = local_day_bounds_utc()
            total = db.execute("""SELECT COALESCE(SUM(amount_cents),0) FROM transfers
                WHERE sender_account_id IN (SELECT id FROM accounts WHERE owner_user_id=?)
                AND status IN ('Pending','Completed') AND datetime(created_at)>=datetime(?) AND datetime(created_at)<datetime(?)""",
                               (user["user_id"], start_utc, end_utc)).fetchone()[0]
            if total + cents > DAILY_LIMIT_CENTS:
                remaining = max(DAILY_LIMIT_CENTS - total, 0) / CENT
                db.rollback()
                self.send_json(400, {"error": f"Daily transfer limit is {DAILY_LIMIT_CENTS / CENT:,.2f} KZT. Remaining today: {remaining:,.2f} KZT"})
                return

            created = now()
            db.execute("""INSERT INTO transfers(request_id,sender_account_id,recipient_account_id,amount_cents,status,
                created_at,checked_at,failure_reason,description) VALUES(?,?,?,?,?,?,?,?,?)""",
                       (request_id, sender_id, recipient_id, cents, "Pending", created, created, None, description))
            row = self.transfer_row(db, request_id)
            db.commit()
        schedule_transfer(request_id)
        self.send_json(202, self.transfer_payload(row, [sender_id]))

    def auth_post(self, path: str) -> None:
        try:
            payload = self.read_json()
            email = str(payload.get("email", "")).strip().lower()
            password = str(payload.get("password", ""))
            if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email) or len(email) > 254:
                raise ValueError("Enter a valid email address")
            if path == "/api/register":
                name = str(payload.get("name", "")).strip()
                confirmation = str(payload.get("passwordConfirmation", ""))
                if len(name) < 2 or len(name) > 80:
                    raise ValueError("Name must be between 2 and 80 characters")
                if len(password) < 12 or len(password) > 128:
                    raise ValueError("Password must be 12–128 characters")
                if password != confirmation:
                    raise ValueError("Passwords do not match")
            elif not password or len(password) > 128:
                raise ValueError("Enter your password")
        except (ValueError, TypeError) as exc:
            self.send_json(400, {"error": str(exc) or "Invalid details"})
            return

        with connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if path == "/api/register":
                if db.execute("SELECT 1 FROM users WHERE email=? COLLATE NOCASE", (email,)).fetchone():
                    db.rollback()
                    self.send_json(409, {"error": "An account with this email already exists"})
                    return
                cursor = db.execute("INSERT INTO users(full_name,email,password_hash,role,created_at) VALUES(?,?,?,?,?)",
                                    (name, email, password_hash(password), "customer", now()))
                user_id = cursor.lastrowid
                cust = db.execute("INSERT INTO customers(name,account_number) VALUES(?,?)",
                                  (name, "KZ•• " + secrets.token_hex(4).upper())).lastrowid
                db.execute("INSERT INTO accounts(customer_id,label,balance_cents,owner_user_id) VALUES(?,?,?,?)",
                           (cust, "Everyday account", 0, user_id))
            else:
                found = db.execute("SELECT * FROM users WHERE email=? COLLATE NOCASE", (email,)).fetchone()
                if not found or not verify_password(password, found["password_hash"]):
                    db.rollback()
                    self.send_json(401, {"error": "Email or password is incorrect"})
                    return
                user_id = found["id"]
            db.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
            sid, csrf = create_session(db, user_id)
            user = db.execute("SELECT full_name,email FROM users WHERE id=?", (user_id,)).fetchone()
            db.commit()
        cookie = f"bank_session={sid}; HttpOnly; SameSite=Strict; Path=/; Max-Age={SESSION_SECONDS}"
        self.send_json(200, {"authenticated": True, "csrfToken": csrf,
                             "user": {"name": user["full_name"], "email": user["email"]}}, [("Set-Cookie", cookie)])


if __name__ == "__main__":
    if MIN_TRANSFER_CENTS <= 0 or MAX_TRANSFER_CENTS < MIN_TRANSFER_CENTS or DAILY_LIMIT_CENTS < MAX_TRANSFER_CENTS or TRANSFER_DELAY_SECONDS < 1:
        raise SystemExit("Transfer limit settings are invalid; require 0 < minimum <= maximum <= daily limit")
    init_db()
    schedule_pending()
    print(f"Internet Banking Demo running at http://{HOST}:{PORT}")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
