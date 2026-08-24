"""The "Update an Agent" workflow.

Anyone can propose a change to a published asset; nothing moves until an
administrator accepts it. These tests cover that separation — that a request
never touches the asset on its own, that only an unlocked console can resolve
one, that accepting applies every ticked field in one transaction with a
version entry, and that declining changes nothing.
"""
import pytest

from app.extensions import db
from app.models import ActivityLog, Asset, AssetFile, AssetVersion, UpdateRequest
from tests.conftest import (create_asset, data, err, publish, stage_file,
                            unlock, update_request_payload)


@pytest.fixture()
def published(app, visitor, admin_user):
    """One published asset, with the console left locked."""
    return publish(app, visitor)


# ---------------------------------------------------------------------------
# raising a request
# ---------------------------------------------------------------------------
def test_anyone_can_raise_a_request(visitor, published):
    response = visitor.post("/api/update-requests",
                            json=update_request_payload(published["id"]))
    assert response.status_code == 201
    record = data(response)["updateRequest"]
    assert record["status"] == "Open"
    assert record["fields"] == ["description"]
    assert record["fieldLabels"] == ["Description"]
    assert record["wgtCode"] == published["wgtCode"]


def test_raising_a_request_does_not_touch_the_asset(visitor, published):
    before = data(visitor.get("/api/assets/%d" % published["id"]))["asset"]
    visitor.post("/api/update-requests", json=update_request_payload(published["id"]))
    after = data(visitor.get("/api/assets/%d" % published["id"]))["asset"]
    assert after["description"] == before["description"]
    assert after["currentVersion"] == before["currentVersion"]
    assert AssetVersion.query.filter_by(asset_id=published["id"]).count() == 2


def test_the_request_snapshots_the_asset_name_and_code(visitor, published):
    data(visitor.post("/api/update-requests",
                      json=update_request_payload(published["id"])))
    record = UpdateRequest.query.one()
    assert record.wgt_code == published["wgtCode"]
    assert record.asset_name == published["name"]
    assert record.submitted_ip == "127.0.0.1"


def test_a_request_against_an_unpublished_asset_is_refused(visitor):
    pending = create_asset(visitor)
    response = visitor.post("/api/update-requests",
                            json=update_request_payload(pending["id"]))
    assert response.status_code == 404


def test_a_request_against_an_unknown_asset_is_refused(visitor):
    response = visitor.post("/api/update-requests",
                            json=update_request_payload(999999))
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------
def test_at_least_one_field_must_be_selected(visitor, published):
    response = visitor.post("/api/update-requests",
                            json=update_request_payload(published["id"], fields=[]))
    assert response.status_code == 400
    assert "fields" in err(response)["fields"]


def test_a_selected_field_must_carry_a_value(visitor, published):
    response = visitor.post("/api/update-requests", json=update_request_payload(
        published["id"], fields=["description"], proposed={"description": "   "}))
    assert response.status_code == 400
    assert "proposed" in err(response)["fields"]


def test_a_field_the_asset_type_does_not_offer_is_refused(visitor, published):
    """A ChatGPT GPT has no "Context" field — that belongs to a Claude Skill."""
    response = visitor.post("/api/update-requests", json=update_request_payload(
        published["id"], fields=["context"], proposed={"context": "x"}))
    assert response.status_code == 400
    assert "fields" in err(response)["fields"]


def test_requester_name_and_email_are_required(visitor, published):
    response = visitor.post("/api/update-requests", json=update_request_payload(
        published["id"], requesterName="", requesterEmail=""))
    assert response.status_code == 400
    fields = err(response)["fields"]
    assert "requesterName" in fields and "requesterEmail" in fields


def test_a_malformed_requester_email_is_refused(visitor, published):
    response = visitor.post("/api/update-requests", json=update_request_payload(
        published["id"], requesterEmail="not-an-email"))
    assert response.status_code == 400


def test_a_proposed_link_must_be_a_safe_url(visitor, published):
    response = visitor.post("/api/update-requests", json=update_request_payload(
        published["id"], fields=["link"],
        proposed={"link": "javascript:alert(1)"}))
    assert response.status_code == 400
    assert "proposed" in err(response)["fields"]


def test_a_proposed_model_must_be_one_of_the_options(visitor, published):
    response = visitor.post("/api/update-requests", json=update_request_payload(
        published["id"], fields=["preferredModel"],
        proposed={"preferredModel": "Something Invented"}))
    assert response.status_code == 400


