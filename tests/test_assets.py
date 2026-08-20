"""Asset creation, validation, search, filtering, update, versions, review."""
import pytest

from app.extensions import db
from app.models import ActivityLog, Asset, AssetVersion
from tests.conftest import asset_payload, create_asset, data, err


# ---------------------------------------------------------------------------
# creation & validation
# ---------------------------------------------------------------------------
def test_create_asset_returns_a_server_generated_record(as_user):
    asset = create_asset(as_user)
    assert isinstance(asset["id"], int)
    assert asset["wgtCode"] == "WGT201"           # Finance is digit 2
    assert asset["status"] == "Pending Review"
    assert asset["currentVersion"] == "1.0"
    assert asset["platform"] == "ChatGPT"
    assert sorted(asset["tags"]) == ["Analysis", "Reporting"]


def test_create_asset_records_an_initial_version(as_user, app):
    asset = create_asset(as_user)
    versions = AssetVersion.query.filter_by(asset_id=asset["id"]).all()
    assert len(versions) == 1
    assert versions[0].version_number == "1.0"
    assert versions[0].is_current is True


def test_create_asset_is_audited(as_user):
    create_asset(as_user)
    assert ActivityLog.query.filter_by(action="asset.submit").count() == 1


def test_required_fields_are_validated_on_the_server(as_user):
    response = as_user.post("/api/assets", json={"type": "gpt"})
    assert response.status_code == 400
    fields = err(response)["fields"]
    for required in ("department", "name", "description", "instructions",
                     "link", "ownerName", "ownerEmail"):
        assert required in fields


def test_invalid_department_is_rejected(as_user):
    response = as_user.post("/api/assets",
                            json=asset_payload(department="Ministry of Magic"))
    assert response.status_code == 400
    assert "department" in err(response)["fields"]


def test_invalid_asset_type_is_rejected(as_user):
    response = as_user.post("/api/assets", json=asset_payload(type="wizard"))
    assert response.status_code == 400
    assert "type" in err(response)["fields"]


def test_malformed_email_is_rejected(as_user):
    response = as_user.post("/api/assets", json=asset_payload(ownerEmail="not-an-email"))
    assert response.status_code == 400
    assert "ownerEmail" in err(response)["fields"]


@pytest.mark.parametrize("bad_link", [
    "javascript:alert(1)",
    "data:text/html,<script>alert(1)</script>",
    "file:///etc/passwd",
    "not a url",
])
def test_dangerous_link_schemes_are_rejected(as_user, bad_link):
    response = as_user.post("/api/assets", json=asset_payload(link=bad_link))
    assert response.status_code == 400
    assert "link" in err(response)["fields"]


def test_other_type_requires_naming_the_actual_tool(as_user):
    response = as_user.post("/api/assets", json=asset_payload(type="other", otherPlatform=""))
    assert response.status_code == 400
    assert "otherPlatform" in err(response)["fields"]


def test_tag_limit_is_enforced_on_the_server(as_user):
    response = as_user.post("/api/assets",
                            json=asset_payload(tags=["a", "b", "c", "d", "e"]))
    assert response.status_code == 400
    assert "tags" in err(response)["fields"]


def test_overlong_name_is_rejected(as_user):
    response = as_user.post("/api/assets", json=asset_payload(name="x" * 500))
    assert response.status_code == 400
    assert "name" in err(response)["fields"]


def test_a_plain_user_cannot_self_feature_an_asset(as_user):
    asset = create_asset(as_user, featured=True)
    assert asset["featured"] is False


