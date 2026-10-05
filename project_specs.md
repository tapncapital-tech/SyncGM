# Project Specs: Bi-Directional Contact Synchronization Engine (Google & Microsoft)

## 1. Project Overview & Core Objectives
Build a production-grade, bi-directional contact synchronization application between **Google Contacts** (Google People API) and **Microsoft Contacts** (Microsoft Graph API). 

The system operates across two distinct execution modes:
1. **Interactive Guided Mode:** A constrained, button-driven conversational agent interface built with **Google ADK** (Agent Development Kit). Users do not type open-ended text; they trigger workflows, preview diffs, and resolve ambiguous conflicts using guided action buttons and option chips.
2. **Automated Scheduled Mode:** A deterministic background task running on user-defined schedules (e.g., hourly, daily) without requiring an active agent session.

---

## 2. Architectural Blueprint & Decoupling

To ensure reliability, security, and zero LLM hallucination in data writes, the system strictly separates agentic orchestration from data synchronization logic:

```
                  +----------------------------------------------+
                  |           Client / Guided Interface          |
                  |     (Button Choices & Action Prompts)        |
                  +----------------------+-----------------------+
                                         |
                                         v
+--------------------------------------------------------------------------------+
| Google ADK Agent Layer (Orchestration & Human-in-the-Loop)                      |
| - Guides user through setup, preview, and conflict resolution                  |
| - Presents human-readable dry runs and diff summaries                          |
| - Translates button clicks into structured internal engine tool calls          |
+----------------------------------------+---------------------------------------+
                                         |
                                         v
+--------------------------------------------------------------------------------+
| Deterministic Python Sync Engine (Core Business Logic)                         |
| - Multi-field identity matching with weighted confidence scoring               |
| - 3-Way differential merge (Base State vs Google State vs Microsoft State)     |
| - Field-level conflict isolation and safe fallback policies                    |
| - Idempotent write execution and transaction rollback safety                  |
+-------------------+------------------------------------+-----------------------+
                    |                                    |
                    v                                    v
+------------------------------------+ +-----------------------------------------+
| External API Clients (OAuth 2.0)   | | Persistent Relational Store (Cloud SQL) |
| - Google People API v1             | | - Identity Mappings (Google ID <-> MS)  |
| - Microsoft Graph API v1.0         | | - Last-Known Snapshots & Field Hashes   |
| - Rate limiting & token refresh    | | - Conflict Review Queue & Audit Logs    |
+------------------------------------+ +-----------------------------------------+
```

### Architectural Principles
* **Agent Isolation:** The ADK Agent never calls external Google or Microsoft APIs directly. It only interacts with internal engine tools.
* **Deterministic Synchronization:** Merging, matching, and write decisions are handled strictly by Python algorithms, not by LLM inference.
* **Autonomous Background Operation:** The scheduler executes directly against the Sync Engine and logs unresolved conflicts to the database for future user review.

---

## 3. Interaction Model & Guided UI Flow

The interface avoids freeform chat text entry to prevent ambiguity and errors. The user interacts via defined action buttons, selection chips, and structured comparison cards.

### 3.1. Main Menu / Welcome Prompt
On application launch or session start:
> **Agent:** "Hello John Doe. Your Google Contacts and Microsoft Contacts are linked. How would you like to proceed?"  
> • `[Sync Now]`  
> • `[Preview Changes (Dry Run)]`  
> • `[Compare Contacts]`  
> • `[Configure Schedule]`  
> • `[View Unresolved Conflicts]`

---

### 3.2. Interactive Conflict Resolution Flow
When field discrepancies cannot be safely auto-merged, the engine pauses modifications for that specific contact, flags the record, and prompts the user with side-by-side choices:

> **Agent:**  
> "I found a conflict for **John Smith**. Both providers have different primary phone numbers:  
> • **Google:** `(555) 123-4567`  
> • **Microsoft:** `(555) 987-6543`  
>  
> How would you like to resolve this record?"  
> • `[Keep Both (Store one as Secondary)]`  
> • `[Use Google Info]`  
> • `[Use Microsoft Info]`  
> • `[Skip / Leave Unchanged]`

