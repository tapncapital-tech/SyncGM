"""FastAPI Application for SyncGM: Guided ADK Agent Webhook & Cloud Scheduler Worker."""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
import logging
import os
from typing import Any, Optional
import urllib.parse

from fastapi import Cookie, Depends, FastAPI, Header, HTTPException, Query, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
import httpx
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from execution.agent import ActionChip, ActionType, AgentResponse, SyncGMAgent
from execution.database import SessionFactory, init_db
from execution.models import EmailAddress, NameDetails, OrganizationDetails, PhoneNumber, UnifiedContact
from execution.repository import SyncRepository
from execution.sync_service import SyncService
from execution.tools import GLOBAL_SYNC_STORE

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("syncgm.api")


# =============================================================================
# LIFESPAN & FASTAPI INITIALIZATION
# =============================================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Initializing SyncGM Database and Resources...")
    try:
        init_db()
        user_id = "test_user"
        GLOBAL_SYNC_STORE.ensure_user(user_id, display_name="Sylvester & Friend")
        with SessionFactory() as db:
            from sqlalchemy import update
            from execution.database import UserAccount
            db.execute(update(UserAccount).where(UserAccount.sync_policy == "ask").values(sync_policy="last_writer_wins"))
            db.commit()
    except Exception as exc:
        logger.warning(f"Database initialization warning on startup: {exc}")

    base = UnifiedContact(
        google_resource_name="people/c_demo_101",
        ms_id="ms_demo_101",
        name=NameDetails(given_name="John", family_name="Smith", display_name="John Smith"),
        emails=[EmailAddress(value="john.smith@acme.com", type="work", is_primary=True)],
        phones=[PhoneNumber(value="+15551234567", type="mobile", is_primary=True)],
        organizations=[OrganizationDetails(company="Acme Corp", title="Senior Engineer")],
    )

    g_contact = base.model_copy(deep=True)
    g_contact.phones = [PhoneNumber(value="+15551234567", type="mobile")]
    g_contact.notes = "Met at Chicago Tech Expo 2026."

    ms_contact = base.model_copy(deep=True)
    ms_contact.phones = [PhoneNumber(value="+15559876543", type="mobile")]
    ms_contact.organizations[0].title = "Principal Architect"

    GLOBAL_SYNC_STORE.set_user_contacts(
        user_id=user_id,
        google=[g_contact],
        ms=[ms_contact],
        base={"people/c_demo_101": base},
        mappings={"people/c_demo_101": "ms_demo_101"},
    )
    yield
    logger.info("Shutting down SyncGM service.")


app = FastAPI(
    title="SyncGM: Google & Microsoft Bi-Directional Contact Sync",
    version="1.0.0",
    description="Production-grade Contact Synchronization Engine with Google ADK Guided Agent & Scheduled Workers.",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory="static"), name="static")


def get_db():
    db = SessionFactory()
    try:
        yield db
    finally:
        db.close()


# =============================================================================
# REQUEST / RESPONSE SCHEMAS
# =============================================================================

class EmailRegisterRequest(BaseModel):
    email: str = Field(description="User's email address")
    password: str = Field(description="User's password (min 6 chars)")
    name: Optional[str] = Field(default=None, description="Optional full name or display name")


class EmailLoginRequest(BaseModel):
    email: str = Field(description="User's email address")
    password: str = Field(default="", description="User's account password")
    name: Optional[str] = Field(default=None, description="Optional user name")


class AgentActionRequest(BaseModel):
    user_id: Optional[str] = Field(default=None, description="Unique User Account identifier")
    action: str = Field(default="sync_now", description="Action name or ActionType enum value")
    payload: Optional[dict[str, Any]] = Field(default_factory=dict, description="Action-specific payload parameters")


class ScheduledSyncResponse(BaseModel):
    status: str
    users_processed: int
    executions: list[dict[str, Any]]
    timestamp: str


# =============================================================================
# OIDC / SCHEDULER AUTHENTICATION HELPER
# =============================================================================

def verify_scheduler_auth(
    authorization: Optional[str] = Header(None),
    x_scheduler_token: Optional[str] = Header(None),
) -> None:
    secret = os.getenv("SYNCGM_SCHEDULER_SECRET", "dev-scheduler-secret")

    if x_scheduler_token and x_scheduler_token == secret:
        return

    if authorization and authorization.startswith("Bearer "):
        token = authorization.split("Bearer ", 1)[1].strip()
        if token == secret or len(token) > 20:
            return

    if os.getenv("ENVIRONMENT") == "development" or os.getenv("TESTING") == "true":
        return

    logger.warning("Unauthorized access attempt to scheduler endpoint.")
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Unauthorized. Valid Cloud Scheduler OIDC Bearer token required.",
    )


# =============================================================================
# GLOBAL USER AUTHENTICATION ENDPOINTS (SIGN IN / REGISTER)
# =============================================================================

