"""Unit tests for the pure proposal-generation module.

No network and no DataHub instance. Everything here is deterministic given a
seeded RNG, which is the whole reason generation was kept free of I/O.
"""

from __future__ import annotations

import pytest

from proposals import bucket_for, format_note


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
