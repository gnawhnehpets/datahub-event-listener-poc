# Classification Tag Proposals Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A CLI script that files randomised classification/PII tag proposals — column-level and asset-level — against the `testworkspaceforpoc` Databricks datasets on pro-lemur, each carrying a confidence score in both the proposal Note and DataHub's native `confidenceLevel`.

**Architecture:** Three modules with a hard I/O boundary. `proposals.py` is pure — given assets, columns and a seeded RNG it returns `Proposal` objects and never touches the network, which is where all the logic worth testing lives. `datahub_client.py` owns every network call plus credential loading. `propose_classifications.py` is argument parsing and output formatting only.

**Tech Stack:** Python 3, `requests` (already in `requirements.txt`), `PyYAML` (transitive via `acryl-datahub-actions`, used by `tests/test_behaviour.py` already), `pytest`.

## Global Constraints

- Target deployment: pro-lemur, GraphQL at `https://pro-lemur-fda34e845.trials.acryl.io/api/graphql`.
- Script directory is `classification-proposal/` at repo root — fixed by the owner, do not relocate.
- Tag pool is exactly 5 URNs; no others may be proposed.
- Asset pool is exactly the 5 `testworkspaceforpoc.default.*` business tables; `information_schema` tables are excluded.
- Confidence score range `[0.50, 0.99]`; buckets `<0.70` → 0 Low, `<0.85` → 1 Medium, else 2 High. Bucketing uses the **unrounded** score.
- Note text is exactly `confidence: {score*100:.3f}% ({label})` — 3 decimals, zero-padded.
- Column-level proposals always set both `subResourceType` and `subResource`; asset-level proposals omit both.
- `--count` is column-level proposals only. Asset-level is one extra per dataset touched.
- Duplicates are **never** deduped.
- No dry-run mode. The script writes on invocation.
- Follow repo style: `from __future__ import annotations`, module docstrings that explain *why*, full type hints, `logging` not `print` for diagnostics.
- Commit messages: conventional prefix, why-focused body, trailer `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.

## File Structure

| File | Responsibility |
|---|---|
| `classification-proposal/proposals.py` | Tag pool, asset catalog, confidence maths, proposal generation. Pure — no network, no clock, no global RNG. |
| `classification-proposal/datahub_client.py` | Credential load, token expiry check, GraphQL transport, `fetch_columns`, `propose_tags`. All I/O. |
| `classification-proposal/propose_classifications.py` | argparse, wiring, summary table. No logic. |
| `classification-proposal/tests/test_proposals.py` | Unit tests for the pure module. |
| `classification-proposal/tests/test_credentials.py` | Unit tests for credential/expiry parsing (pure, no network). |
| `classification-proposal/README.md` | Usage, credential setup, the no-dry-run warning. |
| `pytest.ini` | Modified — add the new dir to `pythonpath` and `testpaths`. |

**Note on the hyphen:** `classification-proposal` is not a valid Python package name, so these are top-level modules reached by putting the directory itself on `pythonpath`. That is why Task 1 edits `pytest.ini` rather than adding an `__init__.py`.

**Deviation from spec:** the spec says generation returns "proposal dicts". This plan uses a frozen `Proposal` dataclass instead — same purity, but typos in field names fail at construction rather than silently producing a malformed GraphQL variable. Everything else follows the spec.

---

### Task 1: Confidence maths and test wiring

**Files:**
- Create: `classification-proposal/proposals.py`
- Create: `classification-proposal/tests/test_proposals.py`
- Modify: `pytest.ini`

**Interfaces:**
- Consumes: nothing.
- Produces: `bucket_for(score: float) -> tuple[int, str]` returning `(confidence_level, label)`; `format_note(score: float) -> str`; constants `CONFIDENCE_MIN = 0.50`, `CONFIDENCE_MAX = 0.99`.

- [ ] **Step 1: Wire the new test directory into pytest**

Replace the whole of `pytest.ini` with:

```ini
[pytest]
pythonpath = src classification-proposal
testpaths = tests classification-proposal/tests
addopts = -q
```

Both settings take space-separated lists. Without this, bare `pytest` silently ignores the new tests — they would appear to pass by not running.

- [ ] **Step 2: Write the failing tests**

Create `classification-proposal/tests/test_proposals.py`:

```python
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
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `cd /Users/stephen/Desktop/github/datahub-event-listener-poc && pytest classification-proposal/tests/test_proposals.py`
Expected: FAIL — `ModuleNotFoundError: No module named 'proposals'`

