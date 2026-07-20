"""Web API smoke tests."""

import time

from fastapi.testclient import TestClient

from grokchess.web.app import app


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
