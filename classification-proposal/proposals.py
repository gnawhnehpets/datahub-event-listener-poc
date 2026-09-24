"""Pure generation of randomised classification tag proposals.

Deliberately free of network, clock and global RNG so the interesting
behaviour -- confidence bucketing and the column/asset split -- is testable
without a DataHub instance. All I/O lives in datahub_client.py.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

CONFIDENCE_MIN = 0.50
CONFIDENCE_MAX = 0.99

_MEDIUM_FLOOR = 0.70
_HIGH_FLOOR = 0.85


def bucket_for(score: float) -> tuple[int, str]:
    """Map a raw score to DataHub's (confidenceLevel, human label).

    Boundaries are half-open on the upper end: 0.70 is Medium, 0.85 is High.
    Bucketing uses the unrounded score on purpose. Rounding first would let
    the native confidenceLevel disagree with the score actually drawn, and
    that field is the one a reviewer filters on.
    """
    if score < _MEDIUM_FLOOR:
        return 0, "Low"
    if score < _HIGH_FLOOR:
        return 1, "Medium"
    return 2, "High"


def format_note(score: float) -> str:
    """The proposalNote text, e.g. 'confidence: 87.342% (High)'.

    Three decimals, always padded, so a column of notes lines up when read.
    """
    _, label = bucket_for(score)
    return f"confidence: {score * 100:.3f}% ({label})"


_WORKSPACE = "testworkspaceforpoc.default"


def _dataset_urn(table: str) -> str:
    return f"urn:li:dataset:(urn:li:dataPlatform:databricks,{_WORKSPACE}.{table},PROD)"


# The five business tables. The other 29 datasets in this workspace are
# Databricks information_schema system tables -- classification tags on
# information_schema.columns would be noise, so they are excluded.
ASSETS: dict[str, str] = {
    "banking_accounts": _dataset_urn("banking_accounts"),
    "banking_loans": _dataset_urn("banking_loans"),
    "banking_customers": _dataset_urn("banking_customers"),
    "banking_transactions": _dataset_urn("banking_transactions"),
    "pattern_analysis": _dataset_urn("pattern_analysis"),
}

# Classification levels and PII only. Every URN verified to exist on pro-lemur.
TAG_POOL: tuple[tuple[str, str], ...] = (
    ("PII", "urn:li:tag:PII"),
    ("PII_Data", "urn:li:tag:sample_data_PII_Data"),
    ("Confidential", "urn:li:tag:Confidential"),
    ("Restricted", "urn:li:tag:Restricted"),
    ("Public", "urn:li:tag:Public"),
)


@dataclass(frozen=True)
class Proposal:
    """One tag proposal. sub_resource None means asset-level."""

    resource_urn: str
    tag_urn: str
    tag_name: str
    score: float
    confidence_level: int
    note: str
    sub_resource: str | None


def _draw(rng: random.Random, resource_urn: str, sub_resource: str | None) -> Proposal:
    tag_name, tag_urn = rng.choice(TAG_POOL)
    score = rng.uniform(CONFIDENCE_MIN, CONFIDENCE_MAX)
    level, _ = bucket_for(score)
    return Proposal(
        resource_urn=resource_urn,
        tag_urn=tag_urn,
        tag_name=tag_name,
        score=score,
        confidence_level=level,
        note=format_note(score),
        sub_resource=sub_resource,
    )


def generate_proposals(
    asset_columns: dict[str, list[str]],
    count: int,
    rng: random.Random,
) -> list[Proposal]:
    """Build one asset-level proposal per dataset, plus `count` column-level.

    `count` covers column-level proposals only; asset-level ones are additional,
    one per dataset touched. Assets are walked in sorted order so a given seed
    always produces the same batch regardless of dict insertion order.

    Duplicate (column, tag) pairs are not deduped -- multiple proposals on one
    attribute is a requirement.

    Constraint: every key in `asset_columns` must exist in the `ASSETS` global.
    An unknown key raises `KeyError` because URNs are derived from `ASSETS` at
    draw time. Callers are responsible for passing only keys that appear there.
    """
    names = sorted(asset_columns)
    out = [_draw(rng, ASSETS[name], None) for name in names]
    for _ in range(count):
        name = rng.choice(names)
        column = rng.choice(asset_columns[name])
        out.append(_draw(rng, ASSETS[name], column))
    return out
