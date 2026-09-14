"""Resolve DataHub principal URNs to email addresses.

Pure by design: this module takes the payload of the batch ``entities(urns:)``
query and returns addresses. The network call lives in
``DataHubClient.resolve_principals``, which keeps the fallback rules testable
without HTTP.

The fallback chain is load-bearing, not defensive. Verified against a live
instance: ``urn:li:corpuser:admin`` has both ``properties.email`` and
``editableProperties.email`` null, while ``urn:li:corpuser:a.user@example.com``
resolves from ``properties.email``, and many users carry the address only in
``username``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Recipient:
    urn: str
    email: str
    display_name: str


def _first_non_empty(*values: str | None) -> str | None:
    for value in values:
        if value and value.strip():
            return value.strip()
    return None


def address_for(entity: dict) -> str | None:
    """editableProperties.email -> properties.email -> email-shaped username."""
    editable = entity.get("editableProperties") or {}
    properties = entity.get("properties") or {}

    address = _first_non_empty(editable.get("email"), properties.get("email"))
    if address:
        return address

    username = _first_non_empty(entity.get("username"))
    if username and "@" in username:
        return username

    return None


def display_name_for(entity: dict) -> str:
    properties = entity.get("properties") or {}
    return (
        _first_non_empty(
            properties.get("displayName"),
            entity.get("username"),
            entity.get("name"),
            entity.get("urn"),
        )
        or ""
    )


def recipients_from_entities(entities) -> tuple[list[Recipient], list[str]]:
    """Return ``(resolved, unresolved_urns)``.

    Order is preserved, and duplicates collapse both by URN and by address --
    a requester who is also a listed data steward gets one email. An
    unresolvable principal is warned about and reported rather than silently
    dropped, so a systematically unreachable steward is visible; it must not
    block the notice to everyone else.
    """
    resolved: list[Recipient] = []
    unresolved: list[str] = []
    seen_urns: set[str] = set()
    seen_emails: set[str] = set()

    for entity in entities or []:
        if not entity:
            continue

        urn = entity.get("urn") or ""
        if not urn or urn in seen_urns:
            continue
        seen_urns.add(urn)

        address = address_for(entity)
        if not address:
            logger.warning(
                "No email address for %s; it will not receive this notification. "
                "Set an email on the user or group in DataHub.",
                urn,
            )
            unresolved.append(urn)
            continue

        lowered = address.lower()
        if lowered in seen_emails:
            continue
        seen_emails.add(lowered)

        resolved.append(
            Recipient(urn=urn, email=address, display_name=display_name_for(entity))
        )

    return resolved, unresolved
