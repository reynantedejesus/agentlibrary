"""WGT code allocation: format, uniqueness, concurrency and immutability."""
import threading

import pytest
from sqlalchemy.exc import IntegrityError

from app import reference, wgt
from app.extensions import db
from app.models import Asset, WgtCounter
from tests.conftest import asset_payload, create_asset, data, err


def test_department_digit_follows_the_numbering_order(app):
    assert reference.department_digit("Sales") == "1"
    assert reference.department_digit("Finance") == "2"
    assert reference.department_digit("Other") == "9"
    assert reference.department_digit("Company Wide") == "10"


def test_unknown_department_falls_back_to_other(app):
    assert reference.department_digit("Ministry of Magic") == \
        reference.department_digit("Other")


def test_code_format(app):
    assert wgt.format_code("WGT2", 1) == "WGT201"
    assert wgt.format_code("WGT2", 42) == "WGT242"
    assert wgt.format_code("WGT10", 3) == "WGT1003"


def test_sequences_past_99_grow_a_digit(app):
    assert wgt.format_code("WGT2", 100) == "WGT2100"


@pytest.mark.parametrize("code,digit,expected", [
    ("WGT201", "2", 1),        # Finance, first
    ("WGT101", "1", 1),        # Sales, first
    ("WGT1001", "10", 1),      # Company Wide, first
    ("WGT1001", "1", None),    # a leading-zero tail is not Sales' code
    ("WGT1100", "1", 100),     # Sales, hundredth
    ("WGT1100", "10", None),   # tail too short for Company Wide
    ("WGT10100", "10", 100),   # Company Wide, hundredth
    ("WGT10100", "1", None),
])
def test_prefix_ambiguity_is_resolved(app, code, digit, expected):
    """Sales (digit 1) and Company Wide (digit 10) share a textual prefix."""
    assert wgt._parse_sequence(code, digit) == expected


def test_sequential_codes_increment_within_a_department(as_user):
    first = create_asset(as_user, name="One")
    second = create_asset(as_user, name="Two")
    third = create_asset(as_user, name="Three")
    assert [first["wgtCode"], second["wgtCode"], third["wgtCode"]] == \
        ["WGT201", "WGT202", "WGT203"]


def test_departments_have_independent_sequences(as_user):
    finance = create_asset(as_user, name="Finance One", department="Finance")
    sales = create_asset(as_user, name="Sales One", department="Sales")
    finance_two = create_asset(as_user, name="Finance Two", department="Finance")
    assert finance["wgtCode"] == "WGT201"
    assert sales["wgtCode"] == "WGT101"
    assert finance_two["wgtCode"] == "WGT202"


def test_wgt_code_has_a_unique_database_constraint(app, as_user):
    """The constraint is the backstop behind the counter."""
    asset = create_asset(as_user)
    duplicate = Asset(
        wgt_code=asset["wgtCode"],            # deliberate collision
        name="Collision", asset_type="gpt", platform="ChatGPT",
        department="Finance", status="Pending Review",
        description="x", use_case="x", owner_name="x", owner_email="x@y.co",
        creator_name="x", submitted_by_name="x", configuration={},
        additional_departments=[],
    )
    db.session.add(duplicate)
    with pytest.raises(IntegrityError):
        db.session.commit()
    db.session.rollback()
    assert Asset.query.filter_by(wgt_code=asset["wgtCode"]).count() == 1


def test_allocation_retries_past_an_imported_collision(app, as_user):
    """A code inserted outside the counter must not be handed out again."""
    create_asset(as_user, name="First")                  # WGT201
    # Simulate a legacy import that claimed WGT202 without touching the counter.
    legacy = Asset(
        wgt_code="WGT202", name="Legacy Import", asset_type="gpt",
        platform="ChatGPT", department="Finance", status="Approved",
        description="x", use_case="x", owner_name="x", owner_email="x@y.co",
        creator_name="x", submitted_by_name="x", configuration={},
        additional_departments=[],
    )
    db.session.add(legacy)
    db.session.commit()
    counter = db.session.get(WgtCounter, "2")
    counter.last_sequence = 1                            # deliberately stale
    db.session.commit()

    third = create_asset(as_user, name="Third")
    assert third["wgtCode"] == "WGT203"                  # skipped the taken code
    assert Asset.query.filter_by(wgt_code="WGT202").count() == 1


