# Internet Banking Demo 

A local educational banking demo with customer registration and sign-in, account ownership checks, internal and scheduled transfers, transfer detail views, readable audit events, automated system-check tests, and printable digital receipts. The database and sample accounts remain local to this folder. No real money is moved.

## Team Members & Responsibilities 

| Student | Student ID | Role | Key Contribution |
| :--- | :--- | :--- | :--- |
| **Imekeshova Malika** | `24B031819` | Systems Analysis & Requirements | End-to-end transfer requirements, `request_id` retry design, status consistency rules (E4) |
| **Akhmettayev Alimansur** | `24B030251` | Data & Database | SQLite schema, integer tiyn precision (`1 KZT = 100 tiyn`), `CHECK`/`UNIQUE` constraints & data governance |
| **Sultanbekuly Yelnur** | `24B032037` | Application & Development | Server implementation (`server.py`), scheduled transfers, audit log, UI (`index.html`), and `PRAGMA foreign_keys=ON` |
| **Yernazarov Madiyar** | `24B031756` | Architecture & Security | ERD normalization, network firewall & USB control plan, session/CSRF & access isolation |
| **Kaldybay Khanshaiym** | `24B032136` | Testing, Analytics & Integration | Automated test suite (`tests/test_bank.py`), data freshness audit (E5), ledger reconciliation & terminal TCO analysis |

## Project Structure

```text
internet-banking-demo_2/
├── tests/
│   ├── __init__.py       # Test package marker
│   └── test_bank.py      # Automated unit, security, audit & reconciliation tests
├── index.html            # Single-page web client interface
├── server.py             # Python HTTP server, SQLite database & background worker
├── start.bat             # One-click Windows launcher
└── README.md             # Project documentation
```

## Run on Windows

1. Install **Python 3.10** or newer.
2. Open PowerShell in `internet-banking-demo_2` and run:

```powershell
python server.py
```

3. Open `http://127.0.0.1:8000` in your browser.
4. Stop the server with `Ctrl+C`.

You can also double-click `start.bat`. The app creates or upgrades `banking.sqlite3` at startup and preserves existing demo balances and transfer history. To reset the entire demo, stop the server and remove `banking.sqlite3`. Set `BANK_PORT` to choose another local port or `BANK_DB` to use a different database path.

## Demo Sign-Ins

All three original sample accounts use the same demo password: **`DemoPass!23`**.

| Customer | Email | Starting Balance |
| --- | --- | --- |
| **Aruzhan S.** | `aruzhan@example.test` | ₸100,000.00 |
| **Dias T.** | `dias@example.test` | ₸25,000.00 |
| **Amina K.** | `amina@example.test` | ₸75,000.00 |

You can also create a new local account from the sign-in screen. New accounts start with a zero balance. Use the sample accounts to send a demo transfer to a newly registered account.

On a fresh database, the completed example transfer moves ₸10,000 from Aruzhan to Dias, so their visible balances become ₸90,000 and ₸35,000. The failed ₸150,000 example does not change either balance.

## Sign-In and Session Behavior

* Passwords are stored as `PBKDF2-HMAC-SHA256` hashes with a per-password random salt; plaintext passwords are not stored.
* Successful sign-in creates an eight-hour server-side session. The session ID is held in an `HttpOnly`, `SameSite=Strict` cookie.
* Mutating API calls require a session CSRF token (`X-CSRF-Token`). The server checks account ownership for reads and transfers.
* Customers can see and send from their own accounts. Recipient choices show only masked account identifiers belonging to other users.

## Transfer Limits and Statuses

Defaults can be changed with environment variables before launching the server:

| Setting | Default | Meaning |
| --- | --- | --- |
| `BANK_MIN_TRANSFER_KZT` | `100` | Minimum amount per transfer |
| `BANK_MAX_TRANSFER_KZT` | `1,000,000` | Maximum amount per transfer |
| `BANK_DAILY_LIMIT_KZT` | `2,000,000` | Maximum outgoing amount per customer per Kazakhstan local day (`UTC+5`) |
| `BANK_PENDING_SECONDS` | `3` | How long a successful request remains `Pending` before processing |