def test_the_owner_field_needs_a_name_or_an_email(visitor, published):
    response = visitor.post("/api/update-requests", json=update_request_payload(
        published["id"], fields=["owner"], proposed={}))
    assert response.status_code == 400


# ---------------------------------------------------------------------------
# authorization
# ---------------------------------------------------------------------------
def test_the_queue_needs_the_admin_password(visitor, published):
    assert visitor.get("/api/update-requests").status_code == 401


def test_reading_one_request_needs_the_admin_password(visitor, published):
    record = data(visitor.post("/api/update-requests",
                               json=update_request_payload(published["id"])))["updateRequest"]
    assert visitor.get("/api/update-requests/%d" % record["id"]).status_code == 401


@pytest.mark.parametrize("action", ["accept", "decline"])
def test_resolving_needs_the_admin_password(visitor, published, action):
    record = data(visitor.post("/api/update-requests",
                               json=update_request_payload(published["id"])))["updateRequest"]
    response = visitor.post("/api/update-requests/%d/%s" % (record["id"], action))
    assert response.status_code == 401
    assert UpdateRequest.query.one().status == "Open"


# ---------------------------------------------------------------------------
# accepting
# ---------------------------------------------------------------------------
def _raise_and_unlock(app, visitor, asset_id, **overrides):
    record = data(visitor.post("/api/update-requests",
                               json=update_request_payload(asset_id, **overrides)))["updateRequest"]
    admin = app.test_client()
    unlock(admin)
    return record, admin


def test_accepting_applies_the_proposed_value(app, visitor, published):
    record, admin = _raise_and_unlock(app, visitor, published["id"])
    payload = data(admin.post("/api/update-requests/%d/accept" % record["id"]))
    assert payload["updateRequest"]["status"] == "Accepted"
    assert payload["asset"]["description"] == "Now drafts board and exec commentary."


def test_accepting_appends_a_version_naming_the_fields(app, visitor, published):
    record, admin = _raise_and_unlock(app, visitor, published["id"])
    before = AssetVersion.query.filter_by(asset_id=published["id"]).count()
    payload = data(admin.post("/api/update-requests/%d/accept" % record["id"]))
    versions = payload["asset"]["versions"]
    assert len(versions) == before + 1
    assert "Update Request" in versions[0]["summary"]
    assert "Description" in versions[0]["summary"]
    assert "Uma User" in versions[0]["summary"]


def test_accepting_applies_several_fields_at_once(app, visitor, published):
    record, admin = _raise_and_unlock(
        app, visitor, published["id"],
        fields=["name", "description", "tags", "link"],
        proposed={
            "name": "Exec Commentary",
            "description": "Broader remit now.",
            "tags": "Reporting, Finance, Exec",
            "link": "https://chatgpt.com/g/exec-commentary",
        })
    asset = data(admin.post("/api/update-requests/%d/accept" % record["id"]))["asset"]
    assert asset["name"] == "Exec Commentary"
    assert asset["description"] == "Broader remit now."
    assert sorted(asset["tags"]) == ["Exec", "Finance", "Reporting"]
    assert asset["directUrl"] == "https://chatgpt.com/g/exec-commentary"


def test_accepting_an_owner_change(app, visitor, published):
    record, admin = _raise_and_unlock(
        app, visitor, published["id"], fields=["owner"],
        proposed={"ownerName": "New Owner", "ownerEmail": "new@wings.test"})
    asset = data(admin.post("/api/update-requests/%d/accept" % record["id"]))["asset"]
    assert asset["owner"] == "New Owner"
    assert asset["ownerEmail"] == "new@wings.test"


def test_accepting_keeps_the_configuration_mirror_in_step(app, visitor, published):
    """description and instructions have type-specific copies inside the JSON."""
    record, admin = _raise_and_unlock(
        app, visitor, published["id"],
        fields=["description", "instructions"],
        proposed={"description": "New blurb.", "instructions": "New prompt."})
    asset = data(admin.post("/api/update-requests/%d/accept" % record["id"]))["asset"]
    assert asset["configuration"]["gptDescription"] == "New blurb."
    assert asset["configuration"]["instructions"] == "New prompt."


def test_tags_are_capped_when_accepted(app, visitor, published):
    record, admin = _raise_and_unlock(
        app, visitor, published["id"], fields=["tags"],
        proposed={"tags": "a, b, c, d, e, f"})
    asset = data(admin.post("/api/update-requests/%d/accept" % record["id"]))["asset"]
    assert len(asset["tags"]) <= 4


