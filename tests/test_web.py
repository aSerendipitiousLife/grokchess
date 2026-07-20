"""Web API smoke tests."""

import sys
import time
import types

import pytest
from fastapi.testclient import TestClient

import grokchess.metrics_db as metrics_db
import grokchess.web.app as web_app
from grokchess.web.app import app

metrics_db.DB_BACKEND = "sqlite"
metrics_db.DATABASE_URL = ""
metrics_db._READY = False


def test_tournament_job_completes():
    client = TestClient(app)
    started = client.post("/api/tournament/start", json={"rounds": 1, "max_plies": 4})
    assert started.status_code == 200

    job = started.json()
    assert job["status"] == "running"
    assert job["total_games"] > 0

    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        polled = client.get(f"/api/tournament/status/{job['id']}")
        assert polled.status_code == 200
        job = polled.json()
        if job["status"] == "done":
            break
        time.sleep(0.1)

    assert job["status"] == "done"
    assert job["completed_games"] == job["total_games"]
    assert len(job["standings"]) >= 2
    assert len(job["games"]) == job["total_games"]
    assert job["games"][0]["frames"]


def test_login_and_metrics_summary():
    client = TestClient(app)
    player = client.post("/api/login", json={"name": "Metrics Tester"}).json()
    assert player["name"] == "Metrics Tester"

    started = client.post("/api/tournament/start", json={"rounds": 1, "max_plies": 4})
    assert started.status_code == 200
    job = started.json()

    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        job = client.get(f"/api/tournament/status/{job['id']}").json()
        if job["status"] == "done":
            break
        time.sleep(0.1)

    summary = client.get("/api/metrics")
    assert summary.status_code == 200
    data = summary.json()
    assert any(engine["games"] > 0 for engine in data["engines"])


def test_postgres_backend_requires_database_url(monkeypatch):
    monkeypatch.setattr(metrics_db, "DB_BACKEND", "postgres")
    monkeypatch.setattr(metrics_db, "DATABASE_URL", "")
    monkeypatch.setattr(metrics_db, "_READY", False)

    with pytest.raises(RuntimeError, match="GROKCHESS_DATABASE_URL"):
        metrics_db.init_db()

    monkeypatch.setattr(metrics_db, "DB_BACKEND", "sqlite")
    monkeypatch.setattr(metrics_db, "_READY", False)


def test_postgres_pooler_connection_disables_prepared_statements(monkeypatch):
    calls = []

    def connect(*args, **kwargs):
        calls.append((args, kwargs))
        return object()

    fake_psycopg = types.SimpleNamespace(connect=connect)
    fake_rows = types.SimpleNamespace(dict_row=object())
    monkeypatch.setitem(sys.modules, "psycopg", fake_psycopg)
    monkeypatch.setitem(sys.modules, "psycopg.rows", fake_rows)
    monkeypatch.setattr(metrics_db, "DB_BACKEND", "postgres")
    monkeypatch.setattr(metrics_db, "DATABASE_URL", "postgresql://example")

    conn = metrics_db._connect()

    assert conn is not None
    assert calls[0][1]["prepare_threshold"] is None

    monkeypatch.setattr(metrics_db, "DB_BACKEND", "sqlite")
    monkeypatch.setattr(metrics_db, "DATABASE_URL", "")
    monkeypatch.setattr(metrics_db, "_READY", False)


def test_metrics_endpoint_reports_database_unavailable(monkeypatch):
    def fail_metrics():
        raise RuntimeError("database is sleeping")

    monkeypatch.setattr(web_app, "metrics_summary", fail_metrics)

    response = TestClient(app).get("/api/metrics")

    assert response.status_code == 503
    assert "Database unavailable" in response.json()["detail"]


def test_tournament_completes_when_metrics_recording_fails(monkeypatch):
    def fail_recording(result, *, mode="tournament"):
        raise RuntimeError("database is sleeping")

    monkeypatch.setattr(web_app, "record_result_game", fail_recording)
    client = TestClient(app)

    started = client.post("/api/tournament/start", json={"rounds": 1, "max_plies": 4})
    assert started.status_code == 200
    job = started.json()

    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        job = client.get(f"/api/tournament/status/{job['id']}").json()
        if job["status"] == "done":
            break
        time.sleep(0.1)

    assert job["status"] == "done"
    assert job["completed_games"] == job["total_games"]
    assert "Database unavailable" in job["warnings"][0]
