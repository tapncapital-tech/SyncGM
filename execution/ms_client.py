"""Microsoft Contacts API Client using Microsoft Graph API v1.0."""
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


class MicrosoftContactsClient:
    """Client for interacting with Microsoft Graph API v1.0 for Contacts synchronization."""

    BASE_URL = "https://graph.microsoft.com/v1.0"
    DEFAULT_SELECT = (
        "id,displayName,givenName,surname,middleName,title,generation,"
        "emailAddresses,mobilePhone,businessPhones,homePhones,companyName,"
        "department,jobTitle,businessAddress,homeAddress,otherAddress,personalNotes,changeKey,lastModifiedDateTime"
    )
    TOKEN_ENDPOINT = "https://login.microsoftonline.com/common/oauth2/v2.0/token"

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

    def _get_headers(self, if_match: Optional[str] = None) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if if_match:
            headers["If-Match"] = if_match
        return headers

    def _refresh_access_token(self) -> str:
        """Refreshes the Microsoft OAuth 2.0 access token using the stored refresh token."""
        if not self.refresh_token or not self.client_id or not self.client_secret:
            raise TokenExpiredError(provider="Microsoft", message="Missing OAuth refresh credentials for Microsoft")

        payload = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "refresh_token": self.refresh_token,
            "grant_type": "refresh_token",
            "scope": "Contacts.ReadWrite offline_access",
        }
        try:
            resp = self._client.post(self.TOKEN_ENDPOINT, data=payload)
            if resp.status_code != 200:
                raise AuthenticationError(
                    provider="Microsoft",
                    message=f"Failed to refresh Microsoft token: {resp.text}",
                    status_code=resp.status_code,
                    response_body=resp.text,
                )
            data = resp.json()
            new_token = data.get("access_token")
            if not new_token:
                raise AuthenticationError(provider="Microsoft", message="No access_token in refresh response")

            self.access_token = new_token
            if self.token_refresh_callback:
                self.token_refresh_callback(new_token)
            logger.info("Successfully refreshed Microsoft access token.")
            return new_token
        except httpx.HTTPError as e:
            raise SyncAPIError(f"Network error during Microsoft token refresh: {e}") from e

    def _request(
        self,
        method: str,
        url_or_path: str,
        params: Optional[dict[str, Any]] = None,
        json_data: Optional[dict[str, Any]] = None,
        if_match: Optional[str] = None,
        allow_refresh: bool = True,
    ) -> dict[str, Any]:
        """Executes an HTTP request against Microsoft Graph API with rate limit handling and token refresh."""
        if url_or_path.startswith("http://") or url_or_path.startswith("https://"):
            url = url_or_path
        else:
            url = f"{self.BASE_URL}/{url_or_path.lstrip('/')}"

        attempt = 0

        while True:
            attempt += 1
            try:
                response = self._client.request(
                    method=method,
                    url=url,
                    headers=self._get_headers(if_match=if_match),
                    params=params,
                    json=json_data,
                )

                if response.status_code == 401 and allow_refresh and self.refresh_token:
                    logger.warning("Microsoft access token expired (HTTP 401). Attempting token refresh...")
                    self._refresh_access_token()
                    return self._request(
                        method=method,
                        url_or_path=url_or_path,
                        params=params,
                        json_data=json_data,
                        if_match=if_match,
                        allow_refresh=False,
                    )

                if response.status_code == 401:
                    raise TokenExpiredError(provider="Microsoft", status_code=401, response_body=response.text)

                if response.status_code == 403:
                    raise AuthenticationError(provider="Microsoft", message=response.text, status_code=403, response_body=response.text)

                if response.status_code == 404:
                    raise ContactNotFoundError(resource_id=url_or_path, provider="Microsoft", status_code=404, response_body=response.text)

                if response.status_code in (429, 503):
                    # Check for Retry-After header
                    retry_after = response.headers.get("Retry-After")
                    if retry_after and retry_after.isdigit():
                        sleep_seconds = float(retry_after)
                    else:
                        sleep_seconds = self.base_backoff * (2 ** (attempt - 1))

                    if attempt > self.max_retries:
                        raise RateLimitError(
                            message=f"Microsoft Graph rate limit exceeded after {self.max_retries} retries",
                            retry_after_seconds=sleep_seconds,
                            status_code=response.status_code,
                            response_body=response.text,
                        )

                    logger.warning("Microsoft Graph rate limited (%d). Retrying in %.2fs...", response.status_code, sleep_seconds)
                    time.sleep(sleep_seconds)
                    continue

                if response.status_code >= 400:
                    raise SyncAPIError(
                        message=f"Microsoft Graph API error: {response.text}",
                        status_code=response.status_code,
                        response_body=response.text,
                    )

                if response.status_code == 204 or not response.content:
                    return {}

                return response.json()

            except httpx.RequestError as exc:
                if attempt > self.max_retries:
                    raise SyncAPIError(f"HTTP connection error contacting Microsoft Graph API: {exc}") from exc
                time.sleep(self.base_backoff * (2 ** (attempt - 1)))

    def get_contacts(
        self,
        top: int = 50,
        skip: int = 0,
        next_link: Optional[str] = None,
        select: str = DEFAULT_SELECT,
    ) -> tuple[list[UnifiedContact], Optional[str]]:
        """Retrieves a single page of Microsoft contacts."""
        if next_link:
            data = self._request("GET", next_link)
        else:
            params: dict[str, Any] = {
                "$select": select,
                "$top": min(top, 1000),
            }
            if skip > 0:
                params["$skip"] = skip
            data = self._request("GET", "me/contacts", params=params)

        values = data.get("value", [])
        contacts = [UnifiedContact.from_ms_contact(item) for item in values]
        next_url = data.get("@odata.nextLink")
        return contacts, next_url

    def get_all_contacts(self, select: str = DEFAULT_SELECT) -> list[UnifiedContact]:
        """Paginates through all Microsoft contacts and returns unified models."""
        all_contacts: list[UnifiedContact] = []
        next_link: Optional[str] = None

        while True:
            contacts, next_link = self.get_contacts(top=50, next_link=next_link, select=select)
            all_contacts.extend(contacts)
            if not next_link:
                break

        return all_contacts

    def create_contact(self, contact: UnifiedContact) -> UnifiedContact:
        """Creates a new contact record in Microsoft Contacts."""
        payload = contact.to_ms_payload()
        res = self._request("POST", "me/contacts", json_data=payload)
        return UnifiedContact.from_ms_contact(res)

    def update_contact(self, contact: UnifiedContact, use_optimistic_locking: bool = False) -> UnifiedContact:
        """Updates an existing contact record in Microsoft Contacts."""
        if not contact.ms_id:
            raise ValueError("Cannot update Microsoft contact without a valid 'ms_id'")

        payload = contact.to_ms_payload()
        if_match = contact.ms_etag if (use_optimistic_locking and contact.ms_etag) else None
        res = self._request("PATCH", f"me/contacts/{contact.ms_id}", json_data=payload, if_match=if_match)
        return UnifiedContact.from_ms_contact(res)

    def delete_contact(self, contact_id: str) -> bool:
        """Deletes a contact in Microsoft Contacts."""
        self._request("DELETE", f"me/contacts/{contact_id}")
        return True
