"""CLI commands: admin creation, seeding, and the no-hard-coded-password rule."""
import os

import pytest

from app.extensions import db
from app.models import ActivityLog, Asset, ConfigSetting, User, WgtCounter
from tests.conftest import login

GOOD_PASSWORD = "CorrectHorseBattery9"


@pytest.fixture()
def runner(app):
    return app.test_cli_runner()


def test_create_admin_reads_the_password_from_the_environment(runner, app, monkeypatch):
    """Non-interactive deployments supply the password through systemd's
    EnvironmentFile or a secrets manager — never through an argument."""
    monkeypatch.setenv("ADMIN_INITIAL_PASSWORD", GOOD_PASSWORD)
    result = runner.invoke(args=["create-admin", "--email", "ada@wings.test",
                                 "--name", "Ada Admin"])
    assert result.exit_code == 0, result.output
    user = User.query.filter_by(email="ada@wings.test").one()
    assert user.role == "admin"
    assert user.is_active
    assert user.check_password(GOOD_PASSWORD)
    # The password never appears in the command output.
    assert GOOD_PASSWORD not in result.output


def test_create_admin_stores_only_a_hash(runner, monkeypatch):
    monkeypatch.setenv("ADMIN_INITIAL_PASSWORD", GOOD_PASSWORD)
    runner.invoke(args=["create-admin", "--email", "ada@wings.test"])
    user = User.query.filter_by(email="ada@wings.test").one()
    assert user.password_hash.startswith("pbkdf2:sha256:")
    assert GOOD_PASSWORD not in user.password_hash


def test_create_admin_refuses_a_weak_password(runner, monkeypatch):
    monkeypatch.setenv("ADMIN_INITIAL_PASSWORD", "short")
    result = runner.invoke(args=["create-admin", "--email", "ada@wings.test"])
    assert result.exit_code != 0
    assert "at least" in result.output
    assert User.query.count() == 0


def test_create_admin_refuses_the_prototype_default_password(runner, monkeypatch):
    """The HTML prototype shipped "Wings123+" in its source. It must not work."""
    monkeypatch.setenv("ADMIN_INITIAL_PASSWORD", "Wings123+Wings123+")
    result = runner.invoke(args=["create-admin", "--email", "ada@wings.test"])
    assert result.exit_code != 0
    assert User.query.count() == 0


def test_create_admin_fails_without_a_password_source(runner, monkeypatch):
    """No TTY and no environment variable: refuse rather than invent one."""
    monkeypatch.delenv("ADMIN_INITIAL_PASSWORD", raising=False)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    result = runner.invoke(args=["create-admin", "--email", "ada@wings.test"])
    assert result.exit_code != 0
    assert "secret store" in result.output.lower() or "no tty" in result.output.lower()
    assert User.query.count() == 0


def test_create_admin_refuses_to_clobber_an_existing_account(runner, monkeypatch, admin_user):
    monkeypatch.setenv("ADMIN_INITIAL_PASSWORD", GOOD_PASSWORD)
    result = runner.invoke(args=["create-admin", "--email", admin_user.email])
    assert result.exit_code != 0
    assert "already exists" in result.output


def test_create_admin_force_resets_an_existing_account(runner, monkeypatch, admin_user):
    monkeypatch.setenv("ADMIN_INITIAL_PASSWORD", GOOD_PASSWORD)
    result = runner.invoke(args=["create-admin", "--email", admin_user.email, "--force"])
    assert result.exit_code == 0, result.output
    db.session.refresh(admin_user)
    assert admin_user.check_password(GOOD_PASSWORD)


def test_create_admin_is_audited(runner, monkeypatch):
    monkeypatch.setenv("ADMIN_INITIAL_PASSWORD", GOOD_PASSWORD)
    runner.invoke(args=["create-admin", "--email", "ada@wings.test"])
    entry = ActivityLog.query.filter_by(action="user.create").one()
    assert entry.actor_email == "cli"
    assert entry.detail["role"] == "admin"


def test_created_admin_can_actually_sign_in(app, runner, monkeypatch):
    monkeypatch.setenv("ADMIN_INITIAL_PASSWORD", GOOD_PASSWORD)
    runner.invoke(args=["create-admin", "--email", "ada@wings.test"])
    client = app.test_client()
    response = login(client, "ada@wings.test", GOOD_PASSWORD)
    assert response.status_code == 200
    assert response.get_json()["data"]["user"]["role"] == "admin"
    assert client.get("/api/admin/users").status_code == 200


def test_create_user_supports_the_reviewer_role(runner, monkeypatch):
    monkeypatch.setenv("ADMIN_INITIAL_PASSWORD", GOOD_PASSWORD)
    result = runner.invoke(args=["create-user", "--email", "rob@wings.test",
                                 "--role", "reviewer"])
    assert result.exit_code == 0, result.output
    assert User.query.filter_by(email="rob@wings.test").one().role == "reviewer"


