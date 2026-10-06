"""Deterministic 3-Way Differential Engine & Sync Planner (Section 4.2)."""
from __future__ import annotations

import copy
from datetime import datetime, timezone
from typing import Any, Optional
import uuid

from execution.matcher import ContactMatcher


def ensure_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """Ensures a datetime object is timezone-aware and normalized to UTC."""
    if not dt:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)
from execution.models import (
    ConflictChoice,
    ConflictItem,
    ContactMatchPair,
    ContactUpdateAction,
    EmailAddress,
    FieldPatch,
    MatchCategory,
    MatchingResult,
    NameDetails,
    OrganizationDetails,
    OrphanedContact,
    PhoneNumber,
    PostalAddress,
    SyncPlan,
    UnifiedContact,
    normalize_email,
    normalize_phone,
    normalize_text,
)


class ThreeWayDiffEngine:
    """Computes differential sync plans across Base, Google, and Microsoft contact states."""

    @staticmethod
    def _extract_field_map(contact: Optional[UnifiedContact]) -> dict[str, Any]:
        """Extracts key-value fields from a UnifiedContact preserving original casing."""
        if not contact:
            return {
                "given_name": None,
                "family_name": None,
                "middle_name": None,
                "prefix": None,
                "suffix": None,
                "primary_email": None,
                "primary_phone": None,
                "company": None,
                "department": None,
                "title": None,
                "street": None,
                "city": None,
                "state": None,
                "postal_code": None,
                "country": None,
                "notes": None,
            }

        org = contact.primary_organization
        addr = contact.addresses[0] if contact.addresses else None

        return {
            "given_name": contact.name.given_name,
            "family_name": contact.name.family_name,
            "middle_name": contact.name.middle_name,
            "prefix": contact.name.prefix,
            "suffix": contact.name.suffix,
            "primary_email": contact.primary_email,
            "primary_phone": contact.primary_phone,
            "company": org.company if (org and org.company) else None,
            "department": org.department if (org and org.department) else None,
            "title": org.title if (org and org.title) else None,
            "street": addr.street if (addr and addr.street) else None,
            "city": addr.city if (addr and addr.city) else None,
            "state": addr.state if (addr and addr.state) else None,
            "postal_code": addr.postal_code if (addr and addr.postal_code) else None,
            "country": addr.country if (addr and addr.country) else None,
            "notes": contact.notes.strip() if (contact.notes and contact.notes.strip()) else None,
        }

    @staticmethod
    def _normalize_field_val(field: str, val: Any) -> Any:
        """Helper to normalize field values for comparison without mutating source casing."""
        if val is None:
            return None
        if field == "primary_email":
            return normalize_email(str(val))
        elif field == "primary_phone":
            return normalize_phone(str(val))
        elif isinstance(val, str):
            return normalize_text(val)
        return val

    @classmethod
    def compare_pair(
        cls,
        google_contact: UnifiedContact,
        ms_contact: UnifiedContact,
        base_contact: Optional[UnifiedContact] = None,
    ) -> tuple[list[FieldPatch], list[FieldPatch], Optional[ConflictItem]]:
        """Performs a 3-way differential comparison for a paired contact.
        
        Returns:
            tuple: (google_patches, ms_patches, conflict_item_if_any)
        """
        f_base = cls._extract_field_map(base_contact)
        f_goog = cls._extract_field_map(google_contact)
        f_ms = cls._extract_field_map(ms_contact)

        google_patches: list[FieldPatch] = []
        ms_patches: list[FieldPatch] = []
        conflicting_fields: dict[str, dict[str, Any]] = {}

        all_fields = list(f_goog.keys())

        for field in all_fields:
            v_b = f_base.get(field)
            v_g = f_goog.get(field)
            v_m = f_ms.get(field)

            nb = cls._normalize_field_val(field, v_b)
            ng = cls._normalize_field_val(field, v_g)
            nm = cls._normalize_field_val(field, v_m)

            # 1. Normalized values on both providers already match
            if ng == nm:
                continue

            # 2. Both providers diverged from base (or both added different values when base was empty)
            if nb is None:
                # Field added on both sides differently
                if ng is not None and nm is not None and ng != nm:
                    conflicting_fields[field] = {"base": None, "google": v_g, "microsoft": v_m}
                    continue
                # Field added cleanly on Google only
                elif ng is not None and nm is None:
                    ms_patches.append(
                        FieldPatch(field_name=field, old_value=v_m, new_value=v_g, source_provider="google")
                    )
                    continue
                # Field added cleanly on Microsoft only
                elif nm is not None and ng is None:
                    google_patches.append(
                        FieldPatch(field_name=field, old_value=v_g, new_value=v_m, source_provider="microsoft")
                    )
                    continue

            # Base existed
            if ng != nb and nm != nb:
                # Both sides modified the same field to different values -> HARD CONFLICT
                conflicting_fields[field] = {"base": v_b, "google": v_g, "microsoft": v_m}
                continue

            if ng != nb and nm == nb:
                # Google updated this field, MS untouched -> Propagate to MS
                ms_patches.append(
                    FieldPatch(field_name=field, old_value=v_m, new_value=v_g, source_provider="google")
                )
                continue

            if nm != nb and ng == nb:
                # MS updated this field, Google untouched -> Propagate to Google
                google_patches.append(
                    FieldPatch(field_name=field, old_value=v_g, new_value=v_m, source_provider="microsoft")
                )
                continue

        # If any hard conflict exists on this record, isolate it and return no patches
        if conflicting_fields:
            contact_name = (
                google_contact.name.computed_display_name()
                or ms_contact.name.computed_display_name()
                or "Unknown Contact"
            )
            conflict_item = ConflictItem(
                id=str(uuid.uuid4()),
                contact_name=contact_name,
                google_resource_name=google_contact.google_resource_name,
                ms_id=ms_contact.ms_id,
                conflicting_fields=conflicting_fields,
                available_choices=[
                    ConflictChoice.LAST_WRITER_WINS,
                    ConflictChoice.KEEP_BOTH,
                    ConflictChoice.USE_GOOGLE,
                    ConflictChoice.USE_MICROSOFT,
                    ConflictChoice.SKIP,
                ],
                google_contact=google_contact,
                ms_contact=ms_contact,
                base_contact=base_contact,
            )
            return [], [], conflict_item

        return google_patches, ms_patches, None

    @classmethod
    def apply_patches_to_contact(
        cls,
        target_contact: UnifiedContact,
        patches: list[FieldPatch],
    ) -> UnifiedContact:
        """Applies a list of field patches to a UnifiedContact and returns the updated instance."""
        updated = copy.deepcopy(target_contact)

        for patch in patches:
            fname = patch.field_name
            val = patch.new_value

            if fname == "given_name":
                updated.name.given_name = val
            elif fname == "family_name":
                updated.name.family_name = val
            elif fname == "middle_name":
                updated.name.middle_name = val
            elif fname == "prefix":
                updated.name.prefix = val
            elif fname == "suffix":
                updated.name.suffix = val
            elif fname == "primary_email":
                if val:
                    if updated.emails:
                        updated.emails[0].value = val
                    else:
                        updated.emails.append(EmailAddress(value=val, type="work", is_primary=True))
            elif fname == "primary_phone":
                if val:
                    if updated.phones:
                        updated.phones[0].value = val
                    else:
                        updated.phones.append(PhoneNumber(value=val, type="mobile", is_primary=True))
            elif fname in ("company", "department", "title"):
                if not updated.organizations:
                    updated.organizations.append(OrganizationDetails())
                if fname == "company":
                    updated.organizations[0].company = val
                elif fname == "department":
                    updated.organizations[0].department = val
                elif fname == "title":
                    updated.organizations[0].title = val
            elif fname in ("street", "city", "state", "postal_code", "country"):
                if not updated.addresses:
                    updated.addresses.append(PostalAddress())
                if fname == "street":
                    updated.addresses[0].street = val
                elif fname == "city":
                    updated.addresses[0].city = val
                elif fname == "state":
                    updated.addresses[0].state = val
                elif fname == "postal_code":
                    updated.addresses[0].postal_code = val
                elif fname == "country":
                    updated.addresses[0].country = val
            elif fname == "notes":
                updated.notes = val

        return updated


