"""Transactional Database Repository for Synchronization State (Section 7)."""
from __future__ import annotations

from datetime import datetime, timezone
import logging
from typing import Any, Optional
from sqlalchemy import select
from sqlalchemy.orm import Session

from execution.crypto import decrypt_token, encrypt_token
from execution.database import (
    ContactMapping,
    SyncAuditLog,
    SyncConflict,
    UserAccount,
)

logger = logging.getLogger(__name__)


class SyncRepository:
    """Repository handling atomic database persistence for SyncGM."""

    def __init__(self, session: Session) -> None:
        self.session = session

    # -------------------------------------------------------------------------
    # User Account Operations
    # -------------------------------------------------------------------------
    def get_or_create_user(
        self,
        email: str,
        display_name: str = "User",
        google_refresh_token: Optional[str] = None,
        ms_refresh_token: Optional[str] = None,
        sync_policy: str = "last_writer_wins",
        allow_deletions: bool = True,
        schedule_cron: Optional[str] = "disabled",
    ) -> UserAccount:
        stmt = select(UserAccount).where(UserAccount.email == email.lower().strip())
        user = self.session.execute(stmt).scalar_one_or_none()

        if not user:
            g_enc = encrypt_token(google_refresh_token) if google_refresh_token else None
            m_enc = encrypt_token(ms_refresh_token) if ms_refresh_token else None
            user = UserAccount(
                email=email.lower().strip(),
                display_name=display_name,
                google_refresh_token_enc=g_enc,
                ms_refresh_token_enc=m_enc,
                sync_policy=sync_policy,
                allow_deletions=allow_deletions,
                schedule_cron=schedule_cron,
            )
            self.session.add(user)
            self.session.commit()
            self.session.refresh(user)
        return user

    def get_user(self, user_id: str) -> Optional[UserAccount]:
        stmt = select(UserAccount).where(
            (UserAccount.id == user_id)
            | (UserAccount.email == user_id.lower().strip())
            | (UserAccount.email == f"{user_id.lower().strip()}@example.com")
        )
        return self.session.execute(stmt).scalars().first()

    def get_decrypted_tokens(self, user: UserAccount) -> tuple[Optional[str], Optional[str]]:
        """Decrypts and returns (google_refresh_token, ms_refresh_token)."""
        g_tok = decrypt_token(user.google_refresh_token_enc) if user.google_refresh_token_enc else None
        m_tok = decrypt_token(user.ms_refresh_token_enc) if user.ms_refresh_token_enc else None
        return g_tok, m_tok

    def update_user_tokens(
        self,
        user_id: str,
        google_refresh_token: Optional[str] = None,
        ms_refresh_token: Optional[str] = None,
    ) -> None:
        user = self.get_user(user_id)
        if not user:
            raise ValueError(f"User '{user_id}' not found.")
        if google_refresh_token is not None:
            user.google_refresh_token_enc = encrypt_token(google_refresh_token)
        if ms_refresh_token is not None:
            user.ms_refresh_token_enc = encrypt_token(ms_refresh_token)
        self.session.commit()

    def update_user_policy(
        self,
        user_id: str,
        sync_policy: Optional[str] = None,
        allow_deletions: Optional[bool] = None,
        schedule_cron: Optional[str] = None,
    ) -> UserAccount:
        user = self.get_user(user_id)
        if not user:
            raise ValueError(f"User '{user_id}' not found.")
        if sync_policy is not None:
            user.sync_policy = sync_policy
        if allow_deletions is not None:
            user.allow_deletions = allow_deletions
        if schedule_cron is not None:
            user.schedule_cron = schedule_cron
        self.session.commit()
        self.session.refresh(user)
        return user

    def get_active_scheduled_users(self) -> list[UserAccount]:
        """Returns all users configured with an active background schedule."""
        stmt = select(UserAccount).where(
            UserAccount.schedule_cron.isnot(None),
            UserAccount.schedule_cron != "",
            UserAccount.schedule_cron != "disabled",
        )
        return list(self.session.execute(stmt).scalars().all())

    # -------------------------------------------------------------------------
    # Contact Mappings Operations
    # -------------------------------------------------------------------------
    def get_contact_mappings(self, user_id: str) -> list[ContactMapping]:
        user = self.get_user(user_id)
        actual_id = user.id if user else user_id
        stmt = select(ContactMapping).where(ContactMapping.user_id == actual_id)
        return list(self.session.execute(stmt).scalars().all())

    def get_mapping_dict(self, user_id: str) -> dict[str, str]:
        """Returns mapping of google_resource_id -> ms_contact_id for user."""
        mappings = self.get_contact_mappings(user_id)
        return {m.google_resource_id: m.ms_contact_id for m in mappings}

    def get_mapping_timestamps(self, user_id: str) -> dict[str, datetime]:
        """Returns mapping of google_resource_id and ms_contact_id -> last_synced_at datetime."""
        mappings = self.get_contact_mappings(user_id)
        res: dict[str, datetime] = {}
        for m in mappings:
            res[m.google_resource_id] = m.last_synced_at
            res[m.ms_contact_id] = m.last_synced_at
        return res

    def upsert_contact_mapping(
        self,
        user_id: str,
        google_resource_id: str,
        ms_contact_id: str,
        last_synced_hash: str,
    ) -> ContactMapping:
        user = self.get_user(user_id)
        actual_id = user.id if user else user_id
        stmt = select(ContactMapping).where(
            ContactMapping.user_id == actual_id,
            (ContactMapping.google_resource_id == google_resource_id)
            | (ContactMapping.ms_contact_id == ms_contact_id),
        )
        mapping = self.session.execute(stmt).scalar_one_or_none()

        now = datetime.now(timezone.utc)
        if mapping:
            mapping.google_resource_id = google_resource_id
            mapping.ms_contact_id = ms_contact_id
            mapping.last_synced_hash = last_synced_hash
            mapping.last_synced_at = now
        else:
            mapping = ContactMapping(
                user_id=actual_id,
                google_resource_id=google_resource_id,
                ms_contact_id=ms_contact_id,
                last_synced_hash=last_synced_hash,
                last_synced_at=now,
            )
            self.session.add(mapping)

        self.session.commit()
        self.session.refresh(mapping)
        return mapping

    def delete_contact_mapping(self, user_id: str, mapping_id: str) -> bool:
        user = self.get_user(user_id)
        actual_id = user.id if user else user_id
        stmt = select(ContactMapping).where(
            ContactMapping.user_id == actual_id, ContactMapping.id == mapping_id
        )
        mapping = self.session.execute(stmt).scalar_one_or_none()
        if mapping:
            self.session.delete(mapping)
            self.session.commit()
            return True
        return False

    def delete_contact_mapping_by_keys(
        self,
        user_id: str,
        google_resource_id: Optional[str] = None,
        ms_contact_id: Optional[str] = None,
    ) -> bool:
        user = self.get_user(user_id)
        actual_id = user.id if user else user_id
        conditions = [ContactMapping.user_id == actual_id]
        if google_resource_id and ms_contact_id:
            conditions.append(
                (ContactMapping.google_resource_id == google_resource_id)
                | (ContactMapping.ms_contact_id == ms_contact_id)
            )
        elif google_resource_id:
            conditions.append(ContactMapping.google_resource_id == google_resource_id)
        elif ms_contact_id:
            conditions.append(ContactMapping.ms_contact_id == ms_contact_id)
        else:
            return False

        stmt = select(ContactMapping).where(*conditions)
        mapping = self.session.execute(stmt).scalar_one_or_none()
        if mapping:
            self.session.delete(mapping)
            self.session.commit()
            return True
        return False

    # -------------------------------------------------------------------------
    # Sync Conflict Queue Operations
    # -------------------------------------------------------------------------
    def create_conflict(
        self,
        user_id: str,
        contact_name: str,
        conflict_data: dict[str, Any],
        mapping_id: Optional[str] = None,
    ) -> SyncConflict:
        conflict = SyncConflict(
            user_id=user_id,
            mapping_id=mapping_id,
            contact_name=contact_name,
            conflict_data=conflict_data,
            status="PENDING",
        )
        self.session.add(conflict)
        self.session.commit()
        self.session.refresh(conflict)
        return conflict

    def get_pending_conflicts(self, user_id: str) -> list[SyncConflict]:
        stmt = select(SyncConflict).where(
            SyncConflict.user_id == user_id,
            SyncConflict.status == "PENDING",
        ).order_by(SyncConflict.created_at.desc())
        return list(self.session.execute(stmt).scalars().all())

    def get_conflict(self, conflict_id: str) -> Optional[SyncConflict]:
        stmt = select(SyncConflict).where(SyncConflict.id == conflict_id)
        return self.session.execute(stmt).scalar_one_or_none()

    def resolve_conflict(self, conflict_id: str, status: str = "RESOLVED") -> bool:
        conflict = self.get_conflict(conflict_id)
        if conflict:
            conflict.status = status
            self.session.commit()
            return True
        return False

    # -------------------------------------------------------------------------
    # Sync Audit Logs Operations
    # -------------------------------------------------------------------------
    def create_audit_log(
        self,
        user_id: str,
        trigger_type: str,
        status: str,
        contacts_examined: int,
        contacts_created: int,
        contacts_updated: int,
        conflicts_detected: int,
        error_details: Optional[str] = None,
    ) -> SyncAuditLog:
        log = SyncAuditLog(
            user_id=user_id,
            trigger_type=trigger_type,
            status=status,
            contacts_examined=contacts_examined,
            contacts_created=contacts_created,
            contacts_updated=contacts_updated,
            conflicts_detected=conflicts_detected,
            error_details=error_details,
        )
        self.session.add(log)
        self.session.commit()
        self.session.refresh(log)
        return log

    def get_audit_logs(self, user_id: str, limit: int = 5) -> list[SyncAuditLog]:
        stmt = select(SyncAuditLog).where(
            SyncAuditLog.user_id == user_id
        ).order_by(SyncAuditLog.executed_at.desc()).limit(limit)
        return list(self.session.execute(stmt).scalars().all())
