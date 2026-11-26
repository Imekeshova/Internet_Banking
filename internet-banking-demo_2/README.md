# Internet Banking Demo

A local educational banking demo with customer registration and sign-in, account ownership checks, internal and scheduled transfers, transfer detail views, readable audit events, automated system-check tests, and printable digital receipts. The database and sample accounts remain local to this folder. No real money is moved.

## Team Members & Responsibilities

| Student | Student ID | Role | Key Contribution |
| :--- | :--- | :--- | :--- |
| **Imekeshova Malika** | `24B031819` | Systems Analysis & Requirements | Transfer limits and scheduling requirements, `request_id`/status mapping, RU/KK/EN localization, weekly/monthly transfer statistics and acceptance criteria |
| **Akhmettayev Alimansur** | `24B030251` | Data & Database | SQLite schema, integer tiyn precision (`1 KZT = 100 tiyn`), `CHECK`/`UNIQUE` constraints & data governance |
| **Sultanbekuly Yelnur** | `24B032037` | Application & Development | Server implementation (`server.py`), scheduled transfers, audit log, UI (`index.html`), and `PRAGMA foreign_keys=ON` |
| **Yernazarov Madiyar** | `24B031756` | Architecture & Security | ERD normalization, network firewall & USB control plan, session/CSRF & access isolation |
| **Kaldybay Khanshaiym** | `24B032136` | Testing, Analytics & Integration | Automated test suite (`tests/test_bank.py`), data freshness audit (E5), ledger reconciliation & terminal TCO analysis |

## Project Structure