* **Global Conflict Preference:** The user can configure a global policy: `Ask Every Time (Default)`, `Google Wins`, `Microsoft Wins`, or `Newest Update Wins`.
* **Unresponsive Handling:** If a user closes the interface or does not make a selection, the conflicting contact remains unchanged in both accounts with status `CONFLICT_PENDING`. Non-conflicting contacts continue to sync normally.

---

## 4. Contact Matching & Differential Merge Engine

### 4.1. Identity Matching Algorithm
Contacts from both providers are evaluated across multiple fields to determine if they represent the same entity. Matches are assigned a confidence score from $0$ to $100$:

| Evaluated Field | Match Condition | Confidence Score |
| :--- | :--- | :--- |
| **Database Mapping** | Existing verified mapping in `contact_mappings` | $100$ (Instant Match) |
| **Primary Email** | Exact case-insensitive match on normalized email | $90$ |
| **Phone Number** | Exact match on E.164 normalized phone number | $70$ |
| **Name + Organization** | Exact match on Full Name AND Company Name | $60$ |
| **Full Name Only** | Exact match on First + Last Name | $30$ |

* **Score $\ge 80$:** Automatic link; evaluated for updates.
* **Score $50 - 79$:** Flagged as **Potential Duplicate**; requires user confirmation before linking.
* **Score $< 50$:** Treated as separate, unique contacts.

---

### 4.2. 3-Way Differential Merge Logic
The sync engine maintains the last-known synchronized snapshot ($S_{\text{base}}$). During a sync run, it compares the current Google state ($S_{\text{goog}}$) and current Microsoft state ($S_{\text{ms}}$) against $S_{\text{base}}$:

1. **Non-Conflicting New Additions:**
   * Record exists in $S_{\text{goog}}$ but absent in $S_{\text{ms}}$ and $S_{\text{base}}$ $\rightarrow$ Create in Microsoft.
   * Record exists in $S_{\text{ms}}$ but absent in $S_{\text{goog}}$ and $S_{\text{base}}$ $\rightarrow$ Create in Google.
2. **Safe Field Additions & Updates:**
   * Field modified in $S_{\text{goog}}$ and untouched in $S_{\text{ms}}$ relative to $S_{\text{base}}$ $\rightarrow$ Propagate change to Microsoft.
   * Field modified in $S_{\text{ms}}$ and untouched in $S_{\text{goog}}$ relative to $S_{\text{base}}$ $\rightarrow$ Propagate change to Google.
   * New non-overlapping field (e.g., Job Title present in Microsoft, empty in Google) $\rightarrow$ Merge non-destructively into Google.
3. **Hard Conflicts:**
   * The same field has been modified in both providers to different values since $S_{\text{base}}$.
   * **Action:** Isolate the contact, log to `sync_conflicts`, skip write operations on this record, and prompt user for resolution.
4. **Deletion Safety Policy:**
   * Record present in $S_{\text{base}}$ but missing in $S_{\text{goog}}$ is **NOT** automatically deleted from Microsoft by default.
   * Automatic deletion requires explicit user opt-in (`allow_deletions = TRUE`). Otherwise, the engine flags the record as `ORPHANED_IN_PROVIDER` and leaves it untouched.

---

## 5. Core Workflows

### Workflow A: Manual Synchronization
1. User clicks `[Sync Now]`.
2. Engine authenticates using stored tokens and pulls contact collections from both APIs.
3. Matching algorithm pairs contacts and calculates deltas against database state.
4. Safe additions and non-conflicting field updates are executed.
5. Conflicting items are held and presented in the guided conflict resolution flow.
6. Engine records the new state snapshots and displays a summary report to the user.

### Workflow B: Dry Run / Preview Changes
1. User clicks `[Preview Changes (Dry Run)]`.
2. Engine retrieves contacts and computes matching and differences without committing any API writes.
3. Engine outputs a breakdown:
   * Contacts to create in Google and Microsoft.
   * Fields to update in Google and Microsoft.
   * Detected conflicts requiring resolution.
