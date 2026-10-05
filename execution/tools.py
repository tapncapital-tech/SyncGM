"""ADK Tool Declarations backed by Database Persistence & In-Memory Store."""
from __future__ import annotations

from datetime import datetime, timezone
import logging
from typing import Any, Optional
import uuid
from sqlalchemy.orm import Session

from execution.database import SessionFactory
from execution.diff_engine import SyncPlanner
from execution.matcher import ContactMatcher
from execution.models import (
    ConflictChoice,
    ConflictItem,
    MatchingResult,
    PhoneNumber,
    SyncPlan,
    UnifiedContact,
)
from execution.repository import SyncRepository
from execution.sync_service import SyncService

logger = logging.getLogger(__name__)


class InMemorySyncStore:
    """In-memory data store for fallback state isolation during agent interaction & testing."""

    def __init__(self) -> None:
        self.user_profiles: dict[str, dict[str, Any]] = {}
        self.google_contacts: dict[str, list[UnifiedContact]] = {}
        self.ms_contacts: dict[str, list[UnifiedContact]] = {}
        self.base_contacts: dict[str, dict[str, UnifiedContact]] = {}
        self.stored_mappings: dict[str, dict[str, str]] = {}
        self.conflicts: dict[str, dict[str, ConflictItem]] = {}
        self.schedule_config: dict[str, dict[str, Any]] = {}
        self.audit_logs: dict[str, list[dict[str, Any]]] = {}

    def ensure_user(self, user_id: str, display_name: str = "John Doe") -> None:
        if user_id not in self.user_profiles:
            self.user_profiles[user_id] = {"id": user_id, "name": display_name, "email": f"{user_id}@example.com"}
            self.google_contacts[user_id] = []
            self.ms_contacts[user_id] = []
            self.base_contacts[user_id] = {}
            self.stored_mappings[user_id] = {}
            self.conflicts[user_id] = {}
            self.schedule_config[user_id] = {"frequency": "daily", "enabled": False}
            self.audit_logs[user_id] = []

    def set_user_contacts(
        self,
        user_id: str,
        google: list[UnifiedContact],
        ms: list[UnifiedContact],
        base: Optional[dict[str, UnifiedContact]] = None,
        mappings: Optional[dict[str, str]] = None,
    ) -> None:
        self.ensure_user(user_id)
        self.google_contacts[user_id] = list(google)
        self.ms_contacts[user_id] = list(ms)
        self.base_contacts[user_id] = dict(base or {})
        self.stored_mappings[user_id] = dict(mappings or {})


GLOBAL_SYNC_STORE = InMemorySyncStore()


# =============================================================================
# LIVE ADK TOOLS (DATABASE & ORCHESTRATION BACKED)
# =============================================================================

def preview_sync(
    user_id: str,
    db_session: Optional[Session] = None,
    store: Optional[InMemorySyncStore] = None,
) -> dict[str, Any]:
    """Calculates proposed synchronization changes without writing to external APIs."""
    if db_session:
        service = SyncService(db_session=db_session)
        return service.preview(user_id=user_id)

    repo = store or GLOBAL_SYNC_STORE
    repo.ensure_user(user_id)

    g_contacts = repo.google_contacts.get(user_id, [])
    ms_contacts = repo.ms_contacts.get(user_id, [])
    base_contacts = repo.base_contacts.get(user_id, {})
    mappings = repo.stored_mappings.get(user_id, {})

    plan: SyncPlan = SyncPlanner.plan_sync(
        google_contacts=g_contacts,
        ms_contacts=ms_contacts,
        base_contacts=base_contacts,
        stored_mappings=mappings,
        allow_deletions=False,
        auto_resolve_conflicts=False,
    )

    for conflict in plan.conflicts:
        repo.conflicts[user_id][conflict.id] = conflict

    return {
        "status": "SUCCESS",
        "user_id": user_id,
        "summary": {
            "to_create_in_google": len(plan.to_create_in_google),
            "to_create_in_ms": len(plan.to_create_in_ms),
            "to_update_in_google": len(plan.to_update_in_google),
            "to_update_in_ms": len(plan.to_update_in_ms),
            "conflicts_detected": len(plan.conflicts),
            "orphaned_records": len(plan.orphaned),
            "unchanged_records": plan.unchanged_count,
        },
        "details": {
            "to_create_in_google": [c.name.computed_display_name() for c in plan.to_create_in_google],
            "to_create_in_ms": [c.name.computed_display_name() for c in plan.to_create_in_ms],
            "to_update_in_google": [
                {"id": u.contact_id, "patches": [p.model_dump() for p in u.patches]}
                for u in plan.to_update_in_google
            ],
            "to_update_in_ms": [
                {"id": u.contact_id, "patches": [p.model_dump() for p in u.patches]}
                for u in plan.to_update_in_ms
            ],
            "conflicts": [c.model_dump() for c in plan.conflicts],
        },
    }