def test_a_request_can_only_be_resolved_once(app, visitor, published):
    record, admin = _raise_and_unlock(app, visitor, published["id"])
    assert admin.post("/api/update-requests/%d/accept" % record["id"]).status_code == 200
    second = admin.post("/api/update-requests/%d/accept" % record["id"])
    assert second.status_code == 409
    assert err(second)["code"] == "ALREADY_RESOLVED"


def test_accepting_is_audited(app, visitor, published):
    record, admin = _raise_and_unlock(app, visitor, published["id"])
    admin.post("/api/update-requests/%d/accept" % record["id"])
    entry = ActivityLog.query.filter_by(action="update_request.accept").one()
    assert entry.detail["wgtCode"] == published["wgtCode"]
    assert entry.detail["fields"] == ["description"]


# ---------------------------------------------------------------------------
# declining
# ---------------------------------------------------------------------------
def test_declining_changes_nothing(app, visitor, published):
    record, admin = _raise_and_unlock(app, visitor, published["id"])
    before = data(visitor.get("/api/assets/%d" % published["id"]))["asset"]
    payload = data(admin.post("/api/update-requests/%d/decline" % record["id"],
                              json={"reason": "Already handled."}))
    assert payload["updateRequest"]["status"] == "Declined"
    after = data(visitor.get("/api/assets/%d" % published["id"]))["asset"]
    assert after["description"] == before["description"]
    assert after["currentVersion"] == before["currentVersion"]
    assert ActivityLog.query.filter_by(action="update_request.decline").count() == 1


def test_a_declined_request_leaves_the_queue(app, visitor, published):
    record, admin = _raise_and_unlock(app, visitor, published["id"])
    admin.post("/api/update-requests/%d/decline" % record["id"])
    assert data(admin.get("/api/update-requests"))["openCount"] == 0
    assert data(admin.get("/api/update-requests?status=Declined"))["total"] == 1


# ---------------------------------------------------------------------------
# proposed files
# ---------------------------------------------------------------------------
def test_proposed_files_attach_to_the_request_not_the_asset(app, visitor, published):
    staged = stage_file(visitor, "new-kb.md")
    record = data(visitor.post("/api/update-requests", json=update_request_payload(
        published["id"], fields=["knowledgeBase"], proposed={},
        knowledgeFileIds=[staged["id"]])))["updateRequest"]

    stored = db.session.get(AssetFile, staged["id"])
    assert stored.update_request_id == record["id"]
    assert stored.asset_id is None
    assert stored.kind == "knowledge"
    # The asset itself has gained nothing.
    assert data(visitor.get("/api/assets/%d" % published["id"]))["asset"]["knowledgeFiles"] == []


def test_accepting_moves_proposed_files_onto_the_asset(app, visitor, published):
    staged = stage_file(visitor, "new-kb.md")
    record, admin = _raise_and_unlock(
        app, visitor, published["id"], fields=["knowledgeBase"], proposed={},
        knowledgeFileIds=[staged["id"]])
    asset = data(admin.post("/api/update-requests/%d/accept" % record["id"]))["asset"]
    assert [f["name"] for f in asset["knowledgeFiles"]] == ["new-kb.md"]
    stored = db.session.get(AssetFile, staged["id"])
    assert stored.asset_id == published["id"]
    assert stored.update_request_id is None


def test_accepted_files_replace_the_previous_set(app, visitor, admin_user):
    """Matches the prototype: the proposed list replaces, it does not append."""
    original = stage_file(visitor, "old-kb.md")
    asset = create_asset(visitor, knowledgeFileIds=[original["id"]])
    admin = app.test_client()
    unlock(admin)
    admin.post("/api/assets/%d/approve" % asset["id"])

    replacement = stage_file(visitor, "new-kb.md")
    record = data(visitor.post("/api/update-requests", json=update_request_payload(
        asset["id"], fields=["knowledgeBase"], proposed={},
        knowledgeFileIds=[replacement["id"]])))["updateRequest"]
    updated = data(admin.post("/api/update-requests/%d/accept" % record["id"]))["asset"]
    assert [f["name"] for f in updated["knowledgeFiles"]] == ["new-kb.md"]
    assert AssetFile.query.filter_by(asset_id=asset["id"]).count() == 1


