# Phase 3: Google ADK Agent & Guided Action UI

## 1. Overview
Phase 3 builds the orchestration layer using **Google ADK (Agent Development Kit)** design principles. The agent operates strictly as a constrained, button-driven conversational orchestrator.

The agent translates user action button clicks into internal engine tool invocations and renders structured side-by-side comparison and conflict resolution cards.

---

## 2. Core Architecture & Constraints

```
+-------------------------------------------------------------------------+
|                        Client / Guided Interface                        |
|   (Renders Action Chips, Comparison Tables, Side-by-Side Diff Cards)   |
+------------------------------------+------------------------------------+
                                     | Structured Action Events
                                     v
+-------------------------------------------------------------------------+
|                  SyncGMAgent (ADK Orchestration Layer)                  |
| - Manages dialog state machine & guided user journey                    |
| - Formats human-readable summaries and side-by-side diff cards          |
| - Dispatches actions strictly to internal engine tools                  |
| - ZERO direct API access (Never calls Google/Microsoft APIs directly)  |
+------------------------------------+------------------------------------+
                                     | Tool Invocations
                                     v
+-------------------------------------------------------------------------+
|                         ADK Tools (execution/tools.py)                  |
| - preview_sync(user_id)                                                 |
| - execute_sync(user_id, auto_resolve_policy)                            |
| - get_pending_conflicts(user_id)                                        |
| - resolve_conflict_item(user_id, conflict_id, resolution_choice)        |
| - configure_schedule(user_id, frequency, enabled)                       |
| - compare_contacts(user_id)                                             |
| - get_sync_audit_report(user_id, limit)                                 |
+-------------------------------------------------------------------------+
```

---

## 3. Tool Specifications (`execution/tools.py`)
- Standardized tool schemas with complete type hints and JSON-serializable outputs.
- Backed by an in-memory repository for Phase 3 testing to decouple from physical databases.

---

## 4. Agent State Machine & UI Flows (`execution/agent.py`)
- **Main Menu**: Greeting and action chips (`[Sync Now]`, `[Preview Changes (Dry Run)]`, `[Compare Contacts]`, `[Configure Schedule]`, `[View Unresolved Conflicts]`).
- **Preview Flow**: Shows counts of proposed additions/updates and prompts `[Apply All Safe Changes]` or `[Cancel]`.
- **Conflict Flow**: Renders side-by-side diff cards displaying conflicting fields (Google vs Microsoft) with action chips (`[Keep Both (Store one as Secondary)]`, `[Use Google Info]`, `[Use Microsoft Info]`, `[Skip / Leave Unchanged]`).
- **Schedule Flow**: Configures unattended schedule frequency (`hourly`, `every_6_hours`, `daily`, `weekly`).

---

## 5. Verification & Test Flow
- Automated simulation in `tests/test_agent_flow.py` covering:
  - Main menu generation.
  - Preview dry-run with detected conflict.
  - Conflict card presentation.
  - Resolution button trigger (`keep_both`, `use_google`, `use_microsoft`, `skip`).
  - Tool execution and resolution verification.
