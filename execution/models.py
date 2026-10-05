"""Unified Contact Data Contract, Serializers, and Diff Planning Models (Pydantic v2)."""
from __future__ import annotations

from datetime import datetime
from enum import Enum
import hashlib
import json
import re
from typing import Any, Optional
from pydantic import BaseModel, ConfigDict, Field


def normalize_email(email: Optional[str]) -> Optional[str]:
    """Normalizes an email address to lowercase and trimmed string."""
    if not email:
        return None
    return email.strip().lower()


def normalize_phone(phone: Optional[str]) -> Optional[str]:
    """Normalizes a phone number to standard E.164-compatible digit format where possible."""
    if not phone:
        return None
    cleaned = phone.strip()
    has_plus = cleaned.startswith("+")
    digits = re.sub(r"\D", "", cleaned)
    if not digits:
        return None
    return f"+{digits}" if has_plus else digits


def normalize_text(text: Optional[str]) -> Optional[str]:
    """Normalizes whitespace and casing for text matching."""
    if not text:
        return None
    cleaned = " ".join(text.strip().split())
    return cleaned.lower() if cleaned else None


# =============================================================================
# UNIFIED CONTACT COMPONENT MODELS
# =============================================================================

class NameDetails(BaseModel):
    """Detailed components of a contact's name."""
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    given_name: Optional[str] = Field(default=None, description="First name")
    family_name: Optional[str] = Field(default=None, description="Last name")
    middle_name: Optional[str] = Field(default=None, description="Middle name")
    display_name: Optional[str] = Field(default=None, description="Full formatted display name")
    prefix: Optional[str] = Field(default=None, description="Honorific prefix e.g. Dr., Mr.")
    suffix: Optional[str] = Field(default=None, description="Honorific suffix e.g. Jr., PhD")

    def computed_display_name(self) -> str:
        """Returns display_name or constructs one from given and family names."""
        if self.display_name and self.display_name.strip():
            return self.display_name.strip()
        parts = [p for p in [self.prefix, self.given_name, self.middle_name, self.family_name, self.suffix] if p and p.strip()]
        return " ".join(parts) if parts else "Unnamed Contact"

    def full_name_normalized(self) -> Optional[str]:
        """Returns a normalized string of first + last name for identity matching."""
        parts = [p for p in [self.given_name, self.family_name] if p and p.strip()]
        if not parts and self.display_name:
            return normalize_text(self.display_name)
        return normalize_text(" ".join(parts)) if parts else None


class EmailAddress(BaseModel):
    """Contact email address with classification."""
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    value: str = Field(description="Normalized email string")
    type: str = Field(default="work", description="Type/label: 'work', 'home', 'other'")
    is_primary: bool = Field(default=False, description="Primary email flag")

    def model_post_init(self, __context: Any) -> None:
        normalized = normalize_email(self.value)
        if normalized:
            self.value = normalized


class PhoneNumber(BaseModel):
    """Contact phone number with classification."""
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    value: str = Field(description="Normalized phone number")
    type: str = Field(default="mobile", description="Type/label: 'mobile', 'work', 'home', 'other'")
    is_primary: bool = Field(default=False, description="Primary phone flag")

    def model_post_init(self, __context: Any) -> None:
        normalized = normalize_phone(self.value)
        if normalized:
            self.value = normalized


class OrganizationDetails(BaseModel):
    """Professional organization and job details."""
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    company: Optional[str] = Field(default=None, description="Company/Organization name")
    department: Optional[str] = Field(default=None, description="Department name")
    title: Optional[str] = Field(default=None, description="Job title / role")
    job_description: Optional[str] = Field(default=None, description="Role description")


class PostalAddress(BaseModel):
    """Physical or mailing postal address."""
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    street: Optional[str] = Field(default=None, description="Street address including suite/apt")
    city: Optional[str] = Field(default=None, description="City / Locality")
    state: Optional[str] = Field(default=None, description="State / Region / Province")
    postal_code: Optional[str] = Field(default=None, description="Postal code / ZIP")
    country: Optional[str] = Field(default=None, description="Country or region")
    type: str = Field(default="work", description="Type/label: 'work', 'home', 'other'")


