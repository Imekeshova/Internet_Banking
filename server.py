from __future__ import annotations

import base64
import html
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import smtplib
import ssl
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from email.message import EmailMessage
from email.utils import formataddr
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

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
SCHEDULE_MIN_SECONDS = 30
SCHEDULE_MAX_DAYS = 365
KZ_TIMEZONE = timezone(timedelta(hours=5))
DB_LOCK = threading.Lock()
PASSWORD_RESET_TTL_SECONDS = 30 * 60
PASSWORD_RESET_RATE_LIMIT = 5
PASSWORD_RESET_RATE_KEY = os.environ.get("BANK_RESET_RATE_KEY", "").encode() or secrets.token_bytes(32)


def password_reset_mail_config() -> dict | None:
    host = os.environ.get("BANK_SMTP_HOST", "").strip()
    sender = os.environ.get("BANK_SMTP_FROM", "").strip()
    public_url = os.environ.get("BANK_PUBLIC_URL", "").strip().rstrip("/")
    username = os.environ.get("BANK_SMTP_USERNAME", "").strip()
    password = os.environ.get("BANK_SMTP_PASSWORD", "")
    if not host or not sender or not public_url or bool(username) != bool(password):
        return None
    try:
        port = int(os.environ.get("BANK_SMTP_PORT", "587"))
    except ValueError:
        return None
    parsed = urlparse(public_url)
    if port < 1 or port > 65535 or parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None
    return {"host": host, "port": port, "sender": sender, "publicUrl": public_url,
            "username": username, "password": password, "tls": "ssl" if port == 465 else "starttls"}


def send_password_reset_email(config: dict, recipient: str, display_name: str, link: str, language: str) -> None:
    messages = {
        "en": ("Reset your Northstar Bank demo password", "Hello {name},", "Use the link below to choose a new password:",
               "This link expires in 30 minutes and can only be used once.", "If you did not request this, you can ignore this email."),
        "ru": ("Сброс пароля демоверсии Northstar Bank", "Здравствуйте, {name}!", "Перейдите по ссылке ниже, чтобы задать новый пароль:",
               "Ссылка действует 30 минут и может быть использована только один раз.", "Если вы не запрашивали сброс пароля, просто проигнорируйте это письмо."),
        "kk": ("Northstar Bank демо нұсқасының құпиясөзін қалпына келтіру", "Сәлеметсіз бе, {name}!", "Жаңа құпиясөз орнату үшін төмендегі сілтемені ашыңыз:",
               "Сілтеме 30 минут жарамды және оны бір рет қана пайдалануға болады.", "Егер сұрауды сіз жібермесеңіз, бұл хатты елемеуге болады."),
    }
    subject, greeting, instruction, expiry, ignore = messages.get(language, messages["en"])
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = formataddr(("Northstar Bank", config["sender"]))
    message["To"] = recipient
    safe_greeting = greeting.format(name=display_name or "")
    message.set_content(f"{safe_greeting}\n\n{instruction}\n{link}\n\n{expiry}\n\n{ignore}\n")
    message.add_alternative(
        "<!doctype html><html><body style='font:16px/1.5 system-ui,sans-serif;color:#152923'>"
        f"<p>{html.escape(safe_greeting)}</p><p>{html.escape(instruction)}</p>"
        f"<p><a href='{html.escape(link, quote=True)}'>{html.escape(link)}</a></p>"
        f"<p>{html.escape(expiry)}</p><p>{html.escape(ignore)}</p></body></html>", subtype="html")
    context = ssl.create_default_context()
    if config["tls"] == "ssl":
        with smtplib.SMTP_SSL(config["host"], config["port"], timeout=15, context=context) as server:
            if config["username"]:
                server.login(config["username"], config["password"])
            server.send_message(message)
    else:
        with smtplib.SMTP(config["host"], config["port"], timeout=15) as server:
            server.ehlo()
            server.starttls(context=context)
            server.ehlo()
            if config["username"]:
                server.login(config["username"], config["password"])
            server.send_message(message)


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


