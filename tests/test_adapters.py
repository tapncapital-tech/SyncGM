"""Comprehensive test suite for Phase 1 Data Contracts & API Adapters."""
from __future__ import annotations

import json
from typing import Any
import httpx
import pytest

from execution.exceptions import (
    AuthenticationError,
    ContactNotFoundError,
    RateLimitError,
    TokenExpiredError,
)
from execution.google_client import GoogleContactsClient
from execution.models import (
    EmailAddress,
    NameDetails,
    OrganizationDetails,
    PhoneNumber,
    PostalAddress,
    UnifiedContact,
    normalize_email,
    normalize_phone,
)
from execution.ms_client import MicrosoftContactsClient


# =============================================================================
# MOCK JSON FIXTURES
# =============================================================================

GOOGLE_PERSON_FIXTURE: dict[str, Any] = {
    "resourceName": "people/c1029384756",
    "etag": "%EhkBAgMEBQYHCAkKCwwNDg8QERITFBU=",
    "metadata": {
        "sources": [{"type": "CONTACT", "id": "1029384756"}],
        "objectType": "PERSON",
    },
    "names": [
        {
            "metadata": {"primary": True, "source": {"type": "CONTACT"}},
            "displayName": "Dr. Sylvester Alexander Moore El Jr.",
            "familyName": "Moore El",
            "givenName": "Sylvester",
            "middleName": "Alexander",
            "honorificPrefix": "Dr.",
            "honorificSuffix": "Jr.",
        }
    ],
    "emailAddresses": [
        {
            "metadata": {"primary": True},
            "value": "Sylvester.Moore@Enterprise.com",
            "type": "work",
            "formattedType": "Work",
        },
        {
            "metadata": {"primary": False},
            "value": "smoore@personal.net",
            "type": "home",
            "formattedType": "Home",
        },
    ],
    "phoneNumbers": [
        {
            "metadata": {"primary": True},
            "value": "+1 (555) 234-5678",
            "type": "mobile",
            "formattedType": "Mobile",
        },
        {
            "metadata": {"primary": False},
            "value": "555-876-5432",
            "type": "work",
            "formattedType": "Work",
        },
    ],
    "organizations": [
        {
            "metadata": {"primary": True},
            "name": "Acme Global Solutions",
            "department": "Engineering Operations",
            "title": "Principal Architect",
            "jobDescription": "Oversees distributed synchronization pipelines",
        }
    ],
    "addresses": [
        {
            "metadata": {"primary": True},
            "streetAddress": "742 Evergreen Terrace",
            "city": "Springfield",
            "region": "IL",
            "postalCode": "62704",
            "country": "United States",
            "type": "work",
        }
    ],
    "biographies": [
        {
            "metadata": {"primary": True},
            "value": "Key stakeholder and technical lead.",
            "contentType": "TEXT_PLAIN",
        }
    ],
}

MS_CONTACT_FIXTURE: dict[str, Any] = {
    "id": "AAMkAGI2AAAt1p2AAA=",
    "changeKey": "EQAAABY+aaBbCcDdEeFf",
    "createdDateTime": "2026-01-15T08:30:00Z",
    "lastModifiedDateTime": "2026-03-20T14:22:10Z",
    "displayName": "Dr. Sylvester Alexander Moore El Jr.",
    "givenName": "Sylvester",
    "surname": "Moore El",
    "middleName": "Alexander",
    "title": "Dr.",
    "generation": "Jr.",
    "jobTitle": "Principal Architect",
    "companyName": "Acme Global Solutions",
    "department": "Engineering Operations",
    "personalNotes": "Key stakeholder and technical lead.",
    "emailAddresses": [
        {"name": "Work Email", "address": "sylvester.moore@enterprise.com"},
        {"name": "Personal Email", "address": "smoore@personal.net"},
    ],
    "mobilePhone": "+1 (555) 234-5678",
    "businessPhones": ["555-876-5432"],
    "homePhones": [],
    "businessAddress": {
        "street": "742 Evergreen Terrace",
        "city": "Springfield",
        "state": "IL",
        "postalCode": "62704",
        "countryOrRegion": "United States",
    },
    "homeAddress": {},
    "otherAddress": {},
}


# =============================================================================
# DATA CONTRACT / SERIALIZER TESTS
# =============================================================================

