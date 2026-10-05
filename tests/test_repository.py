"""Test suite for Phase 4 Database Repository, Cryptography, and Persistence."""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from execution.crypto import TokenEncryptor, decrypt_token, encrypt_token
from execution.database import Base, UserAccount
from execution.repository import SyncRepository


@pytest.fixture
def db_session() -> Session:
    """In-memory SQLite session fixture."""
    engine = create_engine("sqlite:///:memory:", echo=False)
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(bind=engine)
    session = session_factory()
    yield session
    session.close()


def test_token_encryption_and_decryption_roundtrip() -> None:
    encryptor = TokenEncryptor(secret_key="my-super-secret-key-32-bytes!!")
    raw_token = "1//04_AbCdEfGhIjKlMnOpQrStUvWxYz_1234567890"

    encrypted = encryptor.encrypt(raw_token)
    assert encrypted != raw_token
    assert len(encrypted) > 28

    decrypted = encryptor.decrypt(encrypted)
    assert decrypted == raw_token


def test_default_crypto_helpers() -> None:
    token = "ms_refresh_token_example_12345"
    enc = encrypt_token(token)
    dec = decrypt_token(enc)
    assert dec == token


def test_user_repository_and_token_storage(db_session: Session) -> None:
    repo = SyncRepository(db_session)

    user = repo.get_or_create_user(
        email="sylvester.moore@example.com",
        display_name="Sylvester Moore El",
        google_refresh_token="google_secret_token_123",
        ms_refresh_token="ms_secret_token_456",
        sync_policy="ask",
        allow_deletions=False,
    )

    assert user.id is not None
    assert user.email == "sylvester.moore@example.com"
    assert user.google_refresh_token_enc != "google_secret_token_123"

    g_tok, m_tok = repo.get_decrypted_tokens(user)
    assert g_tok == "google_secret_token_123"
    assert m_tok == "ms_secret_token_456"


def test_contact_mapping_upsert(db_session: Session) -> None:
    repo = SyncRepository(db_session)
    user = repo.get_or_create_user("test@example.com")

    # Insert mapping
    mapping = repo.upsert_contact_mapping(
        user_id=user.id,
        google_resource_id="people/c555",
        ms_contact_id="ms_555",
        last_synced_hash="hash_v1",
    )
    assert mapping.id is not None
    assert mapping.last_synced_hash == "hash_v1"

    # Update mapping
    updated_mapping = repo.upsert_contact_mapping(
        user_id=user.id,
        google_resource_id="people/c555",
        ms_contact_id="ms_555",
        last_synced_hash="hash_v2",
    )
    assert updated_mapping.id == mapping.id
    assert updated_mapping.last_synced_hash == "hash_v2"

    mappings_dict = repo.get_mapping_dict(user.id)
    assert mappings_dict == {"people/c555": "ms_555"}


def test_conflict_queue_lifecycle(db_session: Session) -> None:
    repo = SyncRepository(db_session)
    user = repo.get_or_create_user("conflict_user@example.com")

    conflict = repo.create_conflict(
        user_id=user.id,
        contact_name="Alice Wonderland",
        conflict_data={"phone": {"google": "+15551111", "ms": "+15552222"}},
    )
    assert conflict.status == "PENDING"

    pending = repo.get_pending_conflicts(user.id)
    assert len(pending) == 1
    assert pending[0].contact_name == "Alice Wonderland"

    resolved = repo.resolve_conflict(conflict.id, status="RESOLVED")
    assert resolved is True

    pending_after = repo.get_pending_conflicts(user.id)
    assert len(pending_after) == 0


def test_audit_logging(db_session: Session) -> None:
    repo = SyncRepository(db_session)
    user = repo.get_or_create_user("audit_user@example.com")

    log = repo.create_audit_log(
        user_id=user.id,
        trigger_type="SCHEDULED",
        status="SUCCESS",
        contacts_examined=150,
        contacts_created=5,
        contacts_updated=12,
        conflicts_detected=1,
    )

    assert log.id is not None
    assert log.status == "SUCCESS"

    logs = repo.get_audit_logs(user.id, limit=5)
    assert len(logs) == 1
    assert logs[0].trigger_type == "SCHEDULED"
    assert logs[0].contacts_examined == 150
