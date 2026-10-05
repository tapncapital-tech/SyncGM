"""Google Contacts API Client using Google People API v1."""
from __future__ import annotations

import logging
import time
from typing import Any, Callable, Optional
import httpx

from execution.exceptions import (
    AuthenticationError,
    ContactNotFoundError,
    RateLimitError,
    SyncAPIError,
    TokenExpiredError,
)
from execution.models import UnifiedContact

logger = logging.getLogger(__name__)


class GoogleContactsClient:
    """Client for interacting with Google People API v1 for Contacts synchronization."""

    BASE_URL = "https://people.googleapis.com/v1"
    TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
    DEFAULT_PERSON_FIELDS = "names,emailAddresses,phoneNumbers,organizations,addresses,biographies,metadata"

    def __init__(
        self,
        access_token: str,
        refresh_token: Optional[str] = None,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        token_refresh_callback: Optional[Callable[[str], None]] = None,
        http_client: Optional[httpx.Client] = None,
        max_retries: int = 3,
        base_backoff: float = 1.0,
    ) -> None:
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.client_id = client_id
        self.client_secret = client_secret
        self.token_refresh_callback = token_refresh_callback
        self.max_retries = max_retries
        self.base_backoff = base_backoff
        self._client = http_client or httpx.Client(timeout=30.0)

    def _get_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _refresh_access_token(self) -> str:
        """Refreshes the OAuth 2.0 access token using the stored refresh token."""
        if not self.refresh_token or not self.client_id or not self.client_secret:
            raise TokenExpiredError(provider="Google", message="Missing OAuth refresh credentials for Google")

        payload = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "refresh_token": self.refresh_token,
            "grant_type": "refresh_token",
        }
        try:
            resp = self._client.post(self.TOKEN_ENDPOINT, data=payload)
            if resp.status_code != 200:
                raise AuthenticationError(
                    provider="Google",
                    message=f"Failed to refresh token: {resp.text}",
                    status_code=resp.status_code,
                    response_body=resp.text,
                )
            data = resp.json()
            new_token = data.get("access_token")
            if not new_token:
                raise AuthenticationError(provider="Google", message="No access_token in refresh response")

            self.access_token = new_token
            if self.token_refresh_callback:
                self.token_refresh_callback(new_token)
            logger.info("Successfully refreshed Google access token.")
            return new_token
        except httpx.HTTPError as e:
            raise SyncAPIError(f"Network error during Google token refresh: {e}") from e

    def _request(
        self,
        method: str,
        path: str,
        params: Optional[dict[str, Any]] = None,
        json_data: Optional[dict[str, Any]] = None,
        allow_refresh: bool = True,
    ) -> dict[str, Any]:
        """Executes an HTTP request against Google People API with exponential backoff and auth recovery."""
        url = f"{self.BASE_URL}/{path.lstrip('/')}"
        attempt = 0

        while True:
            attempt += 1
            try:
                response = self._client.request(
                    method=method,
                    url=url,
                    headers=self._get_headers(),
                    params=params,
                    json=json_data,
                )

                if response.status_code == 401 and allow_refresh and self.refresh_token:
                    logger.warning("Google access token expired (HTTP 401). Attempting token refresh...")
                    self._refresh_access_token()
                    return self._request(method, path, params, json_data, allow_refresh=False)

                if response.status_code == 401:
                    raise TokenExpiredError(provider="Google", status_code=401, response_body=response.text)

                if response.status_code == 403:
                    raise AuthenticationError(provider="Google", message=response.text, status_code=403, response_body=response.text)

                if response.status_code == 404:
                    raise ContactNotFoundError(resource_id=path, provider="Google", status_code=404, response_body=response.text)

                if response.status_code in (429, 503):
                    if attempt > self.max_retries:
                        raise RateLimitError(
                            message=f"Google API rate limit exceeded after {self.max_retries} retries",
                            retry_after_seconds=self.base_backoff * (2 ** (attempt - 1)),
                            status_code=response.status_code,
                            response_body=response.text,
                        )
                    sleep_time = self.base_backoff * (2 ** (attempt - 1))
                    logger.warning("Google API rate limited (%d). Retrying in %.2fs...", response.status_code, sleep_time)
                    time.sleep(sleep_time)
                    continue

                if response.status_code >= 400:
                    raise SyncAPIError(
                        message=f"Google People API error: {response.text}",
                        status_code=response.status_code,
                        response_body=response.text,
                    )

                if response.status_code == 204 or not response.content:
                    return {}

                return response.json()

            except httpx.RequestError as exc:
                if attempt > self.max_retries:
                    raise SyncAPIError(f"HTTP connection error contacting Google People API: {exc}") from exc
                time.sleep(self.base_backoff * (2 ** (attempt - 1)))

    def get_connections(
        self,
        page_size: int = 100,
        page_token: Optional[str] = None,
        person_fields: str = DEFAULT_PERSON_FIELDS,
    ) -> tuple[list[UnifiedContact], Optional[str]]:
        """Retrieves a page of user's Google contacts.
        
        Returns:
            tuple: (List of UnifiedContact instances, next_page_token or None)
        """
        params: dict[str, Any] = {
            "personFields": person_fields,
            "pageSize": min(page_size, 1000),
        }
        if page_token:
            params["pageToken"] = page_token

        data = self._request("GET", "people/me/connections", params=params)
        connections = data.get("connections", [])
        contacts = [UnifiedContact.from_google_person(item) for item in connections]
        next_token = data.get("nextPageToken")
        return contacts, next_token

    def get_all_contacts(self, person_fields: str = DEFAULT_PERSON_FIELDS) -> list[UnifiedContact]:
        """Paginates through all Google Contacts connections and returns unified models."""
        all_contacts: list[UnifiedContact] = []
        page_token: Optional[str] = None

        while True:
            contacts, page_token = self.get_connections(
                page_size=100, page_token=page_token, person_fields=person_fields
            )
            all_contacts.extend(contacts)
            if not page_token:
                break

        return all_contacts

    def create_contact(self, contact: UnifiedContact) -> UnifiedContact:
        """Creates a new contact in Google Contacts."""
        payload = contact.to_google_payload(include_etag=False)
        params = {"personFields": self.DEFAULT_PERSON_FIELDS}
        res = self._request("POST", "people:createContact", params=params, json_data=payload)
        return UnifiedContact.from_google_person(res)

    def update_contact(
        self,
        contact: UnifiedContact,
        update_person_fields: str = "names,emailAddresses,phoneNumbers,organizations,addresses,biographies",
    ) -> UnifiedContact:
        """Updates an existing contact in Google Contacts."""
        if not contact.google_resource_name:
            raise ValueError("Cannot update Google contact without a valid 'google_resource_name'")
        if not contact.google_etag:
            raise ValueError("Cannot update Google contact without a valid 'google_etag' concurrency token")

        payload = contact.to_google_payload(include_etag=True)
        params = {"updatePersonFields": update_person_fields}
        path = f"{contact.google_resource_name}:updateContact"
        res = self._request("PATCH", path, params=params, json_data=payload)
        return UnifiedContact.from_google_person(res)

    def delete_contact(self, resource_name: str) -> bool:
        """Deletes a contact in Google Contacts."""
        if not resource_name.startswith("people/"):
            resource_name = f"people/{resource_name}"
        path = f"{resource_name}:deleteContact"
        self._request("DELETE", path)
        return True
