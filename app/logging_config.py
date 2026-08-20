"""Application logging.

Two sinks:

* **stdout** — picked up by systemd and readable with
  ``journalctl -u agentlibrary``. This is the default in production.
* **rotating file** — ``$LOG_DIR/agentlibrary.log`` (10 MB x 5), enabled when
  ``LOG_DIR`` exists and is writable by the service account. Useful when you
  want the app log separate from the unit journal.

Access logging is left to nginx and gunicorn; this handles application events,
including the audit trail mirrored from ``app/audit.py``.

Connects to: ``app/__init__.py`` calls :func:`configure_logging` early in the
factory, before any blueprint is registered.
"""
from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler

LOG_FORMAT = ("%(asctime)s %(levelname)-8s [%(name)s] %(message)s "
              "[in %(pathname)s:%(lineno)d]")
SIMPLE_FORMAT = "%(asctime)s %(levelname)-8s [%(name)s] %(message)s"


def configure_logging(app) -> None:
    level = getattr(logging, str(app.config.get("LOG_LEVEL", "INFO")).upper(), logging.INFO)

    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    if app.config.get("LOG_TO_STDOUT", True):
        stream = logging.StreamHandler(sys.stdout)
        stream.setFormatter(logging.Formatter(SIMPLE_FORMAT))
        stream.setLevel(level)
        root.addHandler(stream)

    log_dir = app.config.get("LOG_DIR")
    if log_dir:
        try:
            os.makedirs(log_dir, exist_ok=True)
            path = os.path.join(log_dir, "agentlibrary.log")
            rotating = RotatingFileHandler(path, maxBytes=10 * 1024 * 1024,
                                           backupCount=5, encoding="utf-8")
            rotating.setFormatter(logging.Formatter(LOG_FORMAT))
            rotating.setLevel(level)
            root.addHandler(rotating)
        except (OSError, PermissionError) as exc:
            # Never abort startup because a log directory is missing.
            logging.getLogger(__name__).warning(
                "File logging disabled (%s): %s", log_dir, exc)

    app.logger.setLevel(level)
    # SQLAlchemy's engine logger is noisy at INFO; keep it at WARNING unless
    # the whole app is in DEBUG.
    logging.getLogger("sqlalchemy.engine").setLevel(
        logging.INFO if level <= logging.DEBUG else logging.WARNING)
    logging.getLogger("werkzeug").setLevel(max(level, logging.INFO))
    app.logger.info("Logging configured at %s (env=%s)",
                    logging.getLevelName(level), app.config.get("ENV_NAME"))
