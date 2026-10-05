"""Comprehensive test suite for Phase 2 Deterministic Matching & 3-Way Diff Engine."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import pytest

from execution.diff_engine import SyncPlanner, ThreeWayDiffEngine
from execution.matcher import ContactMatcher
from execution.models import (
    ConflictChoice,
    EmailAddress,
    MatchCategory,
    NameDetails,
    OrganizationDetails,
    PhoneNumber,
    PostalAddress,
    UnifiedContact,
)


# =============================================================================
# FIXTURE GENERATORS
# =============================================================================

@pytest.fixture
def base_contact() -> UnifiedContact:
    return UnifiedContact(
        google_resource_name="people/c100",
        google_etag="etag_base_g",
        ms_id="ms_100",
        ms_etag="etag_base_m",
        name=NameDetails(given_name="John", family_name="Smith", display_name="John Smith"),
        emails=[EmailAddress(value="john.smith@example.com", type="work", is_primary=True)],
        phones=[PhoneNumber(value="+15551234567", type="mobile", is_primary=True)],
        organizations=[OrganizationDetails(company="Acme Corp", title="Developer")],
        addresses=[PostalAddress(street="100 Main St", city="Springfield", state="IL", postal_code="62701")],
        notes="Initial notes.",
    )


# =============================================================================
# 1. IDENTITY MATCHER TESTS (Section 4.1)
# =============================================================================

def test_matcher_stored_database_mapping() -> None:
    g = UnifiedContact(
        google_resource_name="people/c100",
        name=NameDetails(given_name="Alice", family_name="Wonderland"),
    )
    m = UnifiedContact(
        ms_id="ms_999",
        name=NameDetails(given_name="Alice", family_name="Unknown"),
    )
    mappings = {"people/c100": "ms_999"}

    breakdown = ContactMatcher.evaluate_pair(g, m, stored_mappings=mappings)
    assert breakdown.score == 100
    assert breakdown.category == MatchCategory.DEFINITE_MATCH
    assert "Database Mapping" in breakdown.matched_criteria


def test_matcher_email_confidence() -> None:
    g = UnifiedContact(
        name=NameDetails(given_name="John", family_name="Doe"),
        emails=[EmailAddress(value="John.Doe@Work.COM", type="work")],
    )
    m = UnifiedContact(
        name=NameDetails(given_name="J.", family_name="Doe"),
        emails=[EmailAddress(value="john.doe@work.com", type="work")],
    )
    breakdown = ContactMatcher.evaluate_pair(g, m)
    assert breakdown.score == 90
    assert breakdown.category == MatchCategory.DEFINITE_MATCH
    assert "Primary/Normalized Email" in breakdown.matched_criteria


def test_matcher_phone_confidence() -> None:
    g = UnifiedContact(
        name=NameDetails(given_name="Bob", family_name="Builder"),
        phones=[PhoneNumber(value="+1 (555) 333-4444", type="mobile")],
    )
    m = UnifiedContact(
        name=NameDetails(given_name="Robert", family_name="Builder"),
        phones=[PhoneNumber(value="5553334444", type="work")],
    )
    breakdown = ContactMatcher.evaluate_pair(g, m)
    assert breakdown.score == 85
    assert breakdown.category == MatchCategory.DEFINITE_MATCH
    assert "E.164 Phone Number" in breakdown.matched_criteria


def test_matcher_name_and_company_confidence() -> None:
    g = UnifiedContact(
        name=NameDetails(given_name="Sara", family_name="Connor"),
        organizations=[OrganizationDetails(company="Cyberdyne Systems")],
    )
    m = UnifiedContact(
        name=NameDetails(given_name="Sara", family_name="Connor"),
        organizations=[OrganizationDetails(company="cyberdyne systems")],
    )
    breakdown = ContactMatcher.evaluate_pair(g, m)
    assert breakdown.score == 85
    assert breakdown.category == MatchCategory.DEFINITE_MATCH
    assert "Name + Organization" in breakdown.matched_criteria


def test_matcher_name_only_confidence() -> None:
    g = UnifiedContact(
        name=NameDetails(given_name="Michael", family_name="Scott"),
    )
    m = UnifiedContact(
        name=NameDetails(given_name="Michael", family_name="Scott"),
    )
    breakdown = ContactMatcher.evaluate_pair(g, m)
    assert breakdown.score == 80
    assert breakdown.category == MatchCategory.DEFINITE_MATCH
    assert "Full Name (Non-Conflicting)" in breakdown.matched_criteria


def test_matcher_name_with_conflicting_email_guardrail() -> None:
    g = UnifiedContact(
        name=NameDetails(given_name="Michael", family_name="Scott"),
        emails=[EmailAddress(value="michael@dundermifflin.com")],
    )
    m = UnifiedContact(
        name=NameDetails(given_name="Michael", family_name="Scott"),
        emails=[EmailAddress(value="michael@vancerefrigeration.com")],
    )
    breakdown = ContactMatcher.evaluate_pair(g, m)
    assert breakdown.score == 40
    assert breakdown.category == MatchCategory.UNIQUE
    assert "Name Match (Conflicting Details)" in breakdown.matched_criteria


# =============================================================================
# 2. THREE-WAY DIFF & SYNC PLANNER TESTS (Section 4.2)
# =============================================================================

def test_clean_one_sided_additions() -> None:
    """Case 1: Record exists on Google only, absent in MS and Base -> Create in MS.
               Record exists on MS only, absent in Google and Base -> Create in Google.
    """
    g_new = UnifiedContact(
        google_resource_name="people/c_new_1",
        name=NameDetails(given_name="Google", family_name="Exclusive"),
        emails=[EmailAddress(value="exclusive.google@example.com")],
    )
    ms_new = UnifiedContact(
        ms_id="ms_new_1",
        name=NameDetails(given_name="Microsoft", family_name="Exclusive"),
        emails=[EmailAddress(value="exclusive.ms@example.com")],
    )

    plan = SyncPlanner.plan_sync(
        google_contacts=[g_new],
        ms_contacts=[ms_new],
        base_contacts={},
        stored_mappings={},
    )

    assert len(plan.to_create_in_ms) == 1
    assert plan.to_create_in_ms[0].google_resource_name == "people/c_new_1"

    assert len(plan.to_create_in_google) == 1
    assert plan.to_create_in_google[0].ms_id == "ms_new_1"

    assert len(plan.to_update_in_google) == 0
    assert len(plan.to_update_in_ms) == 0
    assert len(plan.conflicts) == 0


def test_safe_field_merge(base_contact: UnifiedContact) -> None:
    """Case 2: Safe updates and non-overlapping field additions.
       - Microsoft added job title 'Principal Engineer' (Base had 'Developer', Google unchanged).
       - Google added personal notes 'VIP Client' (Base had 'Initial notes.', MS unchanged).
    """
    g_curr = base_contact.model_copy(deep=True)
    g_curr.notes = "VIP Client"  # Google updated notes

    ms_curr = base_contact.model_copy(deep=True)
    ms_curr.organizations[0].title = "Principal Engineer"  # MS updated title

    plan = SyncPlanner.plan_sync(
        google_contacts=[g_curr],
        ms_contacts=[ms_curr],
        base_contacts={"people/c100": base_contact},
        stored_mappings={"people/c100": "ms_100"},
    )

    assert len(plan.conflicts) == 0

    # Google should receive the title update from Microsoft
    assert len(plan.to_update_in_google) == 1
    g_action = plan.to_update_in_google[0]
    assert g_action.target_provider == "google"
    assert g_action.contact_id == "people/c100"
    assert g_action.updated_contact.organizations[0].title == "principal engineer"

    # Microsoft should receive the notes update from Google
    assert len(plan.to_update_in_ms) == 1
    ms_action = plan.to_update_in_ms[0]
    assert ms_action.target_provider == "microsoft"
    assert ms_action.contact_id == "ms_100"
    assert ms_action.updated_contact.notes == "VIP Client"


def test_hard_conflict_different_phones(base_contact: UnifiedContact) -> None:
    """Case 3: Hard conflict on phone number.
       - Google updated phone to (555) 111-2222
       - Microsoft updated phone to (555) 999-8888
       - Both diverged from Base (+15551234567).
    """
    g_curr = base_contact.model_copy(deep=True)
    g_curr.phones = [PhoneNumber(value="+15551112222", type="mobile")]

    ms_curr = base_contact.model_copy(deep=True)
    ms_curr.phones = [PhoneNumber(value="+15559998888", type="mobile")]

    plan = SyncPlanner.plan_sync(
        google_contacts=[g_curr],
        ms_contacts=[ms_curr],
        base_contacts={"people/c100": base_contact},
        stored_mappings={"people/c100": "ms_100"},
        auto_resolve_conflicts=False,
    )

    # Must isolate the conflict and generate ZERO updates for this contact
    assert len(plan.conflicts) == 1
    conflict = plan.conflicts[0]
    assert conflict.contact_name == "John Smith"
    assert "primary_phone" in conflict.conflicting_fields
    assert conflict.conflicting_fields["primary_phone"]["google"] == "+15551112222"
    assert conflict.conflicting_fields["primary_phone"]["microsoft"] == "+15559998888"
    assert conflict.conflicting_fields["primary_phone"]["base"] == "+15551234567"
    assert ConflictChoice.KEEP_BOTH in conflict.available_choices

    assert len(plan.to_update_in_google) == 0
    assert len(plan.to_update_in_ms) == 0


def test_identical_record_no_op(base_contact: UnifiedContact) -> None:
    """Case 4: Identical record on both providers and base state (no-op)."""
    g_curr = base_contact.model_copy(deep=True)
    ms_curr = base_contact.model_copy(deep=True)

    plan = SyncPlanner.plan_sync(
        google_contacts=[g_curr],
        ms_contacts=[ms_curr],
        base_contacts={"people/c100": base_contact},
        stored_mappings={"people/c100": "ms_100"},
    )

    assert plan.unchanged_count == 1
    assert len(plan.to_update_in_google) == 0
    assert len(plan.to_update_in_ms) == 0
    assert len(plan.to_create_in_google) == 0
    assert len(plan.to_create_in_ms) == 0
    assert len(plan.conflicts) == 0


def test_deletion_safety_policy(base_contact: UnifiedContact) -> None:
    """Case 5: Deletion safety enforcement.
       - Contact was previously mapped.
       - Deleted in Microsoft (missing in ms_contacts).
       - When allow_deletions = False: Held as ORPHANED_IN_PROVIDER without deleting.
       - When allow_deletions = True: Plan to_delete_in_google.
    """
    g_curr = base_contact.model_copy(deep=True)
    stored_mappings = {"people/c100": "ms_100"}

    # Sub-test 5A: allow_deletions = False (default safe behavior)
    safe_plan = SyncPlanner.plan_sync(
        google_contacts=[g_curr],
        ms_contacts=[],  # Deleted in MS
        base_contacts={"people/c100": base_contact},
        stored_mappings=stored_mappings,
        allow_deletions=False,
    )

    assert len(safe_plan.to_delete_in_google) == 0
    assert len(safe_plan.orphaned) == 1
    assert safe_plan.orphaned[0].provider == "google"
    assert safe_plan.orphaned[0].status == "ORPHANED_IN_PROVIDER"
    assert len(safe_plan.to_create_in_ms) == 0

    # Sub-test 5B: allow_deletions = True (explicit opt-in)
    delete_plan = SyncPlanner.plan_sync(
        google_contacts=[g_curr],
        ms_contacts=[],  # Deleted in MS
        base_contacts={"people/c100": base_contact},
        stored_mappings=stored_mappings,
        allow_deletions=True,
    )

    assert len(delete_plan.to_delete_in_google) == 1
    assert delete_plan.to_delete_in_google[0] == "people/c100"
    assert len(delete_plan.orphaned) == 0


def test_fourteen_contacts_sync_scenarios() -> None:
    """Detailed testing with 14 contacts:
    - 13 contacts are identical on Google and Microsoft.
    - 14th contact is tested in different states:
      a) Clean new contact in Google -> plan creates in MS
      b) Clean new contact in MS -> plan creates in Google
      c) Hard conflict with policy 'skip' -> skipped, 13 unchanged
      d) Hard conflict with policy 'google_wins' -> MS updated
      e) Hard conflict with policy 'microsoft_wins' -> Google updated
      f) Hard conflict with policy 'keep_both' -> both updated/merged
    """
    from execution.sync_service import SyncService
    from execution.database import create_db_engine, Base
    from sqlalchemy.orm import sessionmaker

    # 13 identical contacts
    g_13: list[UnifiedContact] = []
    ms_13: list[UnifiedContact] = []
    mappings_13: dict[str, str] = {}

    for i in range(1, 14):
        g_c = UnifiedContact(
            google_resource_name=f"people/c_{i}",
            name=NameDetails(given_name=f"Person{i}", family_name="Tester"),
            emails=[EmailAddress(value=f"person{i}@example.com", is_primary=True)],
            phones=[PhoneNumber(value=f"+155500000{i:02d}", is_primary=True)],
        )
        ms_c = UnifiedContact(
            ms_id=f"ms_id_{i}",
            name=NameDetails(given_name=f"Person{i}", family_name="Tester"),
            emails=[EmailAddress(value=f"person{i}@example.com", is_primary=True)],
            phones=[PhoneNumber(value=f"+155500000{i:02d}", is_primary=True)],
        )
        g_13.append(g_c)
        ms_13.append(ms_c)
        mappings_13[f"people/c_{i}"] = f"ms_id_{i}"

    # Verify 13 identical contacts produces exactly 13 unchanged
    plan_base = SyncPlanner.plan_sync(
        google_contacts=g_13,
        ms_contacts=ms_13,
        stored_mappings=mappings_13,
    )
    assert plan_base.unchanged_count == 13
    assert len(plan_base.to_create_in_google) == 0
    assert len(plan_base.to_create_in_ms) == 0
    assert len(plan_base.to_update_in_google) == 0
    assert len(plan_base.to_update_in_ms) == 0
    assert len(plan_base.conflicts) == 0

    # Scenario A: 14th contact exists only in Google
    g_14_only = UnifiedContact(
        google_resource_name="people/c_14",
        name=NameDetails(given_name="Fourteenth", family_name="GoogleOnly"),
        emails=[EmailAddress(value="fourteen.g@example.com", is_primary=True)],
    )
    plan_a = SyncPlanner.plan_sync(
        google_contacts=g_13 + [g_14_only],
        ms_contacts=ms_13,
        stored_mappings=mappings_13,
    )
    assert plan_a.unchanged_count == 13
    assert len(plan_a.to_create_in_ms) == 1
    assert plan_a.to_create_in_ms[0].name.given_name == "Fourteenth"

    # Scenario B: 14th contact exists only in Microsoft
    ms_14_only = UnifiedContact(
        ms_id="ms_id_14",
        name=NameDetails(given_name="Fourteenth", family_name="MsOnly"),
        emails=[EmailAddress(value="fourteen.ms@example.com", is_primary=True)],
    )
    plan_b = SyncPlanner.plan_sync(
        google_contacts=g_13,
        ms_contacts=ms_13 + [ms_14_only],
        stored_mappings=mappings_13,
    )
    assert plan_b.unchanged_count == 13
    assert len(plan_b.to_create_in_google) == 1
    assert plan_b.to_create_in_google[0].name.given_name == "Fourteenth"

    # Scenario C: 14th contact has conflicting phone numbers
    g_14_conf = UnifiedContact(
        google_resource_name="people/c_14",
        name=NameDetails(given_name="Fourteenth", family_name="Conflicted"),
        emails=[EmailAddress(value="fourteen@example.com", is_primary=True)],
        phones=[PhoneNumber(value="+15551111111", is_primary=True)],
    )
    ms_14_conf = UnifiedContact(
        ms_id="ms_id_14",
        name=NameDetails(given_name="Fourteenth", family_name="Conflicted"),
        emails=[EmailAddress(value="fourteen@example.com", is_primary=True)],
        phones=[PhoneNumber(value="+15552222222", is_primary=True)],
    )
    mappings_14 = {**mappings_13, "people/c_14": "ms_id_14"}

    plan_c = SyncPlanner.plan_sync(
        google_contacts=g_13 + [g_14_conf],
        ms_contacts=ms_13 + [ms_14_conf],
        stored_mappings=mappings_14,
        auto_resolve_conflicts=False,
    )
    assert plan_c.unchanged_count == 13
    assert len(plan_c.conflicts) == 1

    # Test executing full sync via SyncService with live_google / live_ms
    from sqlalchemy.orm import sessionmaker
    from execution.database import create_db_engine, Base
    engine = create_db_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = SessionLocal()

    service = SyncService(db_session=session)
    user = service.repo.get_or_create_user("test14@example.com")
    for gid, mid in mappings_14.items():
        service.repo.upsert_contact_mapping(user.id, gid, mid, "hash_init")

    # Sync with policy 'skip' -> 13 unchanged, 0 updated, 0 created
    res_skip = service.execute(
        user_id=user.id,
        auto_resolve_policy="skip",
        live_google=g_13 + [g_14_conf],
        live_ms=ms_13 + [ms_14_conf],
    )
    assert res_skip["unchanged"] == 13
    assert res_skip["created_google"] == 0
    assert res_skip["created_ms"] == 0
    assert res_skip["updated_google"] == 0
    assert res_skip["updated_ms"] == 0

    # Sync with policy 'google_wins' -> 13 unchanged, updated_ms = 1
    res_gwins = service.execute(
        user_id=user.id,
        auto_resolve_policy="google_wins",
        live_google=g_13 + [g_14_conf],
        live_ms=ms_13 + [ms_14_conf],
    )
    assert res_gwins["unchanged"] == 13
    assert res_gwins["updated_ms"] == 1

    # Sync with policy 'microsoft_wins' -> 13 unchanged, updated_google = 1
    res_mwins = service.execute(
        user_id=user.id,
        auto_resolve_policy="microsoft_wins",
        live_google=g_13 + [g_14_conf],
        live_ms=ms_13 + [ms_14_conf],
    )
    assert res_mwins["unchanged"] == 13
    assert res_mwins["updated_google"] == 1

    # Sync with policy 'keep_both' -> 13 unchanged, updated_google = 1, updated_ms = 1
    res_both = service.execute(
        user_id=user.id,
        auto_resolve_policy="keep_both",
        live_google=g_13 + [g_14_conf],
        live_ms=ms_13 + [ms_14_conf],
    )
    assert res_both["unchanged"] == 13
    assert res_both["updated_google"] == 1
    assert res_both["updated_ms"] == 1


def test_two_way_deletion_and_update_sync() -> None:
    """Verifies:
    1. Deletion in Google propagates to delete in Microsoft.
    2. Deletion in Microsoft propagates to delete in Google.
    3. Two-way field updates propagate in both directions.
    """
    from execution.sync_service import SyncService
    from execution.database import create_db_engine, Base
    from sqlalchemy.orm import sessionmaker

    engine = create_db_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = SessionLocal()

    service = SyncService(db_session=session)
    user = service.repo.get_or_create_user("twoway@example.com")

    # Setup contact 1 (mapped) and contact 2 (mapped)
    g_1 = UnifiedContact(
        google_resource_name="people/c_1",
        name=NameDetails(given_name="Alice", family_name="Smith"),
        emails=[EmailAddress(value="alice@example.com", is_primary=True)],
    )
    ms_1 = UnifiedContact(
        ms_id="ms_1",
        name=NameDetails(given_name="Alice", family_name="Smith"),
        emails=[EmailAddress(value="alice@example.com", is_primary=True)],
    )

    g_2 = UnifiedContact(
        google_resource_name="people/c_2",
        name=NameDetails(given_name="Bob", family_name="Jones"),
        emails=[EmailAddress(value="bob@example.com", is_primary=True)],
    )
    ms_2 = UnifiedContact(
        ms_id="ms_2",
        name=NameDetails(given_name="Bob", family_name="Jones"),
        emails=[EmailAddress(value="bob@example.com", is_primary=True)],
    )

    service.repo.upsert_contact_mapping(user.id, "people/c_1", "ms_1", "hash_1")
    service.repo.upsert_contact_mapping(user.id, "people/c_2", "ms_2", "hash_2")

    # 1. User deletes Alice from Google (g_contacts has only Bob; ms_contacts has Alice & Bob)
    res_del_g = service.execute(
        user_id=user.id,
        live_google=[g_2],
        live_ms=[ms_1, ms_2],
    )
    assert res_del_g["deleted_ms"] == 1
    assert res_del_g["unchanged"] == 1  # Bob is unchanged
    # Verify mapping was deleted
    mappings_after = service.repo.get_mapping_dict(user.id)
    assert "people/c_1" not in mappings_after

    # 2. User deletes Bob from Microsoft (g_contacts has Bob; ms_contacts is empty)
    res_del_ms = service.execute(
        user_id=user.id,
        live_google=[g_2],
        live_ms=[],
    )
    assert res_del_ms["deleted_google"] == 1
    assert "people/c_2" not in service.repo.get_mapping_dict(user.id)

    # 3. Two-way updates:
    # Google adds phone to Contact 3; Microsoft adds company to Contact 4
    g_3 = UnifiedContact(
        google_resource_name="people/c_3",
        name=NameDetails(given_name="Charlie", family_name="Brown"),
        phones=[PhoneNumber(value="+15551234567", is_primary=True)],
    )
    ms_3 = UnifiedContact(
        ms_id="ms_3",
        name=NameDetails(given_name="Charlie", family_name="Brown"),
    )

    g_4 = UnifiedContact(
        google_resource_name="people/c_4",
        name=NameDetails(given_name="Diana", family_name="Prince"),
    )
    ms_4 = UnifiedContact(
        ms_id="ms_4",
        name=NameDetails(given_name="Diana", family_name="Prince"),
        organizations=[OrganizationDetails(company="Acme Corp")],
    )

    service.repo.upsert_contact_mapping(user.id, "people/c_3", "ms_3", "hash_3")
    service.repo.upsert_contact_mapping(user.id, "people/c_4", "ms_4", "hash_4")

    res_updates = service.execute(
        user_id=user.id,
        live_google=[g_3, g_4],
        live_ms=[ms_3, ms_4],
    )
    # g_3 phone propagates to MS -> updated_ms = 1
    # ms_4 company propagates to Google -> updated_google = 1
    assert res_updates["updated_ms"] == 1
    assert res_updates["updated_google"] == 1


def test_last_writer_wins_resolution() -> None:
    """Verifies that Last-Writer-Wins automatically resolves conflicts by comparing timestamps."""
    from datetime import datetime, timezone, timedelta
    from execution.database import create_db_engine, Base
    from execution.sync_service import SyncService
    from sqlalchemy.orm import sessionmaker

    engine = create_db_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db_session = SessionLocal()

    t_older = datetime.now(timezone.utc) - timedelta(hours=1)
    t_newer = datetime.now(timezone.utc)

    # Base state
    base_c = UnifiedContact(
        google_resource_name="people/c_win",
        ms_id="ms_win",
        name=NameDetails(given_name="Eva", family_name="Winner"),
        phones=[PhoneNumber(value="+15551111111", is_primary=True)],
    )

    # Google modified at t_newer (winner)
    g_c = UnifiedContact(
        google_resource_name="people/c_win",
        last_modified=t_newer,
        name=NameDetails(given_name="Eva", family_name="Winner"),
        phones=[PhoneNumber(value="+15552222222", is_primary=True)],
    )

    # MS modified at t_older (loser)
    ms_c = UnifiedContact(
        ms_id="ms_win",
        last_modified=t_older,
        name=NameDetails(given_name="Eva", family_name="Winner"),
        phones=[PhoneNumber(value="+15553333333", is_primary=True)],
    )

    service = SyncService(db_session=db_session)
    user = service.repo.get_or_create_user(email="lww_test@example.com", sync_policy="last_writer_wins")
    service.repo.upsert_contact_mapping(user.id, "people/c_win", "ms_win", base_c.compute_field_hash())

    res = service.execute(
        user_id=user.id,
        auto_resolve_policy="last_writer_wins",
        live_google=[g_c],
        live_ms=[ms_c],
    )
    # Google had newer timestamp -> MS updated with Google value
    assert res["status"] == "SUCCESS"
    assert res["updated_ms"] == 1
    assert res["conflicts_held"] == 0


def test_last_writer_wins_modify_vs_delete() -> None:
    """Verifies that if MS was modified after baseline sync, it wins over Google deletion."""
    from datetime import datetime, timezone, timedelta
    from execution.database import create_db_engine, Base
    from execution.sync_service import SyncService
    from sqlalchemy.orm import sessionmaker

    engine = create_db_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db_session = SessionLocal()

    t_synced = datetime.now(timezone.utc) - timedelta(hours=2)
    t_ms_modified = datetime.now(timezone.utc) - timedelta(minutes=10)

    service = SyncService(db_session=db_session)
    user = service.repo.get_or_create_user(email="mvd_test@example.com", sync_policy="last_writer_wins")
    mapping = service.repo.upsert_contact_mapping(user.id, "people/c_mvd", "ms_mvd", "hash_mvd")
    mapping.last_synced_at = t_synced
    db_session.commit()

    # Contact is missing in Google (deleted from Google), but modified in MS at t_ms_modified > t_synced
    ms_c = UnifiedContact(
        ms_id="ms_mvd",
        last_modified=t_ms_modified,
        name=NameDetails(given_name="Frank", family_name="Castle"),
        phones=[PhoneNumber(value="+15559998888", is_primary=True)],
    )

    res = service.execute(
        user_id=user.id,
        auto_resolve_policy="last_writer_wins",
        live_google=[],
        live_ms=[ms_c],
    )
    # MS update was newer than base sync -> created back in Google
    assert res["created_google"] == 1
    assert res["deleted_ms"] == 0


def test_ms_to_google_update_propagation() -> None:
    """Verifies that updating a contact in Microsoft propagates to Google."""
    t_google_old = datetime.now(timezone.utc) - timedelta(hours=3)
    t_ms_new = datetime.now(timezone.utc) - timedelta(minutes=5)

    g_contact = UnifiedContact(
        google_resource_name="people/c_prop",
        google_etag="etag_g_123",
        last_modified=t_google_old,
        name=NameDetails(given_name="Sarah", family_name="Connor"),
        phones=[PhoneNumber(value="+15551112222", is_primary=True)],
    )

    ms_contact = UnifiedContact(
        ms_id="ms_prop_123",
        ms_etag="etag_ms_123",
        last_modified=t_ms_new,
        name=NameDetails(given_name="Sarah", family_name="Connor"),
        phones=[PhoneNumber(value="+15559990000", is_primary=True)],  # Updated in MS
    )

    plan = SyncPlanner.plan_sync(
        google_contacts=[g_contact],
        ms_contacts=[ms_contact],
        stored_mappings={"people/c_prop": "ms_prop_123"},
        auto_resolve_conflicts=True,
    )

    assert len(plan.to_update_in_google) == 1
    assert len(plan.to_update_in_ms) == 0
    assert plan.to_update_in_google[0].contact_id == "people/c_prop"
    assert plan.to_update_in_google[0].updated_contact.phones[0].value == "+15559990000"