4. User is presented with: `[Apply All Safe Changes]` or `[Cancel]`.

### Workflow C: Contact Comparison
1. User clicks `[Compare Contacts]`.
2. Engine inspects both providers and categorizes the current state:
   * Google-only contacts.
   * Microsoft-only contacts.
   * Fully synchronized matching contacts.
   * Contacts with field differences.
3. Displays a human-readable comparison without changing any data.

### Workflow D: Scheduled Unattended Synchronization
1. Google Cloud Scheduler triggers the synchronization worker endpoint at the configured interval (e.g., daily at 06:00 UTC).
2. Engine executes the 3-way differential merge independently of any user session.
3. Safe additions and updates are synchronized automatically.
4. Ambiguous conflicts are saved to the database with status `PENDING_REVIEW`.
5. An audit log entry is recorded, and an alert is flagged for the user's next login.

---

## 6. Internal ADK Tool Specifications

The Google ADK Agent interacts with the backend using the following structured Python tool interfaces:

```python
def preview_sync(user_id: str) -> dict:
    """Calculates proposed synchronization changes without writing to APIs.
    
    Returns:
        dict: Counts and previews of additions, updates, and conflicts.
    """
    pass

def execute_sync(user_id: str, auto_resolve_policy: str = "ask") -> dict:
    """Executes bi-directional sync for safe records and holds conflicts.
    
    Args:
        user_id: Unique account identifier.
        auto_resolve_policy: 'ask' | 'google_wins' | 'microsoft_wins' | 'newest_wins'
    Returns:
        dict: Execution metrics (created, updated, unchanged, conflicts).
    """
    pass

def get_pending_conflicts(user_id: str) -> list[dict]:
    """Retrieves all unresolved conflicts for the user.
    
    Returns:
        list[dict]: Array of conflict items with provider values and available options.
    """
    pass

def resolve_conflict_item(user_id: str, conflict_id: str, resolution_choice: str) -> dict:
    """Applies user decision to a specific conflict record.
    
    Args:
        resolution_choice: 'keep_both' | 'use_google' | 'use_microsoft' | 'ignore'
    """
    pass

def configure_schedule(user_id: str, frequency: str, enabled: bool) -> dict:
    """Updates background sync schedule settings.
    
    Args:
        frequency: 'hourly' | 'every_6_hours' | 'daily' | 'weekly'
        enabled: Boolean flag to enable or disable automatic runs.
    """
    pass

def get_sync_audit_report(user_id: str, limit: int = 5) -> list[dict]:
    """Retrieves recent synchronization run logs and status details."""
    pass
```

---

## 7. Data Storage & Schema Design

Persistent state is stored in a relational PostgreSQL database (Google Cloud SQL) to guarantee transactional integrity.

### Database Tables

#### `user_accounts`
* `id` (UUID, Primary Key)
* `email` (VARCHAR, Unique)
* `google_refresh_token_enc` (BYTEA, Encrypted at rest)
* `ms_refresh_token_enc` (BYTEA, Encrypted at rest)
* `sync_policy` (VARCHAR, default `'ask'`)
* `allow_deletions` (BOOLEAN, default `FALSE`)
* `schedule_cron` (VARCHAR, Nullable)
* `created_at` (TIMESTAMP)

#### `contact_mappings`
* `id` (UUID, Primary Key)
* `user_id` (UUID, Foreign Key $\rightarrow$ `user_accounts.id`)
* `google_resource_id` (VARCHAR, Indexed)
* `ms_contact_id` (VARCHAR, Indexed)
* `last_synced_hash` (VARCHAR: SHA-256 hash of normalized contact fields)
* `last_synced_at` (TIMESTAMP)

