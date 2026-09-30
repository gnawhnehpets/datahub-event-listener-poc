# Column classification routed to domain data stewards

DataHub Cloud — Action Workflows + external classifier
Demo runbook · 30 September 2026

---

## 1. What this demonstrates

An external classifier proposes a tag for a **single column**, and the review
task reaches **only the data steward(s) of that dataset's domain** — not the
dataset's owners, not every Admin and Editor on the platform.

The contrast is the demo. Both mechanisms are live on the same asset, so you can
show them side by side on one screen.

| | Native tag proposal (`proposeTags`) | This workflow |
|---|---|---|
| Reviewers | 6 users **+ `Admin` and `Editor` roles** (21 + 9 = 30 more people) | **2 users**, no roles |
| Who, exactly | dataset owners + `admin` twice | the domain's data stewards |
| Configurable | no — nothing in the schema controls it | yes — `dynamicAssignment` |
| Column-level | yes, natively | yes, via a form field |

Same dataset. 36 reviewers versus 2.

## 2. Why the two mechanisms pick different people

This is the point worth landing, because "it routes to stewards" sounds like a
setting until you see that the two lists barely overlap.

**`workspace.default.accounts_banking` owners** — these become native-proposal
assignees:

| User | Ownership type |
|---|---|
| `alice.nguyen@example.com` | Data Owner |
| `ben.okafor@example.com` | Technical Owner |
| `carla.mendes@example.com` | Data Steward |
| `dana.whitfield@example.com` | Data Steward |

**Domain `Policy` owners** — the workflow filters these to Data Steward only:

| Owner | Ownership type | Routed to? |
|---|---|---|
| `erin.castillo@example.com` | Business Owner | no |
| `urn:li:corpGroup:2e5c8586…` | Business Owner | no |
| `urn:li:corpGroup:a0db2eae…` | Business Owner | no |
| `urn:li:corpGroup:sample_data_ORG_DATA_PLATFORM` | Business Owner | no |
| `felix_moreau@example.com` | **Data Steward** | **yes** |
| `dana.whitfield@example.com` | **Data Steward** | **yes** |

Note what this proves:

- `felix_moreau` is a domain steward and gets the workflow task, but appears in
  **no** native tag proposal on this dataset.
- `alice.nguyen` and `ben.okafor` own the dataset and get every native
  proposal, but get **none** of the workflow tasks.
- `carla.mendes` is a Data Steward *of the dataset* but not of the domain —
  native yes, workflow no.
- `dana.whitfield` is the only overlap.
- The `ownershipTypeUrns` filter excluded 4 of the 6 domain owners, including
  all three groups. It is not "domain owners", it is "domain owners of this
  ownership type".

So the mechanisms select genuinely different audiences. This is not a
notification tweak.

## 3. Already set up — nothing to deploy

**Workflow, deployed and live:**

```
urn:li:actionWorkflow:column-classification-review
```

Definition version-controlled at
`workflows/definitions/action-workflow-column-classification-review_v2.json`.
It uses a chosen URN slug rather than a UI-generated GUID, so it is portable
across deployments without name resolution at startup.

**Four PENDING requests waiting in the stewards' queues,** all on
`workspace.default.accounts_banking`:

| Column | Proposed tag | Confidence | actionRequest |
|---|---|---|---|
| `balance` | `Confidential` | 91.204% (High) | `8c77323a-ef6b-4eef-851f-585d0a83bf62` |
| `customer_id` | `PII` | 87.342% (High) | `652dfc01-f5df-4938-8cb2-03826a7632d9` |
| `risk_flag` | `Restricted` | 72.918% (Medium) | `93904226-4924-43f3-a4aa-05b715d50dc7` |
| `account_status` | `Public` | 54.809% (Low) | `034e4a7e-a6ff-480a-9164-1f9cc021eea5` |

Each assigned to exactly `felix_moreau@example.com` and
`dana.whitfield@example.com`, with `assignedRoles: []`.

The confidence spread is deliberate — High, High, Medium, Low — so you can make
the point that a reviewer treats a 54.8% suggestion differently from a 91.2%
one. That is the whole reason for surfacing confidence.

**Six PENDING native tag proposals on the same dataset,** each carrying 6 users
plus the Admin and Editor roles. Those are your "before".

## 4. Prerequisites

```bash
cd /Users/stephen/Desktop/github/datahub-event-listener-poc
git checkout dev/classification-proposals
```

A valid **Personal** Access Token in `~/.datahubenv_prolemur`:

```yaml
gms:
  server: https://pro-lemur-fda34e845.trials.acryl.io/gms
  token: <Personal Access Token>
```

Current token expires **2026-10-24**. The script checks `exp` locally and
aborts before writing if it has lapsed, naming the date. Regenerate under
Settings → Access Tokens.

