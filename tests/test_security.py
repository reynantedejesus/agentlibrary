"""CSRF, XSS-safe rendering, transaction rollback and other hardening."""
import io
import os

import pytest
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import ActivityLog, Asset, AssetFile, AssetVersion
from tests.conftest import (PASSWORD, asset_payload, create_asset, data, err,
                            unlock, update_request_payload)

XSS = '<script>alert("xss")</script>'
IMG_XSS = '<img src=x onerror=alert(1)>'


# ---------------------------------------------------------------------------
# CSRF
# ---------------------------------------------------------------------------
@pytest.fixture()
def csrf_app(app):
    """The same app with CSRF protection switched back on."""
    app.config["WTF_CSRF_ENABLED"] = True
    return app


def _csrf_token(client):
    return data(client.get("/api/auth/csrf"))["csrfToken"]


def test_state_changing_request_without_a_token_is_rejected(csrf_app):
    client = csrf_app.test_client()
    response = client.post("/api/auth/unlock", json={"password": PASSWORD})
    assert response.status_code == 400
    assert err(response)["code"] == "CSRF_INVALID"


def test_csrf_failure_message_leaks_nothing(csrf_app):
    client = csrf_app.test_client()
    message = err(client.post("/api/auth/unlock", json={}))["message"]
    assert "session" in message.lower()
    # Flask-WTF's own reason strings ("The CSRF token is missing", "The
    # referrer does not match the host", "The CSRF session token is missing")
    # can hint at session internals, so the handler substitutes a generic
    # message and logs the detail server-side instead.
    for leak in ("referrer", "hmac", "secret", "signature", "does not match"):
        assert leak not in message.lower()


def test_request_with_a_valid_token_succeeds(csrf_app, admin_user):
    client = csrf_app.test_client()
    token = _csrf_token(client)
    response = client.post("/api/auth/unlock", json={"password": PASSWORD},
                           headers={"X-CSRFToken": token})
    assert response.status_code == 200


def test_a_forged_token_is_rejected(csrf_app, admin_user):
    client = csrf_app.test_client()
    _csrf_token(client)
    response = client.post("/api/auth/unlock", json={"password": PASSWORD},
                           headers={"X-CSRFToken": "not-a-real-token"})
    assert response.status_code == 400
    assert err(response)["code"] == "CSRF_INVALID"


def test_a_token_from_another_session_is_rejected(csrf_app, admin_user):
    victim = csrf_app.test_client()
    attacker = csrf_app.test_client()
    stolen = _csrf_token(attacker)          # token bound to the attacker's session
    response = victim.post("/api/auth/unlock", json={"password": PASSWORD},
                           headers={"X-CSRFToken": stolen})
    assert response.status_code == 400


def test_asset_submission_requires_a_token(csrf_app):
    """Submitting is open, but still not forgeable from another origin."""
    client = csrf_app.test_client()
    assert client.post("/api/assets", json=asset_payload()).status_code == 400
    ok_response = client.post("/api/assets", json=asset_payload(),
                              headers={"X-CSRFToken": _csrf_token(client)})
    assert ok_response.status_code == 201


def test_get_requests_need_no_token(csrf_app):
    client = csrf_app.test_client()
    assert client.get("/api/assets").status_code == 200
    assert client.get("/api/config").status_code == 200
    assert client.get("/health").status_code == 200


# ---------------------------------------------------------------------------
# cookies
# ---------------------------------------------------------------------------
def test_session_cookie_is_httponly_and_samesite(app, admin_user):
    app.config["SESSION_COOKIE_SECURE"] = True
    client = app.test_client()
    response = unlock(client)
    cookies = [h for k, h in response.headers if k == "Set-Cookie"]
    session_cookie = next(c for c in cookies if c.startswith("agentlibrary_session="))
    assert "HttpOnly" in session_cookie
    assert "Secure" in session_cookie
    assert "SameSite=Lax" in session_cookie


