"""Contact Identity Matcher with Weighted Confidence Scoring (Section 4.1)."""
from __future__ import annotations

import re
from typing import Optional
from execution.models import (
    ContactMatchPair,
    MatchBreakdown,
    MatchCategory,
    MatchingResult,
    UnifiedContact,
    normalize_email,
    normalize_phone,
    normalize_text,
)


def phones_match(p1: str, p2: str) -> bool:
    """Checks if two phone numbers match, handling E.164 with or without country code."""
    if not p1 or not p2:
        return False
    if p1 == p2:
        return True
    d1 = re.sub(r"\D", "", p1)
    d2 = re.sub(r"\D", "", p2)
    if not d1 or not d2:
        return False
    if d1 == d2:
        return True
    # If one has country code (e.g., US '1') and the other is 10 digits
    if len(d1) >= 10 and len(d2) >= 10:
        return d1[-10:] == d2[-10:]
    return False


class ContactMatcher:
    """Evaluates identity similarity between Google Contacts and Microsoft Contacts."""

    SCORE_STORED_MAPPING = 100
    SCORE_PRIMARY_EMAIL = 90
    SCORE_PHONE_NUMBER = 70
    SCORE_NAME_AND_COMPANY = 60
    SCORE_FULL_NAME_ONLY = 30

    @classmethod
    def evaluate_pair(
        cls,
        google_contact: UnifiedContact,
        ms_contact: UnifiedContact,
        stored_mappings: Optional[dict[str, str]] = None,
    ) -> MatchBreakdown:
        """Evaluates a single pair of contacts and computes a weighted confidence score (0-100)."""
        criteria: list[str] = []
        best_score = 0
        reasons: list[str] = []

        # 1. Stored Database Mapping (Score = 100)
        if stored_mappings and google_contact.google_resource_name and ms_contact.ms_id:
            mapped_ms_id = stored_mappings.get(google_contact.google_resource_name)
            if mapped_ms_id == ms_contact.ms_id:
                return MatchBreakdown(
                    score=cls.SCORE_STORED_MAPPING,
                    category=MatchCategory.DEFINITE_MATCH,
                    matched_criteria=["Database Mapping"],
                    reason=f"Verified database mapping found: {google_contact.google_resource_name} <-> {ms_contact.ms_id}",
                )

        # 2. Email Matching (Score = 90)
        g_emails = {e.value for e in google_contact.emails if e.value}
        ms_emails = {e.value for e in ms_contact.emails if e.value}
        common_emails = g_emails.intersection(ms_emails)

        if common_emails:
            best_score = max(best_score, cls.SCORE_PRIMARY_EMAIL)
            criteria.append("Primary/Normalized Email")
            reasons.append(f"Matching email: {', '.join(sorted(common_emails))}")

        # 3. Phone Number Matching (Score = 85)
        g_phones = [p.value for p in google_contact.phones if p.value]
        ms_phones = [p.value for p in ms_contact.phones if p.value]
        matched_phones = [
            f"{gp} ~ {mp}"
            for gp in g_phones
            for mp in ms_phones
            if phones_match(gp, mp)
        ]

        if matched_phones:
            best_score = max(best_score, 85)
            criteria.append("E.164 Phone Number")
            reasons.append(f"Matching phone: {', '.join(matched_phones)}")

        # 4. Name Matching with Conflict Guardrails
        g_name = google_contact.name.full_name_normalized()
        ms_name = ms_contact.name.full_name_normalized()

        if g_name and ms_name and g_name == ms_name:
            g_org = google_contact.primary_organization
            ms_org = ms_contact.primary_organization
            g_company = normalize_text(g_org.company) if (g_org and g_org.company) else None
            ms_company = normalize_text(ms_org.company) if (ms_org and ms_org.company) else None

            # Collision Guardrails: Do they have directly contradicting emails or phones?
            has_email_conflict = bool(g_emails and ms_emails and not common_emails)
            has_phone_conflict = bool(g_phones and ms_phones and not matched_phones)

            if has_email_conflict or has_phone_conflict:
                # Contradicting details -> Protect against merging two different people with same name
                best_score = max(best_score, 40)
                criteria.append("Name Match (Conflicting Details)")
                reasons.append(f"Name matched ('{g_name}') but emails or phones contradict")
            elif g_company and ms_company and g_company == ms_company:
                best_score = max(best_score, 85)
                criteria.append("Name + Organization")
                reasons.append(f"Exact name ('{g_name}') and company ('{g_company}') match")
            else:
                # Safe Name Match (30% threshold promoted to 80 Definite Match if no contradictory data)
                best_score = max(best_score, 80)
                criteria.append("Full Name (Non-Conflicting)")
                reasons.append(f"Exact name match ('{g_name}') without conflicting info")

        # Determine Category
        if best_score >= 80:
            category = MatchCategory.DEFINITE_MATCH
        elif 50 <= best_score < 80:
            category = MatchCategory.POTENTIAL_DUPLICATE
        else:
            category = MatchCategory.UNIQUE

        reason_str = "; ".join(reasons) if reasons else "No matching identity fields found."
        return MatchBreakdown(
            score=best_score,
            category=category,
            matched_criteria=criteria,
            reason=reason_str,
        )

    @classmethod
    def match_collections(
        cls,
        google_contacts: list[UnifiedContact],
        ms_contacts: list[UnifiedContact],
        stored_mappings: Optional[dict[str, str]] = None,
    ) -> MatchingResult:
        """Evaluates collections of Google and Microsoft contacts and groups them into match categories."""
        definite_matches: list[ContactMatchPair] = []
        potential_duplicates: list[ContactMatchPair] = []
        unmatched_google = list(google_contacts)
        unmatched_ms = list(ms_contacts)

        # 1. First Pass: Find Definite Matches (Score >= 80) greedily by highest score
        candidate_pairs: list[tuple[int, MatchBreakdown, UnifiedContact, UnifiedContact]] = []
        for g in google_contacts:
            for ms in ms_contacts:
                breakdown = cls.evaluate_pair(g, ms, stored_mappings=stored_mappings)
                if breakdown.category == MatchCategory.DEFINITE_MATCH:
                    candidate_pairs.append((breakdown.score, breakdown, g, ms))

        candidate_pairs.sort(key=lambda x: x[0], reverse=True)

        paired_g_ids: set[str] = set()
        paired_ms_ids: set[str] = set()

        for score, breakdown, g, ms in candidate_pairs:
            g_key = g.google_resource_name or id(g)
            ms_key = ms.ms_id or id(ms)
            if g_key in paired_g_ids or ms_key in paired_ms_ids:
                continue

            definite_matches.append(
                ContactMatchPair(
                    google_contact=g,
                    ms_contact=ms,
                    score=score,
                    category=MatchCategory.DEFINITE_MATCH,
                    matched_criteria=breakdown.matched_criteria,
                    reason=breakdown.reason,
                )
            )
            paired_g_ids.add(g_key)
            paired_ms_ids.add(ms_key)

        # Remove paired from unmatched lists
        unmatched_google = [g for g in unmatched_google if (g.google_resource_name or id(g)) not in paired_g_ids]
        unmatched_ms = [ms for ms in unmatched_ms if (ms.ms_id or id(ms)) not in paired_ms_ids]

        # 2. Second Pass: Find Potential Duplicates (50 <= Score < 80)
        dup_pairs: list[tuple[int, MatchBreakdown, UnifiedContact, UnifiedContact]] = []
        for g in unmatched_google:
            for ms in unmatched_ms:
                breakdown = cls.evaluate_pair(g, ms, stored_mappings=stored_mappings)
                if breakdown.category == MatchCategory.POTENTIAL_DUPLICATE:
                    dup_pairs.append((breakdown.score, breakdown, g, ms))

        dup_pairs.sort(key=lambda x: x[0], reverse=True)
        flagged_g_ids: set[str] = set()
        flagged_ms_ids: set[str] = set()

        for score, breakdown, g, ms in dup_pairs:
            g_key = g.google_resource_name or id(g)
            ms_key = ms.ms_id or id(ms)
            if g_key in flagged_g_ids or ms_key in flagged_ms_ids:
                continue

            potential_duplicates.append(
                ContactMatchPair(
                    google_contact=g,
                    ms_contact=ms,
                    score=score,
                    category=MatchCategory.POTENTIAL_DUPLICATE,
                    matched_criteria=breakdown.matched_criteria,
                    reason=breakdown.reason,
                )
            )
            flagged_g_ids.add(g_key)
            flagged_ms_ids.add(ms_key)

        # Remaining contacts that aren't definite matches remain in unmatched
        unmatched_google = [g for g in unmatched_google if (g.google_resource_name or id(g)) not in paired_g_ids and (g.google_resource_name or id(g)) not in flagged_g_ids]
        unmatched_ms = [ms for ms in unmatched_ms if (ms.ms_id or id(ms)) not in paired_ms_ids and (ms.ms_id or id(ms)) not in flagged_ms_ids]

        return MatchingResult(
            definite_matches=definite_matches,
            potential_duplicates=potential_duplicates,
            unmatched_google=unmatched_google,
            unmatched_ms=unmatched_ms,
        )
