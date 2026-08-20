"""Environment-file loading and the SECRET_KEY startup guard.

Regression cover for a real deployment failure: running
``gunicorn -c gunicorn.conf.py wsgi:application`` by hand aborted with
"SECRET_KEY is not set", because systemd's ``EnvironmentFile=`` only applies to
the service it starts — a plain shell reads nothing. The loader below makes
manual invocations (gunicorn, ``flask db upgrade``, ``flask create-admin``) see
the same configuration, and the error message now explains itself when they
still cannot.
"""
from __future__ import annotations

import os
import textwrap

import pytest

import app as app_module
from app import create_app

SECRET = "x" * 64
# A MySQL URL: SQLAlchemy builds the engine without connecting, and production
# config carries pool options that SQLite's StaticPool rejects.
MYSQL_URL = "mysql+pymysql://u:p@127.0.0.1:3306/agentlibrary"


@pytest.fixture()
def env_file(tmp_path):
    """A realistic production env file, including awkward values."""
    path = tmp_path / "agentlibrary.env"
    path.write_text(textwrap.dedent("""\
        # Agent Library production environment
        FLASK_ENV=production
        SECRET_KEY={secret}

        # A password containing a space and a hash — the shape that broke the
        # `env $(grep ... | xargs)` workaround this loader replaces.
        MYSQL_PASSWORD=p@ss word#1
        QUOTED_VALUE="spaced   value"
        SINGLE_QUOTED='single value'
        export EXPORTED_VALUE=from-export
        SESSION_COOKIE_SECURE=0
        DATABASE_URL=mysql+pymysql://u:p@127.0.0.1:3306/agentlibrary
        """).format(secret=SECRET))
    return path


@pytest.fixture(autouse=True)
def isolated_environment():
    """Snapshot and restore os.environ around every test in this module.

    monkeypatch is not enough here: the loader under test calls
    ``load_dotenv``, which writes straight into ``os.environ`` where monkeypatch
    cannot see it. Without a wholesale restore those variables would leak into
    the rest of the suite — and, because create_app() now re-applies the
    environment over the config class, a stray DATABASE_URL would send other
    test modules at a MySQL server that is not there.
    """
    snapshot = dict(os.environ)
    for key in ("ENV_FILE", "AGENTLIBRARY_ENV_FILE", "SECRET_KEY",
                "MYSQL_PASSWORD", "QUOTED_VALUE", "SINGLE_QUOTED",
                "EXPORTED_VALUE", "DATABASE_URL", "SESSION_COOKIE_SECURE"):
        os.environ.pop(key, None)
    os.environ["FLASK_ENV"] = "production"
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(snapshot)


# ---------------------------------------------------------------------------
# loading
# ---------------------------------------------------------------------------
def test_env_file_lets_a_manual_run_start(monkeypatch, env_file):
    """The exact failure the operator hit, now resolved."""
    monkeypatch.setenv("ENV_FILE", str(env_file))
    application = create_app()
    assert application.config["SECRET_KEY"] == SECRET
    assert application.debug is False


def test_values_with_spaces_and_hashes_survive(monkeypatch, env_file):
    monkeypatch.setenv("ENV_FILE", str(env_file))
    create_app()
    assert os.environ["MYSQL_PASSWORD"] == "p@ss word#1"
    assert os.environ["QUOTED_VALUE"] == "spaced   value"
    assert os.environ["SINGLE_QUOTED"] == "single value"
    assert os.environ["EXPORTED_VALUE"] == "from-export"


def test_existing_environment_wins_over_the_file(monkeypatch, env_file):
    """systemd's EnvironmentFile= (or a deliberate export) must not be clobbered."""
    monkeypatch.setenv("ENV_FILE", str(env_file))
    monkeypatch.setenv("SECRET_KEY", "set-by-systemd-" + "y" * 40)
    application = create_app()
    assert application.config["SECRET_KEY"].startswith("set-by-systemd-")


def test_production_auto_loads_the_documented_path(monkeypatch, env_file):
    """No ENV_FILE set: fall back to /etc/agentlibrary/agentlibrary.env."""
    monkeypatch.setattr(app_module, "DEFAULT_PRODUCTION_ENV_FILE", str(env_file))
    application = create_app()
    assert application.config["SECRET_KEY"] == SECRET


def test_development_still_reads_dotenv_not_the_production_path(monkeypatch, tmp_path):
    monkeypatch.setenv("FLASK_ENV", "development")
    monkeypatch.setattr(app_module, "DEFAULT_PRODUCTION_ENV_FILE", "/nonexistent/prod.env")
    monkeypatch.setattr(app_module, "PROJECT_ROOT", str(tmp_path))
    (tmp_path / ".env").write_text(
        "SECRET_KEY=" + SECRET + "\nDATABASE_URL=" + MYSQL_URL + "\n")
    application = create_app()
    assert application.config["SECRET_KEY"] == SECRET