def test_normalization_helpers() -> None:
    assert normalize_email("  Test.User+Filter@Domain.COM  ") == "test.user+filter@domain.com"
    assert normalize_email(None) is None
    assert normalize_email("") is None

    assert normalize_phone("+1 (555) 123-4567") == "+15551234567"
    assert normalize_phone("  (800) 555-0199  ") == "8005550199"
    assert normalize_phone(None) is None


def test_google_person_deserialization() -> None:
    contact = UnifiedContact.from_google_person(GOOGLE_PERSON_FIXTURE)

    assert contact.google_resource_name == "people/c1029384756"
    assert contact.google_etag == "%EhkBAgMEBQYHCAkKCwwNDg8QERITFBU="

    # Names
    assert contact.name.given_name == "Sylvester"
    assert contact.name.family_name == "Moore El"
    assert contact.name.middle_name == "Alexander"
    assert contact.name.prefix == "Dr."
    assert contact.name.suffix == "Jr."

    # Emails
    assert len(contact.emails) == 2
    assert contact.primary_email == "sylvester.moore@enterprise.com"
    assert contact.emails[0].type == "work"
    assert contact.emails[0].is_primary is True
    assert contact.emails[1].value == "smoore@personal.net"

    # Phones
    assert len(contact.phones) == 2
    assert contact.primary_phone == "+15552345678"
    assert contact.phones[0].type == "mobile"
    assert contact.phones[1].value == "5558765432"

    # Organization
    assert contact.primary_organization is not None
    assert contact.primary_organization.company == "Acme Global Solutions"
    assert contact.primary_organization.department == "Engineering Operations"
    assert contact.primary_organization.title == "Principal Architect"

    # Address
    assert len(contact.addresses) == 1
    assert contact.addresses[0].street == "742 Evergreen Terrace"
    assert contact.addresses[0].city == "Springfield"
    assert contact.addresses[0].state == "IL"
    assert contact.addresses[0].postal_code == "62704"

    # Notes
    assert contact.notes == "Key stakeholder and technical lead."


def test_google_payload_serialization() -> None:
    contact = UnifiedContact.from_google_person(GOOGLE_PERSON_FIXTURE)
    payload = contact.to_google_payload(include_etag=True)

    assert payload["etag"] == "%EhkBAgMEBQYHCAkKCwwNDg8QERITFBU="
    assert payload["names"][0]["givenName"] == "Sylvester"
    assert payload["names"][0]["familyName"] == "Moore El"
    assert payload["names"][0]["honorificPrefix"] == "Dr."
    assert len(payload["emailAddresses"]) == 2
    assert payload["emailAddresses"][0]["value"] == "sylvester.moore@enterprise.com"
    assert len(payload["phoneNumbers"]) == 2
    assert payload["organizations"][0]["name"] == "Acme Global Solutions"
    assert payload["addresses"][0]["streetAddress"] == "742 Evergreen Terrace"
    assert payload["biographies"][0]["value"] == "Key stakeholder and technical lead."


def test_ms_contact_deserialization() -> None:
    contact = UnifiedContact.from_ms_contact(MS_CONTACT_FIXTURE)

    assert contact.ms_id == "AAMkAGI2AAAt1p2AAA="
    assert contact.ms_etag == "EQAAABY+aaBbCcDdEeFf"

    assert contact.name.given_name == "Sylvester"
    assert contact.name.family_name == "Moore El"
    assert contact.name.middle_name == "Alexander"
    assert contact.name.prefix == "Dr."
    assert contact.name.suffix == "Jr."

    assert len(contact.emails) == 2
    assert contact.primary_email == "sylvester.moore@enterprise.com"

    assert len(contact.phones) == 2
    assert contact.primary_phone == "+15552345678"
    assert contact.phones[0].type == "mobile"
    assert contact.phones[1].value == "5558765432"

    assert contact.primary_organization is not None
    assert contact.primary_organization.company == "Acme Global Solutions"
    assert contact.primary_organization.title == "Principal Architect"

    assert len(contact.addresses) == 1
    assert contact.addresses[0].street == "742 Evergreen Terrace"
    assert contact.addresses[0].type == "work"
    assert contact.notes == "Key stakeholder and technical lead."