Verify the tenant is up before you present:

```bash
curl -s -o /dev/null -w "%{http_code}\n" https://pro-lemur-fda34e845.trials.acryl.io/
```

Expect `200`. See §7 if not.

## 5. Demo walkthrough

### Step 0 — The "before": show native routing

Open `workspace.default.accounts_banking` → **Proposals** tab. Point at a
pending tag proposal and show its reviewers: six users, plus the `Admin` and
`Editor` roles. Note that every Admin and Editor on the platform is an
assignee, on every proposal, regardless of domain. This is the symptom the
customer originally reported.

### Step 1 — Show the routing target

Open the **Policy** domain → Ownership. Point out `felix_moreau` and
`dana.whitfield` carrying **Data Steward**, alongside four Business Owners who
should *not* be involved in classification decisions.

### Step 2 — Predict the routing, writing nothing

```bash
./.venv/bin/python classification-proposal/propose_via_workflow.py \
  --input classification-proposal/sample-classifications.json
```

```
  OK       balance              -> urn:li:tag:Confidential
           domain='Policy' steward(s)=['felix_moreau@example.com', 'dana.whitfield@example.com']
  ...
  BLOCKED  account_id           -> urn:li:tag:PII
           banking_accounts is not assigned to a domain, so no domain steward
           can be resolved. Assign a domain before classifying it.

3 routable, 1 blocked
Nothing written.
```

Two things to say here:

1. The tool resolves the reviewer **before** writing, so routing is predictable
   rather than discovered afterwards.
2. The `BLOCKED` line is the safety property. A dataset with no domain produces
   a request that is *invisible* rather than failed — it would sit in nobody's
   queue forever. The tool refuses to file it. On this tenant 470 of 499
   datasets have no domain, so this is the common case, not the edge case.

### Step 3 — Show the queued tasks

As yourself you will **not** see these tasks, which is the correct behaviour and
worth calling out — the proposer is not the approver. Either:

- have `felix_moreau` or `dana.whitfield` open their Task Center live, or
- read the assignees back from the API:

```bash
./.venv/bin/python - <<'PY'
import json, pathlib, urllib.request, yaml
cfg = yaml.safe_load(pathlib.Path("~/.datahubenv_prolemur").expanduser().read_text())
url = cfg["gms"]["server"].rstrip("/").removesuffix("/gms") + "/api/graphql"
ds = "urn:li:dataset:(urn:li:dataPlatform:databricks,workspace.default.accounts_banking,PROD)"
q = '''{ listActionRequests(input:{start:0,count:40,allActionRequests:true,resourceUrn:"%s"})
 { actionRequests { type status subResource assignedUsers assignedRoles
   params { workflowFormRequest { workflowUrn fields { id values { ... on StringValue { stringValue } } } }
            tagProposal { tags { properties { name } } } } } } }''' % ds
req = urllib.request.Request(url, json.dumps({"query": q}).encode(),
    {"Authorization": f"Bearer {cfg['gms']['token']}", "Content-Type": "application/json"})
rows = json.load(urllib.request.urlopen(req))["data"]["listActionRequests"]["actionRequests"]
OURS = "column-classification-review"
print(f'{"routing":20} {"kind":16} {"column":15} {"tag":14} {"reviewers":>9}  roles')
print("-" * 92)
for a in sorted(rows, key=lambda r: 0 if (((r["params"] or {}).get("workflowFormRequest") or {})
                                          .get("workflowUrn", "").endswith(OURS)) else 1):
    p = a["params"] or {}
    wf = p.get("workflowFormRequest") or {}
    f = {x["id"]: (x["values"][0].get("stringValue") if x["values"] else None)
         for x in (wf.get("fields") or [])}
    is_ours = wf.get("workflowUrn", "").endswith(OURS)
    col = f.get("target_column") or a["subResource"] or "-"
    tag = (f.get("proposed_tag") or "").split(":")[-1] \
          or ", ".join(t["properties"]["name"]
                       for t in ((p.get("tagProposal") or {}).get("tags") or [])) or "-"
    print(f'{("DOMAIN STEWARD" if is_ours else "owners + roles"):20} '
          f'{("workflow" if is_ours else a["type"][:16]):16} {col:15} {tag:14} '
          f'{len(a["assignedUsers"] or []):>9}  '
          f'{[r.split(":")[-1] for r in (a["assignedRoles"] or [])]}')
PY
```

Output, abridged:

```
routing              kind             column          tag            reviewers  roles
--------------------------------------------------------------------------------------------
DOMAIN STEWARD       workflow         balance         Confidential           2  []
DOMAIN STEWARD       workflow         customer_id     PII                    2  []
DOMAIN STEWARD       workflow         risk_flag       Restricted             2  []
DOMAIN STEWARD       workflow         account_status  Public                 2  []
owners + roles       TAG_ASSOCIATION  customer_id     PII                    6  ['Admin', 'Editor']
owners + roles       TAG_ASSOCIATION  balance         PII                    6  ['Admin', 'Editor']
owners + roles       TAG_ASSOCIATION  account_type    Confidential           6  ['Admin', 'Editor']
```

**The strongest two rows are `customer_id` → `PII`, which exists in both
mechanisms.** Identical column, identical tag, filed on the same dataset — and
one routes to 2 domain stewards while the other routes to 6 users plus every
Admin and Editor. Put those two rows side by side and the argument needs no
further explanation.

`reviewers` counts `assignedUsers` only. Add 30 to every `owners + roles` row
for the Admin and Editor memberships.

### Step 4 — Approve one as a steward

Have a steward approve the `balance` → `Confidential` request. The decision is
recorded and the request moves to `COMPLETED`.

**Be straight about what happens next:** the tag is **not** yet applied to the
column. The listener that does that is not built — see §8. Do not claim the
tag lands; claim the *routing* works, which is what this demo is about.

## 6. Filing a fresh batch

To generate new requests during a demo, edit or replace the input JSON:

```json
[{"dataset": "<dataset urn>", "column": "balance",
  "tag": "urn:li:tag:PII", "confidence": "87.342% (High)",
  "classifier": "acme-classifier v1.4"}]
```

```bash
./.venv/bin/python classification-proposal/propose_via_workflow.py \
  --input my-classifications.json --execute
```

Each run prints `predicted` versus `actual` assignees and a
`ROUTED AS PREDICTED` / `ROUTING DIFFERS` verdict per request, plus every
created URN.

**Requests accumulate.** There is no dedupe; re-running files more. Cancel a
batch with `cancelActionWorkflowFormRequest`, or in the UI.

Pick a target whose domain has a Data Steward. Only **22 of 46** domains do.

## 7. Troubleshooting

**`502 Proxy connection failed: null` with `server: istio-envoy`.** The gateway
is up but GMS is unreachable. Happened twice in a week on this tenant. The
frontend `/` still returns `200`, so check `/api/graphql` specifically. It has
resolved on its own within minutes both times; if not, check GMS pods in
`963c4ae6c4-pro-lemur-fda34e845`.

**Assignees come back empty right after filing.** Routing resolves
asynchronously. Reading immediately returns `[]`, which is indistinguishable
from a routing failure — it produced a false `ROUTING DIFFERS` on the first
run here. `verify_routing` polls for up to 12 seconds. If you query by hand,
retry before concluding anything.

**`actionRequest(urn:)` returns `null` for a request you just created.** Same
lag, different symptom. Read back through `listActionRequests` with
`allActionRequests: true` instead.

**Counts look far too low.** You forgot `allActionRequests: true`.
`listActionRequests` silently scopes to the calling user without it — on this
tenant that is the difference between 28 and 147 results.

**Everything `BLOCKED` in preflight.** The datasets have no domain, or their
domains have no Data Steward owner. That is the real prerequisite, not a bug;
see §8.

## 8. Limits — say these out loud

**The tag is not applied on approval.** Approvals record but no metadata
changes. Closing that gap means catching the `actionRequestStatus` event and
calling
`addTags(tagUrns, resourceUrn, subResourceType: DATASET_FIELD, subResource)`.
The listener in this repo already does decision → mutation for Propose New
Domain, so it is a known pattern, roughly a day's work — but it is not done.

**Domain ownership is the gating prerequisite.** Routing reads ownership
recorded on each domain:

| Measure | Count |
|---|---:|
| Domains | 46 |
| With any owner | 39 |
| **With a Data Steward** | **22** |
| Datasets assigned to a domain | 29 of 499 |

A classification whose dataset has no domain routes to nobody. Populating this
is a data exercise and it gates the entire approach, including the
simpler alternatives.

**Reviewers see a form task, not the native Proposals tab.** Arguably better —
confidence is a first-class field rather than buried in a note — but it is a
different surface, and native tag proposals filed elsewhere still route the old
way.

**Why not just use native tag proposals?** They cannot be routed.
`ActionWorkflowStepActors` is the only object in the schema carrying
`dynamicAssignment`, and `ActionWorkflowTriggerType` has exactly one value,
`FORM_SUBMITTED` — so a tag proposal cannot trigger a workflow by construction.
Workflow requests in turn cannot target a column, which is why the column
travels as a form field. That asymmetry is the reason this indirection exists,
and it is worth a feature request: either a proposal-triggered workflow type,
or `dynamicAssignment` on native proposal routing.