```text
internet-banking-demo_2/
├── tests/
│   ├── __init__.py       # Test package marker
│   ├── test_bank.py      # Core, security, audit & reconciliation tests
│   ├── test_analytics.py # Period boundaries, aggregation, privacy & migration
│   ├── test_limits.py    # Invalid inputs and UTC+5 daily-limit boundaries
│   ├── test_scheduled.py # Scheduled transfer lifecycle and recovery
│   ├── test_ui.cjs       # Localization and form behavior checks
│   ├── test_analytics_ui.cjs # Async statistics and keyboard-focus regressions
│   └── test_date_picker.cjs # Date parsing, leap years and timezone independence
├── analytics.js          # Statistics rendering and API refresh
├── analytics.css         # Statistics cards and responsive layout
├── date-picker.js        # Localized date entry and keyboard-accessible calendar
├── date-picker.css       # Calendar and date-field layout
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

## Languages and Transfer Statistics

After sign-in, the top navigation separates **Overview** (balance, transfer form and history) from **Statistics** (weekly/monthly totals and monthly highlights). Switching tabs preserves entered form values and filters. Both tabs are translated into EN/RU/KK and support arrow keys, Home and End. Statistics load when opened, refresh while visible and retain the last valid result between tabs; signing out clears the data and resets navigation to Overview. Transfer details and personal information remain accessible from Statistics.

The EN / RU / KK buttons switch languages in one click before and after sign-in. English (`en`), Russian (`ru`), and Kazakh (`kk`) cover the main forms, transfer statuses, errors, history, details and receipts. The choice is saved in the current browser. Names and notes entered by users are kept as written. Currency remains KZT; displayed operation times and scheduling use UTC+5. Kazakh month names have an explicit fallback for browsers with incomplete locale data.

Scheduled transfers and history filters use the application's own localized date controls, so the browser's language cannot supply a conflicting date placeholder or calendar. English shows `DD/MM/YYYY` (with `HH:mm` for scheduling); Russian and Kazakh show day-month-year separated by dots, with localized placeholder letters, month/day names and actions. Dates can be typed or selected from the calendar. Changing language preserves the chosen day and time. Internally, filters compare ISO dates and scheduling sends `YYYY-MM-DDTHH:mm`, still interpreted as UTC+5. Impossible dates and incomplete times are rejected; the calendar supports arrow keys, Escape, Apply and Clear.

The dashboard shows two separate totals across all accounts owned by the signed-in customer:

* **This week:** successful outgoing amount and count since Monday 00:00, UTC+5.
* **This month:** successful outgoing amount and count since the first day of the month, 00:00, UTC+5.

Monthly highlights show the **largest individual transfer**, **smallest individual transfer** (amount, recipient and time), and **most frequent recipients by number of transfers**. If recipients share the highest count, all are shown. Transfers to different accounts of the same recipient are grouped together. Equal largest/smallest amounts choose the earliest completed transfer, then reference, for a stable result.

Only outgoing `Completed` ledger entries count. Incoming, `Pending`, `Failed`, `Scheduled` and `Cancelled` entries do not. A scheduled transfer enters the statistics once, after successful execution. Empty totals are zero; missing highlights show a translated empty state. The initial successful demo transfer is included when it falls in the period.

Statistics come from authenticated `GET /api/analytics`, over the complete ledger, independently of the history panel's latest-50 limit and its filters. The response contains `currency`, `timeZone`, `asOf`, `week` and `month`. Both periods contain inclusive `start`, exclusive `end`, `totalAmount` (KZT) and `count`. `month` also contains `largest`, `smallest` and `frequentRecipients`. No completion after `asOf` is counted.

### Completion Dates and Existing Data

New successful transfers save an immutable `completed_at` in the same transaction as the balance changes. Viewing a finished transfer does not change its recorded decision time. The database upgrade preserves balances and history.

For older completed records, migration uses an available successful audit event or the matching completed schedule's timestamp; otherwise it uses the original creation time. Older versions overwrote `checked_at` when details were opened, so it cannot reliably recover historical completion time. The creation-time fallback can affect classification of an old transfer near a week/month boundary. New transfers have an exact completion time.

### Requirements and Acceptance Criteria

| ID | Rule | How to accept it |
| --- | --- | --- |
| LANG-01 | Three languages on the same application | Switch EN → RU → KK on sign-in and in the dashboard; labels, statuses, errors and receipts use the chosen language. |
| LANG-02 | Language persists without losing form data | Switch with a partially filled form; values remain. Refresh; the selected language remains. |
| LANG-03 | Dates stay readable and use UTC+5 in every language | Check operation dates, the statistics month and scheduled audit details in EN/RU/KK, including midnight and year rollover; Kazakh months must not appear as `M11`. |
| LANG-04 | Form validation uses the selected interface language | In a Russian-language browser, select KK and enter an amount below/above the limits, an invalid decimal, an empty required field, invalid email, short password or invalid scheduled time. Native validation messages must use KK before submission and after further editing. Repeat in RU/EN, switch languages with an invalid value, then correct it and confirm the form is usable. Changes to server limits and recipient options must refresh validity too. |
| LANG-05 | Date entry and calendars follow the interface language | With a Russian-language browser, EN displays `DD/MM/YYYY HH:mm` and English month/day/action labels in scheduling and both history filters. Switch to RU/KK without changing the chosen instant. Test a leap day, an impossible date, keyboard selection, Escape/Apply/Clear, and ISO submission with UTC+5. |
| LIM-01 | Amount boundaries and daily allowance are enforced by the server | With default settings, reject below 100 KZT and above 1,000,000 KZT; include Pending and Completed requests created in the current UTC+5 day toward 2,000,000 KZT across the customer's accounts. Failed requests do not consume allowance. |
| LIM-02 | Invalid inputs never select an unintended account or crash a handler | Both transfer endpoints return 400 for fractional/boolean/out-of-range account IDs and oversized/nonfinite amounts, without creating a transfer or timer. Integer IDs and integer strings remain accepted. |
| STAT-01 | Week and month are separate calendar periods in UTC+5 | A successful transfer before Monday but within this month increases only the monthly total. Test Sunday/Monday, month and year boundaries. |
| STAT-02 | Only successful outgoing transfers count | Compare totals with the ledger including incoming, pending, failed and cancelled examples; none of those increases the totals. |
| STAT-03 | Correct sums and counts | Two successful transfers of 1,000 and 2,000 KZT in both periods give 3,000 KZT and count 2 in each. |
| STAT-04 | Monthly extrema are individual transfers | In that example, smallest is 1,000 KZT and largest is 2,000 KZT, with their recipient and date. |
| STAT-05 | Frequency means number, not money | Three transfers to A beat two transfers to B even if B received more money. Equal highest counts show all tied recipients. |
| STAT-06 | Statistics are complete and private | More than 50 matching transfers all count; a second customer cannot see the first customer's totals. |
| STAT-07 | Scheduled success counts once | Create a schedule: no change. Execute: amount/count increase once. Execute the worker again: no extra increase. |
| STAT-08 | Reading does not rewrite completion time | Open a completed transfer repeatedly; its completion time and statistics stay unchanged. |
| STAT-09 | Refresh failures and session changes preserve correctness | A network error retains the last successful figures with a warning; sign-out clears them, and a late response from the previous session cannot restore them. |
| SCH-01 | Scheduling reserves neither balance nor daily allowance | After creation, balances and used daily limit remain unchanged. |
| SCH-02 | Execution rechecks current conditions | Reduce funds or exhaust the daily limit after scheduling; execution fails with a reason and no balance changes. |
| SCH-03 | Only the owner can cancel a Scheduled entry | Owner cancellation succeeds before processing; foreign and already-final requests cannot cancel it. |
| SCH-04 | Restart resumes persisted work | Pending transfers and Scheduled entries are rescheduled; terminal entries are not sent again. |
| SCH-05 | Scheduling respects its exact time boundaries | Reject less than 30 seconds or more than 365 days ahead. Round the form's earliest selectable minute upward and preserve fractional seconds submitted through the API. Invalid timezone/date overflow returns 400. |
| UI-01 | Layout works on desktop and phone | Cards, forms and actions remain readable at desktop and 390px phone width in all languages. |
| UI-02 | Statistics refresh preserves keyboard position | Focus the smallest-transfer detail button when smallest and largest are the same transfer; refreshing or changing language keeps focus on that same button. |
| UI-03 | Background polling keeps unchanged actions usable | Focus Cancel in a scheduled-transfer notice or a history row; polling keeps the existing button. A translated notice restores focus to the same action. |
| UI-04 | Top navigation separates Overview and Statistics without losing work | Fill a transfer and history filter, open Statistics, change language and return. Values remain; one tab/panel is selected. Verify keyboard navigation, details, refresh after a successful transfer, and fresh Overview after another user signs in. |

### Field Mapping: Form → API → Database

| Meaning / form | API JSON | Database | Rule |
| --- | --- | --- | --- |
| Transfer reference | `requestId` | `request_id` | New requests use `TR-` + 32 uppercase hexadecimal characters. The same reference links a completed schedule to its ledger entry. |
| Selected sender account | `fromAccountId` | `sender_account_id` | Positive integer ID in SQLite's signed 64-bit range; must belong to the signed-in user. No boolean or fractional coercion. |
| Recipient account | `toAccountId` | `recipient_account_id` | Same ID validation; must exist and differ from the sender account. |
| Amount entered in KZT | `amount` | `amount_cents` | Stored as integer tiyn: 100.50 KZT → 10050. No currency exchange. |
| Ordinary transfer description | `description` | `transfers.description` | Optional, up to 200 characters. |
| Scheduled reminder note | POST `reminderNote`; response `description` | `scheduled_transfers.reminder_note` | Copied to the successful ledger entry's description; it is a note, not a notification. |
| Scheduled date/time | `scheduledFor` | `scheduled_for` | Input without an offset means UTC+5; stored timestamps include a timezone. Minimum 30 seconds ahead, maximum 365 days. |
| Created | `createdAt` | `created_at` | Creation of the record; for a successful schedule's ledger entry, its execution time. |
| Last checked/decision | `checkedAt` | `checked_at`, or schedule `updated_at` | Pending lookups may refresh it; terminal lookups preserve it. |
| Successful completion | `completedAt` | `transfers.completed_at` | Immutable completion timestamp used for statistics; null until successful. Historical fallback described above. |
| Operation state | `status` | `status` | Canonical English values remain unchanged in API/DB; only displayed labels are translated. |
| Failure explanation | `failureReason` | `failure_reason` | Displayed in the selected language for known system reasons; no balance change on failure. |

Ordinary status flow: `Pending → Completed / Failed`. Schedule flow: `Scheduled → Completed / Failed / Cancelled`.

Ordinary retries with the same reference and identical fields return the existing transfer (`200`); changed fields return `409`. Scheduled creation currently rejects any reused reference (`409`), including an identical payload. Retrying a worker never moves money twice. These are intentionally documented as different API contracts.

### Implementation Decisions and Assumptions

* Keep the existing Python/SQLite and single-page client for a small local coursework demo; no new service or framework is required. Translation dictionaries stay beside the interface for the team to maintain.
* Use server-side aggregation instead of adding up visible history rows, so the latest-50 display limit cannot truncate statistics. Integer-tiyn aggregation preserves monetary precision.
* Use calendar weeks/months in UTC+5 rather than rolling 7/30-day windows, matching the agreed separate week/month totals. Highlights remain monthly.
* Rank recipients by count rather than total amount, matching the revised requirement. Keep every tied leader rather than arbitrarily hiding one.
* Refresh statistics with account activity. Keep the last valid result on a temporary network failure and show a translated refresh error; never substitute an error with a zero total. User data is cleared on sign-out.
* Preserve existing balances/history and run automated checks only against temporary databases. New database fields are added through repeatable migration.

## Automated Testing

The project includes an automated test suite that runs against an isolated temporary SQLite database without touching `banking.sqlite3`.

### Run Tests

From `internet-banking-demo_2`:

```powershell
python -m unittest discover -v
```

With Node.js installed, run the dependency-free JavaScript checks for translation coverage, form behavior, async statistics, keyboard focus and syntax:

```powershell
node --test tests/test_ui.cjs tests/test_analytics_ui.cjs tests/test_date_picker.cjs
```

### Covered Verification Areas

* **Transfer Lifecycle & Atomicity (`test_01` – `test_04`):** `Pending` creation, atomic debit/credit to `Completed`, rollback on insufficient funds (`Failed`), and self-transfer rejection.
* **Idempotency & Request Reference Control (`test_05` – `test_08`):** Strict `TR-<32 hex>` format validation, duplicate retry protection (`200 OK` without double debit), and payload mismatch rejection (`409 Conflict`).
* **Security, Sessions & Access Isolation (`test_09` – `test_12`, `test_24`):** Salted `PBKDF2-HMAC-SHA256` password hashing, unauthenticated rejection (`401`), CSRF enforcement (`403`), IDOR protection across user accounts, and access checks on the actual audit-log endpoint.
* **Limits & Data-Quality Audit (`test_13` – `test_16`):** Min/max boundary checks (`100` – `1,000,000 KZT`), daily `UTC+5` limit calculation, invalid amount rejection (`E3 Validity`), and 5-minute stale `Pending` detection (`E5 Timeliness`).
* **Database Integrity & Reconciliation (`test_17` – `test_23`):** Foreign key enforcement, `CHECK` constraints, pending recovery after restart, ledger balance conservation, `/api/next-id` format verification, and recipient privacy masking.
* **Analytics (`test_analytics.py`):** calendar boundaries, exact totals and extrema, recipient frequency/ties, user isolation, history beyond 50 entries, immutable completion time and non-destructive migration.
* **Scheduled transfers (`test_scheduled.py`):** deferred debit, cancellation, execution-time checks, retry safety and restart recovery.
* **Input boundaries (`test_limits.py`):** strict account IDs, oversized/nonfinite amounts, no unintended records/timers on rejection and inclusive/exclusive UTC+5 day boundaries.
* **UI behavior (`test_ui.cjs`, `test_analytics_ui.cjs`):** preserved form values and canonical statuses, language-aware dates/validation, hidden-field constraints, safe rendering, stale-response isolation and statistics refresh recovery. These tests execute application JavaScript with lightweight DOM adapters; browser layout is checked separately.
* **Date controls (`test_date_picker.cjs`):** strict localized/ISO parsing, Gregorian leap years, minute precision, language roundtrips and results independent of four operating-system timezones. UI integration additionally checks ISO filtering/submission values and scheduling bounds.

Backend verification on 25 November 2026: **64 Python tests passed**. The subsequent frontend verification passed **31 JavaScript checks**, including validation before submission, repeated edits, language changes and dynamic constraints. Browser checks covered EN/RU/KK switching and persistence, preserved form input, localized Kazakh dates, desktop/390px/320px layout widths, successful transfers, safe retries, failed transfers, scheduled cancellation and keyboard focus during polling. Two successful transfers of 100.33 and 200.67 KZT added exactly 301 KZT to the seeded 10,000 KZT total; the failed 150,000 KZT transfer and cancelled 500 KZT schedules changed neither statistics nor daily usage. The validation follow-up reproduced Russian native tooltips under KK, verified translated amount/email/password/date messages in the browser, and confirmed that correcting an amount allows a transfer again. Browser checks used a separate temporary demo database, not the user's saved balances.

Date-control follow-up on 26 November 2026: **44 JavaScript checks and the public-asset allowlist/MIME test passed**. Browser checks verified localized placeholders/months/weekdays/actions, selection and preservation across EN/RU/KK, Escape and arrow keys, history filtering/clearing, and calendar fit at 1280px, 390px and 320px. A temporary-database schedule entered as `27.11.2026 14:35` sent `2026-11-27T14:35` and was stored as `2026-11-27T09:35:00+00:00`, then cancelled.

## Security, Privacy & Ethics Compliance

| Dimension | Implemented Control | Verification / Evidence |
| --- | --- | --- |
| **Threats (Credentials & Sessions)** | Salted `PBKDF2-HMAC-SHA256` password hashing, `HttpOnly; SameSite=Strict` cookies, and mandatory `X-CSRF-Token` validation. | `test_09`, `test_10`, `test_11` |
| **Threats (Access Control / IDOR)** | Server-side ownership checks on accounts, transfers, and restricted access to the system **Audit Log**. | `test_12`, `test_24` |
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
7. Switch EN → RU → KK and refresh to show that the language is remembered.
8. Compare the week/month totals and monthly largest/smallest/frequent-recipient cards with successful outgoing transfers. Remember the seeded 10,000 KZT transfer.
9. Run `python -m unittest discover -v` and show the results.
10. Present the terminal plan (PCI-PTS PIN pad, USB control) and the TCO analysis.

## Demo Scope

The service binds to `127.0.0.1` by default and is intended for local coursework demonstrations. It does not connect to a real bank, send email verification or password recovery messages, or implement bank-grade deployment, account recovery, or monitoring. Do not expose this demo to the public internet.
