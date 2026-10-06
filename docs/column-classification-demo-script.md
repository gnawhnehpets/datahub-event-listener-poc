# Column classification — demo script

A six-step walkthrough you can read off the screen. For the full background,
the reviewer-by-reviewer comparison, and the troubleshooting notes, see
[`column-classification-demo.md`](column-classification-demo.md).

---

## What it proves

An external classifier proposes a tag for a **single column**, and the review
task reaches **only the data steward(s) of that dataset's domain** — not the
dataset's owners, not the domain's business owners, and not every Admin and
Editor on the instance.

The demo makes the routing visible by **breaking it first**. With no data
steward on the domain, the tool refuses to file anything. Add the steward back,
re-run, and the same batch routes cleanly. That contrast is the whole point:
routing is a property you can configure, not a fixed behaviour.

---

## Quickstart

### 1. Install

```bash
cd /path/to/datahub-event-listener-poc
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
```

`requirements.txt` covers everything needed to file and route proposals.
`PyYAML` arrives transitively via `acryl-datahub-actions`. For the test suite
only, add `pytest` separately:

```bash
./.venv/bin/pip install pytest && ./.venv/bin/python -m pytest
```

> The listener in `src/actions` is **not** needed for this demo. Routing is
> resolved by DataHub when the request is created, so everything below works
> with nothing running.

### 2. Credentials

Create a **Personal** Access Token in the target instance under
**Settings → Access Tokens**, then write `~/.datahubenv_prolemur`:

```yaml
gms:
  server: https://<your-subdomain>.trials.acryl.io/gms
  token: <Personal Access Token>
```

The tool decodes the token's `exp` locally and aborts before writing anything
if it has lapsed, naming the date. Confirm the instance is reachable:

```bash
curl -s -o /dev/null -w "%{http_code}\n" https://<your-subdomain>.trials.acryl.io/
# expect 200
```

### 3. Deploy the workflow definition

The workflow is version-controlled at
`workflows/definitions/action-workflow-column-classification-review_v2.json`.
It uses a chosen URN slug rather than a UI-generated GUID, so it is portable
across deployments without name resolution at startup.

```bash
export DATAHUB_TOKEN=<Personal Access Token>

# preview the call without sending it
./.venv/bin/python workflows/definitions/upsert_workflows.py \
  --server https://<your-subdomain>.trials.acryl.io \
  --file workflows/definitions/action-workflow-column-classification-review_v2.json \
  --print-curl

# deploy
./.venv/bin/python workflows/definitions/upsert_workflows.py \
  --server https://<your-subdomain>.trials.acryl.io \
  --file workflows/definitions/action-workflow-column-classification-review_v2.json
```

Upsert is idempotent — re-running updates the existing workflow in place.

### 4. Pick a target and check preconditions

The walkthrough below uses `banking_accounts`, whose domain is
**Universal Banking**. Any dataset works provided its domain has at least one
owner carrying the **Data Steward** ownership type.

Dry-run the preflight. It writes nothing and tells you exactly how each item
would route:

```bash
./.venv/bin/python classification-proposal/propose_via_workflow.py \
  --input classification-proposal/banking-accounts-classifications.json
```

---

## The demo

### Step 1 — Clear the data steward

In the UI, open the **Universal Banking** domain → **Ownership**.

Note the two owners and their types, then **remove the Data Steward**, leaving
the Business Owner in place.

Say out loud what you expect: the dataset still has a domain, that domain still
has an owner, and the classifier still has valid findings. The *only* thing
missing is an owner of the right ownership type.

### Step 2 — Run it. Everything blocks.

```bash
./.venv/bin/python classification-proposal/propose_via_workflow.py \
  --input classification-proposal/banking-accounts-classifications.json --execute
```

```
  BLOCKED  balance              -> urn:li:tag:Confidential
           domain 'Universal Banking' has no owner with the data steward
           ownership type, so the request would route to nobody.
  ...
  0 routable, 12 blocked
```

Two things to land here:

1. The tool resolves the reviewer **before** writing, so routing is predictable
   rather than discovered afterwards.
2. This is the safety property. A request that routes to nobody is *invisible*,
   not failed — it would sit in an empty queue forever. The tool refuses to
   create it. Note that `--execute` was passed and still nothing was written.

### Step 3 — Confirm nothing was filed

```bash
./.venv/bin/python classification-proposal/show_pending.py
```

