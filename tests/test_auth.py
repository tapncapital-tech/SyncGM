"""Unit and Integration Tests for Global Authentication & Multi-Tenancy."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from execution.database import Base
from execution.repository import SyncRepository
from main import app, get_db


@pytest.fixture
def db_session():
    """In-memory SQLite session fixture with StaticPool for multi-thread test support."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        echo=False,
    )
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = session_factory()
    yield session
    session.close()


@pytest.fixture
def client(db_session: Session):
    def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def test_email_login_and_session(client: TestClient, db_session: Session):
    # 1. Invalid email check
    resp = client.post("/api/v1/auth/login-email", json={"email": "invalid-email"})
    assert resp.status_code == 400
    assert "valid email" in resp.json()["detail"]

    # 2. Valid email login
    test_email = "alex.founder@startup.io"
    resp = client.post("/api/v1/auth/login-email", json={"email": test_email, "name": "Alex"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "SUCCESS"
    assert data["email"] == test_email
    assert "user_id" in data

    # Verify session cookie was set
    assert "syncgm_session" in resp.cookies
    user_id = data["user_id"]

    # 3. Check /api/v1/auth/me using the session cookie
    me_resp = client.get("/api/v1/auth/me")
    assert me_resp.status_code == 200
    me_data = me_resp.json()
    assert me_data["email"] == test_email
    assert me_data["user_id"] == user_id


def test_multi_tenant_isolation(client: TestClient, db_session: Session):
    repo = SyncRepository(db_session)
    user_a = repo.get_or_create_user(email="tenant_a@example.com", display_name="Tenant A")
    user_b = repo.get_or_create_user(email="tenant_b@example.com", display_name="Tenant B")

    # Connect Google for User A only
    repo.update_user_tokens(user_id=user_a.id, google_refresh_token="google_token_a")

    # Query User A state
    state_a = client.get(f"/api/v1/user/state?user_id={user_a.id}").json()
    assert state_a["google_connected"] is True
    assert state_a["email"] == "tenant_a@example.com"

    # Query User B state
    state_b = client.get(f"/api/v1/user/state?user_id={user_b.id}").json()
    assert state_b["google_connected"] is False
    assert state_b["email"] == "tenant_b@example.com"


def test_logout(client: TestClient):
    # Log in first
    client.post("/api/v1/auth/login-email", json={"email": "logout_test@example.com"})
    assert "syncgm_session" in client.cookies

    # Logout
    logout_resp = client.get("/api/v1/auth/logout", follow_redirects=False)
    assert logout_resp.status_code == 303
    assert logout_resp.headers["location"] == "/login"


def test_register_and_password_login(client: TestClient):
    test_email = "sarah.lead@enterprise.com"
    test_password = "SecretPassword123!"

    # 1. Register new account
    reg_resp = client.post(
        "/api/v1/auth/register",
        json={"email": test_email, "password": test_password, "name": "Sarah Lead"},
    )
    assert reg_resp.status_code == 200
    reg_data = reg_resp.json()
    assert reg_data["status"] == "SUCCESS"
    assert reg_data["email"] == test_email
    assert reg_data["display_name"] == "Sarah Lead"
    assert "syncgm_session" in reg_resp.cookies

    # 2. Duplicate registration should fail
    dup_resp = client.post(
        "/api/v1/auth/register",
        json={"email": test_email, "password": "AnotherPassword", "name": "Sarah"},
    )
    assert dup_resp.status_code == 400
    assert "already exists" in dup_resp.json()["detail"]

    # 3. Login with wrong password should fail
    bad_login = client.post(
        "/api/v1/auth/login",
        json={"email": test_email, "password": "WrongPassword"},
    )
    assert bad_login.status_code == 401
    assert "Invalid email or password" in bad_login.json()["detail"]

    # 4. Login with correct password should succeed
    good_login = client.post(
        "/api/v1/auth/login",
        json={"email": test_email, "password": test_password},
    )
    assert good_login.status_code == 200
    good_data = good_login.json()
    assert good_data["status"] == "SUCCESS"
    assert good_data["email"] == test_email
    assert "syncgm_session" in good_login.cookies