class SyncPlanner:
    """Generates the full deterministic SyncPlan for two contact collections and base snapshot state."""

    @classmethod
    def plan_sync(
        cls,
        google_contacts: list[UnifiedContact],
        ms_contacts: list[UnifiedContact],
        base_contacts: Optional[dict[str, UnifiedContact]] = None,
        stored_mappings: Optional[dict[str, str]] = None,
        base_timestamps: Optional[dict[str, datetime]] = None,
        allow_deletions: bool = False,
        auto_resolve_conflicts: bool = True,
    ) -> SyncPlan:
        """Evaluates inputs and constructs a full, idempotent SyncPlan."""
        base_map = base_contacts or {}
        mappings = stored_mappings or {}
        timestamps = base_timestamps or {}

        # 1. Run Identity Matching
        matching_res: MatchingResult = ContactMatcher.match_collections(
            google_contacts=google_contacts,
            ms_contacts=ms_contacts,
            stored_mappings=mappings,
        )

        plan = SyncPlan()

        # 2. Process Definite Matches (3-Way Differential Merge)
        for match in matching_res.definite_matches:
            g_contact = match.google_contact
            ms_contact = match.ms_contact

            # Always preserve identity mapping baseline for all definite matches (even if unchanged)
            g_id = g_contact.google_resource_name
            ms_id = ms_contact.ms_id
            if g_id and ms_id:
                plan.persisted_mappings.append((g_id, ms_id, g_contact.compute_field_hash()))

            # Lookup Base snapshot if available
            base_key = g_contact.google_resource_name or ms_contact.ms_id
            base_contact = base_map.get(base_key) if base_key else None

            g_patches, ms_patches, conflict = ThreeWayDiffEngine.compare_pair(
                google_contact=g_contact,
                ms_contact=ms_contact,
                base_contact=base_contact,
            )

            if conflict:
                if not auto_resolve_conflicts:
                    plan.conflicts.append(conflict)
                    continue

                # Automatically resolve field differences using Last-Writer-Wins timestamps
                g_lm = ensure_utc(g_contact.last_modified)
                ms_lm = ensure_utc(ms_contact.last_modified)

                resolved_g_patches = list(g_patches)
                resolved_ms_patches = list(ms_patches)

                if ms_lm and g_lm:
                    if ms_lm > g_lm:
                        for fname, vals in conflict.conflicting_fields.items():
                            resolved_g_patches.append(
                                FieldPatch(field_name=fname, old_value=vals.get("google"), new_value=vals.get("microsoft"), source_provider="microsoft")
                            )
                    else:
                        for fname, vals in conflict.conflicting_fields.items():
                            resolved_ms_patches.append(
                                FieldPatch(field_name=fname, old_value=vals.get("microsoft"), new_value=vals.get("google"), source_provider="google")
                            )
                elif ms_lm and not g_lm:
                    for fname, vals in conflict.conflicting_fields.items():
                        resolved_g_patches.append(
                            FieldPatch(field_name=fname, old_value=vals.get("google"), new_value=vals.get("microsoft"), source_provider="microsoft")
                        )
                else:
                    for fname, vals in conflict.conflicting_fields.items():
                        resolved_ms_patches.append(
                            FieldPatch(field_name=fname, old_value=vals.get("microsoft"), new_value=vals.get("google"), source_provider="google")
                        )

                if resolved_g_patches:
                    updated_g = ThreeWayDiffEngine.apply_patches_to_contact(g_contact, resolved_g_patches)
                    plan.to_update_in_google.append(
                        ContactUpdateAction(
                            target_provider="google",
                            contact_id=g_contact.google_resource_name or "",
                            etag=g_contact.google_etag,
                            updated_contact=updated_g,
                            patches=resolved_g_patches,
                        )
                    )
                if resolved_ms_patches:
                    updated_ms = ThreeWayDiffEngine.apply_patches_to_contact(ms_contact, resolved_ms_patches)
                    plan.to_update_in_ms.append(
                        ContactUpdateAction(
                            target_provider="microsoft",
                            contact_id=ms_contact.ms_id or "",
                            etag=ms_contact.ms_etag,
                            updated_contact=updated_ms,
                            patches=resolved_ms_patches,
                        )
                    )
                continue

            if not g_patches and not ms_patches:
                plan.unchanged_count += 1
                continue

            if g_patches:
                updated_g = ThreeWayDiffEngine.apply_patches_to_contact(g_contact, g_patches)
                plan.to_update_in_google.append(
                    ContactUpdateAction(
                        target_provider="google",
                        contact_id=g_contact.google_resource_name or "",
                        etag=g_contact.google_etag,
                        updated_contact=updated_g,
                        patches=g_patches,
                    )
                )

            if ms_patches:
                updated_ms = ThreeWayDiffEngine.apply_patches_to_contact(ms_contact, ms_patches)
                plan.to_update_in_ms.append(
                    ContactUpdateAction(
                        target_provider="microsoft",
                        contact_id=ms_contact.ms_id or "",
                        etag=ms_contact.ms_etag,
                        updated_contact=updated_ms,
                        patches=ms_patches,
                    )
                )

        # 3. Process Potential Duplicates as Conflicts for User Review
        for dup in matching_res.potential_duplicates:
            g_c = dup.google_contact
            ms_c = dup.ms_contact
            c_name = g_c.name.computed_display_name() or ms_c.name.computed_display_name()
            plan.conflicts.append(
                ConflictItem(
                    id=str(uuid.uuid4()),
                    contact_name=c_name,
                    google_resource_name=g_c.google_resource_name,
                    ms_id=ms_c.ms_id,
                    conflicting_fields={
                        "identity_match": {
                            "score": dup.score,
                            "reason": dup.reason,
                            "google": g_c.name.computed_display_name(),
                            "microsoft": ms_c.name.computed_display_name(),
                        }
                    },
                    available_choices=[
                        ConflictChoice.LAST_WRITER_WINS,
                        ConflictChoice.KEEP_BOTH,
                        ConflictChoice.USE_GOOGLE,
                        ConflictChoice.USE_MICROSOFT,
                        ConflictChoice.SKIP,
                    ],
                    google_contact=g_c,
                    ms_contact=ms_c,
                )
            )

        # 4. Process Unmatched Google Contacts
        for g_c in matching_res.unmatched_google:
            # Check if this contact existed in Base (was deleted from Microsoft)
            g_id = g_c.google_resource_name
            if g_id and g_id in mappings:
                # Was mapped before, now missing in MS (deleted in MS)
                base_time = ensure_utc(timestamps.get(g_id))
                g_lm = ensure_utc(g_c.last_modified)
                # If Google contact was modified after baseline sync, Google update wins over MS deletion
                if base_time and g_lm and g_lm > base_time:
                    plan.to_create_in_ms.append(g_c)
                elif allow_deletions:
                    plan.to_delete_in_google.append(g_id)
                else:
                    plan.orphaned.append(
                        OrphanedContact(
                            contact=g_c,
                            provider="google",
                            reason="Contact existed in base mapping but is missing from Microsoft; held due to deletion safety policy.",
                        )
                    )
            else:
                # Clean new Google contact -> Create in Microsoft
                plan.to_create_in_ms.append(g_c)

        # 5. Process Unmatched Microsoft Contacts
        for ms_c in matching_res.unmatched_ms:
            ms_id = ms_c.ms_id
            mapped_google_id = next((gid for gid, mid in mappings.items() if mid == ms_id), None)
            if mapped_google_id:
                # Was mapped before, now missing in Google (deleted in Google)
                base_time = ensure_utc(timestamps.get(mapped_google_id) or (timestamps.get(ms_id) if ms_id else None))
                ms_lm = ensure_utc(ms_c.last_modified)
                # If MS contact was modified after baseline sync, MS update wins over Google deletion
                if base_time and ms_lm and ms_lm > base_time:
                    plan.to_create_in_google.append(ms_c)
                elif allow_deletions:
                    plan.to_delete_in_ms.append(ms_id)
                else:
                    plan.orphaned.append(
                        OrphanedContact(
                            contact=ms_c,
                            provider="microsoft",
                            reason="Contact existed in base mapping but is missing from Google; held due to deletion safety policy.",
                        )
                    )
            else:
                # Clean new Microsoft contact -> Create in Google
                plan.to_create_in_google.append(ms_c)

        return plan