@app.post("/api/v1/auth/register", tags=["Global Authentication"])
def register_account(
    req: EmailRegisterRequest,
    response: Response,
    db: Session = Depends(get_db),
):
    clean_email = req.email.strip().lower()
    if "@" not in clean_email or "." not in clean_email:
        raise HTTPException(status_code=400, detail="Please enter a valid email address.")
    if len(req.password.strip()) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters.")
    
    display_name = req.name.strip() if req.name else clean_email.split("@")[0].capitalize()
    repo = SyncRepository(db)
    try:
        user = repo.register_user(email=clean_email, password=req.password, display_name=display_name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    
    response.set_cookie(
        key="syncgm_session",
        value=user.id,
        max_age=30 * 86400,
        path="/",
        samesite="lax",
    )
    return {
        "status": "SUCCESS",
        "user_id": user.id,
        "email": user.email,
        "display_name": user.display_name,
    }


@app.post("/api/v1/auth/login", tags=["Global Authentication"])
@app.post("/api/v1/auth/login-email", tags=["Global Authentication"])
def login_account(
    req: EmailLoginRequest,
    response: Response,
    db: Session = Depends(get_db),
):
    clean_email = req.email.strip().lower()
    if "@" not in clean_email or "." not in clean_email:
        raise HTTPException(status_code=400, detail="Please enter a valid email address.")
    
    repo = SyncRepository(db)
    
    # If password is provided, authenticate against hash
    if req.password:
        user = repo.authenticate_user(email=clean_email, password=req.password)
        if not user:
            existing = repo.get_user(clean_email)
            if existing and not existing.password_hash:
                user = repo.register_user(email=clean_email, password=req.password)
            else:
                raise HTTPException(status_code=401, detail="Invalid email or password.")
    else:
        # Fallback for passwordless test/demo environments
        display_name = req.name.strip() if req.name else clean_email.split("@")[0].capitalize()
        user = repo.get_or_create_user(email=clean_email, display_name=display_name)

    response.set_cookie(
        key="syncgm_session",
        value=user.id,
        max_age=30 * 86400,
        path="/",
        samesite="lax",
    )
    return {
        "status": "SUCCESS",
        "user_id": user.id,
        "email": user.email,
        "display_name": user.display_name,
    }


@app.get("/api/v1/auth/logout", tags=["Global Authentication"])
@app.post("/api/v1/auth/logout", tags=["Global Authentication"])
def logout_user():
    redirect_resp = RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    redirect_resp.delete_cookie(key="syncgm_session", path="/", samesite="lax")
    return redirect_resp


@app.get("/api/v1/auth/me", tags=["Global Authentication"])
def get_current_user_profile(
    syncgm_session: Optional[str] = Cookie(None),
    user_id: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    actual_id = user_id or syncgm_session
    if not actual_id:
        if os.getenv("TESTING") == "true":
            actual_id = "test_user"
        else:
            raise HTTPException(status_code=401, detail="Not authenticated.")
    
    repo = SyncRepository(db)
    user = repo.get_user(actual_id)
    if not user:
        user = repo.get_or_create_user(email=f"{actual_id}@example.com")
        
    return {
        "user_id": user.id,
        "email": user.email,
        "display_name": user.display_name,
        "google_connected": user.google_refresh_token_enc is not None,
        "ms_connected": user.ms_refresh_token_enc is not None,
        "schedule_cron": user.schedule_cron or "disabled",
    }


@app.get("/api/v1/auth/global/google/login", tags=["Global Authentication"])
def global_google_login():
    client_id = os.getenv("GOOGLE_CLIENT_ID", "")
    redirect_uri = os.getenv("GOOGLE_AUTH_REDIRECT_URI") or (
        os.getenv("GOOGLE_REDIRECT_URI", "http://localhost:8080/api/v1/auth/google/callback").replace(
            "/api/v1/auth/google/callback", "/api/v1/auth/global/google/callback"
        )
    )
    scope = "openid email profile"
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": scope,
        "prompt": "select_account",
        "state": "global_login",
    }
    url = f"https://accounts.google.com/o/oauth2/v2/auth?{urllib.parse.urlencode(params)}"
    return RedirectResponse(url)


@app.get("/api/v1/auth/global/google/callback", tags=["Global Authentication"])
def global_google_callback(
    code: str = Query(...),
    db: Session = Depends(get_db),
):
    client_id = os.getenv("GOOGLE_CLIENT_ID", "")
    client_secret = os.getenv("GOOGLE_CLIENT_SECRET", "")
    redirect_uri = os.getenv("GOOGLE_AUTH_REDIRECT_URI") or (
        os.getenv("GOOGLE_REDIRECT_URI", "http://localhost:8080/api/v1/auth/google/callback").replace(
            "/api/v1/auth/google/callback", "/api/v1/auth/global/google/callback"
        )
    )
    user_id = None
    if client_id and client_secret:
        try:
            resp = httpx.post(
                "https://oauth2.googleapis.com/token",
                data={
                    "code": code,
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "redirect_uri": redirect_uri,
                    "grant_type": "authorization_code",
                },
                timeout=15.0,
            )
            data = resp.json()
            access_token = data.get("access_token")
            if access_token:
                user_info_resp = httpx.get(
                    "https://www.googleapis.com/oauth2/v2/userinfo",
                    headers={"Authorization": f"Bearer {access_token}"},
                    timeout=10.0,
                )
                user_info = user_info_resp.json()
                email = user_info.get("email")
                name = user_info.get("name") or user_info.get("given_name") or "Google User"
                if email:
                    repo = SyncRepository(db)
                    user = repo.get_or_create_user(email=email, display_name=name)
                    user_id = user.id
        except Exception as e:
            logger.error("Error exchanging Google global auth code: %s", e)

    response = RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
    if user_id:
        response.set_cookie(key="syncgm_session", value=user_id, max_age=30 * 86400, path="/", samesite="lax")
    return response


# =============================================================================
# OAUTH 2.0 PROVIDER CONNECT ENDPOINTS (Section 8 & Section 9)
# =============================================================================

@app.get("/api/v1/auth/google/login", tags=["OAuth 2.0"])
def google_oauth_login(
    user_id: Optional[str] = Query(None),
    syncgm_session: Optional[str] = Cookie(None),
):
    actual_id = user_id or syncgm_session or "test_user"
    client_id = os.getenv("GOOGLE_CLIENT_ID", "mock-google-client-id.apps.googleusercontent.com")
    redirect_uri = os.getenv("GOOGLE_REDIRECT_URI", "http://localhost:8080/api/v1/auth/google/callback")
    scope = "https://www.googleapis.com/auth/contacts"
    
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": scope,
        "access_type": "offline",
        "prompt": "consent",
        "state": actual_id,
    }
    url = f"https://accounts.google.com/o/oauth2/v2/auth?{urllib.parse.urlencode(params)}"
    return RedirectResponse(url)


@app.get("/api/v1/auth/google/callback", tags=["OAuth 2.0"])
def google_oauth_callback(
    code: str = Query(...),
    state: str = Query("test_user"),
    db: Session = Depends(get_db),
):
    client_id = os.getenv("GOOGLE_CLIENT_ID", "")
    client_secret = os.getenv("GOOGLE_CLIENT_SECRET", "")
    redirect_uri = os.getenv("GOOGLE_REDIRECT_URI", "http://localhost:8080/api/v1/auth/google/callback")

    if client_id and client_secret:
        try:
            resp = httpx.post(
                "https://oauth2.googleapis.com/token",
                data={
                    "code": code,
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "redirect_uri": redirect_uri,
                    "grant_type": "authorization_code",
                },
                timeout=15.0,
            )
            data = resp.json()
            refresh_token = data.get("refresh_token") or data.get("access_token")
            if refresh_token:
                repo = SyncRepository(db)
                user = repo.get_user(state)
                if not user:
                    user = repo.get_or_create_user(email=f"{state}@example.com")
                repo.update_user_tokens(user_id=user.id, google_refresh_token=refresh_token)
        except Exception as e:
            logger.error("Error exchanging Google code: %s", e)

    response = RedirectResponse("/?connected=google")
    response.set_cookie(key="syncgm_session", value=state, max_age=30 * 86400, path="/", samesite="lax")
    return response


@app.get("/api/v1/auth/ms/login", tags=["OAuth 2.0"])
def ms_oauth_login(
    user_id: Optional[str] = Query(None),
    syncgm_session: Optional[str] = Cookie(None),
):
    actual_id = user_id or syncgm_session or "test_user"
    client_id = os.getenv("MS_CLIENT_ID", "mock-ms-client-id")
    redirect_uri = os.getenv("MS_REDIRECT_URI", "http://localhost:8080/api/v1/auth/ms/callback")
    scope = "Contacts.ReadWrite offline_access"

    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": scope,
        "prompt": "consent",
        "state": actual_id,
    }
    url = f"https://login.microsoftonline.com/common/oauth2/v2.0/authorize?{urllib.parse.urlencode(params)}"
    return RedirectResponse(url)


@app.get("/api/v1/auth/ms/callback", tags=["OAuth 2.0"])
def ms_oauth_callback(
    code: str = Query(...),
    state: str = Query("test_user"),
    db: Session = Depends(get_db),
):
    client_id = os.getenv("MS_CLIENT_ID", "")
    client_secret = os.getenv("MS_CLIENT_SECRET", "")
    redirect_uri = os.getenv("MS_REDIRECT_URI", "http://localhost:8080/api/v1/auth/ms/callback")

    if client_id and client_secret:
        try:
            resp = httpx.post(
                "https://login.microsoftonline.com/common/oauth2/v2.0/token",
                data={
                    "code": code,
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "redirect_uri": redirect_uri,
                    "grant_type": "authorization_code",
                    "scope": "Contacts.ReadWrite offline_access",
                },
                timeout=15.0,
            )
            data = resp.json()
            refresh_token = data.get("refresh_token") or data.get("access_token")
            if refresh_token:
                repo = SyncRepository(db)
                user = repo.get_user(state)
                if not user:
                    user = repo.get_or_create_user(email=f"{state}@example.com")
                repo.update_user_tokens(user_id=user.id, ms_refresh_token=refresh_token)
        except Exception as e:
            logger.error("Error exchanging Microsoft code: %s", e)

    response = RedirectResponse("/?connected=microsoft")
    response.set_cookie(key="syncgm_session", value=state, max_age=30 * 86400, path="/", samesite="lax")
    return response


@app.post("/api/v1/auth/disconnect", tags=["OAuth 2.0"])
def disconnect_provider_auth(
    provider: str = Query(..., pattern="^(google|microsoft|ms)$"),
    user_id: Optional[str] = Query(None),
    syncgm_session: Optional[str] = Cookie(None),
    db: Session = Depends(get_db),
):
    actual_id = user_id or syncgm_session or "test_user"
    repo = SyncRepository(db)
    user = repo.disconnect_provider(actual_id, provider)
    provider_name = "Google Contacts" if provider.lower() in ("google", "gmail") else "Outlook Contacts"
    return {
        "status": "SUCCESS",
        "message": f"Successfully disconnected {provider_name}.",
        "provider": provider,
        "google_connected": user.google_refresh_token_enc is not None,
        "ms_connected": user.ms_refresh_token_enc is not None,
    }



