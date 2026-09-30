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
_REQUEST_ASSIGNEES = """
query requestsForResource($resourceUrn: String!) {
  listActionRequests(input: {
    start: 0, count: 50, allActionRequests: true, resourceUrn: $resourceUrn
  }) {
    actionRequests { urn assignedUsers assignedGroups assignedRoles }
  }
}
"""


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


def verify_routing(
    client: DataHubClient,
    request_urn: str,
    resource_urn: str,
    predicted: list[str],
    attempts: int = 6,
    delay_seconds: float = 2.0,
) -> tuple[bool, list[str], list[str]]:
    """Compare who the server actually assigned against the prediction.

    This is the point of the exercise. If ENTITY_DOMAIN_OWNERS resolves the
    same way for a scripted request as for a UI form submission, actual should
    equal predicted and no platform-wide roles should appear.

    Assignment resolves asynchronously, so this polls. Reading immediately
    returns an empty assignee list and looks exactly like a routing failure --
    which cost one false alarm before the retry was added.
    """
    for attempt in range(attempts):
        data = client.execute(_REQUEST_ASSIGNEES, {"resourceUrn": resource_urn})
        rows = (data.get("listActionRequests") or {}).get("actionRequests") or []
        req = next((r for r in rows if r.get("urn") == request_urn), None)
        if req:
            actual = list(req.get("assignedUsers") or []) + list(
                req.get("assignedGroups") or []
            )
            roles = list(req.get("assignedRoles") or [])
            if actual:
                return set(actual) == set(predicted) and not roles, actual, roles
        if attempt < attempts - 1:
            time.sleep(delay_seconds)
    return False, [], []


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
    for item in items:
        try:
            domain, stewards = predict_stewards(client, item["dataset"])
        except (RoutingError, DataHubError) as exc:
            blocked.append((item, str(exc)))
            print(f"  BLOCKED  {item['column']:20} -> {item['tag']}")
            print(f"           {exc}")
        else:
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
            except DataHubError as exc:
                failed.append((label, str(exc)))
                print(f"  FAIL  {label}  -- {exc}")
                continue

            matches, actual, roles = verify_routing(
                client, urn, item["dataset"], stewards
            )
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
