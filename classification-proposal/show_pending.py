#!/usr/bin/env python3
"""Show who each pending Column Classification Review request is assigned to.

This is the objective check behind the whole exercise. The DataHub UI shows a
reviewer their own task list, which cannot demonstrate the thing that matters
-- that a request did NOT go to everyone else. Reading assignees off each
request shows both halves at once: the stewards who got it, and the absence of
platform-wide roles.

Read-only. Routing is resolved by DataHub when the request is created, so the
listener does not need to be running for this to mean anything.

    ./.venv/bin/python classification-proposal/show_pending.py
    ./.venv/bin/python classification-proposal/show_pending.py --dataset <urn>

Interpreting the output:

  assignedRoles must be empty. A native tag proposal on the same asset carries
  the Admin and Editor platform roles, which is every holder of those roles on
  the instance. A workflow request routed by ENTITY_DOMAIN_OWNERS should carry
  the domain's data stewards and nothing else -- not the dataset's owners, not
  the domain's business owners.
"""

from __future__ import annotations

import argparse
import sys

from datahub_client import CredentialError, DataHubClient, DataHubError, load_credentials

WORKFLOW_URN = "urn:li:actionWorkflow:column-classification-review"

DEFAULT_DATASET = (
    "urn:li:dataset:(urn:li:dataPlatform:databricks,"
    "testworkspaceforpoc.default.banking_accounts,PROD)"
)

# allActionRequests is required or the list is silently scoped to the caller,
# and a request assigned to someone else goes missing entirely -- which would
# make this tool report the opposite of the truth.
_PENDING = """
query pendingForResource($resourceUrn: String!, $start: Int!, $count: Int!) {
  listActionRequests(input: {
    start: $start, count: $count, allActionRequests: true, resourceUrn: $resourceUrn
  }) {
    actionRequests {
      urn
      status
      assignedUsers
      assignedGroups
      assignedRoles
      params {
        workflowFormRequest {
          workflowUrn
          fields { id values { ... on StringValue { stringValue } } }
        }
      }
    }
  }
}
"""

PAGE_SIZE = 50


def short(urn: str) -> str:
    return urn.replace("urn:li:corpuser:", "").replace("urn:li:corpGroup:", "group:")


def fetch_pending(client: DataHubClient, dataset_urn: str) -> list[dict]:
    """Every PENDING request for our workflow on this dataset, paged to the end."""
    rows: list[dict] = []
    start = 0
    while True:
        data = client.execute(
            _PENDING,
            {"resourceUrn": dataset_urn, "start": start, "count": PAGE_SIZE},
        )
        page = (data.get("listActionRequests") or {}).get("actionRequests") or []
        rows.extend(page)
        if len(page) < PAGE_SIZE:
            break
        start += PAGE_SIZE

    return [
        r
        for r in rows
        if r.get("status") == "PENDING"
        and ((r.get("params") or {}).get("workflowFormRequest") or {}).get("workflowUrn")
        == WORKFLOW_URN
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", default=DEFAULT_DATASET, help="dataset urn")
    args = parser.parse_args(argv)

    try:
        client = DataHubClient(load_credentials())
    except CredentialError as exc:
        print(f"credentials: {exc}", file=sys.stderr)
        return 1

    try:
        pending = fetch_pending(client, args.dataset)
    except DataHubError as exc:
        print(f"query failed: {exc}", file=sys.stderr)
        return 1

    print(f"\npending Column Classification Review requests: {len(pending)}\n")
    if not pending:
        print("  (none -- nothing is awaiting review on this dataset)\n")
        return 0

    print(f"  {'column':18} {'tag':32} reviewers")
    print("  " + "-" * 88)
    roles_total = 0
    for req in pending:
        fields = {
            f["id"]: (f["values"][0].get("stringValue") if f.get("values") else None)
            for f in (req["params"]["workflowFormRequest"].get("fields") or [])
        }
        who = [short(u) for u in (req.get("assignedUsers") or [])]
        who += [short(g) for g in (req.get("assignedGroups") or [])]
        roles = req.get("assignedRoles") or []
        roles_total += len(roles)
        print(f"  {str(fields.get('target_column')):18} {str(fields.get('proposed_tag')):32} {who}")
        if roles:
            print(f"  {'':18} {'':32} ROLES: {roles}  <-- platform-wide, not steward-scoped")

    print(f"\n  platform-wide roles across all requests: {roles_total}  (0 is the point)\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