class UnifiedContact(BaseModel):
    """Canonical Unified Contact model for cross-provider synchronization."""
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    # Provider tracking IDs and optimistic concurrency etags
    google_resource_name: Optional[str] = Field(default=None, description="Google People API resource name, e.g. people/c123")
    google_etag: Optional[str] = Field(default=None, description="Google People API etag")
    ms_id: Optional[str] = Field(default=None, description="Microsoft Graph contact ID")
    ms_etag: Optional[str] = Field(default=None, description="Microsoft Graph changeKey / etag")

    # Canonical Field Collections
    last_modified: Optional[datetime] = Field(default=None, description="Timestamp of last contact modification")
    name: NameDetails = Field(default_factory=NameDetails, description="Structured name components")
    emails: list[EmailAddress] = Field(default_factory=list, description="List of email addresses")
    phones: list[PhoneNumber] = Field(default_factory=list, description="List of phone numbers")
    organizations: list[OrganizationDetails] = Field(default_factory=list, description="List of organization roles")
    addresses: list[PostalAddress] = Field(default_factory=list, description="List of postal addresses")
    notes: Optional[str] = Field(default=None, description="Personal notes or biographical info")

    # Raw payloads for audit and fidelity retention
    raw_google: Optional[dict[str, Any]] = Field(default=None, exclude=True)
    raw_ms: Optional[dict[str, Any]] = Field(default=None, exclude=True)

    @property
    def primary_email(self) -> Optional[str]:
        """Returns the primary email or the first available email address."""
        if not self.emails:
            return None
        for email in self.emails:
            if email.is_primary:
                return email.value
        return self.emails[0].value

    @property
    def primary_phone(self) -> Optional[str]:
        """Returns the primary phone or the first available phone number."""
        if not self.phones:
            return None
        for phone in self.phones:
            if phone.is_primary:
                return phone.value
        return self.phones[0].value

    @property
    def primary_organization(self) -> Optional[OrganizationDetails]:
        """Returns the primary organization or first entry."""
        return self.organizations[0] if self.organizations else None

    def compute_field_hash(self) -> str:
        """Computes a deterministic SHA-256 hash of normalized contact business fields."""
        comparable_dict = {
            "name": {
                "given_name": self.name.given_name,
                "family_name": self.name.family_name,
                "middle_name": self.name.middle_name,
                "display_name": self.name.display_name,
                "prefix": self.name.prefix,
                "suffix": self.name.suffix,
            },
            "emails": sorted([e.value for e in self.emails]),
            "phones": sorted([p.value for p in self.phones]),
            "organizations": [
                {"company": o.company, "department": o.department, "title": o.title}
                for o in self.organizations
            ],
            "addresses": [
                {
                    "street": a.street,
                    "city": a.city,
                    "state": a.state,
                    "postal_code": a.postal_code,
                    "country": a.country,
                }
                for a in self.addresses
            ],
            "notes": self.notes,
        }
        serialized = json.dumps(comparable_dict, sort_keys=True)
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    # -------------------------------------------------------------------------
    # Google People API v1 Serialization
    # -------------------------------------------------------------------------
    @classmethod
    def from_google_person(cls, data: dict[str, Any]) -> UnifiedContact:
        """Parses a Google People API Person resource into a UnifiedContact."""
        resource_name = data.get("resourceName")
        etag = data.get("etag")

        name_obj = NameDetails()
        names_list = data.get("names", [])
        if names_list:
            primary_name = next((n for n in names_list if n.get("metadata", {}).get("primary")), names_list[0])
            name_obj = NameDetails(
                given_name=primary_name.get("givenName"),
                family_name=primary_name.get("familyName"),
                middle_name=primary_name.get("middleName"),
                display_name=primary_name.get("displayName"),
                prefix=primary_name.get("honorificPrefix"),
                suffix=primary_name.get("honorificSuffix"),
            )

        emails: list[EmailAddress] = []
        for e in data.get("emailAddresses", []):
            val = e.get("value")
            if val:
                emails.append(
                    EmailAddress(
                        value=val,
                        type=e.get("type", "work").lower(),
                        is_primary=bool(e.get("metadata", {}).get("primary", False)),
                    )
                )

        phones: list[PhoneNumber] = []
        for p in data.get("phoneNumbers", []):
            val = p.get("value")
            if val:
                phones.append(
                    PhoneNumber(
                        value=val,
                        type=p.get("type", "mobile").lower(),
                        is_primary=bool(p.get("metadata", {}).get("primary", False)),
                    )
                )

        organizations: list[OrganizationDetails] = []
        for o in data.get("organizations", []):
            organizations.append(
                OrganizationDetails(
                    company=o.get("name"),
                    department=o.get("department"),
                    title=o.get("title"),
                    job_description=o.get("jobDescription"),
                )
            )

        addresses: list[PostalAddress] = []
        for a in data.get("addresses", []):
            addresses.append(
                PostalAddress(
                    street=a.get("streetAddress"),
                    city=a.get("city"),
                    state=a.get("region"),
                    postal_code=a.get("postalCode"),
                    country=a.get("country"),
                    type=a.get("type", "work").lower(),
                )
            )

        notes: Optional[str] = None
        bios = data.get("biographies", [])
        if bios:
            notes = bios[0].get("value")

        last_modified: Optional[datetime] = None
        sources = data.get("metadata", {}).get("sources", [])
        for s in sources:
            ut = s.get("updateTime")
            if ut:
                try:
                    last_modified = datetime.fromisoformat(ut.replace("Z", "+00:00"))
                    break
                except Exception:
                    pass

        return cls(
            google_resource_name=resource_name,
            google_etag=etag,
            last_modified=last_modified,
            name=name_obj,
            emails=emails,
            phones=phones,
            organizations=organizations,
            addresses=addresses,
            notes=notes,
            raw_google=data,
        )

    def to_google_payload(self, include_etag: bool = True) -> dict[str, Any]:
        """Serializes the UnifiedContact into a Google People API create/update payload."""
        payload: dict[str, Any] = {}

        if include_etag and self.google_etag:
            payload["etag"] = self.google_etag

        name_dict: dict[str, Any] = {}
        if self.name.given_name:
            name_dict["givenName"] = self.name.given_name
        if self.name.family_name:
            name_dict["familyName"] = self.name.family_name
        if self.name.middle_name:
            name_dict["middleName"] = self.name.middle_name
        if self.name.prefix:
            name_dict["honorificPrefix"] = self.name.prefix
        if self.name.suffix:
            name_dict["honorificSuffix"] = self.name.suffix

        if name_dict:
            payload["names"] = [name_dict]

        if self.emails:
            payload["emailAddresses"] = [
                {"value": e.value, "type": e.type} for e in self.emails
            ]

        if self.phones:
            payload["phoneNumbers"] = [
                {"value": p.value, "type": p.type} for p in self.phones
            ]

        if self.organizations:
            payload["organizations"] = [
                {
                    k: v
                    for k, v in {
                        "name": o.company,
                        "department": o.department,
                        "title": o.title,
                        "jobDescription": o.job_description,
                    }.items()
                    if v is not None
                }
                for o in self.organizations
            ]

        if self.addresses:
            payload["addresses"] = [
                {
                    k: v
                    for k, v in {
                        "streetAddress": a.street,
                        "city": a.city,
                        "region": a.state,
                        "postalCode": a.postal_code,
                        "country": a.country,
                        "type": a.type,
                    }.items()
                    if v is not None
                }
                for a in self.addresses
            ]

        if self.notes:
            payload["biographies"] = [{"value": self.notes, "contentType": "TEXT_PLAIN"}]

        return payload

    # -------------------------------------------------------------------------
    # Microsoft Graph API v1.0 Serialization
    # -------------------------------------------------------------------------
    @classmethod
    def from_ms_contact(cls, data: dict[str, Any]) -> UnifiedContact:
        """Parses a Microsoft Graph API contact entity into a UnifiedContact."""
        ms_id = data.get("id")
        change_key = data.get("changeKey")

        name_obj = NameDetails(
            given_name=data.get("givenName"),
            family_name=data.get("surname"),
            middle_name=data.get("middleName"),
            display_name=data.get("displayName"),
            prefix=data.get("title"),
            suffix=data.get("generation"),
        )

        emails: list[EmailAddress] = []
        ms_emails = data.get("emailAddresses", [])
        for idx, e in enumerate(ms_emails):
            addr = e.get("address")
            if addr:
                name_label = (e.get("name") or "").lower()
                email_type = "work" if "work" in name_label else ("home" if "home" in name_label else "work")
                emails.append(EmailAddress(value=addr, type=email_type, is_primary=(idx == 0)))

        phones: list[PhoneNumber] = []
        mobile = data.get("mobilePhone")
        if mobile:
            phones.append(PhoneNumber(value=mobile, type="mobile", is_primary=True))

        for bp in data.get("businessPhones", []):
            if bp:
                phones.append(PhoneNumber(value=bp, type="work", is_primary=(len(phones) == 0)))

        for hp in data.get("homePhones", []):
            if hp:
                phones.append(PhoneNumber(value=hp, type="home", is_primary=(len(phones) == 0)))

        organizations: list[OrganizationDetails] = []
        company = data.get("companyName")
        dept = data.get("department")
        job_title = data.get("jobTitle")
        if company or dept or job_title:
            organizations.append(
                OrganizationDetails(company=company, department=dept, title=job_title)
            )

        addresses: list[PostalAddress] = []
        addr_mappings = [
            ("businessAddress", "work"),
            ("homeAddress", "home"),
            ("otherAddress", "other"),
        ]
        for key, addr_type in addr_mappings:
            addr_data = data.get(key)
            if addr_data and any(addr_data.values()):
                addresses.append(
                    PostalAddress(
                        street=addr_data.get("street"),
                        city=addr_data.get("city"),
                        state=addr_data.get("state"),
                        postal_code=addr_data.get("postalCode"),
                        country=addr_data.get("countryOrRegion"),
                        type=addr_type,
                    )
                )

        notes = data.get("personalNotes")

        last_modified: Optional[datetime] = None
        lmd = data.get("lastModifiedDateTime")
        if lmd:
            try:
                last_modified = datetime.fromisoformat(lmd.replace("Z", "+00:00"))
            except Exception:
                pass

        return cls(
            ms_id=ms_id,
            ms_etag=change_key,
            last_modified=last_modified,
            name=name_obj,
            emails=emails,
            phones=phones,
            organizations=organizations,
            addresses=addresses,
            notes=notes,
            raw_ms=data,
        )

    def to_ms_payload(self) -> dict[str, Any]:
        """Serializes the UnifiedContact into a Microsoft Graph API create/update payload."""
        payload: dict[str, Any] = {}

        if self.name.given_name is not None:
            payload["givenName"] = self.name.given_name
        if self.name.family_name is not None:
            payload["surname"] = self.name.family_name
        if self.name.middle_name is not None:
            payload["middleName"] = self.name.middle_name
        if self.name.prefix is not None:
            payload["title"] = self.name.prefix
        if self.name.suffix is not None:
            payload["generation"] = self.name.suffix

        payload["displayName"] = self.name.computed_display_name()

        if self.emails:
            payload["emailAddresses"] = [
                {"address": e.value, "name": f"{e.type.capitalize()} Email"}
                for e in self.emails
            ]

        mobile_val: Optional[str] = None
        biz_phones: list[str] = []
        home_phones: list[str] = []

        for p in self.phones:
            if p.type == "mobile" and mobile_val is None:
                mobile_val = p.value
            elif p.type == "work":
                biz_phones.append(p.value)
            elif p.type == "home":
                home_phones.append(p.value)
            else:
                if mobile_val is None:
                    mobile_val = p.value
                else:
                    biz_phones.append(p.value)

        if mobile_val:
            payload["mobilePhone"] = mobile_val
        if biz_phones:
            payload["businessPhones"] = biz_phones
        if home_phones:
            payload["homePhones"] = home_phones

        if self.organizations:
            org = self.organizations[0]
            if org.company:
                payload["companyName"] = org.company
            if org.department:
                payload["department"] = org.department
            if org.title:
                payload["jobTitle"] = org.title

        for a in self.addresses:
            addr_payload = {
                "street": a.street or "",
                "city": a.city or "",
                "state": a.state or "",
                "postalCode": a.postal_code or "",
                "countryOrRegion": a.country or "",
            }
            if a.type == "work":
                payload["businessAddress"] = addr_payload
            elif a.type == "home":
                payload["homeAddress"] = addr_payload
            else:
                payload["otherAddress"] = addr_payload

        if self.notes:
            payload["personalNotes"] = self.notes

        return payload