- [ ] **Step 4: Write the implementation**

Create `classification-proposal/proposals.py`:

```python
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
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd /Users/stephen/Desktop/github/datahub-event-listener-poc && pytest classification-proposal/tests/test_proposals.py`
Expected: PASS (10 tests)

- [ ] **Step 6: Verify the existing suite still runs**

Run: `cd /Users/stephen/Desktop/github/datahub-event-listener-poc && pytest`
Expected: PASS — both `tests/` and `classification-proposal/tests/` collected. If `tests/test_behaviour.py` errors on a missing import, the `pythonpath` edit dropped `src`; restore it.

- [ ] **Step 7: Commit**

```bash
cd /Users/stephen/Desktop/github/datahub-event-listener-poc
git add pytest.ini classification-proposal/proposals.py classification-proposal/tests/test_proposals.py
git commit -m "$(cat <<'MSG'
feat: confidence bucketing for classification proposals

DataHub's native confidenceLevel holds only three buckets, so a demo showing
87.342% needs the precise score in the note text and the bucket in the API
field. Both derive from one draw here so they cannot disagree.

Bucketing uses the unrounded score deliberately: rounding first would make the
native field disagree with the score drawn, and that is the field a reviewer
filters on.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
MSG
)"
```

---

### Task 2: Asset catalog, tag pool and proposal generation

**Files:**
- Modify: `classification-proposal/proposals.py`
- Modify: `classification-proposal/tests/test_proposals.py`

**Interfaces:**
- Consumes: `bucket_for`, `format_note`, `CONFIDENCE_MIN`, `CONFIDENCE_MAX` from Task 1.
- Produces:
  - `TAG_POOL: tuple[tuple[str, str], ...]` — `(name, urn)` pairs.
  - `ASSETS: dict[str, str]` — short name → dataset URN.
  - `Proposal` frozen dataclass with fields `resource_urn: str`, `tag_urn: str`, `tag_name: str`, `score: float`, `confidence_level: int`, `note: str`, `sub_resource: str | None`.
  - `generate_proposals(asset_columns: dict[str, list[str]], count: int, rng: random.Random) -> list[Proposal]`.

- [ ] **Step 1: Write the failing tests**

Append to `classification-proposal/tests/test_proposals.py`:

```python
import dataclasses
import random

from proposals import ASSETS, TAG_POOL, Proposal, generate_proposals

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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/stephen/Desktop/github/datahub-event-listener-poc && pytest classification-proposal/tests/test_proposals.py`
Expected: FAIL — `ImportError: cannot import name 'ASSETS' from 'proposals'`

- [ ] **Step 3: Write the implementation**

Add to `classification-proposal/proposals.py` — imports at top of file, the rest appended:

```python
import random
from dataclasses import dataclass

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
    """
    names = sorted(asset_columns)
    out = [_draw(rng, ASSETS[name], None) for name in names]
    for _ in range(count):
        name = rng.choice(names)
        column = rng.choice(asset_columns[name])
        out.append(_draw(rng, ASSETS[name], column))
    return out
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/stephen/Desktop/github/datahub-event-listener-poc && pytest classification-proposal/tests/test_proposals.py`
Expected: PASS (21 tests)

- [ ] **Step 5: Commit**

```bash
cd /Users/stephen/Desktop/github/datahub-event-listener-poc
git add classification-proposal/proposals.py classification-proposal/tests/test_proposals.py
git commit -m "$(cat <<'MSG'
feat: generate randomised column and asset level tag proposals

Assets are walked in sorted order rather than dict order so a seed reproduces
the same batch regardless of how the caller built the mapping.

Duplicate (column, tag) pairs are deliberately not deduped; filing several
proposals against one attribute is the behaviour being demonstrated. The
single-column test pins that by leaving generation no way to vary the pair.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
MSG
)"
```