def execute_sync(
    user_id: str,
    auto_resolve_policy: Optional[str] = None,
    db_session: Optional[Session] = None,
    store: Optional[InMemorySyncStore] = None,
) -> dict[str, Any]:
    """Executes bi-directional sync for safe records and holds unresolved conflicts."""
    if db_session:
        service = SyncService(db_session=db_session)
        user = service.repo.get_user(user_id)
        policy = auto_resolve_policy or (user.sync_policy if user and user.sync_policy != "ask" else "last_writer_wins")
        return service.execute(user_id=user_id, auto_resolve_policy=policy, trigger_type="MANUAL")

    repo = store or GLOBAL_SYNC_STORE
    repo.ensure_user(user_id)

    g_contacts = repo.google_contacts.get(user_id, [])
    ms_contacts = repo.ms_contacts.get(user_id, [])
    base_contacts = repo.base_contacts.get(user_id, {})
    mappings = repo.stored_mappings.get(user_id, {})

    plan: SyncPlan = SyncPlanner.plan_sync(
        google_contacts=g_contacts,
        ms_contacts=ms_contacts,
        base_contacts=base_contacts,
        stored_mappings=mappings,
        allow_deletions=False,
    )

    created_g = 0
    created_ms = 0
    updated_g = 0
    updated_ms = 0

    for item in plan.to_create_in_google:
        new_g = item.model_copy(deep=True)
        if not new_g.google_resource_name:
            new_g.google_resource_name = f"people/c_{uuid.uuid4().hex[:8]}"
        g_contacts.append(new_g)
        if new_g.ms_id and new_g.google_resource_name:
            mappings[new_g.google_resource_name] = new_g.ms_id
        created_g += 1

    for item in plan.to_create_in_ms:
        new_ms = item.model_copy(deep=True)
        if not new_ms.ms_id:
            new_ms.ms_id = f"ms_{uuid.uuid4().hex[:8]}"
        ms_contacts.append(new_ms)
        if new_ms.google_resource_name and new_ms.ms_id:
            mappings[new_ms.google_resource_name] = new_ms.ms_id
        created_ms += 1

    for action in plan.to_update_in_google:
        for idx, existing in enumerate(g_contacts):
            if existing.google_resource_name == action.contact_id:
                g_contacts[idx] = action.updated_contact
                updated_g += 1
                break

    for action in plan.to_update_in_ms:
        for idx, existing in enumerate(ms_contacts):
            if existing.ms_id == action.contact_id:
                ms_contacts[idx] = action.updated_contact
                updated_ms += 1
                break

    held_conflicts: list[ConflictItem] = []
    for conflict in plan.conflicts:
        if auto_resolve_policy == "google_wins":
            resolve_conflict_item(user_id, conflict.id, "use_google", store=repo)
            updated_ms += 1
        elif auto_resolve_policy == "microsoft_wins":
            resolve_conflict_item(user_id, conflict.id, "use_microsoft", store=repo)
            updated_g += 1
        elif auto_resolve_policy == "keep_both":
            resolve_conflict_item(user_id, conflict.id, "keep_both", store=repo)
            updated_g += 1
            updated_ms += 1
        elif auto_resolve_policy == "skip":
            resolve_conflict_item(user_id, conflict.id, "skip", store=repo)
        else:
            repo.conflicts[user_id][conflict.id] = conflict
            held_conflicts.append(conflict)

    audit_entry = {
        "id": str(uuid.uuid4()),
        "user_id": user_id,
        "trigger_type": "MANUAL",
        "status": "SUCCESS" if not held_conflicts else "PARTIAL_SUCCESS",
        "contacts_created_google": created_g,
        "contacts_created_ms": created_ms,
        "contacts_updated_google": updated_g,
        "contacts_updated_ms": updated_ms,
        "conflicts_detected": len(held_conflicts),
        "executed_at": datetime.now(timezone.utc).isoformat(),
    }
    repo.audit_logs[user_id].insert(0, audit_entry)

    return {
        "status": "SUCCESS",
        "user_id": user_id,
        "created_google": created_g,
        "created_ms": created_ms,
        "updated_google": updated_g,
        "updated_ms": updated_ms,
        "unchanged": plan.unchanged_count,
        "conflicts_held": len(held_conflicts),
        "audit_id": audit_entry["id"],
    }


