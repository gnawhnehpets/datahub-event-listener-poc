#!/usr/bin/env python3
"""Raise Column Classification Review requests from an external classifier.

Why this exists alongside propose_classifications.py: native tag proposals
(proposeTags) cannot be routed. Their reviewers are the resource's direct
owners plus every holder of the Admin and Editor platform roles -- 30 people on
the pro-lemur tenant -- and domain owners are excluded entirely. There is no
configuration surface for it anywhere in the GraphQL schema.

Action Workflows can be routed, via dynamicAssignment: ENTITY_DOMAIN_OWNERS
filtered to the data_steward ownership type. They cannot target a column, so
the column travels as a form field value instead of as subResource, and a
listener applies the tag with addTags(subResource=...) once a steward approves.

This script owns the "raise the request" step. It predicts the routing before
writing and verifies the prediction afterwards, because a request that routes
to nobody is invisible rather than failed -- the worst outcome here.

    # predict routing only, write nothing
    ./.venv/bin/python classification-proposal/propose_via_workflow.py --input classifications.json

    # file the requests
    ./.venv/bin/python classification-proposal/propose_via_workflow.py \\
        --input classifications.json --execute

Input is the external classifier's output, a JSON list:

    [{"dataset": "<dataset urn>", "column": "balance",
      "tag": "urn:li:tag:PII", "confidence": "87.342% (High)"}]
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time

from datahub_client import CredentialError, DataHubError, DataHubClient, load_credentials

WORKFLOW_URN = "urn:li:actionWorkflow:column-classification-review"

STEWARD_OWNERSHIP_TYPE = "urn:li:ownershipType:__system__data_steward"

_DOMAIN_STEWARDS = """
query domainStewards($urn: String!) {
  dataset(urn: $urn) {
    name
    domain {
      domain {
        urn
        properties { name }
        ownership {
          owners {
            owner { ... on CorpUser { urn } ... on CorpGroup { urn } }
            ownershipType { urn }
          }
        }
      }
    }
  }
}
"""

# Returns String! -- the created actionRequest URN -- not an object, so no
# subselection. Same shape as proposeTags.
_CREATE_REQUEST = """
mutation createActionWorkflowFormRequest($input: CreateActionWorkflowFormRequestInput!) {
  createActionWorkflowFormRequest(input: $input)
}
"""

# Read back via listActionRequests rather than actionRequest(urn:). A freshly
# created request is not yet resolvable by URN -- that path returns null for
# several seconds -- whereas the resource-scoped list returns it with assignees
# populated. allActionRequests is required or the list is silently scoped to
# the caller and the new request may be missing entirely.
#
# start/count are variables rather than literals because requests accumulate on
# a resource and there is no dedupe. Once a dataset crosses one page our rows
# fall off the end, and a request that routed correctly reads as ROUTING
# DIFFERS -- a false alarm that looks exactly like the bug this tool exists to
# detect.
_REQUEST_ASSIGNEES = """
query requestsForResource($resourceUrn: String!, $start: Int!, $count: Int!) {
  listActionRequests(input: {
    start: $start, count: $count, allActionRequests: true, resourceUrn: $resourceUrn
  }) {
    actionRequests { urn assignedUsers assignedGroups assignedRoles }
  }
}
"""

PAGE_SIZE = 50


class RoutingError(Exception):
    """A classification cannot reach a reviewer, so filing it would hide it."""


def predict_stewards(client: DataHubClient, dataset_urn: str) -> tuple[str, list[str]]:
    """Return (domain name, steward urns) or raise RoutingError.

    Routing resolves from domain ownership at request time. Two things can make
    it resolve to nobody: the dataset has no domain, or the domain has no owner
    carrying the data steward ownership type. Both are silent in the UI, so
    they are checked here and treated as fatal for that item.
    """
    data = client.execute(_DOMAIN_STEWARDS, {"urn": dataset_urn})
    dataset = data.get("dataset")
    if not dataset:
        raise RoutingError(f"dataset not found: {dataset_urn}")

    domain = (dataset.get("domain") or {}).get("domain")
    if not domain:
        raise RoutingError(
            f"{dataset['name']} is not assigned to a domain, so no domain steward "
            f"can be resolved. Assign a domain before classifying it."
        )

    name = domain["properties"]["name"]
    stewards = [
        o["owner"]["urn"]
        for o in ((domain.get("ownership") or {}).get("owners") or [])
        if (o.get("ownershipType") or {}).get("urn") == STEWARD_OWNERSHIP_TYPE
    ]
    if not stewards:
        raise RoutingError(
            f"domain {name!r} has no owner with the data steward ownership type, "
            f"so the request would route to nobody."
        )
    return name, stewards


def build_request(item: dict) -> dict:
    """Map one classifier finding onto CreateActionWorkflowFormRequestInput.

    The column is a form field, not subResource: workflow form requests have no
    sub-resource field, which is the whole reason this indirection exists.
    """
    return {
        "workflowUrn": WORKFLOW_URN,
        "entityUrn": item["dataset"],
        "description": (
            f"External classifier proposes {item['tag']} for column "
            f"{item['column']} (confidence {item['confidence']})."
        ),
        "fields": [
            {"id": "target_column", "values": [{"stringValue": item["column"]}]},
            {"id": "proposed_tag", "values": [{"stringValue": item["tag"]}]},
            {"id": "confidence", "values": [{"stringValue": item["confidence"]}]},
            {"id": "classifier", "values": [{"stringValue": item.get("classifier", "external-classifier")}]},
        ],
    }


def fetch_assignees(
    client: DataHubClient, resource_urn: str
) -> dict[str, tuple[list[str], list[str]]]:
    """Map every request on a resource to its (assignees, roles).

    Pages to exhaustion. One read covers every request filed against that
    resource, which is what makes batch verification a single call rather than
    one per request.
    """
    found: dict[str, tuple[list[str], list[str]]] = {}
    start = 0
    while True:
        data = client.execute(
            _REQUEST_ASSIGNEES,
            {"resourceUrn": resource_urn, "start": start, "count": PAGE_SIZE},
        )
        rows = (data.get("listActionRequests") or {}).get("actionRequests") or []
        for row in rows:
            urn = row.get("urn")
            if not urn:
                continue
            found[urn] = (
                list(row.get("assignedUsers") or [])
                + list(row.get("assignedGroups") or []),
                list(row.get("assignedRoles") or []),
            )
        if len(rows) < PAGE_SIZE:
            return found
        start += PAGE_SIZE


def verify_batch(
    client: DataHubClient,
    created: list[tuple[str, str, str, list[str]]],
    attempts: int = 6,
    delay_seconds: float = 2.0,
) -> dict[str, tuple[list[str], list[str]]]:
    """Resolve actual assignees for a whole batch in one poll loop.

    This is the point of the exercise. If ENTITY_DOMAIN_OWNERS resolves the
    same way for a scripted request as for a UI form submission, actual should
    equal predicted and no platform-wide roles should appear.

    Assignment resolves asynchronously, so this still polls -- reading
    immediately returns empty assignee lists and looks exactly like a routing
    failure, which cost one false alarm before the retry existed. The change is
    that one sleep now covers the entire batch instead of one sleep per
    request. Filing 11 proposals used to spend minutes asleep for ~7s of actual
    network.

    Each `created` entry is (label, request_urn, resource_urn, predicted).
    Requests that never gain an assignee come back as ([], []) so the caller
    reports ROUTING DIFFERS rather than silently dropping them.
    """
    resources = sorted({resource for _, _, resource, _ in created})
    pending = {request for _, request, _, _ in created}
    resolved: dict[str, tuple[list[str], list[str]]] = {}

    for attempt in range(attempts):
        for resource in resources:
            for urn, value in fetch_assignees(client, resource).items():
                # An empty assignee list means assignment has not landed yet,
                # so leave it pending rather than recording a false negative.
                if urn in pending and value[0]:
                    resolved[urn] = value
        pending -= resolved.keys()
        if not pending:
            break
        if attempt < attempts - 1:
            time.sleep(delay_seconds)

    for urn in pending:
        resolved[urn] = ([], [])
    return resolved


def short(urn: str) -> str:
    return urn.replace("urn:li:corpuser:", "").replace("urn:li:corpGroup:", "group:")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, help="classifier output, JSON list")
    parser.add_argument(
        "--execute",
        action="store_true",
        help="actually file the requests. Without this, routing is predicted and nothing is written.",
    )
    args = parser.parse_args(argv)

    try:
        items = json.loads(pathlib.Path(args.input).read_text())
    except (OSError, json.JSONDecodeError) as exc:
        print(f"could not read {args.input}: {exc}", file=sys.stderr)
        return 1
    if not isinstance(items, list) or not items:
        print(f"{args.input} must be a non-empty JSON list", file=sys.stderr)
        return 1

    try:
        client = DataHubClient(load_credentials())
    except CredentialError as exc:
        print(f"credentials: {exc}", file=sys.stderr)
        return 1

    # Predict first, for every item, before writing anything. A run that would
    # file some requests into a void should be fixed, not half-executed.
    print(f"\nRouting preflight for {len(items)} classification(s):\n")
    routable: list[tuple[dict, str, list[str]]] = []
    blocked: list[tuple[dict, str]] = []
    # Routing is a property of the dataset, not the column, so a batch of 11
    # columns on one table asked the same question 11 times. Cache the verdict
    # -- failures included, or a blocked dataset is re-queried per column.
    seen: dict[str, tuple[str, list[str]] | Exception] = {}
    for item in items:
        dataset = item["dataset"]
        if dataset not in seen:
            try:
                seen[dataset] = predict_stewards(client, dataset)
            except (RoutingError, DataHubError) as exc:
                seen[dataset] = exc
        cached = seen[dataset]
        if isinstance(cached, Exception):
            blocked.append((item, str(cached)))
            print(f"  BLOCKED  {item['column']:20} -> {item['tag']}")
            print(f"           {cached}")
        else:
            domain, stewards = cached
            routable.append((item, domain, stewards))
            print(f"  OK       {item['column']:20} -> {item['tag']}")
            print(f"           domain={domain!r} steward(s)={[short(s) for s in stewards]}")

    print(f"\n{len(routable)} routable, {len(blocked)} blocked")

    if not args.execute:
        print("\nNothing written. Re-run with --execute to file the routable requests.")
        return 1 if blocked else 0

    if not routable:
        print("\nNothing routable to file.", file=sys.stderr)
        return 1

    print(f"\nFiling {len(routable)} request(s):\n")
    created: list[tuple[str, str]] = []
    # Parallel to `created`, carrying what verification needs: the resource to
    # look the request up under, and who we predicted would be assigned.
    pending: list[tuple[str, str, str, list[str]]] = []
    failed: list[tuple[str, str]] = []
    try:
        for item, domain, stewards in routable:
            label = f"{item['column']} -> {item['tag']}"
            try:
                urn = client.execute(_CREATE_REQUEST, {"input": build_request(item)})[
                    "createActionWorkflowFormRequest"
                ]
                if not urn:
                    raise DataHubError("mutation returned no actionRequest URN")
                created.append((label, urn))
                pending.append((label, urn, item["dataset"], stewards))
            except DataHubError as exc:
                failed.append((label, str(exc)))
                print(f"  FAIL  {label}  -- {exc}")
                continue

        # Verify once, after everything is filed. Assignment is asynchronous
        # either way, so waiting per request bought nothing -- the whole batch
        # settles during a single wait.
        if pending:
            resolved = verify_batch(client, pending)
            for label, urn, _resource, stewards in pending:
                actual, roles = resolved.get(urn, ([], []))
                matches = set(actual) == set(stewards) and not roles
                verdict = "ROUTED AS PREDICTED" if matches else "ROUTING DIFFERS"
                print(f"  OK    {label}")
                print(f"        {urn}")
                print(f"        predicted: {[short(s) for s in stewards]}")
                print(f"        actual   : {[short(a) for a in actual]}")
                if roles:
                    print(f"        roles    : {roles}  <-- platform-wide, not steward-scoped")
                print(f"        {verdict}")
    finally:
        # The URNs are the only handle for withdrawing a batch, so print them
        # even if the loop dies partway.
        if created:
            print("\nactionRequest URNs (use these to cancel the batch):")
            for _, urn in created:
                print(f"  {urn}")

    print(f"\n{len(created)} created, {len(failed)} failed, {len(blocked)} blocked")
    return 1 if (failed or blocked) else 0


if __name__ == "__main__":
    sys.exit(main())