def test_concurrent_allocation_produces_no_duplicates(app, upload_dir):
    """Twelve simultaneous submissions across four threads.

    Each thread builds its own Flask app, its own database session and its own
    client against one shared SQLite file, so the allocations really do
    interleave. The counter row lock (``SELECT ... FOR UPDATE`` on MySQL; on
    SQLite the file write lock) plus the UNIQUE constraint on
    ``assets.wgt_code`` and the bounded retry in ``wgt.allocate_with_retry``
    must still produce twelve distinct codes with no gaps.
    """
    import os
    import tempfile

    from app import create_app
    from app.config import TestingConfig
    from app.models import User
    from tests.conftest import PASSWORD, login

    db_path = os.path.join(tempfile.mkdtemp(), "concurrent.db")
    url = "sqlite:///" + db_path

    # Flask-SQLAlchemy builds its engine inside init_app(), so the URI has to
    # be in place BEFORE create_app() runs — patching app.config afterwards
    # would leave every worker pointing at the default in-memory database.
    original_uri = TestingConfig.SQLALCHEMY_DATABASE_URI
    original_options = TestingConfig.SQLALCHEMY_ENGINE_OPTIONS
    TestingConfig.SQLALCHEMY_DATABASE_URI = url
    # SQLite's default 5s lock timeout is too short once four writers contend.
    TestingConfig.SQLALCHEMY_ENGINE_OPTIONS = {"connect_args": {"timeout": 60}}

    try:
        maker = create_app("testing")
        maker.config.update(UPLOAD_DIR=upload_dir, WTF_CSRF_ENABLED=False)
        with maker.app_context():
            db.create_all()
            user = User(email="racer@wings.test", full_name="Racer", role="user")
            user.set_password(PASSWORD)
            db.session.add(user)
            db.session.commit()
            wgt.ensure_counter_rows()

        codes = []
        errors = []
        lock = threading.Lock()
        start_line = threading.Barrier(4)

        def submit(index):
            try:
                worker = create_app("testing")
                worker.config.update(UPLOAD_DIR=upload_dir, WTF_CSRF_ENABLED=False)
                with worker.app_context():
                    client = worker.test_client()
                    signin = login(client, "racer@wings.test", PASSWORD)
                    if signin.status_code != 200:
                        with lock:
                            errors.append(("login", signin.status_code, signin.get_json()))
                        return
                    start_line.wait(timeout=30)   # all four submit at once
                    for step in range(3):
                        response = client.post("/api/assets", json=asset_payload(
                            name="Racer %d-%d" % (index, step), department="Finance"))
                        if response.status_code != 201:
                            with lock:
                                errors.append((response.status_code, response.get_json()))
                            continue
                        with lock:
                            codes.append(response.get_json()["data"]["asset"]["wgtCode"])
            except Exception as exc:                     # pragma: no cover
                with lock:
                    errors.append(repr(exc))

        threads = [threading.Thread(target=submit, args=(i,)) for i in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=120)

        assert not errors, errors
        assert len(codes) == 12
        assert len(set(codes)) == 12, "duplicate WGT codes: %r" % sorted(codes)
        # Twelve consecutive codes with no gaps and no repeats.
        assert sorted(codes) == sorted("WGT2%02d" % n for n in range(1, 13))

        checker = create_app("testing")
        with checker.app_context():
            stored = [a.wgt_code for a in Asset.query.all()]
            assert len(stored) == 12
            assert len(set(stored)) == 12
            assert db.session.get(WgtCounter, "2").last_sequence == 12
    finally:
        TestingConfig.SQLALCHEMY_DATABASE_URI = original_uri
        TestingConfig.SQLALCHEMY_ENGINE_OPTIONS = original_options


def test_preview_is_not_a_reservation(as_user):
    first = data(as_user.get("/api/assets/next-code?department=Finance"))["preview"]
    second = data(as_user.get("/api/assets/next-code?department=Finance"))["preview"]
    assert first == second == "WGT201"
    assert data(as_user.get("/api/assets/next-code?department=Finance"))["authoritative"] is False


def test_preview_rejects_an_unknown_department(as_user):
    response = as_user.get("/api/assets/next-code?department=Atlantis")
    assert response.status_code == 400
    assert "department" in err(response)["fields"]


def test_admin_department_migration_mints_a_new_code(app, as_user, admin_user):
    from tests.conftest import login
    asset = create_asset(as_user, department="Finance")
    assert asset["wgtCode"] == "WGT201"
    admin = app.test_client()
    login(admin, admin_user.email)
    payload = data(admin.post("/api/admin/assets/%d/migrate-department" % asset["id"],
                              json={"department": "Tech", "reason": "Team moved."}))
    assert payload["previousCode"] == "WGT201"
    assert payload["asset"]["wgtCode"] == "WGT601"          # Tech is digit 6
    assert payload["asset"]["department"] == "Tech"


def test_department_migration_is_admin_only(app, as_user, reviewer_user):
    from tests.conftest import login
    asset = create_asset(as_user)
    reviewer = app.test_client()
    login(reviewer, reviewer_user.email)
    response = reviewer.post("/api/admin/assets/%d/migrate-department" % asset["id"],
                             json={"department": "Tech", "reason": "why not"})
    assert response.status_code == 403


def test_department_migration_requires_a_reason(app, as_user, admin_user):
    from tests.conftest import login
    asset = create_asset(as_user)
    admin = app.test_client()
    login(admin, admin_user.email)
    response = admin.post("/api/admin/assets/%d/migrate-department" % asset["id"],
                          json={"department": "Tech"})
    assert response.status_code == 400
    assert "reason" in err(response)["fields"]


def test_department_migration_is_audited(app, as_user, admin_user):
    from tests.conftest import login
    from app.models import ActivityLog
    asset = create_asset(as_user)
    admin = app.test_client()
    login(admin, admin_user.email)
    admin.post("/api/admin/assets/%d/migrate-department" % asset["id"],
               json={"department": "Tech", "reason": "Team moved."})
    entry = ActivityLog.query.filter_by(action="asset.department_migrate").one()
    assert entry.detail["fromCode"] == "WGT201"
    assert entry.detail["toCode"] == "WGT601"
    assert entry.detail["reason"] == "Team moved."
