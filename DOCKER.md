# Docker: build, test, and publish

This is the step-by-step for building the `control-room` image, testing it
against a real MySQL container locally (not just "does it boot"), and
pushing it to Docker Hub so it can be pulled on the production VPS. Every
command below was actually run once to produce this guide - it's not
theoretical.

## Prerequisites

- Docker Desktop installed and running. On Windows this needs **WSL2**
  (`wsl --install` from an elevated PowerShell, then reboot) - Docker
  Desktop's processes can be running without this and still fail every
  command with a 500 error from the daemon, which is exactly what "not
  built yet" looks like from the CLI.
- Confirm it's actually up before doing anything else:
  ```
  docker info --format "{{.ServerVersion}} / OSType={{.OSType}}"
  ```
  Should print a version and `OSType=linux`, not an error.

## What NOT to ship in the image

Before the first build, two things matter and are already handled by
`.dockerignore`:

- **`data/`** - real client fleet data (positions, plates, tampering
  reports). It's deliberately committed to *git* (see `.gitignore`'s own
  comment - it's what makes `git clone && run` work instantly as a demo),
  but a Docker image is a much wider distribution channel than a private
  git remote, especially once it's on Docker Hub. Excluded entirely; the
  app creates `data/` itself at startup and repopulates it from the real
  import/pollers.
- **`database/users.json`** - real password hashes (flagged for rotation
  in `docs/SECURITY_AUDIT.md`). Same reasoning, excluded entirely.
- `scripts/` is deliberately **kept in** the image (unlike `test_*.py`,
  `docs/`, `*.md`) - `scripts/init_db.py` and
  `scripts/export_sheets_to_mysql.py` are meant to be run via
  `docker exec` on the deployed container itself.

If you ever add new top-level files with real data or secrets, add them
to `.dockerignore` before your next build - `COPY . .` in the `Dockerfile`
ships everything not explicitly excluded.

## 1. Build

```
docker build -t control-room:test .
```

Sanity-check what actually made it into the image before going further:

```
docker run --rm control-room:test sh -c "ls -la /app/data/ 2>&1; ls -la /app/database/; du -sh /app"
```

