"""WSGI entry point.

Gunicorn loads this module and serves the ``application`` callable::

    gunicorn --config gunicorn.conf.py wsgi:application

The Flask CLI also uses it (``FLASK_APP=wsgi.py flask db upgrade``), which is
why the app object is created at import time rather than behind a factory call
the CLI would have to guess at.

Connects to: ``app/__init__.py::create_app``; referenced by
``deploy/agentlibrary.service`` and every migration command in the README.
"""
from __future__ import annotations

import os

from app import create_app

application = create_app(os.environ.get("FLASK_ENV"))
# Flask's CLI looks for a variable named `app` as well.
app = application

if __name__ == "__main__":
    # Development convenience only — production always goes through gunicorn.
    application.run(
        host=os.environ.get("DEV_HOST", "127.0.0.1"),
        port=int(os.environ.get("DEV_PORT", "5000")),
        debug=application.config.get("DEBUG", False),
    )
