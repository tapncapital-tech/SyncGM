# Phase 4: Database Persistence, Scheduler Endpoint & Cloud Packaging

## 1. Overview
Phase 4 provides end-to-end production persistence, headless scheduled synchronization, live ADK tool database integration, and Google Cloud Run deployment packaging.

---

## 2. Core Modules

### 2.1. Token Cryptography (`execution/crypto.py`)
- Standard AES-GCM-256 authenticated encryption.
- Encrypts OAuth 2.0 refresh tokens with random 12-byte nonces before storage in `user_accounts`.
- Enforces tag authentication on decryption.

### 2.2. Relational Schema & ORM (`execution/database.py`)
- SQLAlchemy 2.0 models for PostgreSQL (Cloud SQL) & SQLite:
  - `UserAccount`: Account metadata, encrypted tokens, sync policy, scheduler cron.
  - `ContactMapping`: Bidirectional ID pairings (`google_resource_id` $\leftrightarrow$ `ms_contact_id`) with SHA-256 hashes.
  - `SyncConflict`: Conflict isolation queue with JSON payload and lifecycle status (`PENDING`, `RESOLVED`, `IGNORED`).
  - `SyncAuditLog`: Immutable audit trail for manual and scheduled sync executions.

### 2.3. Repository & Live Service (`execution/repository.py`, `execution/sync_service.py`)
- Atomic transactional operations for mapping updates and conflict reviews.
- `SyncService` coordinates live API fetching, base state reconstruction, diff engine execution, idempotent writes, and PII-redacted logging.

### 2.4. Headless Scheduler & Web Service (`main.py`)
- FastAPI endpoints:
  - `GET /health`: Health and readiness probe.
  - `POST /api/v1/agent/action`: Guided ADK Agent interaction endpoint.
  - `POST /api/v1/jobs/sync-scheduled`: Headless worker triggered by Google Cloud Scheduler (validates OIDC bearer tokens).

### 2.5. Cloud Packaging & Containerization
- Multi-stage `Dockerfile` with non-root security context for Google Cloud Run.
- `docker-compose.yml` for local multi-container development (FastAPI + PostgreSQL).
- `cloudbuild.yaml` and `cloud_run_service.yaml` deployment descriptors.
