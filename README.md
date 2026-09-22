# datahub-event-listener — action config (minimal v3)

A DataHub Actions listener that reacts to Action Workflow decisions. This branch
contains only what is needed to run and validate this:

| Workflow | ACCEPTED | REJECTED |
|---|---|---|
| Propose New Domain | creates the domain | emails requester + form owners |
| Dataset Access Request | emails the requester | emails the requester |


## Before you run
You'll need:

1. Python 3.12. What this was verified against.

2. A DataHub Cloud instance with Action Workflows, and the two
workflows already created there. This repo carries the Propose New Domain
definition; deploy it with:

```bash
set -a && . ./.env && set +a

.venv/bin/python workflows/definitions/upsert_workflows.py \
  --server "$DATAHUB_GMS_URL" --token-env DATAHUB_GMS_TOKEN \
  --file workflows/definitions/action-workflow-propose-new-domain_v2.json
```

`upsertActionWorkflow` replaces the whole definition for that URN, so re-running
it against a URN someone else owns overwrites their workflow.

3. A personal access token that can create domains. Settings → Access Tokens
→ Generate New Token. It needs `manageDomains` and `listActionWorkflows`
(startup now calls `listActionWorkflows` to validate and resolve workflow
keys; a token without that privilege turns any config into a startup refusal).

4. The two structured properties, scoped to the domain entity type:
`collibra.gcDomainCode` (MULTIPLE) and `collibra.lobLevel` (SINGLE). The listener
stamps both onto every domain it creates. Missing, the create succeeds and the
enrichment fails — after all three approvals.

5. Parent domains carrying a GC code. The code for a new domain is derived
from its parents. A parent with no `collibra.gcDomainCode` is rejected with
`has no GC Domain Code`, and a parent carrying several is rejected as ambiguous
unless the requester picks one — both after approval, both by design, because
guessing stamps a wrong code onto a governance artifact and nobody finds out.