def get_pending_conflicts(
    user_id: str,
    db_session: Optional[Session] = None,
    store: Optional[InMemorySyncStore] = None,
) -> list[dict[str, Any]]:
    """Retrieves all unresolved conflicts for the user."""
    if db_session:
        repo = SyncRepository(db_session)
        conflicts = repo.get_pending_conflicts(user_id)
        return [{"id": c.id, "contact_name": c.contact_name, **c.conflict_data} for c in conflicts]

    repo_mem = store or GLOBAL_SYNC_STORE
    repo_mem.ensure_user(user_id)
    conflicts_dict = repo_mem.conflicts.get(user_id, {})
    return [item.model_dump() for item in conflicts_dict.values()]


def resolve_conflict_item(
    user_id: str,
    conflict_id: str,
    resolution_choice: str,
    db_session: Optional[Session] = None,
    store: Optional[InMemorySyncStore] = None,
) -> dict[str, Any]:
    """Applies a user decision to a specific conflict record."""
    if db_session:
        service = SyncService(db_session=db_session)
        return service.resolve_conflict(user_id, conflict_id, resolution_choice)

    repo = store or GLOBAL_SYNC_STORE
    repo.ensure_user(user_id)

    user_conflicts = repo.conflicts.get(user_id, {})
    if conflict_id not in user_conflicts:
        return {
            "status": "ERROR",
            "message": f"Conflict '{conflict_id}' not found or already resolved.",
        }

    conflict: ConflictItem = user_conflicts[conflict_id]
    choice = resolution_choice.lower()

    if choice not in ("keep_both", "use_google", "use_microsoft", "skip"):
        return {
            "status": "ERROR",
            "message": f"Invalid resolution choice '{resolution_choice}'.",
        }

    g_contacts = repo.google_contacts[user_id]
    ms_contacts = repo.ms_contacts[user_id]
    g_res_name = conflict.google_resource_name
    ms_id = conflict.ms_id

    if choice == "use_google":
        winner = conflict.google_contact.model_copy(deep=True)
        winner.ms_id = ms_id
        for idx, ms_c in enumerate(ms_contacts):
            if ms_c.ms_id == ms_id:
                ms_contacts[idx] = winner
                break

    elif choice == "use_microsoft":
        winner = conflict.ms_contact.model_copy(deep=True)
        winner.google_resource_name = g_res_name
        for idx, g_c in enumerate(g_contacts):
            if g_c.google_resource_name == g_res_name:
                g_contacts[idx] = winner
                break

    elif choice == "keep_both":
        g_c_idx = next((i for i, c in enumerate(g_contacts) if c.google_resource_name == g_res_name), None)
        ms_c_idx = next((i for i, c in enumerate(ms_contacts) if c.ms_id == ms_id), None)

        if g_c_idx is not None and ms_c_idx is not None:
            merged = conflict.google_contact.model_copy(deep=True)
            merged.ms_id = ms_id

            existing_phone_vals = {p.value for p in merged.phones}
            for p in conflict.ms_contact.phones:
                if p.value not in existing_phone_vals:
                    merged.phones.append(PhoneNumber(value=p.value, type=p.type, is_primary=False))

            g_contacts[g_c_idx] = merged
            ms_contacts[ms_c_idx] = merged.model_copy(deep=True)

    elif choice == "skip":
        return {
            "status": "SKIPPED",
            "conflict_id": conflict_id,
            "message": "Conflict left unchanged.",
        }

    del user_conflicts[conflict_id]

    return {
        "status": "RESOLVED",
        "conflict_id": conflict_id,
        "contact_name": conflict.contact_name,
        "resolution_choice": choice,
        "message": f"Successfully resolved conflict for '{conflict.contact_name}' using choice '{choice}'.",
    }


