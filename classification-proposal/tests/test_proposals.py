"""Unit tests for the pure proposal-generation module.

No network and no DataHub instance. Everything here is deterministic given a
seeded RNG, which is the whole reason generation was kept free of I/O.
"""

from __future__ import annotations

import dataclasses
import random

import pytest

from proposals import (
    ASSETS,
    CONFIDENCE_MAX,
    CONFIDENCE_MIN,
    TAG_POOL,
    Proposal,
    bucket_for,
    format_note,
    generate_proposals,
)


@pytest.mark.parametrize(
    ("score", "expected"),
    [
        (0.50, (0, "Low")),
        (0.6999, (0, "Low")),
        (0.70, (1, "Medium")),       # boundary: half-open, 0.70 is Medium
        (0.8499, (1, "Medium")),
        (0.85, (2, "High")),         # boundary: half-open, 0.85 is High
        (0.99, (2, "High")),
    ],
)
def test_bucket_boundaries(score: float, expected: tuple[int, str]) -> None:
    assert bucket_for(score) == expected


@pytest.mark.parametrize(
    ("score", "expected"),
    [
        (0.87342, "confidence: 87.342% (High)"),
        (0.50, "confidence: 50.000% (Low)"),      # zero-padded, not 50.0%
        (0.7, "confidence: 70.000% (Medium)"),
    ],
)
def test_note_format(score: float, expected: str) -> None:
    assert format_note(score) == expected


def test_note_label_agrees_with_bucket() -> None:
    """The note must never claim a level the native field would not carry."""
    for i in range(1000):
        score = 0.50 + (i * 0.00049)
        _, label = bucket_for(score)
        assert f"({label})" in format_note(score)


TAG_URNS = {urn for _, urn in TAG_POOL}

TWO_ASSETS = {
    "banking_accounts": ["account_id", "balance", "risk_flag"],
    "banking_loans": ["loan_amount", "interest_rate"],
}


def test_asset_catalog_is_the_five_business_tables() -> None:
    assert set(ASSETS) == {
        "banking_accounts",
        "banking_loans",
        "banking_customers",
        "banking_transactions",
        "pattern_analysis",
    }
    for urn in ASSETS.values():
        assert "testworkspaceforpoc.default." in urn
        assert "information_schema" not in urn


def test_seeded_generation_is_deterministic() -> None:
    a = generate_proposals(TWO_ASSETS, count=10, rng=random.Random(42))
    b = generate_proposals(TWO_ASSETS, count=10, rng=random.Random(42))
    assert a == b


def test_different_seeds_differ() -> None:
    a = generate_proposals(TWO_ASSETS, count=10, rng=random.Random(1))
    b = generate_proposals(TWO_ASSETS, count=10, rng=random.Random(2))
    assert a != b


def test_count_controls_column_level_only() -> None:
    out = generate_proposals(TWO_ASSETS, count=7, rng=random.Random(0))
    column_level = [p for p in out if p.sub_resource is not None]
    assert len(column_level) == 7


def test_exactly_one_asset_level_per_dataset_touched() -> None:
    out = generate_proposals(TWO_ASSETS, count=7, rng=random.Random(0))
    asset_level = [p for p in out if p.sub_resource is None]
    assert len(asset_level) == 2
    assert {p.resource_urn for p in asset_level} == {
        ASSETS["banking_accounts"],
        ASSETS["banking_loans"],
    }


def test_single_asset_yields_one_asset_level() -> None:
    one = {"banking_accounts": ["account_id", "balance"]}
    out = generate_proposals(one, count=5, rng=random.Random(3))
    assert len([p for p in out if p.sub_resource is None]) == 1
    assert len(out) == 6  # count + 1


def test_all_five_assets_yield_five_asset_level() -> None:
    every = {name: ["col_a", "col_b"] for name in ASSETS}
    out = generate_proposals(every, count=12, rng=random.Random(4))
    assert len([p for p in out if p.sub_resource is None]) == 5
    assert len(out) == 17  # 12 column + 5 asset


def test_tags_and_columns_are_in_bounds() -> None:
    out = generate_proposals(TWO_ASSETS, count=40, rng=random.Random(5))
    urn_to_name = {urn: name for name, urn in ASSETS.items()}
    for p in out:
        assert p.tag_urn in TAG_URNS
        if p.sub_resource is not None:
            assert p.sub_resource in TWO_ASSETS[urn_to_name[p.resource_urn]]


def test_confidence_fields_are_internally_consistent() -> None:
    out = generate_proposals(TWO_ASSETS, count=40, rng=random.Random(6))
    for p in out:
        assert CONFIDENCE_MIN <= p.score <= CONFIDENCE_MAX
        level, label = bucket_for(p.score)
        assert p.confidence_level == level
        assert p.note == format_note(p.score)
        assert f"({label})" in p.note


def test_duplicate_pairs_are_permitted() -> None:
    """Multiple proposals on one attribute is a requirement, not a bug.

    One column and one tag leaves generation no way to vary the pair, so every
    result must be the same (column, tag) -- proving nothing dedupes them.
    """
    single = {"banking_accounts": ["account_id"]}
    out = generate_proposals(single, count=5, rng=random.Random(7))
    column_level = [p for p in out if p.sub_resource is not None]
    assert len(column_level) == 5
    assert len({(p.resource_urn, p.sub_resource) for p in column_level}) == 1


def test_proposal_is_immutable() -> None:
    out = generate_proposals(TWO_ASSETS, count=1, rng=random.Random(8))
    with pytest.raises(dataclasses.FrozenInstanceError):
        out[0].score = 0.1  # type: ignore[misc]
