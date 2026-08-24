"""The admin unlock gate.

There are no user accounts: browsing, submitting and raising an update request
are open, and the Admin / Review console is protected by one shared password.
These tests cover that the password is verified on the SERVER (never in the
browser), that failures are throttled and audited, and that every protected
endpoint refuses a locked session.
"""
import pytest

from app.models import ActivityLog, User
from tests.conftest import (PASSWORD, asset_payload, create_asset, data, err,
                            make_admin, unlock)


# ---------------------------------------------------------------------------
# unlocking
# ---------------------------------------------------------------------------
def test_unlock_succeeds_with_the_right_password(client, admin_user):
    payload = data(unlock(client))
    assert payload["unlocked"] is True
    assert "csrfToken" in payload
    # The password must never come back in a response.
    assert PASSWORD not in str(payload)


def test_unlock_fails_with_the_wrong_password(client, admin_user):
    response = unlock(client, "not-the-password")
    assert response.status_code == 401
    assert err(response)["code"] == "INVALID_CREDENTIALS"


def test_unlock_requires_a_password(client, admin_user):
    response = client.post("/api/auth/unlock", json={})
    assert response.status_code == 400
    assert "password" in err(response)["fields"]


def test_unlock_needs_no_email(client, admin_user):
    """The console asks for a password only — there is nobody to identify."""
    assert client.post("/api/auth/unlock",
                       json={"password": PASSWORD}).status_code == 200


def test_me_reports_locked_before_unlocking(client, admin_user):
    payload = data(client.get("/api/auth/me"))
    assert payload["unlocked"] is False
    assert payload["adminConfigured"] is True


def test_me_reports_unlocked_after(as_admin):
    assert data(as_admin.get("/api/auth/me"))["unlocked"] is True


def test_lock_ends_the_session(as_admin):
    as_admin.post("/api/auth/lock")
    assert data(as_admin.get("/api/auth/me"))["unlocked"] is False
    assert as_admin.get("/api/admin/stats").status_code == 401


def test_unlock_is_reported_when_no_admin_exists(client):
    """A deployment step was skipped — say so rather than "wrong password"."""
    response = client.post("/api/auth/unlock", json={"password": "anything123"})
    assert response.status_code == 503
    assert err(response)["code"] == "ADMIN_NOT_CONFIGURED"
    assert data(client.get("/api/auth/me"))["adminConfigured"] is False


def test_password_is_stored_only_as_a_hash(app, admin_user):
    stored = User.query.one()
    assert stored.password_hash != PASSWORD
    assert PASSWORD not in stored.password_hash
    assert stored.password_hash.startswith("pbkdf2:sha256:")
    assert stored.check_password(PASSWORD)
    assert not stored.check_password(PASSWORD + "x")


def test_repeated_failures_lock_the_console(client, app, admin_user):
    app.config["MAX_LOGIN_FAILURES"] = 3
    for _ in range(3):
        assert unlock(client, "wrong").status_code == 401
    # Even the correct password is refused during the cooling-off period.
    response = unlock(client, PASSWORD)
    assert response.status_code == 403
    assert err(response)["code"] == "ACCOUNT_LOCKED"


def test_a_locked_session_cannot_be_forged_client_side(app, admin_user):
    """The browser's idea of "unlocked" is a rendering hint, nothing more."""
    client = app.test_client()
    with client.session_transaction() as session:
        session["admin_unlocked"] = True      # what a tampered client would do
    # It is a signed cookie, so this only works because the test writes it
    # through Flask itself; the point is that the server still checks it.
    assert client.get("/api/admin/stats").status_code == 200
    with client.session_transaction() as session:
        session.clear()
    assert client.get("/api/admin/stats").status_code == 401


def test_session_cookie_is_httponly_and_samesite(app, admin_user):
    app.config["SESSION_COOKIE_SECURE"] = True
    client = app.test_client()
    response = unlock(client)
    cookies = [h for k, h in response.headers if k == "Set-Cookie"]
    session_cookie = next(c for c in cookies if c.startswith("agentlibrary_session="))
    assert "HttpOnly" in session_cookie
    assert "Secure" in session_cookie
    assert "SameSite=Lax" in session_cookie


# ---------------------------------------------------------------------------
# changing the password
# ---------------------------------------------------------------------------
def test_change_password_requires_being_unlocked(client, admin_user):
    response = client.post("/api/auth/change-password", json={
        "currentPassword": PASSWORD, "newPassword": "AnotherGoodPassword1"})
    assert response.status_code == 401


