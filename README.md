# Agent Library — Wings® Global Travel

Internal registry and catalogue for ChatGPT GPTs, Claude Skills, Copilot Agents
and other automation tools. This is the production Flask application converted
from the single-file HTML prototype (`agentlibrary_30.html`); the visual design
is preserved intact, everything behind it is new.

**Stack:** Rocky Linux 9 · Python 3.9+ · Flask · Gunicorn · Nginx · MySQL 8 ·
systemd · SELinux enforcing · firewalld · HTTPS

---

## Contents

1. [What changed from the prototype](#1-what-changed-from-the-prototype)
2. [Project layout](#2-project-layout)
3. [Development quick start](#3-development-quick-start)
4. [Rocky Linux 9 installation](#4-rocky-linux-9-installation)
5. [Database setup](#5-database-setup)
6. [Migrations](#6-migrations)
7. [Initial administrator](#7-initial-administrator)
8. [systemd](#8-systemd)
9. [Nginx](#9-nginx)
10. [firewalld](#10-firewalld)
11. [SELinux](#11-selinux)
12. [Gunicorn test command](#12-gunicorn-test-command)
13. [Health checks](#13-health-checks)
14. [Backup and restore](#14-backup-and-restore)
15. [Rollback](#15-rollback)
16. [Malware scanning](#16-malware-scanning)
17. [API reference](#17-api-reference)
18. [Testing](#18-testing)
19. [Troubleshooting](#19-troubleshooting)
20. [Production-readiness checklist](#20-production-readiness-checklist)

Design analysis, the localStorage/mock-data inventory, the UI-action → route
map, the schema and the migration plan are in **[`docs/ANALYSIS.md`](docs/ANALYSIS.md)**.

---

## 1. What changed from the prototype

| Prototype | Now |
| --- | --- |
| `localStorage` DataStore | MySQL via SQLAlchemy; every read/write is a Flask API call |
| 16 hard-coded mock assets | Empty database; **no code path seeds demo assets** |
| `AdminAuth` comparing `"Wings123+"` in page source | Flask-Login sessions, PBKDF2-SHA256 hashes, `user`/`reviewer`/`admin` roles |
| Client-side `State.unlocked` flag | Server-enforced authorisation on every protected endpoint |
| Client-computed WGT codes (`max()+1`) | Server allocation inside a transaction, row-locked counter, `UNIQUE` constraint, bounded retry |
| Simulated downloads (a toast) | Real uploads with validation and authorised, audited downloads |
| `versions[0]` overwritten by approve/reject | Append-only `asset_versions` table |
| Activity log written but never shown | `activity_log` table + an Admin activity view |
| "My Submissions" mentioned in a comment only | Implemented, scoped server-side |
| No CSRF, no security headers | CSRF on every state-changing request, CSP, HSTS, nosniff, DENY framing |

Preserved exactly: the boarding-pass card design, all five detail tabs, the
two-step wizard, filters, card/list views, toasts, confirmation modals,
keyboard accessibility, `/` search shortcut, browser back/forward, and the
responsive breakpoints.

---

## 2. Project layout

```
agentlibrary/
├── wsgi.py                      # gunicorn / flask CLI entry point
├── gunicorn.conf.py             # worker, timeout, bind and logging config
├── requirements.txt             # pinned runtime dependencies
├── requirements-dev.txt         # + pytest, flake8
├── pyproject.toml / setup.cfg   # pytest, coverage, flake8 configuration
├── .env.example                 # environment template (no real secrets)
│
├── app/
│   ├── __init__.py              # application factory
│   ├── config.py                # Development / Testing / Production config
│   ├── extensions.py            # db, migrate, csrf, login_manager, limiter
│   ├── models.py                # 9 tables + serialisation
│   ├── reference.py             # server-side CONFIG (departments, tags, ...)
│   ├── validators.py            # server-side validation
│   ├── security.py              # role decorators, CSP, host guard, IDOR guards
│   ├── audit.py                 # activity_log helpers
│   ├── wgt.py                   # WGT code allocation (race-safe)
│   ├── storage.py               # private file storage + ClamAV
│   ├── errors.py                # {ok, data} / {ok, error} envelopes
│   ├── logging_config.py        # stdout + rotating file sinks
│   ├── cli.py                   # create-admin, seed-config, import-legacy
│   └── api/
│       ├── meta.py              # /api/config, /health
│       ├── auth.py              # login, logout, me, change-password
│       ├── assets.py            # the /api/assets surface, /api/my-submissions
│       ├── files.py             # upload, download, delete
│       └── admin.py             # activity, stats, users, dept migration
│
├── templates/index.html         # SPA shell (no catalogue data)
├── static/css/app.css           # the prototype's stylesheet, extracted
├── static/js/app.js             # the prototype's UI, now fetch()-driven
├── static/images/wings-logo.png # extracted from the prototype's data URI
├── static/fonts/README.md       # how to self-host the webfonts
│
├── migrations/versions/0001_initial_schema.py
├── deploy/                      # systemd, nginx, logrotate, SELinux
├── scripts/backup.sh, restore.sh
├── tests/                       # 202 tests
└── docs/ANALYSIS.md             # prototype analysis + migration plan
```

---

## 3. Development quick start

```bash
git clone <repo> agentlibrary && cd agentlibrary

python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements-dev.txt

cp .env.example .env
# Edit .env: set FLASK_ENV=development, SESSION_COOKIE_SECURE=0, and a
# SECRET_KEY. For a local run without MySQL, set:
#   DATABASE_URL=sqlite:///var/dev.db
#   UPLOAD_DIR=./var/uploads
python -c "import secrets; print('SECRET_KEY=' + secrets.token_urlsafe(64))"

mkdir -p var/uploads
export FLASK_APP=wsgi.py FLASK_ENV=development
flask db upgrade                 # create the schema
flask seed-config                # reference data only — never demo assets
flask create-admin --email you@wingsglobaltravel.com --name "Your Name"
                                 # prompts for the password; nothing is stored in source

flask run --port 5000            # or: python wsgi.py
```

Open <http://127.0.0.1:5000>, sign in with the admin account you just created.

Useful during development:

```bash
pytest -q                                    # full suite
pytest -q tests/test_wgt_codes.py            # one file
pytest --cov=app --cov-report=term-missing   # coverage
flake8 app tests wsgi.py gunicorn.conf.py    # lint
flask show-status                            # inventory
```

**`.env` is only ever read when `FLASK_ENV` is `development` or `testing`.** In
production the environment comes from systemd. `.env` is git-ignored.

---

## 4. Rocky Linux 9 installation

### 4.1 System packages

```bash
sudo dnf -y update
sudo dnf -y install epel-release
sudo dnf -y groupinstall "Development Tools"

# Python. Rocky 9 ships 3.9 as `python3`; 3.12 is available from AppStream and
# is the better choice for a new deployment. Use ONE of these.
sudo dnf -y install python3 python3-pip python3-devel                 # 3.9
# sudo dnf -y install python3.12 python3.12-pip python3.12-devel      # 3.12

# MySQL client libraries and build headers (PyMySQL is pure Python, but
# mysqldump/mysql are needed for backup and restore).
sudo dnf -y install mysql mysql-devel

# Web tier and utilities
sudo dnf -y install nginx openssl curl git tar gzip logrotate \
                    policycoreutils-python-utils firewalld

# Optional: TLS via Let's Encrypt
sudo dnf -y install certbot python3-certbot-nginx

# Optional: malware scanning (see section 16)
# sudo dnf -y install clamav clamav-update clamd

sudo systemctl enable --now firewalld
sudo systemctl enable --now nginx
```

### 4.2 Service account

```bash
# System account: no login shell, no home directory to log into.
sudo useradd --system --no-create-home --shell /sbin/nologin \
             --comment "Agent Library service account" agentlibrary

# nginx must reach the gunicorn socket, so it joins the group.
sudo usermod -a -G agentlibrary nginx

id agentlibrary
```

### 4.3 Directories

```bash
sudo mkdir -p /opt/agentlibrary                 # application code
sudo mkdir -p /var/lib/agentlibrary/uploads     # private file store
sudo mkdir -p /var/log/agentlibrary             # application log
sudo mkdir -p /etc/agentlibrary                 # environment file
sudo mkdir -p /var/backups/agentlibrary         # backups

sudo chown -R agentlibrary:agentlibrary /opt/agentlibrary \
                                        /var/lib/agentlibrary \
                                        /var/log/agentlibrary
sudo chmod 750 /opt/agentlibrary /var/log/agentlibrary
sudo chmod 750 /var/lib/agentlibrary /var/lib/agentlibrary/uploads
sudo chmod 700 /var/backups/agentlibrary
sudo chmod 750 /etc/agentlibrary

# nginx reads only the static tree.
sudo chmod 755 /opt/agentlibrary/static 2>/dev/null || true
```

### 4.4 Deploy the code

```bash
sudo -u agentlibrary git clone <repo-url> /opt/agentlibrary
# or: sudo tar -xzf agentlibrary.tar.gz -C /opt/agentlibrary --strip-components=1
cd /opt/agentlibrary
sudo chown -R agentlibrary:agentlibrary /opt/agentlibrary
```

### 4.5 Virtual environment and dependencies

```bash
sudo -u agentlibrary python3 -m venv /opt/agentlibrary/.venv
sudo -u agentlibrary /opt/agentlibrary/.venv/bin/pip install --upgrade pip wheel
sudo -u agentlibrary /opt/agentlibrary/.venv/bin/pip install -r /opt/agentlibrary/requirements.txt

# Verify
sudo -u agentlibrary /opt/agentlibrary/.venv/bin/python -c \
  "import flask, sqlalchemy, pymysql, gunicorn; print('dependencies OK')"
```

### 4.6 Environment file

```bash
sudo cp /opt/agentlibrary/.env.example /etc/agentlibrary/agentlibrary.env
sudo chown root:agentlibrary /etc/agentlibrary/agentlibrary.env
sudo chmod 640 /etc/agentlibrary/agentlibrary.env    # NOT world-readable

# Generate a real SECRET_KEY and a database password
python3 -c "import secrets; print(secrets.token_urlsafe(64))"
python3 -c "import secrets; print(secrets.token_urlsafe(24))"

sudo vi /etc/agentlibrary/agentlibrary.env
```

At minimum set:

```ini
FLASK_ENV=production
SECRET_KEY=<the 64-char value you just generated>
MYSQL_PASSWORD=<the database password>
TRUSTED_HOSTS=agentlibrary.wingsglobaltravel.com
SESSION_COOKIE_SECURE=1
UPLOAD_DIR=/var/lib/agentlibrary/uploads
LOG_DIR=/var/log/agentlibrary
```

The app **refuses to start** in production without a real `SECRET_KEY`.

### How this file gets loaded

| How you start it | What reads the file |
| --- | --- |
| `systemctl start agentlibrary` | systemd, via `EnvironmentFile=` in the unit |
| `gunicorn -c gunicorn.conf.py wsgi:application` | the app itself |
| `flask db upgrade`, `flask create-admin`, … | the app itself |

Both `gunicorn.conf.py` and `create_app()` read
`/etc/agentlibrary/agentlibrary.env` automatically, so a command run by hand
behaves the same as the service. Nothing already present in the environment is
overwritten, so systemd and deliberate `export`s still win.

To point at a different file — a staging copy, or a path of your own — set
`ENV_FILE`:

```bash
ENV_FILE=/etc/agentlibrary/staging.env .venv/bin/gunicorn -c gunicorn.conf.py wsgi:application
```

`ENV_FILE` is strict: if the path does not exist, startup fails saying so,
rather than falling through to a confusing `SECRET_KEY is not set`.

> The file is mode `0640` owned `root:agentlibrary`, so **your own account
> probably cannot read it**. Run these commands as the service account
> (`sudo -u agentlibrary ...`), or add yourself to the group with
> `sudo usermod -a -G agentlibrary $USER` and log out and back in. If the file
> cannot be read, the startup error says exactly that.

Values containing spaces or `#` should be quoted, so that systemd and the
application's parser agree:

```ini
MYSQL_PASSWORD="p@ss word#1"
```

---

## 5. Database setup

```bash
sudo dnf -y install mysql-server        # skip if MySQL is on another host
sudo systemctl enable --now mysqld
sudo mysql_secure_installation
```

Create the database and a **least-privilege** user:

```bash
sudo mysql -u root -p
```

```sql
CREATE DATABASE agentlibrary
  CHARACTER SET utf8mb4
  COLLATE utf8mb4_0900_ai_ci;

-- Bind the account to loopback: it can never be used from another machine.
CREATE USER 'agentlibrary'@'127.0.0.1' IDENTIFIED BY 'REPLACE_WITH_THE_GENERATED_PASSWORD';

-- Exactly the rights the application uses at runtime, and nothing else.
-- No DROP, no GRANT, no FILE, no SUPER, no PROCESS.
GRANT SELECT, INSERT, UPDATE, DELETE ON agentlibrary.* TO 'agentlibrary'@'127.0.0.1';

-- Migrations need DDL. Two options:
--   (a) Grant it permanently (simpler, slightly more privilege):
GRANT CREATE, ALTER, INDEX, REFERENCES, DROP ON agentlibrary.* TO 'agentlibrary'@'127.0.0.1';
--   (b) Preferred: leave the runtime account without DDL and run migrations
--       as a separate account that has it, then revoke. See section 6.

FLUSH PRIVILEGES;
SHOW GRANTS FOR 'agentlibrary'@'127.0.0.1';
EXIT;
```

Verify connectivity as the service account:

```bash
mysql -h 127.0.0.1 -u agentlibrary -p agentlibrary -e "SELECT VERSION(), DATABASE();"
```

Recommended `/etc/my.cnf.d/agentlibrary.cnf`:

```ini
[mysqld]
bind-address              = 127.0.0.1
character-set-server      = utf8mb4
collation-server          = utf8mb4_0900_ai_ci
default-storage-engine    = InnoDB
innodb_file_per_table     = 1
max_connections           = 151
wait_timeout              = 28800
local_infile              = 0
```

```bash
sudo systemctl restart mysqld
```

> `DB_POOL_RECYCLE` (280s) must stay well below `wait_timeout`, otherwise
> workers hand out connections MySQL has already closed and you get
> intermittent "MySQL server has gone away".

---

## 6. Migrations

```bash
cd /opt/agentlibrary
export FLASK_APP=wsgi.py

# What is applied now, and what exists
sudo -u agentlibrary env FLASK_APP=wsgi.py .venv/bin/flask db current
sudo -u agentlibrary env FLASK_APP=wsgi.py .venv/bin/flask db history

# Apply everything
sudo -u agentlibrary env FLASK_APP=wsgi.py .venv/bin/flask db upgrade

# Seed reference data (departments, tags, model tiers, WGT counters).
# This command CANNOT create assets — there is no demo-data path.
sudo -u agentlibrary env FLASK_APP=wsgi.py .venv/bin/flask seed-config
```

Verify:

```bash
mysql -h 127.0.0.1 -u agentlibrary -p agentlibrary -e "SHOW TABLES;"
# users, assets, asset_versions, asset_files, tags, asset_tags,
# activity_log, wgt_counters, config_settings, alembic_version
```

**Preferred: run migrations with a DDL account, then keep runtime least-privilege.**

```bash
# One-off, using a separate migration account
sudo -u agentlibrary env FLASK_APP=wsgi.py \
  DATABASE_URL="mysql+pymysql://al_migrate:PASS@127.0.0.1:3306/agentlibrary?charset=utf8mb4" \
  .venv/bin/flask db upgrade
```

Creating a new migration after a model change:

```bash
sudo -u agentlibrary env FLASK_APP=wsgi.py .venv/bin/flask db migrate -m "add x to assets"
# ALWAYS read the generated file before applying it — autogenerate misses
# server defaults, column renames (it drops and recreates) and index changes.
sudo -u agentlibrary env FLASK_APP=wsgi.py .venv/bin/flask db upgrade
```

---

## 7. Initial administrator

No password is ever hard-coded, defaulted, or written into source.

**Interactive (preferred):**

```bash
cd /opt/agentlibrary
sudo -u agentlibrary env FLASK_APP=wsgi.py .venv/bin/flask \
    create-admin --email admin@wingsglobaltravel.com --name "Governance Admin"
# New password (min 12 chars): ********
# Confirm password: ********
# Administrator ready: admin@wingsglobaltravel.com
```

**Non-interactive** (a deployment pipeline with a secrets manager) — the
password comes from the environment and is removed immediately afterwards:

```bash
sudo -u agentlibrary env FLASK_APP=wsgi.py \
    ADMIN_INITIAL_PASSWORD="$(vault kv get -field=password secret/agentlibrary/admin)" \
    .venv/bin/flask create-admin --email admin@wingsglobaltravel.com
```

Never leave `ADMIN_INITIAL_PASSWORD` in `agentlibrary.env`.

Other account commands:

```bash
flask create-user --email rob@wingsglobaltravel.com --role reviewer
flask set-role --email rob@wingsglobaltravel.com --role admin
flask reset-password --email rob@wingsglobaltravel.com
```

Roles:

| Role | Can |
| --- | --- |
| `user` | browse, submit, edit their own pending/rejected submissions, upload files to their own assets |
| `reviewer` | all of the above, plus approve / reject / archive / feature any asset and read the activity log |
| `admin` | all of the above, plus manage user accounts and perform controlled department migrations |

---

## 8. systemd

```bash
sudo install -m 0644 /opt/agentlibrary/deploy/agentlibrary.service /etc/systemd/system/
sudo install -m 0644 /opt/agentlibrary/deploy/agentlibrary-logrotate /etc/logrotate.d/agentlibrary

sudo systemctl daemon-reload
sudo systemctl enable --now agentlibrary
sudo systemctl status agentlibrary
```

The unit:

* runs as **`agentlibrary`**, never root (`User=` / `Group=`);
* binds a **Unix socket** in `/run/agentlibrary` (`RuntimeDirectory=` creates it
  at start and removes it at stop, so a stale socket can never linger);
* reads secrets from `EnvironmentFile=/etc/agentlibrary/agentlibrary.env`;
* `Restart=always` with `RestartSec=5` and a start-limit so a bad config does
  not spin forever;
* `TimeoutStopSec=45` exceeds gunicorn's 30s `graceful_timeout`, so in-flight
  requests finish instead of being SIGKILLed;
* is hardened with `ProtectSystem=strict`, `PrivateTmp`, `NoNewPrivileges`,
  an empty `CapabilityBoundingSet` and `SystemCallFilter=@system-service`.
  `ReadWritePaths=` names the only writable locations.

### Inspecting logs with journalctl

```bash
journalctl -u agentlibrary -f                      # follow live
journalctl -u agentlibrary -n 200 --no-pager       # last 200 lines
journalctl -u agentlibrary --since "10 min ago"
journalctl -u agentlibrary --since today -p err    # errors only
journalctl -u agentlibrary -o json-pretty -n 5     # full structured records
journalctl -u agentlibrary --grep "audit action"   # the audit trail
journalctl -u agentlibrary -b                      # since this boot
journalctl --disk-usage && sudo journalctl --vacuum-time=30d
```

Common operations:

```bash
sudo systemctl reload agentlibrary     # SIGHUP: reload workers, keep the socket
sudo systemctl restart agentlibrary
sudo systemctl stop agentlibrary
systemd-analyze verify /etc/systemd/system/agentlibrary.service
```

---

## 9. Nginx

```bash
sudo install -m 0644 /opt/agentlibrary/deploy/agentlibrary-proxy.inc /etc/nginx/conf.d/
sudo install -m 0644 /opt/agentlibrary/deploy/nginx-agentlibrary.conf /etc/nginx/conf.d/agentlibrary.conf

sudo vi /etc/nginx/conf.d/agentlibrary.conf   # set server_name and cert paths

sudo nginx -t                    # ALWAYS validate before reloading
sudo systemctl reload nginx      # zero-downtime
```

The configuration:

* listens on **80 and 443**; :80 serves only the ACME challenge and the health
  check and **redirects everything else to HTTPS** (before TLS exists, comment
  out the redirect and uncomment the proxy block, both marked in the file);
* proxies to the gunicorn Unix socket via an `upstream` with keepalive;
* serves **only `/static/`** from disk, with execution of `.php/.py/.sh` denied
  and dotfiles blocked;
* **never exposes the upload directory** — `/var/lib/agentlibrary/uploads` has
  no URL mapping at all, and a catch-all `location` returns 404 for anything
  resembling it. Downloads go through `/api/files/<id>/download`, which checks
  authorisation and writes an audit record;
* sets `client_max_body_size 25m`, matching `MAX_CONTENT_LENGTH=26214400`;
* forwards `Host`, `X-Real-IP`, `X-Forwarded-For` and `X-Forwarded-Proto` (see
  `agentlibrary-proxy.inc`, included by every proxying location so they cannot
  drift apart);
* adds HSTS, `X-Content-Type-Options`, `X-Frame-Options: DENY`,
  `Referrer-Policy`, `Permissions-Policy` with `always`, so they apply to
  nginx-generated errors too;
* rate-limits `/api/auth/login` and `/api/auth/change-password` to 10/min and
  the rest of `/api/` to 60/s.

### Certificates

**Organisation-issued (preferred):**

```bash
sudo install -m 0644 -o root -g root agentlibrary.crt /etc/pki/tls/certs/agentlibrary.crt
sudo install -m 0600 -o root -g root agentlibrary.key /etc/pki/tls/private/agentlibrary.key
# The .crt must be the leaf followed by the intermediate chain, in that order.
sudo restorecon -v /etc/pki/tls/certs/agentlibrary.crt /etc/pki/tls/private/agentlibrary.key
sudo nginx -t && sudo systemctl reload nginx
```

Verify the chain before reloading:

```bash
openssl x509 -in /etc/pki/tls/certs/agentlibrary.crt -noout -subject -issuer -dates
openssl verify -untrusted /etc/pki/tls/certs/agentlibrary.crt /etc/pki/tls/certs/agentlibrary.crt
# Key and certificate must match:
openssl x509 -noout -modulus -in /etc/pki/tls/certs/agentlibrary.crt | openssl md5
openssl rsa  -noout -modulus -in /etc/pki/tls/private/agentlibrary.key | openssl md5
```

**Certbot (where a public hostname and outbound access exist):**

```bash
sudo mkdir -p /var/lib/letsencrypt
sudo certbot --nginx -d agentlibrary.wingsglobaltravel.com \
     --agree-tos -m tech@wingsglobaltravel.com --redirect
sudo systemctl enable --now certbot-renew.timer
sudo certbot renew --dry-run
```

Confirm from outside:

```bash
curl -sI https://agentlibrary.wingsglobaltravel.com | head -20
curl -sI http://agentlibrary.wingsglobaltravel.com | grep -i location   # 301 to https
openssl s_client -connect agentlibrary.wingsglobaltravel.com:443 -servername agentlibrary.wingsglobaltravel.com </dev/null 2>/dev/null | openssl x509 -noout -dates
```

---

## 10. firewalld

```bash
sudo systemctl enable --now firewalld
sudo firewall-cmd --state

# Open only HTTP and HTTPS. MySQL and gunicorn stay unreachable from outside.
sudo firewall-cmd --permanent --add-service=http
sudo firewall-cmd --permanent --add-service=https
sudo firewall-cmd --permanent --add-service=ssh          # keep your way in

sudo firewall-cmd --reload
sudo firewall-cmd --list-all
```

Confirm nothing else is exposed:

```bash
sudo firewall-cmd --list-ports          # expect empty
sudo ss -tlnp | grep -E '3306|8000'     # MySQL/gunicorn must be loopback or absent
```

If MySQL is on a separate host, allow only this application server:

```bash
sudo firewall-cmd --permanent --zone=internal \
     --add-rich-rule='rule family="ipv4" source address="10.0.0.0/24" service name="mysql" accept'
sudo firewall-cmd --reload
```

---

## 11. SELinux

Keep SELinux **enforcing**. Do not set permissive mode to "fix" a problem —
read the denial and set the correct context or boolean.

### Check status

```bash
getenforce                    # expect: Enforcing
sestatus
sudo semodule -l | head
```

### Set the contexts

SELinux decides by *label*, not by path. Four locations need labels other than
the defaults they inherit:

```bash
# 1. Static assets. /opt is not a web root, so files there get usr_t and nginx
#    is denied read access (403 + an AVC). httpd_sys_content_t is the label
#    nginx is permitted to read.
sudo semanage fcontext -a -t httpd_sys_content_t "/opt/agentlibrary/static(/.*)?"

# 2. The gunicorn socket directory. httpd_var_run_t is what nginx may connect
#    to. /run is a tmpfs recreated at every boot, so the semanage rule — not
#    restorecon alone — is what makes this survive a reboot.
sudo semanage fcontext -a -t httpd_var_run_t "/run/agentlibrary(/.*)?"

# 3. The private upload store, written by the app and read by nobody else.
sudo semanage fcontext -a -t var_lib_t "/var/lib/agentlibrary(/.*)?"

# 4. Application logs.
sudo semanage fcontext -a -t httpd_log_t "/var/log/agentlibrary(/.*)?"

# Apply the rules to the files that already exist.
sudo restorecon -Rv /opt/agentlibrary/static \
                    /var/lib/agentlibrary \
                    /var/log/agentlibrary
sudo restorecon -Rv /run/agentlibrary 2>/dev/null || true
```

### Set the booleans

```bash
# Lets nginx (httpd_t) open a connection to the gunicorn socket. Without this,
# every request returns 502 with "Permission denied" in the nginx error log.
sudo setsebool -P httpd_can_network_connect 1

# Only if the app must reach MySQL on ANOTHER host:
# sudo setsebool -P httpd_can_network_connect_db 1

sudo getsebool -a | grep httpd_can_network
```

### Verify the labels

```bash
ls -Zd /opt/agentlibrary/static /var/lib/agentlibrary/uploads /run/agentlibrary
sudo semanage fcontext -l | grep agentlibrary
ps -eZ | grep gunicorn
```

### When something is still denied

```bash
sudo ausearch -m AVC -ts recent
sudo ausearch -m AVC -ts recent | audit2why      # explains it, names the boolean
sudo journalctl -t setroubleshoot -n 50
```

Only if contexts and booleans genuinely cannot express what is needed, build a
module from the *actual* denials (never from guesswork) —
see [`deploy/selinux/README.md`](deploy/selinux/README.md):

```bash
sudo ausearch -m AVC -ts recent | audit2allow -M agentlibrary_local
cat agentlibrary_local.te        # READ THIS before installing it
sudo semodule -i agentlibrary_local.pp
```

---

## 12. Gunicorn test command

Before wiring systemd, prove gunicorn serves the app.

**Foreground on the loopback interface:**

```bash
cd /opt/agentlibrary
sudo -u agentlibrary GUNICORN_BIND=127.0.0.1:8000 \
     .venv/bin/gunicorn --config gunicorn.conf.py wsgi:application
```

In a second shell:

```bash
curl -s http://127.0.0.1:8000/health
# {"checks":{"database":"ok"},"status":"ok","version":"1.0.0"}
curl -s http://127.0.0.1:8000/api/config | head -c 200
curl -sI http://127.0.0.1:8000/ | head -5
```

Then Ctrl-C and test the **Unix socket** exactly as systemd will run it:

```bash
sudo mkdir -p /run/agentlibrary
sudo chown agentlibrary:agentlibrary /run/agentlibrary
sudo chmod 750 /run/agentlibrary

sudo -u agentlibrary .venv/bin/gunicorn --config gunicorn.conf.py wsgi:application
```

```bash
# Talk to the socket directly
curl -s --unix-socket /run/agentlibrary/agentlibrary.sock http://localhost/health
ls -lZ /run/agentlibrary/agentlibrary.sock       # srw-rw---- agentlibrary agentlibrary

# Confirm nginx's user can reach it
sudo -u nginx curl -s --unix-socket /run/agentlibrary/agentlibrary.sock http://localhost/health
```

`gunicorn.conf.py` **refuses to start** if `GUNICORN_BIND` names a public
interface — a public bind would skip TLS, the security headers and the request
size limit.

Check the configuration without serving:

```bash
sudo -u agentlibrary .venv/bin/gunicorn --config gunicorn.conf.py --print-config wsgi:application
sudo -u agentlibrary .venv/bin/gunicorn --config gunicorn.conf.py --check-config wsgi:application
```

---

## 13. Health checks

```bash
# Through the socket (bypasses nginx — isolates app vs proxy problems)
curl -s --unix-socket /run/agentlibrary/agentlibrary.sock http://localhost/health

# Through nginx over HTTP
curl -s http://agentlibrary.wingsglobaltravel.com/health

# Through nginx over HTTPS
curl -s https://agentlibrary.wingsglobaltravel.com/health | python3 -m json.tool

# Just the status code, for a monitor
curl -s -o /dev/null -w '%{http_code}\n' https://agentlibrary.wingsglobaltravel.com/health
```

Healthy:

```json
{"status": "ok", "version": "1.0.0", "checks": {"database": "ok"}}
```

`200` = application up **and** database reachable.
`503` with `"status":"degraded"` = application up, database not.
No response = gunicorn or nginx is down.

The endpoint needs no authentication and returns no configuration, hostnames or
credentials, so it is safe to expose to a load balancer.

Fuller post-deploy verification:

```bash
systemctl is-active agentlibrary nginx mysqld
curl -sI https://agentlibrary.wingsglobaltravel.com/ | grep -Ei 'HTTP/|strict-transport|x-frame|content-security'
curl -s https://agentlibrary.wingsglobaltravel.com/api/config | head -c 120
sudo -u agentlibrary env FLASK_APP=wsgi.py /opt/agentlibrary/.venv/bin/flask show-status
```

---

## 14. Backup and restore

### Automated backup

```bash
sudo install -m 0750 -o root -g root /opt/agentlibrary/scripts/backup.sh /opt/agentlibrary/scripts/backup.sh
sudo /opt/agentlibrary/scripts/backup.sh                       # default /var/backups/agentlibrary
sudo /opt/agentlibrary/scripts/backup.sh /mnt/nfs/backups      # elsewhere
```

Schedule it (03:15 daily, 30-day retention):

```bash
sudo crontab -e
```

```cron
15 3 * * * /opt/agentlibrary/scripts/backup.sh >> /var/log/agentlibrary/backup.log 2>&1
```

Each run produces a timestamped directory containing `database.sql.gz`,
`uploads.tar.gz`, a `manifest.txt` recording the Alembic head, and
`SHA256SUMS`. The dump uses `--single-transaction`, so it is consistent without
locking writers. Credentials are passed through a temporary `--defaults-extra-file`
so they never appear in `ps`.

### Manual backup

```bash
# Database
mysqldump -h 127.0.0.1 -u agentlibrary -p \
    --single-transaction --quick --routines --triggers \
    --default-character-set=utf8mb4 agentlibrary \
    | gzip -9 > agentlibrary-$(date +%F).sql.gz

# Files
sudo tar -czf uploads-$(date +%F).tar.gz -C /var/lib/agentlibrary uploads
```

### Restore

```bash
sudo /opt/agentlibrary/scripts/restore.sh /var/backups/agentlibrary/20260820-031500
```

The script verifies checksums, stops the service, takes a safety dump of the
*current* database first, restores both database and files, runs any
outstanding migrations, restarts, and health-checks. It refuses to proceed
unless you type `RESTORE`.

Manual restore:

```bash
sudo systemctl stop agentlibrary
gunzip -c agentlibrary-2026-08-20.sql.gz \
    | mysql -h 127.0.0.1 -u agentlibrary -p --default-character-set=utf8mb4 agentlibrary
sudo tar -xzf uploads-2026-08-20.tar.gz -C /var/lib/agentlibrary
sudo chown -R agentlibrary:agentlibrary /var/lib/agentlibrary/uploads
sudo restorecon -R /var/lib/agentlibrary
sudo -u agentlibrary env FLASK_APP=wsgi.py /opt/agentlibrary/.venv/bin/flask db upgrade
sudo systemctl start agentlibrary
curl -s --unix-socket /run/agentlibrary/agentlibrary.sock http://localhost/health
```

**Test the restore on a staging host quarterly.** An untested backup is a
hypothesis, not a backup.

---

## 15. Rollback

### Application code

```bash
cd /opt/agentlibrary
sudo -u agentlibrary git log --oneline -10
sudo -u agentlibrary git checkout <previous-good-tag>
sudo -u agentlibrary .venv/bin/pip install -r requirements.txt   # deps may have changed
sudo systemctl restart agentlibrary
curl -s --unix-socket /run/agentlibrary/agentlibrary.sock http://localhost/health
```

### Database schema

```bash
cd /opt/agentlibrary
export FLASK_APP=wsgi.py

sudo -u agentlibrary env FLASK_APP=wsgi.py .venv/bin/flask db current
sudo -u agentlibrary env FLASK_APP=wsgi.py .venv/bin/flask db downgrade -1     # one step
sudo -u agentlibrary env FLASK_APP=wsgi.py .venv/bin/flask db downgrade <rev>  # to a revision
```

> **Take a backup before any downgrade.** A downgrade that drops a column
> destroys the data in it; Alembic cannot bring it back.

### Full rollback (code + schema + data)

```bash
sudo systemctl stop agentlibrary
sudo /opt/agentlibrary/scripts/restore.sh /var/backups/agentlibrary/<pre-deploy-backup>
cd /opt/agentlibrary && sudo -u agentlibrary git checkout <previous-good-tag>
sudo -u agentlibrary .venv/bin/pip install -r requirements.txt
sudo systemctl start agentlibrary
curl -s --unix-socket /run/agentlibrary/agentlibrary.sock http://localhost/health
```

### Nginx

```bash
sudo cp /etc/nginx/conf.d/agentlibrary.conf.bak /etc/nginx/conf.d/agentlibrary.conf
sudo nginx -t && sudo systemctl reload nginx
```

**Always take a pre-deploy backup**, so a rollback is a restore rather than an
improvisation:

```bash
sudo /opt/agentlibrary/scripts/backup.sh
```

---

## 16. Malware scanning

Uploads are validated by extension, sniffed MIME type, magic bytes and size
(`app/storage.py`), and executable content is refused outright. Content scanning
is a separate layer, disabled by default.

### Enabling ClamAV

```bash
sudo dnf -y install clamav clamav-update clamd

# Signature database
sudo freshclam
sudo systemctl enable --now clamav-freshclam

# Listen on loopback TCP, which is what app/storage.py speaks to.
sudo vi /etc/clamd.d/scan.conf
#   TCPSocket 3310
#   TCPAddr 127.0.0.1
#   LocalSocket /run/clamd.scan/clamd.sock
#   (comment out the `Example` line)

sudo systemctl enable --now clamd@scan
sudo ss -tlnp | grep 3310
```

Then in `/etc/agentlibrary/agentlibrary.env`:

```ini
CLAMAV_ENABLED=1
CLAMAV_HOST=127.0.0.1
CLAMAV_PORT=3310
CLAMAV_TIMEOUT=30
```

```bash
sudo systemctl restart agentlibrary
```

### Behaviour

`storage.scan_file()` streams the file to clamd over the INSTREAM protocol
**before** the database row is committed:

* clean → `scan_status='clean'`, upload proceeds;
* infected → the file is deleted from disk, the row is never written, the
  client gets `400 FILE_INFECTED`, and the attempt is audited;
* scanner unreachable → the upload is **refused** with `503 SCAN_UNAVAILABLE`.
  This fails closed on purpose: accepting unscanned files while the scanner is
  down defeats the control. If you prefer fail-open, change the `except` block
  in `storage.scan_file()` and document the decision.

Test it with the EICAR string (a harmless standard test file):

```bash
printf 'X5O!P%%@AP[4\\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*' > /tmp/eicar.txt
clamdscan /tmp/eicar.txt        # expect: Eicar-Signature FOUND
```

### Alternatives

* **ICAP gateway** (McAfee Web Gateway, Symantec Protection Engine): replace
  the socket protocol in `scan_file()` with an ICAP `RESPMOD` request.
* **Asynchronous scanning**: store with `scan_status='pending'`, block download
  until a worker flips it to `clean` — the download endpoint already refuses
  `infected` files, so add `pending` to that check.
* **Defender for Endpoint** on-access scanning of `/var/lib/agentlibrary/uploads`
  catches files at rest, but does not stop them being served in the window
  before detection.

---

## 17. API reference

All responses use one of two envelopes:

```json
{"ok": true,  "data": {}}
{"ok": false, "error": {"code": "VALIDATION_ERROR", "message": "…", "fields": {}}}
```

| Method | Path | Auth | Notes |
| --- | --- | --- | --- |
| GET | `/health` | none | liveness + database |
| GET | `/api/config` | none | reference lists and limits |
| POST | `/api/auth/login` | none | rate-limited |
| POST | `/api/auth/logout` | session | |
| GET | `/api/auth/me` | none | `user: null` when anonymous |
| GET | `/api/auth/csrf` | none | fresh token |
| POST | `/api/auth/change-password` | session | invalidates other sessions |
| GET | `/api/assets` | browse | `q`, `asset_type`, `department`, `platform`, `tag`, `status`, `featured`, `review_due`, `sort`, `page`, `per_page` |
| GET | `/api/assets/<id>` | browse¹ | full detail, all five tabs |
| POST | `/api/assets` | user | allocates the WGT code |
| PUT | `/api/assets/<id>` | owner/reviewer | department, code and type are immutable |
| POST | `/api/assets/<id>/versions` | owner/reviewer | append-only |
| GET | `/api/assets/<id>/files` | browse¹ | |
| POST | `/api/assets/<id>/files` | owner/reviewer | multipart/form-data |
| GET | `/api/files/<id>/download` | browse¹ | audited; `?disposition=inline` |
| DELETE | `/api/files/<id>` | owner/reviewer | |
| POST | `/api/assets/<id>/approve` | reviewer | |
| POST | `/api/assets/<id>/reject` | reviewer | optional `reason` |
| POST | `/api/assets/<id>/archive` | reviewer | "Remove from Library" |
| GET | `/api/assets/next-code` | user | preview only, not a reservation |
| GET | `/api/assets/duplicate-check` | user | |
| GET | `/api/my-submissions` | user | scoped server-side |
| GET | `/api/admin/activity` | reviewer | audit trail |
| GET | `/api/admin/stats` | reviewer | dashboard counters |
| GET/POST | `/api/admin/users` | admin | |
| PATCH | `/api/admin/users/<id>` | admin | |
| POST | `/api/admin/assets/<id>/migrate-department` | admin | mints a new WGT code |

¹ Approved assets are visible to anyone who may browse; anything else only to
its creator/owner or a reviewer. Requesting a hidden asset returns **404**, not
403, so the endpoint does not confirm that it exists.

Error codes: `VALIDATION_ERROR`, `AUTH_REQUIRED`, `INVALID_CREDENTIALS`,
`ACCOUNT_LOCKED`, `ACCOUNT_DISABLED`, `FORBIDDEN`, `NOT_FOUND`, `CONFLICT`,
`DEPARTMENT_IMMUTABLE`, `IMMUTABLE_FIELD`, `VERSION_EXISTS`,
`WGT_ALLOCATION_FAILED`, `FILE_LIMIT_REACHED`, `FILE_INFECTED`,
`SCAN_UNAVAILABLE`, `PAYLOAD_TOO_LARGE`, `CSRF_INVALID`, `RATE_LIMITED`,
`UNTRUSTED_HOST`, `INTERNAL_ERROR`.

Browsers must send `X-CSRFToken` on every state-changing request. The token
comes from the `<meta name="csrf-token">` tag, the readable `csrf_token`
cookie, or `GET /api/auth/csrf`.

---

## 18. Testing

```bash
source .venv/bin/activate
pytest -q                                     # 202 tests
pytest --cov=app --cov-report=term-missing
flake8 app tests wsgi.py gunicorn.conf.py
```

| File | Covers |
| --- | --- |
| `test_health_and_config.py` | health endpoint, reference data, security headers, JSON 404 envelope |
| `test_auth.py` | login, logout, wrong credentials, user enumeration, lockout, password hashing, change-password, session invalidation, role authorisation, audit |
| `test_assets.py` | creation, required-field validation, vocabulary validation, dangerous URL schemes, search, SQL-injection and wildcard handling, filtering, sorting, pagination, IDOR on detail and update, immutability, versions, approve/reject/archive, my-submissions scoping, duplicate detection |
| `test_wgt_codes.py` | digit mapping, format, prefix ambiguity (`WGT101` vs `WGT1001`), sequential allocation, uniqueness constraint, **concurrent allocation across four threads**, legacy-import collisions, department migration |
| `test_files.py` | extension/MIME/magic-byte/size validation, random stored names, storage outside `static`, file permissions, path traversal, upload and download authorisation, tampered-path containment, deletion, audit |
| `test_security.py` | CSRF absent/forged/cross-session, cookie flags, XSS-safe rendering, bootstrap escaping, transaction rollback (asset, WGT counter, orphaned file), error-message disclosure, trusted hosts, full audit coverage |
| `test_cli.py` | `create-admin` (env and TTY paths, weak passwords, no hard-coded credential), `seed-config` never creating assets, source scanned for `Wings123+`, `localStorage` and `AdminAuth` absent from the frontend |

The concurrency test is a real one: reverting `app/wgt.py` to the prototype's
`max()+1` approach makes it fail with duplicate-key errors.

---

## 19. Troubleshooting

### The site returns 502 Bad Gateway

```bash
systemctl status agentlibrary
journalctl -u agentlibrary -n 100 --no-pager
sudo tail -50 /var/log/nginx/agentlibrary.error.log
ls -lZ /run/agentlibrary/agentlibrary.sock
sudo -u nginx curl -s --unix-socket /run/agentlibrary/agentlibrary.sock http://localhost/health
sudo ausearch -m AVC -ts recent | audit2why
```

Usual causes: the service is not running; nginx is not in the `agentlibrary`
group (`groups nginx`); the socket has the wrong SELinux label; or
`httpd_can_network_connect` is off.

### The service will not start

```bash
journalctl -u agentlibrary -n 50 --no-pager
sudo -u agentlibrary /opt/agentlibrary/.venv/bin/python -c \
     "import app; app.create_app(); print('factory OK')"
sudo -u agentlibrary test -r /etc/agentlibrary/agentlibrary.env && echo readable
systemd-analyze verify /etc/systemd/system/agentlibrary.service
```

### `RuntimeError: SECRET_KEY is not set`

The error names the cause. The three cases are:

| Message says | Meaning | Fix |
| --- | --- | --- |
| *No environment file was found at …* | The path does not exist | Create it, or pass `ENV_FILE=/path/to/file` |
| *… is not readable by uid N* | The file is `0640 root:agentlibrary` and you are not that user | `sudo -u agentlibrary …`, or join the `agentlibrary` group |
| *… was read, but it does not define SECRET_KEY* | The file loaded but the key is absent | Add a generated key to it |

```bash
# Which file is it looking at, and can you read it?
sudo -u agentlibrary test -r /etc/agentlibrary/agentlibrary.env && echo readable
sudo grep -c SECRET_KEY /etc/agentlibrary/agentlibrary.env

# Generate and append one
python3 -c "import secrets; print('SECRET_KEY=' + secrets.token_urlsafe(64))" \
    | sudo tee -a /etc/agentlibrary/agentlibrary.env
sudo systemctl restart agentlibrary
```

Note that `systemctl start agentlibrary` loads the file through
`EnvironmentFile=`; running `gunicorn` from your own shell relies on the
application loading it, which needs the file to be readable *by you*.

### Gunicorn settings from the env file appear to be ignored

`gunicorn.conf.py` is executed before `wsgi.py` is imported, so it loads the
environment file itself. If `GUNICORN_WORKERS` or `GUNICORN_BIND` still look
wrong, check what it actually resolved:

```bash
sudo -u agentlibrary .venv/bin/gunicorn -c gunicorn.conf.py --print-config wsgi:application | head
journalctl -u agentlibrary | grep "Agent Library starting"
```

### `connection to /run/agentlibrary/agentlibrary.sock failed`

Running gunicorn by hand without the directory systemd normally creates.
Either use the service (`sudo systemctl start agentlibrary`), create the
directory, or bind a loopback port for the test:

```bash
sudo mkdir -p /run/agentlibrary
sudo chown agentlibrary:agentlibrary /run/agentlibrary
# or
GUNICORN_BIND=127.0.0.1:8000 .venv/bin/gunicorn -c gunicorn.conf.py wsgi:application
```

### Database connection errors

```bash
mysql -h 127.0.0.1 -u agentlibrary -p agentlibrary -e "SELECT 1;"
systemctl status mysqld
sudo tail -50 /var/log/mysql/mysqld.log
sudo ss -tlnp | grep 3306
sudo getsebool httpd_can_network_connect_db     # only if MySQL is remote
```

"MySQL server has gone away" → `DB_POOL_RECYCLE` is at or above MySQL's
`wait_timeout`. Lower it.

### 413 on upload

`client_max_body_size` and `MAX_CONTENT_LENGTH` disagree.

```bash
grep client_max_body_size /etc/nginx/conf.d/agentlibrary.conf   # 25m
grep MAX_CONTENT_LENGTH /etc/agentlibrary/agentlibrary.env      # 26214400
```

### 400 CSRF_INVALID in the browser

Usually a stale page after a restart. Reload; `app.js` fetches a new token
automatically. If it persists, `SECRET_KEY` changed (every session and token is
invalidated) or the clock is skewed (`timedatectl status`).

### Static files 403 / 404

```bash
ls -lZ /opt/agentlibrary/static/css/app.css       # expect httpd_sys_content_t
sudo restorecon -Rv /opt/agentlibrary/static
namei -l /opt/agentlibrary/static/css/app.css     # every parent needs +x for nginx
```

### Uploads fail with a permission error

```bash
ls -ldZ /var/lib/agentlibrary/uploads
sudo chown -R agentlibrary:agentlibrary /var/lib/agentlibrary
sudo restorecon -Rv /var/lib/agentlibrary
df -h /var/lib/agentlibrary        # out of space?
```

### Login rate limit fires too early

`memory://` storage is **per worker**, so with 3 workers the effective limit is
inconsistent. Use Redis for accurate shared counting:

```ini
RATELIMIT_STORAGE_URI=redis://127.0.0.1:6379/0
```

### General diagnostics

```bash
systemctl is-active agentlibrary nginx mysqld
sudo ss -tlnp
sudo -u agentlibrary env FLASK_APP=wsgi.py /opt/agentlibrary/.venv/bin/flask show-status
journalctl -u agentlibrary --grep "audit action" -n 50
curl -s --unix-socket /run/agentlibrary/agentlibrary.sock http://localhost/health
df -h && free -h && uptime
```

---

## 20. Production-readiness checklist

Work through this before going live. Every item is verifiable with a command.

### Secrets and configuration

- [ ] `SECRET_KEY` is a unique 64-character random value, not the example
- [ ] `/etc/agentlibrary/agentlibrary.env` is `root:agentlibrary`, mode `0640`
- [ ] No `.env` file exists in `/opt/agentlibrary` (`ls -la /opt/agentlibrary/.env`)
- [ ] `.gitignore` excludes `.env` and `git status` shows no secret staged
- [ ] `ADMIN_INITIAL_PASSWORD` is **not** left in the environment file
- [ ] `grep -r "Wings123" /opt/agentlibrary --exclude-dir=.git` returns nothing
- [ ] `FLASK_ENV=production` and `flask show-status` runs without warnings

### Application

- [ ] Debug mode off — `journalctl -u agentlibrary | grep "debug=False"`
- [ ] `TRUSTED_HOSTS` names the real hostname
- [ ] `SESSION_COOKIE_SECURE=1`, `SameSite=Lax`, HttpOnly (check `curl -sI`)
- [ ] CSRF rejects a token-less POST (`curl -X POST .../api/auth/login` → 400)
- [ ] `MAX_CONTENT_LENGTH` matches nginx `client_max_body_size`
- [ ] Rate limiting enabled; Redis configured if `GUNICORN_WORKERS > 1`
- [ ] `pytest -q` passes on the deployed checkout

### Database

- [ ] MySQL 8 with `utf8mb4` / `utf8mb4_0900_ai_ci`
- [ ] `bind-address = 127.0.0.1` (or firewalled to this host only)
- [ ] Runtime user has SELECT/INSERT/UPDATE/DELETE only — no `GRANT`, `FILE`, `SUPER`
- [ ] `SHOW GRANTS FOR 'agentlibrary'@'127.0.0.1';` reviewed
- [ ] `flask db current` matches the deployed migration head
- [ ] `flask seed-config` run; **no demo assets** (`flask show-status` → `Assets: 0`)
- [ ] At least one admin exists and can sign in

### Files

- [ ] `UPLOAD_DIR` is outside `static/` and outside every nginx root
- [ ] `curl -sI https://host/uploads/` returns 404
- [ ] Upload directory is `agentlibrary:agentlibrary`, mode `0750`
- [ ] A test upload lands with mode `0600` and a random 32-hex name
- [ ] Download of another user's pending asset returns 404
- [ ] Malware scanning enabled, or its absence explicitly accepted

### Gunicorn / systemd

- [ ] Bound to a Unix socket (or `127.0.0.1`) — never `0.0.0.0`
- [ ] Running as `agentlibrary`, not root (`ps -o user= -C gunicorn`)
- [ ] `GUNICORN_WORKERS` set for the box; `TimeoutStopSec` > `graceful_timeout`
- [ ] `systemctl is-enabled agentlibrary` → `enabled`
- [ ] Service survives `systemctl restart` and a full reboot
- [ ] `journalctl -u agentlibrary` shows startup and audit lines

### Nginx / TLS

- [ ] `nginx -t` passes
- [ ] HTTP redirects to HTTPS (`curl -sI http://host` → 301)
- [ ] Valid certificate chain; renewal scheduled and dry-run tested
- [ ] HSTS, `X-Frame-Options: DENY`, `nosniff`, CSP present on responses
- [ ] Only `/static/` is served from disk
- [ ] `server_tokens off`

### OS hardening

- [ ] `getenforce` → `Enforcing`
- [ ] `semanage fcontext -l | grep agentlibrary` shows all four rules
- [ ] `httpd_can_network_connect` is on
- [ ] `ausearch -m AVC -ts today` is clean after a full exercise of the UI
- [ ] `firewall-cmd --list-all` shows only ssh/http/https
- [ ] `ss -tlnp` shows nothing unexpected listening publicly
- [ ] OS packages patched (`dnf check-update`)

### Operations

- [ ] `backup.sh` runs from cron and produces a non-empty archive
- [ ] **A restore has been rehearsed on a staging host**
- [ ] Retention set; backups stored off this machine
- [ ] Log rotation configured (`logrotate -d /etc/logrotate.d/agentlibrary`)
- [ ] `/health` monitored with alerting
- [ ] Rollback procedure (§15) walked through at least once
- [ ] Disk-space alerting on `/var/lib/agentlibrary`

### Audit

- [ ] `login.success`, `login.failure`, `asset.submit`, `asset.approve`,
      `asset.reject`, `asset.archive`, `asset.update`, `file.upload`,
      `file.download`, `password.change` all appear after an end-to-end run
- [ ] `journalctl -u agentlibrary --grep "audit action"` shows entries
- [ ] Retention decided (`flask prune-activity --days 730`)

---

## Licence

Proprietary — Wings® Global Travel internal use.
