"""Test script simulating the Google ADK Agent guided interaction flow."""
from __future__ import annotations

import pytest

from execution.agent import ActionType, SyncGMAgent
from execution.models import (
    EmailAddress,
    NameDetails,
    OrganizationDetails,
    PhoneNumber,
    PostalAddress,
    UnifiedContact,
)
from execution.tools import InMemorySyncStore


@pytest.fixture
def mock_store() -> InMemorySyncStore:
    store = InMemorySyncStore()
    user_id = "user_sylvester"

    # Base Contact State
    base = UnifiedContact(
        google_resource_name="people/c101",
        ms_id="ms_101",
        name=NameDetails(given_name="John", family_name="Smith", display_name="John Smith"),
        emails=[EmailAddress(value="john.smith@acme.com", type="work", is_primary=True)],
        phones=[PhoneNumber(value="+15550000000", type="mobile", is_primary=True)],
        organizations=[OrganizationDetails(company="Acme Corp", title="Architect")],
    )

    # Google State: Conflicting Phone A: (555) 123-4567
    g_contact = base.model_copy(deep=True)
    g_contact.phones = [PhoneNumber(value="+15551234567", type="mobile")]

    # Microsoft State: Conflicting Phone B: (555) 987-6543
    ms_contact = base.model_copy(deep=True)
    ms_contact.phones = [PhoneNumber(value="+15559876543", type="mobile")]

    store.set_user_contacts(
        user_id=user_id,
        google=[g_contact],
        ms=[ms_contact],
        base={"people/c101": base},
        mappings={"people/c101": "ms_101"},
    )
    return store


def test_agent_welcome_prompt(mock_store: InMemorySyncStore) -> None:
    agent = SyncGMAgent(store=mock_store)
    response = agent.get_welcome_prompt("user_sylvester", user_name="Sylvester Moore El")

    assert "Hello **Sylvester Moore El**" in response.message
    assert len(response.action_chips) == 5
    chip_labels = [c.label for c in response.action_chips]
    assert "Sync Now" in chip_labels
    assert "Preview Changes (Dry Run)" in chip_labels
    assert "Compare Contacts" in chip_labels
    assert "Configure Schedule" in chip_labels


def test_agent_preview_and_conflict_resolution_flow(mock_store: InMemorySyncStore) -> None:
    """Simulates the 5-step interactive conflict resolution flow:
    1. User triggers [Preview Changes].
    2. Engine returns a conflict.
    3. Agent renders conflict card with buttons.
    4. User clicks [Keep Both].
    5. Agent calls resolve_conflict_item() and confirms resolution.
    """
    user_id = "user_sylvester"
    agent = SyncGMAgent(store=mock_store)

    # Step 1: User triggers [Preview Changes]
    preview_resp = agent.handle_action(user_id=user_id, action=ActionType.PREVIEW_CHANGES)

    # Step 2 & 3: Agent renders summary and side-by-side conflict card
    assert "Synchronization Preview (Dry Run)" in preview_resp.message
    assert len(preview_resp.conflict_cards) == 1

    card = preview_resp.conflict_cards[0]
    assert card.contact_name == "John Smith"

    # Verify side-by-side diff fields
    diff_field_names = [d.field_name for d in card.field_diffs]
    assert "Primary Phone" in diff_field_names
    phone_diff = next(d for d in card.field_diffs if d.field_name == "Primary Phone")
    assert phone_diff.google_value == "+15551234567"
    assert phone_diff.microsoft_value == "+15559876543"

    # Verify mandatory action buttons
    button_labels = [b.label for b in card.action_buttons]
    assert "Keep Both (Store one as Secondary)" in button_labels
    assert "Use Google Info" in button_labels
    assert "Use Microsoft Info" in button_labels
    assert "Skip / Leave Unchanged" in button_labels

    # Step 4: Simulated user clicks [Keep Both]
    keep_both_button = next(b for b in card.action_buttons if b.payload.get("resolution_choice") == "keep_both")

    # Step 5: Agent invokes resolve_conflict_item and returns confirmation
    resolve_resp = agent.handle_action(
        user_id=user_id,
        action=keep_both_button.action,
        payload=keep_both_button.payload,
    )

    assert "Successfully resolved conflict for 'John Smith'" in resolve_resp.message
    assert len(resolve_resp.conflict_cards) == 0  # No remaining conflicts

    # Verify internal store state: Both phones should now be preserved
    updated_g = mock_store.google_contacts[user_id][0]
    updated_ms = mock_store.ms_contacts[user_id][0]

    g_phones = [p.value for p in updated_g.phones]
    ms_phones = [p.value for p in updated_ms.phones]
    assert "+15551234567" in g_phones and "+15559876543" in g_phones
    assert "+15551234567" in ms_phones and "+15559876543" in ms_phones


def test_agent_compare_and_schedule_flow(mock_store: InMemorySyncStore) -> None:
    user_id = "user_sylvester"
    agent = SyncGMAgent(store=mock_store)

    # Compare Contacts Flow
    compare_resp = agent.handle_action(user_id=user_id, action=ActionType.COMPARE_CONTACTS)
    assert "Contact Store Comparison" in compare_resp.message
    assert "- **Total in Google:** 1" in compare_resp.message
    assert "- **Total in Microsoft:** 1" in compare_resp.message

    # Schedule Configuration Flow
    sched_resp = agent.handle_action(user_id=user_id, action=ActionType.CONFIGURE_SCHEDULE)
    assert "Automated Background Sync Schedule" in sched_resp.message
    assert len(sched_resp.action_chips) == 5

    # Set Schedule to Hourly
    set_sched_resp = agent.handle_action(
        user_id=user_id,
        action=ActionType.SET_SCHEDULE,
        payload={"frequency": "hourly", "enabled": True},
    )
    assert "Scheduled sync is now ENABLED (hourly)" in set_sched_resp.message
    assert mock_store.schedule_config[user_id]["enabled"] is True
    assert mock_store.schedule_config[user_id]["frequency"] == "hourly"


def test_agent_set_policy_flow(mock_store: InMemorySyncStore) -> None:
    user_id = "user_sylvester"
    agent = SyncGMAgent(store=mock_store)

    resp = agent.handle_action(
        user_id=user_id,
        action=ActionType.SET_POLICY,
        payload={"policy": "google_wins"},
    )
    assert "Conflict resolution preference updated to: **Prioritize Google**" in resp.message
    assert resp.data["sync_policy"] == "google_wins"
