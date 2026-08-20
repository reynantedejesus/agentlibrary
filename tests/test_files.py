"""File upload validation, authorised download, and storage safety."""
import io
import os

import pytest

from app.extensions import db
from app.models import ActivityLog, AssetFile
from tests.conftest import create_asset, data, err, login, make_user, PASSWORD

PDF = b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n%%EOF\n"
PNG = (b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
MARKDOWN = b"# Instructions\n\nDo the thing.\n"
ZIP = b"PK\x03\x04" + b"\x00" * 60
ELF = b"\x7fELF" + b"\x00" * 64
EXE = b"MZ\x90\x00" + b"\x00" * 64
SHEBANG = b"#!/bin/sh\nrm -rf /\n"


def upload(client, asset_id, filename, content, kind="documentation", **extra):
    payload = {"file": (io.BytesIO(content), filename), "kind": kind}
    payload.update(extra)
    return client.post("/api/assets/%d/files" % asset_id,
                       data=payload, content_type="multipart/form-data")


@pytest.fixture()
def asset(as_user):
    return create_asset(as_user)


# ---------------------------------------------------------------------------
# upload validation
# ---------------------------------------------------------------------------
def test_owner_can_upload_an_allowed_file(as_user, asset):
    response = upload(as_user, asset["id"], "instructions.md", MARKDOWN, kind="source")
    assert response.status_code == 201
    record = data(response)["file"]
    assert record["name"] == "instructions.md"
    assert record["kind"] == "source"
    assert record["ext"] == "md"
    assert record["sizeBytes"] == len(MARKDOWN)
    assert record["downloadUrl"] == "/api/files/%d/download" % record["id"]


def test_upload_never_exposes_the_storage_path(as_user, asset, app):
    record = data(upload(as_user, asset["id"], "notes.md", MARKDOWN))["file"]
    row = AssetFile.query.one()
    serialised = str(record)
    # Neither the physical path nor the random on-disk name reaches the client;
    # the only address it gets is the authorised download route.
    assert "relative_path" not in serialised and "relativePath" not in serialised
    assert row.relative_path not in serialised
    assert row.stored_name not in serialised
    assert app.config["UPLOAD_DIR"] not in serialised
    assert record["downloadUrl"] == "/api/files/%d/download" % record["id"]


def test_stored_filename_is_random_not_the_users(as_user, asset, app):
    data(upload(as_user, asset["id"], "instructions.md", MARKDOWN))
    row = AssetFile.query.one()
    assert row.original_name == "instructions.md"
    assert row.stored_name != "instructions.md"
    assert len(row.stored_name.split(".")[0]) == 32       # 16 random bytes, hex
    assert row.stored_name.endswith(".md")
    assert row.sha256 and len(row.sha256) == 64


def test_stored_file_lands_outside_the_static_directory(as_user, asset, app):
    data(upload(as_user, asset["id"], "instructions.md", MARKDOWN))
    row = AssetFile.query.one()
    absolute = os.path.join(app.config["UPLOAD_DIR"], row.relative_path)
    assert os.path.isfile(absolute)
    static_root = os.path.realpath(app.static_folder)
    assert not os.path.realpath(absolute).startswith(static_root)
    # And only the service account can read it.
    assert oct(os.stat(absolute).st_mode)[-3:] == "600"


@pytest.mark.parametrize("filename", ["payload.exe", "script.sh", "lib.so", "page.html"])
def test_disallowed_extensions_are_refused(as_user, asset, filename):
    response = upload(as_user, asset["id"], filename, MARKDOWN)
    assert response.status_code == 400
    assert "file" in err(response)["fields"]


def test_file_without_an_extension_is_refused(as_user, asset):
    response = upload(as_user, asset["id"], "README", MARKDOWN)
    assert response.status_code == 400


@pytest.mark.parametrize("content,label", [
    (ELF, "linux executable"), (EXE, "windows executable"), (SHEBANG, "shell script"),
])
def test_executable_content_is_refused_despite_a_safe_extension(as_user, asset, content, label):
    response = upload(as_user, asset["id"], "harmless.txt", content)
    assert response.status_code == 400, label
    assert "file" in err(response)["fields"]


def test_content_must_match_the_claimed_extension(as_user, asset):
    """A PNG renamed to .pdf is rejected on sniffed type, not on its name."""
    response = upload(as_user, asset["id"], "report.pdf", PNG)
    assert response.status_code == 400
    assert "content type" in str(err(response)["fields"]).lower()


def test_genuine_pdf_is_accepted(as_user, asset):
    assert upload(as_user, asset["id"], "guide.pdf", PDF).status_code == 201


def test_genuine_zip_is_accepted(as_user, asset):
    assert upload(as_user, asset["id"], "skill.zip", ZIP, kind="deployable").status_code == 201


def test_empty_file_is_refused(as_user, asset):
    response = upload(as_user, asset["id"], "empty.md", b"")
    assert response.status_code == 400


def test_oversized_upload_is_refused(as_user, asset, app):
    app.config["MAX_CONTENT_LENGTH"] = 1024
    response = upload(as_user, asset["id"], "big.md", b"x" * 5000)
    assert response.status_code == 413
    assert err(response)["code"] == "PAYLOAD_TOO_LARGE"


def test_unknown_file_kind_is_refused(as_user, asset):
    response = upload(as_user, asset["id"], "notes.md", MARKDOWN, kind="malicious")
    assert response.status_code == 400
    assert "kind" in err(response)["fields"]


def test_path_traversal_in_the_filename_is_neutralised(as_user, asset, app):
    response = upload(as_user, asset["id"], "../../../../etc/passwd.md", MARKDOWN)
    assert response.status_code == 201
    row = AssetFile.query.one()
    assert ".." not in row.stored_name
    assert ".." not in row.relative_path
    assert ".." not in row.original_name
    absolute = os.path.realpath(os.path.join(app.config["UPLOAD_DIR"], row.relative_path))
    assert absolute.startswith(os.path.realpath(app.config["UPLOAD_DIR"]))


def test_no_file_part_is_refused(as_user, asset):
    response = as_user.post("/api/assets/%d/files" % asset["id"],
                            data={"kind": "source"}, content_type="multipart/form-data")
    assert response.status_code == 400


def test_file_limit_per_asset(as_user, asset, app):
    app.config["MAX_FILES_PER_ASSET"] = 2
    upload(as_user, asset["id"], "a.md", MARKDOWN)
    upload(as_user, asset["id"], "b.md", MARKDOWN)
    response = upload(as_user, asset["id"], "c.md", MARKDOWN)
    assert response.status_code == 409
    assert err(response)["code"] == "FILE_LIMIT_REACHED"


def test_upload_is_audited(as_user, asset):
    upload(as_user, asset["id"], "instructions.md", MARKDOWN)
    entry = ActivityLog.query.filter_by(action="file.upload").one()
    assert entry.detail["wgtCode"] == asset["wgtCode"]
    assert entry.detail["sizeBytes"] == len(MARKDOWN)


# ---------------------------------------------------------------------------
# upload authorisation
# ---------------------------------------------------------------------------
def test_anonymous_cannot_upload(client, as_user, asset):
    as_user.post("/api/auth/logout")
    response = upload(client, asset["id"], "notes.md", MARKDOWN)
    assert response.status_code == 401


def test_a_stranger_cannot_upload_to_someone_elses_asset(app, as_user, asset):
    make_user("mallory@wings.test", "user")
    other = app.test_client()
    login(other, "mallory@wings.test", PASSWORD)
    response = upload(other, asset["id"], "notes.md", MARKDOWN)
    assert response.status_code == 404          # cannot even see the pending asset


def test_a_version_from_another_asset_cannot_be_attached(app, as_user, asset):
    second = create_asset(as_user, name="Another Asset", department="Sales")
    detail = data(as_user.get("/api/assets/%d" % second["id"]))["asset"]
    foreign_version_id = detail["versions"][0]["id"]
    response = upload(as_user, asset["id"], "notes.md", MARKDOWN,
                      versionId=str(foreign_version_id))
    assert response.status_code == 400
    assert "versionId" in err(response)["fields"]


# ---------------------------------------------------------------------------
# download authorisation
# ---------------------------------------------------------------------------
def test_owner_can_download_their_own_file(as_user, asset):
    record = data(upload(as_user, asset["id"], "instructions.md", MARKDOWN))["file"]
    response = as_user.get("/api/files/%d/download" % record["id"])
    assert response.status_code == 200
    assert response.data == MARKDOWN
    assert "attachment" in response.headers["Content-Disposition"]
    assert "instructions.md" in response.headers["Content-Disposition"]
    assert response.headers["X-Content-Type-Options"] == "nosniff"


def test_anonymous_cannot_download_a_pending_assets_file(client, as_user, asset):
    record = data(upload(as_user, asset["id"], "instructions.md", MARKDOWN))["file"]
    as_user.post("/api/auth/logout")
    response = client.get("/api/files/%d/download" % record["id"])
    assert response.status_code == 404
    assert MARKDOWN not in response.data


def test_a_stranger_cannot_download_a_pending_assets_file(app, as_user, asset):
    record = data(upload(as_user, asset["id"], "instructions.md", MARKDOWN))["file"]
    make_user("mallory@wings.test", "user")
    other = app.test_client()
    login(other, "mallory@wings.test", PASSWORD)
    response = other.get("/api/files/%d/download" % record["id"])
    assert response.status_code == 404


def test_anyone_can_download_from_an_approved_asset(client, app, as_user, asset,
                                                    reviewer_user):
    record = data(upload(as_user, asset["id"], "instructions.md", MARKDOWN))["file"]
    reviewer = app.test_client()
    login(reviewer, reviewer_user.email)
    reviewer.post("/api/assets/%d/approve" % asset["id"])
    response = client.get("/api/files/%d/download" % record["id"])
    assert response.status_code == 200
    assert response.data == MARKDOWN


def test_download_of_a_missing_file_id_is_404(client):
    assert client.get("/api/files/999999/download").status_code == 404


def test_download_is_audited(as_user, asset):
    record = data(upload(as_user, asset["id"], "instructions.md", MARKDOWN))["file"]
    as_user.get("/api/files/%d/download" % record["id"])
    assert ActivityLog.query.filter_by(action="file.download").count() == 1


def test_inline_disposition_only_for_safe_types(as_user, asset):
    pdf = data(upload(as_user, asset["id"], "guide.pdf", PDF))["file"]
    zipf = data(upload(as_user, asset["id"], "skill.zip", ZIP, kind="deployable"))["file"]
    inline = as_user.get("/api/files/%d/download?disposition=inline" % pdf["id"])
    assert "inline" in inline.headers["Content-Disposition"]
    # A zip is never rendered inline, even when asked.
    forced = as_user.get("/api/files/%d/download?disposition=inline" % zipf["id"])
    assert "attachment" in forced.headers["Content-Disposition"]


def test_download_is_sandboxed_by_csp(as_user, asset):
    record = data(upload(as_user, asset["id"], "guide.pdf", PDF))["file"]
    response = as_user.get("/api/files/%d/download" % record["id"])
    assert "sandbox" in response.headers["Content-Security-Policy"]


def test_a_tampered_storage_path_cannot_escape_the_upload_root(as_user, asset, app):
    """Defence in depth: even a corrupted database row cannot read /etc."""
    data(upload(as_user, asset["id"], "instructions.md", MARKDOWN))
    row = AssetFile.query.one()
    row.relative_path = "../../../../etc/passwd"
    db.session.commit()
    response = as_user.get("/api/files/%d/download" % row.id)
    assert response.status_code == 500
    assert err(response)["code"] == "STORAGE_ERROR"
    assert b"root:" not in response.data


# ---------------------------------------------------------------------------
# listing & deletion
# ---------------------------------------------------------------------------
def test_file_listing_reports_upload_permission(client, as_user, asset):
    upload(as_user, asset["id"], "instructions.md", MARKDOWN)
    mine = data(as_user.get("/api/assets/%d/files" % asset["id"]))
    assert len(mine["files"]) == 1
    assert mine["canUpload"] is True


def test_owner_can_delete_their_file_and_it_leaves_disk(as_user, asset, app):
    record = data(upload(as_user, asset["id"], "instructions.md", MARKDOWN))["file"]
    row = AssetFile.query.one()
    absolute = os.path.join(app.config["UPLOAD_DIR"], row.relative_path)
    assert os.path.isfile(absolute)
    assert as_user.delete("/api/files/%d" % record["id"]).status_code == 200
    assert AssetFile.query.count() == 0
    assert not os.path.exists(absolute)
    assert ActivityLog.query.filter_by(action="file.delete").count() == 1


def test_a_stranger_cannot_delete_a_file(app, as_user, asset):
    record = data(upload(as_user, asset["id"], "instructions.md", MARKDOWN))["file"]
    make_user("mallory@wings.test", "user")
    other = app.test_client()
    login(other, "mallory@wings.test", PASSWORD)
    assert other.delete("/api/files/%d" % record["id"]).status_code == 403
    assert AssetFile.query.count() == 1
