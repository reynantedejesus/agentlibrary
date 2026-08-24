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
    # flask_wtf caches the signed CSRF token on ``g``. Leaving it in place
    # would break the unlock flow specifically, because unlocking calls
    # session.clear() and then relies on generate_csrf() re-seeding
    # session["csrf_token"] — which it skips when g already holds a token.
    @application.before_request
    def _reset_per_request_caches():
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


def make_admin(email="admin@wings.test", name="WGT Automation", password=PASSWORD):
    """The single administrator row whose hash backs the console password."""
    user = User(email=email, full_name=name, role="admin")
    user.set_password(password)
    _db.session.add(user)
    _db.session.commit()
    return user


def unlock(client, password=PASSWORD):
    """Enter the shared admin password, as the console prompt does."""
    return client.post("/api/auth/unlock", json={"password": password})


@pytest.fixture()
def admin_user(app):
    return make_admin()


@pytest.fixture()
def visitor(app):
    """A separate client that is definitely NOT unlocked.

    ``as_admin`` unlocks the shared ``client``, so a test that needs both an
    admin and an ordinary visitor must not reuse ``client`` for the latter.
    """
    return app.test_client()


@pytest.fixture()
def as_admin(client, admin_user):
    """A client with the Admin / Review console unlocked."""
    response = unlock(client)
    assert response.status_code == 200, response.get_json()
    return client


def asset_payload(**overrides):
    """A valid Add-to-Library submission. No credentials: submitting is open."""
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


def approve(client, asset_id):
    """Approve an asset with an already-unlocked client."""
    response = client.post("/api/assets/%d/approve" % asset_id)
    assert response.status_code == 200, response.get_json()
    return response.get_json()["data"]["asset"]


def publish(app, client, **overrides):
    """Submit anonymously, then approve on a separate unlocked client.

    Keeps ``client`` locked, which is what most tests want as a starting
    point — the catalogue is public but the console is not. Creates the
    administrator row if the test did not ask for one.
    """
    asset = create_asset(client, **overrides)
    if User.query.filter_by(role="admin").first() is None:
        make_admin()
    admin = app.test_client()
    response = unlock(admin)
    assert response.status_code == 200, response.get_json()
    return approve(admin, asset["id"])


def stage_file(client, name="notes.md", content=b"# notes\n"):
    """Upload a file that nothing owns yet, as the dropzone does."""
    import io
    response = client.post(
        "/api/uploads",
        data={"file": (io.BytesIO(content), name)},
        content_type="multipart/form-data")
    assert response.status_code == 201, response.get_json()
    return response.get_json()["data"]["file"]


def update_request_payload(asset_id, **overrides):
    payload = {
        "assetId": asset_id,
        "fields": ["description"],
        "proposed": {"description": "Now drafts board and exec commentary."},
        "notes": "Prompt was rewritten.",
        "requesterName": "Uma User",
        "requesterEmail": "uma@wings.test",
    }
    payload.update(overrides)
    return payload


@pytest.fixture()
def submitted_asset(client):
    return create_asset(client)


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