def test_set_role_changes_an_existing_account(runner, normal_user):
    result = runner.invoke(args=["set-role", "--email", normal_user.email,
                                 "--role", "reviewer"])
    assert result.exit_code == 0, result.output
    db.session.refresh(normal_user)
    assert normal_user.role == "reviewer"


def test_seed_config_writes_reference_rows_and_counters(runner):
    result = runner.invoke(args=["seed-config"])
    assert result.exit_code == 0, result.output
    assert db.session.get(ConfigSetting, "departments").value[0] == "Company Wide"
    assert db.session.get(WgtCounter, "2").last_sequence == 0
    assert WgtCounter.query.count() == 10


def test_seed_config_never_creates_assets(runner):
    """Requirement: no demo assets are ever seeded."""
    runner.invoke(args=["seed-config"])
    assert Asset.query.count() == 0
    result = runner.invoke(args=["seed-config"])
    assert "Assets in database: 0" in result.output


def test_seed_config_is_idempotent(runner):
    runner.invoke(args=["seed-config"])
    before = ConfigSetting.query.count()
    second = runner.invoke(args=["seed-config"])
    assert second.exit_code == 0
    assert ConfigSetting.query.count() == before
    assert "Reference rows written: 0" in second.output


def test_no_password_is_hard_coded_anywhere_in_the_source():
    """Grep the shipped source for the prototype's plaintext credential."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    offenders = []
    for directory, subdirs, filenames in os.walk(root):
        subdirs[:] = [d for d in subdirs if d not in
                      (".git", ".venv", "__pycache__", "node_modules", "var", "docs")]
        for filename in filenames:
            if not filename.endswith((".py", ".js", ".html", ".css", ".service", ".conf")):
                continue
            path = os.path.join(directory, filename)
            if path.startswith(os.path.join(root, "tests")):
                continue
            with open(path, "r", encoding="utf-8", errors="ignore") as handle:
                content = handle.read()
            if "Wings123+" in content and "startswith" not in content:
                offenders.append(path)
            if "defaultAdminPassword" in content:
                offenders.append(path)
    assert offenders == [], "hard-coded credential found in: %r" % offenders


def _strip_comments(source, relative):
    """Remove comments so a scan looks at code, not prose.

    The files legitimately *describe* what was removed ("Replaces the
    prototype's AdminAuth prompt", "there is no localStorage"), so scanning
    raw text would flag the documentation rather than any real usage.
    """
    import re
    if relative.endswith(".html"):
        return re.sub(r"<!--.*?-->|\{#.*?#\}", "", source, flags=re.S)
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    return re.sub(r"(?m)^\s*//.*$", "", source)


def test_no_browser_storage_calls_remain_in_the_frontend():
    """Every localStorage read/write from the prototype is gone."""
    import re
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    call_pattern = re.compile(r"\b(local|session)Storage\s*[.\[]")
    for relative in ("static/js/app.js", "templates/index.html"):
        with open(os.path.join(root, relative), encoding="utf-8") as handle:
            code = _strip_comments(handle.read(), relative)
        found = call_pattern.findall(code)
        assert found == [], "%s still calls browser storage: %r" % (relative, found)


def test_no_client_side_auth_or_mock_data_remains():
    """The prototype's AdminAuth module, its plaintext password and its
    mock-asset factory must not survive anywhere in the shipped frontend."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for relative in ("static/js/app.js", "templates/index.html"):
        with open(os.path.join(root, relative), encoding="utf-8") as handle:
            code = _strip_comments(handle.read(), relative)
        for banned in ("buildMockAssets", "AdminAuth", "defaultAdminPassword",
                       "wgt_agent_library_v1", "Wings123", "makeAsset("):
            assert banned not in code, "%s still references %s" % (relative, banned)


def test_prune_activity_respects_the_retention_window(runner, app, as_user):
    import datetime as dt
    from tests.conftest import create_asset
    create_asset(as_user)
    old = ActivityLog.query.first()
    old.ts = dt.datetime.utcnow() - dt.timedelta(days=900)
    db.session.commit()
    before = ActivityLog.query.count()
    result = runner.invoke(args=["prune-activity", "--days", "730", "--yes"])
    assert result.exit_code == 0, result.output
    assert ActivityLog.query.count() == before - 1


def test_show_status_reports_an_inventory(runner, as_user):
    from tests.conftest import create_asset
    create_asset(as_user)
    result = runner.invoke(args=["show-status"])
    assert result.exit_code == 0, result.output
    assert "Assets    : 1" in result.output
    assert "Pending Review" in result.output
    # The database password must never be printed.
    assert "password" not in result.output.lower()