6. A Resend account, if you want the email actions. Sign up at
[resend.com](https://resend.com) — the free tier is enough — and create an API
key under **API Keys**. Put it in `.env` as `RESEND_API_KEY`.

> The shipped `from_address` is `onboarding@resend.dev`, Resend's shared testing
> sender. It only delivers to addresses you control. Sending to anyone else
> returns `422 Invalid 'to' field` — which is exactly what happened here when a
> replay tried to mail the `example.com` owners on old test requests. To reach
> real recipients, verify your own domain in Resend and change `from_address` in
> the recipe.
>
> Don't want email at all? Route those outcomes to `log` instead and skip this
> entirely. DataHub still notifies the requester natively; you lose the reason
> and the submitted values. The pipeline refuses to start if an outcome routes
> to a `notify_*` action with no key, rather than failing on the first real
> decision.

### Check them

```bash
set -a && . ./.env && set +a
.venv/bin/python - <<'EOF'
import os
from actions.client import DataHubClient

c = DataHubClient(server=os.environ["DATAHUB_GMS_URL"], token=os.environ["DATAHUB_GMS_TOKEN"])
me = c.execute("query{ me { corpUser { urn } platformPrivileges { manageDomains } } }", {})["me"]
print("token        :", me["corpUser"]["urn"], "| manageDomains:", me["platformPrivileges"]["manageDomains"])

q = """query($urn:String!){ entity(urn:$urn){ ... on StructuredPropertyEntity {
  definition { qualifiedName cardinality } } } }"""
for name in ("collibra.gcDomainCode", "collibra.lobLevel"):
    d = (c.execute(q, {"urn": f"urn:li:structuredProperty:{name}"})["entity"] or {}).get("definition")
    print(f"property     : {name} -> {d['cardinality'] if d else 'MISSING'}")

live = c.execute(
    "query{ listActionWorkflows(input:{start:0,count:1000}){ workflows { urn name } } }", {}
)["listActionWorkflows"]["workflows"]
live_urns = {w["urn"] for w in live if w.get("urn")}
from collections import Counter
name_counts = Counter(w["name"].strip() for w in live if w.get("name"))
import yaml
from expandvars import expandvars
for key in yaml.safe_load(expandvars(open("action-route-workflow-decisions.yaml").read()))["action"]["config"]["workflows"]:
    key_s = key.strip()
    if key_s in live_urns:
        label = "found"
    elif name_counts.get(key_s, 0) > 1:
        label = "AMBIGUOUS"
    elif name_counts.get(key_s, 0) == 1:
        label = "found"
    else:
        label = "MISSING"
    print(f"workflow     : {label} {key}")

key = os.environ.get("RESEND_API_KEY", "")
print("resend key   :", "set" if key else "NOT SET (fine if every outcome routes to log)")
EOF
```

Every line should read `True`, `MULTIPLE`/`SINGLE`, `found`, and `set`.

## Run

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt

cp .env.example .env && $EDITOR .env     # see "Before you run" above
set -a && . ./.env && set +a

.venv/bin/datahub-actions actions run -c action-route-workflow-decisions.yaml
```

`.env` carries `PYTHONPATH=src` and `FAILED_EVENTS_DIR`, so loading it is the
whole setup. Use `datahub-actions`, not `datahub` — the latter's log filter hides
this package's output at INFO, so the routing table never prints and a working
listener looks dead.

A healthy start prints one line per workflow and outcome:

```
Routing urn:li:actionWorkflow:7110b19c-... (Dataset Access Request) on ACCEPTED -> notify_approval, REJECTED -> notify_rejection
Routing urn:li:actionWorkflow:propose-new-domain (Propose New Domain) on ACCEPTED -> create_domain, REJECTED -> notify_rejection
Action Pipeline with name 'action-route-workflow-decisions' is now running.
```

## Recovery: what happens if the listener is down

`lookback_days: 7` makes a decision survive an outage. On startup the source
re-reads that window, so an approval granted while the listener was stopped still
creates its domain when it comes back.

Verified: a request was approved through all three gates with **no listener
running**, the domain did not exist, and starting the listener afterwards created
it.

### It replays on every restart, not just after an outage

A restart five minutes after the last one re-reads the whole window. Both actions
are built for that, and they get there differently:

| Action | Replay behaviour |
|---|---|
| `create_domain` | Idempotent. Deterministic urn, create skipped when the entity exists, both enrich calls are upserts. Converges. |
| `notify_approval` / `notify_rejection` | Made idempotent. Sending is not naturally repeatable, so each (request, outcome) is recorded once delivered and never sent again. |

Measured across two runs of the same window:

```
run 1   12 decisions   3 emails sent   0 created   5 already exists
run 2   12 decisions   0 emails sent   0 created   5 already exists   3 already notified
```

The record lives in `notify.state_dir` (default `${FAILED_EVENTS_DIR}/notified`),
one small file per delivery. **It must survive restarts** — an in-process set
would defend against nothing, since a restart is the thing being defended
against. If you wipe that directory, the next replay re-sends the window.

A send that *fails* is deliberately not recorded, so it will be retried. The
flip side: a permanently failing send — a recipient Resend will not deliver to,
say — retries on every restart. `failure_mode: CONTINUE` keeps that from stopping
the pipeline, but it is noise in the log.

### What it does not cover

- **An outage longer than the window.** Decisions that fall out of `lookback_days`
  are gone. Raise it if the listener may be down for days; a larger window only
  costs a slower startup, since replayed work is suppressed either way.

## Pointing at another deployment

`generate_form_fields.py --write` regenerates every instance-specific reference
inside the form definition — `parent_domain`'s `valueType`, `allowedEntityTypes`,
and `allowedValues` (the curated Level 1 list), the `level_2_*` conditions and
options, the `l1_gc_domain_code` values, and the field ids derived from domain names.

The routing table needs one manual decision. Keys in
`action-route-workflow-decisions.yaml` may be a workflow **URN** or a workflow
**name**:

- A workflow this repo deploys has a URN *you* chose (`propose-new-domain`), so
  it is portable. Keep it URN-keyed.
- A workflow created through the DataHub UI has a per-instance GUID. Key it by
  **name** so it resolves on any deployment.

Every key is validated at startup. An unknown or ambiguous key stops the
listener with the available names listed — deliberately, because an unmatched key
otherwise means decisions from that workflow are silently ignored.

Name-keying is not universally available: two workflows sharing a name cannot be
addressed by it, and the error says so. Use the URN for those.

Still manual on a new deployment, and not covered by the generator:

- The two structured properties `collibra.gcDomainCode` and `collibra.lobLevel`
  must exist, or the domain create succeeds and the enrichment fails after all
  three approvals.
- `QUERY_COUNT = 1000` in the generator caps the domain fetch; past that it
  raises `IncompleteFetch` rather than proposing mass removals.

### Drift detection and its limits

`generate_form_fields.py --check` is the drift detector: it exits 1 if the
committed definition no longer matches the live domain tree. **Nothing runs it
automatically on this branch** — there is no CI, no cron, and no scheduled job.
You must run it manually after any structural change to the domain hierarchy.

Two failure modes worth knowing before you commit to that cadence:

- **Renaming a root domain** breaks in-flight requests loudly, but only after
  all three approvals have completed. The submitted name no longer matches any
  root, so `_resolve_root` raises `HierarchyError`. The form must be regenerated,
  deployed, and the request resubmitted.

- **Creating a new root domain** makes that domain unproposable-under until the
  form is regenerated and deployed. No error is raised at submission time — the
  name simply does not appear in the dropdown, so the requester cannot select it.
  The window is bounded by when you next run `--write` and deploy.

Contrast this with the workflow-name resolution added in the same change cycle:
a misconfigured workflow key fails fast at startup, before any event is processed,
giving the operator an actionable error immediately. The domain-dropdown staleness
fails late, after approvals, and only for the affected domains. Both are design
choices; neither is an oversight. Know which you are dealing with.

## Known upstream issues

Behaviour in `acryl-datahub-actions==1.7.0.7` that is not ours to fix, recorded
because each one presents as a bug in this repo. All raised with Acryl.

Version-scoped deliberately: the offset behaviour is server-side, so another
tenant or release may differ. Re-check before assuming these still hold.

### `consumer_id` is declared but never read

`DataHubEventsSourceConfig` accepts `consumer_id` with the description *"Used to
store offset for the consumer."* The value is never read. In
`datahub_actions/plugin/source/acryl/datahub_cloud_event_source.py` the identity
comes from the pipeline name instead (line ~94):

```python
self.base_consumer_id = DataHubEventSource._get_pipeline_urn(
    self.ctx.pipeline_name
)
```

Consumers are then keyed `f"{base_consumer_id}-{topic}"`, i.e.
`urn:li:dataHubAction:<pipeline-name>-<topic>`. A search for
`source_config.consumer_id` across the package returns nothing.

**Consequence:** setting `consumer_id` does nothing, silently. The only way to
change the offset identity is to **rename the pipeline**. This repo therefore
omits the field rather than setting it to something misleading.

### A stuck cursor looks exactly like a healthy listener

Because the cursor is server-side and keyed on the pipeline name, it can advance
past events you are waiting for. The listener then starts cleanly, prints its
routing table, reports "now running" — and receives nothing. There is no error.

`reset_offsets: true` is accepted but did not durably fix it here. Measured
against one pipeline:

| Configuration | Events received |
|---|---|
| Old pipeline name, any `consumer_id` | 0 |
| Old name + `reset_offsets` + `lookback_days: 1` | 109 |
| **Fresh pipeline name** | **127** |

`reset_offsets` worked for a single run, then a later restart returned to the
stuck cursor. Renaming the pipeline was the only durable fix — a blunt
instrument, since a new name discards all offset state.

**If this listener ever goes quiet while decisions are being made, suspect the
cursor before the code.** Confirm by running a throwaway pipeline under a
different name; if that receives events, the cursor was the problem.

We have asked Acryl whether a committed offset can be inspected or rewound.

### `filterCondition` cannot restrict an entity picker by hierarchy

We wanted `parent_domain` to offer only root (Level 1) domains. **It cannot be
done**, and this is recorded so nobody spends another afternoon on it.

`upsertActionWorkflow` validates `filterCondition` against a fixed allowlist and
rejects the entire mutation otherwise:

```
Workflow validation failed: [FILTER] field[parent_domain].filterCondition:
Criterion.field '<x>' is neither a 'formField:<id>' reference nor a known
launching-entity attribute [owners, entityType, subTypes, tags, platform,
domain, applications, dataProducts, container, glossaryTerms]
```

Three candidates were tested against a live instance. All rejected:

| Candidate | Valid search-index field? | `filterCondition`? |
|---|---|---|
| `parentDomain` EXISTS, negated | Yes — partitions 19 / 26 | **Rejected** |
| `hasParentDomain` EQUAL `"false"` | Yes — partitions 20 / 26 | **Rejected** |
| `structuredProperties.collibra.lobLevel` EQUAL `"1"` | — | **Rejected** |

The first two are real, working index fields — verified by querying
`searchAcrossEntities` directly, where each cleanly separates root domains from
Level 2s and 3s. **`filterCondition` simply does not consult the search index.**

The fields it *does* accept (`tags`, `subTypes`, `entityType`, `domain` — all
confirmed accepted) are useless here, because **none of them exist on the `Domain`
type**. `Field 'tags' in type 'Domain' is undefined`; Domain exposes only
`properties`, `ownership`, `parentDomains`, `structuredProperties` and
`displayProperties`. So a tag-based workaround would validate and then silently
match nothing, emptying the picker with no API-visible symptom.

**`filterCondition` is not a candidate filter at all.** It controls field
*visibility*. Proof: the server derives it from `condition`. Our `level_2_*`
fields set only `condition` (`SINGLE_FIELD_VALUE` on `parent_domain`), and the
live definition reports a matching `filterCondition` the repo never wrote:

```
condition       : SINGLE_FIELD_VALUE  field=parent_domain  EQUAL [urn:li:domain:LOB]
filterCondition : AND [ field=formField:parent_domain  EQUAL [urn:li:domain:LOB] ]
```

So "allow `hasParentDomain` here" would mean *show the field when the launching
entity has no parent* — unrelated to which domains appear in the dropdown.

### There is no way to constrain a URN entity picker

Pursuing it properly leads to a dead end that is worth recording in full, because
the server's own error message sends you to a mechanism that does not exist.

`allowedValues` on a URN field is rejected — with advice:

```
[R_VALUE_TYPE_MISMATCH] field[parent_domain].allowedValues: URN-typed fields
cannot use allowedValues — the picker is not value-scoped. Express the curated
URN list as a dynamicSource with resolver=LITERAL_ENTITIES and the target URNs
instead.
```

Following that advice fails:

```
[R1] field[parent_domain].dynamicSource.resolvers[0].resolver:
Unknown resolver: LITERAL_ENTITIES
```

**The validator recommends a resolver the same validator rejects.** Sweeping
thirteen plausible names (`LITERAL`, `ENTITIES`, `STATIC`, `URNS`, `SEARCH`,
`EXPLICIT`, …) returns `Unknown resolver` for every one. The only resolver that
exists is `IDENTITY`, and it is explicitly the wrong tool:

```
[R2] Resolver 'IDENTITY' returns actor URNs (CorpUser / CorpGroup)
```

`dynamicSource.filter` accepts a `DataHubViewFilterInput` and would be the natural
home for `hasParentDomain`, but it cannot be used alone —
`UpsertDynamicSourceInput.resolvers must not be empty` — and no resolver exists to
pair it with.

So: `allowedValues` forbidden, `dynamicSource` unusable, `filterCondition` is a
different feature. **A URN picker cannot be narrowed at all in 1.7.0.7.**

**Two traps worth internalising:**

1. **GraphQL introspection does not catch any of this.**
   `FacetFilterInput.field` and `DynamicResolverInput.resolver` are both typed
   `String`, so every rejected value above is type-valid. The constraints are
   semantic and enforced only at upsert.
2. **`upsert_workflows.py --dry-run` is worthless as validation.** It makes no
   HTTP call — it only checks the file parses and has an `input` key. It reported
   success on a definition the server rejects with a 400.

The good news: **a failed upsert is completely safe.** The 400 is atomic, and live
state was verified byte-identical afterwards (`lastModified`, field and step counts
unchanged). Attempting a real upsert is the only trustworthy validation, and it
costs nothing. Probing on a throwaway URN with `entrypoints: []` keeps it invisible
to users; `deleteActionWorkflow` cleans up.

Consequence for the form: rather than an unconstrained URN picker, `parent_domain`
is now a generated STRING dropdown listing only Level 1 (root) domain names.
The generator scopes the list to roots at write time; the listener resolves the
submitted name back to a root URN at runtime and refuses any name that does not
match a current root — so a Level 2 or Level 3 domain cannot be chosen even if
the dropdown were to become stale.

### Windows is not a declared platform

The package declares `Operating System :: Unix`, `POSIX :: Linux` and
`Environment :: MacOS X` — no Windows. In practice it does start on Windows:
there is a single `SIGINT` handler, and Windows wheels exist for
`confluent-kafka` and `psycopg2-binary`. Treat it as viable but vendor-untested,
and expect to own any breakage.

The commands in [Run](#run) are bash-specific, so a Windows user is blocked
before reaching any of this. Setup instructions for Git Bash, WSL2 and the VM and
container fallbacks live on the `dev/windows` branch.

### Related: the wider question

These are all consequences of self-hosting a consumer. We have asked Acryl what
deployment topology they intend for custom Actions — whether they can run inside
DataHub Cloud, or ride the Remote Executor that `ExecutorAction` already uses for
ingestion. A hosted answer would retire this whole section.

## Validate

```bash
.venv/bin/pip install pytest
.venv/bin/python -m pytest
```

66 tests, no credentials, covering each cell of the table. They drive the real
router against the shipped config file and fake only the two edges, GraphQL
and Resend — so a test cannot pass while the YAML says something else.

Each cell is mutation-checked: flipping any outcome to `log`, or changing who is
notified, fails the suite.

## Layout

```
action-route-workflow-decisions.yaml   the pipeline: source, filter, routing, notify config

src/actions/router.py                  resolve the workflow, dispatch on outcome
src/actions/domain.py                  create the proposed domain
src/actions/notify.py                  render and send the outcome email
src/actions/client.py                  every GraphQL call
src/actions/fields.py                  submitted-form parsing
src/actions/hierarchy.py               domain levels and the depth limit
src/actions/domain_code.py             GC code selection and the alpha prefixes
src/actions/properties.py              structured-property and ownership payloads
src/actions/recipients.py              principal -> email address
src/actions/errors.py                  the exception types

workflows/definitions/                 the form definition and upsert_workflows.py to deploy it
workflows/definitions/generate_form_fields.py
                                       regenerates parent_domain (valueType, allowedEntityTypes,
                                       allowedValues), l1_gc_domain_code values/conditions,
                                       and level_2_* dropdowns from the live domain tree

tests/test_behaviour.py                proves the table above
tests/test_generator.py                unit tests for the pure derivation in generate_form_fields.py
```

## The one thing not to lose

`src/actions/domain.py` keeps four guards, each because its absence produced a
wrong governance artifact. Rewrite that file freely; keep these:

| Guard | Refuses |
|---|---|
| Level 1 derived upward from the chosen parent | mismatched Level 1 / Level 2 pairs, caught only after all three approvals |
| `select_code` will not guess | a wrong-but-valid GC code stamped silently when a parent carries several |
| Depth check | a domain at Level 4 in a 3-level taxonomy |
| Id derived from the code, not the name | two same-named domains colliding on one URN |

NOTE: dataset-access approval workflow is currently configured to validate multiple
workflows with custom actions with a single listener process. It only sends emails
for approval or rejections. 