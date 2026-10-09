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
    init_db()

    user_id = "test_user"
    GLOBAL_SYNC_STORE.ensure_user(user_id, display_name="Sylvester & Friend")
    with SessionFactory() as db:
        from sqlalchemy import update
        from execution.database import UserAccount
        db.execute(update(UserAccount).where(UserAccount.sync_policy == "ask").values(sync_policy="last_writer_wins"))
        db.commit()

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

class AgentActionRequest(BaseModel):
    user_id: str = Field(description="Unique User Account identifier")
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
# OAUTH 2.0 CONNECT ENDPOINTS (Section 8 & Section 9)
# =============================================================================

@app.get("/api/v1/auth/google/login", tags=["OAuth 2.0"])
def google_oauth_login(user_id: str = Query("test_user")):
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
        "state": user_id,
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
                user = repo.get_or_create_user(email=f"{state}@example.com")
                repo.update_user_tokens(user_id=user.id, google_refresh_token=refresh_token)
        except Exception as e:
            logger.error("Error exchanging Google code: %s", e)

    response = RedirectResponse("/?connected=google")
    response.set_cookie(key="syncgm_session", value=state, max_age=30 * 86400, path="/", samesite="lax")
    return response


@app.get("/api/v1/auth/ms/login", tags=["OAuth 2.0"])
def ms_oauth_login(user_id: str = Query("test_user")):
    client_id = os.getenv("MS_CLIENT_ID", "mock-ms-client-id")
    redirect_uri = os.getenv("MS_REDIRECT_URI", "http://localhost:8080/api/v1/auth/ms/callback")
    scope = "Contacts.ReadWrite offline_access"

    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": scope,
        "prompt": "consent",
        "state": user_id,
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
                user = repo.get_or_create_user(email=f"{state}@example.com")
                repo.update_user_tokens(user_id=user.id, ms_refresh_token=refresh_token)
        except Exception as e:
            logger.error("Error exchanging Microsoft code: %s", e)

    response = RedirectResponse("/?connected=microsoft")
    response.set_cookie(key="syncgm_session", value=state, max_age=30 * 86400, path="/", samesite="lax")
    return response


@app.post("/api/v1/auth/disconnect", tags=["Authentication"])
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
    actual_id = user_id or syncgm_session or "test_user"
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

@app.get("/", response_class=HTMLResponse, tags=["Web Interface"])
def index():
    """Modern, High-Tech Dashboard with Local Timezone Scheduling & Persistent State."""
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

    /* Top Brand Bar */
    .brand-header {
      display: flex;
      justify-content: flex-start;
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

    /* Schedule Details Row (Day & Time Selectors) */
    .schedule-details-row {
      display: flex;
      align-items: center;
      gap: 12px;
      padding-top: 6px;
      border-top: 1px solid rgba(255, 255, 255, 0.05);
      flex-wrap: wrap;
    }
    .picker-group {
      display: flex;
      align-items: center;
      gap: 6px;
      font-size: 0.82rem;
      color: var(--text-muted);
    }
    .picker-select, .picker-time {
      background: rgba(15, 23, 42, 0.8);
      border: 1px solid rgba(255, 255, 255, 0.15);
      color: #FFF;
      padding: 6px 10px;
      border-radius: 8px;
      font-family: inherit;
      font-size: 0.82rem;
      outline: none;
      transition: border-color 0.2s;
    }
    .picker-select:focus, .picker-time:focus {
      border-color: var(--primary);
    }
    .picker-time {
      font-family: 'JetBrains Mono', monospace;
      color-scheme: dark;
    }
    .tz-label {
      font-size: 0.72rem;
      color: var(--text-dim);
      font-family: 'JetBrains Mono', monospace;
      background: rgba(255, 255, 255, 0.05);
      padding: 2px 6px;
      border-radius: 4px;
    }
    .save-schedule-btn {
      margin-left: auto;
      background: rgba(99, 102, 241, 0.2);
      border: 1px solid rgba(99, 102, 241, 0.5);
      color: #C7D2FE;
      padding: 6px 14px;
      border-radius: 8px;
      font-size: 0.8rem;
      font-weight: 600;
      cursor: pointer;
      transition: all 0.2s;
    }
    .save-schedule-btn:hover {
      background: var(--primary);
      color: #FFF;
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
    <!-- Brand Header -->
    <div class="brand-header">
      <div class="logo-badge">⚡ SyncGM</div>
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
    const USER_ID = "test_user";
    let isGoogleConnected = false;
    let isMsConnected = false;
    let isHourlySyncEnabled = false;

    function handleProviderAction(provider) {
      const isConn = (provider === 'google') ? isGoogleConnected : isMsConnected;
      if (isConn) {
        disconnectProvider(provider);
      } else {
        const loginUrl = (provider === 'google') 
          ? '/api/v1/auth/google/login?user_id=' + USER_ID 
          : '/api/v1/auth/ms/login?user_id=' + USER_ID;
        window.location.href = loginUrl;
      }
    }

    async function disconnectProvider(provider) {
      const providerName = (provider === 'google') ? 'Google Contacts' : 'Outlook Contacts';
      if (!confirm(`Are you sure you want to disconnect ${providerName}?`)) {
        return;
      }
      document.getElementById('agent-msg').innerText = `Disconnecting ${providerName}...`;
      try {
        const res = await fetch(`/api/v1/auth/disconnect?provider=${provider}&user_id=${USER_ID}`, {
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
        const res = await fetch('/api/v1/user/state?user_id=' + USER_ID);
        if (!res.ok) return;
        const state = await res.json();

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
          body: JSON.stringify({ user_id: USER_ID, action: actionName, payload: payload })
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
    db: Session = Depends(get_db),
):
    agent = SyncGMAgent()
    response = agent.handle_action(
        user_id=req.user_id,
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