def normalize_phone(value) -> str | None:
    """Return a canonical Kazakhstan phone (+7 followed by ten digits)."""
    if value is None or value == "":
        return None
    if not isinstance(value, str) or len(value) > 32 or not re.fullmatch(r"\+?[0-9()\s.-]+", value):
        raise ValueError("Enter a valid Kazakhstan phone number")
    digits = re.sub(r"[^0-9]", "", value)
    if len(digits) == 10:
        digits = "7" + digits
    elif len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    if len(digits) != 11 or not digits.startswith("7"):
        raise ValueError("Enter a valid Kazakhstan phone number")
    return "+" + digits


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
            phone_number TEXT,
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
            completed_at TEXT,
            failure_reason TEXT,
            description TEXT NOT NULL DEFAULT '',
            scheduled_for TEXT
        );
        CREATE TABLE IF NOT EXISTS request_ids (request_id TEXT PRIMARY KEY, reserved_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS scheduled_transfers (
            request_id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id),
            sender_account_id INTEGER NOT NULL REFERENCES accounts(id),
            recipient_account_id INTEGER NOT NULL REFERENCES accounts(id),
            amount_cents INTEGER NOT NULL CHECK(amount_cents > 0),
            scheduled_for TEXT NOT NULL,
            reminder_note TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL CHECK(status IN ('Scheduled','Cancelled','Completed','Failed')),
            failure_reason TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS audit_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            actor_user_id INTEGER REFERENCES users(id),
            action TEXT NOT NULL,
            entity_type TEXT NOT NULL,
            entity_id TEXT NOT NULL,
            details TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS app_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS sessions (
            session_id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            csrf_token TEXT NOT NULL,
            expires_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS password_reset_tokens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            token_hash TEXT NOT NULL UNIQUE,
            created_at INTEGER NOT NULL,
            expires_at INTEGER NOT NULL,
            used_at INTEGER
        );
        CREATE TABLE IF NOT EXISTS password_reset_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email_key TEXT NOT NULL,
            ip_key TEXT NOT NULL,
            requested_at INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS transfers_sender_created ON transfers(sender_account_id, created_at DESC);
        CREATE INDEX IF NOT EXISTS transfers_recipient_created ON transfers(recipient_account_id, created_at DESC);
        CREATE INDEX IF NOT EXISTS sessions_expiry ON sessions(expires_at);
        CREATE INDEX IF NOT EXISTS scheduled_due ON scheduled_transfers(status,scheduled_for);
        CREATE INDEX IF NOT EXISTS audit_by_actor ON audit_logs(actor_user_id,created_at DESC);
        CREATE INDEX IF NOT EXISTS password_reset_by_user ON password_reset_tokens(user_id,expires_at);
        CREATE INDEX IF NOT EXISTS password_reset_attempt_email ON password_reset_attempts(email_key,requested_at);
        CREATE INDEX IF NOT EXISTS password_reset_attempt_ip ON password_reset_attempts(ip_key,requested_at);
        """)

        # Upgrade databases created by the earlier demo without discarding account or transfer data.
        account_columns = table_columns(db, "accounts")
        if "owner_user_id" not in account_columns:
            db.execute("ALTER TABLE accounts ADD COLUMN owner_user_id INTEGER REFERENCES users(id)")
        transfer_columns = table_columns(db, "transfers")
        if "description" not in transfer_columns:
            db.execute("ALTER TABLE transfers ADD COLUMN description TEXT NOT NULL DEFAULT ''")
        if "scheduled_for" not in transfer_columns:
            db.execute("ALTER TABLE transfers ADD COLUMN scheduled_for TEXT")
        if "completed_at" not in transfer_columns:
            db.execute("ALTER TABLE transfers ADD COLUMN completed_at TEXT")
        if "risk_score" not in transfer_columns:
            db.execute("ALTER TABLE transfers ADD COLUMN risk_score INTEGER NOT NULL DEFAULT 0")
        if "risk_reason" not in transfer_columns:
            db.execute("ALTER TABLE transfers ADD COLUMN risk_reason TEXT NOT NULL DEFAULT ''")
        user_columns = table_columns(db, "users")
        if "phone_number" not in user_columns:
            db.execute("ALTER TABLE users ADD COLUMN phone_number TEXT")
        db.execute("CREATE UNIQUE INDEX IF NOT EXISTS users_phone_unique ON users(phone_number) WHERE phone_number IS NOT NULL")

        # Old checked_at values were overwritten when receipts were viewed. Recover
        # completion from a success audit or a matching completed schedule instead.
        # Without either record, creation time is the documented historical fallback.
        # Only fill missing values: restarting must never move a completed transfer.
        db.execute("""UPDATE transfers SET completed_at=COALESCE(
            (SELECT a.created_at FROM audit_logs a
             WHERE a.entity_type='transfer' AND a.entity_id=transfers.request_id
               AND a.action IN ('Transfer completed','Scheduled transfer completed')
               AND julianday(a.created_at)>=julianday(transfers.created_at)
             ORDER BY julianday(a.created_at),a.id LIMIT 1),
            (SELECT s.updated_at FROM scheduled_transfers s
             WHERE s.request_id=transfers.request_id AND s.status='Completed'
               AND s.sender_account_id=transfers.sender_account_id
               AND s.recipient_account_id=transfers.recipient_account_id
               AND s.amount_cents=transfers.amount_cents
               AND s.scheduled_for=transfers.scheduled_for
               AND julianday(s.updated_at)>=julianday(transfers.created_at)),
            created_at)
            WHERE status='Completed' AND completed_at IS NULL""")
        db.execute("CREATE INDEX IF NOT EXISTS transfers_sender_completed ON transfers(sender_account_id,completed_at)")

        sample_seeded = db.execute("SELECT 1 FROM app_meta WHERE key='sample_accounts_seeded'").fetchone()
        seed_sample_accounts = db.execute("SELECT COUNT(*) FROM customers").fetchone()[0] == 0 and not sample_seeded
        if seed_sample_accounts:
            db.executemany("INSERT INTO customers(id,name,account_number) VALUES(?,?,?)", [
                (1, "Aruzhan S.", "KZ•• 1042"), (2, "Dias T.", "KZ•• 8821"), (3, "Amina K.", "KZ•• 3307")
            ])
            db.executemany("INSERT INTO accounts(id,customer_id,label,balance_cents) VALUES(?,?,?,?)", [
                (1, 1, "Everyday account", 10_000_000),
                (2, 2, "Everyday account", 2_500_000),
                (3, 3, "Savings account", 7_500_000)
            ])
        db.execute("INSERT OR IGNORE INTO app_meta(key,value) VALUES('sample_accounts_seeded','true')")

        # Give the three original sample customers persistent local-demo sign-in credentials.
        demo_users = {
            1: ("Aruzhan S.", "aruzhan@example.test", "DemoPass!23"),
            2: ("Dias T.", "dias@example.test", "DemoPass!23"),
            3: ("Amina K.", "amina@example.test", "DemoPass!23")
        }
        for customer in db.execute("SELECT id,name FROM customers").fetchall():
            cid = customer["id"]
            linked_user = db.execute("SELECT owner_user_id FROM accounts WHERE customer_id=? AND owner_user_id IS NOT NULL LIMIT 1",
                                     (cid,)).fetchone()
            if linked_user:
                continue
            display, email, demo_password = demo_users.get(
                cid, (customer["name"], f"demo{cid}@example.test", "DemoPass!23")
            )
            if not db.execute("SELECT 1 FROM users WHERE email=? COLLATE NOCASE", (email,)).fetchone():
                db.execute("INSERT INTO users(full_name,email,password_hash,role,created_at) VALUES(?,?,?,?,?)",
                           (display, email, password_hash(demo_password), "customer", now()))
            user_id = db.execute("SELECT id FROM users WHERE email=? COLLATE NOCASE", (email,)).fetchone()[0]
            db.execute("UPDATE accounts SET owner_user_id=? WHERE customer_id=? AND owner_user_id IS NULL", (user_id, cid))
        db.execute("UPDATE users SET role='admin' WHERE email='aruzhan@example.test' COLLATE NOCASE")

        if seed_sample_accounts and db.execute("SELECT COUNT(*) FROM transfers").fetchone()[0] == 0:
            timestamp = now()
            db.execute("UPDATE accounts SET balance_cents=balance_cents-1000000 WHERE id=1")
            db.execute("UPDATE accounts SET balance_cents=balance_cents+1000000 WHERE id=2")
            db.executemany("""INSERT INTO transfers(request_id,sender_account_id,recipient_account_id,amount_cents,status,
                created_at,checked_at,completed_at,failure_reason,description,scheduled_for) VALUES(?,?,?,?,?,?,?,?,?,?,?)""", [
                ("TR-DEMO-COMPLETE", 1, 2, 1_000_000, "Completed", timestamp, timestamp, timestamp, None,
                 "Monthly allowance", None),
                ("TR-DEMO-FAILED", 1, 2, 15_000_000, "Failed", timestamp, timestamp, None, "Insufficient funds",
                 "Utility bills", None)
            ])

        db.execute("DELETE FROM sessions WHERE expires_at <= ?", (int(datetime.now(timezone.utc).timestamp()),))


def create_session(db: sqlite3.Connection, user_id: int) -> tuple[str, str]:
    sid, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    expires = int(datetime.now(timezone.utc).timestamp()) + SESSION_SECONDS
    db.execute("INSERT INTO sessions(session_id,user_id,csrf_token,expires_at) VALUES(?,?,?,?)", (sid, user_id, csrf, expires))
    return sid, csrf


def insert_audit(db: sqlite3.Connection, actor_user_id: int | None, action: str,
                 entity_type: str, entity_id: str, details: str) -> None:
    db.execute("INSERT INTO audit_logs(actor_user_id,action,entity_type,entity_id,details,created_at) VALUES(?,?,?,?,?,?)",
               (actor_user_id, action, entity_type, entity_id, details, now()))


# Suspicious-transfer detector: statistics and rules only. It flags, never blocks.
RISK_AVG_MULTIPLIER = 3
RISK_NEW_RECIPIENT_CENTS = int(os.environ.get("BANK_RISK_NEW_RECIPIENT_KZT", "100000")) * CENT
RISK_BURST_COUNT = 5
RISK_BURST_MINUTES = 10


def assess_risk(db: sqlite3.Connection, user_id: int, recipient_id: int, cents: int) -> tuple[int, list[str]]:
    """Score a new transfer (0-100) before it is inserted; returns (score, reasons)."""
    own = "sender_account_id IN (SELECT id FROM accounts WHERE owner_user_id=?)"
    score, reasons = 0, []

    average = db.execute(f"SELECT AVG(amount_cents) FROM transfers WHERE {own} AND status<>'Failed'",
                         (user_id,)).fetchone()[0]
    if average and cents > RISK_AVG_MULTIPLIER * average:
        score += 40
        reasons.append(f"Amount is {cents / average:.1f}x the customer's average of {average / CENT:,.2f} KZT")

    known = db.execute(f"SELECT 1 FROM transfers WHERE {own} AND recipient_account_id=? LIMIT 1",
                       (user_id, recipient_id)).fetchone()
    if not known and cents >= RISK_NEW_RECIPIENT_CENTS:
        score += 35
        reasons.append(f"Large amount ({cents / CENT:,.2f} KZT) to a new recipient")

    since = (datetime.now(timezone.utc) - timedelta(minutes=RISK_BURST_MINUTES)).isoformat(timespec="seconds")
    recent = db.execute(f"SELECT COUNT(*) FROM transfers WHERE {own} AND datetime(created_at)>=datetime(?)",
                        (user_id, since)).fetchone()[0]
    if recent >= RISK_BURST_COUNT:  # this transfer would be number recent + 1
        score += 25
        reasons.append(f"More than {RISK_BURST_COUNT} transfers in {RISK_BURST_MINUTES} minutes")

    return min(score, 100), reasons


def parse_scheduled_for(value: str) -> datetime:
    try:
        due = datetime.fromisoformat(value)
        if due.tzinfo is None:
            due = due.replace(tzinfo=KZ_TIMEZONE)
        return due.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("Choose a valid scheduled date and time") from exc


def schedule_pending() -> None:
    with connect() as db:
        ids = [row[0] for row in db.execute("SELECT request_id FROM transfers WHERE status='Pending'")]
        scheduled = [(row[0], row[1]) for row in db.execute(
            "SELECT request_id,scheduled_for FROM scheduled_transfers WHERE status='Scheduled'")]
    for request_id in ids:
        schedule_transfer(request_id)
    for request_id, scheduled_for in scheduled:
        schedule_scheduled_transfer(request_id, scheduled_for)


def schedule_transfer(request_id: str) -> None:
    timer = threading.Timer(TRANSFER_DELAY_SECONDS, process_transfer, args=(request_id,))
    timer.daemon = True
    timer.start()


def schedule_scheduled_transfer(request_id: str, scheduled_for: str) -> None:
    try:
        delay = max((parse_scheduled_for(scheduled_for) - datetime.now(timezone.utc)).total_seconds(), 0)
    except ValueError:
        delay = 0
    timer = threading.Timer(delay, process_scheduled_transfer, args=(request_id,))
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
            sender = db.execute("SELECT balance_cents,owner_user_id FROM accounts WHERE id=?", (transfer["sender_account_id"],)).fetchone()
            if not sender or sender["balance_cents"] < transfer["amount_cents"]:
                db.execute("UPDATE transfers SET status='Failed',failure_reason='Insufficient funds',checked_at=? WHERE request_id=?",
                           (now(), request_id))
                insert_audit(db, sender["owner_user_id"] if sender else None, "Transfer failed", "transfer", request_id,
                             f"{transfer['amount_cents'] / CENT:,.2f} KZT was not sent: insufficient funds.")
            else:
                db.execute("UPDATE accounts SET balance_cents=balance_cents-? WHERE id=?",
                           (transfer["amount_cents"], transfer["sender_account_id"]))
                db.execute("UPDATE accounts SET balance_cents=balance_cents+? WHERE id=?",
                           (transfer["amount_cents"], transfer["recipient_account_id"]))
                completed = now()
                db.execute("UPDATE transfers SET status='Completed',checked_at=?,completed_at=? WHERE request_id=?",
                           (completed, completed, request_id))
                insert_audit(db, sender["owner_user_id"], "Transfer completed", "transfer", request_id,
                             f"{transfer['amount_cents'] / CENT:,.2f} KZT transfer completed.")
            db.commit()
    except sqlite3.Error as exc:
        print(f"Could not process pending transfer {request_id}: {exc}")


def process_scheduled_transfer(request_id: str) -> None:
    try:
        with DB_LOCK, connect() as db:
            db.execute("BEGIN IMMEDIATE")
            scheduled = db.execute("SELECT * FROM scheduled_transfers WHERE request_id=?", (request_id,)).fetchone()
            if not scheduled or scheduled["status"] != "Scheduled":
                db.rollback()
                return
            due = parse_scheduled_for(scheduled["scheduled_for"])
            if due > datetime.now(timezone.utc):
                db.rollback()
                schedule_scheduled_transfer(request_id, scheduled["scheduled_for"])
                return

            sender = db.execute("SELECT balance_cents,owner_user_id FROM accounts WHERE id=?",
                                (scheduled["sender_account_id"],)).fetchone()
            recipient = db.execute("SELECT id FROM accounts WHERE id=?", (scheduled["recipient_account_id"],)).fetchone()
            reason = None
            if not sender or sender["owner_user_id"] != scheduled["user_id"] or not recipient:
                reason = "Account is no longer available"
            elif scheduled["amount_cents"] < MIN_TRANSFER_CENTS:
                reason = "Amount is below the minimum transfer limit"
            elif scheduled["amount_cents"] > MAX_TRANSFER_CENTS:
                reason = "Amount exceeds the maximum per-transfer limit"
            else:
                start_utc, end_utc = local_day_bounds_utc()
                used = db.execute("""SELECT COALESCE(SUM(amount_cents),0) FROM transfers
                    WHERE sender_account_id IN (SELECT id FROM accounts WHERE owner_user_id=?)
                    AND status IN ('Pending','Completed') AND datetime(created_at)>=datetime(?) AND datetime(created_at)<datetime(?)""",
                                  (scheduled["user_id"], start_utc, end_utc)).fetchone()[0]
                if used + scheduled["amount_cents"] > DAILY_LIMIT_CENTS:
                    reason = "Daily transfer limit would be exceeded"
                elif sender["balance_cents"] < scheduled["amount_cents"]:
                    reason = "Insufficient funds at scheduled time"

            timestamp = now()
            if reason:
                db.execute("UPDATE scheduled_transfers SET status='Failed',failure_reason=?,updated_at=? WHERE request_id=?",
                           (reason, timestamp, request_id))
                insert_audit(db, scheduled["user_id"], "Scheduled transfer failed", "scheduled_transfer", request_id,
                             f"{scheduled['amount_cents'] / CENT:,.2f} KZT was not sent: {reason}.")
            else:
                try:
                    db.execute("""INSERT INTO transfers(request_id,sender_account_id,recipient_account_id,amount_cents,status,
                        created_at,checked_at,completed_at,failure_reason,description,scheduled_for) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                               (request_id, scheduled["sender_account_id"], scheduled["recipient_account_id"],
                                scheduled["amount_cents"], "Completed", timestamp, timestamp, timestamp, None,
                                scheduled["reminder_note"], scheduled["scheduled_for"]))
                except sqlite3.IntegrityError:
                    reason = "Transfer reference is already in use"
                    db.execute("UPDATE scheduled_transfers SET status='Failed',failure_reason=?,updated_at=? WHERE request_id=?",
                               (reason, timestamp, request_id))
                    insert_audit(db, scheduled["user_id"], "Scheduled transfer failed", "scheduled_transfer", request_id,
                                 f"The transfer could not be created: {reason}.")
                else:
                    db.execute("UPDATE accounts SET balance_cents=balance_cents-? WHERE id=?",
                               (scheduled["amount_cents"], scheduled["sender_account_id"]))
                    db.execute("UPDATE accounts SET balance_cents=balance_cents+? WHERE id=?",
                               (scheduled["amount_cents"], scheduled["recipient_account_id"]))
                    db.execute("UPDATE scheduled_transfers SET status='Completed',updated_at=? WHERE request_id=?",
                               (timestamp, request_id))
                    insert_audit(db, scheduled["user_id"], "Scheduled transfer completed", "transfer", request_id,
                                 f"{scheduled['amount_cents'] / CENT:,.2f} KZT scheduled transfer completed.")
            db.commit()
    except (sqlite3.Error, ValueError) as exc:
        print(f"Could not process scheduled transfer {request_id}: {exc}")