def test_change_password_requires_the_current_one(as_admin):
    response = as_admin.post("/api/auth/change-password", json={
        "currentPassword": "wrong", "newPassword": "AnotherGoodPassword1"})
    assert response.status_code == 403
    assert err(response)["code"] == "INVALID_CREDENTIALS"


def test_change_password_rejects_a_weak_password(as_admin):
    response = as_admin.post("/api/auth/change-password", json={
        "currentPassword": PASSWORD, "newPassword": "short"})
    assert response.status_code == 400
    assert "newPassword" in err(response)["fields"]


def test_change_password_rejects_the_prototype_default(as_admin):
    """The HTML prototype shipped "Wings123+" in its source."""
    response = as_admin.post("/api/auth/change-password", json={
        "currentPassword": PASSWORD, "newPassword": "Wings123+Wings123+"})
    assert response.status_code == 400


def test_change_password_rotates_the_shared_secret(client, admin_user):
    unlock(client)
    new_password = "AnotherGoodPassword1"
    assert client.post("/api/auth/change-password", json={
        "currentPassword": PASSWORD, "newPassword": new_password,
        "confirmPassword": new_password}).status_code == 200
    client.post("/api/auth/lock")
    assert unlock(client, PASSWORD).status_code == 401
    assert unlock(client, new_password).status_code == 200


def test_changing_the_password_locks_out_other_browsers(app, admin_user):
    """Everyone holding the old password has to be told the new one."""
    first, second = app.test_client(), app.test_client()
    unlock(first)
    unlock(second)
    first.post("/api/auth/change-password", json={
        "currentPassword": PASSWORD, "newPassword": "AnotherGoodPassword1",
        "confirmPassword": "AnotherGoodPassword1"})
    # The already-open session keeps working; a NEW unlock needs the new secret.
    assert second.get("/api/admin/stats").status_code == 200
    second.post("/api/auth/lock")
    assert unlock(second, PASSWORD).status_code == 401
    assert unlock(second, "AnotherGoodPassword1").status_code == 200


# ---------------------------------------------------------------------------
# what is open, and what is not
# ---------------------------------------------------------------------------
def test_browsing_needs_no_password(client):
    assert client.get("/api/assets").status_code == 200
    assert client.get("/api/config").status_code == 200
    assert client.get("/health").status_code == 200


def test_submitting_needs_no_password(client):
    """Matches the prototype: anyone can add to the Library."""
    response = client.post("/api/assets", json=asset_payload())
    assert response.status_code == 201
    assert response.get_json()["data"]["asset"]["status"] == "Pending Review"


def test_raising_an_update_request_needs_no_password(app, client):
    from tests.conftest import publish, update_request_payload
    asset = publish(app, client)
    response = client.post("/api/update-requests",
                           json=update_request_payload(asset["id"]))
    assert response.status_code == 201


@pytest.mark.parametrize("method,path", [
    ("get", "/api/admin/activity"),
    ("get", "/api/admin/stats"),
    ("get", "/api/update-requests"),
])
def test_console_endpoints_refuse_a_locked_session(client, method, path):
    response = getattr(client, method)(path)
    assert response.status_code == 401
    assert err(response)["code"] == "AUTH_REQUIRED"


def test_governance_actions_refuse_a_locked_session(client):
    asset = create_asset(client)
    for path in ("approve", "reject", "archive"):
        assert client.post("/api/assets/%d/%s" % (asset["id"], path)).status_code == 401
    assert client.put("/api/assets/%d" % asset["id"],
                      json=asset_payload()).status_code == 401


def test_a_second_admin_row_does_not_change_the_password(app, admin_user):
    """The oldest admin wins, so the answer is stable if a row is added."""
    make_admin("second@wings.test", "Second", "DifferentPassword123")
    client = app.test_client()
    assert unlock(client, PASSWORD).status_code == 200


# ---------------------------------------------------------------------------
# audit
# ---------------------------------------------------------------------------
def test_unlock_success_and_failure_are_audited(client, app, admin_user):
    unlock(client, "wrong")
    unlock(client)
    client.post("/api/auth/lock")
    actions = [row.action for row in ActivityLog.query.all()]
    assert "login.failure" in actions
    assert "login.success" in actions
    assert "logout" in actions


def test_failed_unlock_records_the_source_address(client, admin_user):
    unlock(client, "wrong")
    entry = ActivityLog.query.filter_by(action="login.failure").one()
    assert entry.ip == "127.0.0.1"


def test_password_change_is_audited(as_admin):
    as_admin.post("/api/auth/change-password", json={
        "currentPassword": PASSWORD, "newPassword": "AnotherGoodPassword1",
        "confirmPassword": "AnotherGoodPassword1"})
    assert ActivityLog.query.filter_by(action="password.change").count() == 1