```
pending Column Classification Review requests: 0

  (none -- nothing is awaiting review on this dataset)
```

This reads from the API rather than a task list, which matters: the UI only
ever shows *you* your own tasks, so it cannot prove a request didn't go to
someone else. Reading assignees off each request shows both halves at once.

### Step 4 — Restore the data steward

Back in **Universal Banking → Ownership**, add the steward again: add the owner
and set their ownership type to **Data Steward**.

Nothing else changes. Same dataset, same domain, same input file.

### Step 5 — Run the identical command again

```bash
./.venv/bin/python classification-proposal/propose_via_workflow.py \
  --input classification-proposal/banking-accounts-classifications.json --execute
```

```
  OK       balance              -> urn:li:tag:Confidential
           domain='Universal Banking' steward(s)=['...']
  ...
  11 routable, 1 blocked

  OK    balance -> urn:li:tag:Confidential
        urn:li:actionRequest:...
        predicted: [...]
        actual   : [...]
        ROUTED AS PREDICTED
```

Three things worth calling out:

- **`predicted` vs `actual`** is the verification. It proves
  `ENTITY_DOMAIN_OWNERS` resolves the same way for a scripted request as for a
  UI form submission.
- **One entry still blocks on purpose.** The last item in the input file targets
  a domain-less dataset, so the safety property stays visible even in the happy
  path.
- **Every created URN is printed.** They are the only handle for withdrawing the
  batch — see Reset below.

### Step 6 — Confirm the reviewers

```bash
./.venv/bin/python classification-proposal/show_pending.py
```

```
pending Column Classification Review requests: 11

  column             tag                              reviewers
  ----------------------------------------------------------------------------
  balance            urn:li:tag:Confidential          ['<the domain steward>']
  ...

  platform-wide roles across all requests: 0  (0 is the point)
```

This is the close. Every request carries the domain's **data steward and
nobody else**:

- not the **dataset's** owners,
- not the domain's **business owner**, who is still an owner of that very
  domain and gets nothing,
- and **no platform roles** — `assignedRoles` is empty, so no Admin or Editor
  is pulled in.

For the contrast, open the same asset's **Proposals** tab and look at a native
tag proposal: its reviewers are the resource's owners *plus every holder of the
Admin and Editor platform roles*. Same asset, same kind of decision, entirely
different audience — and nothing in the native path is configurable.

---

## Reset between runs

Requests **accumulate** — there is no dedupe, so each rehearsal adds more.
Cancel a batch with the URNs printed in step 5:

```bash
./.venv/bin/python - <<'PY'
import json, pathlib, urllib.request, yaml
cfg = yaml.safe_load(pathlib.Path("~/.datahubenv_prolemur").expanduser().read_text())
url = cfg["gms"]["server"].rstrip("/").removesuffix("/gms") + "/api/graphql"
H = {"Authorization": f"Bearer {cfg['gms']['token']}", "Content-Type": "application/json"}
URNS = [
    "urn:li:actionRequest:...",   # paste from step 5
]
M = ('mutation c($input:CancelActionWorkflowFormRequestInput!)'
     '{cancelActionWorkflowFormRequest(input:$input)}')
for u in URNS:
    body = {"query": M, "variables": {"input": {"urn": u, "note": "Withdrawing demo batch."}}}
    r = urllib.request.Request(url, json.dumps(body).encode(), H)
    res = json.load(urllib.request.urlopen(r))
    print("FAIL" if res.get("errors") else "OK  ", u)
PY
```

Cancelling is one call per request — there is no bulk variant, same as creation.

Only cancel requests for `column-classification-review`. Native tag proposals
on the same asset are the "before" half of the comparison, and they are not
cancellable through this mutation anyway.

---

## Limits — say these out loud

**The tag is not applied on approval.** A steward approving records the
decision and changes no metadata. Closing that gap means catching the
`actionRequestStatus` event and calling
`addTags(tagUrns, resourceUrn, subResourceType: DATASET_FIELD, subResource)`.

**Domain ownership is the gating prerequisite.** Routing reads ownership
recorded on the *domain*, not the dataset. A dataset with no domain, or a
domain with no Data Steward, blocks — which is what steps 1–3 demonstrate.

**The proposer is not the approver.** You will not see these tasks in your own
Task Center unless you are yourself a steward of that domain. That is correct
behaviour; `show_pending.py` exists precisely because the UI cannot show it.