Expected: `/app/data/` doesn't exist (`No such file or directory`),
`/app/database/` is empty, and the whole image content is ~1-2MB (the
Python dependencies are a separate, cached layer - this is just checking
your own code+data didn't sneak anything in).

## 2. Test against a real MySQL (not just "does Flask boot")

Testing the container in isolation only proves Flask starts - it doesn't
prove the MySQL migration actually works. Spin up both together on an
isolated Docker network:

```
docker network create cr-test-net

docker run -d --name cr-test-mysql --network cr-test-net \
    -e MYSQL_ROOT_PASSWORD=testroot \
    -e MYSQL_DATABASE=control_room \
    -e MYSQL_USER=cruser \
    -e MYSQL_PASSWORD=crpass \
    mysql:8.0
```

Wait for it to actually be ready (the container starts long before MySQL
inside it finishes initializing):

```
docker exec cr-test-mysql mysqladmin ping -h localhost -u root -ptestroot
```
Retry every few seconds until it says `mysqld is alive`.

Create the schema (this is the same command you'll run once on the real
VPS - see `DEPLOY.md`):

```
docker run --rm --network cr-test-net \
    -e MYSQL_HOST=cr-test-mysql -e MYSQL_PORT=3306 -e MYSQL_DATABASE=control_room \
    -e MYSQL_USER=cruser -e MYSQL_PASSWORD=crpass \
    control-room:test python scripts/init_db.py
```
Expect `Creating 15 table(s) if they don't already exist. Done.`

Now start the actual app container against that database:

```
docker run -d --name cr-test-app --network cr-test-net -p 8080:8080 \
    -e FLASK_SECRET_KEY=test-secret-not-for-prod \
    -e MYSQL_HOST=cr-test-mysql -e MYSQL_PORT=3306 -e MYSQL_DATABASE=control_room \
    -e MYSQL_USER=cruser -e MYSQL_PASSWORD=crpass \
    -e DISABLE_MIX_API_POLLER=true -e DISABLE_TELETRAC_API_POLLER=true \
    -e DISABLE_FT_CLOUD_API_POLLER=true -e DISABLE_DIGEST_SCHEDULER=true \
    control-room:test

docker logs cr-test-app
```
The `DISABLE_*` flags keep it from trying real outbound API polling
during a local test - leave them unset on the real deployment.
Should show gunicorn starting with no errors.

### Smoke test checklist

```
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8080/login   # expect 200
curl -s http://localhost:8080/api/session                              # expect {"authenticated":false}

# Create a real account (routes to MySQL, not the local JSON fallback,
# because MYSQL_* is set)
docker exec cr-test-app python users.py add testadmin admin "SomePassword123!"

# Log in for real and keep the session cookie
curl -s -c cookies.txt -X POST http://localhost:8080/api/login \
  -H "Content-Type: application/json" \
  -d '{"username":"testadmin","password":"SomePassword123!"}'

# Confirm session-gated routes work
curl -s -b cookies.txt http://localhost:8080/api/dashboard-data        # expect 200
curl -s -b cookies.txt http://localhost:8080/api/users                 # expect "durable":true

# Health checks
curl -s -b cookies.txt http://localhost:8080/api/health/connectivity   # mysql: {"ok":true}, others "not set"
curl -s -X POST http://localhost:8080/api/health/disk                  # expect {"status":"ok",...}
```

`"durable":true` in `/api/users` is the one that matters most - it means
`users.py` correctly detected MySQL and is NOT silently falling back to
the local JSON file.

### Tear down

```
docker rm -f cr-test-app cr-test-mysql
docker network rm cr-test-net
```

## 3. Push to Docker Hub

```
docker login
```
(enter your Docker Hub credentials interactively - never pass a password
as a command-line argument, it ends up in shell history)

```
docker tag control-room:test YOUR_DOCKERHUB_USERNAME/control-room:latest
docker push YOUR_DOCKERHUB_USERNAME/control-room:latest
```

Use a version tag too, not just `latest`, so you can roll back a bad
deploy on the VPS by pulling an older tag:
```
docker tag control-room:test YOUR_DOCKERHUB_USERNAME/control-room:v1
docker push YOUR_DOCKERHUB_USERNAME/control-room:v1
```

## 4. On the VPS (once it's provisioned)

MySQL runs as its **own container**, not installed on the VPS's OS at all -
`mysql:8.0` is a complete, self-contained MySQL server image. The app
container and the MySQL container talk to each other over a shared Docker
network, addressing each other **by container name** (Docker's internal
DNS resolves it) rather than by `127.0.0.1` or an IP - `127.0.0.1` inside
the app container would mean "myself", not "the other container".

### 4a. Network + persistent volume for MySQL's data

```bash
docker network create control-room-net
docker volume create control-room-mysql-data
```
The volume is what makes MySQL's data survive a container restart/
recreate - without it, removing the container would delete the database
too.

### 4b. Generate real credentials - you are CREATING these, not looking them up

The `MYSQL_ROOT_PASSWORD`/`MYSQL_USER`/`MYSQL_PASSWORD` values below are
read by the MySQL image's own startup script **the very first time** the
volume is empty - that one run is what actually creates the root account,
the `control_room` database, and the app user, with whatever values you
give it. Nothing pre-exists to discover; you're inventing a new database
server's credentials from scratch, same as signing up for any new
account. (Re-running the same command later, once the volume already has
data, silently ignores these vars - MySQL won't re-initialize an existing
data directory. If you need to change a password after this first run,
that's a normal MySQL `ALTER USER`/`SET PASSWORD` from inside the
container, not a re-run of this command.)

Generate two strong passwords rather than typing something guessable:
```bash
python3 -c "import secrets; print(''.join(secrets.choice('abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789') for _ in range(20)))"
```
Run it twice - once for the root password, once for the app user's
password.

Put both in a small file on the VPS **instead of typing them directly
into a command** (avoids them sitting in plain text in your shell
history):
```
# ~/mysql.env - not committed anywhere, lives only on the VPS
MYSQL_ROOT_PASSWORD=<paste the first generated password>
MYSQL_DATABASE=control_room
MYSQL_USER=controlroom
MYSQL_PASSWORD=<paste the second generated password>
```

### 4c. Start MySQL

```bash
docker run -d --name control-room-mysql --network control-room-net --restart unless-stopped \
    --env-file ~/mysql.env \
    -v control-room-mysql-data:/var/lib/mysql \
    mysql:8.0
```
Deliberately **no `-p 3306:3306`**. Without publishing that port, MySQL is
reachable only from other containers on `control-room-net` - never from
the internet. Don't add a published port unless you specifically need to
reach the database from outside the VPS (and even then, prefer
`-p 127.0.0.1:3306:3306` plus an SSH tunnel over opening it to the world).

Wait for it to actually be ready before the next step:
```bash
docker exec control-room-mysql mysqladmin ping -h localhost -u root -p"<root password from mysql.env>"
```
Retry every few seconds until it says `mysqld is alive`.

### 4d. Create the schema (once)

```bash
docker run --rm --network control-room-net \
    -e MYSQL_HOST=control-room-mysql -e MYSQL_PORT=3306 -e MYSQL_DATABASE=control_room \
    -e MYSQL_USER=controlroom -e MYSQL_PASSWORD="<app password from mysql.env>" \
    YOUR_DOCKERHUB_USERNAME/control-room:v1 python scripts/init_db.py
```
Expect `Creating N table(s) if they don't already exist. Done.`

### 4e. Real production `.env`, then run the app

The VPS's own `.env` (see `.env.example`) needs `MYSQL_HOST=control-room-mysql`
(the container name - **not** `127.0.0.1`, and not the two passwords'
placeholder values from local testing) along with every other real secret:
`FLASK_SECRET_KEY`, `EMAIL_ADDRESS`/`EMAIL_PASSWORD`, `PUBLIC_BASE_URL`,
`IMPORT_API_KEY`, and the MiX/Teletrac/FT Cloud credentials.

```bash
docker pull YOUR_DOCKERHUB_USERNAME/control-room:v1
docker run -d --name control-room-app --network control-room-net --restart unless-stopped -p 8080:8080 \
    --env-file .env \
    YOUR_DOCKERHUB_USERNAME/control-room:v1
```
`--restart unless-stopped` on both containers so the whole stack survives
a VPS reboot without manual intervention.

Create real accounts the same way as local testing, just against the
running production container:
```bash
docker exec control-room-app python users.py add justin admin "a-real-password"
```

### 4f. Still needed after this

A reverse proxy (Caddy or nginx) in front of port 8080 for real TLS/HTTPS
on your chosen subdomain, and pointing DNS at the VPS - neither is covered
here yet, next piece once the domain/subdomain is decided.