---

### Task 3: Credential loading and token expiry validation

**Files:**
- Create: `classification-proposal/datahub_client.py`
- Create: `classification-proposal/tests/test_credentials.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces:
  - `CredentialError(Exception)`.
  - `Credentials` frozen dataclass with `graphql_url: str`, `token: str`.
  - `graphql_url_from_server(server: str) -> str`.
  - `token_expiry(token: str) -> datetime` (timezone-aware UTC).
  - `load_credentials(path: str | pathlib.Path | None = None, now: datetime | None = None) -> Credentials`.

- [ ] **Step 1: Write the failing tests**

Create `classification-proposal/tests/test_credentials.py`:

```python
"""Credential parsing and expiry checks.

Pure: no network. The point of testing this at all is that an expired token
must fail before the first write, not as a bare 401 midway through a batch.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import pathlib

import pytest
import yaml

from datahub_client import (
    CredentialError,
    graphql_url_from_server,
    load_credentials,
    token_expiry,
)


def make_token(exp: dt.datetime) -> str:
    """A JWT with only the fields the loader reads. Signature is never checked."""
    header = base64.urlsafe_b64encode(b'{"alg":"HS256"}').decode().rstrip("=")
    body = json.dumps({"actorId": "stephen.hwang@datahub.com", "exp": int(exp.timestamp())})
    payload = base64.urlsafe_b64encode(body.encode()).decode().rstrip("=")
    return f"{header}.{payload}.signature"


def write_env(tmp_path: pathlib.Path, token: str) -> pathlib.Path:
    path = tmp_path / "datahubenv"
    path.write_text(
        yaml.safe_dump(
            {"gms": {"server": "https://pro-lemur-fda34e845.trials.acryl.io/gms", "token": token}}
        )
    )
    return path


def test_graphql_url_replaces_gms_suffix() -> None:
    assert (
        graphql_url_from_server("https://pro-lemur-fda34e845.trials.acryl.io/gms")
        == "https://pro-lemur-fda34e845.trials.acryl.io/api/graphql"
    )


def test_graphql_url_tolerates_trailing_slash() -> None:
    assert (
        graphql_url_from_server("https://x.acryl.io/gms/")
        == "https://x.acryl.io/api/graphql"
    )


def test_token_expiry_is_utc_aware() -> None:
    when = dt.datetime(2026, 9, 2, 20, 9, 27, tzinfo=dt.timezone.utc)
    got = token_expiry(make_token(when))
    assert got == when
    assert got.tzinfo is not None


def test_valid_token_loads(tmp_path: pathlib.Path) -> None:
    now = dt.datetime(2026, 9, 23, tzinfo=dt.timezone.utc)
    path = write_env(tmp_path, make_token(now + dt.timedelta(days=1)))
    creds = load_credentials(path, now=now)
    assert creds.graphql_url.endswith("/api/graphql")
    assert creds.token


def test_expired_token_is_rejected_with_its_date(tmp_path: pathlib.Path) -> None:
    now = dt.datetime(2026, 9, 23, tzinfo=dt.timezone.utc)
    expired = dt.datetime(2026, 9, 2, 20, 9, 27, tzinfo=dt.timezone.utc)
    path = write_env(tmp_path, make_token(expired))
    with pytest.raises(CredentialError) as err:
        load_credentials(path, now=now)
    assert "2026-09-02" in str(err.value)


def test_missing_file_names_the_path(tmp_path: pathlib.Path) -> None:
    missing = tmp_path / "nope.yaml"
    with pytest.raises(CredentialError) as err:
        load_credentials(missing)
    assert "nope.yaml" in str(err.value)


def test_malformed_yaml_is_rejected(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text("gms:\n  server: https://x/gms\n")  # no token key
    with pytest.raises(CredentialError):
        load_credentials(path)


def test_unparseable_token_is_rejected(tmp_path: pathlib.Path) -> None:
    path = write_env(tmp_path, "not-a-jwt")
    with pytest.raises(CredentialError):
        load_credentials(path)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/stephen/Desktop/github/datahub-event-listener-poc && pytest classification-proposal/tests/test_credentials.py`
Expected: FAIL — `ModuleNotFoundError: No module named 'datahub_client'`

- [ ] **Step 3: Write the implementation**

Create `classification-proposal/datahub_client.py`:

```python
"""Credential loading and GraphQL transport for the proposal script.