# ---------------------------------------------------------------------------
# search, filtering, sorting, pagination
# ---------------------------------------------------------------------------
@pytest.fixture()
def catalogue(app, as_user, reviewer_user):
    """Three approved assets across three departments and three types.

    Each carries a distinct description so a search assertion can tell them
    apart. Approval runs on a SEPARATE reviewer client so ``as_user`` stays
    signed in for the test that uses this fixture.
    """
    from tests.conftest import login

    first = create_asset(as_user, name="Board Pack Commentary",
                         department="Finance", tags=["Reporting"],
                         description="Drafts monthly board commentary.")
    second = create_asset(as_user, name="Proposal Review Helper",
                          department="Sales", type="skill",
                          link="https://claude.ai/skills/proposal",
                          tags=["Compliance"],
                          description="Checks proposals against the playbook.")
    third = create_asset(as_user, name="Rota Planner", department="Operations",
                         type="agent",
                         link="https://copilotstudio.microsoft.com/x",
                         tags=["Automation"],
                         description="Builds the weekly duty rota.")

    reviewer = app.test_client()
    login(reviewer, reviewer_user.email)
    for asset in (first, second, third):
        assert reviewer.post("/api/assets/%d/approve" % asset["id"]).status_code == 200
    return {"finance": first, "sales": second, "ops": third}


def test_library_lists_only_approved_assets(client, catalogue, as_user):
    create_asset(as_user, name="Still Pending")     # remains Pending Review
    payload = data(client.get("/api/assets"))
    assert payload["total"] == 3
    assert all(item["status"] == "Approved" for item in payload["items"])


def test_search_matches_name_and_description(client, catalogue):
    assert data(client.get("/api/assets?q=commentary"))["total"] == 1
    assert data(client.get("/api/assets?q=Rota"))["total"] == 1
    assert data(client.get("/api/assets?q=nothing-matches-this"))["total"] == 0


def test_search_matches_the_wgt_code(client, catalogue):
    payload = data(client.get("/api/assets?q=WGT101"))
    assert payload["total"] == 1
    assert payload["items"][0]["department"] == "Sales"


def test_search_matches_tags(client, catalogue):
    assert data(client.get("/api/assets?q=Automation"))["total"] == 1


def test_search_treats_sql_wildcards_literally(client, catalogue):
    # "%" must not act as "match everything".
    assert data(client.get("/api/assets?q=%"))["total"] == 0
    assert data(client.get("/api/assets?q=_"))["total"] == 0


def test_sql_injection_attempt_is_harmless(client, catalogue):
    payload = data(client.get("/api/assets?q=' OR 1=1 --"))
    assert payload["total"] == 0
    assert Asset.query.count() == 3          # nothing dropped or altered


def test_filter_by_department(client, catalogue):
    payload = data(client.get("/api/assets?department=Finance"))
    assert payload["total"] == 1
    assert payload["items"][0]["department"] == "Finance"


def test_filter_by_asset_type(client, catalogue):
    assert data(client.get("/api/assets?asset_type=skill"))["total"] == 1
    assert data(client.get("/api/assets?asset_type=gpt"))["total"] == 1


def test_filter_by_tag(client, catalogue):
    assert data(client.get("/api/assets?tag=Reporting"))["total"] == 1


def test_unknown_type_filter_is_rejected(client, catalogue):
    assert client.get("/api/assets?asset_type=wizard").status_code == 400


def test_sorting_by_name(client, catalogue):
    names = [i["name"] for i in data(client.get("/api/assets?sort=name"))["items"]]
    assert names == sorted(names)
    reverse = [i["name"] for i in data(client.get("/api/assets?sort=-name"))["items"]]
    assert reverse == sorted(names, reverse=True)


def test_unknown_sort_key_falls_back_safely(client, catalogue):
    assert data(client.get("/api/assets?sort=;DROP TABLE assets"))["total"] == 3
    assert Asset.query.count() == 3


def test_pagination(client, catalogue):
    first = data(client.get("/api/assets?per_page=2&page=1"))
    assert len(first["items"]) == 2
    assert first["total"] == 3 and first["pages"] == 2
    second = data(client.get("/api/assets?per_page=2&page=2"))
    assert len(second["items"]) == 1
    ids = {i["id"] for i in first["items"]} | {i["id"] for i in second["items"]}
    assert len(ids) == 3


def test_page_size_is_capped(client, app, catalogue):
    payload = data(client.get("/api/assets?per_page=100000"))
    assert payload["perPage"] <= app.config["MAX_PAGE_SIZE"]


def test_plain_user_cannot_list_pending_assets(as_user):
    create_asset(as_user, name="Secret Pending")
    payload = data(as_user.get("/api/assets?status=Pending%20Review"))
    assert payload["total"] == 0


