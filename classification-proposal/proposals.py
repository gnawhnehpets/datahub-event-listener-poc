"""Pure generation of randomised classification tag proposals.

Deliberately free of network, clock and global RNG so the interesting
behaviour -- confidence bucketing and the column/asset split -- is testable
without a DataHub instance. All I/O lives in datahub_client.py.
"""

from __future__ import annotations

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
