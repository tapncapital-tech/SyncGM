# Phase 1: Data Contracts & API Adapters

## 1. Overview
Phase 1 establishes the foundational data layer and external API adapters for bidirectional contact synchronization between **Google People API v1** and **Microsoft Graph API v1.0**.

All synchronization logic downstream relies on the decoupled data contracts defined here.

---

## 2. Core Components

### 2.1. Unified Contact Data Contract (`execution/models.py`)
- **Pydantic v2 Models:**
  - `NameDetails`: Given name, family name, middle name, display name, prefix, suffix.
  - `EmailAddress`: Normalized email address, type/label (`work`, `home`, `other`), primary flag.
  - `PhoneNumber`: Normalized phone number (E.164 compatible), type/label (`mobile`, `work`, `home`), primary flag.
  - `PostalAddress`: Street, city, state, postal code, country, address type.
  - `OrganizationDetails`: Company name, department, title/job role.
  - `UnifiedContact`: Canonical aggregate root supporting bidirectional translation, source ID tracking (`google_resource_name`, `ms_id`), and etag version concurrency control.

### 2.2. Error Handling & Resilience (`execution/exceptions.py`)
- `SyncAPIError`: Base exception for sync adapter failures.
- `RateLimitError`: Raised on HTTP 429 with `retry_after_seconds`.
- `TokenExpiredError`: Raised on HTTP 401 when token refresh fails or is unconfigured.
- `AuthenticationError`: Raised on credential / permission failures.
- `ContactNotFoundError`: Raised on HTTP 404.

### 2.3. Google Contacts Client (`execution/google_client.py`)
- Direct integration with Google People API v1 endpoints:
  - `GET /v1/people/me/connections` (Pagination with `pageToken`)
  - `POST /v1/people:createContact`
  - `PATCH /v1/people/{resourceName}:updateContact` (with `updatePersonFields` & etag)
  - `DELETE /v1/people/{resourceName}:deleteContact`
- Automatic retry with exponential backoff on 429 / 503.
- Automatic token refresh on 401 using stored refresh credentials and callback notification.

### 2.4. Microsoft Contacts Client (`execution/ms_client.py`)
- Direct integration with Microsoft Graph API v1.0 endpoints:
  - `GET /me/contacts` (Pagination with `@odata.nextLink`)
  - `POST /me/contacts`
  - `PATCH /me/contacts/{id}`
  - `DELETE /me/contacts/{id}`
- Automatic `Retry-After` header parsing on 429.
- Automatic token refresh on 401 using OAuth 2.0 token endpoint.

---

## 3. Verification & Test Suite
- Automated test suite in `tests/test_adapters.py` testing:
  - Google payload parsing to `UnifiedContact` and back to Google payload.
  - Microsoft payload parsing to `UnifiedContact` and back to Microsoft payload.
  - Mocked HTTP client behavior for CRUD operations, pagination, rate-limit retry, and 401 token refresh.