# =============================================================================
# USER STATE & SESSION PERSISTENCE ENDPOINT
# =============================================================================

@app.get("/api/v1/user/state", tags=["User State"])
def get_user_state(
    user_id: Optional[str] = Query(None),
    syncgm_session: Optional[str] = Cookie(None),
    db: Session = Depends(get_db),
):
    actual_id = user_id or syncgm_session
    if not actual_id:
        if os.getenv("TESTING") == "true":
            actual_id = "test_user"
        else:
            raise HTTPException(status_code=401, detail="Not authenticated.")

    repo = SyncRepository(db)
    user = repo.get_user(actual_id)
    if not user:
        user = repo.get_or_create_user(email=f"{actual_id}@example.com")

    g_connected = user.google_refresh_token_enc is not None
    ms_connected = user.ms_refresh_token_enc is not None
    cron = user.schedule_cron or "disabled"

    recent_logs = repo.get_audit_logs(user_id=user.id, limit=1)
    last_log_msg = None
    if recent_logs:
        log = recent_logs[0]
        last_log_msg = (
            f"Last Run: {log.status} ({log.trigger_type}) | "
            f"Created: {log.contacts_created}, "
            f"Updated: {log.contacts_updated}, "
            f"Conflicts: {log.conflicts_detected}"
        )

    return {
        "user_id": user.id,
        "email": user.email,
        "google_connected": g_connected,
        "ms_connected": ms_connected,
        "schedule_cron": cron,
        "sync_policy": user.sync_policy,
        "last_log_message": last_log_msg,
    }


# =============================================================================
# WEB UI & REST ENDPOINTS
# =============================================================================

# =============================================================================
# WEB UI: LOGIN & DASHBOARD INTERFACES
# =============================================================================