# =============================================================================
# MATCHING MODELS (Section 4.1)
# =============================================================================

class MatchCategory(str, Enum):
    DEFINITE_MATCH = "DEFINITE_MATCH"          # Score >= 80
    POTENTIAL_DUPLICATE = "POTENTIAL_DUPLICATE"  # Score 50 - 79
    UNIQUE = "UNIQUE"                          # Score < 50


class MatchBreakdown(BaseModel):
    """Detailed breakdown of matching score and conditions met."""
    model_config = ConfigDict(extra="ignore")

    score: int
    category: MatchCategory
    matched_criteria: list[str]
    reason: str


class ContactMatchPair(BaseModel):
    """A paired Google and Microsoft contact representation with scoring."""
    model_config = ConfigDict(extra="ignore")

    google_contact: UnifiedContact
    ms_contact: UnifiedContact
    score: int
    category: MatchCategory
    matched_criteria: list[str]
    reason: str


class MatchingResult(BaseModel):
    """Result of evaluating collections of Google and Microsoft contacts."""
    model_config = ConfigDict(extra="ignore")

    definite_matches: list[ContactMatchPair] = Field(default_factory=list)
    potential_duplicates: list[ContactMatchPair] = Field(default_factory=list)
    unmatched_google: list[UnifiedContact] = Field(default_factory=list)
    unmatched_ms: list[UnifiedContact] = Field(default_factory=list)


