"""Shared pytest fixtures.

The suite runs against SQLite so it needs no MySQL server, but every model and
query in the application is dialect-neutral (``BigInteger.with_variant``,
``sa.JSON``, parameter-bound filters), so what passes here is the same code
that runs against MySQL 8.

Run with::

    pytest -q
    pytest --cov=app --cov-report=term-missing
"""
from __future__ import annotations

import os
import tempfile

import pytest
from flask import g

os.environ.setdefault("FLASK_ENV", "testing")

from app import create_app                      # noqa: E402
from app.extensions import db as _db            # noqa: E402
from app.models import User                     # noqa: E402

PASSWORD = "CorrectHorseBattery9"


@pytest.fixture()
def upload_dir():
    with tempfile.TemporaryDirectory() as path:
        yield path


@pytest.fixture()
def app(upload_dir):
    application = create_app("testing")
    application.config.update(
        UPLOAD_DIR=upload_dir,
        WTF_CSRF_ENABLED=False,
    )

    # The fixture below holds ONE app context open for the whole test so that
    # assertions can query models directly (User.query, ActivityLog.query...).
    # Flask reuses an already-pushed app context for test-client requests, so
    # per-request caches stored on ``g`` would survive from one request into
    # the next — something that never happens in production, where every
    # request gets its own app context.
    #
    # Two extensions cache on ``g`` and both matter here:
    #   * flask_login  -> g._login_user  (identity resolution)
    #   * flask_wtf    -> g.csrf_token   (the signed CSRF token)
    # Leaving the CSRF cache in place would break the login flow specifically,
    # because login calls session.clear() and then relies on generate_csrf()
    # re-seeding session["csrf_token"] — which it skips when g already holds a
    # token. Clearing both keeps the harness faithful to production.
    @application.before_request
    def _reset_per_request_caches():
        g.pop("_login_user", None)
        g.pop("csrf_token", None)

    with application.app_context():
        _db.create_all()
        yield application
        _db.session.remove()
        _db.drop_all()


@pytest.fixture()
def db(app):
    return _db


@pytest.fixture()
def client(app):
    return app.test_client()


def make_user(email, role="user", name=None, password=PASSWORD, active=True):
    user = User(email=email, full_name=name or email.split("@")[0].title(), role=role)
    user.set_password(password)
    user.is_active_flag = active
    _db.session.add(user)
    _db.session.commit()
    return user


@pytest.fixture()
def normal_user(app):
    return make_user("uma@wings.test", "user", "Uma User")


@pytest.fixture()
def reviewer_user(app):
    return make_user("rob@wings.test", "reviewer", "Rob Reviewer")


@pytest.fixture()
def admin_user(app):
    return make_user("ada@wings.test", "admin", "Ada Admin")


def login(client, email, password=PASSWORD):
    return client.post("/api/auth/login", json={"email": email, "password": password})


@pytest.fixture()
def as_user(client, normal_user):
    login(client, normal_user.email)
    return client


@pytest.fixture()
def as_reviewer(client, reviewer_user):
    login(client, reviewer_user.email)
    return client


@pytest.fixture()
def as_admin(client, admin_user):
    login(client, admin_user.email)
    return client


def asset_payload(**overrides):
    """A valid Add-to-Library submission."""
    payload = {
        "type": "gpt",
        "department": "Finance",
        "name": "Board Pack Commentary",
        "description": "Drafts first-pass board commentary from the monthly P&L export.",
        "instructions": "You are the WGT commentary assistant. Flag variances over 10%.",
        "link": "https://chatgpt.com/g/board-commentary",
        "ownerName": "Uma User",
        "ownerEmail": "uma@wings.test",
        "tags": ["Reporting", "Analysis"],
    }
    payload.update(overrides)
    return payload


def create_asset(client, **overrides):
    response = client.post("/api/assets", json=asset_payload(**overrides))
    assert response.status_code == 201, response.get_json()
    return response.get_json()["data"]["asset"]


@pytest.fixture()
def submitted_asset(as_user):
    return create_asset(as_user)


def body(response):
    return response.get_json()


def data(response):
    payload = response.get_json()
    assert payload["ok"] is True, payload
    return payload["data"]


def err(response):
    payload = response.get_json()
    assert payload["ok"] is False, payload
    return payload["error"]
