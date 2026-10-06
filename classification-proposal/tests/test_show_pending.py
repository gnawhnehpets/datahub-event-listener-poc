"""Tests for show_pending.py, the read-only assignee check.

This tool is the objective evidence in the demo -- it is what proves a request
did NOT reach everyone else. Three ways it could lie, all silent:

- stopping at the first page, so requests look cancelled when they are pending
- counting another workflow's requests as ours
- counting already-decided requests as pending

Uses a fake client stub, not the network.
"""

from __future__ import annotations

import show_pending
from show_pending import WORKFLOW_URN

OTHER_WF = "urn:li:actionWorkflow:something-else"


def _req(urn: str, status: str = "PENDING", workflow: str = WORKFLOW_URN) -> dict:
    return {
        "urn": urn,
        "status": status,
        "assignedUsers": ["urn:li:corpuser:steward@example.com"],
        "assignedGroups": [],
        "assignedRoles": [],
        "params": {
            "workflowFormRequest": {
                "workflowUrn": workflow,
                "fields": [
                    {"id": "target_column", "values": [{"stringValue": "balance"}]},
                    {"id": "proposed_tag", "values": [{"stringValue": "urn:li:tag:PII"}]},
                ],
            }
        },
    }


class FakeClient:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.pages_served = 0

    def execute(self, query: str, variables: dict) -> dict:
        self.pages_served += 1
        start, count = variables["start"], variables["count"]
        return {"listActionRequests": {"actionRequests": self.rows[start : start + count]}}


def test_pages_past_the_first_fifty():
    """A resource holding more than one page must not truncate."""
    rows = [_req(f"urn:li:actionRequest:r{i}") for i in range(61)]
    client = FakeClient(rows)

    found = show_pending.fetch_pending(client, "urn:li:dataset:x")

    assert len(found) == 61
    assert client.pages_served == 2


def test_exact_page_boundary_does_not_drop_or_loop():
    """Exactly PAGE_SIZE rows: must fetch a second page, then stop."""
    rows = [_req(f"urn:li:actionRequest:r{i}") for i in range(show_pending.PAGE_SIZE)]
    client = FakeClient(rows)

    found = show_pending.fetch_pending(client, "urn:li:dataset:x")

    assert len(found) == show_pending.PAGE_SIZE
    assert client.pages_served == 2  # second page comes back empty and terminates


def test_other_workflows_are_excluded():
    """Another workflow's requests on the same asset are not ours to report."""
    client = FakeClient([_req("urn:li:actionRequest:a"), _req("urn:li:actionRequest:b", workflow=OTHER_WF)])

    found = show_pending.fetch_pending(client, "urn:li:dataset:x")

    assert [r["urn"] for r in found] == ["urn:li:actionRequest:a"]


def test_decided_requests_are_excluded():
    """Only PENDING counts -- cancelled and completed must not inflate the total."""
    client = FakeClient([
        _req("urn:li:actionRequest:a"),
        _req("urn:li:actionRequest:b", status="COMPLETED"),
        _req("urn:li:actionRequest:c", status="CANCELLED"),
    ])

    found = show_pending.fetch_pending(client, "urn:li:dataset:x")

    assert [r["urn"] for r in found] == ["urn:li:actionRequest:a"]


def test_native_proposals_have_no_workflow_and_are_skipped():
    """TAG_ASSOCIATION proposals carry no workflowFormRequest at all."""
    native = {"urn": "urn:li:actionRequest:n", "status": "PENDING",
              "assignedUsers": [], "assignedGroups": [], "assignedRoles": [],
              "params": {"workflowFormRequest": None}}
    client = FakeClient([_req("urn:li:actionRequest:a"), native])

    found = show_pending.fetch_pending(client, "urn:li:dataset:x")

    assert [r["urn"] for r in found] == ["urn:li:actionRequest:a"]