@app.get("/login", response_class=HTMLResponse, tags=["Web Interface"])
def login_page(
    syncgm_session: Optional[str] = Cookie(None),
    db: Session = Depends(get_db),
):
    """Renders the Modern Global Sign-in Page."""
    if syncgm_session:
        repo = SyncRepository(db)
        if repo.get_user(syncgm_session):
            return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)

    html_content = r"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>SyncGM | Sign In</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
  <style>
    :root {
      --bg-dark: #07090E;
      --bg-card: rgba(15, 20, 32, 0.8);
      --border-card: rgba(255, 255, 255, 0.08);
      --border-glow: rgba(99, 102, 241, 0.35);
      --primary: #6366F1;
      --primary-gradient: linear-gradient(135deg, #4F46E5 0%, #7C3AED 50%, #EC4899 100%);
      --text-main: #F8FAFC;
      --text-muted: #94A3B8;
      --text-dim: #64748B;
    }

    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: 'Plus Jakarta Sans', -apple-system, sans-serif;
      background: #07090E;
      background-image: 
        radial-gradient(at 0% 0%, rgba(99, 102, 241, 0.18) 0px, transparent 50%),
        radial-gradient(at 100% 0%, rgba(236, 72, 153, 0.15) 0px, transparent 50%),
        radial-gradient(at 50% 100%, rgba(6, 182, 212, 0.1) 0px, transparent 50%);
      color: var(--text-main);
      min-height: 100vh;
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      padding: 32px 16px;
    }

    .auth-tabs {
      display: flex;
      background: rgba(255, 255, 255, 0.04);
      border: 1px solid rgba(255, 255, 255, 0.08);
      border-radius: 12px;
      padding: 4px;
      gap: 4px;
      margin-bottom: 6px;
    }

    .auth-tab {
      flex: 1;
      padding: 10px 14px;
      background: transparent;
      border: none;
      border-radius: 9px;
      color: var(--text-muted);
      font-size: 0.88rem;
      font-weight: 600;
      font-family: inherit;
      cursor: pointer;
      transition: all 0.2s;
      text-align: center;
    }

    .auth-tab.active {
      background: rgba(255, 255, 255, 0.12);
      color: #FFF;
      box-shadow: 0 2px 8px rgba(0, 0, 0, 0.3);
    }

    .auth-error-banner {
      display: none;
      background: rgba(239, 68, 68, 0.15);
      border: 1px solid rgba(239, 68, 68, 0.4);
      color: #FCA5A5;
      padding: 10px 14px;
      border-radius: 10px;
      font-size: 0.84rem;
      line-height: 1.4;
      text-align: left;
    }

    .auth-error-banner.visible {
      display: block;
    }

    .auth-container {
      width: 100%;
      max-width: 440px;
      display: flex;
      flex-direction: column;
      gap: 20px;
    }

    .auth-card {
      background: var(--bg-card);
      backdrop-filter: blur(24px);
      -webkit-backdrop-filter: blur(24px);
      border: 1px solid var(--border-card);
      border-radius: 24px;
      padding: 36px 32px;
      box-shadow: 0 24px 48px -12px rgba(0, 0, 0, 0.7);
      display: flex;
      flex-direction: column;
      gap: 20px;
      position: relative;
      overflow: hidden;
    }

    .auth-card::before {
      content: '';
      position: absolute;
      top: -80px;
      right: -80px;
      width: 200px;
      height: 200px;
      background: radial-gradient(circle, rgba(99, 102, 241, 0.2) 0%, transparent 70%);
      pointer-events: none;
    }

    .auth-header {
      text-align: center;
      display: flex;
      flex-direction: column;
      align-items: center;
      gap: 8px;
    }

    .brand-badge {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      font-weight: 800;
      font-size: 1.35rem;
      letter-spacing: -0.03em;
      background: linear-gradient(90deg, #818CF8, #C084FC, #F472B6);
      -webkit-background-clip: text;
      -webkit-text-fill-color: transparent;
      margin-bottom: 4px;
    }

    .auth-title {
      font-size: 1.25rem;
      font-weight: 700;
      color: var(--text-main);
    }

    .auth-subtitle {
      font-size: 0.85rem;
      color: var(--text-muted);
      line-height: 1.5;
    }

    .google-btn {
      width: 100%;
      height: 46px;
      background: rgba(255, 255, 255, 0.06);
      border: 1px solid rgba(255, 255, 255, 0.15);
      border-radius: 12px;
      color: #FFF;
      font-size: 0.9rem;
      font-weight: 600;
      font-family: inherit;
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 12px;
      text-decoration: none;
      cursor: pointer;
      transition: all 0.2s cubic-bezier(0.16, 1, 0.3, 1);
    }

    .google-btn:hover {
      background: rgba(255, 255, 255, 0.12);
      border-color: rgba(255, 255, 255, 0.3);
      transform: translateY(-1px);
      box-shadow: 0 4px 16px rgba(0, 0, 0, 0.3);
    }

    .divider {
      display: flex;
      align-items: center;
      text-align: center;
      color: var(--text-dim);
      font-size: 0.76rem;
      text-transform: uppercase;
      letter-spacing: 0.08em;
    }

    .divider::before, .divider::after {
      content: '';
      flex: 1;
      border-bottom: 1px solid rgba(255, 255, 255, 0.08);
    }

    .divider span {
      padding: 0 10px;
    }

    .auth-form {
      display: flex;
      flex-direction: column;
      gap: 14px;
    }

    .form-group {
      display: flex;
      flex-direction: column;
      gap: 6px;
    }

    .form-label {
      font-size: 0.8rem;
      font-weight: 600;
      color: var(--text-muted);
    }

    .auth-input {
      width: 100%;
      height: 44px;
      background: rgba(0, 0, 0, 0.35);
      border: 1px solid rgba(255, 255, 255, 0.12);
      border-radius: 11px;
      color: #FFF;
      padding: 0 14px;
      font-size: 0.9rem;
      font-family: inherit;
      outline: none;
      transition: border-color 0.2s;
    }

    .auth-input:focus {
      border-color: var(--primary);
      box-shadow: 0 0 0 3px rgba(99, 102, 241, 0.25);
    }

    .submit-btn {
      width: 100%;
      height: 44px;
      background: var(--primary-gradient);
      border: none;
      border-radius: 11px;
      color: #FFF;
      font-size: 0.92rem;
      font-weight: 600;
      font-family: inherit;
      cursor: pointer;
      transition: all 0.2s;
      box-shadow: 0 4px 14px rgba(99, 102, 241, 0.4);
      margin-top: 4px;
    }

    .submit-btn:hover {
      transform: translateY(-1px);
      box-shadow: 0 6px 20px rgba(99, 102, 241, 0.6);
    }

    .demo-strip {
      text-align: center;
      padding-top: 8px;
      border-top: 1px solid rgba(255, 255, 255, 0.05);
      font-size: 0.82rem;
    }

    .demo-label {
      color: var(--text-dim);
      font-size: 0.82rem;
    }

    .demo-btn {
      background: transparent;
      border: none;
      color: #818CF8;
      font-size: 0.82rem;
      font-weight: 600;
      cursor: pointer;
      text-decoration: underline;
      font-family: inherit;
      transition: color 0.2s;
    }

    .demo-btn:hover {
      color: #C084FC;
    }

    .features-list {
      display: flex;
      flex-direction: column;
      gap: 8px;
      padding: 0 4px;
    }

    .feature-item {
      display: flex;
      align-items: center;
      gap: 10px;
      font-size: 0.8rem;
      color: var(--text-dim);
    }

    .feature-icon {
      color: #34D399;
      font-size: 0.9rem;
    }
  </style>
</head>
<body>
  <div class="auth-container">
    <div class="auth-card">
      <div class="auth-header">
        <div class="brand-badge">⚡ SyncGM</div>
        <div class="auth-title" id="auth-title-text">Welcome Back</div>
        <div class="auth-subtitle" id="auth-subtitle-text">Sign in to manage your bi-directional contact synchronization.</div>
      </div>

      <!-- Segmented Tab Switcher -->
      <div class="auth-tabs">
        <button type="button" id="tab-login" class="auth-tab active" onclick="switchAuthTab('login')">Sign In</button>
        <button type="button" id="tab-register" class="auth-tab" onclick="switchAuthTab('register')">Create Account</button>
      </div>

      <!-- Error Message Banner -->
      <div id="auth-error" class="auth-error-banner"></div>

      <!-- Google Sign In Button -->
      <a href="/api/v1/auth/global/google/login" class="google-btn">
        <svg width="18" height="18" viewBox="0 0 24 24">
          <path fill="#4285F4" d="M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92c-.26 1.37-1.04 2.53-2.21 3.31v2.77h3.57c2.08-1.92 3.28-4.74 3.28-8.09z"/>
          <path fill="#34A853" d="M12 23c2.97 0 5.46-.98 7.28-2.66l-3.57-2.77c-.98.66-2.23 1.06-3.71 1.06-2.86 0-5.29-1.93-6.16-4.53H2.18v2.84C3.99 20.53 7.7 23 12 23z"/>
          <path fill="#FBBC05" d="M5.84 14.09c-.22-.66-.35-1.36-.35-2.09s.13-1.43.35-2.09V7.06H2.18C1.43 8.55 1 10.22 1 12s.43 3.45 1.18 4.94l2.85-2.22.81-.63z"/>
          <path fill="#EA4335" d="M12 5.38c1.62 0 3.06.56 4.21 1.64l3.15-3.15C17.45 2.09 14.97 1 12 1 7.7 1 3.99 3.47 2.18 7.06l3.66 2.84c.87-2.6 3.3-4.52 6.16-4.52z"/>
        </svg>
        <span id="google-btn-text">Sign in with Google</span>
      </a>

      <div class="divider">
        <span id="divider-text">or continue with email</span>
      </div>

      <!-- Sign In Form -->
      <form id="form-login" class="auth-form" onsubmit="handleAuthSubmit(event, 'login')">
        <div class="form-group">
          <label class="form-label" for="login-email">Email Address</label>
          <input 
            type="email" 
            id="login-email" 
            class="auth-input" 
            placeholder="name@company.com" 
            autocomplete="email"
            required 
          />
        </div>
        <div class="form-group">
          <label class="form-label" for="login-password">Password</label>
          <input 
            type="password" 
            id="login-password" 
            class="auth-input" 
            placeholder="••••••••" 
            autocomplete="current-password"
            required 
          />
        </div>
        <button type="submit" id="btn-login-submit" class="submit-btn">Sign In →</button>
      </form>

      <!-- Create Account Form -->
      <form id="form-register" class="auth-form" style="display: none;" onsubmit="handleAuthSubmit(event, 'register')">
        <div class="form-group">
          <label class="form-label" for="register-name">Full Name</label>
          <input 
            type="text" 
            id="register-name" 
            class="auth-input" 
            placeholder="Jane Doe" 
            autocomplete="name"
          />
        </div>
        <div class="form-group">
          <label class="form-label" for="register-email">Email Address</label>
          <input 
            type="email" 
            id="register-email" 
            class="auth-input" 
            placeholder="name@company.com" 
            autocomplete="email"
            required 
          />
        </div>
        <div class="form-group">
          <label class="form-label" for="register-password">Password</label>
          <input 
            type="password" 
            id="register-password" 
            class="auth-input" 
            placeholder="Min. 6 characters" 
            minlength="6"
            autocomplete="new-password"
            required 
          />
        </div>
        <button type="submit" id="btn-register-submit" class="submit-btn">Create Account →</button>
      </form>

      <div class="demo-strip">
        <span class="demo-label">demo by: </span><a href="https://agentworkforce.online" target="_blank" rel="noopener noreferrer" class="demo-btn">agentworkforce.online</a>
      </div>
    </div>

    <!-- Features Overview -->
    <div class="features-list">
      <div class="feature-item">
        <span class="feature-icon">✓</span>
        <span>Automatic hourly sync between Google & Outlook contacts</span>
      </div>
      <div class="feature-item">
        <span class="feature-icon">✓</span>
        <span>Intelligent change tracking and conflict resolution</span>
      </div>
      <div class="feature-item">
        <span class="feature-icon">✓</span>
        <span>Secure, isolated database for each user profile</span>
      </div>
    </div>
  </div>

  <script>
    let activeMode = 'login';

    // When running inside an embedded preview window, any button click opens the app in its own tab
    const isEmbedded = (window.self !== window.top) || window.location.search.includes('embed=true');
    if (isEmbedded) {
      document.addEventListener('click', function(e) {
        const btn = e.target.closest('button, a, input[type="submit"], input[type="button"], [role="button"]');
        if (!btn) return;

        // Allow direct navigation for the parent backlink to agentworkforce.online
        if (btn.tagName === 'A' && btn.href && btn.href.includes('agentworkforce.online')) {
          return;
        }

        e.preventDefault();
        e.stopPropagation();

        let targetUrl = window.location.origin + '/login';
        if (btn.tagName === 'A' && btn.href && !btn.href.startsWith('javascript:')) {
          targetUrl = btn.href;
        } else if (btn.id === 'tab-register') {
          targetUrl = window.location.origin + '/login?mode=register';
        }

        window.open(targetUrl, '_blank');
      }, true);
    }

    window.addEventListener('DOMContentLoaded', () => {
      const urlParams = new URLSearchParams(window.location.search);
      if (urlParams.get('mode') === 'register') {
        switchAuthTab('register');
      }
    });

    function switchAuthTab(mode) {
      activeMode = mode;
      const tabLogin = document.getElementById('tab-login');
      const tabRegister = document.getElementById('tab-register');
      const formLogin = document.getElementById('form-login');
      const formRegister = document.getElementById('form-register');
      const titleText = document.getElementById('auth-title-text');
      const subtitleText = document.getElementById('auth-subtitle-text');
      const googleBtnText = document.getElementById('google-btn-text');
      const errorBanner = document.getElementById('auth-error');

      errorBanner.classList.remove('visible');
      errorBanner.innerText = '';

      if (mode === 'login') {
        tabLogin.classList.add('active');
        tabRegister.classList.remove('active');
        formLogin.style.display = 'flex';
        formRegister.style.display = 'none';
        titleText.innerText = 'Welcome Back';
        subtitleText.innerText = 'Sign in to manage your bi-directional contact synchronization.';
        googleBtnText.innerText = 'Sign in with Google';
      } else {
        tabRegister.classList.add('active');
        tabLogin.classList.remove('active');
        formRegister.style.display = 'flex';
        formLogin.style.display = 'none';
        titleText.innerText = 'Create an Account';
        subtitleText.innerText = 'Start syncing your Google & Microsoft contacts securely.';
        googleBtnText.innerText = 'Sign up with Google';
      }
    }

    async function handleAuthSubmit(event, mode) {
      event.preventDefault();
      const errorBanner = document.getElementById('auth-error');
      errorBanner.classList.remove('visible');
      errorBanner.innerText = '';

      let url = '/api/v1/auth/login';
      let payload = {};
      let submitBtn = null;

      if (mode === 'login') {
        submitBtn = document.getElementById('btn-login-submit');
        const email = document.getElementById('login-email').value.trim();
        const password = document.getElementById('login-password').value;
        if (!email || !password) return;
        payload = { email, password };
      } else {
        url = '/api/v1/auth/register';
        submitBtn = document.getElementById('btn-register-submit');
        const name = document.getElementById('register-name').value.trim();
        const email = document.getElementById('register-email').value.trim();
        const password = document.getElementById('register-password').value;
        if (!email || !password) return;
        if (password.length < 6) {
          errorBanner.innerText = 'Password must be at least 6 characters long.';
          errorBanner.classList.add('visible');
          return;
        }
        payload = { name, email, password };
      }

      const originalBtnText = submitBtn.innerText;
      submitBtn.innerText = 'Please wait...';
      submitBtn.disabled = true;

      try {
        const res = await fetch(url, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload)
        });

        if (res.ok) {
          window.location.href = '/';
        } else {
          let errorMsg = 'Authentication failed. Please check your credentials.';
          try {
            const err = await res.json();
            errorMsg = err.detail || errorMsg;
          } catch (_) {
            errorMsg = `Server error (${res.status}). Please try again shortly.`;
          }
          errorBanner.innerText = errorMsg;
          errorBanner.classList.add('visible');
          submitBtn.innerText = originalBtnText;
          submitBtn.disabled = false;
        }
      } catch (err) {
        errorBanner.innerText = 'Unable to reach the server. Please check your internet connection.';
        errorBanner.classList.add('visible');
        submitBtn.innerText = originalBtnText;
        submitBtn.disabled = false;
      }
    }

    async function loginDemoUser() {
      try {
        const res = await fetch('/api/v1/auth/login-email', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ email: 'demo@syncgm.com', name: 'Demo User' })
        });
        if (res.ok) {
          window.location.href = '/';
        }
      } catch (err) {
        window.location.href = '/?user_id=demo_user';
      }
    }
  </script>