* The daily total includes `Pending` and `Completed` outgoing transfers across the customer’s accounts; `Failed` transfers do not count. The app shows the limit, amount used today, and remaining amount. Amount and limit checks are enforced by the server, not only by browser controls.
* Each transfer stores a unique reference and an optional description of up to 200 characters. A submitted transfer is first saved as `Pending`. A background worker then checks funds and atomically updates both balances, setting the status to `Completed`; if funds are insufficient, it sets `Failed` without moving money.
* A retry with the same reference and identical details returns the existing transfer. Reusing the reference with changed details is rejected (`409 Conflict`). `Pending` records are resumed after a server restart.
* Transfer references in **Recent Activity** are clickable. Selecting one opens transfer details in a pop-up dialog with the description, amount, recipient, reference, latest status, last checked time, and any failure reason or scheduled time. A fresh database is seeded with one completed ₸10,000 example and one failed ₸150,000 insufficient-funds example so both detail states can be demonstrated. Existing databases are not given extra sample transfers.
* Choose **Schedule for later** to set a future date and time in `UTC+5` and add a reminder note such as *“Send money to Aigul for utility bills.”* Scheduled transfers appear in **Recent Activity** and can be cancelled while their status is `Scheduled`. At the scheduled time, the server checks the current balance, amount limits, and that day’s remaining limit. It executes the transfer only if all checks pass; otherwise, it marks the schedule `Failed` and records the reason. Scheduled entries resume after a server restart.
* The **Audit Log** button appears only when signed in as `aruzhan@example.test`. Clicking it opens the log; clicking it again hides the log. The server also rejects audit-log requests from every other account. The log lists readable records for sign-in, sign-out, transfer submission, retries, transfer completion or failure, scheduled-transfer creation or cancellation, transfer detail views, and rejected transfer-limit attempts.

## Automated Testing (`tests/test_bank.py`)

The project includes an automated test suite that runs against an isolated temporary SQLite database without touching `banking.sqlite3`.

### Run Tests

From `internet-banking-demo_2`:

```powershell
python -m unittest tests.test_bank -v
```

### Covered Verification Areas

* **Transfer Lifecycle & Atomicity (`test_01` – `test_04`):** `Pending` creation, atomic debit/credit to `Completed`, rollback on insufficient funds (`Failed`), and self-transfer rejection.
* **Idempotency & Request Reference Control (`test_05` – `test_08`):** Strict `TR-<32 hex>` format validation, duplicate retry protection (`200 OK` without double debit), and payload mismatch rejection (`409 Conflict`).
* **Security, Sessions & Access Isolation (`test_09` – `test_12`):** Salted `PBKDF2-HMAC-SHA256` password hashing, unauthenticated rejection (`401`), CSRF enforcement (`403`), and IDOR protection across user accounts and audit logs.
* **Limits & Data-Quality Audit (`test_13` – `test_16`):** Min/max boundary checks (`100` – `1,000,000 KZT`), daily `UTC+5` limit calculation, invalid amount rejection (`E3 Validity`), and 5-minute stale `Pending` detection (`E5 Timeliness`).
* **Database Integrity & Reconciliation (`test_17` – `test_23`):** Foreign key enforcement, `CHECK` constraints, pending recovery after restart, ledger balance conservation, `/api/next-id` format verification, and recipient privacy masking.

## Security, Privacy & Ethics Compliance 

| Dimension | Implemented Control | Verification / Evidence |
| --- | --- | --- |
| **Threats (Credentials & Sessions)** | Salted `PBKDF2-HMAC-SHA256` password hashing, `HttpOnly; SameSite=Strict` cookies, and mandatory `X-CSRF-Token` validation. | `test_09`, `test_10`, `test_11` |
| **Threats (Access Control / IDOR)** | Server-side ownership checks on accounts, transfers, and restricted access to the system **Audit Log**. | `test_12` |
| **Threats (Physical Terminal)** | Branch terminal architecture specifies a `PCI-PTS` encrypted PIN pad and strict USB device whitelisting. | Hardware & Security Plan (Lab 5–6) |
| **Privacy (Data Minimization)** | Recipient lists display only masked account numbers (`KZ•• 1042`) without exposing full account details or balances. | `GET /api/recipients`, `test_23` |
| **Ethics (Financial Accuracy)** | All amounts are stored and processed in integer tiyns (`1 KZT = 100 tiyn`) with `CHECK(balance_cents >= 0)` to prevent rounding or overdraft errors. | `test_15`, `test_17`, `test_20` |
| **Ethics (Transparency & Fault Resilience)** | Idempotent `request_id` prevents accidental double charges; stale `Pending` records (`> 5 min`) are flagged under Rule E5; digital receipts remain printable/exportable. | `test_06`, `test_16` |

