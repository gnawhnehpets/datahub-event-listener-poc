"""Tests for the CLI entrypoint in propose_classifications.py.

Uses a fake client stub — NOT mocking requests or touching the network. The
stub is injected via monkeypatch so the tests exercise main() end-to-end
without a DataHub instance.

These tests pin the three behaviours changed in task 5's fix round:
- no --asset exits 1 without constructing a client
- unknown --asset exits 1
- partial failure exits 1 AND prints the URN block
And regression-test C1:
- KeyboardInterrupt mid-batch still prints the URN block
- a non-DataHubError exception mid-batch still prints the URN block
"""

from __future__ import annotations

import pytest

import propose_classifications
from datahub_client import Credentials, DataHubError
from proposals import ASSETS, Proposal, bucket_for, format_note


def _make_proposal(sub_resource: str | None = "account_id", score: float = 0.87342) -> Proposal:
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


class FakeClient:
    """Minimal stub that replaces DataHubClient for CLI tests.

    call_log tracks every propose_tag call so assertions can check partial
    progress after an interrupted or erroring batch.
    """

    def __init__(
        self,
        columns: list[str] | None = None,
        propose_side_effects: list | None = None,
    ) -> None:
        self._columns = columns or ["account_id", "balance"]
        # Each entry is either a string URN (success) or an exception to raise.
        self._effects = list(propose_side_effects or [])
        self.call_log: list[Proposal] = []

    def fetch_columns(self, dataset_urn: str) -> list[str]:
        return list(self._columns)

    def propose_tag(self, proposal: Proposal) -> str:
        self.call_log.append(proposal)
        if self._effects:
            effect = self._effects.pop(0)
            if isinstance(effect, BaseException):
                raise effect
            return str(effect)
        return f"urn:li:actionRequest:{len(self.call_log)}"


@pytest.fixture()
def fake_client(monkeypatch: pytest.MonkeyPatch) -> FakeClient:
    """Inject a FakeClient and stub out load_credentials so no file is read."""
    client = FakeClient()
    monkeypatch.setattr(
        propose_classifications,
        "load_credentials",
        lambda: Credentials(graphql_url="https://fake/api/graphql", token="fake"),
    )
    monkeypatch.setattr(
        propose_classifications,
        "DataHubClient",
        lambda creds: client,
    )
    return client


def test_no_asset_returns_1_and_never_constructs_client(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """No --asset is the safe default: list available assets and exit 1.

    Critically, no client is constructed and no credentials are read.
    """
    client_constructed = False

    def boom_creds():
        raise AssertionError("load_credentials should not be called")

    def boom_client(creds):
        nonlocal client_constructed
        client_constructed = True

    monkeypatch.setattr(propose_classifications, "load_credentials", boom_creds)
    monkeypatch.setattr(propose_classifications, "DataHubClient", boom_client)

    result = propose_classifications.main([])
    assert result == 1
    assert not client_constructed
    out = capsys.readouterr().out
    assert "banking_accounts" in out


def test_unknown_asset_returns_1(
    fake_client: FakeClient, capsys: pytest.CaptureFixture
) -> None:
    result = propose_classifications.main(["--asset", "nonexistent_table"])
    assert result == 1
    assert fake_client.call_log == []


def test_happy_path_returns_0_and_prints_urns(
    fake_client: FakeClient, capsys: pytest.CaptureFixture
) -> None:
    result = propose_classifications.main(["--asset", "banking_accounts", "--count", "2", "--seed", "1"])
    assert result == 0
    out = capsys.readouterr().out
    assert "urn:li:actionRequest:" in out


def test_partial_failure_returns_1_and_prints_urns(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """One DataHubError mid-batch: exit 1, but the URN block lists what succeeded.

    This is the case where a reviewer needs the URN list to clean up.
    """
    effects = [
        "urn:li:actionRequest:good-1",
        DataHubError("server rejected this one"),
        "urn:li:actionRequest:good-2",
    ]
    client = FakeClient(propose_side_effects=effects)
    monkeypatch.setattr(
        propose_classifications,
        "load_credentials",
        lambda: Credentials(graphql_url="https://fake/api/graphql", token="fake"),
    )
    monkeypatch.setattr(propose_classifications, "DataHubClient", lambda creds: client)

    result = propose_classifications.main(["--asset", "banking_accounts", "--count", "2", "--seed", "1"])
    assert result == 1
    out = capsys.readouterr().out
    # The URNs for successful proposals must be printed even though the batch had a failure.
    assert "urn:li:actionRequest:good-1" in out
    assert "urn:li:actionRequest:good-2" in out


def test_keyboard_interrupt_mid_batch_prints_urns(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """C1 regression: KeyboardInterrupt must not lose already-filed URNs.

    Two proposals file successfully, the third raises KeyboardInterrupt.
    The first two URNs must appear in the output.
    """
    effects = [
        "urn:li:actionRequest:ki-1",
        "urn:li:actionRequest:ki-2",
        KeyboardInterrupt(),
    ]
    client = FakeClient(propose_side_effects=effects)
    monkeypatch.setattr(
        propose_classifications,
        "load_credentials",
        lambda: Credentials(graphql_url="https://fake/api/graphql", token="fake"),
    )
    monkeypatch.setattr(propose_classifications, "DataHubClient", lambda creds: client)

    result = propose_classifications.main(["--asset", "banking_accounts", "--count", "2", "--seed", "1"])
    assert result == 1
    out = capsys.readouterr().out
    assert "urn:li:actionRequest:ki-1" in out
    assert "urn:li:actionRequest:ki-2" in out
    assert "interrupted" in out.lower()


def test_unexpected_exception_mid_batch_prints_urns_and_reraises(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """C1 regression: a non-DataHubError exception must not lose already-filed URNs.

    The reviewer proved this is reachable: propose_tag raises KeyError when the
    server returns {"data": {}} — but _build_payload now converts that to
    DataHubError. This test uses a bare RuntimeError to prove the general case.
    """
    effects = [
        "urn:li:actionRequest:ue-1",
        RuntimeError("unexpected server shape"),
    ]
    client = FakeClient(propose_side_effects=effects)
    monkeypatch.setattr(
        propose_classifications,
        "load_credentials",
        lambda: Credentials(graphql_url="https://fake/api/graphql", token="fake"),
    )
    monkeypatch.setattr(propose_classifications, "DataHubClient", lambda creds: client)

    with pytest.raises(RuntimeError, match="unexpected server shape"):
        propose_classifications.main(["--asset", "banking_accounts", "--count", "2", "--seed", "1"])

    out = capsys.readouterr().out
    # URN for the first proposal must be printed even though the batch exploded.
    assert "urn:li:actionRequest:ue-1" in out
