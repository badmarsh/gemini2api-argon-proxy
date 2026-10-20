"""Unit tests for the structured logs router (/admin/logs) and LogStore integration."""

import pytest
from app.core.log_store import LogStore, create_log_record

_AUTH = {"Authorization": "Bearer sk-test-key"}


@pytest.fixture
def log_client(app_main):
    from fastapi.testclient import TestClient

    store = LogStore(capacity=100, persist_path="data/test_logs.json")
    app_main.app.state.log_store = store
    return TestClient(app_main.app), store


def test_list_logs_empty(log_client):
    client, store = log_client
    store.clear()

    resp = client.get("/admin/logs", headers=_AUTH)
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 0
    assert data["records"] == []
    assert data["limit"] == 100
    assert data["offset"] == 0


def test_list_logs_pagination_and_filtering(log_client):
    client, store = log_client
    store.clear()

    rec1 = create_log_record(method="POST", path="/v1/chat/completions", direction="ingress", model="gemini-flash", status=200)
    rec2 = create_log_record(method="POST", path="/v1beta/models/gemini-pro:generateContent", direction="egress", model="gemini-pro", status=200)
    rec3 = create_log_record(method="GET", path="/admin/status", direction="ingress", status=404)

    store.add(rec1)
    store.add(rec2)
    store.add(rec3)

    # All
    resp = client.get("/admin/logs", headers=_AUTH)
    assert resp.status_code == 200
    assert resp.json()["total"] == 3

    # Direction filter: egress
    resp = client.get("/admin/logs?direction=egress", headers=_AUTH)
    assert resp.status_code == 200
    assert resp.json()["total"] == 1
    assert resp.json()["records"][0]["direction"] == "egress"

    # Direction filter: ingress
    resp = client.get("/admin/logs?direction=ingress", headers=_AUTH)
    assert resp.status_code == 200
    assert resp.json()["total"] == 2

    # Limit and offset
    resp = client.get("/admin/logs?limit=1&offset=1", headers=_AUTH)
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 3
    assert len(data["records"]) == 1
    assert data["offset"] == 1


def test_list_logs_traffic_type_filter(log_client):
    client, store = log_client
    store.clear()

    rec1 = create_log_record(method="POST", path="/v1/chat/completions", direction="ingress", model="gemini-flash", status=200)
    rec2 = create_log_record(method="POST", path="/v1/completions", direction="ingress", model="gemini-pro", status=200)
    rec3 = create_log_record(method="POST", path="/v1beta/models/gemini-pro:generateContent", direction="egress", model="gemini-pro", status=200)
    rec4 = create_log_record(method="GET", path="/admin/status", direction="ingress", status=200)
    rec5 = create_log_record(method="GET", path="/health", direction="ingress", status=200)

    for r in (rec1, rec2, rec3, rec4, rec5):
        store.add(r)

    # Default traffic_type=all
    resp = client.get("/admin/logs", headers=_AUTH)
    assert resp.status_code == 200
    assert resp.json()["total"] == 5

    # Filter: completions
    resp = client.get("/admin/logs?traffic_type=completions", headers=_AUTH)
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 2
    paths = [r["path"] for r in data["records"]]
    assert "/v1/chat/completions" in paths
    assert "/v1/completions" in paths
    assert "/v1beta/models/gemini-pro:generateContent" not in paths

    # Filter: completions with pagination (limit=1, offset=1)
    resp = client.get("/admin/logs?traffic_type=completions&limit=1&offset=1", headers=_AUTH)
    assert resp.status_code == 200
    page_data = resp.json()
    assert page_data["total"] == 2
    assert len(page_data["records"]) == 1
    assert page_data["limit"] == 1
    assert page_data["offset"] == 1

    # Filter: completions with direction=ingress
    resp = client.get("/admin/logs?traffic_type=completions&direction=ingress", headers=_AUTH)
    assert resp.status_code == 200
    assert resp.json()["total"] == 2

    # Case-insensitivity test
    resp = client.get("/admin/logs?traffic_type=COMPLETIONS", headers=_AUTH)
    assert resp.status_code == 200
    assert resp.json()["total"] == 2

    # Filter: ai
    resp = client.get("/admin/logs?traffic_type=ai", headers=_AUTH)
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 3
    paths = [r["path"] for r in data["records"]]
    assert "/v1/chat/completions" in paths
    assert "/v1/completions" in paths
    assert "/v1beta/models/gemini-pro:generateContent" in paths
    assert "/admin/status" not in paths

    # Fallback: unknown traffic_type defaults to all
    resp = client.get("/admin/logs?traffic_type=unknown_type", headers=_AUTH)
    assert resp.status_code == 200
    assert resp.json()["total"] == 5


def test_search_by_model_path_id_request_id(log_client):
    client, store = log_client
    store.clear()

    rec = create_log_record(
        method="POST",
        path="/v1/chat/completions",
        direction="ingress",
        model="custom-model-search-target",
        status=200,
    )
    store.add(rec)

    # Search by model
    resp = client.get("/admin/logs?search=custom-model", headers=_AUTH)
    assert resp.status_code == 200
    assert resp.json()["total"] == 1

    # Search by record ID
    resp = client.get(f"/admin/logs?search={rec.id}", headers=_AUTH)
    assert resp.status_code == 200
    assert resp.json()["total"] == 1

    # Search by request ID
    resp = client.get(f"/admin/logs?search={rec.request_id}", headers=_AUTH)
    assert resp.status_code == 200
    assert resp.json()["total"] == 1

    # Non-matching search
    resp = client.get("/admin/logs?search=nonexistent-query-string", headers=_AUTH)
    assert resp.status_code == 200
    assert resp.json()["total"] == 0


def test_get_log_record_found_and_not_found(log_client):
    client, store = log_client
    store.clear()

    rec = create_log_record(method="POST", path="/v1/chat/completions", status=200)
    store.add(rec)

    resp = client.get(f"/admin/logs/{rec.id}", headers=_AUTH)
    assert resp.status_code == 200
    assert resp.json()["id"] == rec.id
    assert resp.json()["path"] == "/v1/chat/completions"

    # Not found
    resp = client.get("/admin/logs/missing-record-id-12345", headers=_AUTH)
    assert resp.status_code == 404
    assert resp.json()["error"]["type"] == "not_found"


def test_clear_logs_post_and_delete(log_client):
    client, store = log_client
    store.clear()

    rec = create_log_record(method="POST", path="/v1/chat/completions", status=200)
    store.add(rec)
    assert len(store.query()["records"]) == 1

    # Clear via POST /admin/logs/clear
    resp = client.post("/admin/logs/clear", headers=_AUTH)
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert len(store.query()["records"]) == 0

    # Add again and clear via DELETE /admin/logs
    store.add(rec)
    assert len(store.query()["records"]) == 1
    resp = client.delete("/admin/logs", headers=_AUTH)
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert len(store.query()["records"]) == 0


def test_log_state_endpoints(log_client):
    client, store = log_client

    # Get state
    resp = client.get("/admin/logs/state", headers=_AUTH)
    assert resp.status_code == 200
    assert "enabled" in resp.json()
    assert "paused" in resp.json()

    # Update state
    resp = client.post("/admin/logs/state", json={"paused": True}, headers=_AUTH)
    assert resp.status_code == 200
    assert resp.json()["paused"] is True
    assert store.state.paused is True

    # Resume
    resp = client.post("/admin/logs/state", json={"paused": False}, headers=_AUTH)
    assert resp.status_code == 200
    assert resp.json()["paused"] is False
    assert store.state.paused is False
