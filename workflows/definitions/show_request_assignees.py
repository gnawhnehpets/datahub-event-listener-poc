#!/usr/bin/env python
"""Show who each Dataset Access Request was assigned to, newest first.

This is the objective check behind the domain-scoped approver change. The
DataHub UI shows a reviewer their own task list, which cannot demonstrate the
thing that matters -- that a request did NOT go to everyone else. Reading the
assignees off each request shows both halves at once.

    set -a && . ./.env && set +a
    python workflows/definitions/show_request_assignees.py

Read-only. Approval routing is resolved by DataHub when the request is created,
so the listener does not need to be running for this to be meaningful.

Interpreting the output:

  Before the change, every request looked like
      assigned -> ['urn:li:corpuser:data.steward', <the dataset's own owners>]
  because the step combined a hardcoded user with ENTITY_OWNERS, and an empty
  ownershipTypes matches EVERY ownership type.

  After it, a request should carry exactly the Data Steward(s) of the DATASET'S
  DOMAIN -- one name, not the union of every steward in the org.
"""

from __future__ import annotations

import datetime
import json
import os
import sys
import urllib.request

# Both the production workflow and the "-2" test copy route the same way.
DATASET_ACCESS_WORKFLOWS = {
    "urn:li:actionWorkflow:7110b19c-45b9-4450-b8bb-ba47d9f522ba",
    "urn:li:actionWorkflow:zz-dataset-access-request-2",
}
PAGE = 100


def _gql(url: str, token: str, query: str) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps({"query": query}).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    payload = json.load(urllib.request.urlopen(request, timeout=40))
    if "errors" in payload:
        raise SystemExit(json.dumps(payload["errors"], indent=2))
    return payload


def main() -> int:
    server = os.environ.get("DATAHUB_GMS_URL", "").strip()
    token = os.environ.get("DATAHUB_GMS_TOKEN", "").strip()
    if not server or not token:
        raise SystemExit("Set DATAHUB_GMS_URL and DATAHUB_GMS_TOKEN (set -a && . ./.env && set +a)")
    url = server.rstrip("/") + "/api/v2/graphql"

    # Two pages is enough for a demo tenant. A short read here only truncates the
    # history shown; it cannot mislead about a request that IS listed.
    requests_seen: list[dict] = []
    for start in (0, PAGE):
        query = (
            "{ listActionRequests(input:{start:%d,count:%d}){ actionRequests{ urn status "
            "created{time} entity{urn} assignedUsers assignedGroups "
            "params{ workflowFormRequest{ workflowUrn } } } } }" % (start, PAGE)
        )
        requests_seen += _gql(url, token, query)["data"]["listActionRequests"]["actionRequests"]

    access = [
        r
        for r in requests_seen
        if (((r.get("params") or {}).get("workflowFormRequest") or {}).get("workflowUrn"))
        in DATASET_ACCESS_WORKFLOWS
    ]
    access.sort(key=lambda r: (r.get("created") or {}).get("time") or 0, reverse=True)

    print(f"{len(access)} Dataset Access Request(s), newest first\n")
    for r in access[:15]:
        stamp = (r.get("created") or {}).get("time")
        when = (
            datetime.datetime.fromtimestamp(stamp / 1000, datetime.UTC).strftime("%Y-%m-%d %H:%M")
            if stamp
            else "?"
        )
        entity = (r.get("entity") or {}).get("urn") or "(no entity)"
        # Dataset urns are long; the middle segment is the readable table path.
        short = entity.split(",")[1] if "," in entity else entity
        print(f"  {when}  {r['status']:9} {short}")
        print(f"       users  : {r.get('assignedUsers') or []}")
        if r.get("assignedGroups"):
            print(f"       groups : {r['assignedGroups']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