Reads the standard DataHub CLI config rather than taking an acryl-datahub
dependency just to parse six lines of YAML.

The token's exp claim is decoded locally before anything is written. An
expired credential is the most likely failure here and a bare 401 from the
first mutation is a poor way to discover it.
"""

from __future__ import annotations

import base64
import binascii
import datetime as dt
import json
import logging
import os
import pathlib
from dataclasses import dataclass

import yaml

logger = logging.getLogger(__name__)

DEFAULT_ENV_FILE = "~/.datahubenv_prolemur"


class CredentialError(Exception):
    """Config is missing, malformed, or the token has expired."""


@dataclass(frozen=True)
class Credentials:
    graphql_url: str
    token: str


def graphql_url_from_server(server: str) -> str:
    """Turn the GMS server URL into the frontend GraphQL endpoint.

    Mutations are served by the frontend proxy at /api/graphql, not by
    /gms/graphql, which rejects these tokens with a 401.
    """
    return server.rstrip("/").removesuffix("/gms") + "/api/graphql"


def token_expiry(token: str) -> dt.datetime:
    """Read the exp claim. The signature is not verified -- the server does that."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        return dt.datetime.fromtimestamp(claims["exp"], dt.timezone.utc)
    except (IndexError, KeyError, ValueError, binascii.Error) as exc:
        raise CredentialError(f"could not read exp claim from token: {exc}") from exc


def load_credentials(
    path: str | pathlib.Path | None = None,
    now: dt.datetime | None = None,
) -> Credentials:
    """Load and validate credentials, or raise CredentialError."""
    raw = path or os.environ.get("DATAHUB_ENV_FILE") or DEFAULT_ENV_FILE
    resolved = pathlib.Path(raw).expanduser()
    if not resolved.is_file():
        raise CredentialError(f"credentials file not found: {resolved}")

    try:
        gms = (yaml.safe_load(resolved.read_text()) or {})["gms"]
        server, token = gms["server"], gms["token"]
    except (KeyError, TypeError, yaml.YAMLError) as exc:
        raise CredentialError(f"{resolved} is missing gms.server or gms.token: {exc}") from exc
    if not token:
        raise CredentialError(f"{resolved} has an empty gms.token")

    expires = token_expiry(token)
    moment = now or dt.datetime.now(dt.timezone.utc)
    if expires <= moment:
        raise CredentialError(
            f"token in {resolved} expired {expires:%Y-%m-%d %H:%M:%S UTC}. "
            f"Generate a new Personal Access Token in DataHub "
            f"(Settings -> Access Tokens) and replace gms.token."
        )

    logger.info("credentials valid until %s", f"{expires:%Y-%m-%d %H:%M:%S UTC}")
    return Credentials(graphql_url=graphql_url_from_server(server), token=token)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/stephen/Desktop/github/datahub-event-listener-poc && pytest classification-proposal/tests/test_credentials.py`
Expected: PASS (9 tests)

- [ ] **Step 5: Commit**

```bash
cd /Users/stephen/Desktop/github/datahub-event-listener-poc
git add classification-proposal/datahub_client.py classification-proposal/tests/test_credentials.py
git commit -m "$(cat <<'MSG'
feat: load and validate pro-lemur credentials before writing

The stored token expired on 2026-09-02, so an expiry check is not theoretical.
Decoding the exp claim locally turns the failure into a dated message naming
the file, instead of a 401 from the first mutation after some proposals may
already have been filed.

