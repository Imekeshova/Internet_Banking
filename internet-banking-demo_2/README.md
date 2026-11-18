# Internet Banking Demo

A local educational banking demo with customer registration and sign-in, account ownership checks, internal and scheduled transfers, transfer detail views, readable audit events, and printable digital receipts. The database and sample accounts remain local to this folder. No real money is moved.

## Run on Windows

1. Install Python 3.10 or newer.
2. Open PowerShell in this folder and run:

   ```powershell
   python server.py
   ```

3. Open [http://127.0.0.1:8000](http://127.0.0.1:8000).
4. Stop the server with **Ctrl+C**.

You can also double-click `start.bat`. The app creates or upgrades `banking.sqlite3` at startup and preserves existing demo balances and transfer history. To reset the entire demo, stop the server and remove `banking.sqlite3`. Set `BANK_PORT` to choose another local port or `BANK_DB` to use a different database path.

## Demo sign-ins

All three original sample accounts use the same demo password, `DemoPass!23`.

| Customer | Email | Starting balance |
| --- | --- | ---: |
| Aruzhan S. | `aruzhan@example.test` | ₸100,000.00 |
| Dias T. | `dias@example.test` | ₸25,000.00 |
| Amina K. | `amina@example.test` | ₸75,000.00 |

You can also create a new local account from the sign-in screen. New accounts start with a zero balance. Use the sample accounts to send a demo transfer to a newly registered account.

On a fresh database, the completed example transfer moves ₸10,000 from Aruzhan to Dias, so their visible balances become ₸90,000 and ₸35,000. The failed ₸150,000 example does not change either balance.

## Sign-in and session behavior

- Passwords are stored as PBKDF2-HMAC-SHA256 hashes with a per-password random salt; plaintext passwords are not stored.
- Successful sign-in creates an eight-hour server-side session. The session ID is held in an HttpOnly, SameSite=Strict cookie.
- Mutating API calls require a session CSRF token. The server checks account ownership for reads and transfers.
- Customers can see and send from their own accounts. Recipient choices show only masked account identifiers belonging to other users.

## Transfer limits and statuses

Defaults can be changed with environment variables before launching the server:

| Setting | Default | Meaning |
| --- | ---: | --- |
| `BANK_MIN_TRANSFER_KZT` | 100 | Minimum amount per transfer |
| `BANK_MAX_TRANSFER_KZT` | 1,000,000 | Maximum amount per transfer |
| `BANK_DAILY_LIMIT_KZT` | 2,000,000 | Maximum outgoing amount per customer per Kazakhstan local day (UTC+5) |
| `BANK_PENDING_SECONDS` | 3 | How long a successful request remains Pending before processing |

The daily total includes Pending and Completed outgoing transfers across the customer’s accounts; Failed transfers do not count. The app shows the limit, amount used today, and remaining amount. Amount and limit checks are enforced by the server, not only by browser controls.

Each transfer stores a unique reference and an optional description of up to 200 characters. A submitted transfer is first saved as Pending. A background worker then checks funds and atomically updates both balances, setting the status to Completed; if funds are insufficient, it sets Failed without moving money. A retry with the same reference and identical details returns the existing transfer. Reusing the reference with changed details is rejected. Pending records are resumed after a server restart.

Transfer references in Recent Activity are clickable. Selecting one opens transfer details in a pop-up dialog with the description, amount, recipient, reference, latest status, last checked time, and any failure reason or scheduled time. A fresh database is seeded with one completed ₸10,000 example and one failed ₸150,000 insufficient-funds example so both detail states can be demonstrated. Existing databases are not given extra sample transfers.

Choose **Schedule for later** to set a future date and time in UTC+5 and add a reminder note such as “Send money to Aigul for utility bills.” Scheduled transfers appear in Recent Activity and can be cancelled while their status is Scheduled. At the scheduled time, the server checks the current balance, amount limits, and that day’s remaining limit. It executes the transfer only if all checks pass; otherwise, it marks the schedule Failed and records the reason. Scheduled entries resume after a server restart.

The Audit Log button appears only when signed in as `aruzhan@example.test`. Clicking it opens the log; clicking it again hides the log. The server also rejects audit-log requests from every other account. The log lists readable records for sign-in, sign-out, transfer submission, retries, transfer completion or failure, scheduled-transfer creation or cancellation, transfer detail views, and rejected transfer-limit attempts.

## Demo scope

The service binds to `127.0.0.1` by default and is intended for local coursework demonstrations. It does not connect to a real bank, send email verification or password recovery messages, or implement bank-grade deployment, account recovery, or monitoring. Do not expose this demo to the public internet.
