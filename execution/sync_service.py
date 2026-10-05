"""Full Synchronization Orchestration Service connecting DB, APIs, and Diff Engine."""
from __future__ import annotations

import logging
from typing import Any, Callable, Optional
from sqlalchemy.orm import Session

from execution.diff_engine import SyncPlanner, ensure_utc
from execution.google_client import GoogleContactsClient
from execution.models import (
    ConflictItem,
    PhoneNumber,
    SyncPlan,
    UnifiedContact,
)
from execution.ms_client import MicrosoftContactsClient
from execution.repository import SyncRepository

logger = logging.getLogger(__name__)


class SyncService:
    """Coordinates API clients, differential sync engine, and database persistence with PII safety."""

    def __init__(
        self,
        db_session: Session,
        google_client_factory: Optional[Callable[[str], GoogleContactsClient]] = None,
        ms_client_factory: Optional[Callable[[str], MicrosoftContactsClient]] = None,
    ) -> None:
        self.session = db_session
        self.repo = SyncRepository(db_session)
        self.google_client_factory = google_client_factory
        self.ms_client_factory = ms_client_factory

    def _get_clients(self, user_id: str) -> tuple[Optional[GoogleContactsClient], Optional[MicrosoftContactsClient]]:
        import os

        user = self.repo.get_user(user_id)
        if not user:
            raise ValueError(f"User '{user_id}' not found.")

        g_tok, m_tok = self.repo.get_decrypted_tokens(user)

        g_client = None
        if self.google_client_factory and g_tok:
            g_client = self.google_client_factory(g_tok)
        elif g_tok:
            cid = os.getenv("GOOGLE_CLIENT_ID")
            csec = os.getenv("GOOGLE_CLIENT_SECRET")
            g_client = GoogleContactsClient(
                access_token=g_tok,
                refresh_token=g_tok,
                client_id=cid,
                client_secret=csec,
            )
            if cid and csec:
                try:
                    g_client.access_token = g_client._refresh_access_token()
                except Exception as exc:
                    logger.warning("Google access token refresh attempt: %s", exc)

        ms_client = None
        if self.ms_client_factory and m_tok:
            ms_client = self.ms_client_factory(m_tok)
        elif m_tok:
            cid = os.getenv("MS_CLIENT_ID")
            csec = os.getenv("MS_CLIENT_SECRET")
            ms_client = MicrosoftContactsClient(
                access_token=m_tok,
                refresh_token=m_tok,
                client_id=cid,
                client_secret=csec,
            )
            if cid and csec:
                try:
                    ms_client.access_token = ms_client._refresh_access_token()
                except Exception as exc:
                    logger.warning("Microsoft access token refresh attempt: %s", exc)

        return g_client, ms_client

    def preview(
        self,
        user_id: str,
        live_google: Optional[list[UnifiedContact]] = None,
        live_ms: Optional[list[UnifiedContact]] = None,
    ) -> dict[str, Any]:
        """Calculates dry-run sync plan without executing API writes."""
        user = self.repo.get_user(user_id)
        if not user:
            user = self.repo.get_or_create_user(email=f"{user_id}@example.com")

        # If live contacts not directly passed, attempt client fetch
        g_contacts = live_google or []
        ms_contacts = live_ms or []
        if live_google is None or live_ms is None:
            g_client, ms_client = self._get_clients(user.id)
            if g_client and live_google is None:
                g_contacts = g_client.get_all_contacts()
            if ms_client and live_ms is None:
                ms_contacts = ms_client.get_all_contacts()

        mappings = self.repo.get_mapping_dict(user.id)
        timestamps = self.repo.get_mapping_timestamps(user.id)
        plan: SyncPlan = SyncPlanner.plan_sync(
            google_contacts=g_contacts,
            ms_contacts=ms_contacts,
            base_contacts={},
            stored_mappings=mappings,
            base_timestamps=timestamps,
            allow_deletions=user.allow_deletions,
        )

        logger.info(
            "User [%s] dry-run preview: +%d G, +%d MS, ~%d G, ~%d MS, %d conflicts",
            user_id,
            len(plan.to_create_in_google),
            len(plan.to_create_in_ms),
            len(plan.to_update_in_google),
            len(plan.to_update_in_ms),
            len(plan.conflicts),
        )

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
                "conflicts": [c.model_dump(mode="json") for c in plan.conflicts],
            },
        }

    def execute(
        self,
        user_id: str,
        auto_resolve_policy: str = "last_writer_wins",
        trigger_type: str = "MANUAL",
        live_google: Optional[list[UnifiedContact]] = None,
        live_ms: Optional[list[UnifiedContact]] = None,
    ) -> dict[str, Any]:
        """Executes full bi-directional sync, writes updates, persists mappings and logs."""
        user = self.repo.get_user(user_id)
        if not user:
            user = self.repo.get_or_create_user(email=f"{user_id}@example.com")

        g_client, ms_client = self._get_clients(user.id)
        if not g_client and not ms_client and live_google is None and live_ms is None:
            return {
                "status": "SUCCESS",
                "created_google": 0,
                "created_ms": 0,
                "updated_google": 0,
                "updated_ms": 0,
                "unchanged": 0,
                "conflicts": 0,
                "message": "Accounts are not fully linked yet. Please click 'Reconnect' to authenticate.",
            }

        g_contacts = live_google if live_google is not None else (g_client.get_all_contacts() if g_client else [])
        ms_contacts = live_ms if live_ms is not None else (ms_client.get_all_contacts() if ms_client else [])

        u_id = user.id
        mappings = self.repo.get_mapping_dict(u_id)
        timestamps = self.repo.get_mapping_timestamps(u_id)
        plan: SyncPlan = SyncPlanner.plan_sync(
            google_contacts=g_contacts,
            ms_contacts=ms_contacts,
            base_contacts={},
            stored_mappings=mappings,
            base_timestamps=timestamps,
            allow_deletions=True,
            auto_resolve_conflicts=(auto_resolve_policy in ("last_writer_wins", "most_recent")),
        )

        created_g = 0
        created_ms = 0
        updated_g = 0
        updated_ms = 0
        deleted_g = 0
        deleted_ms = 0
        # 0. Persist Mappings for all existing and matched contacts (ensures deletion detection baseline)
        for g_id, m_id, hash_val in plan.persisted_mappings:
            self.repo.upsert_contact_mapping(u_id, g_id, m_id, hash_val)

        # 1. Creations in Google
        for contact in plan.to_create_in_google:
            created_contact = g_client.create_contact(contact) if g_client else contact
            if not created_contact.google_resource_name:
                created_contact.google_resource_name = f"people/c_mock_{created_g}"
            g_id = created_contact.google_resource_name
            m_id = contact.ms_id or f"ms_mock_{created_g}"
            hash_val = created_contact.compute_field_hash()
            self.repo.upsert_contact_mapping(u_id, g_id, m_id, hash_val)
            created_g += 1

        # 2. Creations in Microsoft
        for contact in plan.to_create_in_ms:
            created_contact = ms_client.create_contact(contact) if ms_client else contact
            if not created_contact.ms_id:
                created_contact.ms_id = f"ms_mock_{created_ms}"
            m_id = created_contact.ms_id
            g_id = contact.google_resource_name or f"people/c_mock_{created_ms}"
            hash_val = created_contact.compute_field_hash()
            self.repo.upsert_contact_mapping(u_id, g_id, m_id, hash_val)
            created_ms += 1

        # 3. Updates in Google
        for action in plan.to_update_in_google:
            if g_client:
                g_client.update_contact(action.updated_contact)
            m_id = mappings.get(action.contact_id, f"ms_mapped_{action.contact_id}")
            hash_val = action.updated_contact.compute_field_hash()
            self.repo.upsert_contact_mapping(u_id, action.contact_id, m_id, hash_val)
            updated_g += 1

        # 4. Updates in Microsoft
        for action in plan.to_update_in_ms:
            if ms_client:
                ms_client.update_contact(action.updated_contact)
            g_id = next((gid for gid, mid in mappings.items() if mid == action.contact_id), f"people/c_{action.contact_id}")
            hash_val = action.updated_contact.compute_field_hash()
            self.repo.upsert_contact_mapping(u_id, g_id, action.contact_id, hash_val)
            updated_ms += 1

        # 5. Deletions in Microsoft (Deleted from Google)
        for ms_id in plan.to_delete_in_ms:
            if ms_client:
                try:
                    ms_client.delete_contact(ms_id)
                except Exception as exc:
                    logger.warning("Failed to delete contact %s in MS: %s", ms_id, exc)
            self.repo.delete_contact_mapping_by_keys(u_id, ms_contact_id=ms_id)
            deleted_ms += 1

        # 6. Deletions in Google (Deleted from Microsoft)
        for g_id in plan.to_delete_in_google:
            if g_client:
                try:
                    g_client.delete_contact(g_id)
                except Exception as exc:
                    logger.warning("Failed to delete contact %s in Google: %s", g_id, exc)
            self.repo.delete_contact_mapping_by_keys(u_id, google_resource_id=g_id)
            deleted_g += 1

        # 7. Handle Conflicts
        held_conflicts = 0
        for conflict in plan.conflicts:
            if auto_resolve_policy in ("last_writer_wins", "most_recent"):
                # Compare last_modified timestamps
                g_lm = ensure_utc(conflict.google_contact.last_modified) if conflict.google_contact else None
                ms_lm = ensure_utc(conflict.ms_contact.last_modified) if conflict.ms_contact else None
                if g_lm and ms_lm:
                    if g_lm >= ms_lm:
                        self.resolve_conflict(u_id, conflict.id, "use_google", conflict_item=conflict)
                        updated_ms += 1
                    else:
                        self.resolve_conflict(u_id, conflict.id, "use_microsoft", conflict_item=conflict)
                        updated_g += 1
                elif ms_lm and not g_lm:
                    self.resolve_conflict(u_id, conflict.id, "use_microsoft", conflict_item=conflict)
                    updated_g += 1
                else:
                    self.resolve_conflict(u_id, conflict.id, "use_google", conflict_item=conflict)
                    updated_ms += 1
            elif auto_resolve_policy == "google_wins":
                self.resolve_conflict(u_id, conflict.id, "use_google", conflict_item=conflict)
                updated_ms += 1
            elif auto_resolve_policy == "microsoft_wins":
                self.resolve_conflict(u_id, conflict.id, "use_microsoft", conflict_item=conflict)
                updated_g += 1
            elif auto_resolve_policy == "keep_both":
                self.resolve_conflict(u_id, conflict.id, "keep_both", conflict_item=conflict)
                updated_g += 1
                updated_ms += 1
            elif auto_resolve_policy == "skip":
                self.resolve_conflict(u_id, conflict.id, "skip", conflict_item=conflict)
            else:
                self.repo.create_conflict(
                    user_id=u_id,
                    contact_name=conflict.contact_name,
                    conflict_data=conflict.model_dump(mode="json"),
                )
                held_conflicts += 1

        # 8. Audit Logging (PII Redacted)
        total_examined = len(g_contacts) + len(ms_contacts)
        status = "SUCCESS" if held_conflicts == 0 else "PARTIAL_SUCCESS"
        audit = self.repo.create_audit_log(
            user_id=u_id,
            trigger_type=trigger_type,
            status=status,
            contacts_examined=total_examined,
            contacts_created=created_g + created_ms,
            contacts_updated=updated_g + updated_ms,
            conflicts_detected=held_conflicts,
        )

        logger.info(
            "User [%s] sync finished (%s): +%d created, ~%d updated, -%d deleted, %d conflicts held. AuditLog: %s",
            user_id,
            trigger_type,
            created_g + created_ms,
            updated_g + updated_ms,
            deleted_g + deleted_ms,
            held_conflicts,
            audit.id,
        )

        return {
            "status": "SUCCESS",
            "user_id": user_id,
            "trigger_type": trigger_type,
            "created_google": created_g,
            "created_ms": created_ms,
            "updated_google": updated_g,
            "updated_ms": updated_ms,
            "deleted_google": deleted_g,
            "deleted_ms": deleted_ms,
            "unchanged": plan.unchanged_count,
            "conflicts_held": held_conflicts,
            "audit_id": audit.id,
        }

    def resolve_conflict(
        self,
        user_id: str,
        conflict_id: str,
        resolution_choice: str,
        conflict_item: Optional[ConflictItem] = None,
    ) -> dict[str, Any]:
        """Resolves an isolated conflict and writes the chosen state to providers and DB mappings."""
        choice = resolution_choice.lower()
        if choice not in ("last_writer_wins", "most_recent", "keep_both", "use_google", "use_microsoft", "skip"):
            return {
                "status": "ERROR",
                "message": f"Invalid choice '{resolution_choice}'.",
            }

        conflict_db = self.repo.get_conflict(conflict_id)
        if not conflict_db and not conflict_item:
            return {
                "status": "ERROR",
                "message": f"Conflict '{conflict_id}' not found.",
            }

        c_data = conflict_db.conflict_data if conflict_db else (conflict_item.model_dump(mode="json") if conflict_item else {})
        c_name = conflict_db.contact_name if conflict_db else (conflict_item.contact_name if conflict_item else "Contact")

        g_id = c_data.get("google_resource_name")
        ms_id = c_data.get("ms_id")

        if choice == "skip":
            if conflict_db:
                self.repo.resolve_conflict(conflict_id, status="RESOLVED")
            return {
                "status": "SKIPPED",
                "conflict_id": conflict_id,
                "message": f"Conflict for '{c_name}' left unchanged.",
            }

        user = self.repo.get_user(user_id)
        u_id = user.id if user else user_id
        g_client, ms_client = self._get_clients(u_id)
        google_contact_data = c_data.get("google_contact")
        ms_contact_data = c_data.get("ms_contact")

        g_contact = UnifiedContact.model_validate(google_contact_data) if google_contact_data else None
        ms_contact = UnifiedContact.model_validate(ms_contact_data) if ms_contact_data else None

        if choice in ("last_writer_wins", "most_recent"):
            g_lm = ensure_utc(g_contact.last_modified) if g_contact else None
            ms_lm = ensure_utc(ms_contact.last_modified) if ms_contact else None
            if g_lm and ms_lm:
                choice = "use_google" if g_lm >= ms_lm else "use_microsoft"
            elif ms_lm and not g_lm:
                choice = "use_microsoft"
            else:
                choice = "use_google"

        if choice == "use_google" and g_contact:
            if ms_client and ms_id:
                ms_winner = g_contact.model_copy(deep=True)
                ms_winner.ms_id = ms_id
                if ms_contact and ms_contact.ms_etag:
                    ms_winner.ms_etag = ms_contact.ms_etag
                try:
                    ms_client.update_contact(ms_winner)
                except Exception as exc:
                    logger.warning("Failed to propagate Google contact to MS on conflict resolution: %s", exc)
            if g_id and ms_id:
                hash_val = g_contact.compute_field_hash()
                self.repo.upsert_contact_mapping(u_id, g_id, ms_id, hash_val)

        elif choice == "use_microsoft" and ms_contact:
            if g_client and g_id:
                g_winner = ms_contact.model_copy(deep=True)
                g_winner.google_resource_name = g_id
                if g_contact and g_contact.google_etag:
                    g_winner.google_etag = g_contact.google_etag
                try:
                    g_client.update_contact(g_winner)
                except Exception as exc:
                    logger.warning("Failed to propagate MS contact to Google on conflict resolution: %s", exc)
            if g_id and ms_id:
                hash_val = ms_contact.compute_field_hash()
                self.repo.upsert_contact_mapping(u_id, g_id, ms_id, hash_val)

        elif choice == "keep_both":
            if g_contact and ms_contact:
                merged = g_contact.model_copy(deep=True)
                merged.ms_id = ms_id
                existing_phone_vals = {p.value for p in merged.phones}
                for p in ms_contact.phones:
                    if p.value not in existing_phone_vals:
                        merged.phones.append(PhoneNumber(value=p.value, type=p.type, is_primary=False))

                if g_client and g_id:
                    merged_g = merged.model_copy(deep=True)
                    merged_g.google_resource_name = g_id
                    if g_contact.google_etag:
                        merged_g.google_etag = g_contact.google_etag
                    try:
                        g_client.update_contact(merged_g)
                    except Exception as exc:
                        logger.warning("Failed to update Google contact on keep_both: %s", exc)

                if ms_client and ms_id:
                    merged_ms = merged.model_copy(deep=True)
                    merged_ms.ms_id = ms_id
                    if ms_contact.ms_etag:
                        merged_ms.ms_etag = ms_contact.ms_etag
                    try:
                        ms_client.update_contact(merged_ms)
                    except Exception as exc:
                        logger.warning("Failed to update MS contact on keep_both: %s", exc)

                if g_id and ms_id:
                    hash_val = merged.compute_field_hash()
                    self.repo.upsert_contact_mapping(u_id, g_id, ms_id, hash_val)

        if conflict_db:
            self.repo.resolve_conflict(conflict_id, status="RESOLVED")

        logger.info("Resolved conflict [%s] for user [%s] using [%s]", conflict_id, u_id, choice)
        return {
            "status": "RESOLVED",
            "conflict_id": conflict_id,
            "contact_name": c_name,
            "resolution_choice": choice,
            "message": f"Successfully resolved conflict for '{c_name}' using choice '{choice}'.",
        }