# =============================================================================
# 3-WAY DIFF & SYNC PLAN MODELS (Section 4.2 & Section 6)
# =============================================================================

class ConflictChoice(str, Enum):
    LAST_WRITER_WINS = "last_writer_wins"
    KEEP_BOTH = "keep_both"
    USE_GOOGLE = "use_google"
    USE_MICROSOFT = "use_microsoft"
    SKIP = "skip"


class FieldPatch(BaseModel):
    """A specific field update patch applied to a provider."""
    model_config = ConfigDict(extra="ignore")

    field_name: str
    old_value: Any
    new_value: Any
    source_provider: str  # 'google' | 'microsoft'


class ContactUpdateAction(BaseModel):
    """A planned update action for an existing contact record."""
    model_config = ConfigDict(extra="ignore")

    target_provider: str  # 'google' | 'microsoft'
    contact_id: str       # Google resourceName or Microsoft ID
    etag: Optional[str] = None
    updated_contact: UnifiedContact
    patches: list[FieldPatch] = Field(default_factory=list)


class ConflictItem(BaseModel):
    """An ambiguous or conflicting contact field delta requiring resolution."""
    model_config = ConfigDict(extra="ignore")

    id: str = Field(description="Unique conflict item UUID or identifier")
    contact_name: str
    google_resource_name: Optional[str] = None
    ms_id: Optional[str] = None
    conflicting_fields: dict[str, dict[str, Any]] = Field(
        description="Field diffs mapping field_name -> {'base': val, 'google': val, 'microsoft': val}"
    )
    available_choices: list[ConflictChoice] = Field(
        default_factory=lambda: [
            ConflictChoice.LAST_WRITER_WINS,
            ConflictChoice.KEEP_BOTH,
            ConflictChoice.USE_GOOGLE,
            ConflictChoice.USE_MICROSOFT,
            ConflictChoice.SKIP,
        ]
    )
    google_contact: UnifiedContact
    ms_contact: UnifiedContact
    base_contact: Optional[UnifiedContact] = None


class OrphanedContact(BaseModel):
    """A contact present on one provider but deleted/missing on the other with safety flag active."""
    model_config = ConfigDict(extra="ignore")

    contact: UnifiedContact
    provider: str
    status: str = "ORPHANED_IN_PROVIDER"
    reason: str


class SyncPlan(BaseModel):
    """The complete deterministic synchronization execution plan."""
    model_config = ConfigDict(extra="ignore")

    to_create_in_google: list[UnifiedContact] = Field(default_factory=list)
    to_create_in_ms: list[UnifiedContact] = Field(default_factory=list)
    to_update_in_google: list[ContactUpdateAction] = Field(default_factory=list)
    to_update_in_ms: list[ContactUpdateAction] = Field(default_factory=list)
    to_delete_in_google: list[str] = Field(default_factory=list)  # resource names
    to_delete_in_ms: list[str] = Field(default_factory=list)      # ms IDs
    persisted_mappings: list[tuple[str, str, str]] = Field(default_factory=list)  # (google_id, ms_id, hash)
    conflicts: list[ConflictItem] = Field(default_factory=list)
    orphaned: list[OrphanedContact] = Field(default_factory=list)
    unchanged_count: int = 0