def account_id_from(value) -> int:
    """Reject fractional/boolean IDs instead of silently selecting another account."""
    message = "Choose one of your accounts and a different recipient"
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError(message)
    if isinstance(value, str) and not re.fullmatch(r"[0-9]+", value):
        raise ValueError(message)
    try:
        account_id = int(value)
    except ValueError as exc:
        raise ValueError(message) from exc
    if not 0 < account_id <= 2**63 - 1:
        raise ValueError(message)
    return account_id


def cents_from(value) -> int:
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or amount <= 0 or amount.as_tuple().exponent < -2:
            raise ValueError("Enter a positive amount with up to 2 decimal places")
        # Bound before multiplication/int conversion: large exponents must be a
        # validation error, not Decimal overflow or an oversized SQLite integer.
        if amount > Decimal(2**63 - 1) / CENT:
            raise ValueError("Enter a valid transfer amount")
        cents = int(amount * CENT)
    except (InvalidOperation, TypeError, ValueError) as exc:
        if isinstance(exc, ValueError) and str(exc):
            raise
        raise ValueError("Enter a valid transfer amount") from exc
    return cents


def local_day_bounds_utc() -> tuple[str, str]:
    local = datetime.now(KZ_TIMEZONE)
    start_local = local.replace(hour=0, minute=0, second=0, microsecond=0)
    end_local = start_local + timedelta(days=1)
    return (start_local.astimezone(timezone.utc).isoformat(timespec="seconds"),
            end_local.astimezone(timezone.utc).isoformat(timespec="seconds"))


