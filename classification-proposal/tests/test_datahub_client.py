"""Tests for datahub_client internals that are pure (no network).

The I/O layer (DataHubClient) is not unit-tested — thin network code whose real
verification is a live call. But _build_payload is pure payload construction and
is explicitly NOT covered by the thin-I/O exemption: it decides whether DataHub
files a column-level or asset-level proposal, and an inverted condition silently
files the wrong kind against a live tenant.
"""

from __future__ import annotations

import pytest

from datahub_client import DataHubError, _build_payload
from proposals import ASSETS, Proposal, bucket_for, format_note


def _make_proposal(sub_resource: str | None = None, score: float = 0.87342) -> Proposal:
    level, _ = bucket_for(score)
    return Proposal(
        resource_urn=ASSETS["banking_accounts"],
        tag_urn="urn:li:tag:PII",
        tag_name="PII",
        score=score,
        confidence_level=level,
        note=format_note(score),
        sub_resource=sub_resource,
    )


def test_asset_level_payload_has_no_sub_resource_keys() -> None:
    """Omitting sub-resource fields is what tells DataHub this is asset-level.

    An inverted condition would silently file the wrong kind against a live tenant.
    """
    payload = _build_payload(_make_proposal(sub_resource=None))
    assert "subResourceType" not in payload
    assert "subResource" not in payload


def test_column_level_payload_has_dataset_field_type() -> None:
    payload = _build_payload(_make_proposal(sub_resource="account_id"))
    assert payload["subResourceType"] == "DATASET_FIELD"


def test_column_level_payload_sub_resource_matches_proposal() -> None:
    payload = _build_payload(_make_proposal(sub_resource="balance"))
    assert payload["subResource"] == "balance"


def test_payload_description_is_proposal_note() -> None:
    """description is the proposalNote — the precise score the reviewer sees."""
    proposal = _make_proposal(score=0.87342)
    payload = _build_payload(proposal)
    assert payload["description"] == proposal.note
    assert "87.342" in payload["description"]


def test_payload_inference_metadata_confidence_level() -> None:
    proposal = _make_proposal(score=0.87342)
    payload = _build_payload(proposal)
    assert payload["inferenceMetadata"]["confidenceLevel"] == proposal.confidence_level


def test_payload_origin_is_inferred() -> None:
    payload = _build_payload(_make_proposal())
    assert payload["origin"] == "INFERRED"


def test_payload_tag_urn_is_wrapped_in_list() -> None:
    payload = _build_payload(_make_proposal())
    assert payload["tagUrns"] == ["urn:li:tag:PII"]


def test_payload_resource_urn_matches_proposal() -> None:
    proposal = _make_proposal()
    payload = _build_payload(proposal)
    assert payload["resourceUrn"] == proposal.resource_urn


@pytest.mark.parametrize(
    ("score", "expected_level"),
    [
        (0.50, 0),   # Low
        (0.70, 1),   # Medium boundary
        (0.85, 2),   # High boundary
        (0.99, 2),   # High
    ],
)
def test_confidence_level_in_payload_matches_bucket(score: float, expected_level: int) -> None:
    proposal = _make_proposal(score=score)
    payload = _build_payload(proposal)
    assert payload["inferenceMetadata"]["confidenceLevel"] == expected_level