# ---------------------------------------------------------------------------
# detail visibility (IDOR)
# ---------------------------------------------------------------------------
def test_owner_can_see_their_own_pending_asset(as_user):
    asset = create_asset(as_user)
    assert as_user.get("/api/assets/%d" % asset["id"]).status_code == 200


def test_anonymous_cannot_see_a_pending_asset(client, as_user):
    asset = create_asset(as_user)
    as_user.post("/api/auth/logout")
    response = client.get("/api/assets/%d" % asset["id"])
    assert response.status_code == 404          # not 403 — do not confirm existence


def test_another_user_cannot_see_someone_elses_pending_asset(app, as_user):
    from tests.conftest import login, make_user, PASSWORD
    asset = create_asset(as_user)
    make_user("mallory@wings.test", "user")
    other = app.test_client()
    login(other, "mallory@wings.test", PASSWORD)
    assert other.get("/api/assets/%d" % asset["id"]).status_code == 404


def test_reviewer_can_see_any_pending_asset(app, as_user, reviewer_user):
    from tests.conftest import login
    asset = create_asset(as_user)
    reviewer = app.test_client()
    login(reviewer, reviewer_user.email)
    assert reviewer.get("/api/assets/%d" % asset["id"]).status_code == 200


def test_missing_asset_returns_404_envelope(client):
    response = client.get("/api/assets/999999")
    assert response.status_code == 404
    assert err(response)["code"] == "NOT_FOUND"


# ---------------------------------------------------------------------------
# update
# ---------------------------------------------------------------------------
def test_owner_can_update_their_pending_asset(as_user):
    asset = create_asset(as_user)
    response = as_user.put("/api/assets/%d" % asset["id"],
                           json=asset_payload(name="Renamed Asset", department=None))
    assert response.status_code == 200
    updated = data(response)["asset"]
    assert updated["name"] == "Renamed Asset"
    assert updated["wgtCode"] == asset["wgtCode"]       # unchanged
    assert updated["currentVersion"] == "1.1"           # bumped


def test_update_appends_rather_than_overwriting_version_history(as_user, app):
    asset = create_asset(as_user)
    as_user.put("/api/assets/%d" % asset["id"],
                json=asset_payload(name="Renamed", department=None))
    versions = AssetVersion.query.filter_by(asset_id=asset["id"]).all()
    assert len(versions) == 2
    assert {v.version_number for v in versions} == {"1.0", "1.1"}
    assert sum(1 for v in versions if v.is_current) == 1


def test_department_cannot_be_changed_through_update(as_user):
    asset = create_asset(as_user)
    response = as_user.put("/api/assets/%d" % asset["id"],
                           json=asset_payload(department="Sales"))
    assert response.status_code == 409
    assert err(response)["code"] == "DEPARTMENT_IMMUTABLE"


def test_wgt_code_cannot_be_changed_through_update(as_user):
    asset = create_asset(as_user)
    payload = asset_payload(department=None)
    payload["wgtCode"] = "WGT999"
    response = as_user.put("/api/assets/%d" % asset["id"], json=payload)
    assert response.status_code == 409
    assert err(response)["code"] == "IMMUTABLE_FIELD"


def test_asset_type_cannot_be_changed_through_update(as_user):
    asset = create_asset(as_user)
    response = as_user.put("/api/assets/%d" % asset["id"],
                           json=asset_payload(type="skill", department=None))
    assert response.status_code == 409


def test_a_stranger_cannot_update_someone_elses_asset(app, as_user):
    from tests.conftest import login, make_user, PASSWORD
    asset = create_asset(as_user)
    make_user("mallory@wings.test", "user")
    other = app.test_client()
    login(other, "mallory@wings.test", PASSWORD)
    response = other.put("/api/assets/%d" % asset["id"],
                         json=asset_payload(name="Hijacked", department=None))
    assert response.status_code == 404


def test_owner_cannot_edit_an_approved_asset(app, as_user, reviewer_user):
    from tests.conftest import login
    asset = create_asset(as_user)
    reviewer = app.test_client()
    login(reviewer, reviewer_user.email)
    reviewer.post("/api/assets/%d/approve" % asset["id"])
    response = as_user.put("/api/assets/%d" % asset["id"],
                           json=asset_payload(name="Sneaky", department=None))
    assert response.status_code == 403