def test_csrf_cookie_is_readable_but_the_session_cookie_is_not(app, admin_user):
    client = app.test_client()
    response = unlock(client)
    cookies = [h for k, h in response.headers if k == "Set-Cookie"]
    csrf_cookie = next(c for c in cookies if c.startswith("csrf_token="))
    session_cookie = next(c for c in cookies if c.startswith("agentlibrary_session="))
    assert "HttpOnly" not in csrf_cookie          # JavaScript must read this one
    assert "HttpOnly" in session_cookie           # and must never read this one


def test_api_responses_are_not_cached(client):
    assert client.get("/api/config").headers["Cache-Control"] == "no-store"


def test_hsts_is_sent_when_running_behind_tls(app):
    app.config["SESSION_COOKIE_SECURE"] = True
    header = app.test_client().get("/").headers.get("Strict-Transport-Security")
    assert header and "max-age=31536000" in header


# ---------------------------------------------------------------------------
# XSS-safe rendering
# ---------------------------------------------------------------------------
def test_script_payloads_are_stored_verbatim_and_returned_as_json_data(as_admin):
    """The API is a JSON API: it stores exactly what was sent and escapes on
    output in the browser. What matters is that the payload is never
    interpolated into HTML by the server."""
    asset = create_asset(as_admin, name="Report " + XSS, description="Desc " + IMG_XSS)
    assert asset["name"] == "Report " + XSS
    detail = data(as_admin.get("/api/assets/%d" % asset["id"]))["asset"]
    assert detail["description"] == "Desc " + IMG_XSS


def test_json_responses_cannot_be_sniffed_as_html(as_admin):
    """A JSON API stores markup verbatim; what stops it executing is that the
    response can never be treated as a document."""
    asset = create_asset(as_admin, name="Report " + XSS)
    response = as_admin.get("/api/assets/%d" % asset["id"])
    assert response.headers["Content-Type"].startswith("application/json")
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
    # The payload round-trips exactly — escaping is the renderer's job, and
    # static/js/app.js routes every value through Util.escapeHtml().
    assert data(response)["asset"]["name"] == "Report " + XSS


def test_the_html_shell_never_interpolates_asset_data(client, as_admin):
    create_asset(as_admin, name="Report " + XSS)
    html = client.get("/").get_data(as_text=True)
    assert XSS not in html
    assert "Report" not in html          # the shell carries no catalogue data at all


def test_bootstrap_block_carries_no_user_controlled_values(app, admin_user):
    """The shell only ever bootstraps a boolean, so there is nothing to inject."""
    import json
    client = app.test_client()
    html = client.get("/").get_data(as_text=True)
    start = html.index('id="bootstrap-data"')
    block = html[start:html.index("</script>", start)]
    payload = json.loads(block[block.index(">") + 1:])
    assert set(payload) == {"unlocked", "version"}
    assert payload["unlocked"] is False


def test_stored_xss_in_a_filename_is_neutralised(as_admin):
    asset = create_asset(as_admin)
    response = as_admin.post(
        "/api/assets/%d/files" % asset["id"],
        data={"file": (io.BytesIO(b"# hi\n"), '<img src=x onerror=alert(1)>.md'),
              "kind": "documentation"},
        content_type="multipart/form-data")
    assert response.status_code == 201
    # secure_filename strips the dangerous characters from the display name.
    stored_name = data(response)["file"]["name"]
    assert "<" not in stored_name and ">" not in stored_name


def test_javascript_url_cannot_be_stored_as_a_direct_link(as_admin):
    response = as_admin.post("/api/assets",
                             json=asset_payload(link="javascript:alert(document.cookie)"))
    assert response.status_code == 400
    assert Asset.query.count() == 0


def test_null_bytes_are_stripped_from_input(as_admin):
    asset = create_asset(as_admin, name="Clean\x00Name")
    assert "\x00" not in asset["name"]


# ---------------------------------------------------------------------------
# transaction rollback
# ---------------------------------------------------------------------------
def test_failed_submission_leaves_no_partial_asset(as_admin, monkeypatch):
    """If the version insert blows up, the asset row must not survive."""
    import app.api.assets as assets_api

    def explode(*args, **kwargs):
        raise RuntimeError("simulated failure after the asset insert")

    monkeypatch.setattr(assets_api, "add_version", explode)
    before = Asset.query.count()
    response = as_admin.post("/api/assets", json=asset_payload())
    assert response.status_code == 500
    db.session.rollback()
    assert Asset.query.count() == before
    assert AssetVersion.query.count() == 0


