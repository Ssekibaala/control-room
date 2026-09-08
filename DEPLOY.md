# GTL Control Room - Deployment Guide

> **Current deployment target: a self-managed Namecheap VPS (Pulsar plan),
> running the app as a Docker container with MySQL as its own container
> alongside it.** That full walkthrough - building/testing/pushing the
> image, then the exact VPS-side network/volume/credentials setup - lives
> in **[`DOCKER.md`](DOCKER.md)**, not below. The Northflank and cPanel/
> Namecheap-shared-hosting sections further down are earlier evaluations,
> kept for reference (cPanel's cron/resource limits were the reason the
> VPS path was chosen instead) - not the current plan.

Everything below has been tested locally (Flask's real test client, a real
headless Chromium browser, and the tampering engine against your actual
historical CSVs), including a full run from a **fully isolated copy of this
exact repo with no external folders present**, to prove the earlier
`gtl_integrity`-sibling-folder dependency is gone for good, `fleet_logic/`
is now consolidated inside this project. The one thing that CANNOT be
tested until you deploy is the live IMAP connection to
mail.teletracfleets.com, this sandbox has no network access to arbitrary
mail servers. Everything else has already run for real against real data.

## What's already proven to work

- `fleet_logic/` - the classification/adapter logic, now consolidated
  into this repo (previously an external sibling folder dependency,
  fixed and re-verified from a completely isolated copy).
- `importer/tamper_engine.py` - reproduces the real tampering report
  exactly (`importer/test_tamper_engine.py` diffs it against your actual
  Device_Tampering_Risk_Report_v2.xlsx, and passes).
- `importer/mail_reader.py` - attachment and signed-link extraction,
  tested against the real email structures you shared.
- `permissions.py` + `app.py` - role-based access, tested at the actual
  HTTP response level (`test_permissions.py`) AND in a real browser
  across all three roles (admin/technician/client), including the fix
  where hidden nav items were removed from the page entirely, not just
  CSS-hidden.
- `importer/run_import.py` - the full pipeline, `process_reports()`
  tested end-to-end against your real files, `check_periods_overlap()`
  tested against the real June/July mismatch we found earlier,
  feedback loading from Sheets now actually wired into the import
  (was a silent no-op before, fixed).
- `POST /api/feedback` - now exists, admin/technician only, client
  role stays read-only, tested including the missing-field and
  wrong-role cases.

## What you need to do, in order

### 1. Google Cloud service account (for Sheets-backed feedback)
1. console.cloud.google.com -> create or pick a project.
2. APIs & Services -> Enable APIs -> enable "Google Sheets API".
3. IAM & Admin -> Service Accounts -> Create Service Account.
4. On that service account, Keys -> Add Key -> Create new key -> JSON.
   Download it, keep it safe, never commit it to any repo.
5. Create a Google Sheet (or use an existing one). Click Share, paste
   in the service account's email (it's the `client_email` field in
   the JSON you downloaded, ends in `...iam.gserviceaccount.com`),
   give it Editor access.
6. Copy the Sheet's ID from its URL:
   `docs.google.com/spreadsheets/d/THIS_PART_HERE/edit`

### 2. Northflank web service
This app moved off Render - it now deploys from the `Dockerfile` at the
repo root (`CMD gunicorn app:app --bind 0.0.0.0:${PORT:-8080}`, since
Northflank injects `$PORT` at runtime the same way Render did). No
build/start command fields to fill in separately; Northflank builds the
image straight from that Dockerfile.
1. Push this whole `gtl_control_room/` folder to a GitHub repo.
2. northflank.com -> create a new Service -> connect that repo/branch
   -> build type "Dockerfile" (should auto-detect it at the repo root).
3. Under the service's Environment / Secrets, add every variable from
   `.env.example` / your local `.env`:
   - `FLASK_SECRET_KEY`: any long random string
   - `PUBLIC_BASE_URL`: your real Northflank service URL, e.g.
     `https://your-app.your-team.northflank.app`. **Required here** -
     Render used to auto-inject `RENDER_EXTERNAL_URL` and this app fell
     back to it; Northflank doesn't provide an equivalent, and without
     this set, emailed action links don't go out broken, they silently
     stop being sent at all (see `_public_base_url()` in
     `importer/run_import.py`).
   - `EMAIL_ADDRESS` / `EMAIL_PASSWORD`: the real mailbox credentials
     (mailer.py currently sends via SMTP with these - see the note
     below, `RESEND_API_KEY` is set in `.env` but not yet wired in)
   - `IMPORT_API_KEY`: any long random string, you'll reuse this exact
     value in the Apps Script below
   - `GOOGLE_SERVICE_ACCOUNT_JSON`: paste the entire downloaded JSON
     key as one line
   - `FEEDBACK_SHEET_ID`: from step 1.6
   - the MiX/Teletrac/FT Cloud API credentials, if you want those
     pollers running in production too