def test_ms_payload_serialization() -> None:
    contact = UnifiedContact.from_ms_contact(MS_CONTACT_FIXTURE)
    payload = contact.to_ms_payload()

    assert payload["givenName"] == "Sylvester"
    assert payload["surname"] == "Moore El"
    assert payload["title"] == "Dr."
    assert payload["generation"] == "Jr."
    assert payload["mobilePhone"] == "+15552345678"
    assert payload["businessPhones"] == ["5558765432"]
    assert payload["companyName"] == "Acme Global Solutions"
    assert payload["businessAddress"]["street"] == "742 Evergreen Terrace"
    assert payload["personalNotes"] == "Key stakeholder and technical lead."


# =============================================================================
# GOOGLE CONTACTS CLIENT TESTS (MOCKED HTTP)
# =============================================================================

def test_google_client_get_connections_pagination() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        assert "people/me/connections" in url
        if "pageToken=page2" in url:
            body = {
                "connections": [GOOGLE_PERSON_FIXTURE],
                "nextPageToken": None,
            }
        else:
            body = {
                "connections": [GOOGLE_PERSON_FIXTURE],
                "nextPageToken": "page2",
            }
        return httpx.Response(200, json=body)

    mock_client = httpx.Client(transport=httpx.MockTransport(handler))
    client = GoogleContactsClient(access_token="mock_google_token", http_client=mock_client)

    contacts = client.get_all_contacts()
    assert len(contacts) == 2
    assert contacts[0].name.given_name == "Sylvester"
    assert contacts[1].name.given_name == "Sylvester"


def test_google_client_create_and_update() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if request.method == "POST" and "people:createContact" in url:
            req_data = json.loads(request.content)
            res = dict(GOOGLE_PERSON_FIXTURE)
            res["resourceName"] = "people/c_created_99"
            return httpx.Response(200, json=res)

        if request.method == "PATCH" and "people/c1029384756:updateContact" in url:
            return httpx.Response(200, json=GOOGLE_PERSON_FIXTURE)

        if request.method == "DELETE" and "people/c1029384756:deleteContact" in url:
            return httpx.Response(204)

        return httpx.Response(404)

    mock_client = httpx.Client(transport=httpx.MockTransport(handler))
    client = GoogleContactsClient(access_token="mock_token", http_client=mock_client)

    contact = UnifiedContact.from_google_person(GOOGLE_PERSON_FIXTURE)
    created = client.create_contact(contact)
    assert created.google_resource_name == "people/c_created_99"

    updated = client.update_contact(contact)
    assert updated.google_resource_name == "people/c1029384756"

    deleted = client.delete_contact("people/c1029384756")
    assert deleted is True


def test_google_client_rate_limit_and_backoff() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            return httpx.Response(429, json={"error": "Rate limit exceeded"})
        return httpx.Response(200, json={"connections": [GOOGLE_PERSON_FIXTURE]})

    mock_client = httpx.Client(transport=httpx.MockTransport(handler))
    client = GoogleContactsClient(
        access_token="mock_token", http_client=mock_client, base_backoff=0.01, max_retries=3
    )

    contacts, _ = client.get_connections()
    assert len(contacts) == 1
    assert attempts == 3


def test_google_client_token_refresh_on_401() -> None:
    calls: list[str] = []
    refreshed_tokens: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        calls.append(f"{request.method} {url}")

        if "oauth2.googleapis.com/token" in url:
            return httpx.Response(200, json={"access_token": "new_google_access_token"})

        if "people/me/connections" in url:
            auth_header = request.headers.get("Authorization", "")
            if auth_header == "Bearer old_google_token":
                return httpx.Response(401, json={"error": "Token expired"})
            elif auth_header == "Bearer new_google_access_token":
                return httpx.Response(200, json={"connections": [GOOGLE_PERSON_FIXTURE]})

        return httpx.Response(400)

    mock_client = httpx.Client(transport=httpx.MockTransport(handler))
    client = GoogleContactsClient(
        access_token="old_google_token",
        refresh_token="mock_refresh_token",
        client_id="mock_client_id",
        client_secret="mock_client_secret",
        token_refresh_callback=lambda token: refreshed_tokens.append(token),
        http_client=mock_client,
    )

    contacts, _ = client.get_connections()
    assert len(contacts) == 1
    assert client.access_token == "new_google_access_token"
    assert refreshed_tokens == ["new_google_access_token"]


