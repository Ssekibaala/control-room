# Deploying to Azure (App Service for Containers + Azure Database for MySQL)

This is the Azure Portal (click-through, not `az` CLI) path for hosting the
existing `maertin24/control-room` Docker Hub image. Two decisions this guide
assumes, both made deliberately over the alternatives:

- **Compute: App Service for Containers**, not a raw VM. It points straight
  at the same Docker Hub image DOCKER.md already built and tested, and Azure
  supervises the process for you - restarts it if it crashes, restarts it
  after you change a setting, keeps it running. Several "why is this showing
  old data" moments earlier in this project turned out to be the Flask dev
  server never having restarted after a code change; App Service's own
  restart-on-deploy behavior is what a VM does NOT give you for free.
- **Database: Azure Database for MySQL - Flexible Server**, not a
  self-hosted MySQL container. Backups, patching, and restart-after-failure
  become Azure's job instead of yours - the same reasoning as the compute
  choice, applied to the database this time.

If you outgrow either later, the container image itself doesn't change -
only where it runs.

## Prerequisites

- An Azure subscription (Portal: [portal.azure.com](https://portal.azure.com)).
- The image already pushed to Docker Hub (`maertin24/control-room:latest`
  or `:v1` - see DOCKER.md).
- Every secret from `.env.example` at hand: `FLASK_SECRET_KEY`,
  `EMAIL_ADDRESS`/`EMAIL_PASSWORD`, `IMPORT_API_KEY`, `FT_CLOUD_WEBHOOK_SECRET`,
  and the MiX/Teletrac/FT Cloud API credentials. `PUBLIC_BASE_URL` you won't
  know until Step 5 (it's the App Service's own URL, or your custom domain).
- A domain name if you want `app.yourcompany.com` instead of the default
  `*.azurewebsites.net` URL (optional - can be added after everything else
  works).

---

## 1. Resource Group

Portal → **Resource groups** → **Create**. One resource group holding
everything below (`control-room-rg` or similar) makes teardown/cost-tracking
a single delete rather than hunting down five separate resources. Pick a
region close to your users/clients - the same region for every resource
below avoids cross-region latency and (for the database) extra egress cost.

---

## 2. Azure Database for MySQL - Flexible Server

Portal → **Create a resource** → search **Azure Database for MySQL** →
**Flexible server**.

- **Resource group**: the one from Step 1.
- **Server name**: e.g. `control-room-mysql` (this becomes part of its
  hostname: `control-room-mysql.mysql.database.azure.com`).
- **Region**: same as Step 1.
- **MySQL version**: 8.0 (matches what DOCKER.md tested against locally).
- **Workload type**: *Development* is enough to start - cheapest compute
  tier (Burstable, B1ms). Bump this later under **Compute + storage** once
  real traffic justifies it; nothing else in this guide changes if you do.
- **Authentication**: MySQL authentication only. Set a real admin username
  (not `root`) and a strong password - this becomes `MYSQL_USER`/
  `MYSQL_PASSWORD` for the *admin* connection used once in Step 3, not
  necessarily what the app itself connects as (you can create a
  narrower-privilege app user afterward the same way `db.py`'s docstring
  describes for any MySQL host, or just use the admin account directly for
  a first deployment and tighten later).
- **Networking**: **Public access** (the simplest correct choice here,
  *not* the private-VNet option) - App Service for Containers isn't on the
  same private network as the database by default, and wiring up VNet
  integration is real extra work this app doesn't need yet. Public access
  + firewall rules is the same tradeoff cPanel/shared-hosting MySQL makes.
  - Tick **"Allow public access from any Azure service within Azure to
    this server"** - this is what lets the App Service container reach the
    database at all, since App Service's outbound IP isn't fixed/known
    ahead of time on the cheaper tiers.
  - Add your own current IP too (the Portal has an "Add current client IP
    address" button) - needed for Step 3 below, remove it afterward if you
    want to lock things down further.

Create it, then open the resource once provisioned → **Databases** (left
nav) → **+ Add** → name it `control_room` (matches `MYSQL_DATABASE` in
every other guide in this repo).

---

## 3. Create the schema (once)

Run this from your own machine (needs Docker Desktop, same as DOCKER.md) -
it uses the image you already built to run `scripts/init_db.py` once
against the new Azure database, before the app itself is even deployed:

```bash
docker run --rm \
    -e MYSQL_HOST=control-room-mysql.mysql.database.azure.com \
    -e MYSQL_PORT=3306 \
    -e MYSQL_DATABASE=control_room \
    -e MYSQL_USER=<the admin username from Step 2> \
    -e MYSQL_PASSWORD="<the admin password from Step 2>" \
    maertin24/control-room:latest python scripts/init_db.py
```

Expect `Creating N table(s) if they don't already exist. Done.` - if it
instead times out or refuses the connection, the most common cause is your
current IP not actually being in the server's firewall allow-list yet (Step
2's "Add current client IP address"); check **Networking** on the MySQL
resource.

If this is a migration from an existing deployment rather than a fresh
start, this is also the point to run `scripts/export_sheets_to_mysql.py` or
your own dump/restore from the old MySQL host into this new one, the same
way DEPLOY.md's cPanel migration section describes - the schema is
identical either way.

---

## 4. App Service for Containers

Portal → **Create a resource** → **Web App**.

- **Resource group**: Step 1's.
- **Name**: becomes `<name>.azurewebsites.net` - this is your
  `PUBLIC_BASE_URL` unless you add a custom domain later.
- **Publish**: **Docker Container** (not "Code" - that path assumes Azure
  builds the app from source, which isn't the setup here).
- **Operating System**: **Linux**.
- **Region**: same as Steps 1-2.
- **App Service Plan**: create new. **B1 (Basic)** is the minimum tier that
  supports **Always On** (Step 6 needs this - the free/shared tiers don't
  offer it, and this app's background pollers depend on the process never
  idling out). Scale up later without touching anything else in this guide.
- **Docker tab**: 
  - **Options**: Single Container.
  - **Image Source**: Docker Hub.
  - **Access Type**: Public (unless you made the Docker Hub repo private,
    per the earlier note in this project about it having briefly gone
    public by accident - if it's private now, choose Private and supply
    your Docker Hub credentials here).
  - **Image and tag**: `maertin24/control-room:latest` (or `:v1` for a
    pinned version instead of always pulling latest on restart).

Create it. First boot will fail health checks until Step 5's settings are
in place - that's expected, keep going.

---

## 5. Application Settings (environment variables)

On the new Web App → **Settings → Environment variables** (older Portal
layouts: **Configuration → Application settings**) → **+ Add** for each of
the following, then **Save** (this restarts the container, which is
expected and required for new settings to take effect):

| Name | Value |
|---|---|
| `WEBSITES_PORT` | `8080` - tells Azure which port the container listens on; matches the Dockerfile's default (`CMD gunicorn app:app --bind 0.0.0.0:${PORT:-8080}`). Without this, Azure's health check can't reach the app and the site shows "Application Error." |
| `FLASK_SECRET_KEY` | a long random string |
| `PUBLIC_BASE_URL` | `https://<your-app-name>.azurewebsites.net` (or your custom domain, once Step 8 is done) |
| `EMAIL_ADDRESS` | `brandon.b@teletracfleets.com` (or whichever mailbox) |
| `EMAIL_PASSWORD` | its password |
| `IMPORT_API_KEY` | a long random string - whatever triggers `/api/import` needs to send this back |
| `MYSQL_HOST` | `control-room-mysql.mysql.database.azure.com` |
| `MYSQL_PORT` | `3306` |
| `MYSQL_DATABASE` | `control_room` |
| `MYSQL_USER` | from Step 2 |
| `MYSQL_PASSWORD` | from Step 2 |
| `MIX_CLIENT_ID` / `MIX_CLIENT_SECRET` / `MIX_USERNAME` / `MIX_PASSWORD` | real MiX Integrate API credentials |
| `TELETRAC_API_KEY` / `TELETRAC_API_SECRET` | real Teletrac Integrate API credentials |
| `FT_CLOUD_API_SIGN` / `FT_CLOUD_TENANT_ID` | real FT Cloud OpenAPI credentials |
| `FT_CLOUD_WEBHOOK_SECRET` | a long random string (`python -c "import secrets; print(secrets.token_hex(24))"`) |

Leave `CRON_API_KEY` unset - App Service for Containers keeps the app
running continuously (unlike request-driven cPanel hosting), so the
in-process pollers/digest scheduler this image already runs don't need
cron at all. That's the whole reason the compute choice matters here.

Then, same page, **General settings** tab (or **Configuration → General
settings**):

- **Always On**: **On**. Without this, Azure can idle the container after
  a period of no HTTP traffic - which would silently kill the MiX/Teletrac/
  FT Cloud poll loops and the digest scheduler, the same background-thread
  behavior the original cPanel-feasibility research flagged as
  request-driven hosting's core limitation. App Service with Always On
  avoids that problem entirely; it's not optional for this app.

---

## 6. Persistent storage for `data/` and `database/`

`.dockerignore` deliberately keeps `data/` and `database/users.json` out of
the image (real client fleet data and password hashes have no business
being baked into a distributed image - see DOCKER.md). The app tolerates a
missing `data/` directory at startup and rebuilds it from the next
import/poll cycle, so this step isn't required just to boot - but without
it, **every container restart or redeploy silently resets `data/settings.ini`
to hardcoded defaults** (thresholds, which MiX/Teletrac/FT Cloud
organisations get polled) and throws away the last known-good platform
snapshots until the next poll cycle repopulates them. Worth doing before
real use, not just as a nice-to-have.

1. Create a **Storage Account** (Portal → Create a resource → Storage
   account) in the same resource group/region. Redundancy: **LRS** is fine,
   this isn't irreplaceable data.
2. Inside it, **File shares** → **+ File share** → name it e.g. `crdata`.
3. Back on the Web App → **Settings → Path mappings** (or
   **Configuration → Path mappings** on older layouts) → **+ Azure Storage
   mount**:
   - **Name**: `crdata`
   - **Storage account** / **Storage container**: the ones just created.
   - **Mount path**: `/app/data`
4. Repeat for `database/` if you want `role_panels.json` (permission
   overrides) to survive restarts too - a second share mounted at
   `/app/database`. (Never mount `database/users.json` itself this way in
   a shared/public context - it's real credential data; MySQL already holds
   the real `users`/`user_clients` tables per the migration, so this file
   is a local-dev fallback only, see `users.py`'s docstring.)

Save, which restarts the container again. From here, `data/settings.ini`
persists across restarts - if this is a fresh deployment (not a migration),
copy your tuned `data/settings.ini` (thresholds, `[mix_api]`/
`[teletrac_api]`/`[ft_cloud_api]` org/client/fleet-id mappings) into the
new file share once, via **Storage browser** on the Storage Account
resource, before relying on it.

---

## 7. First boot checks

- **Log stream**: Web App → **Monitoring → Log stream** - watch for the
  gunicorn boot lines and confirm no crash loop. This is your fastest
  signal if an Application Setting is missing or wrong.
- Visit `https://<your-app-name>.azurewebsites.net` - should show the
  sign-in page.
- Open a **console** on the running container: Web App → **Development
  Tools → SSH** (or **Console**) → create the first real account the same
  way DOCKER.md's VPS section does, just via this in-browser shell instead
  of `docker exec`:
  ```
  python users.py add justin admin "a-real-password"
  ```
- Log in, confirm the dashboard loads with real data once the first poll
  cycle completes (a few minutes, per `settings.ini`'s `poll_interval_minutes`).

---

## 8. Custom domain + HTTPS

Web App → **Custom domains** → **+ Add custom domain**, follow the CNAME/TXT
verification against your DNS provider, then **Certificates → Add binding**
→ **App Service Managed Certificate** (free, auto-renewing - no
Caddy/nginx/Let's Encrypt setup needed, unlike the VPS path in DOCKER.md).
Once it's bound, update `PUBLIC_BASE_URL` in Step 5's settings to the real
domain and save.

---

## 9. FT Cloud webhook

If this is a **new** deployment: subscribe FT Cloud's webhook to
`https://<your-domain>/webhook/ftcloud/<FT_CLOUD_WEBHOOK_SECRET>` (the
value from Step 5) - see `fleet_logic/adapters/ft_cloud_webhook.py` for the
subscribe call.

If this is a **cutover** from an existing deployment: re-subscribe to the
new URL with a **fresh** secret before decommissioning the old one, so
there's never a moment where the old deployment's webhook secret is still
live pointed at a URL nothing is listening on. Same cutover discipline
DEPLOY.md's migration section already describes for the cPanel path -
disable the old deployment's pollers/scheduler via its own `DISABLE_*` env
vars, verify one full cycle on Azure, then decommission the old host.

---

## Cost, roughly

At the tiers above (App Service **B1 Basic**, MySQL **Burstable B1ms**),
expect on the order of **$25-40/month** combined, before any custom-domain
or bandwidth costs - check the [Azure Pricing
Calculator](https://azure.microsoft.com/pricing/calculator/) for your exact
region before committing, prices vary by region and change over time. Both
tiers scale up independently later with no code or config changes beyond
the tier selection itself.