def test_a_missing_default_file_is_not_fatal(monkeypatch):
    """Supplying everything through the shell is a valid deployment."""
    monkeypatch.setattr(app_module, "DEFAULT_PRODUCTION_ENV_FILE", "/nonexistent/prod.env")
    monkeypatch.setenv("SECRET_KEY", SECRET)
    monkeypatch.setenv("DATABASE_URL", MYSQL_URL)
    assert create_app().config["SECRET_KEY"] == SECRET


def test_an_explicit_env_file_that_is_missing_fails_loudly(monkeypatch):
    """A typo in ENV_FILE must not degrade into a confusing SECRET_KEY error."""
    monkeypatch.setenv("ENV_FILE", "/nonexistent/typo.env")
    with pytest.raises(RuntimeError) as excinfo:
        create_app()
    assert "does not exist" in str(excinfo.value)
    assert "/nonexistent/typo.env" in str(excinfo.value)


def test_agentlibrary_env_file_alias_is_honoured(monkeypatch, env_file):
    monkeypatch.setenv("AGENTLIBRARY_ENV_FILE", str(env_file))
    assert create_app().config["SECRET_KEY"] == SECRET


def test_unreadable_explicit_env_file_names_the_permission_problem(monkeypatch, env_file):
    """The 0640 root:agentlibrary case, run as the wrong user.

    os.access is patched rather than chmod-ing the file, because the suite may
    run as root — for whom permissions do not apply.
    """
    real_access = os.access
    monkeypatch.setattr(
        os, "access",
        lambda path, mode, *a, **k: False if str(path) == str(env_file)
        else real_access(path, mode, *a, **k))
    monkeypatch.setenv("ENV_FILE", str(env_file))
    with pytest.raises(RuntimeError) as excinfo:
        create_app()
    message = str(excinfo.value)
    assert "not readable" in message
    assert "sudo -u agentlibrary" in message
    assert "agentlibrary group" in message


def test_the_builtin_parser_matches_dotenv(env_file):
    """The fallback used when python-dotenv is absent."""
    parsed = app_module._parse_env_file(str(env_file))
    assert parsed["SECRET_KEY"] == SECRET
    assert parsed["MYSQL_PASSWORD"] == "p@ss word#1"
    assert parsed["QUOTED_VALUE"] == "spaced   value"
    assert parsed["SINGLE_QUOTED"] == "single value"
    assert parsed["EXPORTED_VALUE"] == "from-export"
    assert "# Agent Library production environment" not in parsed


