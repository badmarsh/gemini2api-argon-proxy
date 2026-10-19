"""Tests for Babysit experiment monitoring endpoints in /admin/babysit/*."""

import json
from pathlib import Path

_AUTH = {"Authorization": "Bearer sk-test-key", "X-Admin-Key": "sk-test-key"}


def _get(client, path):
    return client.get(path, headers=_AUTH)


def _post(client, path, json_data=None):
    return client.post(path, headers=_AUTH, json=json_data)


def test_babysit_runs_empty(client, tmp_path, monkeypatch):
    import app.routers.admin as admin_module

    monkeypatch.setattr(admin_module, "_get_runs_root", lambda: tmp_path)
    resp = _get(client, "/admin/babysit/runs")
    assert resp.status_code == 200
    data = resp.json()
    assert data["runs"] == []
    assert data["global_stop"] is False


def test_babysit_runs_with_data(client, tmp_path, monkeypatch):
    import app.routers.admin as admin_module

    monkeypatch.setattr(admin_module, "_get_runs_root", lambda: tmp_path)
    run_dir = tmp_path / "exp-test-01"
    run_dir.mkdir(parents=True)

    # Write tasks.jsonl
    (run_dir / "tasks.jsonl").write_text(
        json.dumps({"task_id": "T-1", "status": "completed"}) + "\n" +
        json.dumps({"task_id": "T-2", "status": "failed"}) + "\n",
        encoding="utf-8"
    )

    # Write findings.jsonl
    (run_dir / "findings.jsonl").write_text(
        json.dumps({"id": "F-1", "severity": "HIGH", "title": "Stall detected"}) + "\n" +
        json.dumps({"id": "F-2", "severity": "INFO", "title": "Model warmed up"}) + "\n",
        encoding="utf-8"
    )

    # Write trace
    (run_dir / "argon_trace.jsonl").write_text(
        json.dumps({"step": 1, "action": "tool_call"}) + "\n",
        encoding="utf-8"
    )

    resp = _get(client, "/admin/babysit/runs")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["runs"]) == 1
    run = data["runs"][0]
    assert run["run_id"] == "exp-test-01"
    assert run["task_count"] == 2
    assert run["task_completed"] == 1
    assert run["task_failed"] == 1
    assert run["findings_count"] == 2
    assert run["critical_findings"] == 1
    assert run["trace_events"] == 1
    assert run["stopped"] is False


def test_babysit_run_detail_and_traversal_protection(client, tmp_path, monkeypatch):
    import app.routers.admin as admin_module

    monkeypatch.setattr(admin_module, "_get_runs_root", lambda: tmp_path)
    run_dir = tmp_path / "exp-test-02"
    run_dir.mkdir(parents=True)

    (run_dir / "tasks.jsonl").write_text(
        json.dumps({"task_id": "T-100", "description": "Write a script", "status": "in_progress"}) + "\n",
        encoding="utf-8"
    )

    # Valid run detail
    resp = _get(client, "/admin/babysit/runs/exp-test-02")
    assert resp.status_code == 200
    data = resp.json()
    assert data["run_id"] == "exp-test-02"
    assert len(data["tasks"]) == 1
    assert data["tasks"][0]["task_id"] == "T-100"
    assert data["stopped"] is False

    # Path traversal attempt
    resp_invalid = _get(client, "/admin/babysit/runs/..%2F..%2Fetc")
    assert resp_invalid.status_code in (400, 404)

    # 404 for non-existent run
    resp_missing = _get(client, "/admin/babysit/runs/non-existent-run")
    assert resp_missing.status_code == 404


def test_babysit_kill_switch_lifecycle(client, tmp_path, monkeypatch):
    import app.routers.admin as admin_module

    monkeypatch.setattr(admin_module, "_get_runs_root", lambda: tmp_path)
    run_dir = tmp_path / "exp-switch-test"
    run_dir.mkdir(parents=True)

    # Trigger Stop
    resp_stop = _post(client, "/admin/babysit/kill-switch", {"run_id": "exp-switch-test", "action": "stop"})
    assert resp_stop.status_code == 200
    assert resp_stop.json()["status"] == "ok"
    assert (run_dir / "STOP_BABYSIT").exists()

    # Detail now reflects stopped
    resp_detail = _get(client, "/admin/babysit/runs/exp-switch-test")
    assert resp_detail.json()["stopped"] is True

    # Trigger Resume
    resp_resume = _post(client, "/admin/babysit/kill-switch", {"run_id": "exp-switch-test", "action": "resume"})
    assert resp_resume.status_code == 200
    assert resp_resume.json()["status"] == "ok"
    assert not (run_dir / "STOP_BABYSIT").exists()

    # Detail now reflects resumed
    resp_detail_after = _get(client, "/admin/babysit/runs/exp-switch-test")
    assert resp_detail_after.json()["stopped"] is False


