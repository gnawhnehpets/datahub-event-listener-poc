"""Tests for propose_via_workflow.py, the workflow-routed proposal CLI.

Uses a fake client stub — NOT mocking requests or touching the network. The
stub is injected via monkeypatch so the tests exercise main() end-to-end
without a DataHub instance.

These pin the batch-verification rework. The original implementation did one
verification poll loop *per request*, each sleeping up to 10s, which made an
11-item batch take minutes while the actual network cost was ~7s. The rework
files everything first, then verifies the whole batch in one pass:

- steward routing is resolved once per dataset, not once per item
- assignees are fetched once per resource for the whole batch, not per request
- the assignee read pages past the 50-row ceiling (requests accumulate, there
  is no dedupe, so a busy resource silently hid our rows before)
- the async-assignment retry still exists, but one sleep now covers the batch
- the per-request predicted/actual/verdict block is unchanged
"""

from __future__ import annotations

import json

import pytest

import propose_via_workflow
from datahub_client import DataHubError

DATASET = "urn:li:dataset:(urn:li:dataPlatform:databricks,testworkspaceforpoc.default.banking_accounts,PROD)"
OTHER_DATASET = "urn:li:dataset:(urn:li:dataPlatform:databricks,workspace.default.accounts_banking,PROD)"
STEWARD = "urn:li:corpuser:steward@example.com"
STEWARD_TYPE = "urn:li:ownershipType:__system__data_steward"


def _item(column: str, dataset: str = DATASET) -> dict:
    return {
        "dataset": dataset,
        "column": column,
        "tag": "urn:li:tag:PII",
        "confidence": "91.204% (High)",
        "classifier": "acme-classifier v1.5",
    }


class FakeClient:
    """Dispatches on query text and counts calls by kind.

    populate_after models asynchronous assignment: the first N assignee reads
    return rows with empty assignedUsers, exactly as the live API does for a
    few seconds after creation.
    """

    def __init__(
        self,
        *,
        domains: dict[str, str | None] | None = None,
        stewards: list[str] | None = None,
        populate_after: int = 0,
        extra_rows: int = 0,
    ) -> None:
        self.domains = domains if domains is not None else {DATASET: "Universal Banking"}
        self.stewards = stewards if stewards is not None else [STEWARD]
        self.populate_after = populate_after
        self.extra_rows = extra_rows
        self.calls: dict[str, int] = {"domain": 0, "create": 0, "assignees": 0}
        self.domain_queries: list[str] = []
        self.created: list[str] = []
        self._assignee_reads = 0

    def _rows_for(self, resource_urn: str) -> list[dict]:
        # Unrelated requests that accumulate on a busy resource. Ours are
        # appended last so they fall past the first page.
        rows = [
            {
                "urn": f"urn:li:actionRequest:noise-{i}",
                "assignedUsers": ["urn:li:corpuser:someone@example.com"],
                "assignedGroups": [],
                "assignedRoles": [],
            }
            for i in range(self.extra_rows)
        ]
        populated = self._assignee_reads > self.populate_after
        for urn in self.created:
            rows.append(
                {
                    "urn": urn,
                    "assignedUsers": list(self.stewards) if populated else [],
                    "assignedGroups": [],
                    "assignedRoles": [],
                }
            )
        return rows

    def execute(self, query: str, variables: dict) -> dict:
        if "domainStewards" in query:
            self.calls["domain"] += 1
            urn = variables["urn"]
            self.domain_queries.append(urn)
            name = self.domains.get(urn)
            if name is None:
                return {"dataset": {"name": urn.split(",")[1], "domain": None}}
            return {
                "dataset": {
                    "name": urn.split(",")[1],
                    "domain": {
                        "domain": {
                            "urn": f"urn:li:domain:{name}",
                            "properties": {"name": name},
                            "ownership": {
                                "owners": [
                                    {"owner": {"urn": s}, "ownershipType": {"urn": STEWARD_TYPE}}
                                    for s in self.stewards
                                ]
                            },
                        }
                    },
                }
            }

        if "createActionWorkflowFormRequest" in query:
            self.calls["create"] += 1
            urn = f"urn:li:actionRequest:req-{self.calls['create']}"
            self.created.append(urn)
            return {"createActionWorkflowFormRequest": urn}

        if "requestsForResource" in query:
            self.calls["assignees"] += 1
            self._assignee_reads += 1
            rows = self._rows_for(variables["resourceUrn"])
            start = variables.get("start", 0)
            count = variables.get("count", 50)
            return {"listActionRequests": {"actionRequests": rows[start : start + count]}}

        raise AssertionError(f"unexpected query: {query[:80]}")


@pytest.fixture
def no_sleep(monkeypatch):
    """Strip real sleeping and record how often the batch waited."""
    slept: list[float] = []
    monkeypatch.setattr(propose_via_workflow.time, "sleep", lambda s: slept.append(s))
    return slept


