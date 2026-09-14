"""Extraction helpers for Action Workflow form payloads.

Two shapes in the live data drive this module's design:

1. ``PropertyValue`` is a GraphQL union of exactly ``StringValue`` and
   ``NumberValue``. URN-typed and DATE-typed form fields therefore arrive as
   plain strings, not typed objects.
2. Unfilled optional fields arrive as ``"values": []`` — an empty list, never
   ``null`` and never a missing key.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Iterable

from actions.errors import MissingFieldError

MAX_SLUG_LENGTH = 64

_NON_SLUG_CHARS = re.compile(r"[^a-z0-9]+")


def _value_to_scalar(value: dict[str, Any]) -> Any:
    if "stringValue" in value and value["stringValue"] is not None:
        return value["stringValue"]
    if "numberValue" in value and value["numberValue"] is not None:
        return value["numberValue"]
    return None


class FormFields:
    """Read-only accessor over a ``workflowFormRequest.fields`` list."""

    def __init__(self, raw: Iterable[dict[str, Any]] | None) -> None:
        self._by_id: dict[str, list[Any]] = {}
        for field in raw or []:
            field_id = field.get("id")
            if field_id is None:
                continue
            scalars = [
                scalar
                for scalar in (_value_to_scalar(v) for v in (field.get("values") or []))
                if scalar is not None
            ]
            self._by_id[field_id] = scalars

    def ids(self) -> list[str]:
        """Field ids present on the submission, in payload order.

        Needed for fields whose ids are generated rather than fixed -- the
        Level 2 dropdowns are one per parent domain, so a handler has to
        discover which one the requester actually filled in.
        """
        return list(self._by_id)

    def all(self, field_id: str) -> list[str]:
        return [str(v) for v in self._by_id.get(field_id, [])]

    def one(self, field_id: str) -> str | None:
        values = self.all(field_id)
        return values[0] if values else None

    def require(self, field_id: str) -> str:
        value = self.one(field_id)
        if value is None or not value.strip():
            raise MissingFieldError(f"Required form field {field_id!r} is missing or empty")
        return value

    def number(self, field_id: str) -> float | None:
        values = self._by_id.get(field_id) or []
        if not values:
            return None
        try:
            return float(values[0])
        except (TypeError, ValueError):
            return None


def slugify(name: str, max_length: int = MAX_SLUG_LENGTH) -> str:
    """Derive a stable, readable entity id from a display name.

    Deterministic by design: the same proposed name always yields the same id,
    which is what makes event replay converge instead of duplicating entities.
    """
    ascii_only = (
        unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    )
    collapsed = _NON_SLUG_CHARS.sub("_", ascii_only.lower()).strip("_")
    if not collapsed:
        raise ValueError(f"Cannot derive a slug from name: {name!r}")

    result = collapsed[:max_length]

    # If we truncated in the middle of a word, back up to the previous boundary
    if (len(collapsed) > max_length and
        max_length > 0 and
        result and
        result[-1].isalnum() and
        collapsed[max_length].isalnum()):
        # Both last char of truncated and next char are alphanumeric
        # Back up to the previous underscore boundary
        last_underscore = result.rfind("_")
        if last_underscore > 0:
            result = result[:last_underscore]

    return result.rstrip("_")