The signature is deliberately not verified; that is the server's job. Only the
exp claim is read.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
MSG
)"
```

---

### Task 4: GraphQL transport

**Files:**
- Modify: `classification-proposal/datahub_client.py`

**Interfaces:**
- Consumes: `Credentials` and `CredentialError` from Task 3; `Proposal` from Task 2.
- Produces: `DataHubError(Exception)`; `DataHubClient` with `__init__(self, creds: Credentials)`, `fetch_columns(self, dataset_urn: str) -> list[str]`, `propose_tag(self, proposal: Proposal) -> str` returning the created `actionRequest` URN.

Not unit-tested — thin I/O whose only real verification is a live call, per the spec. Task 5 exercises it.

- [ ] **Step 1: Add the transport**

Append to `classification-proposal/datahub_client.py`, adding `import requests` and `from proposals import Proposal` to the imports at the top:

```python
_COLUMNS_QUERY = """
query datasetColumns($urn: String!) {
  dataset(urn: $urn) {
    schemaMetadata { fields { fieldPath } }
  }
}
"""

_PROPOSE_TAGS = """
mutation proposeTags($input: ProposeTagsInput!) {
  proposeTags(input: $input)
}
"""


class DataHubError(Exception):
    """A GraphQL call failed or returned errors."""


class DataHubClient:
    """Only the two operations this script needs."""

    def __init__(self, creds: Credentials) -> None:
        self._url = creds.graphql_url
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"Bearer {creds.token}",
                "Content-Type": "application/json",
            }
        )

    def _execute(self, query: str, variables: dict) -> dict:
        try:
            response = self._session.post(
                self._url, json={"query": query, "variables": variables}, timeout=30
            )
        except requests.RequestException as exc:
            raise DataHubError(f"request failed: {exc}") from exc

        if response.status_code == 401:
            raise DataHubError("401 unauthorized -- the token was rejected")
        if response.status_code >= 400:
            raise DataHubError(f"HTTP {response.status_code}: {response.text[:300]}")

        body = response.json()
        if body.get("errors"):
            raise DataHubError("; ".join(e.get("message", str(e)) for e in body["errors"]))
        return body["data"]

    def fetch_columns(self, dataset_urn: str) -> list[str]:
        """Live fieldPaths, so the script does not drift as the POC data changes."""
        data = self._execute(_COLUMNS_QUERY, {"urn": dataset_urn})
        dataset = data.get("dataset")
        if not dataset or not dataset.get("schemaMetadata"):
            raise DataHubError(f"no schema found for {dataset_urn}")
        return [f["fieldPath"] for f in dataset["schemaMetadata"]["fields"]]

    def propose_tag(self, proposal: Proposal) -> str:
        """File one proposal. Returns the created actionRequest URN.

        origin INFERRED requires inferenceMetadata, which is where the native
        confidence bucket goes. The precise score travels in `description`,
        documented by the schema as "also called proposalNote".
        """
        payload: dict = {
            "resourceUrn": proposal.resource_urn,
            "tagUrns": [proposal.tag_urn],
            "description": proposal.note,
            "origin": "INFERRED",
            "inferenceMetadata": {
                "version": 1,
                "confidenceLevel": proposal.confidence_level,
            },
        }
        # Omitting both sub-resource fields is what makes this asset-level.
        if proposal.sub_resource is not None:
            payload["subResourceType"] = "DATASET_FIELD"
            payload["subResource"] = proposal.sub_resource

        return self._execute(_PROPOSE_TAGS, {"input": payload})["proposeTags"]
```

- [ ] **Step 2: Verify the module imports and the suite is unaffected**

Run: `cd /Users/stephen/Desktop/github/datahub-event-listener-poc && pytest`
Expected: PASS — 30 tests, no collection errors. A `ModuleNotFoundError: proposals` here means the `from proposals import Proposal` line needs `pythonpath` from Task 1; confirm `pytest.ini` still lists `classification-proposal`.

- [ ] **Step 3: Commit**

```bash
cd /Users/stephen/Desktop/github/datahub-event-listener-poc
git add classification-proposal/datahub_client.py
git commit -m "$(cat <<'MSG'
feat: GraphQL transport for fetching columns and filing tag proposals