def _run(tmp_path, monkeypatch, client, items, execute=True):
    path = tmp_path / "input.json"
    path.write_text(json.dumps(items))
    monkeypatch.setattr(propose_via_workflow, "load_credentials", lambda: object())
    monkeypatch.setattr(propose_via_workflow, "DataHubClient", lambda creds: client)
    argv = ["--input", str(path)] + (["--execute"] if execute else [])
    return propose_via_workflow.main(argv)


def test_stewards_resolved_once_per_dataset(tmp_path, monkeypatch, no_sleep, capsys):
    """11 columns on one dataset must cost one domain lookup, not eleven."""
    client = FakeClient()
    items = [_item(f"col_{i}") for i in range(11)]

    _run(tmp_path, monkeypatch, client, items)

    assert client.calls["domain"] == 1
    assert client.domain_queries == [DATASET]
    # Every item still reports its routing in the preflight.
    assert capsys.readouterr().out.count("domain='Universal Banking'") == 11


def test_distinct_datasets_each_resolved_once(tmp_path, monkeypatch, no_sleep):
    """Memoisation must key on dataset, not collapse different ones together."""
    client = FakeClient(domains={DATASET: "Universal Banking", OTHER_DATASET: "Policy"})
    items = [_item("a"), _item("b"), _item("c", OTHER_DATASET), _item("d", OTHER_DATASET)]

    _run(tmp_path, monkeypatch, client, items)

    assert client.calls["domain"] == 2
    assert sorted(client.domain_queries) == sorted([DATASET, OTHER_DATASET])


def test_verification_is_one_pass_for_whole_batch(tmp_path, monkeypatch, no_sleep):
    """Assignee reads must not scale with the number of requests filed."""
    client = FakeClient()
    items = [_item(f"col_{i}") for i in range(11)]

    _run(tmp_path, monkeypatch, client, items)

    assert client.calls["create"] == 11
    # One resource, assignees already populated -> exactly one read.
    assert client.calls["assignees"] == 1
    assert no_sleep == []


def test_verification_pages_past_the_fifty_row_ceiling(tmp_path, monkeypatch, no_sleep, capsys):
    """A busy resource pushes our rows onto page 2; they must still be found."""
    client = FakeClient(extra_rows=60)
    items = [_item("balance")]

    rc = _run(tmp_path, monkeypatch, client, items)

    out = capsys.readouterr().out
    assert "ROUTED AS PREDICTED" in out
    assert "ROUTING DIFFERS" not in out
    assert rc == 0


def test_async_assignment_retries_once_for_the_batch(tmp_path, monkeypatch, no_sleep, capsys):
    """One sleep covers every request, instead of one sleep per request."""
    client = FakeClient(populate_after=1)
    items = [_item(f"col_{i}") for i in range(5)]

    _run(tmp_path, monkeypatch, client, items)

    out = capsys.readouterr().out
    assert out.count("ROUTED AS PREDICTED") == 5
    # Two reads: one empty, one populated. One sleep between them -- not five.
    assert client.calls["assignees"] == 2
    assert no_sleep == [pytest.approx(2.0)]


def test_blocked_items_are_not_filed_and_exit_is_nonzero(tmp_path, monkeypatch, no_sleep, capsys):
    """A dataset with no domain must block rather than file into a void."""
    client = FakeClient(domains={DATASET: "Universal Banking", OTHER_DATASET: None})
    items = [_item("balance"), _item("account_id", OTHER_DATASET)]

    rc = _run(tmp_path, monkeypatch, client, items)

    out = capsys.readouterr().out
    assert client.calls["create"] == 1
    assert "BLOCKED" in out
    assert rc == 1


def test_roles_on_a_request_are_reported_as_a_routing_difference(tmp_path, monkeypatch, no_sleep, capsys):
    """Platform-wide roles are the failure this whole exercise exists to detect."""
    client = FakeClient()
    monkeypatch.setattr(
        FakeClient,
        "_rows_for",
        lambda self, res: [
            {
                "urn": urn,
                "assignedUsers": list(self.stewards),
                "assignedGroups": [],
                "assignedRoles": ["urn:li:dataHubRole:Admin"],
            }
            for urn in self.created
        ],
    )
    _run(tmp_path, monkeypatch, client, [_item("balance")])

    out = capsys.readouterr().out
    assert "ROUTING DIFFERS" in out
    assert "platform-wide" in out


def test_created_urns_are_printed_even_when_a_later_create_fails(tmp_path, monkeypatch, no_sleep, capsys):
    """The URNs are the only handle for cancelling a partial batch."""
    client = FakeClient()
    real_execute = client.execute

    def flaky(query, variables):
        if "createActionWorkflowFormRequest" in query and client.calls["create"] == 1:
            client.calls["create"] += 1
            raise DataHubError("boom")
        return real_execute(query, variables)

    monkeypatch.setattr(client, "execute", flaky)
    rc = _run(tmp_path, monkeypatch, client, [_item("a"), _item("b"), _item("c")])

    out = capsys.readouterr().out
    assert "actionRequest URNs" in out
    assert "urn:li:actionRequest:req-1" in out
    assert rc == 1
