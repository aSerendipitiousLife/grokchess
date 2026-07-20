"""Web API smoke tests."""

import sys
import time
import types

import pytest
from fastapi.testclient import TestClient

import grokchess.metrics_db as metrics_db
import grokchess.web.app as web_app
from slow_web_engine import SlowWebEngine
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


def test_new_game_still_starts_when_metrics_game_creation_fails(monkeypatch):
    def fail_start_game(**kwargs):
        raise RuntimeError("database is sleeping")

    monkeypatch.setattr(web_app, "start_game", fail_start_game)
    client = TestClient(app)

    response = client.post("/api/new", json={"engine": "random-mover", "human_color": "white"})

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "playing"
    assert "Database unavailable" in data["warnings"][0]


def test_move_still_advances_when_metrics_recording_fails(monkeypatch):
    client = TestClient(app)
    started = client.post("/api/new", json={"engine": "random-mover", "human_color": "white"})
    assert started.status_code == 200
    state = started.json()
    move = state["legal_moves"][0]

    def fail_record_move(*args, **kwargs):
        raise RuntimeError("database is sleeping")

    monkeypatch.setattr(web_app, "record_move", fail_record_move)

    response = client.post(
        "/api/move",
        json={"game_id": state["game_id"], "from": move["from"], "to": move["to"]},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["fen"] != state["fen"]
    assert data["move_events"]

    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        data = client.get(f"/api/state/{state['game_id']}").json()
        if data["warnings"]:
            break
        time.sleep(0.05)

    assert "Database unavailable" in data["warnings"][0]


def test_move_response_does_not_wait_for_metrics_write(monkeypatch):
    client = TestClient(app)
    started = client.post("/api/new", json={"engine": "random-mover", "human_color": "white"})
    assert started.status_code == 200
    state = started.json()
    move = state["legal_moves"][0]

    def slow_record_move(*args, **kwargs):
        time.sleep(0.75)
        return {}

    monkeypatch.setattr(web_app, "record_move", slow_record_move)

    started_at = time.monotonic()
    response = client.post(
        "/api/move",
        json={"game_id": state["game_id"], "from": move["from"], "to": move["to"]},
    )
    elapsed = time.monotonic() - started_at

    assert response.status_code == 200
    assert elapsed < 0.5


def test_move_response_does_not_wait_for_engine_reply(monkeypatch):
    monkeypatch.setitem(web_app._REGISTRY, SlowWebEngine.name, SlowWebEngine)
    client = TestClient(app)
    started = client.post("/api/new", json={"engine": SlowWebEngine.name, "human_color": "white"})
    assert started.status_code == 200
    state = started.json()
    move = state["legal_moves"][0]

    started_at = time.monotonic()
    response = client.post(
        "/api/move",
        json={"game_id": state["game_id"], "from": move["from"], "to": move["to"]},
    )
    elapsed = time.monotonic() - started_at

    assert response.status_code == 200
    data = response.json()
    assert elapsed < 0.5
    assert data["engine_pending"] is True
    assert len(data["move_events"]) == 1

    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        data = client.get(f"/api/state/{state['game_id']}").json()
        if not data["engine_pending"]:
            break
        time.sleep(0.05)

    assert data["engine_pending"] is False
    assert len(data["move_events"]) >= 2