def test_update_is_audited(as_user):
    asset = create_asset(as_user)
    as_user.put("/api/assets/%d" % asset["id"],
                json=asset_payload(name="Renamed", department=None))
    assert ActivityLog.query.filter_by(action="asset.update").count() == 1


# ---------------------------------------------------------------------------
# versions
# ---------------------------------------------------------------------------
def test_owner_can_add_a_version(as_user):
    asset = create_asset(as_user)
    response = as_user.post("/api/assets/%d/versions" % asset["id"],
                            json={"versionNumber": "2.0", "summary": "Rewrote the prompt."})
    assert response.status_code == 201
    payload = data(response)["asset"]
    assert payload["currentVersion"] == "2.0"
    assert [v["versionNumber"] for v in payload["versions"]][0] == "2.0"
    assert len(payload["versions"]) == 2


def test_version_summary_is_required(as_user):
    asset = create_asset(as_user)
    response = as_user.post("/api/assets/%d/versions" % asset["id"],
                            json={"versionNumber": "2.0", "summary": ""})
    assert response.status_code == 400
    assert "summary" in err(response)["fields"]


def test_duplicate_version_number_is_refused(as_user):
    asset = create_asset(as_user)
    as_user.post("/api/assets/%d/versions" % asset["id"],
                 json={"versionNumber": "2.0", "summary": "First"})
    response = as_user.post("/api/assets/%d/versions" % asset["id"],
                            json={"versionNumber": "2.0", "summary": "Again"})
    assert response.status_code == 409
    assert err(response)["code"] == "VERSION_EXISTS"


def test_malformed_version_number_is_refused(as_user):
    asset = create_asset(as_user)
    response = as_user.post("/api/assets/%d/versions" % asset["id"],
                            json={"versionNumber": "two point oh", "summary": "x"})
    assert response.status_code == 400


def test_a_stranger_cannot_add_a_version(app, as_user, reviewer_user):
    from tests.conftest import login, make_user, PASSWORD
    asset = create_asset(as_user)
    reviewer = app.test_client()
    login(reviewer, reviewer_user.email)
    reviewer.post("/api/assets/%d/approve" % asset["id"])
    make_user("mallory@wings.test", "user")
    other = app.test_client()
    login(other, "mallory@wings.test", PASSWORD)
    response = other.post("/api/assets/%d/versions" % asset["id"],
                          json={"versionNumber": "9.0", "summary": "mine now"})
    assert response.status_code == 403


# ---------------------------------------------------------------------------
# approve / reject / archive
# ---------------------------------------------------------------------------
def test_reviewer_can_approve(app, as_user, reviewer_user):
    from tests.conftest import login
    asset = create_asset(as_user)
    reviewer = app.test_client()
    login(reviewer, reviewer_user.email)
    payload = data(reviewer.post("/api/assets/%d/approve" % asset["id"]))["asset"]
    assert payload["status"] == "Approved"
    assert payload["publishedDate"]
    assert ActivityLog.query.filter_by(action="asset.approve").count() == 1


def test_plain_user_cannot_approve(as_user):
    asset = create_asset(as_user)
    response = as_user.post("/api/assets/%d/approve" % asset["id"])
    assert response.status_code == 403
    assert err(response)["code"] == "FORBIDDEN"


def test_anonymous_cannot_approve(client, as_user):
    asset = create_asset(as_user)
    as_user.post("/api/auth/logout")
    assert client.post("/api/assets/%d/approve" % asset["id"]).status_code == 401


def test_double_approval_is_a_conflict(app, as_user, reviewer_user):
    from tests.conftest import login
    asset = create_asset(as_user)
    reviewer = app.test_client()
    login(reviewer, reviewer_user.email)
    reviewer.post("/api/assets/%d/approve" % asset["id"])
    response = reviewer.post("/api/assets/%d/approve" % asset["id"])
    assert response.status_code == 409
    assert err(response)["code"] == "ALREADY_APPROVED"