4. Deploy. Confirm whether the service is set to auto-deploy on every
   push to your default branch, or whether it needs a manual "Deploy"
   click per Northflank's own dashboard - that setting lives in the
   service's build/deploy configuration and isn't something this repo
   controls.

> **Note on outbound email:** `.env`'s comment says feedback
> notifications go out "via Resend", and `RESEND_API_KEY` is set, but
> `mailer.py` as it stands still sends over SMTP using `EMAIL_ADDRESS`/
> `EMAIL_PASSWORD` (`smtplib`), not Resend's HTTP API. Worth confirming
> which one is actually intended before relying on outbound mail in
> production - if Resend was meant to fully replace SMTP, that
> migration doesn't look finished in the code.

### 3. Apps Script scheduler
1. script.google.com -> New project.
2. Paste in `apps_script_scheduler.gs`.
3. Replace `RENDER_APP_URL` with your real Northflank service URL +
   `/api/import` (the constant name is a leftover from Render, the
   value just needs to be the right URL).
4. Replace `API_KEY` with the exact same string you set as
   `IMPORT_API_KEY` on Northflank.
5. Run `setupDailyTrigger` once manually (top toolbar, function
   dropdown, then Run). It'll ask you to authorize, that's expected,
   approve it.
6. Confirm under the clock icon (Triggers) on the left that a daily
   trigger now exists for ~4:15 AM.

### 4. Create real user accounts
Locally, or via however Northflank exposes a shell/one-off job for this
service (check its dashboard - naming varies by platform):
```
python users.py add justin admin "a-real-password"
python users.py add brandon.b technician "a-real-password"
python users.py add gtl-client client "a-real-password"
```
Change these usernames/passwords to whatever you actually want, these
are just examples matching names already in this conversation.

### 5. First real import
Don't wait for 4:15 AM the first time. Trigger it manually to see it
work:
```
curl -H "X-API-Key: your-import-api-key" https://your-app-on-northflank/api/import
```
Watch the service's Logs in the Northflank dashboard while this runs.
If a report is missing or a download link can't be found, the error
will name exactly which report subject failed, that's deliberate, see
`mail_reader.py` and `run_import.py`'s `fetch_reports()`.

### 6. Log in
Go to your Northflank service's URL, sign in as one of the accounts
from step 4, confirm the right panels show up for that role.

## Deploying to Namecheap Stellar Plus (cPanel + MySQL)

This is the alternative deployment path: MySQL instead of Google Sheets
(see `db.py`/`db_store.py`), and cron-triggered polling/digests instead of
the in-process background threads Northflank/Docker rely on (`app.py`'s
pollers still exist and still work there - this path just replaces their
trigger mechanism for hosting that can't keep a background thread alive
between requests). Verified specifically against Namecheap's own published
limits for this plan: Python 3.12 is supported via "Setup Python App",
jailed SSH is included, and cron enforces a 5-minute floor with a
5-simultaneous-job cap - all accounted for below.

### 1. MySQL database
cPanel -> MySQL Databases -> create a database and a user, add the user to
the database with all privileges. Namecheap prefixes both with your cPanel
username (e.g. `yourusername_controlroom`, `yourusername_ccuser`) -
that's normal, use the prefixed names exactly as shown.

### 2. Setup Python App
cPanel -> Setup Python App -> Create Application:
- Python version: 3.12 (matches this repo's `Dockerfile`, but nothing
  here is 3.12-specific - an older 3.9+ version offered by your host
  would also work).
- Application root: wherever you upload this repo.
- Application startup file: `passenger_wsgi.py` (already in this repo).
- Application Entry point: `application` (the module-level name
  `passenger_wsgi.py` exposes - Namecheap's Passenger integration
  requires exactly this name).

Use the "Run Pip Install" button (or the venv-activation command the
panel shows you) against `requirements.txt`.

### 3. Environment variables
Either cPanel's own "Environment Variables" section on the Python App
page (preferred for secrets - a `.env` file sitting in a web-accessible
directory is a real, if usually mitigated, exposure risk), or a `.env`
file next to `app.py` (python-dotenv already loads this). Set everything
in `.env.example`, specifically including for this path:
- `MYSQL_HOST` (usually `localhost` on shared hosting), `MYSQL_PORT`,
  `MYSQL_DATABASE`, `MYSQL_USER`, `MYSQL_PASSWORD` from step 1.
- `CRON_API_KEY`: any long random string, reused in the cron commands below.
- `PUBLIC_BASE_URL`: your real domain - required here the same way it's
  required on Northflank (see step 2 above); this host injects no
  equivalent to `RENDER_EXTERNAL_URL` either.
- Every other existing var (`FLASK_SECRET_KEY`, `EMAIL_ADDRESS`/
  `EMAIL_PASSWORD`, `IMPORT_API_KEY`, the MiX/Teletrac/FT Cloud
  credentials) - unchanged from the Northflank setup.
