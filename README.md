# GTL Control Room

Flask backend for the GTL Fleet Intelligence dashboard, replacing the
local EXE workflow with a scheduled email import, role-based access
(admin / technician / client), and a live web dashboard.

## Quick start, local testing

```
pip install -r requirements.txt
python app.py
```

Open http://127.0.0.1:5000. Which accounts actually work depends on
whether Sheets is configured (see `users.py`'s `load_users()`):

- **`GOOGLE_SERVICE_ACCOUNT_JSON` / `FEEDBACK_SHEET_ID` NOT set** (a
  genuinely fresh clone, nothing configured yet): the demo accounts
  below, from `database/users.json`, are what you get.

  | Username     | Password       | Role       |
  |--------------|----------------|------------|
  | Alex       | TestPass123!   | admin      |
  | brandon.b    | TestPass123!   | technician |
  | gtl-client   | TestPass123!   | client     |

  **Change these before going live**, see `users.py`.

- **Sheets configured** (true for every real deployment, and for local
  dev once you've set those two env vars): accounts live in the
  spreadsheet's "Users" tab instead, and `database/users.json` is
  ignored entirely - the table above will NOT work, real accounts do,
  and deleted accounts stay deleted (`python users.py delete
  <username>`, or the "x" next to an account in Manage Users). This is
  also why account changes survive redeploys: the Sheet, not this repo,
  is the actual source of truth once it's wired up.

The dataset in `data/fleet_today.json` is real output from your actual
historical files (142 assets, 27 escalations, tested and verified
throughout this build), so the dashboard works immediately without
needing a live mailbox connection first, regardless of which accounts
path above applies.

## What's real vs. what needs your credentials to activate

| Piece | Status |
|---|---|
| Tampering detection engine | Ported verbatim, proven against your real report |
| Fleet classification logic (`fleet_logic/`) | Consolidated into this repo, no external folder dependency, proven from a fully isolated copy |
| Role-based access control | Tested at the HTTP level and in a real browser |
| Dashboard, all panels/charts/exports | Working, tested |
| Customer feedback (Sheets read + write + dashboard route) | Wired end to end, needs a service account to actually run (see `sheets_store.py`) |
| Email reading (`importer/mail_reader.py`) | Code complete, tested against real email structures, needs live IMAP credentials to actually connect |
| Daily scheduled import | Code complete (`importer/run_import.py`), needs deployment + Apps Script trigger |

See `DEPLOY.md` for the full path from here to a live daily-updating
dashboard.

## Email: what goes out, to whom, and when

Nine emails, in three groups. Every one of them is scoped to a single client:
one message per client, listing only that client's vehicles, to only that
client's contacts (`notifications._client_recipients`). A vehicle whose
platform account isn't mapped to a client is in nobody's email — that's a
configuration gap to fix, not a reason to broadcast.

**Weekly rollups** — a snapshot of current state, not a per-vehicle alert.
A vehicle stays in them until it actually resolves.

| Email | Audience | Contents |
|---|---|---|
| Weekly check-in | Client | Vehicles in Pending Customer Confirmation, plus escalated vehicles nobody has commented on yet. Two respond buttons per vehicle. |
| Known-issue re-confirmation | Client | Vehicles marked Known Issue, quoting what was said last time. |
| Technical escalation | Staff (admins + that client's technicians) | Vehicles where every platform is silent. Links to the dashboard, no respond tokens. |
| Tampering report | Client | Confirmed/unconfirmed location gaps, by severity, plus the vehicles being asked for a physical check. |

**Schedule** — `data/settings.ini`, `[digests]`:

```ini
weekly_send_day = monday      ; a weekday name, "daily", or "monday,thursday"
weekly_send_time = 08:00      ; 24h, East Africa Time
weekly_send_window_hours = 6  ; how long the slot stays open
send_pending_digest = true    ; each rollup can be turned off individually
```

A slot opens at `weekly_send_time` on each configured day. Inside it, each
client's digest goes out exactly once; outside it, nothing goes out. This is
a named slot rather than "at least 7 days since the last one" because the
elapsed-days version floored to whole days, so a cycle running a minute early
skipped and pushed the send to the next day — the weekly check-in walked
forward through the week. `app.py`'s digest scheduler thread ticks every 10
minutes and lets the slot decide; the daily 04:15 import calls the same code,
which simply does nothing outside a window.

**State-change emails** — triggered by what changed this cycle, not by a clock.

| Email | Fires when |
|---|---|
| Reconnect check | A vehicle with an *unanswered follow-up* comes back online. Asks whether that resolves it — two buttons, not an automatic close. |
| Back online (FYI) | A vehicle comes back online. States how long it was offline, which platforms were silent, and when it was last seen. |
| Comment update | A technician/admin comments and wants the client's input. |
| Outcome | Anyone records an answer. Goes to client and staff. |
| Internal action note | A technician sets a Recommended Action. **Staff only** — this is a job-card note, never sent to the customer. |

`[recovery]` controls the back-online FYI:

```ini
requires_comment = true                      ; only vehicles someone has commented on
suppress_when_reconnect_check_sent = true    ; never two emails for one reconnection
```

`requires_comment` is the per-asset control: a comment on file is somebody
having said this vehicle matters. Without it, a fleet reconnecting all day
generates enough mail that the client learns to filter it, losing the ones
that matter too. Each recovery is also recorded in
`data/recovery_notified.json` keyed by which offline spell it ended, so a
failed status write can't cause a repeat.

**Ad-hoc** — the paper-plane button in the header (admin/technician).
Sends any of the above reports, or a full single-asset summary, to a
hand-typed address. It never writes the weekly last-sent stamp, so a copy
sent on Wednesday can't suppress Monday's real send; it refuses any client
or plate outside your own access; and the single-asset email carries no
respond buttons, because those are a signed write credential for that plate.

## Running the test suites

```
cd importer && python3 test_tamper_engine.py      # proves the tampering port matches your real report
python3 test_correlation_logic.py                  # proves the power-event matching logic in isolation
cd .. && python3 test_permissions.py                # proves role boundaries hold at the HTTP level
```

## Project layout

```
app.py                     - Flask routes: login, dashboard data, exports, import trigger, feedback
permissions.py              - the one file that defines who sees what
users.py                    - account management (add/verify users)
sheets_store.py              - Google Sheets-backed feedback storage
fleet_logic/                  - fleet classification logic, consolidated into this repo
  schema.py, border_risk.py, classifier.py, settings.py, control_room.py
  adapters/                    - one parser per report format
importer/
  mail_reader.py             - IMAP + attachment/link extraction
  tamper_engine.py            - the tampering detection algorithm, ported verbatim
  run_import.py                - wires fetch + process into the daily job
  test_*.py                     - the three test suites above
templates/
  dashboard.html               - the SPA shell (same CSS/JS engine as the EXE version)
  login.html                    - sign-in page
apps_script_scheduler.gs        - the ENTIRE Apps Script, just a daily trigger
DEPLOY.md                        - step-by-step path to production
.env.example                      - every environment variable explained
```