def test_files_for_an_unticked_field_are_refused_not_dropped(app, visitor, published):
    """Quietly discarding an attachment would leave the requester believing
    they had sent something that never arrived."""
    staged = stage_file(visitor, "ctx.md")
    response = visitor.post("/api/update-requests", json=update_request_payload(
        published["id"], fields=["description"], contextFileIds=[staged["id"]]))
    assert response.status_code == 400
    assert "contextFileIds" in err(response)["fields"]


def test_a_file_field_the_asset_type_does_not_offer_is_refused(app, visitor, published):
    """A ChatGPT GPT has no "Context" field at all."""
    staged = stage_file(visitor, "ctx.md")
    response = visitor.post("/api/update-requests", json=update_request_payload(
        published["id"], fields=["context"], proposed={},
        contextFileIds=[staged["id"]]))
    assert response.status_code == 400
    assert "fields" in err(response)["fields"]


def test_an_already_claimed_file_cannot_be_reused(app, visitor, published):
    staged = stage_file(visitor, "kb.md")
    first = update_request_payload(published["id"], fields=["knowledgeBase"],
                                   proposed={}, knowledgeFileIds=[staged["id"]])
    assert visitor.post("/api/update-requests", json=first).status_code == 201
    # The same upload id is now bound; a second request must not steal it.
    assert visitor.post("/api/update-requests", json=first).status_code == 400


# ---------------------------------------------------------------------------
# owner mismatch
# ---------------------------------------------------------------------------
def test_a_request_from_the_owner_is_not_flagged(app, visitor, published):
    record, admin = _raise_and_unlock(
        app, visitor, published["id"],
        requesterName=published["owner"], requesterEmail=published["ownerEmail"])
    detail = data(admin.get("/api/update-requests/%d" % record["id"]))["updateRequest"]
    assert detail["ownerMismatch"] is False


def test_a_request_from_someone_else_is_flagged(app, visitor, published):
    record, admin = _raise_and_unlock(
        app, visitor, published["id"],
        requesterName="Someone Else", requesterEmail="else@wings.test")
    detail = data(admin.get("/api/update-requests/%d" % record["id"]))["updateRequest"]
    assert detail["ownerMismatch"] is True
    assert detail["assetOwner"] == published["owner"]


# ---------------------------------------------------------------------------
# transactional integrity
# ---------------------------------------------------------------------------
def test_a_failed_accept_leaves_the_request_open(app, visitor, published, monkeypatch):
    record, admin = _raise_and_unlock(app, visitor, published["id"])
    import app.api.updates as updates_api
    monkeypatch.setattr(updates_api, "add_version",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert admin.post("/api/update-requests/%d/accept" % record["id"]).status_code == 500
    db.session.rollback()
    assert UpdateRequest.query.one().status == "Open"
    asset = db.session.get(Asset, published["id"])
    assert asset.description == published["description"]


def test_deleting_an_asset_removes_its_requests(app, visitor, published):
    data(visitor.post("/api/update-requests",
                      json=update_request_payload(published["id"])))
    asset = db.session.get(Asset, published["id"])
    db.session.delete(asset)
    db.session.commit()
    assert UpdateRequest.query.count() == 0


# ---------------------------------------------------------------------------
# the admin queue
# ---------------------------------------------------------------------------
def test_the_queue_lists_open_requests_newest_first(app, visitor, published):
    for i in range(3):
        visitor.post("/api/update-requests", json=update_request_payload(
            published["id"], notes="request %d" % i))
    admin = app.test_client()
    unlock(admin)
    payload = data(admin.get("/api/update-requests"))
    assert payload["total"] == 3
    assert payload["openCount"] == 3
    ids = [r["id"] for r in payload["items"]]
    assert ids == sorted(ids, reverse=True)


def test_the_queue_can_be_filtered_by_status(app, visitor, published):
    record, admin = _raise_and_unlock(app, visitor, published["id"])
    admin.post("/api/update-requests/%d/accept" % record["id"])
    assert data(admin.get("/api/update-requests?status=Open"))["total"] == 0
    assert data(admin.get("/api/update-requests?status=Accepted"))["total"] == 1
    assert data(admin.get("/api/update-requests?status=all"))["total"] == 1


def test_an_unknown_status_filter_is_refused(app, visitor, published):
    admin = app.test_client()
    unlock(admin)
    assert admin.get("/api/update-requests?status=Nope").status_code == 400


def test_admin_stats_count_open_requests(app, visitor, published):
    visitor.post("/api/update-requests", json=update_request_payload(published["id"]))
    admin = app.test_client()
    unlock(admin)
    assert data(admin.get("/api/admin/stats"))["openUpdateRequests"] == 1