def test_reviewer_can_reject_with_a_reason(app, as_user, reviewer_user):
    from tests.conftest import login
    asset = create_asset(as_user)
    reviewer = app.test_client()
    login(reviewer, reviewer_user.email)
    payload = data(reviewer.post("/api/assets/%d/reject" % asset["id"],
                                 json={"reason": "Duplicates WGT201."}))["asset"]
    assert payload["status"] == "Rejected"
    assert payload["rejectionReason"] == "Duplicates WGT201."
    assert ActivityLog.query.filter_by(action="asset.reject").count() == 1


def test_rejected_asset_disappears_from_the_library(client, app, as_user, reviewer_user):
    from tests.conftest import login
    asset = create_asset(as_user)
    reviewer = app.test_client()
    login(reviewer, reviewer_user.email)
    reviewer.post("/api/assets/%d/reject" % asset["id"], json={"reason": "no"})
    assert data(client.get("/api/assets"))["total"] == 0


def test_archive_removes_from_library_but_keeps_the_record(client, app, as_user, reviewer_user):
    from tests.conftest import login
    asset = create_asset(as_user)
    reviewer = app.test_client()
    login(reviewer, reviewer_user.email)
    reviewer.post("/api/assets/%d/approve" % asset["id"])
    assert data(client.get("/api/assets"))["total"] == 1
    payload = data(reviewer.post("/api/assets/%d/archive" % asset["id"],
                                 json={"reason": "Superseded."}))["asset"]
    assert payload["status"] == "Archived"
    assert payload["featured"] is False
    assert data(client.get("/api/assets"))["total"] == 0
    assert db.session.get(Asset, asset["id"]) is not None     # record preserved
    assert ActivityLog.query.filter_by(action="asset.archive").count() == 1


def test_governance_actions_append_to_version_history(app, as_user, reviewer_user):
    from tests.conftest import login
    asset = create_asset(as_user)
    reviewer = app.test_client()
    login(reviewer, reviewer_user.email)
    reviewer.post("/api/assets/%d/approve" % asset["id"])
    reviewer.post("/api/assets/%d/archive" % asset["id"], json={"reason": "x"})
    versions = AssetVersion.query.filter_by(asset_id=asset["id"]).all()
    # 1.0 submission + 1.1 approval + 1.2 archive — the original survives.
    assert len(versions) == 3
    assert any(v.version_number == "1.0" for v in versions)
    assert sum(1 for v in versions if v.is_current) == 1


# ---------------------------------------------------------------------------
# my submissions
# ---------------------------------------------------------------------------
def test_my_submissions_is_scoped_to_the_caller(app, as_user):
    from tests.conftest import login, make_user, PASSWORD
    create_asset(as_user, name="Mine")
    make_user("mallory@wings.test", "user")
    other = app.test_client()
    login(other, "mallory@wings.test", PASSWORD)
    create_asset(other, name="Theirs", department="Sales")

    mine = data(as_user.get("/api/my-submissions"))
    assert mine["total"] == 1
    assert mine["items"][0]["name"] == "Mine"

    theirs = data(other.get("/api/my-submissions"))
    assert theirs["total"] == 1
    assert theirs["items"][0]["name"] == "Theirs"


def test_my_submissions_buckets_by_status(as_user):
    create_asset(as_user)
    payload = data(as_user.get("/api/my-submissions"))
    assert payload["counts"]["Pending Review"] == 1
    assert payload["counts"]["Approved"] == 0


# ---------------------------------------------------------------------------
# duplicate detection
# ---------------------------------------------------------------------------
def test_duplicate_check_flags_a_near_match(as_user):
    create_asset(as_user, name="Board Pack Commentary")
    payload = data(as_user.get(
        "/api/assets/duplicate-check?department=Finance&name=Board%20Pack%20Commentary"))
    assert len(payload["duplicates"]) == 1


def test_duplicate_check_ignores_other_departments(as_user):
    create_asset(as_user, name="Board Pack Commentary")
    payload = data(as_user.get(
        "/api/assets/duplicate-check?department=Sales&name=Board%20Pack%20Commentary"))
    assert payload["duplicates"] == []
