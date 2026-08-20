"""Health endpoint and reference-data endpoint."""
from tests.conftest import data


def test_health_reports_ok_with_a_working_database(client):
    response = client.get("/health")
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["status"] == "ok"
    assert payload["checks"]["database"] == "ok"
    assert "version" in payload


def test_health_needs_no_authentication(client):
    assert client.get("/health").status_code == 200


def test_health_leaks_no_configuration(client):
    text = client.get("/health").get_data(as_text=True).lower()
    for secret in ("password", "secret_key", "mysql", "sqlite", "upload_dir", "/var/"):
        assert secret not in text


def test_config_serves_reference_lists(client):
    payload = data(client.get("/api/config"))
    assert "Finance" in payload["departments"]
    assert [t["id"] for t in payload["assetTypes"]] == ["gpt", "skill", "agent", "other"]
    assert payload["statuses"][0] == "Pending Review"
    assert payload["limits"]["maxTagsPerAsset"] == 4
    # The prototype's plaintext admin password must not survive anywhere.
    assert "defaultAdminPassword" not in payload


def test_index_page_renders_the_shell(client):
    response = client.get("/")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert 'name="csrf-token"' in html
    assert "css/app.css" in html and "js/app.js" in html
    assert "localStorage" not in html


def test_security_headers_are_present(client):
    headers = client.get("/").headers
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["X-Frame-Options"] == "DENY"
    assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
    assert "script-src 'self'" in headers["Content-Security-Policy"]


def test_unknown_api_route_returns_the_json_envelope(client):
    response = client.get("/api/does-not-exist")
    assert response.status_code == 404
    payload = response.get_json()
    assert payload["ok"] is False
    assert payload["error"]["code"] == "NOT_FOUND"
    assert "fields" in payload["error"]