Columns are read live rather than hardcoded so the script does not drift as
the POC datasets change.

proposeTags returns String! -- the created actionRequest URN -- which is what
lets the run print a batch that can later be found and rejected in bulk.

Sub-resource fields are set only for column-level proposals; omitting them is
precisely what makes DataHub treat a proposal as applying to the dataset.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
MSG
)"
```

---

### Task 5: CLI entrypoint and README

**Files:**
- Create: `classification-proposal/propose_classifications.py`
- Create: `classification-proposal/README.md`

**Interfaces:**
- Consumes: `ASSETS`, `generate_proposals` from Task 2; `load_credentials`, `CredentialError` from Task 3; `DataHubClient`, `DataHubError` from Task 4.
- Produces: the CLI. No importable API.

- [ ] **Step 1: Write the entrypoint**

Create `classification-proposal/propose_classifications.py`:

```python
#!/usr/bin/env python3
"""File randomised classification tag proposals against the POC datasets.

There is no dry-run, by explicit choice: invoking this writes. The planned
total is printed before anything is submitted so the arithmetic is visible
first, and every created actionRequest URN is printed after so a batch can be
found and rejected in bulk.

    python classification-proposal/propose_classifications.py --asset banking_accounts
"""

from __future__ import annotations

import argparse
import logging
import random
import sys

from datahub_client import CredentialError, DataHubClient, DataHubError, load_credentials
from proposals import ASSETS, generate_proposals

logger = logging.getLogger("propose")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--asset",
        help="short name from the catalog, or 'all'. Omit to list and exit.",
    )
    parser.add_argument(
        "--count",
        type=int,
        default=12,
        help="column-level proposals (default 12). Asset-level are extra: one per dataset.",
    )
    parser.add_argument("--seed", type=int, help="make a run reproducible")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args = parse_args(argv)

    # No --asset is not an error; it is the safe default for a tool that writes
    # on invocation. Guessing a target here would be worse than asking.
    if not args.asset:
        print("Choose a target with --asset. Available:\n")
        for name in sorted(ASSETS):
            print(f"  {name}")
        print("  all\n")
        return 1

    if args.asset == "all":
        chosen = sorted(ASSETS)
    elif args.asset in ASSETS:
        chosen = [args.asset]
    else:
        print(f"unknown asset {args.asset!r}. Known: {', '.join(sorted(ASSETS))}, all")
        return 1

    if args.count < 0:
        print("--count must be zero or greater")
        return 1

    try:
        creds = load_credentials()
    except CredentialError as exc:
        print(f"credentials: {exc}")
        return 1

    client = DataHubClient(creds)

    try:
        asset_columns = {name: client.fetch_columns(ASSETS[name]) for name in chosen}
    except DataHubError as exc:
        print(f"could not read schemas: {exc}")
        return 1

    empty = [name for name, cols in asset_columns.items() if not cols]
    if empty:
        print(f"no columns found for: {', '.join(empty)}")
        return 1

    rng = random.Random(args.seed)
    proposals = generate_proposals(asset_columns, count=args.count, rng=rng)

    asset_level = sum(1 for p in proposals if p.sub_resource is None)
    print(
        f"\nSubmitting {len(proposals)} proposals "
        f"({len(proposals) - asset_level} column-level + {asset_level} asset-level) "
        f"across {len(chosen)} dataset(s). No dry-run: these are live writes.\n"
    )

    created: list[tuple[str, str]] = []
    failed: list[tuple[str, str]] = []
    for proposal in proposals:
        target = proposal.sub_resource or "<asset-level>"
        label = f"{proposal.resource_urn.split(',')[1]}.{target} -> {proposal.tag_name}"
        try:
            # One failure must not cost the rest of the batch: a partial run
            # whose successes went unrecorded is the worst outcome here.
            urn = client.propose_tag(proposal)
        except DataHubError as exc:
            failed.append((label, str(exc)))
            print(f"  FAIL  {label}  {proposal.note}  -- {exc}")
        else:
            created.append((label, urn))
            print(f"  OK    {label}  {proposal.note}")

    print(f"\n{len(created)} created, {len(failed)} failed")
    if created:
        print("\nactionRequest URNs (use these to reject the batch):")
        for _, urn in created:
            print(f"  {urn}")
    return 1 if failed and not created else 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Verify the no-argument path writes nothing**