def analytics_periods(as_of: datetime) -> tuple[dict, dict]:
    """Current calendar week (Monday first) and month in Kazakhstan's UTC+5."""
    local = as_of.astimezone(KZ_TIMEZONE)
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = midnight - timedelta(days=midnight.weekday())
    month_start = midnight.replace(day=1)
    month_end = (month_start.replace(day=28) + timedelta(days=4)).replace(day=1)

    def period(start: datetime, end: datetime) -> dict:
        return {"start": start.astimezone(timezone.utc).isoformat(timespec="seconds"),
                "end": end.astimezone(timezone.utc).isoformat(timespec="seconds"),
                "totalAmount": 0, "count": 0}

    return period(week_start, week_start + timedelta(days=7)), period(month_start, month_end)


def transfer_analytics(user_id: int) -> dict:
    as_of = now()
    week, month = analytics_periods(datetime.fromisoformat(as_of))
    # Read the full relevant ledger, independently of the 50-row activity feed.
    # Integer tiyn are summed before conversion to the API's KZT amounts.
    with connect() as db:
        rows = db.execute("""SELECT t.request_id,t.amount_cents,t.completed_at,
                ra.owner_user_id recipient_user_id,ra.customer_id recipient_customer_id,
                COALESCE(ru.full_name,rc.name) recipient_name,rc.account_number recipient_number,
                julianday(t.completed_at)>=julianday(?) in_week,
                julianday(t.completed_at)>=julianday(?) in_month
            FROM transfers t JOIN accounts sa ON sa.id=t.sender_account_id
            JOIN accounts ra ON ra.id=t.recipient_account_id
            JOIN customers rc ON rc.id=ra.customer_id
            LEFT JOIN users ru ON ru.id=ra.owner_user_id
            WHERE sa.owner_user_id=? AND t.status='Completed'
              AND julianday(t.completed_at)>=julianday(?)
              AND julianday(t.completed_at)<=julianday(?)
            ORDER BY julianday(t.completed_at),t.request_id""",
                          (week["start"], month["start"], user_id,
                           min(week["start"], month["start"]), as_of)).fetchall()
    weekly = [row for row in rows if row["in_week"]]
    monthly = [row for row in rows if row["in_month"]]
    for period, entries in ((week, weekly), (month, monthly)):
        period["totalAmount"] = sum(row["amount_cents"] for row in entries) / CENT
        period["count"] = len(entries)

    def transfer_summary(row: sqlite3.Row) -> dict:
        return {"requestId": row["request_id"], "amount": row["amount_cents"] / CENT,
                "recipient": row["recipient_name"], "recipientNumber": row["recipient_number"],
                "completedAt": row["completed_at"]}

    month["largest"] = transfer_summary(max(monthly, key=lambda row: row["amount_cents"])) if monthly else None
    month["smallest"] = transfer_summary(min(monthly, key=lambda row: row["amount_cents"])) if monthly else None
    recipients = {}
    for row in monthly:
        # Legacy accounts without an owner still represent distinct customers.
        # Negative customer IDs cannot collide with positive registered user IDs.
        recipient_id = row["recipient_user_id"] or -row["recipient_customer_id"]
        recipient = recipients.setdefault(recipient_id, {
            "recipientId": recipient_id, "name": row["recipient_name"], "count": 0, "totalCents": 0})
        recipient["count"] += 1
        recipient["totalCents"] += row["amount_cents"]
    highest_count = max((recipient["count"] for recipient in recipients.values()), default=0)
    leaders = [{"recipientId": recipient["recipientId"], "name": recipient["name"],
                "count": recipient["count"], "totalAmount": recipient["totalCents"] / CENT}
               for recipient in recipients.values() if recipient["count"] == highest_count]
    month["frequentRecipients"] = sorted(leaders, key=lambda recipient: (recipient["name"].casefold(), recipient["recipientId"]))
    return {"currency": "KZT", "timeZone": "UTC+5", "asOf": as_of, "week": week, "month": month}


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
            row = db.execute("""SELECT s.session_id,s.user_id,s.csrf_token,u.full_name,u.email,u.phone_number,u.role
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
        public_files = {
            "/": ("index.html", "text/html; charset=utf-8"),
            "/analytics.js": ("analytics.js", "text/javascript; charset=utf-8"),
            "/analytics.css": ("analytics.css", "text/css; charset=utf-8"),
            "/date-picker.js": ("date-picker.js", "text/javascript; charset=utf-8"),
            "/date-picker.css": ("date-picker.css", "text/css; charset=utf-8"),
        }
        if path in public_files:
            filename, content_type = public_files[path]
            body = (ROOT / filename).read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
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
                                     "user": {"name": user["full_name"], "email": user["email"], "phone": user["phone_number"], "role": user["role"]}})
            return
        user = self.require_user()
        if not user:
            return
        if path == "/api/profile":
            self.send_json(200, {"name": user["full_name"], "email": user["email"], "phone": user["phone_number"], "role": user["role"]})
            return
        if path == "/api/analytics":
            self.send_json(200, transfer_analytics(user["user_id"]))
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
        if path == "/api/audit-logs":
            if user["role"] != "admin":
                self.send_json(403, {"error": "Audit log access is limited to aruzhan@example.test"})
                return
            with connect() as db:
                rows = db.execute("""SELECT action,entity_type,entity_id,details,created_at FROM audit_logs
                    WHERE actor_user_id=? ORDER BY id DESC""", (user["user_id"],)).fetchall()
                self.send_json(200, [{"action": r["action"], "entityType": r["entity_type"],
                                     "reference": r["entity_id"], "details": r["details"],
                                     "createdAt": r["created_at"]} for r in rows])
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
                    ORDER BY t.created_at DESC""", (*ids, *ids)).fetchall()
                entries = [self.transfer_payload(r, ids) for r in rows]
                scheduled = db.execute("""SELECT s.*,su.full_name sender_name,ru.full_name recipient_name,
                    sc.account_number sender_number,rc.account_number recipient_number
                    FROM scheduled_transfers s JOIN accounts sa ON sa.id=s.sender_account_id
                    JOIN accounts ra ON ra.id=s.recipient_account_id
                    JOIN customers sc ON sc.id=sa.customer_id JOIN customers rc ON rc.id=ra.customer_id
                    LEFT JOIN users su ON su.id=sa.owner_user_id LEFT JOIN users ru ON ru.id=ra.owner_user_id
                    WHERE s.user_id=? AND s.status IN ('Scheduled','Cancelled','Failed')
                    ORDER BY s.created_at DESC""", (user["user_id"],)).fetchall()
                entries.extend(self.scheduled_payload(r) for r in scheduled)
                entries.sort(key=lambda row: row["createdAt"], reverse=True)
                self.send_json(200, entries)
            return
        match = re.fullmatch(r"/api/transfers/([^/]+)", path)
        if match:
            ids = self.user_account_ids(user["user_id"])
            with connect() as db:
                row = self.transfer_row(db, match.group(1))
                if row and (row["sender_account_id"] in ids or row["recipient_account_id"] in ids):
                    db.execute("UPDATE transfers SET checked_at=? WHERE request_id=? AND status='Pending'",
                               (now(), row["request_id"]))
                    row = self.transfer_row(db, row["request_id"])
                    result = self.transfer_payload(row, ids)
                else:
                    scheduled = self.scheduled_row(db, match.group(1))
                    if not scheduled or scheduled["user_id"] != user["user_id"]:
                        self.send_json(404, {"error": "Transfer not found"})
                        return
                    db.execute("UPDATE scheduled_transfers SET updated_at=? WHERE request_id=? AND status='Scheduled'",
                               (now(), scheduled["request_id"]))
                    scheduled = self.scheduled_row(db, match.group(1))
                    result = self.scheduled_payload(scheduled)
                insert_audit(db, user["user_id"], "Transfer details viewed", "transfer", result["requestId"],
                             f"Viewed transfer details; latest status is {result['status']}.")
                self.send_json(200, result)
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
    def scheduled_row(db: sqlite3.Connection, request_id: str):
        return db.execute("""SELECT s.*,su.full_name sender_name,ru.full_name recipient_name,
            sc.account_number sender_number,rc.account_number recipient_number
            FROM scheduled_transfers s JOIN accounts sa ON sa.id=s.sender_account_id
            JOIN accounts ra ON ra.id=s.recipient_account_id
            JOIN customers sc ON sc.id=sa.customer_id JOIN customers rc ON rc.id=ra.customer_id
            LEFT JOIN users su ON su.id=sa.owner_user_id LEFT JOIN users ru ON ru.id=ra.owner_user_id
            WHERE s.request_id=?""", (request_id,)).fetchone()

    @staticmethod
    def transfer_payload(row: sqlite3.Row, account_ids: list[int]) -> dict:
        outgoing = row["sender_account_id"] in account_ids
        return {"requestId": row["request_id"], "fromAccountId": row["sender_account_id"],
                "toAccountId": row["recipient_account_id"], "from": row["sender_name"],
                "to": row["recipient_name"], "counterparty": row["recipient_name"] if outgoing else row["sender_name"],
                "recipientNumber": row["recipient_number"], "amount": row["amount_cents"] / CENT,
                "direction": "outgoing" if outgoing else "incoming", "status": row["status"],
                "description": row["description"], "createdAt": row["created_at"],
                "checkedAt": row["checked_at"], "completedAt": row["completed_at"], "failureReason": row["failure_reason"],
                "scheduledFor": row["scheduled_for"]}

    @staticmethod
    def scheduled_payload(row: sqlite3.Row) -> dict:
        return {"requestId": row["request_id"], "fromAccountId": row["sender_account_id"],
                "toAccountId": row["recipient_account_id"], "from": row["sender_name"],
                "to": row["recipient_name"], "counterparty": row["recipient_name"],
                "recipientNumber": row["recipient_number"], "amount": row["amount_cents"] / CENT,
                "direction": "outgoing", "status": row["status"], "description": row["reminder_note"],
                "createdAt": row["created_at"], "checkedAt": row["updated_at"],
                "completedAt": row["updated_at"] if row["status"] == "Completed" else None,
                "failureReason": row["failure_reason"], "scheduledFor": row["scheduled_for"]}

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/password/request-reset":
            self.request_password_reset()
            return
        if path == "/api/password/reset":
            self.reset_password_with_token()
            return
        if path == "/api/password/change":
            self.change_password()
            return
        if path in ("/api/register", "/api/login"):
            self.auth_post(path)
            return
        if path == "/api/logout":
            user = self.require_user(csrf=True)
            if not user:
                return
            with connect() as db:
                insert_audit(db, user["user_id"], "User signed out", "user", str(user["user_id"]),
                             f"{user['full_name']} signed out.")
                db.execute("DELETE FROM sessions WHERE session_id=?", (user["session_id"],))
            self.send_json(200, {"ok": True}, [("Set-Cookie", "bank_session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0")])
            return
        user = self.require_user(csrf=True)
        if not user:
            return
        if path == "/api/profile":
            self.update_profile(user)
            return
        if path == "/api/account/delete":
            self.delete_account(user)
            return
        if path == "/api/next-id":
            self.send_json(200, {"requestId": "TR-" + secrets.token_hex(16).upper()})
            return
        cancel_match = re.fullmatch(r"/api/scheduled/([^/]+)/cancel", path)
        if cancel_match:
            request_id = cancel_match.group(1)
            with DB_LOCK, connect() as db:
                db.execute("BEGIN IMMEDIATE")
                scheduled = db.execute("SELECT * FROM scheduled_transfers WHERE request_id=? AND user_id=?",
                                       (request_id, user["user_id"])).fetchone()
                if not scheduled:
                    db.rollback()
                    self.send_json(404, {"error": "Scheduled transfer not found"})
                    return
                if scheduled["status"] != "Scheduled":
                    db.rollback()
                    self.send_json(409, {"error": f"This transfer is already {scheduled['status'].lower()} and cannot be cancelled"})
                    return
                timestamp = now()
                db.execute("UPDATE scheduled_transfers SET status='Cancelled',updated_at=? WHERE request_id=?",
                           (timestamp, request_id))
                insert_audit(db, user["user_id"], "Scheduled transfer cancelled", "scheduled_transfer", request_id,
                             f"Cancelled scheduled payment for {scheduled['amount_cents'] / CENT:,.2f} KZT.")
                result = self.scheduled_row(db, request_id)
                db.commit()
            self.send_json(200, self.scheduled_payload(result))
            return
        if path == "/api/scheduled":
            self.create_scheduled(user)
            return
        if path != "/api/transfers":
            self.send_json(404, {"error": "Not found"})
            return
        try:
            payload = self.read_json()
            request_id = str(payload.get("requestId", "")).strip()
            if not re.fullmatch(r"TR-[A-F0-9]{32}", request_id):
                raise ValueError("Transfer reference is invalid. Please start a new transfer")
            sender_id = account_id_from(payload.get("fromAccountId", 0))
            recipient_phone = normalize_phone(payload.get("recipientPhone"))
            recipient_id = None if recipient_phone else account_id_from(payload.get("toAccountId", 0))
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
            if recipient_phone:
                recipient = db.execute("""SELECT a.* FROM users u JOIN accounts a ON a.owner_user_id=u.id
                    WHERE u.phone_number=? AND u.id<>? ORDER BY a.id LIMIT 1""",
                                       (recipient_phone, user["user_id"])).fetchone()
                recipient_id = recipient["id"] if recipient else None
            else:
                recipient = db.execute("SELECT * FROM accounts WHERE id=?", (recipient_id,)).fetchone()
            if not sender or not recipient or sender_id == recipient_id:
                db.rollback()
                message = "No account was found for this phone number" if recipient_phone and not recipient else "Choose one of your accounts and a different recipient"
                self.send_json(400, {"error": message})
                return

            if db.execute("SELECT 1 FROM scheduled_transfers WHERE request_id=?", (request_id,)).fetchone():
                db.rollback()
                self.send_json(409, {"error": "This reference is already attached to a scheduled transfer"})
                return

            existing = db.execute("SELECT * FROM transfers WHERE request_id=?", (request_id,)).fetchone()
            if existing:
                matches = (existing["sender_account_id"] == sender_id and existing["recipient_account_id"] == recipient_id
                           and existing["amount_cents"] == cents and existing["description"] == description)
                if not matches:
                    db.rollback()
                    self.send_json(409, {"error": "This reference is already attached to different transfer details"})
                    return
                insert_audit(db, user["user_id"], "Transfer retry received", "transfer", request_id,
                             f"Repeated request returned existing {existing['status'].lower()} transfer; no second debit was made.")
                row = self.transfer_row(db, request_id)
                db.commit()
                self.send_json(200, self.transfer_payload(row, [sender_id]))
                return

            if cents < MIN_TRANSFER_CENTS:
                insert_audit(db, user["user_id"], "Transfer rejected", "transfer", request_id,
                             f"Rejected {cents / CENT:,.2f} KZT amount below the minimum limit.")
                db.commit()
                self.send_json(400, {"error": f"Minimum transfer is {MIN_TRANSFER_CENTS / CENT:,.2f} KZT"})
                return
            if cents > MAX_TRANSFER_CENTS:
                insert_audit(db, user["user_id"], "Transfer rejected", "transfer", request_id,
                             f"Rejected {cents / CENT:,.2f} KZT amount above the maximum limit.")
                db.commit()
                self.send_json(400, {"error": f"Maximum per transfer is {MAX_TRANSFER_CENTS / CENT:,.2f} KZT"})
                return

            start_utc, end_utc = local_day_bounds_utc()
            total = db.execute("""SELECT COALESCE(SUM(amount_cents),0) FROM transfers
                WHERE sender_account_id IN (SELECT id FROM accounts WHERE owner_user_id=?)
                AND status IN ('Pending','Completed') AND datetime(created_at)>=datetime(?) AND datetime(created_at)<datetime(?)""",
                               (user["user_id"], start_utc, end_utc)).fetchone()[0]
            if total + cents > DAILY_LIMIT_CENTS:
                remaining = max(DAILY_LIMIT_CENTS - total, 0) / CENT
                insert_audit(db, user["user_id"], "Transfer rejected", "transfer", request_id,
                             f"Rejected because the daily limit would be exceeded; {remaining:,.2f} KZT remained.")
                db.commit()
                self.send_json(400, {"error": f"Daily transfer limit is {DAILY_LIMIT_CENTS / CENT:,.2f} KZT. Remaining today: {remaining:,.2f} KZT"})
                return

            risk_score, risk_reasons = assess_risk(db, user["user_id"], recipient_id, cents)
            created = now()
            db.execute("""INSERT INTO transfers(request_id,sender_account_id,recipient_account_id,amount_cents,status,
                created_at,checked_at,failure_reason,description,risk_score,risk_reason) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                       (request_id, sender_id, recipient_id, cents, "Pending", created, created, None, description,
                        risk_score, "; ".join(risk_reasons)))
            row = self.transfer_row(db, request_id)
            insert_audit(db, user["user_id"], "Transfer submitted", "transfer", request_id,
                         f"Submitted {cents / CENT:,.2f} KZT to {row['recipient_name']}; current status is Pending.")
            if risk_reasons:
                insert_audit(db, user["user_id"], "Transfer flagged as suspicious", "transfer", request_id,
                             f"Risk score {risk_score}/100: " + "; ".join(risk_reasons) + ".")
            db.commit()
        schedule_transfer(request_id)
        self.send_json(202, self.transfer_payload(row, [sender_id]))

    def create_scheduled(self, user: sqlite3.Row) -> None:
        try:
            payload = self.read_json()
            request_id = str(payload.get("requestId", "")).strip()
            if not re.fullmatch(r"TR-[A-F0-9]{32}", request_id):
                raise ValueError("Transfer reference is invalid. Please start a new transfer")
            sender_id = account_id_from(payload.get("fromAccountId", 0))
            recipient_phone = normalize_phone(payload.get("recipientPhone"))
            recipient_id = None if recipient_phone else account_id_from(payload.get("toAccountId", 0))
            cents = cents_from(payload.get("amount", 0))
            reminder = str(payload.get("reminderNote", "")).strip()
            due = parse_scheduled_for(str(payload.get("scheduledFor", "")))
            current = datetime.now(timezone.utc)
            if due < current + timedelta(seconds=SCHEDULE_MIN_SECONDS):
                raise ValueError("Choose a time at least 30 seconds in the future")
            if due > current + timedelta(days=SCHEDULE_MAX_DAYS):
                raise ValueError("Transfers can be scheduled up to 365 days ahead")
            if len(reminder) > 200:
                raise ValueError("Reminder note must be 200 characters or fewer")
        except (ValueError, TypeError) as exc:
            self.send_json(400, {"error": str(exc) or "Invalid scheduled transfer"})
            return

        if cents < MIN_TRANSFER_CENTS:
            self.send_json(400, {"error": f"Minimum transfer is {MIN_TRANSFER_CENTS / CENT:,.2f} KZT"})
            return
        if cents > MAX_TRANSFER_CENTS:
            self.send_json(400, {"error": f"Maximum per transfer is {MAX_TRANSFER_CENTS / CENT:,.2f} KZT"})
            return

        with DB_LOCK, connect() as db:
            db.execute("BEGIN IMMEDIATE")
            sender = db.execute("SELECT id FROM accounts WHERE id=? AND owner_user_id=?",
                                (sender_id, user["user_id"])).fetchone()
            if recipient_phone:
                recipient = db.execute("""SELECT a.id FROM users u JOIN accounts a ON a.owner_user_id=u.id
                    WHERE u.phone_number=? AND u.id<>? ORDER BY a.id LIMIT 1""",
                                       (recipient_phone, user["user_id"])).fetchone()
                recipient_id = recipient["id"] if recipient else None
            else:
                recipient = db.execute("SELECT id FROM accounts WHERE id=?", (recipient_id,)).fetchone()
            used = db.execute("SELECT 1 FROM transfers WHERE request_id=? UNION SELECT 1 FROM scheduled_transfers WHERE request_id=?",
                              (request_id, request_id)).fetchone()
            if not sender or not recipient or sender_id == recipient_id:
                db.rollback()
                message = "No account was found for this phone number" if recipient_phone and not recipient else "Choose one of your accounts and a different recipient"
                self.send_json(400, {"error": message})
                return
            if used:
                db.rollback()
                self.send_json(409, {"error": "This transfer reference is already in use"})
                return
            due_text = due.isoformat()
            timestamp = now()
            db.execute("""INSERT INTO scheduled_transfers(request_id,user_id,sender_account_id,recipient_account_id,
                amount_cents,scheduled_for,reminder_note,status,failure_reason,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,'Scheduled',NULL,?,?)""",
                       (request_id, user["user_id"], sender_id, recipient_id, cents, due_text, reminder, timestamp, timestamp))
            result = self.scheduled_row(db, request_id)
            local_due = due.astimezone(KZ_TIMEZONE).strftime("%Y-%m-%d %H:%M UTC+5")
            insert_audit(db, user["user_id"], "Scheduled transfer created", "scheduled_transfer", request_id,
                         f"Scheduled {cents / CENT:,.2f} KZT for {local_due}. Reminder: {reminder or 'No reminder note'}.")
            db.commit()
        schedule_scheduled_transfer(request_id, due_text)
        self.send_json(201, self.scheduled_payload(result))

    def update_profile(self, session_user: sqlite3.Row) -> None:
        try:
            payload = self.read_json()
            name = str(payload.get("name", "")).strip()
            email = str(payload.get("email", "")).strip().lower()
            phone = normalize_phone(payload.get("phone"))
            current_password = str(payload.get("currentPassword", ""))
            if not 2 <= len(name) <= 80:
                raise ValueError("Name must be between 2 and 80 characters")
            if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email) or len(email) > 254:
                raise ValueError("Enter a valid email address")
            if not current_password or len(current_password) > 128:
                raise ValueError("Enter your current password")
        except (ValueError, TypeError) as exc:
            self.send_json(400, {"error": str(exc) or "Invalid profile details"})
            return

        with DB_LOCK, connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("SELECT * FROM users WHERE id=?", (session_user["user_id"],)).fetchone()
            if not current or not verify_password(current_password, current["password_hash"]):
                db.rollback()
                self.send_json(401, {"error": "Current password is incorrect"})
                return
            if db.execute("SELECT 1 FROM users WHERE email=? COLLATE NOCASE AND id<>?", (email, current["id"])).fetchone():
                db.rollback()
                self.send_json(409, {"error": "An account already uses this email address"})
                return
            if email == "aruzhan@example.test" and current["role"] != "admin":
                db.rollback()
                self.send_json(409, {"error": "This email address is reserved for the audit-log account."})
                return
            if phone and db.execute("SELECT 1 FROM users WHERE phone_number=? AND id<>?", (phone, current["id"])).fetchone():
                db.rollback()
                self.send_json(409, {"error": "This phone number is already linked to another account"})
                return
            changes = []
            if current["full_name"] != name: changes.append("name")
            if current["email"].lower() != email: changes.append("email")
            if current["phone_number"] != phone: changes.append("phone number")
            db.execute("UPDATE users SET full_name=?,email=?,phone_number=? WHERE id=?",
                       (name, email, phone, current["id"]))
            db.execute("UPDATE customers SET name=? WHERE id IN (SELECT customer_id FROM accounts WHERE owner_user_id=?)",
                       (name, current["id"]))
            if changes:
                insert_audit(db, current["id"], "Profile updated", "user", str(current["id"]),
                             "Updated personal information: " + ", ".join(changes) + ".")
            db.commit()
        self.send_json(200, {"ok": True, "user": {"name": name, "email": email, "phone": phone, "role": current["role"]}})

    def delete_account(self, session_user: sqlite3.Row) -> None:
        try:
            payload = self.read_json()
            password = str(payload.get("password", ""))
            confirmation = str(payload.get("confirmation", ""))
            if not password or len(password) > 128:
                raise ValueError("Enter your current password")
            if confirmation != "DELETE":
                raise ValueError("Type DELETE to confirm account deletion")
        except (ValueError, TypeError) as exc:
            self.send_json(400, {"error": str(exc) or "Invalid deletion request"})
            return

        with DB_LOCK, connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("SELECT * FROM users WHERE id=?", (session_user["user_id"],)).fetchone()
            if not current or not verify_password(password, current["password_hash"]):
                db.rollback()
                self.send_json(401, {"error": "Current password is incorrect"})
                return
            accounts = db.execute("SELECT id,customer_id FROM accounts WHERE owner_user_id=?", (current["id"],)).fetchall()
            account_ids = [row["id"] for row in accounts]
            customer_ids = {row["customer_id"] for row in accounts}
            transfer_refs = []
            schedule_refs = [row[0] for row in db.execute("SELECT request_id FROM scheduled_transfers WHERE user_id=?", (current["id"],))]
            if account_ids:
                marks = ",".join("?" for _ in account_ids)
                transfer_refs = [row[0] for row in db.execute(
                    f"SELECT request_id FROM transfers WHERE sender_account_id IN ({marks}) OR recipient_account_id IN ({marks})",
                    (*account_ids, *account_ids))]
                schedule_refs = list(set(schedule_refs + [row[0] for row in db.execute(
                    f"SELECT request_id FROM scheduled_transfers WHERE sender_account_id IN ({marks}) OR recipient_account_id IN ({marks})",
                    (*account_ids, *account_ids))]))
            db.execute("DELETE FROM scheduled_transfers WHERE user_id=?", (current["id"],))
            if account_ids:
                marks = ",".join("?" for _ in account_ids)
                db.execute(f"DELETE FROM scheduled_transfers WHERE sender_account_id IN ({marks}) OR recipient_account_id IN ({marks})",
                           (*account_ids, *account_ids))
                db.execute(f"DELETE FROM transfers WHERE sender_account_id IN ({marks}) OR recipient_account_id IN ({marks})",
                           (*account_ids, *account_ids))
            if schedule_refs:
                marks = ",".join("?" for _ in schedule_refs)
                db.execute(f"DELETE FROM audit_logs WHERE entity_type='scheduled_transfer' AND entity_id IN ({marks})", tuple(schedule_refs))
            if transfer_refs:
                marks = ",".join("?" for _ in transfer_refs)
                db.execute(f"DELETE FROM audit_logs WHERE entity_type='transfer' AND entity_id IN ({marks})", tuple(transfer_refs))
            db.execute("DELETE FROM audit_logs WHERE actor_user_id=?", (current["id"],))
            db.execute("DELETE FROM sessions WHERE user_id=?", (current["id"],))
            if account_ids:
                marks = ",".join("?" for _ in account_ids)
                db.execute(f"DELETE FROM accounts WHERE id IN ({marks})", tuple(account_ids))
            for customer_id in customer_ids:
                db.execute("DELETE FROM customers WHERE id=? AND NOT EXISTS (SELECT 1 FROM accounts WHERE customer_id=?)",
                           (customer_id, customer_id))
            db.execute("DELETE FROM users WHERE id=?", (current["id"],))
            db.commit()
        self.send_json(200, {"ok": True, "message": "Account deleted"},
                       [("Set-Cookie", "bank_session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0")])

    def request_password_reset(self) -> None:
        config = password_reset_mail_config()
        if not config:
            self.send_json(503, {"error": "Password reset email is not configured on the server"})
            return
        try:
            payload = self.read_json()
            email = str(payload.get("email", "")).strip().lower()
            language = str(payload.get("language", "en"))
            if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email) or len(email) > 254:
                raise ValueError("Enter a valid email address")
            if language not in ("en", "ru", "kk"):
                language = "en"
        except (ValueError, TypeError) as exc:
            self.send_json(400, {"error": str(exc) or "Invalid request"})
            return

        current_time = int(datetime.now(timezone.utc).timestamp())
        email_key = hmac.new(PASSWORD_RESET_RATE_KEY, b"email:" + email.encode(), hashlib.sha256).hexdigest()
        remote_ip = str(self.client_address[0])
        ip_key = hmac.new(PASSWORD_RESET_RATE_KEY, b"ip:" + remote_ip.encode(), hashlib.sha256).hexdigest()
        delivery = None
        with DB_LOCK, connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM password_reset_attempts WHERE requested_at<?", (current_time - 3600,))
            email_count = db.execute("SELECT COUNT(*) FROM password_reset_attempts WHERE email_key=? AND requested_at>=?",
                                     (email_key, current_time - 3600)).fetchone()[0]
            ip_count = db.execute("SELECT COUNT(*) FROM password_reset_attempts WHERE ip_key=? AND requested_at>=?",
                                  (ip_key, current_time - 3600)).fetchone()[0]
            generic = {"ok": True, "message": "If an account exists for that email, a password reset link has been sent."}
            if email_count >= PASSWORD_RESET_RATE_LIMIT or ip_count >= PASSWORD_RESET_RATE_LIMIT:
                db.commit()
                self.send_json(200, generic)
                return
            db.execute("INSERT INTO password_reset_attempts(email_key,ip_key,requested_at) VALUES(?,?,?)",
                       (email_key, ip_key, current_time))
            user = db.execute("SELECT id,full_name,email FROM users WHERE email=? COLLATE NOCASE", (email,)).fetchone()
            if user:
                db.execute("DELETE FROM password_reset_tokens WHERE user_id=?", (user["id"],))
                raw_token = secrets.token_urlsafe(32)
                token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
                expires_at = current_time + PASSWORD_RESET_TTL_SECONDS
                db.execute("INSERT INTO password_reset_tokens(user_id,token_hash,created_at,expires_at) VALUES(?,?,?,?)",
                           (user["id"], token_hash, current_time, expires_at))
                link = f"{config['publicUrl']}/?reset_token={raw_token}"
                delivery = (user["email"], user["full_name"], link, language, config)
            db.commit()

        if delivery:
            threading.Thread(target=self.deliver_password_reset_email, args=delivery, daemon=True).start()
        self.send_json(200, generic)

    @staticmethod
    def deliver_password_reset_email(recipient: str, display_name: str, link: str, language: str, config: dict) -> None:
        try:
            send_password_reset_email(config, recipient, display_name, link, language)
        except (OSError, smtplib.SMTPException, ssl.SSLError) as exc:
            print(f"Could not deliver password reset email: {type(exc).__name__}")

    def reset_password_with_token(self) -> None:
        try:
            payload = self.read_json()
            token = str(payload.get("token", ""))
            new_password = str(payload.get("newPassword", ""))
            confirmation = str(payload.get("passwordConfirmation", ""))
            if not re.fullmatch(r"[A-Za-z0-9_-]{40,64}", token):
                raise ValueError("This password reset link is invalid or has expired")
            if len(new_password) < 12 or len(new_password) > 128:
                raise ValueError("New password must be 12–128 characters")
            if new_password != confirmation:
                raise ValueError("New passwords do not match")
        except (ValueError, TypeError) as exc:
            self.send_json(400, {"error": str(exc) or "Invalid reset request"})
            return

        current_time = int(datetime.now(timezone.utc).timestamp())
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        with DB_LOCK, connect() as db:
            db.execute("BEGIN IMMEDIATE")
            reset = db.execute("""SELECT t.id,t.user_id,u.full_name FROM password_reset_tokens t
                JOIN users u ON u.id=t.user_id
                WHERE t.token_hash=? AND t.used_at IS NULL AND t.expires_at>?""",
                               (token_hash, current_time)).fetchone()
            if not reset:
                db.rollback()
                self.send_json(400, {"error": "This password reset link is invalid or has expired"})
                return
            db.execute("UPDATE users SET password_hash=? WHERE id=?",
                       (password_hash(new_password), reset["user_id"]))
            db.execute("UPDATE password_reset_tokens SET used_at=? WHERE id=?", (current_time, reset["id"]))
            db.execute("DELETE FROM password_reset_tokens WHERE user_id=? AND id<>?", (reset["user_id"], reset["id"]))
            db.execute("DELETE FROM sessions WHERE user_id=?", (reset["user_id"],))
            insert_audit(db, reset["user_id"], "Password reset", "user", str(reset["user_id"]),
                         f"{reset['full_name']} reset the account password using an email link. All active sessions were signed out.")
            db.commit()
        self.send_json(200, {"ok": True})

    def change_password(self) -> None:
        try:
            payload = self.read_json()
            email = str(payload.get("email", "")).strip().lower()
            old_password = str(payload.get("oldPassword", ""))
            new_password = str(payload.get("newPassword", ""))
            confirmation = str(payload.get("passwordConfirmation", ""))
            if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email) or len(email) > 254:
                raise ValueError("Enter a valid email address")
            if not old_password or len(old_password) > 128:
                raise ValueError("Enter your old password")
            if len(new_password) < 12 or len(new_password) > 128:
                raise ValueError("New password must be 12–128 characters")
            if new_password != confirmation:
                raise ValueError("New passwords do not match")
            if new_password == old_password:
                raise ValueError("Choose a new password different from the old password")
        except (ValueError, TypeError) as exc:
            self.send_json(400, {"error": str(exc) or "Invalid password details"})
            return

        with DB_LOCK, connect() as db:
            db.execute("BEGIN IMMEDIATE")
            user = db.execute("SELECT id,full_name,password_hash FROM users WHERE email=? COLLATE NOCASE", (email,)).fetchone()
            if not user or not verify_password(old_password, user["password_hash"]):
                db.rollback()
                self.send_json(401, {"error": "Email or old password is incorrect"})
                return
            db.execute("UPDATE users SET password_hash=? WHERE id=?", (password_hash(new_password), user["id"]))
            db.execute("DELETE FROM sessions WHERE user_id=?", (user["id"],))
            insert_audit(db, user["id"], "Password changed", "user", str(user["id"]),
                         f"{user['full_name']} changed the account password. All active sessions were signed out.")
            db.commit()
        self.send_json(200, {"ok": True, "message": "Password changed. Please sign in with your new password."})

    def auth_post(self, path: str) -> None:
        try:
            payload = self.read_json()
            email = str(payload.get("email", "")).strip().lower()
            password = str(payload.get("password", ""))
            phone = normalize_phone(payload.get("phone")) if path == "/api/register" else None
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

        with DB_LOCK, connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if path == "/api/register":
                if email == "aruzhan@example.test":
                    db.rollback()
                    self.send_json(409, {"error": "This email address is reserved for the audit-log account."})
                    return
                if db.execute("SELECT 1 FROM users WHERE email=? COLLATE NOCASE", (email,)).fetchone():
                    db.rollback()
                    self.send_json(409, {"error": "An account with this email already exists"})
                    return
                if phone and db.execute("SELECT 1 FROM users WHERE phone_number=?", (phone,)).fetchone():
                    db.rollback()
                    self.send_json(409, {"error": "This phone number is already linked to another account"})
                    return
                cursor = db.execute("INSERT INTO users(full_name,email,phone_number,password_hash,role,created_at) VALUES(?,?,?,?,?,?)",
                                    (name, email, phone, password_hash(password), "customer", now()))
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
            user = db.execute("SELECT full_name,email,phone_number,role FROM users WHERE id=?", (user_id,)).fetchone()
            action = "Account registered" if path == "/api/register" else "User signed in"
            insert_audit(db, user_id, action, "user", str(user_id), f"{user['full_name']} {action.lower()}.")
            db.commit()
        cookie = f"bank_session={sid}; HttpOnly; SameSite=Strict; Path=/; Max-Age={SESSION_SECONDS}"
        self.send_json(200, {"authenticated": True, "csrfToken": csrf,
                             "user": {"name": user["full_name"], "email": user["email"], "phone": user["phone_number"], "role": user["role"]}}, [("Set-Cookie", cookie)])


if __name__ == "__main__":
    if MIN_TRANSFER_CENTS <= 0 or MAX_TRANSFER_CENTS < MIN_TRANSFER_CENTS or DAILY_LIMIT_CENTS < MAX_TRANSFER_CENTS or TRANSFER_DELAY_SECONDS < 1:
        raise SystemExit("Transfer limit settings are invalid; require 0 < minimum <= maximum <= daily limit")
    init_db()
    schedule_pending()
    print(f"Internet Banking Demo running at http://{HOST}:{PORT}")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
