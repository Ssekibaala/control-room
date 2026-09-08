# Security & Code Quality Audit

Date: 2026-08-07
Scope: full repository (Flask backend, permission model, token/session handling,
templates, committed data files).

Findings are ranked by severity. Each includes the affected file/line, why it
matters, and a concrete fix.

---

## 🔴 Critical

### 1. Real account passwords are committed to git and were confirmed crackable

- **Where:** [`database/users.json`](../database/users.json) (password hashes for
  `justin` / admin, `brandon.b` / technician, `gtl-client` / client) and
  [`test_permissions.py`](../test_permissions.py) (lines 46, 69, 88, 128, 287, 299,
  316, 333, 338, 348, 364 — all log in with the same literal password string).
- **What was verified:** hashing that literal string with the same scrypt
  parameters (`scrypt:32768:8:1`) as the committed hashes reproduces all three
  stored hashes exactly. All three real, named accounts share one password that
  is sitting in plaintext in the test file.
- **Why it's critical, not just "a fixture leak":**
  - `brandon.b@teletracfleets.com` is the real mailbox address used for outbound
    SMTP (see `.env.example`), and `justin`/`brandon.b` are named in
    `permissions.py`'s own docstring as real admin/technician users — these are
    not placeholder accounts.
  - Per `users.py`'s `load_users()` (around line 60), if the Google Sheet's
    `Users` tab is ever empty, this local file is **automatically migrated into
    the live production Sheet** (`_migrate_local_to_sheet`). This file is not
    guaranteed to stay a harmless local-only fallback — it can become the real,
    deployed credential set.
  - Anyone with read access to this repository (current or historical, since
    git history retains old file contents even after edits) has a working
    admin login.
- **Fix:**
  1. Rotate all three passwords in whatever store is actually live today, immediately.
  2. Stop committing real credentials for test fixtures — generate throwaway
     accounts in a temp file/fixture, never reuse real usernames or a shared
     literal password.
  3. Purge the file and the literal password from git history (`git filter-repo`
     or BFG Repo-Cleaner) — deleting/editing the file today does not remove it
     from history.

---

## 🟠 High

### 2. Secret key silently falls back to a hardcoded value instead of failing loudly

- **Where:** [`app.py:49`](../app.py#L49) — `app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-only-change-me")`,
  and [`respond_tokens.py:24`](../respond_tokens.py#L24) — same fallback string,
  used to sign the `/feedback/respond` email-response tokens.
- **Why it matters:** `FLASK_SECRET_KEY` is documented as required in `DEPLOY.md`,
  so a correctly-configured deployment is fine today. But the code has no guard
  rail: if the env var is ever missing (new environment, a misconfigured secret
  store, a redeploy that drops it), the app starts anyway and silently signs
  session cookies **and** the tokens that let an email link write feedback for a
  vehicle — using a value that is plaintext in this public source file. Anyone
  could then forge an admin session or a respond-token.
- **Fix:** raise at import/startup time if `FLASK_SECRET_KEY` is unset in any
  non-explicitly-local-dev mode, rather than defaulting.

### 3. No brute-force protection on login

- **Where:** `/api/login` — [`app.py:134`](../app.py#L134).
- **Why it matters:** no rate limiting, no lockout after repeated failures.
  Combined with Finding 1 (weak, shared, known password) this makes credential
  stuffing/brute forcing trivial and invisible — there's nothing here to even
  notice it happening.
- **Fix:** add a per-IP/per-username rate limit (e.g. Flask-Limiter) and/or a
  short lockout after N failed attempts.

### 4. Inconsistent timing-safe comparison of shared secrets

- **Where:** [`app.py:2449`](../app.py#L2449) — `/api/import`'s API-key check uses
  `provided_key != expected_key` (not constant-time), while the FT Cloud webhook
  secret check two hundred lines earlier ([`app.py:2310`](../app.py#L2310))
  correctly uses `hmac.compare_digest(secret, expected)`.
- **Why it matters:** a plain `!=` comparison can leak timing information about
  how many leading characters matched, in principle allowing an attacker to
  recover the key byte-by-byte given enough attempts.
- **Fix:** use `hmac.compare_digest` consistently for every secret comparison.

---

## 🟡 Medium

### 5. `app.run(debug=True, ...)` left in the entrypoint

- **Where:** [`app.py:2495`](../app.py#L2495).
- **Why it matters:** production runs via gunicorn per the `Dockerfile`, so this
  exact line isn't the live path today. It's still a footgun: Werkzeug's
  interactive debugger allows arbitrary code execution if this file is ever run
  directly (`python app.py`) in a reachable environment.
- **Fix:** gate `debug` on an explicit env var (default `False`), never hardcode `True`.

### 6. No explicit cookie/security-header hardening

- **Where:** app-wide — no `SESSION_COOKIE_SECURE`, `SESSION_COOKIE_SAMESITE`
  configured; no CSP, HSTS, X-Frame-Options, or X-Content-Type-Options anywhere;
  no Flask-Talisman or equivalent in `requirements.txt`.
- **Why it matters:** this app carries multi-tenant fleet/ops data behind a
  session cookie. Without `Secure`/`SameSite` set explicitly, behavior relies on
  browser defaults rather than an intentional policy, and there's no defense in
  depth against clickjacking or MIME-sniffing.
- **Fix:** set `SESSION_COOKIE_SECURE=True`, `SESSION_COOKIE_SAMESITE="Lax"`
  explicitly; add baseline security headers (Flask-Talisman or a small
  `after_request` hook).

### 7. Weak password policy

- **Where:** [`app.py:234`](../app.py#L234) — 8-character minimum only, no
  complexity or breach-list check.
- **Why it matters:** stacks with Finding 3 (no rate limiting) to make weak
  passwords practically guessable.
- **Fix:** raise the minimum length, and/or check against a known-breached-password list.

---

## 🔵 Lower priority / code quality

### 8. In-process background threads for pollers + digest scheduler

- **Where:** [`app.py:1903`](../app.py#L1903) (MiX poller), similar Teletrac/FT
  Cloud pollers, and the digest scheduler — all started as a side effect of
  importing the module, guarded only by `"pytest" in sys.modules` checks.
- **Why it matters:** works today because the `Dockerfile`'s gunicorn command
  defaults to a single worker. Scaling to multiple workers later would silently
  multiply outbound API polling and risk duplicate digest sends to real
  clients — a risk the code's own comments acknowledge but only partially guard
  against (file-lock cooldowns, not a real single-writer guarantee).
- **Fix:** move recurring jobs to a separate worker/cron process rather than
  threads inside the web process.

### 9. `app.py` is a ~2,500-line monolith

- **Where:** the whole file.
- **Why it matters:** routing, three platform pollers, digest scheduling, and
  business logic are all in one file. Not a vulnerability by itself, but it's
  the kind of structure that makes the next security review slower and
  increases the odds a new route misses a permission check that a smaller,
  focused module would make obvious.
- **Fix:** split into blueprints/modules (routes, pollers, scheduler) over time.

---

## What's already done well (for context, not action items)

- `permissions.py`'s client-scoping is thorough and fails closed by default
  (unmapped/unknown clients and plates are denied, not allowed).
- `dashboard.html`'s `innerHTML` usage consistently routes user-influenced
  strings through a single `escapeHTML()` helper — no XSS found in the spots sampled.
- Outbound email addresses are regex-validated before being placed in SMTP
  headers, blocking header injection.
- No TLS-verification bypasses (`verify=False`, `CERT_NONE`, etc.) anywhere in
  the platform adapters.
