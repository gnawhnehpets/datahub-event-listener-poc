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
.venv/bin/python workflows/definitions/upsert_workflows.py \
  --server "$DATAHUB_GMS_URL" --token-env DATAHUB_GMS_TOKEN \
  --file workflows/definitions/action-workflow-propose-new-domain_v2.json
```

`upsertActionWorkflow` replaces the whole definition for that URN, so re-running
it against a URN someone else owns overwrites their workflow.

3. A personal access token that can create domains. Settings → Access Tokens
→ Generate New Token. It needs `manageDomains`.

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

wf = {w["urn"] for w in c.execute(
    "query{ listActionWorkflows(input:{start:0,count:100}){ workflows { urn } } }", {}
)["listActionWorkflows"]["workflows"]}
import yaml
from expandvars import expandvars
for urn in yaml.safe_load(expandvars(open("action-route-workflow-decisions.yaml").read()))["action"]["config"]["workflows"]:
    print(f"workflow     : {'found' if urn in wf else 'MISSING'} {urn}")

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

### A workflow can look dead because you are watching a different one

A request raised against a workflow the listener does not route is simply
invisible — no error, no log line, nothing to notice. That is what a duplicated
workflow makes easy: the UI offers both, you submit through one, and the listener
watches the other.

It happened here. Thirteen Dataset Access Requests had accumulated on the `-2`
test copy while the recipe routed only the real workflow, so none of them
produced anything. The recipe now lists **both**, with the copy taking the real
one's behaviour through a YAML merge key so the two cannot drift:

```yaml
"urn:li:actionWorkflow:7110b19c-...": &dataset_access
  label: Dataset Access Request
  accepted: notify_approval
  rejected: notify_rejection
  notify: {...}

"urn:li:actionWorkflow:zz-dataset-access-request-2":
  <<: *dataset_access
  label: Dataset Access Request (test copy)
```

If a workflow seems to do nothing, check that the urn you are submitting against
is one of the lines the listener prints at startup. That table is the whole
contract.

### One recipient per send, and what happens when one fails

Each recipient gets their own message. Resend rejects the **entire** call if any
single address is unacceptable, so batching means one undeliverable co-owner
costs everyone else their notice — including the requester, who is who the mail
is for. Observed live: a rejection naming a group of DataHub sample users as
technical owners delivered to nobody, though the requester's address was fine.
It also stops a governance notice disclosing recipients to each other.

Failures are then handled by kind:

| Failure | Recorded? | Raised? | Why |
|---|---|---|---|
| 5xx, network | no | yes | may yet succeed, so the framework should retry |
| 4xx (bad address, rejected domain) | **yes**, as undeliverable | no | a 422 answers 422 forever; raising would burn four framework retries and bury the recipients who *were* served |

So a mixed audience behaves sensibly: the deliverable people are mailed, the
undeliverable ones are recorded once with the reason, and a replay re-attempts
neither. Identical failures are grouped, so six sample users produce one warning
line rather than six stack traces.

### Settling a notification that will never deliver

A send that fails is not recorded, so it retries — which is right for a transient
failure and wrong for a permanent one. Three old test rejections here named
DataHub's sample users as owners; those accounts have no email set, so the address
falls back to their email-shaped username (`alex@example.com`) and Resend's
sandbox sender answers `422`. Never deliverable, retried on every restart: eight
errors per replay.

Action requests cannot be deleted, and the sample users belong on the instance, so
the fix is to settle the notification rather than remove anything:

```bash
set -a && . ./.env && set +a
PYTHONPATH=src .venv/bin/python - <<'EOF'
import os, datetime
from actions.notify import _marker

REQUESTS = ["urn:li:actionRequest:..."]     # the ones failing in your log
RESULT = "REJECTED"

cfg = {"state_dir": os.path.join(os.environ["FAILED_EVENTS_DIR"], "notified")}
when = datetime.datetime.utcnow().isoformat(timespec="seconds")
for urn in REQUESTS:
    m = _marker(cfg, urn, RESULT)
    m.parent.mkdir(parents=True, exist_ok=True)
    m.write_text(f"{urn}\n{RESULT}\nSUPPRESSED {when}Z\nreason: not deliverable\n")
    print("suppressed", urn)
EOF
```

It writes the same marker a delivery would, because the effect wanted is
identical — do not send this again. Write the reason into the file; otherwise
that directory becomes a set of unexplained hashes.

Afterwards the replay was clean: 11 decisions, 3 already-notified, **0 errors**,
down from 8.

Do this only for notifications that genuinely cannot be delivered. Suppressing a
transient failure throws the notification away.

### What it does not cover

- **An outage longer than the window.** Decisions that fall out of `lookback_days`
  are gone. Raise it if the listener may be down for days; a larger window only
  costs a slower startup, since replayed work is suppressed either way.
- **A poisoned cursor.** See below — recovery depends on events arriving at all.

### If the listener goes quiet

A cursor can end up past the events you are waiting for, and the listener then
receives **nothing** while looking perfectly healthy. It happened to the name this
recipe used to carry:

| Pipeline | Events received |
|---|---|
| old name, any `consumer_id` | **0** |
| old name + `reset_offsets` + `lookback_days: 1` | 109 |
| **fresh name** | 127 |

`consumer_id` is not the lever — acryl-datahub-actions 1.7.0.7 ignores it and keys
the cursor on the **pipeline name**, as `urn:li:dataHubAction:<name>-<topic>`.
`reset_offsets` did not durably fix it either. Renaming the pipeline did, because
a new name is a new cursor. That is why this recipe is now
`action-route-workflow-decisions-v4`.

Confirm you are hitting it by running a throwaway pipeline under a different name
and seeing whether events appear. The cause is not established — the cursor is
server-side state in a Private Beta feature — and is worth raising with Acryl.

## Validate

```bash
.venv/bin/pip install pytest
.venv/bin/python -m pytest
```

14 tests, no credentials, covering each cell of the table. They drive the real
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

workflows/definitions/                 the form definition, and upsert_workflows.py
                                       to deploy it

tests/test_behaviour.py                proves the table above
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