## Emerging Technologies in IS 

The demo is intentionally a small, local system. The table below shows how current emerging technologies relate to it. Items marked **Implemented** exist in this repository; items marked **Proposed** are design ideas for a production version and are not part of the code.

| Technology | Relevance to Internet Banking | Status in this project |
| --- | --- | --- |
| **Biometric authentication** | Fingerprint verification as a second factor at the branch terminal, with PIN as fallback | Proposed (terminal plan, Lab 5–6) |
| **Encrypted PIN entry (PCI-PTS)** | Protects PIN capture at physical terminals | Proposed (terminal architecture) |
| **Idempotent APIs / event-style processing** | Safe retries and no double charges via `request_id` and a background worker | **Implemented** (`server.py`, `test_05` – `test_08`) |
| **Automated data-quality monitoring** | Detects stale `Pending` transfers (E5) and balance drift via ledger reconciliation | **Implemented** (`test_16`, `test_20`) |
| **AI-assisted fraud detection** | Flag unusual amounts, frequency or recipients using the audit log as input | Proposed |
| **Open banking APIs** | Controlled third-party access to account data with customer consent | Proposed |

## Business/IT Strategy: Planning & Implementation 

Suggested rollout path if this demo were evolved into a real service:

| Phase | Goal | Main Activities | Success Check |
| --- | --- | --- | --- |
| **1. Pilot (current)** | Prove core transfer logic | Local demo, automated tests, ledger reconciliation | All tests pass; balances match ledger |
| **2. Hardening** | Production-grade security and data | Move to a managed database, HTTPS, rate limiting, monitoring, independent security review | No critical findings; audit trail complete |
| **3. Branch rollout** | Add physical channel | Terminals with PCI-PTS PIN pad, USB whitelisting, receipt printer with digital-receipt fallback | Terminal TCO within approved budget (see TCO analysis by Kaldybay K.) |
| **4. Scale & extend** | New services | Open-banking APIs, fraud analytics, support tooling | Measured reduction in failed/duplicate transfers and support tickets |

**Key risks and mitigations**

| Risk | Mitigation |
| --- | --- |
| Double charge on network retry | Unique `request_id`; identical retries return the existing transfer |
| Balance inconsistency | Atomic updates, `CHECK` constraints, ledger reconciliation, E4 consistency rules |
| Stale or stuck transfers | E5 freshness rule (`Pending` > 5 min flagged); pending recovery after restart |
| Unauthorized access | Server-side ownership checks, CSRF tokens, restricted audit log |
| Hardware failure (printer out of paper) | Plan B: digital/printable receipt |

## Responsible Use of AI

AI tools were used as an assistant for drafting code and documentation. All generated code was reviewed, run and tested by the team, and the team is responsible for the final result. The demo itself contains no AI component and handles no real customer data.

## Final Defense Demo Script (suggested order)

1. Start the server (`python server.py`) and sign in as `aruzhan@example.test`.
2. Send a transfer and show `Pending` → `Completed`; open the transfer details dialog.
3. Retry with the same reference to show no double debit; change the details to show `409 Conflict`.
4. Send an amount above the balance to show `Failed` without balance change.
5. Schedule a transfer for later and cancel it.
6. Open the **Audit Log** (Aruzhan only) and show that other accounts are rejected.
7. Run `python -m unittest tests.test_bank -v` and show all tests passing.
8. Present the terminal plan (PCI-PTS PIN pad, USB control) and the TCO analysis.

## Demo Scope

The service binds to `127.0.0.1` by default and is intended for local coursework demonstrations. It does not connect to a real bank, send email verification or password recovery messages, or implement bank-grade deployment, account recovery, or monitoring. Do not expose this demo to the public internet.