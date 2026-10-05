"""Test suite for FastAPI endpoints, ADK action webhook, and Cloud Scheduler background sync."""
from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from execution.database import Base
from execution.google_client import GoogleContactsClient
from execution.ms_client import MicrosoftContactsClient
from execution.repository import SyncRepository
from main import app, get_db


@pytest.fixture
def client_with_db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    TestingSessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    def override_get_db():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db

    # Mock client factories that return empty/mock contacts
    def mock_google_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"connections": [], "nextPageToken": None})

    def mock_ms_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"value": [], "@odata.nextLink": None})

    mock_g_http = httpx.Client(transport=httpx.MockTransport(mock_google_handler))
    mock_ms_http = httpx.Client(transport=httpx.MockTransport(mock_ms_handler))

    app.state.google_client_factory = lambda tok: GoogleContactsClient(access_token=tok, http_client=mock_g_http)
    app.state.ms_client_factory = lambda tok: MicrosoftContactsClient(access_token=tok, http_client=mock_ms_http)

    # Populate a test user with active schedule
    db = TestingSessionLocal()
    repo = SyncRepository(db)
    user = repo.get_or_create_user(
        email="scheduled.user@example.com",
        display_name="Scheduled User",
        google_refresh_token="g_mock_token",
        ms_refresh_token="ms_mock_token",
    )
    repo.update_user_policy(user_id=user.id, schedule_cron="0 6 * * *", sync_policy="ask")
    db.close()

    with TestClient(app) as client:
        yield client, user.id

    app.dependency_overrides.clear()
    app.state.google_client_factory = None
    app.state.ms_client_factory = None


def test_health_check_endpoint(client_with_db) -> None:
    client, _ = client_with_db
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "healthy"
    assert data["service"] == "SyncGM"


def test_agent_action_endpoint(client_with_db) -> None:
    client, user_id = client_with_db
    payload = {
        "user_id": user_id,
        "action": "main_menu",
        "payload": {},
    }
    response = client.post("/api/v1/agent/action", json=payload)
    assert response.status_code == 200
    data = response.json()
    assert "Hello" in data["message"]
    assert len(data["action_chips"]) > 0


def test_cloud_scheduler_unauthorized(client_with_db, monkeypatch) -> None:
    client, _ = client_with_db
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("TESTING", "false")

    response = client.post("/api/v1/jobs/sync-scheduled")
    assert response.status_code == 401
    assert "Unauthorized" in response.json()["detail"]


def test_cloud_scheduler_authorized_execution(client_with_db, monkeypatch) -> None:
    client, user_id = client_with_db
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("TESTING", "false")
    monkeypatch.setenv("SYNCGM_SCHEDULER_SECRET", "super-secret-scheduler-key")

    headers = {"Authorization": "Bearer super-secret-scheduler-key"}
    response = client.post("/api/v1/jobs/sync-scheduled", headers=headers)

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "SUCCESS"
    assert data["users_processed"] >= 1
    assert len(data["executions"]) >= 1
    assert data["executions"][0]["user_id"] == user_id
    assert data["executions"][0]["status"] == "SUCCESS"