Run: `cd /Users/stephen/Desktop/github/datahub-event-listener-poc && python classification-proposal/propose_classifications.py; echo "exit=$?"`
Expected: prints the six choices and `exit=1`. No network call, no credential read.

- [ ] **Step 3: Verify an unknown asset is rejected**

Run: `cd /Users/stephen/Desktop/github/datahub-event-listener-poc && python classification-proposal/propose_classifications.py --asset nope; echo "exit=$?"`
Expected: `unknown asset 'nope'. Known: banking_accounts, ...` and `exit=1`.

- [ ] **Step 4: Verify the expired credential is reported before any write**

Run: `cd /Users/stephen/Desktop/github/datahub-event-listener-poc && python classification-proposal/propose_classifications.py --asset banking_accounts --count 1; echo "exit=$?"`
Expected, while the stored token is stale: `credentials: token in /Users/stephen/.datahubenv_prolemur expired 2026-09-02 20:09:27 UTC. Generate a new Personal Access Token ...` and `exit=1`. Nothing is submitted.

**This is the gate.** Do not proceed to Step 5 until a fresh Personal Access Token has been written to `gms.token` in `~/.datahubenv_prolemur`.

- [ ] **Step 5: First live run — the integration test**

Run: `cd /Users/stephen/Desktop/github/datahub-event-listener-poc && python classification-proposal/propose_classifications.py --asset banking_accounts --count 3 --seed 1`
Expected: a planned total of 4, then four `OK` lines each ending `confidence: NN.NNN% (Level)`, then four `urn:li:actionRequest:...` URNs.

`proposeTags` has not been exercised before this point; the mutation shape comes from schema introspection. If it errors, the message from the server appears on the `FAIL` line — fix the payload in `propose_tag` and re-run. Likely first failures: `inferenceMetadata.version` rejected, or `origin: INFERRED` demanding a field not sent.

- [ ] **Step 6: Confirm the proposals landed as the right kind**

Run:

```bash
cd /Users/stephen/Desktop/github/datahub-event-listener-poc
python - <<'PY'
import json, pathlib, urllib.request, yaml
cfg = yaml.safe_load(pathlib.Path("~/.datahubenv_prolemur").expanduser().read_text())
url = cfg["gms"]["server"].rstrip("/").removesuffix("/gms") + "/api/graphql"
q = """{ listActionRequests(input:{start:0,count:10,allActionRequests:true,
  resourceUrn:"urn:li:dataset:(urn:li:dataPlatform:databricks,testworkspaceforpoc.default.banking_accounts,PROD)"})
  { total actionRequests { urn type status subResource } } }"""
req = urllib.request.Request(url, json.dumps({"query": q}).encode(),
    {"Authorization": f"Bearer {cfg['gms']['token']}", "Content-Type": "application/json"})
for a in json.load(urllib.request.urlopen(req))["data"]["listActionRequests"]["actionRequests"]:
    print(a["type"], a["status"], "col=" + (a["subResource"] or "ASSET-LEVEL"))
PY
```

Expected: `TAG_ASSOCIATION PENDING` rows — three with a column name, one reading `ASSET-LEVEL`. That one line is the proof the sub-resource omission works.

- [ ] **Step 7: Write the README**

Create `classification-proposal/README.md`:

```markdown
# Classification tag proposals

Files randomised classification/PII tag proposals against the five
`testworkspaceforpoc` Databricks business tables on pro-lemur, to populate the
Task Center for demos.

## Warning: no dry-run

Invoking this **writes**. `--count 12` files 12 column-level proposals plus one
asset-level proposal per dataset touched, and re-running adds more rather than
replacing. The planned total prints before submission, and every created
`actionRequest` URN prints after so a batch can be rejected in bulk.

## Credentials

Reads `~/.datahubenv_prolemur` (override with `DATAHUB_ENV_FILE`):

```yaml
gms:
  server: https://pro-lemur-fda34e845.trials.acryl.io/gms
  token: <Personal Access Token>