def test_babysit_runs_with_completed_tasks_file(client, tmp_path, monkeypatch):
    import app.routers.admin as admin_module

    monkeypatch.setattr(admin_module, "_get_runs_root", lambda: tmp_path)
    run_dir = tmp_path / "exp-completed-test"
    run_dir.mkdir(parents=True)

    # Orchestrator tasks without status property
    (run_dir / "tasks.jsonl").write_text(
        json.dumps({"task_id": "T-101", "prompt": "Task 1"}) + "\n" +
        json.dumps({"task_id": "T-102", "prompt": "Task 2"}) + "\n",
        encoding="utf-8"
    )
    # Runner completion file
    (run_dir / "completed_tasks.json").write_text(
        json.dumps(["T-101", "T-102"]),
        encoding="utf-8"
    )
    # Findings with severity: error
    (run_dir / "findings.jsonl").write_text(
        json.dumps({"finding_id": "F-1", "severity": "error", "summary": "Tool parse issue"}) + "\n",
        encoding="utf-8"
    )

    # Test list endpoint
    resp = _get(client, "/admin/babysit/runs")
    assert resp.status_code == 200
    runs = resp.json()["runs"]
    assert len(runs) == 1
    assert runs[0]["task_count"] == 2
    assert runs[0]["task_completed"] == 2
    assert runs[0]["critical_findings"] == 1

    # Test detail endpoint
    resp_detail = _get(client, "/admin/babysit/runs/exp-completed-test")
    assert resp_detail.status_code == 200
    detail = resp_detail.json()
    assert detail["completed_task_ids"] == ["T-101", "T-102"]
    assert all(t["status"] == "completed" for t in detail["tasks"])


def test_babysit_runs_completed_tasks_without_tasks_file(client, tmp_path, monkeypatch):
    import app.routers.admin as admin_module

    monkeypatch.setattr(admin_module, "_get_runs_root", lambda: tmp_path)
    run_dir = tmp_path / "exp-no-tasks-file"
    run_dir.mkdir(parents=True)

    # Runner completion file only, no tasks.jsonl
    (run_dir / "completed_tasks.json").write_text(
        json.dumps(["T-201", "T-202", "T-203"]),
        encoding="utf-8"
    )

    resp = _get(client, "/admin/babysit/runs")
    assert resp.status_code == 200
    runs = resp.json()["runs"]
    assert len(runs) == 1
    assert runs[0]["task_count"] == 3
    assert runs[0]["task_completed"] == 3

    resp_detail = _get(client, "/admin/babysit/runs/exp-no-tasks-file")
    assert resp_detail.status_code == 200
    detail = resp_detail.json()
    assert len(detail["tasks"]) == 3
    assert detail["completed_task_ids"] == ["T-201", "T-202", "T-203"]
    assert all(t["status"] == "completed" for t in detail["tasks"])


def test_babysit_empty_and_smoke_run_tags(client, tmp_path, monkeypatch):
    import app.routers.admin as admin_module

    monkeypatch.setattr(admin_module, "_get_runs_root", lambda: tmp_path)
    (tmp_path / "exp-empty").mkdir(parents=True)
    smoke_dir = tmp_path / "smoke-run-01"
    smoke_dir.mkdir(parents=True)
    (smoke_dir / "tasks.jsonl").write_text(json.dumps({"task_id": "S-1"}) + "\n", encoding="utf-8")

    resp = _get(client, "/admin/babysit/runs")
    assert resp.status_code == 200
    runs = {r["run_id"]: r for r in resp.json()["runs"]}
    assert runs["exp-empty"]["is_empty"] is True
    assert runs["exp-empty"]["status_tag"] == "empty"
    assert "prázdny test" in runs["exp-empty"]["display_label"]

    assert runs["smoke-run-01"]["status_tag"] == "smoke_test"
    assert "Smoke Test" in runs["smoke-run-01"]["display_label"]