def configure_schedule(
    user_id: str,
    frequency: str = "daily",
    enabled: bool = True,
    cron_expr: Optional[str] = None,
    db_session: Optional[Session] = None,
    store: Optional[InMemorySyncStore] = None,
) -> dict[str, Any]:
    """Updates background sync schedule settings."""
    cron_map = {
        "hourly": "0 * * * *",
        "every_6_hours": "0 */6 * * *",
        "daily": "0 6 * * *",
        "weekly": "0 6 * * 0",
        "monthly": "0 6 1 * *",
    }
    final_cron = "disabled" if not enabled else (cron_expr or cron_map.get(frequency, "0 6 * * *"))

    if db_session:
        repo = SyncRepository(db_session)
        user = repo.get_user(user_id)
        if not user:
            user = repo.get_or_create_user(email=f"{user_id}@example.com")
        if user:
            repo.update_user_policy(user.id, schedule_cron=final_cron)
            return {
                "status": "SUCCESS",
                "user_id": user.id,
                "frequency": frequency,
                "enabled": enabled,
                "schedule_cron": final_cron,
                "message": f"Scheduled sync is now {'ENABLED' if enabled else 'DISABLED'} ({frequency}).",
            }

    repo_mem = store or GLOBAL_SYNC_STORE
    repo_mem.ensure_user(user_id)
    repo_mem.schedule_config[user_id] = {
        "frequency": frequency,
        "enabled": enabled,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    return {
        "status": "SUCCESS",
        "user_id": user_id,
        "frequency": frequency,
        "enabled": enabled,
        "message": f"Scheduled sync is now {'ENABLED' if enabled else 'DISABLED'} ({frequency}).",
    }


def configure_policy(
    user_id: str,
    sync_policy: str = "ask",
    db_session: Optional[Session] = None,
    store: Optional[InMemorySyncStore] = None,
) -> dict[str, Any]:
    """Updates user conflict resolution policy preference."""
    valid_policies = ("ask", "google_wins", "microsoft_wins", "keep_both", "skip")
    pol = sync_policy.lower() if sync_policy.lower() in valid_policies else "ask"

    if db_session:
        repo = SyncRepository(db_session)
        user = repo.get_user(user_id)
        if not user:
            user = repo.get_or_create_user(email=f"{user_id}@example.com")
        if user:
            repo.update_user_policy(user.id, sync_policy=pol)
            return {
                "status": "SUCCESS",
                "user_id": user.id,
                "sync_policy": pol,
                "message": f"Conflict resolution policy set to '{pol}'.",
            }

    repo_mem = store or GLOBAL_SYNC_STORE
    repo_mem.ensure_user(user_id)
    return {
        "status": "SUCCESS",
        "user_id": user_id,
        "sync_policy": pol,
        "message": f"Conflict resolution policy set to '{pol}'.",
    }


def compare_contacts(
    user_id: str,
    db_session: Optional[Session] = None,
    store: Optional[InMemorySyncStore] = None,
) -> dict[str, Any]:
    """Inspects both providers and categorizes current contact state."""
    if db_session:
        service = SyncService(db_session=db_session)
        user = service.repo.get_user(user_id)
        if user:
            g_client, ms_client = service._get_clients(user.id)
            g_contacts = g_client.get_all_contacts() if g_client else []
            ms_contacts = ms_client.get_all_contacts() if ms_client else []
            mappings = service.repo.get_mapping_dict(user.id)
            res = ContactMatcher.match_collections(
                google_contacts=g_contacts,
                ms_contacts=ms_contacts,
                stored_mappings=mappings,
            )
            return {
                "status": "SUCCESS",
                "user_id": user.id,
                "total_google": len(g_contacts),
                "total_microsoft": len(ms_contacts),
                "definite_matches": len(res.definite_matches),
                "potential_duplicates": len(res.potential_duplicates),
                "google_only": len(res.unmatched_google),
                "microsoft_only": len(res.unmatched_ms),
                "match_details": [
                    {
                        "contact_name": m.google_contact.name.computed_display_name() if m.google_contact else (m.ms_contact.name.computed_display_name() if m.ms_contact else "Contact"),
                        "score": m.score,
                        "reason": m.reason,
                    }
                    for m in res.definite_matches
                ],
            }

    repo_mem = store or GLOBAL_SYNC_STORE
    repo_mem.ensure_user(user_id)

    g_contacts = repo_mem.google_contacts.get(user_id, [])
    ms_contacts = repo_mem.ms_contacts.get(user_id, [])
    mappings = repo_mem.stored_mappings.get(user_id, {})

    res: MatchingResult = ContactMatcher.match_collections(
        google_contacts=g_contacts,
        ms_contacts=ms_contacts,
        stored_mappings=mappings,
    )

    return {
        "status": "SUCCESS",
        "user_id": user_id,
        "total_google": len(g_contacts),
        "total_microsoft": len(ms_contacts),
        "definite_matches": len(res.definite_matches),
        "potential_duplicates": len(res.potential_duplicates),
        "google_only": len(res.unmatched_google),
        "microsoft_only": len(res.unmatched_ms),
        "match_details": [
            {
                "contact_name": m.google_contact.name.computed_display_name() if m.google_contact else (m.ms_contact.name.computed_display_name() if m.ms_contact else "Contact"),
                "score": m.score,
                "reason": m.reason,
            }
            for m in res.definite_matches
        ],
    }


def get_sync_audit_report(
    user_id: str,
    limit: int = 5,
    db_session: Optional[Session] = None,
    store: Optional[InMemorySyncStore] = None,
) -> list[dict[str, Any]]:
    """Retrieves recent synchronization run logs and status details."""
    if db_session:
        repo = SyncRepository(db_session)
        logs = repo.get_audit_logs(user_id, limit=limit)
        return [
            {
                "id": l.id,
                "trigger_type": l.trigger_type,
                "status": l.status,
                "contacts_examined": l.contacts_examined,
                "contacts_created": l.contacts_created,
                "contacts_updated": l.contacts_updated,
                "conflicts_detected": l.conflicts_detected,
                "executed_at": l.executed_at.isoformat() if l.executed_at else None,
            }
            for l in logs
        ]

    repo_mem = store or GLOBAL_SYNC_STORE
    repo_mem.ensure_user(user_id)
    return repo_mem.audit_logs.get(user_id, [])[:limit]