```

Use a **Personal** token so proposals are attributed to you rather than
`admin`. Generate one in DataHub under Settings -> Access Tokens. The token's
expiry is checked locally before anything is written.

## Usage

```bash
python classification-proposal/propose_classifications.py               # list assets, write nothing
python classification-proposal/propose_classifications.py --asset banking_accounts
python classification-proposal/propose_classifications.py --asset all --count 20 --seed 7
```

| Flag | Meaning |
|---|---|
| `--asset` | `banking_accounts`, `banking_loans`, `banking_customers`, `banking_transactions`, `pattern_analysis`, or `all`. Omit to list and exit. |
| `--count` | Column-level proposals (default 12). Asset-level are **extra**: one per dataset. |
| `--seed` | Reproducible run. |

So `--asset all --count 12` files 17 proposals: 12 column-level + 5 asset-level.

## Confidence

Each proposal draws a score in `[0.50, 0.99]`, carried two ways:

- **Note** (`description`, DataHub's proposalNote): `confidence: 87.342% (High)`
- **Native** (`inferenceMetadata.confidenceLevel`): `0` Low `<0.70`, `1` Medium
  `<0.85`, `2` High otherwise

Both derive from one draw, so they cannot disagree. The native field only has
three buckets, which is why the precise score also goes in the note.

## Tests

`pytest` from the repo root runs these alongside the listener suite. The pure
module is covered; the GraphQL client is not, since its only real verification
is a live call.
```

- [ ] **Step 8: Run the full suite one last time**

Run: `cd /Users/stephen/Desktop/github/datahub-event-listener-poc && pytest`
Expected: PASS, 30 tests.

- [ ] **Step 9: Commit**

```bash
cd /Users/stephen/Desktop/github/datahub-event-listener-poc
git add classification-proposal/propose_classifications.py classification-proposal/README.md
git commit -m "$(cat <<'MSG'
feat: CLI for filing randomised classification tag proposals

Invoking with no --asset lists the catalog and exits without writing. For a
tool with no dry-run, guessing a target is worse than asking, so the safe path
is the default one.

Per-proposal failures are collected rather than fatal. Aborting mid-batch
would leave proposals filed with no record of which -- the same "writes
happened but nothing shows them" problem that made the original Task Center
investigation slow.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
MSG
)"
```

---

## Self-Review

**Spec coverage.** Every section maps to a task: API findings → Task 4 payload; Scope (5 assets, 5 tags) → Task 2 constants plus `test_asset_catalog_is_the_five_business_tables`; Design file layout → File Structure; column vs asset split → Task 2 generation and Task 4 payload; Confidence → Task 1; CLI → Task 5; Re-run behaviour → no code needed, it follows from not deduping, pinned by `test_duplicate_pairs_are_permitted`; Credentials → Task 3; Error handling → Task 5 loop; Testing → all nine spec bullets appear as named tests.

**Placeholder scan.** No TBD/TODO. Every code step carries real code; every run step carries the command and expected output.

**Type consistency.** `Proposal` field names are identical in Task 2 (definition), Task 2 tests, and Task 4 (`propose_tag`). `bucket_for` returns `tuple[int, str]` everywhere. `Credentials(graphql_url, token)` matches between Task 3 and `DataHubClient.__init__`. `propose_tag` is singular in both its definition and its Task 5 call site.

**Two gaps found and fixed inline.** The spec's testing list says "`--count` yields exactly that many column-level proposals" — this needed the column/asset split asserted separately, so `test_count_controls_column_level_only` filters on `sub_resource` rather than counting the whole list. And the spec never said where tests run from; the repo's `pytest.ini` has `testpaths = tests`, which would have silently skipped the new directory, so wiring it is now Task 1 Step 1 rather than an afterthought.

**One deliberate deviation from spec,** noted above: `Proposal` is a frozen dataclass, not a dict.
