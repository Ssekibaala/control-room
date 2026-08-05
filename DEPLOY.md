# GTL Control Room - Deployment Guide

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