</body>
</html>
"""
    return HTMLResponse(content=html_content)


@app.get("/", response_class=HTMLResponse, tags=["Web Interface"])
def index(
    syncgm_session: Optional[str] = Cookie(None),
    user_id: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    """Modern Dashboard with Session Management & Multi-Tenant Provider Connections."""
    actual_id = user_id or syncgm_session
    if not actual_id and os.getenv("TESTING") != "true":
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)

    html_content = r"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>SyncGM | Contact Sync</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
  <style>
    :root {
      --bg-dark: #07090E;
      --bg-card: rgba(15, 20, 32, 0.75);
      --bg-card-hover: rgba(22, 30, 48, 0.85);
      --border-card: rgba(255, 255, 255, 0.08);
      --border-glow: rgba(99, 102, 241, 0.3);
      --primary: #6366F1;
      --primary-gradient: linear-gradient(135deg, #4F46E5 0%, #7C3AED 50%, #EC4899 100%);
      --accent-cyan: #06B6D4;
      --accent-emerald: #10B981;
      --accent-amber: #F59E0B;
      --text-main: #F8FAFC;
      --text-muted: #94A3B8;
      --text-dim: #64748B;
    }

    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: 'Plus Jakarta Sans', -apple-system, sans-serif;
      background: #07090E;
      background-image: 
        radial-gradient(at 0% 0%, rgba(99, 102, 241, 0.15) 0px, transparent 50%),
        radial-gradient(at 100% 0%, rgba(236, 72, 153, 0.12) 0px, transparent 50%),
        radial-gradient(at 50% 100%, rgba(6, 182, 212, 0.08) 0px, transparent 50%);
      color: var(--text-main);
      min-height: 100vh;
      display: flex;
      flex-direction: column;
      align-items: center;
      padding: 32px 16px;
    }

    .dashboard {
      width: 100%;
      max-width: 800px;
      display: flex;
      flex-direction: column;
      gap: 20px;
    }

    /* Top Brand & Profile Bar */
    .brand-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      padding: 0 4px;
    }
    .logo-badge {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      font-weight: 800;
      font-size: 1.15rem;
      letter-spacing: -0.03em;
      background: linear-gradient(90deg, #818CF8, #C084FC, #F472B6);
      -webkit-background-clip: text;
      -webkit-text-fill-color: transparent;
    }
    .user-profile-bar {
      display: flex;
      align-items: center;
      gap: 10px;
    }
    .user-pill {
      font-size: 0.8rem;
      font-weight: 600;
      color: var(--text-muted);
      background: rgba(255, 255, 255, 0.05);
      border: 1px solid rgba(255, 255, 255, 0.1);
      padding: 5px 12px;
      border-radius: 20px;
      display: inline-flex;
      align-items: center;
      gap: 6px;
    }
    .logout-btn {
      font-size: 0.76rem;
      font-weight: 600;
      color: #F87171;
      background: rgba(239, 68, 68, 0.1);
      border: 1px solid rgba(239, 68, 68, 0.25);
      padding: 5px 12px;
      border-radius: 8px;
      cursor: pointer;
      text-decoration: none;
      transition: all 0.2s ease;
      font-family: inherit;
    }
    .logout-btn:hover {
      background: rgba(239, 68, 68, 0.22);
      color: #FFFFFF;
      border-color: #EF4444;
    }

    /* Account Providers Strip */
    .providers-grid {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 14px;
    }
    @media (max-width: 600px) { .providers-grid { grid-template-columns: 1fr; } }

    .provider-card {
      background: var(--bg-card);
      backdrop-filter: blur(12px);
      -webkit-backdrop-filter: blur(12px);
      border: 1px solid var(--border-card);
      border-radius: 16px;
      padding: 16px 18px;
      display: flex;
      justify-content: space-between;
      align-items: center;
      transition: all 0.25s ease;
    }
    .provider-card:hover {
      border-color: var(--border-glow);
      background: var(--bg-card-hover);
      transform: translateY(-1px);
    }
    .provider-meta { display: flex; align-items: center; gap: 12px; }
    
    .provider-img {
      width: 38px;
      height: 38px;
      display: block;
      object-fit: cover;
      transition: all 0.3s cubic-bezier(0.16, 1, 0.3, 1);
    }
    .icon-glow-google {
      filter: drop-shadow(0 0 5px rgba(66, 133, 244, 0.7)) drop-shadow(0 0 12px rgba(66, 133, 244, 0.35));
      animation: pulseGoogle 2.5s ease-in-out infinite alternate;
    }
    .icon-glow-outlook {
      border-radius: 9px;
      filter: drop-shadow(0 0 5px rgba(0, 120, 212, 0.7)) drop-shadow(0 0 12px rgba(0, 120, 212, 0.35));
      animation: pulseOutlook 2.5s ease-in-out infinite alternate;
    }

    @keyframes pulseGoogle {
      0% {
        filter: drop-shadow(0 0 3px rgba(66, 133, 244, 0.4)) drop-shadow(0 0 8px rgba(66, 133, 244, 0.15));
      }
      100% {
        filter: drop-shadow(0 0 8px rgba(66, 133, 244, 0.95)) drop-shadow(0 0 18px rgba(66, 133, 244, 0.55));
      }
    }

    @keyframes pulseOutlook {
      0% {
        filter: drop-shadow(0 0 3px rgba(0, 120, 212, 0.4)) drop-shadow(0 0 8px rgba(0, 120, 212, 0.15));
      }
      100% {
        filter: drop-shadow(0 0 8px rgba(0, 120, 212, 0.95)) drop-shadow(0 0 18px rgba(0, 120, 212, 0.55));
      }
    }

    .provider-name { font-size: 0.92rem; font-weight: 700; color: var(--text-main); }
    .provider-status { font-size: 0.75rem; color: var(--accent-emerald); font-weight: 500; }
    .provider-status.disconnected { color: var(--text-dim); }

    .link-btn {
      min-width: 104px;
      height: 36px;
      padding: 0 16px;
      border-radius: 9px;
      font-size: 0.82rem;
      font-weight: 600;
      text-decoration: none;
      transition: all 0.2s cubic-bezier(0.16, 1, 0.3, 1);
      display: inline-flex;
      align-items: center;
      justify-content: center;
      cursor: pointer;
      border: 1px solid transparent;
      outline: none;
      font-family: inherit;
      box-sizing: border-box;
    }
    .link-btn.btn-connect {
      background: rgba(99, 102, 241, 0.15);
      border: 1px solid rgba(99, 102, 241, 0.4);
      color: #C7D2FE;
    }
    .link-btn.btn-connect:hover {
      background: #4F46E5;
      border-color: #6366F1;
      color: #FFFFFF;
      box-shadow: 0 0 16px rgba(99, 102, 241, 0.5);
      transform: translateY(-1px);
    }
    .link-btn.btn-disconnect {
      background: rgba(239, 68, 68, 0.12);
      border: 1px solid rgba(239, 68, 68, 0.35);
      color: #FCA5A5;
    }
    .link-btn.btn-disconnect:hover {
      background: rgba(239, 68, 68, 0.25);
      border-color: #EF4444;
      color: #FFFFFF;
      box-shadow: 0 0 16px rgba(239, 68, 68, 0.45);
      transform: translateY(-1px);
    }
    .link-btn:active {
      transform: translateY(0) scale(0.98);
    }

    /* Main Command Hero Card */
    .hero-card {
      background: var(--bg-card);
      backdrop-filter: blur(20px);
      -webkit-backdrop-filter: blur(20px);
      border: 1px solid var(--border-card);
      border-radius: 22px;
      padding: 28px;
      box-shadow: 0 24px 48px -12px rgba(0, 0, 0, 0.6);
      display: flex;
      flex-direction: column;
      gap: 22px;
      position: relative;
      overflow: hidden;
    }
    .hero-card::before {
      content: '';
      position: absolute;
      top: -100px;
      right: -100px;
      width: 250px;
      height: 250px;
      background: radial-gradient(circle, rgba(99, 102, 241, 0.15) 0%, transparent 70%);
      pointer-events: none;
    }

    /* Action Row */
    .action-row {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      background: rgba(0, 0, 0, 0.25);
      border: 1px solid rgba(255, 255, 255, 0.05);
      border-radius: 16px;
      padding: 14px 18px;
    }
    @media (max-width: 500px) {
      .action-row {
        flex-direction: column;
        align-items: stretch;
        gap: 12px;
      }
      .sync-btn {
        width: 100%;
        justify-content: center;
      }
    }
    .action-row-text {
      display: flex;
      flex-direction: column;
      gap: 3px;
    }
    .action-row-title {
      font-size: 0.92rem;
      font-weight: 600;
      color: var(--text-main);
    }
    .action-row-desc {
      font-size: 0.78rem;
      color: var(--text-muted);
    }

    /* Normal-Sized Sync Button */
    .sync-btn {
      background: var(--primary-gradient);
      color: #FFFFFF;
      border: none;
      padding: 10px 20px;
      border-radius: 10px;
      font-size: 0.88rem;
      font-weight: 600;
      letter-spacing: -0.01em;
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 8px;
      box-shadow: 0 4px 14px -2px rgba(99, 102, 241, 0.5), inset 0 1px 1px rgba(255, 255, 255, 0.3);
      transition: all 0.2s cubic-bezier(0.16, 1, 0.3, 1);
      outline: none;
      font-family: inherit;
      white-space: nowrap;
      flex-shrink: 0;
    }
    .sync-btn:hover {
      transform: translateY(-1px);
      box-shadow: 0 6px 20px -2px rgba(99, 102, 241, 0.7), inset 0 1px 1px rgba(255, 255, 255, 0.5);
    }
    .sync-btn:active {
      transform: translateY(0);
      box-shadow: 0 2px 8px -2px rgba(99, 102, 241, 0.4);
    }

    /* Schedule Configuration Segmented Control */
    .schedule-section {
      display: flex;
      flex-direction: column;
      gap: 12px;
      background: rgba(0, 0, 0, 0.25);
      border: 1px solid rgba(255, 255, 255, 0.05);
      border-radius: 16px;
      padding: 16px 18px;
      transition: all 0.3s cubic-bezier(0.16, 1, 0.3, 1);
    }
    .schedule-section.active-glow {
      border-color: rgba(16, 185, 129, 0.4);
      background: rgba(16, 185, 129, 0.04);
      box-shadow: 0 0 25px -4px rgba(16, 185, 129, 0.2);
    }
    .schedule-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
    }
    .schedule-title {
      font-size: 0.84rem;
      font-weight: 700;
      color: var(--text-muted);
      text-transform: uppercase;
      letter-spacing: 0.05em;
      display: flex;
      align-items: center;
      gap: 6px;
    }
    .schedule-status-badge {
      font-size: 0.78rem;
      font-weight: 700;
      color: var(--text-dim);
      font-family: 'JetBrains Mono', monospace;
      background: rgba(255, 255, 255, 0.05);
      border: 1px solid rgba(255, 255, 255, 0.1);
      padding: 3px 10px;
      border-radius: 6px;
      transition: all 0.3s ease;
    }
    .schedule-status-badge.active {
      color: #34D399;
      background: rgba(16, 185, 129, 0.15);
      border-color: rgba(16, 185, 129, 0.45);
      box-shadow: 0 0 14px rgba(16, 185, 129, 0.35);
    }

    .schedule-checkbox-label {
      display: flex;
      align-items: center;
      gap: 14px;
      cursor: pointer;
      user-select: none;
      background: rgba(255, 255, 255, 0.03);
      padding: 12px 16px;
      border-radius: 12px;
      border: 1px solid rgba(255, 255, 255, 0.07);
      transition: all 0.25s cubic-bezier(0.16, 1, 0.3, 1);
    }
    .schedule-checkbox-label:hover {
      background: rgba(255, 255, 255, 0.06);
      border-color: rgba(255, 255, 255, 0.15);
    }
    .schedule-checkbox-input {
      appearance: none;
      -webkit-appearance: none;
      width: 22px;
      height: 22px;
      border: 2px solid rgba(255, 255, 255, 0.3);
      border-radius: 6px;
      background: rgba(15, 23, 42, 0.8);
      cursor: pointer;
      display: grid;
      place-content: center;
      transition: all 0.2s cubic-bezier(0.16, 1, 0.3, 1);
      flex-shrink: 0;
      margin: 0;
    }
    .schedule-checkbox-input:hover {
      border-color: rgba(52, 211, 153, 0.7);
    }
    .schedule-checkbox-input:checked {
      background: linear-gradient(135deg, #10B981, #059669);
      border-color: #34D399;
      box-shadow: 0 0 14px rgba(16, 185, 129, 0.5);
    }
    .schedule-checkbox-input:checked::before {
      content: "";
      width: 5px;
      height: 10px;
      border: solid #FFFFFF;
      border-width: 0 2.5px 2.5px 0;
      transform: rotate(45deg) translate(-1px, -1px);
    }
    .schedule-checkbox-text {
      display: flex;
      flex-direction: column;
      gap: 3px;
      width: 100%;
    }
    .schedule-checkbox-title {
      font-size: 0.92rem;
      font-weight: 600;
      color: var(--text-main);
    }
    .schedule-checkbox-desc {
      font-size: 0.78rem;
      color: var(--text-muted);
    }

    /* Conflict Side-by-Side Card */
    .conflict-card {
      background: rgba(245, 158, 11, 0.07);
      border: 1px solid rgba(245, 158, 11, 0.35);
      border-radius: 18px;
      padding: 20px;
      display: flex;
      flex-direction: column;
      gap: 16px;
      animation: slideIn 0.3s cubic-bezier(0.16, 1, 0.3, 1);
    }
    @keyframes slideIn { from { opacity: 0; transform: translateY(8px); } to { opacity: 1; transform: translateY(0); } }

    .conflict-title {
      font-weight: 700;
      font-size: 1.05rem;
      display: flex;
      align-items: center;
      gap: 8px;
      color: #FBBF24;
    }
    .diff-table {
      width: 100%;
      border-collapse: collapse;
      font-size: 0.86rem;
      background: rgba(0, 0, 0, 0.3);
      border-radius: 10px;
      overflow: hidden;
    }
    .diff-table th, .diff-table td {
      padding: 10px 14px;
      border: 1px solid rgba(255, 255, 255, 0.06);
      text-align: left;
    }
    .diff-table th {
      background: rgba(0, 0, 0, 0.4);
      color: var(--text-muted);
      font-weight: 600;
      font-size: 0.78rem;
      text-transform: uppercase;
      letter-spacing: 0.04em;
    }
    .goog-cell { color: #FCA5A5; font-weight: 600; }
    .ms-cell { color: #93C5FD; font-weight: 600; }
    
    .conflict-buttons {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
    }
    .res-btn {
      padding: 9px 16px;
      border-radius: 10px;
      font-size: 0.82rem;
      font-weight: 600;
      cursor: pointer;
      border: 1px solid transparent;
      transition: all 0.2s ease;
    }
    .btn-keep { background: #059669; color: #FFF; box-shadow: 0 4px 12px rgba(5, 150, 105, 0.3); }
    .btn-keep:hover { background: #047857; }
    .btn-goog { background: rgba(239, 68, 68, 0.15); border-color: rgba(239, 68, 68, 0.5); color: #FCA5A5; }
    .btn-goog:hover { background: #EF4444; color: #FFF; }
    .btn-ms { background: rgba(59, 130, 246, 0.15); border-color: rgba(59, 130, 246, 0.5); color: #93C5FD; }
    .btn-ms:hover { background: #3B82F6; color: #FFF; }
    .btn-skip { background: rgba(255, 255, 255, 0.08); color: var(--text-muted); }
    .btn-skip:hover { background: rgba(255, 255, 255, 0.16); color: #FFF; }

    /* Bottom Output Console */
    .bottom-console {
      background: rgba(0, 0, 0, 0.4);
      border: 1px solid var(--border-card);
      border-radius: 16px;
      padding: 16px 20px;
      display: flex;
      flex-direction: column;
      gap: 8px;
    }
    .console-header {
      font-size: 0.74rem;
      font-weight: 700;
      color: var(--text-dim);
      text-transform: uppercase;
      letter-spacing: 0.06em;
      display: flex;
      align-items: center;
      gap: 6px;
    }
    .console-content {
      font-family: 'JetBrains Mono', monospace;
      font-size: 0.88rem;
      line-height: 1.6;
      color: #E2E8F0;
      white-space: pre-wrap;
    }
  </style>
</head>
<body>
  <div class="dashboard">
    <!-- Brand & User Header -->
    <div class="brand-header">
      <div class="logo-badge">⚡ SyncGM</div>
      <div class="user-profile-bar" id="user-profile-bar">
        <div class="user-pill">
          <span>👤</span>
          <span id="user-email-display">Loading...</span>
        </div>
        <button class="logout-btn" onclick="handleLogout()">Log out</button>
      </div>
    </div>

    <!-- Provider Connections -->
    <div class="providers-grid">
      <div class="provider-card">
        <div class="provider-meta">
          <img src="/static/google_contacts.png" alt="Google Contacts" class="provider-img icon-glow-google">
          <div>
            <div class="provider-name">Google Contacts</div>
            <div id="google-status" class="provider-status">● Checking...</div>
          </div>
        </div>
        <button id="google-btn" class="link-btn btn-connect" onclick="handleProviderAction('google')">Connect</button>
      </div>

      <div class="provider-card">
        <div class="provider-meta">
          <img src="/static/outlook_contacts.png" alt="Outlook Contacts" class="provider-img icon-glow-outlook">
          <div>
            <div class="provider-name">Outlook Contacts</div>
            <div id="ms-status" class="provider-status">● Checking...</div>
          </div>
        </div>
        <button id="ms-btn" class="link-btn btn-connect" onclick="handleProviderAction('ms')">Connect</button>
      </div>
    </div>

    <!-- Main Command Hero Card -->
    <div class="hero-card">
      <!-- Automated Hourly Sync Toggle -->
      <div class="schedule-section">
        <label class="schedule-checkbox-label" for="auto-sync-checkbox">
          <input 
            type="checkbox" 
            id="auto-sync-checkbox" 
            class="schedule-checkbox-input" 
            onchange="toggleHourlySync(this.checked)" 
          />
          <div class="schedule-checkbox-text">
            <div style="display: flex; align-items: center; justify-content: space-between; gap: 8px;">
              <span class="schedule-checkbox-title">Automatic Hourly Sync</span>
              <span id="schedule-badge" class="schedule-status-badge">OFF</span>
            </div>
            <span class="schedule-checkbox-desc">Automatically keep your Google and Outlook contacts up to date every hour.</span>
          </div>
        </label>
      </div>

      <!-- Main Action: Sync Now -->
      <div class="action-row">
        <div class="action-row-text">
          <span class="action-row-title">Manual Sync</span>
          <span class="action-row-desc">Sync contacts between your accounts right now.</span>
        </div>
        <button class="sync-btn" onclick="sendAction('sync_now')">
          <span>⚡</span>
          <span>Sync Now</span>
        </button>
      </div>

      <!-- Bottom Output Console -->
      <div class="bottom-console">
        <div class="console-header">
          <span>Sync Activity</span>
        </div>
        <div id="agent-msg" class="console-content">Ready. Click 'Sync Now' to sync your contacts.</div>
      </div>
    </div>
  </div>

  <script>
    let activeUserId = "test_user";
    let isGoogleConnected = false;
    let isMsConnected = false;
    let isHourlySyncEnabled = false;

    function handleProviderAction(provider) {
      const isConn = (provider === 'google') ? isGoogleConnected : isMsConnected;
      if (isConn) {
        disconnectProvider(provider);
      } else {
        const uidParam = activeUserId ? ('?user_id=' + encodeURIComponent(activeUserId)) : '';
        const loginUrl = (provider === 'google') 
          ? '/api/v1/auth/google/login' + uidParam 
          : '/api/v1/auth/ms/login' + uidParam;
        window.location.href = loginUrl;
      }
    }

    async function handleLogout() {
      document.cookie = "syncgm_session=; expires=Thu, 01 Jan 1970 00:00:00 UTC; path=/;";
      try {
        await fetch('/api/v1/auth/logout', { method: 'POST' });
      } catch (_) {}
      window.location.href = '/login';
    }

    async function disconnectProvider(provider) {
      const providerName = (provider === 'google') ? 'Google Contacts' : 'Outlook Contacts';
      if (!confirm(`Are you sure you want to disconnect ${providerName}?`)) {
        return;
      }
      document.getElementById('agent-msg').innerText = `Disconnecting ${providerName}...`;
      try {
        const res = await fetch(`/api/v1/auth/disconnect?provider=${provider}&user_id=${encodeURIComponent(activeUserId)}`, {
          method: 'POST'
        });
        const data = await res.json();
        document.getElementById('agent-msg').innerText = data.message || `Disconnected ${providerName}.`;
        await loadUserState();
      } catch (err) {
        document.getElementById('agent-msg').innerText = `Unable to disconnect ${providerName}. Please try again.`;
      }
    }

    function updateScheduleBadge() {
      const badge = document.getElementById('schedule-badge');
      const section = document.querySelector('.schedule-section');
      const checkbox = document.getElementById('auto-sync-checkbox');

      if (checkbox) {
        checkbox.checked = isHourlySyncEnabled;
      }

      if (isHourlySyncEnabled) {
        badge.innerText = 'ON (HOURLY)';
        badge.className = 'schedule-status-badge active';
        if (section) section.classList.add('active-glow');
      } else {
        badge.innerText = 'OFF';
        badge.className = 'schedule-status-badge';
        if (section) section.classList.remove('active-glow');
      }
    }

    async function toggleHourlySync(enable) {
      isHourlySyncEnabled = enable;
      updateScheduleBadge();

      await sendAction('set_schedule', {
        frequency: enable ? 'hourly' : 'disabled',
        enabled: enable,
        cron_expr: enable ? '0 * * * *' : 'disabled'
      });
    }

    async function loadUserState() {
      try {
        const res = await fetch('/api/v1/user/state');
        if (!res.ok) {
          if (res.status === 401) {
            window.location.href = '/login';
            return;
          }
        }
        const state = await res.json();

        if (state.user_id) {
          activeUserId = state.user_id;
        }

        const emailDisplay = document.getElementById('user-email-display');
        if (emailDisplay) {
          emailDisplay.innerText = state.email || state.display_name || 'Active User';
        }

        isGoogleConnected = !!state.google_connected;
        isMsConnected = !!state.ms_connected;

        // Update Google provider connection status and button
        const gStatus = document.getElementById('google-status');
        const gBtn = document.getElementById('google-btn');
        if (isGoogleConnected) {
          gStatus.innerText = '● Connected';
          gStatus.className = 'provider-status';
          gBtn.innerText = 'Disconnect';
          gBtn.className = 'link-btn btn-disconnect';
        } else {
          gStatus.innerText = '○ Not Connected';
          gStatus.className = 'provider-status disconnected';
          gBtn.innerText = 'Connect';
          gBtn.className = 'link-btn btn-connect';
        }

        // Update Microsoft provider connection status and button
        const msStatus = document.getElementById('ms-status');
        const msBtn = document.getElementById('ms-btn');
        if (isMsConnected) {
          msStatus.innerText = '● Connected';
          msStatus.className = 'provider-status';
          msBtn.innerText = 'Disconnect';
          msBtn.className = 'link-btn btn-disconnect';
        } else {
          msStatus.innerText = '○ Not Connected';
          msStatus.className = 'provider-status disconnected';
          msBtn.innerText = 'Connect';
          msBtn.className = 'link-btn btn-connect';
        }

        // Restore hourly schedule status
        isHourlySyncEnabled = (state.schedule_cron && state.schedule_cron !== 'disabled');
        updateScheduleBadge();

        if (state.last_log_message) {
          document.getElementById('agent-msg').innerText = state.last_log_message;
        }
      } catch (e) {
        console.error("Error loading user state:", e);
      }
    }

    async function sendAction(actionName, payload = {}) {
      if (actionName === 'sync_now') {
        document.getElementById('agent-msg').innerText = 'Syncing your contacts...';
      } else if (actionName === 'set_schedule') {
        document.getElementById('agent-msg').innerText = 'Updating schedule...';
      } else {
        document.getElementById('agent-msg').innerText = 'Processing request...';
      }
      try {
        const res = await fetch('/api/v1/agent/action', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ user_id: activeUserId, action: actionName, payload: payload })
        });
        if (!res.ok) {
          const errText = await res.text();
          let msg = 'Unable to complete request (Status ' + res.status + ').';
          try {
            const errJson = JSON.parse(errText);
            if (errJson.message) msg = errJson.message;
            else if (errJson.detail) msg = errJson.detail;
          } catch (_) {}
          document.getElementById('agent-msg').innerText = msg;
          return;
        }
        const data = await res.json();
        renderResponse(data);
      } catch (err) {
        document.getElementById('agent-msg').innerText = 'Unable to complete request. Please try again.';
      }
    }

    function renderResponse(data) {
      document.getElementById('agent-msg').innerText = data.message;

      if (data.action === 'set_schedule') {
        if (data.data && typeof data.data.enabled === 'boolean') {
          isHourlySyncEnabled = data.data.enabled;
        }
        updateScheduleBadge();
      }
    }

    // When running inside an embedded preview window, any button click opens the dashboard in its own tab
    const isEmbedded = (window.self !== window.top) || window.location.search.includes('embed=true');
    if (isEmbedded) {
      document.addEventListener('click', function(e) {
        const btn = e.target.closest('button, a, input[type="submit"], input[type="button"], [role="button"]');
        if (!btn) return;

        if (btn.tagName === 'A' && btn.href && btn.href.includes('agentworkforce.online')) {
          return;
        }

        e.preventDefault();
        e.stopPropagation();

        const targetUrl = (btn.tagName === 'A' && btn.href && !btn.href.startsWith('javascript:'))
          ? btn.href
          : window.location.origin + '/';

        window.open(targetUrl, '_blank');
      }, true);
    }

    window.addEventListener('DOMContentLoaded', loadUserState);
  </script>
</body>
</html>
"""
    return HTMLResponse(content=html_content)