def test_failed_submission_does_not_burn_a_wgt_code(as_admin, monkeypatch):
    """The counter increment commits with the asset — or not at all."""
    import app.api.assets as assets_api
    from app.models import WgtCounter

    monkeypatch.setattr(assets_api, "add_version",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert as_admin.post("/api/assets", json=asset_payload()).status_code == 500
    db.session.rollback()
    monkeypatch.undo()

    asset = create_asset(as_admin)
    assert asset["wgtCode"] == "WGT201"          # the first code, not WGT202
    assert db.session.get(WgtCounter, "2").last_sequence == 1


def test_failed_upload_leaves_no_orphan_on_disk(as_admin, app, monkeypatch):
    asset = create_asset(as_admin)
    import app.api.files as files_api

    real_record = files_api.audit.record

    def explode(action, *args, **kwargs):
        if action == files_api.audit.FILE_UPLOAD:
            raise RuntimeError("simulated database failure")
        return real_record(action, *args, **kwargs)

    monkeypatch.setattr(files_api.audit, "record", explode)
    response = as_admin.post("/api/assets/%d/files" % asset["id"],
                             data={"file": (io.BytesIO(b"# hi\n"), "notes.md"),
                                   "kind": "documentation"},
                             content_type="multipart/form-data")
    assert response.status_code == 500
    db.session.rollback()
    assert AssetFile.query.count() == 0
    # The upload directory must not be left holding a file nothing points at.
    root = app.config["UPLOAD_DIR"]
    on_disk = [f for _, _, files in os.walk(root) for f in files]
    assert on_disk == []


def test_rejected_asset_update_changes_nothing(as_admin):
    asset = create_asset(as_admin, name="Original Name")
    response = as_admin.put("/api/assets/%d" % asset["id"],
                            json=asset_payload(name="", department=None))
    assert response.status_code == 400
    fresh = data(as_admin.get("/api/assets/%d" % asset["id"]))["asset"]
    assert fresh["name"] == "Original Name"
    assert fresh["currentVersion"] == "1.0"       # no version was appended


def test_duplicate_wgt_code_rolls_back_cleanly(app, as_admin):
    asset = create_asset(as_admin)
    clash = Asset(wgt_code=asset["wgtCode"], name="Clash", asset_type="gpt",
                  platform="ChatGPT", department="Finance", status="Pending Review",
                  description="x", use_case="x", owner_name="x",
                  owner_email="x@y.co", creator_name="x", submitted_by_name="x",
                  configuration={}, additional_departments=[])
    db.session.add(clash)
    with pytest.raises(IntegrityError):
        db.session.commit()
    db.session.rollback()
    # The session is usable again and the original record is untouched.
    assert Asset.query.count() == 1
    assert Asset.query.one().name == asset["name"]


# ---------------------------------------------------------------------------
# error handling & information disclosure
# ---------------------------------------------------------------------------
def test_internal_errors_do_not_leak_details(as_admin, monkeypatch):
    import app.api.assets as assets_api
    monkeypatch.setattr(assets_api, "add_version",
                        lambda *a, **k: (_ for _ in ()).throw(
                            RuntimeError("mysql://user:hunter2@db-01/agentlibrary")))
    response = as_admin.post("/api/assets", json=asset_payload())
    body = response.get_data(as_text=True)
    assert response.status_code == 500
    assert "hunter2" not in body
    assert "mysql" not in body.lower()
    assert "Traceback" not in body
    assert err(response)["code"] == "INTERNAL_ERROR"


def test_debug_mode_is_off(app):
    assert app.debug is False


def test_untrusted_host_is_rejected_when_configured(app):
    app.config["SERVER_NAME"] = None          # let any Host reach routing
    app.config["TRUSTED_HOSTS"] = ["library.wings.test"]
    client = app.test_client()
    response = client.get("/api/config", headers={"Host": "evil.example.com"})
    assert response.status_code == 400
    assert err(response)["code"] == "UNTRUSTED_HOST"
    assert client.get("/api/config",
                      headers={"Host": "library.wings.test"}).status_code == 200


def test_no_trusted_hosts_configured_allows_any_host(app):
    app.config["SERVER_NAME"] = None
    app.config["TRUSTED_HOSTS"] = []
    assert app.test_client().get("/api/config",
                                 headers={"Host": "anything.test"}).status_code == 200


def test_audit_trail_covers_every_required_event(app, visitor, as_admin, admin_user):
    """Unlock, submission, approval, rejection, editing, upload, download,
    archive, update-request submit/accept and password change all leave a
    record.

    Ordered deliberately: the password change comes last, because rotating it
    invalidates the credential every other step needs.
    """
    # --- anonymous submission with a staged upload ---
    staged = data(visitor.post("/api/uploads",
                               data={"file": (io.BytesIO(b"# hi\n"), "notes.md")},
                               content_type="multipart/form-data"))["file"]
    asset = create_asset(visitor, knowledgeFileIds=[staged["id"]])
    second = create_asset(visitor, name="To Reject", department="Sales")

    # --- admin acts on them ---
    as_admin.post("/api/assets/%d/approve" % asset["id"])
    as_admin.post("/api/assets/%d/reject" % second["id"], json={"reason": "duplicate"})
    as_admin.put("/api/assets/%d" % asset["id"],
                 json=asset_payload(name="Edited", department=None))
    # The other upload route: an admin attaching to a record that exists.
    as_admin.post("/api/assets/%d/files" % asset["id"],
                  data={"file": (io.BytesIO(b"# guide\n"), "guide.md"),
                        "kind": "documentation"},
                  content_type="multipart/form-data")
    file_id = AssetFile.query.filter(AssetFile.asset_id.isnot(None)).first().id
    visitor.get("/api/files/%d/download" % file_id)

    # --- update request, raised anonymously and accepted ---
    request_id = data(visitor.post(
        "/api/update-requests",
        json=update_request_payload(asset["id"])))["updateRequest"]["id"]
    as_admin.post("/api/update-requests/%d/accept" % request_id)

    as_admin.post("/api/assets/%d/archive" % asset["id"], json={"reason": "stale"})

    # --- credential lifecycle, last ---
    as_admin.post("/api/auth/change-password",
                  json={"currentPassword": PASSWORD,
                        "newPassword": "AnotherGoodPassword1",
                        "confirmPassword": "AnotherGoodPassword1"})
    as_admin.post("/api/auth/lock")

    recorded = {row.action for row in ActivityLog.query.all()}
    for required in ("login.success", "logout", "password.change",
                     "asset.submit", "asset.update", "asset.approve",
                     "asset.reject", "asset.archive",
                     "file.stage", "file.upload", "file.download",
                     "update_request.submit", "update_request.accept"):
        assert required in recorded, "missing audit action: %s" % required


def test_anonymous_submission_records_what_was_typed_not_an_identity(visitor):
    """With no accounts the submitter is unauthenticated, so the audit row has
    no actor — the name and email they typed are recorded as detail, clearly
    unverified, alongside the source address."""
    create_asset(visitor)
    entry = ActivityLog.query.filter_by(action="asset.submit").one()
    assert entry.actor_email is None
    assert entry.detail["submittedEmail"] == "uma@wings.test"
    assert entry.detail["submittedBy"] == "Uma User"
    assert entry.ip == "127.0.0.1"
    assert entry.object_label == "WGT201"


def test_admin_actions_are_attributed_to_the_admin_account(as_admin):
    asset = create_asset(as_admin)
    as_admin.post("/api/assets/%d/approve" % asset["id"])
    entry = ActivityLog.query.filter_by(action="asset.approve").one()
    assert entry.actor_email == "admin@wings.test"
    assert entry.ip == "127.0.0.1"


def test_failed_unlock_is_recorded_even_though_the_request_failed(client, admin_user):
    unlock(client, "wrong-password")
    assert ActivityLog.query.filter_by(action="login.failure").count() == 1
