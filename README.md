# GTL Control Room

Flask backend for the GTL Fleet Intelligence dashboard, replacing the
local EXE workflow with a scheduled email import, role-based access
(admin / technician / client), and a live web dashboard.

## Quick start, local testing

```
pip install -r requirements.txt
python app.py
```

Open http://127.0.0.1:5000, log in with one of the demo accounts
already created in `database/users.json`:

| Username     | Password       | Role       |
|--------------|----------------|------------|
| justin       | TestPass123!   | admin      |
| brandon.b    | TestPass123!   | technician |
| gtl-client   | TestPass123!   | client     |

**Change these before going live**, see `users.py`. The demo dataset
in `data/fleet_today.json` is real output from your actual historical
files (142 assets, 27 escalations, tested and verified throughout
this build), so the dashboard works immediately without needing a
live mailbox connection first.

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