# =============================================================================
# MICROSOFT CONTACTS CLIENT TESTS (MOCKED HTTP)
# =============================================================================

def test_ms_client_get_contacts_pagination() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        assert "graph.microsoft.com" in url
        if "nextLink" in url:
            body = {"value": [MS_CONTACT_FIXTURE], "@odata.nextLink": None}
        else:
            body = {
                "value": [MS_CONTACT_FIXTURE],
                "@odata.nextLink": "https://graph.microsoft.com/v1.0/me/contacts?nextLink=true",
            }
        return httpx.Response(200, json=body)

    mock_client = httpx.Client(transport=httpx.MockTransport(handler))
    client = MicrosoftContactsClient(access_token="mock_ms_token", http_client=mock_client)

    all_contacts = client.get_all_contacts()
    assert len(all_contacts) == 2
    assert all_contacts[0].name.given_name == "Sylvester"
    assert all_contacts[1].name.given_name == "Sylvester"


def test_ms_client_create_and_update() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if request.method == "POST" and "me/contacts" in url:
            res = dict(MS_CONTACT_FIXTURE)
            res["id"] = "AAMk_created_123"
            return httpx.Response(201, json=res)

        if request.method == "PATCH" and "me/contacts/AAMkAGI2AAAt1p2AAA=" in url:
            return httpx.Response(200, json=MS_CONTACT_FIXTURE)

        if request.method == "DELETE" and "me/contacts/AAMkAGI2AAAt1p2AAA=" in url:
            return httpx.Response(204)

        return httpx.Response(404)

    mock_client = httpx.Client(transport=httpx.MockTransport(handler))
    client = MicrosoftContactsClient(access_token="mock_ms_token", http_client=mock_client)

    contact = UnifiedContact.from_ms_contact(MS_CONTACT_FIXTURE)
    created = client.create_contact(contact)
    assert created.ms_id == "AAMk_created_123"

    updated = client.update_contact(contact)
    assert updated.ms_id == "AAMkAGI2AAAt1p2AAA="

    deleted = client.delete_contact("AAMkAGI2AAAt1p2AAA=")
    assert deleted is True


def test_ms_client_rate_limit_retry_after() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(429, headers={"Retry-After": "0"}, json={"error": "Rate limited"})
        return httpx.Response(200, json={"value": [MS_CONTACT_FIXTURE]})

    mock_client = httpx.Client(transport=httpx.MockTransport(handler))
    client = MicrosoftContactsClient(
        access_token="mock_ms_token", http_client=mock_client, base_backoff=0.01, max_retries=2
    )

    contacts, _ = client.get_contacts()
    assert len(contacts) == 1
    assert attempts == 2


def test_ms_client_token_refresh_on_401() -> None:
    refreshed_tokens: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)

        if "login.microsoftonline.com" in url:
            return httpx.Response(200, json={"access_token": "new_ms_access_token"})

        if "me/contacts" in url:
            auth_header = request.headers.get("Authorization", "")
            if auth_header == "Bearer old_ms_token":
                return httpx.Response(401, json={"error": "Token expired"})
            elif auth_header == "Bearer new_ms_access_token":
                return httpx.Response(200, json={"value": [MS_CONTACT_FIXTURE]})

        return httpx.Response(400)

    mock_client = httpx.Client(transport=httpx.MockTransport(handler))
    client = MicrosoftContactsClient(
        access_token="old_ms_token",
        refresh_token="mock_refresh_token",
        client_id="mock_client_id",
        client_secret="mock_client_secret",
        token_refresh_callback=lambda token: refreshed_tokens.append(token),
        http_client=mock_client,
    )

    contacts, _ = client.get_contacts()
    assert len(contacts) == 1
    assert client.access_token == "new_ms_access_token"
    assert refreshed_tokens == ["new_ms_access_token"]


def test_not_found_error_handling() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "Resource not found"})

    mock_client = httpx.Client(transport=httpx.MockTransport(handler))
    google_client = GoogleContactsClient(access_token="token", http_client=mock_client)
    ms_client = MicrosoftContactsClient(access_token="token", http_client=mock_client)

    with pytest.raises(ContactNotFoundError):
        google_client.delete_contact("people/c_missing")

    with pytest.raises(ContactNotFoundError):
        ms_client.delete_contact("missing_id")