- Leave `GOOGLE_SERVICE_ACCOUNT_JSON`/`FEEDBACK_SHEET_ID` set ONLY long
  enough to run the one-time migration in step 5 below, then remove them.

### 4. Create the schema
From cPanel's Terminal (or SSH):
```
python scripts/init_db.py
```
Safe to re-run; every statement is `CREATE TABLE IF NOT EXISTS`.

### 5. Migrate existing data out of the Sheet (skip if starting fresh)
```
python scripts/export_sheets_to_mysql.py --dry-run   # check the printed counts first
python scripts/export_sheets_to_mysql.py             # then actually migrate
```
Watch for a "CORRUPTED id" warning on any client - that means Sheets had
already mangled a long platform id into a float at some point in the
past (see `sheets_store.py`'s `_looks_corrupted_id` docstring); the
original digits are unrecoverable, so re-pick that platform mapping from
the dropdown in Manage Users/Client Contacts after cutover.

### 6. Create user accounts (if not migrated in step 5)
Same command as the Northflank setup - with `MYSQL_*` now configured,
`users.py` writes to MySQL instead of the local JSON fallback:
```
python users.py add justin admin "a-real-password"
python users.py add brandon.b technician "a-real-password"
python users.py add gtl-client client "a-real-password"
```

### 7. Cron jobs
cPanel -> Cron Jobs. Namecheap enforces a 5-minute minimum interval and a
5-simultaneous-job cap on shared hosting - the three entries below total
3 jobs, comfortably under that cap:

```
# Platform polling (MiX + Teletrac + FT Cloud in one request) - every 5 min
*/5 * * * * curl -s -X POST -H "X-API-Key: YOUR_CRON_API_KEY" https://your-domain.example/api/cron/poll-all

# Digest scheduler tick - every 10 min (digests are gated on their own
# weekly window regardless of how often this fires, see run_import.py)
*/10 * * * * curl -s -X POST -H "X-API-Key: YOUR_CRON_API_KEY" https://your-domain.example/api/cron/digest-tick

# Daily mail import - once a day, matching the old Apps Script schedule
15 4 * * * curl -s "https://your-domain.example/api/import" -H "X-API-Key: YOUR_IMPORT_API_KEY"
```
Then retire `apps_script_scheduler.gs`'s trigger (Triggers icon in
script.google.com -> delete it) if this app previously ran there.

Also disable the in-process pollers/scheduler that would otherwise start
as a side effect of the app importing (set all four in your `.env`/cPanel
environment variables, since cron is now doing their job instead):
```
DISABLE_MIX_API_POLLER=true
DISABLE_TELETRAC_API_POLLER=true
DISABLE_FT_CLOUD_API_POLLER=true
DISABLE_DIGEST_SCHEDULER=true
```

### 8. Verify before trusting it with real traffic
- `GET /api/health/disk` right after `POST /api/health/disk` (log in
  first, or curl both with a session cookie) - confirms cron's execution
  context and the web app actually share the same writable `data/`
  directory.
- `GET /api/health/connectivity` (logged in as admin/technician) -
  checks SMTP, IMAP, MySQL, and each configured platform API in one
  call. A `false` on `smtp`/`imap` most likely means the host is
  blocking that port - confirmed allowed ports on Namecheap shared
  hosting are 587/465 for SMTP (25 is blocked) at the time this was
  written, but verify against your actual host rather than assuming.
- Trigger `/api/cron/poll-all` and `/api/cron/digest-tick` manually
  once each (same curl commands as the cron entries, run by hand) and
  check the response before waiting for the first real cron tick.

## Known trade-offs, made deliberately

- **No SQLite, no Drive backup.** Feedback lives in Google Sheets
  (with its own free version history). The daily `fleet_today.json`
  cache lives only on the service's own disk and is never backed up,
  because it's fully regenerable by re-running the import, nothing
  irreplaceable depends on it surviving a redeploy.
- **Apps Script only schedules, it doesn't read mail.** This mailbox
  isn't Gmail, `GmailApp` can't touch it. All mail reading happens in
  Python via standard IMAP.
- **Cold-start behaviour depends on your Northflank plan.** Render's
  free tier used to sleep after idle time and take 30-60s to wake up;
  whether Northflank's plan does the same is a pricing/plan detail on
  their side, not something this app controls. The scheduled import
  can tolerate a slow first request either way; a person waiting on a
  cold dashboard is the only case that'd actually feel it.

## If something breaks

Every failure in this pipeline raises a specific, readable error
rather than a generic stack trace, on purpose:
- Wrong or missing report email -> names which subject wasn't found.
- Movement/event period mismatch -> states both date ranges directly.
- Missing Sheets credentials -> tells you exactly which env vars to set.
- Wrong API key on `/api/import` -> plain 403, no detail leaked.

Check the Northflank service's Logs first, the message there should
tell you exactly what to fix, not just that something failed.