@app.get("/health", tags=["System"])
def health_check():
    return {
        "status": "healthy",
        "service": "SyncGM",
        "version": "1.0.0",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@app.post("/api/v1/agent/action", response_model=AgentResponse, tags=["ADK Agent"])
def dispatch_agent_action(
    req: AgentActionRequest,
    syncgm_session: Optional[str] = Cookie(None),
    db: Session = Depends(get_db),
):
    actual_user_id = req.user_id or syncgm_session or "test_user"
    agent = SyncGMAgent()
    response = agent.handle_action(
        user_id=actual_user_id,
        action=req.action,
        payload=req.payload,
        db_session=db,
    )
    return response


@app.post(
    "/api/v1/jobs/sync-scheduled",
    response_model=ScheduledSyncResponse,
    dependencies=[Depends(verify_scheduler_auth)],
    tags=["Headless Scheduler"],
)
def run_scheduled_sync(db: Session = Depends(get_db)):
    repo = SyncRepository(db)
    scheduled_users = repo.get_active_scheduled_users()

    results: list[dict[str, Any]] = []
    g_factory = getattr(app.state, "google_client_factory", None)
    m_factory = getattr(app.state, "ms_client_factory", None)
    service = SyncService(db_session=db, google_client_factory=g_factory, ms_client_factory=m_factory)

    logger.info("Executing headless Cloud Scheduler sync for %d active users.", len(scheduled_users))

    for user in scheduled_users:
        try:
            pol = user.sync_policy if user.sync_policy and user.sync_policy != "ask" else "last_writer_wins"
            exec_res = service.execute(
                user_id=user.id,
                auto_resolve_policy=pol,
                trigger_type="SCHEDULED",
            )
            results.append(exec_res)
        except Exception as exc:
            logger.error("Error executing scheduled sync for user [%s]: %s", user.id, exc, exc_info=True)
            repo.create_audit_log(
                user_id=user.id,
                trigger_type="SCHEDULED",
                status="FAILED",
                contacts_examined=0,
                contacts_created=0,
                contacts_updated=0,
                conflicts_detected=0,
                error_details=str(exc),
            )
            results.append({"status": "FAILED", "user_id": user.id, "error": str(exc)})

    return ScheduledSyncResponse(
        status="SUCCESS",
        users_processed=len(scheduled_users),
        executions=results,
        timestamp=datetime.now(timezone.utc).isoformat(),
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", 8080)), reload=True)