def test_the_builtin_parser_is_used_without_dotenv(monkeypatch, env_file):
    """Simulate python-dotenv not being installed."""
    import builtins
    real_import = builtins.__import__

    def no_dotenv(name, *args, **kwargs):
        if name == "dotenv":
            raise ImportError("simulated: python-dotenv not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_dotenv)
    monkeypatch.setenv("ENV_FILE", str(env_file))
    application = create_app()
    assert application.config["SECRET_KEY"] == SECRET
    assert os.environ["MYSQL_PASSWORD"] == "p@ss word#1"


# ---------------------------------------------------------------------------
# the SECRET_KEY guard itself
# ---------------------------------------------------------------------------
def test_production_still_refuses_to_start_without_a_key(monkeypatch):
    """The safety property must survive this change."""
    monkeypatch.setattr(app_module, "DEFAULT_PRODUCTION_ENV_FILE", "/nonexistent/prod.env")
    with pytest.raises(RuntimeError) as excinfo:
        create_app()
    assert "SECRET_KEY is not set" in str(excinfo.value)


def test_the_error_explains_a_missing_file(monkeypatch):
    monkeypatch.setattr(app_module, "DEFAULT_PRODUCTION_ENV_FILE", "/nonexistent/prod.env")
    with pytest.raises(RuntimeError) as excinfo:
        create_app()
    message = str(excinfo.value)
    assert "No environment file was found at /nonexistent/prod.env" in message
    assert "ENV_FILE=" in message
    # And it names the systemd-vs-shell distinction that causes this.
    assert "EnvironmentFile=" in message


def test_the_error_explains_a_file_that_lacks_the_key(monkeypatch, tmp_path):
    incomplete = tmp_path / "agentlibrary.env"
    incomplete.write_text("FLASK_ENV=production\nDATABASE_URL=" + MYSQL_URL + "\n")
    monkeypatch.setenv("ENV_FILE", str(incomplete))
    with pytest.raises(RuntimeError) as excinfo:
        create_app()
    message = str(excinfo.value)
    assert "does not define SECRET_KEY" in message
    assert str(incomplete) in message


def test_the_error_never_prints_a_secret(monkeypatch, tmp_path):
    """A diagnostic that leaks the file's contents would be worse than useless."""
    leaky = tmp_path / "agentlibrary.env"
    leaky.write_text(
        "MYSQL_PASSWORD=hunter2-do-not-print\nDATABASE_URL=" + MYSQL_URL + "\n")
    monkeypatch.setenv("ENV_FILE", str(leaky))
    with pytest.raises(RuntimeError) as excinfo:
        create_app()
    assert "hunter2" not in str(excinfo.value)


def test_the_dev_placeholder_key_is_rejected_in_production(monkeypatch):
    monkeypatch.setattr(app_module, "DEFAULT_PRODUCTION_ENV_FILE", "/nonexistent/prod.env")
    monkeypatch.setenv("SECRET_KEY", "dev-only-not-a-real-secret")
    with pytest.raises(RuntimeError):
        create_app()


def test_development_tolerates_the_placeholder_key(monkeypatch, tmp_path):
    monkeypatch.setenv("FLASK_ENV", "development")
    monkeypatch.setattr(app_module, "PROJECT_ROOT", str(tmp_path))
    monkeypatch.setattr(app_module, "DEFAULT_PRODUCTION_ENV_FILE", "/nonexistent/prod.env")
    monkeypatch.setenv("DATABASE_URL", MYSQL_URL)
    assert create_app() is not None


# ---------------------------------------------------------------------------
# the environment surface is an allowlist
# ---------------------------------------------------------------------------
def test_debug_cannot_be_switched_on_from_the_environment_file(monkeypatch, tmp_path):
    """DEBUG is deliberately outside the supported environment surface.

    Flask's debugger executes arbitrary code from the browser, so an operator
    (or anyone who can edit the env file) must not be able to turn it on in
    production by adding a line to it.
    """
    path = tmp_path / "agentlibrary.env"
    path.write_text(
        "SECRET_KEY={0}\nDATABASE_URL={1}\n"
        "DEBUG=1\nTESTING=1\nFLASK_DEBUG=1\n".format(SECRET, MYSQL_URL))
    monkeypatch.setenv("ENV_FILE", str(path))
    application = create_app()
    assert application.config["DEBUG"] is False
    assert application.config["TESTING"] is False
    assert application.debug is False


def test_supported_settings_are_applied_with_the_right_types(monkeypatch, tmp_path):
    path = tmp_path / "agentlibrary.env"
    path.write_text(
        "SECRET_KEY={0}\nDATABASE_URL={1}\n"
        "SESSION_COOKIE_SECURE=0\n"
        "MAX_CONTENT_LENGTH=1048576\n"
        "TRUSTED_HOSTS=a.example.com, b.example.com\n"
        "SESSION_LIFETIME_SECONDS=3600\n".format(SECRET, MYSQL_URL))
    monkeypatch.setenv("ENV_FILE", str(path))
    application = create_app()
    config = application.config
    assert config["SESSION_COOKIE_SECURE"] is False          # bool, not "0"
    assert config["MAX_CONTENT_LENGTH"] == 1048576           # int, not "1048576"
    assert config["TRUSTED_HOSTS"] == ["a.example.com", "b.example.com"]
    # An aliased variable name still reaches the right config key. Flask stores
    # the raw seconds and converts on attribute access.
    assert config["PERMANENT_SESSION_LIFETIME"] == 3600
    assert application.permanent_session_lifetime.total_seconds() == 3600


def test_config_is_immune_to_import_order(monkeypatch, tmp_path):
    """Regression: config classes used to read os.environ in their class body.

    A stray ``import app.config`` before create_app() would then freeze those
    reads against an environment the env file had not populated yet, and the
    app would start with silently wrong settings.
    """
    import app.config                                        # noqa: F401
    path = tmp_path / "agentlibrary.env"
    path.write_text(
        "SECRET_KEY={0}\nDATABASE_URL={1}\nUPLOAD_DIR=/srv/late/uploads\n"
        .format(SECRET, MYSQL_URL))
    monkeypatch.setenv("ENV_FILE", str(path))
    config = create_app().config
    assert config["SECRET_KEY"] == SECRET
    assert config["UPLOAD_DIR"] == "/srv/late/uploads"


def test_database_url_is_assembled_from_mysql_parts(monkeypatch, tmp_path):
    path = tmp_path / "agentlibrary.env"
    path.write_text(
        "SECRET_KEY={0}\n"
        "MYSQL_HOST=db.internal\nMYSQL_PORT=3307\nMYSQL_DATABASE=al\n"
        "MYSQL_USER=al_user\nMYSQL_PASSWORD=p@ss word/1\n".format(SECRET))
    monkeypatch.setenv("ENV_FILE", str(path))
    uri = create_app().config["SQLALCHEMY_DATABASE_URI"]
    assert "db.internal:3307" in uri
    assert "/al?charset=utf8mb4" in uri
    # Characters that would otherwise break the URL must be percent-encoded.
    assert "p%40ss+word%2F1" in uri or "p%40ss%20word%2F1" in uri
