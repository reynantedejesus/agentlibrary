"""Login, logout, password change, lockout and role authorisation."""
import pytest

from app.models import ActivityLog, User
from tests.conftest import PASSWORD, data, err, login, make_user


def test_login_succeeds_with_correct_credentials(client, normal_user):
    payload = data(login(client, "uma@wings.test"))
    assert payload["user"]["email"] == "uma@wings.test"
    assert payload["user"]["role"] == "user"
    assert "csrfToken" in payload
    assert "password" not in str(payload).lower()


def test_login_fails_with_a_wrong_password(client, normal_user):
    response = login(client, "uma@wings.test", "not-the-password")
    assert response.status_code == 401
    assert err(response)["code"] == "INVALID_CREDENTIALS"


def test_login_fails_for_an_unknown_address(client):
    response = login(client, "nobody@wings.test", "whatever12345")
    assert response.status_code == 401
    assert err(response)["code"] == "INVALID_CREDENTIALS"


def test_login_does_not_leak_whether_an_account_exists(client, normal_user):
    unknown = err(login(client, "nobody@wings.test", "whatever12345"))
    wrong = err(login(client, "uma@wings.test", "whatever12345"))
    assert unknown["message"] == wrong["message"]
    assert unknown["code"] == wrong["code"]


def test_login_requires_both_fields(client):
    response = client.post("/api/auth/login", json={"email": ""})
    assert response.status_code == 400
    fields = err(response)["fields"]
    assert "email" in fields and "password" in fields


def test_me_is_anonymous_before_login(client):
    assert data(client.get("/api/auth/me"))["user"] is None


def test_logout_clears_the_session(client, normal_user):
    login(client, "uma@wings.test")
    assert data(client.get("/api/auth/me"))["user"] is not None
    client.post("/api/auth/logout")
    assert data(client.get("/api/auth/me"))["user"] is None


def test_inactive_account_cannot_log_in(client, app):
    make_user("gone@wings.test", "user", active=False)
    response = login(client, "gone@wings.test")
    assert response.status_code == 403
    assert err(response)["code"] == "ACCOUNT_DISABLED"


def test_repeated_failures_lock_the_account(client, app, normal_user):
    app.config["MAX_LOGIN_FAILURES"] = 3
    for _ in range(3):
        assert login(client, "uma@wings.test", "wrong-password").status_code == 401
    # Even the correct password is refused while the lock holds.
    response = login(client, "uma@wings.test", PASSWORD)
    assert response.status_code == 403
    assert err(response)["code"] == "ACCOUNT_LOCKED"


def test_passwords_are_stored_as_hashes_only(app, normal_user):
    stored = User.query.filter_by(email="uma@wings.test").one()
    assert stored.password_hash != PASSWORD
    assert PASSWORD not in stored.password_hash
    assert stored.password_hash.startswith("pbkdf2:sha256:")
    assert stored.check_password(PASSWORD)
    assert not stored.check_password(PASSWORD + "x")


def test_change_password_requires_the_current_one(as_user):
    response = as_user.post("/api/auth/change-password", json={
        "currentPassword": "wrong", "newPassword": "AnotherGoodPassword1",
    })
    assert response.status_code == 403
    assert err(response)["code"] == "INVALID_CREDENTIALS"


def test_change_password_rejects_a_weak_password(as_user):
    response = as_user.post("/api/auth/change-password", json={
        "currentPassword": PASSWORD, "newPassword": "short",
    })
    assert response.status_code == 400
    assert "newPassword" in err(response)["fields"]


def test_change_password_rejects_the_prototype_default(as_user):
    response = as_user.post("/api/auth/change-password", json={
        "currentPassword": PASSWORD, "newPassword": "Wings123+Wings123+",
    })
    assert response.status_code == 400


def test_change_password_works_and_invalidates_the_old_one(client, normal_user):
    login(client, "uma@wings.test")
    new_password = "AnotherGoodPassword1"
    response = client.post("/api/auth/change-password", json={
        "currentPassword": PASSWORD, "newPassword": new_password,
        "confirmPassword": new_password,
    })
    assert response.status_code == 200
    client.post("/api/auth/logout")
    assert login(client, "uma@wings.test", PASSWORD).status_code == 401
    assert login(client, "uma@wings.test", new_password).status_code == 200


def test_change_password_invalidates_other_sessions(app, normal_user):
    """get_id() embeds part of the hash, so old cookies stop resolving."""
    first = app.test_client()
    second = app.test_client()
    login(first, "uma@wings.test")
    login(second, "uma@wings.test")
    first.post("/api/auth/change-password", json={
        "currentPassword": PASSWORD, "newPassword": "AnotherGoodPassword1",
        "confirmPassword": "AnotherGoodPassword1",
    })
    assert data(second.get("/api/auth/me"))["user"] is None
    assert data(first.get("/api/auth/me"))["user"] is not None


# ---------------------------------------------------------------------------
# role authorisation
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("path", [
    "/api/my-submissions",
    "/api/assets/next-code?department=Finance",
])
def test_authenticated_endpoints_reject_anonymous_callers(client, path):
    response = client.get(path)
    assert response.status_code == 401
    assert err(response)["code"] == "AUTH_REQUIRED"


def test_anonymous_cannot_submit_an_asset(client):
    from tests.conftest import asset_payload
    response = client.post("/api/assets", json=asset_payload())
    assert response.status_code == 401


@pytest.mark.parametrize("path", ["/api/admin/activity", "/api/admin/stats"])
def test_admin_endpoints_reject_a_plain_user(as_user, path):
    response = as_user.get(path)
    assert response.status_code == 403
    assert err(response)["code"] == "FORBIDDEN"


def test_reviewer_can_read_the_activity_log(as_reviewer):
    assert as_reviewer.get("/api/admin/activity").status_code == 200


def test_user_administration_is_admin_only(as_reviewer):
    assert as_reviewer.get("/api/admin/users").status_code == 403


def test_admin_can_administer_users(as_admin):
    assert as_admin.get("/api/admin/users").status_code == 200


def test_admin_cannot_demote_themselves(as_admin, admin_user):
    response = as_admin.patch("/api/admin/users/%d" % admin_user.id, json={"role": "user"})
    assert response.status_code == 409
    assert err(response)["code"] == "SELF_DEMOTION"


def test_login_and_failure_are_audited(client, app, normal_user):
    login(client, "uma@wings.test", "wrong")
    login(client, "uma@wings.test")
    client.post("/api/auth/logout")
    actions = [row.action for row in ActivityLog.query.all()]
    assert "login.failure" in actions
    assert "login.success" in actions
    assert "logout" in actions


def test_password_change_is_audited(as_user):
    as_user.post("/api/auth/change-password", json={
        "currentPassword": PASSWORD, "newPassword": "AnotherGoodPassword1",
        "confirmPassword": "AnotherGoodPassword1",
    })
    assert ActivityLog.query.filter_by(action="password.change").count() == 1