#### `sync_conflicts`
* `id` (UUID, Primary Key)
* `user_id` (UUID, Foreign Key $\rightarrow$ `user_accounts.id`)
* `mapping_id` (UUID, Foreign Key $\rightarrow$ `contact_mappings.id`, Nullable)
* `contact_name` (VARCHAR)
* `conflict_data` (JSONB: stores conflicting field payloads from both sides)
* `status` (VARCHAR: `'PENDING'`, `'RESOLVED'`, `'IGNORED'`)
* `created_at` (TIMESTAMP)

#### `sync_audit_logs`
* `id` (UUID, Primary Key)
* `user_id` (UUID, Foreign Key $\rightarrow$ `user_accounts.id`)
* `trigger_type` (VARCHAR: `'MANUAL'` | `'SCHEDULED'`)
* `status` (VARCHAR: `'SUCCESS'` | `'PARTIAL_SUCCESS'` | `'FAILED'`)
* `contacts_examined` (INT)
* `contacts_created` (INT)
* `contacts_updated` (INT)
* `conflicts_detected` (INT)
* `error_details` (TEXT, Nullable)
* `executed_at` (TIMESTAMP)

---

## 8. External API Integration Details

### 8.1. Google Contacts (Google People API v1)
* **Required Scope:** `https://www.googleapis.com/auth/contacts`
* **Key Endpoints:**
  * `GET /v1/people/me/connections`: Retrieve contacts (`personFields=names,emailAddresses,phoneNumbers,organizations,addresses,metadata`).
  * `POST /v1/people:createContact`: Create synchronized contacts.
  * `PATCH /v1/people/{resourceName}:updateContact`: Update contacts (requires `updatePersonFields` and current `etag`).
  * `DELETE /v1/people/{resourceName}:deleteContact`: Remove contacts (only when deletion sync is explicitly enabled).

### 8.2. Microsoft Contacts (Microsoft Graph API v1.0)
* **Required Scope:** `Contacts.ReadWrite`
* **Key Endpoints:**
  * `GET /me/contacts`: Retrieve contacts (`$select=id,displayName,givenName,surname,emailAddresses,mobilePhone,businessPhones,companyName,postalAddresses`).
  * `POST /me/contacts`: Create new contact records.
  * `PATCH /me/contacts/{id}`: Update specific modified fields.
  * `DELETE /me/contacts/{id}`: Remove contact records (only when deletion sync is explicitly enabled).

---

## 9. Security, Authentication & Deployment

### Security Standards
* **Credential Storage:** API client credentials and OAuth secrets reside in **Google Secret Manager**. User OAuth refresh tokens are encrypted at rest with AES-GCM-256 before database insertion.
* **Scope Minimization:** Restrict OAuth grants strictly to contact read/write operations. Never request mailbox or full drive permissions.
* **PII Protection:** Never write raw phone numbers, emails, or street addresses to plain logs. Use internal record UUIDs and aggregate counts for logging.

### Google Cloud Infrastructure
* **Hosting Platform:** **Google Cloud Run** running a containerized Python service (FastAPI) hosting the ADK agent endpoints and sync workers.
* **Scheduling:** **Google Cloud Scheduler** dispatching authenticated OIDC requests to `/api/v1/jobs/sync-scheduled`.
* **Database:** **Google Cloud SQL (PostgreSQL)** connected via Unix socket / Cloud SQL Auth Proxy.

---

## 10. Definition of Done (MVP Criteria)

The MVP is complete and ready for deployment when all acceptance criteria pass:

1. **Authentication:** User connects both Google and Microsoft accounts through standard OAuth 2.0 flows without token exposure.
2. **Matching Accuracy:** Deterministic engine correctly matches existing records across both providers using email and phone normalization.
3. **Dry Run Functionality:** User can request a preview and receive a structured diff table of proposed changes with zero API write executions.
4. **Clean Two-Way Sync:** Safe additions and updates transfer across both providers without creating duplicate records or infinite sync loops.
5. **Interactive Conflict Resolution:** When conflicting field data is introduced, the system pauses that record, surfaces the side-by-side button prompt, and writes the user's selected resolution accurately.
6. **Unattended Execution:** Cloud Scheduler successfully executes a background sync; non-conflicting changes sync, conflicts are logged to the database, and an auditable run log is created.