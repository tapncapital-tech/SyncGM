"""SQLAlchemy 2.0 Declarative Database Schema & Engine (Section 7)."""
from __future__ import annotations

from datetime import datetime, timezone
import os
from typing import Any, Optional
import uuid

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    create_engine,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    relationship,
    sessionmaker,
)


class Base(DeclarativeBase):
    """SQLAlchemy 2.0 Declarative Base."""
    pass


def generate_uuid_str() -> str:
    return str(uuid.uuid4())


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class UserAccount(Base):
    """User account entity storing OAuth credentials, policies, and schedules."""
    __tablename__ = "user_accounts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid_str)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    display_name: Mapped[Optional[str]] = mapped_column(String(255), default="User", nullable=True)
    google_refresh_token_enc: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    ms_refresh_token_enc: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    sync_policy: Mapped[str] = mapped_column(String(50), default="last_writer_wins", nullable=False)
    allow_deletions: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    schedule_cron: Mapped[Optional[str]] = mapped_column(String(50), default="disabled", nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)

    # Relationships
    mappings: Mapped[list[ContactMapping]] = relationship(
        "ContactMapping", back_populates="user", cascade="all, delete-orphan"
    )
    conflicts: Mapped[list[SyncConflict]] = relationship(
        "SyncConflict", back_populates="user", cascade="all, delete-orphan"
    )
    audit_logs: Mapped[list[SyncAuditLog]] = relationship(
        "SyncAuditLog", back_populates="user", cascade="all, delete-orphan"
    )


class ContactMapping(Base):
    """Bidirectional contact entity mapping between Google and Microsoft."""
    __tablename__ = "contact_mappings"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid_str)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("user_accounts.id", ondelete="CASCADE"), index=True, nullable=False)
    google_resource_id: Mapped[str] = mapped_column(String(255), index=True, nullable=False)
    ms_contact_id: Mapped[str] = mapped_column(String(255), index=True, nullable=False)
    last_synced_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    last_synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)

    user: Mapped[UserAccount] = relationship("UserAccount", back_populates="mappings")


class SyncConflict(Base):
    """Isolated contact conflict record requiring human review or policy resolution."""
    __tablename__ = "sync_conflicts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid_str)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("user_accounts.id", ondelete="CASCADE"), index=True, nullable=False)
    mapping_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("contact_mappings.id", ondelete="SET NULL"), nullable=True)
    contact_name: Mapped[str] = mapped_column(String(255), nullable=False)
    conflict_data: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(50), default="PENDING", index=True, nullable=False)  # PENDING, RESOLVED, IGNORED
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)

    user: Mapped[UserAccount] = relationship("UserAccount", back_populates="conflicts")


class SyncAuditLog(Base):
    """Audit log tracking synchronization executions and metrics."""
    __tablename__ = "sync_audit_logs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid_str)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("user_accounts.id", ondelete="CASCADE"), index=True, nullable=False)
    trigger_type: Mapped[str] = mapped_column(String(50), nullable=False)  # MANUAL | SCHEDULED
    status: Mapped[str] = mapped_column(String(50), nullable=False)        # SUCCESS | PARTIAL_SUCCESS | FAILED
    contacts_examined: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    contacts_created: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    contacts_updated: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    conflicts_detected: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_details: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    executed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)

    user: Mapped[UserAccount] = relationship("UserAccount", back_populates="audit_logs")


# =============================================================================
# DATABASE ENGINE & SESSION MANAGEMENT
# =============================================================================

def get_database_url() -> str:
    """Retrieves Database connection URL from environment (defaulting to local SQLite)."""
    db_url = os.getenv("DATABASE_URL")
    if db_url:
        if db_url.startswith("postgres://"):
            db_url = db_url.replace("postgres://", "postgresql+psycopg2://", 1)
        elif db_url.startswith("postgresql://") and not db_url.startswith("postgresql+"):
            db_url = db_url.replace("postgresql://", "postgresql+psycopg2://", 1)
        return db_url
    return "sqlite:///syncgm_dev.db"


def create_db_engine(database_url: Optional[str] = None):
    url = database_url or get_database_url()
    if url.startswith("sqlite"):
        return create_engine(url, connect_args={"check_same_thread": False}, echo=False)
    return create_engine(url, pool_pre_ping=True, pool_recycle=300, echo=False)


GLOBAL_ENGINE = create_db_engine()
SessionFactory = sessionmaker(bind=GLOBAL_ENGINE, autoflush=False, autocommit=False)


def init_db(engine=None) -> None:
    """Initializes all database tables."""
    eng = engine or GLOBAL_ENGINE
    Base.metadata.create_all(bind=eng)
