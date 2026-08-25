"""Gunicorn configuration.

Used by systemd::

    ExecStart=/var/www/agentlibrary/.venv/bin/gunicorn \
        --config /var/www/agentlibrary/gunicorn.conf.py wsgi:application

Every knob reads an environment variable so the same file works in staging and
production without edits — the values come from
``/etc/agentlibrary/agentlibrary.env`` via the unit's ``EnvironmentFile=``.

Binding
-------
The default is the loopback port ``127.0.0.1:8090``, which is what the shipped
nginx config proxies to. Gunicorn NEVER binds to 0.0.0.0 here: nginx is the
only thing that talks to it. A public bind is refused at startup (see the
check below), because that would expose the app without TLS, without the
security headers nginx adds, and without the request size limit.

A Unix socket still works if you prefer it — set
``GUNICORN_BIND=unix:/run/agentlibrary/agentlibrary.sock`` and point the nginx
``upstream`` at the same path. The unit already creates ``/run/agentlibrary``
via ``RuntimeDirectory=``.

Do not open the bind port in firewalld. It is reachable from the host only,
and nginx is the only client.
"""
from __future__ import annotations

import multiprocessing
import os
import sys

# Gunicorn executes THIS FILE before it imports wsgi.py, so create_app() has
# not run yet and nothing has read /etc/agentlibrary/agentlibrary.env. Without
# this, every GUNICORN_* setting below would silently fall back to its default
# even though the environment file defines it. Load the same file here, using
# the same loader the application uses, so both agree.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from app import load_environment_files

    load_environment_files()
except ImportError:  # the app package is not importable yet — defaults apply
    pass


def _int(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


# --- bind --------------------------------------------------------------------
bind = os.environ.get("GUNICORN_BIND", "127.0.0.1:8090")

# Refuse to start on a public interface. This is a deployment mistake that is
# easy to make and expensive to notice, so fail loudly instead of serving.
# unix: and fd: binds are not network binds at all, so they are exempt.
_is_local_socket = bind.startswith("unix:") or bind.startswith("fd://")
_host = bind.rsplit(":", 1)[0].strip("[]") if ":" in bind else bind
if not _is_local_socket and _host not in ("127.0.0.1", "localhost", "::1"):
    raise RuntimeError(
        "Refusing to bind gunicorn to %r. Bind to 127.0.0.1, a Unix socket or "
        "fd://, and let nginx terminate TLS in front of it." % bind
    )

if bind.startswith("unix:"):
    _socket_path = bind[len("unix:"):]
    _socket_dir = os.path.dirname(_socket_path) or "."
    if not os.path.isdir(_socket_dir):
        raise RuntimeError(
            "Gunicorn is set to bind {0}, but {1} does not exist.\n"
            "  systemd creates it automatically (RuntimeDirectory= in "
            "agentlibrary.service), so `systemctl start agentlibrary` needs "
            "nothing extra.\n"
            "  To run gunicorn by hand, either create the directory:\n"
            "      sudo mkdir -p {1} && sudo chown agentlibrary:agentlibrary {1}\n"
            "  or use the loopback bind, which needs no directory:\n"
            "      GUNICORN_BIND=127.0.0.1:8090 gunicorn -c gunicorn.conf.py "
            "wsgi:application".format(bind, _socket_dir)
        )
    if not os.access(_socket_dir, os.W_OK):
        raise RuntimeError(
            "Gunicorn cannot write the socket into {0} as uid {1}. Run as the "
            "service account: sudo -u agentlibrary .venv/bin/gunicorn -c "
            "gunicorn.conf.py wsgi:application".format(_socket_dir, os.getuid())
        )

# Only relevant when GUNICORN_BIND names a Unix socket: 0o660 with the socket
# owned by the agentlibrary user and group, plus nginx added to that group,
# keeps it unreadable by everyone else. Harmless on a TCP bind.
umask = 0o007
backlog = 2048

# --- workers -----------------------------------------------------------------
# Default to (2 x CPU) + 1, capped so a large box does not open a needless
# number of database connections (each worker holds its own SQLAlchemy pool).
_default_workers = min((multiprocessing.cpu_count() * 2) + 1, 8)
workers = _int("GUNICORN_WORKERS", _default_workers)
threads = _int("GUNICORN_THREADS", 2)
worker_class = os.environ.get("GUNICORN_WORKER_CLASS", "gthread")
worker_tmp_dir = os.environ.get("GUNICORN_WORKER_TMP_DIR", "/dev/shm")

# --- timeouts & recycling ----------------------------------------------------
timeout = _int("GUNICORN_TIMEOUT", 60)
graceful_timeout = _int("GUNICORN_GRACEFUL_TIMEOUT", 30)
keepalive = _int("GUNICORN_KEEPALIVE", 5)

# Recycle workers periodically so a slow leak can never accumulate. The jitter
# stops every worker restarting at the same moment.
max_requests = _int("GUNICORN_MAX_REQUESTS", 1000)
max_requests_jitter = _int("GUNICORN_MAX_REQUESTS_JITTER", 100)

# --- request limits ----------------------------------------------------------
# Defence in depth alongside nginx's own limits.
limit_request_line = _int("GUNICORN_LIMIT_REQUEST_LINE", 8190)
limit_request_fields = _int("GUNICORN_LIMIT_REQUEST_FIELDS", 100)
limit_request_field_size = _int("GUNICORN_LIMIT_REQUEST_FIELD_SIZE", 8190)

# --- logging -----------------------------------------------------------------
# "-" sends both streams to stdout/stderr, which systemd captures into the
# journal:  journalctl -u agentlibrary -f
accesslog = os.environ.get("GUNICORN_ACCESS_LOG", "-")
errorlog = os.environ.get("GUNICORN_ERROR_LOG", "-")
loglevel = os.environ.get("GUNICORN_LOG_LEVEL", "info")
capture_output = True

# %({X-Forwarded-For}i)s is the real client address behind nginx.
access_log_format = ('%({X-Forwarded-For}i)s %(l)s %(u)s %(t)s "%(r)s" '
                     '%(s)s %(b)s "%(f)s" "%(a)s" %(L)ss')

# Trust the forwarding headers only from the loopback proxy.
forwarded_allow_ips = os.environ.get("GUNICORN_FORWARDED_ALLOW_IPS", "127.0.0.1")
proxy_allow_ips = forwarded_allow_ips

# --- process naming ----------------------------------------------------------
proc_name = "agentlibrary"
preload_app = os.environ.get("GUNICORN_PRELOAD", "0") == "1"


# --- lifecycle hooks ---------------------------------------------------------
def on_starting(server):
    server.log.info("Agent Library starting: bind=%s workers=%s threads=%s",
                    bind, workers, threads)


def post_fork(server, worker):
    # Each worker gets its own SQLAlchemy engine and connection pool; disposing
    # any pool inherited across the fork avoids sharing a socket between
    # processes, which corrupts MySQL protocol state.
    try:
        from app.extensions import db
        db.engine.dispose()
    except Exception:
        pass
    server.log.debug("Worker %s ready", worker.pid)


def worker_int(worker):
    worker.log.info("Worker %s interrupted, shutting down gracefully", worker.pid)


def on_exit(server):
    server.log.info("Agent Library shut down")
