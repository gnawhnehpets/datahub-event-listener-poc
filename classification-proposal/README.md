# Classification tag proposals

Files classification/PII tag proposals — column-level and asset-level — against
the `testworkspaceforpoc` Databricks tables on pro-lemur, each carrying a
confidence score. Built to populate the Task Center with a realistic review
queue, and to demonstrate that an automated classifier can feed DataHub's
governance workflow through the public API.

---

## Yes, tag proposals can be created programmatically via the GraphQL API

This needs saying plainly, because the opposite belief has been circulating.

The mutation exists, is public, and works:

```graphql
proposeTags(input: ProposeTagsInput): String!
```

It returns the created `actionRequest` URN. `ProposeTagsInput` accepts:

| Field | Purpose |
|---|---|
| `resourceUrn` | the target dataset |
| `tagUrns` | the tags to propose |
| `subResourceType` | `DATASET_FIELD` — makes it a **column-level** proposal |
| `subResource` | the column's `fieldPath` |
| `description` | the proposal Note (the schema calls it "also called proposalNote") |
| `origin` | `MANUAL` or `INFERRED` |
| `inferenceMetadata` | required when `origin: INFERRED` |

Omit `subResourceType` and `subResource` and the proposal applies to the
dataset itself. That omission is the *only* structural difference between an
asset-level and a column-level proposal.

### The evidence, not the assertion

Verified against the live pro-lemur tenant:

- **67 `TAG_ASSOCIATION` proposals** exist — 61 `PENDING`, 6 `COMPLETED`.
- **61 column-level and 6 asset-level**, so both shapes demonstrably work.
- **4 script-created proposals have been `ACCEPTED`** through the normal review
  flow. One of them was actioned by `gita.raman@example.com` — a customer
  reviewer, not us.
- Those accepts **applied their tags**. On
  `testworkspaceforpoc.default.pattern_analysis`:

  | Column | Tag now applied |
  |---|---|
  | `SOURCE_TABLE_FORMAT` | `Public` |
  | `SOURCE_TABLE_SCHEMA` | `Restricted` |
  | `SOURCE_PATH` | `Restricted` |
  | `SOURCE_TABLE_PATH` | `PII_Data` |

So the full lifecycle is closed: **API call → Task Center → human review →
metadata applied.** Nothing about a proposal's origin limits what a reviewer
can do with it.

### Why people conclude it is impossible

Almost always the endpoint. There is one working path and two convincing
dead ends:

| URL | Result |
|---|---|
| `https://<host>/api/graphql` | **200** — this is the one |
| `https://<host>/gms/graphql` | 404 `{"error":"Not Found","path":"/graphql"}` |
| `https://<host>/graphql` | 404 `{"error":"Not Found"}` |

The trap: `~/.datahubenv` stores `gms.server` as `https://<host>/gms`. Appending
`/graphql` to it — the obvious move — yields a 404 that reads like "this API
does not exist" rather than "wrong path". Mutations are served by the
**frontend proxy** at `/api/graphql`, not by GMS directly.

`datahub_client.py` derives the right URL by stripping the `/gms` suffix and
appending `/api/graphql`, which is why this tool works against the same config
file that makes hand-rolled calls 404.

### The one genuine limitation

`origin: INFERRED` and `inferenceMetadata` are **silently discarded** by
DataHub v2.1.3. The client sends both; the mutation returns success with no
error; the server stores `origin: MANUAL` and omits `inferenceMetadata`
entirely. All 67 tag proposals on the tenant read `MANUAL`, and none carries
inference metadata.

This is a server-side gap, not a client bug — the payload is a faithful
encoding of what the schema declares. Practical consequence: **the Note is
currently the only place a confidence score survives.** The code still sends
the native fields so it starts working if a future version honours them.

Worth raising with DataHub engineering on its own merits: a mutation that
accepts fields and discards them without warning is a poor failure mode, and
is very likely part of why the API is believed not to support this.

---

## Data flow

```
                    ~/.datahubenv_prolemur
                    (gms.server + PAT)
                            |
                            v
              +---------------------------+
              |   load_credentials()      |   decode JWT exp locally
              |   datahub_client.py       |   reject if expired / http:// / non-str
              +---------------------------+
                            |  Credentials(graphql_url, token)
                            v
              +---------------------------+
              |   fetch_columns()         |   GraphQL query, per chosen dataset
              |   -> ["account_id", ...]  |   live, so it never drifts from the data
              +---------------------------+
                            |  {asset_name: [fieldPath, ...]}
                            v
              +---------------------------+
              |   generate_proposals()    |   PURE - no network, no clock,
              |   proposals.py            |   seeded RNG only
              |                           |
              |   per dataset:  1 asset-level  (sub_resource = None)
              |   then --count: N column-level (sub_resource = fieldPath)
              |   each: random tag, score in [0.50, 0.99],
              |         bucket -> confidenceLevel, note -> "confidence: X% (L)"
              +---------------------------+
                            |  list[Proposal]
                            v
              +---------------------------+
              |   safety cap              |   total > 50 and not --force -> exit 1
              |   planned total printed   |   BEFORE any write
              +---------------------------+
                            |
                            v
              +---------------------------+
              |   propose_tag() x N       |   POST /api/graphql
              |   mutation proposeTags    |   one call per proposal
              +---------------------------+
                            |  actionRequest URN per call
                            v
              +---------------------------+
              |   summary                 |   OK / FAIL per proposal,
              |                           |   then all URNs (printed even on
              |                           |   crash or Ctrl-C)
              +---------------------------+
```

Three modules, one hard boundary:

| Module | Role | Network |
|---|---|---|
| `proposals.py` | tag pool, asset catalog, confidence maths, generation | never |
| `datahub_client.py` | credentials, expiry check, GraphQL transport | all of it |
| `propose_classifications.py` | argparse, wiring, output | none directly |

Generation is pure so the logic that matters — confidence bucketing, the
column/asset split, the no-dedupe rule — is testable without a DataHub
instance. That is why 97 tests run with no credentials.

---

## Where this fits in the classification approval process

The script occupies exactly one step: it is a **stand-in for the classifier**.

```
  1. CLASSIFY        an automated scanner inspects columns and decides
                     "this looks like PII" with some confidence
                            |
                            |   <-- THIS SCRIPT SUBSTITUTES HERE
                            v
  2. PROPOSE         proposeTags() files a TAG_ASSOCIATION action request,
                     status PENDING, carrying the confidence in the Note
                            |
                            v
  3. ROUTE           DataHub assigns reviewers from the resource's owners
                     and any configured policy. Assignment is server-side;
                     the proposer does not choose.
                            |
                            v
  4. REVIEW          reviewers see it in Task Center / the asset's Proposals
                     tab and weigh the tag against the confidence score
                            |
                    +-------+-------+
                    v               v
  5a. ACCEPT                   5b. REJECT
      tag applied to the           proposal closed COMPLETED/REJECTED,
      column, stored in            no metadata change
      editableSchemaMetadata
```

What this means in practice:

- **Nothing downstream of step 2 is bypassed.** A programmatic proposal is an
  ordinary proposal. It routes, queues, and is accepted or rejected by the same
  rules as one filed by hand in the UI.
- **The proposer is not the approver.** You are only a reviewer on your own
  proposals if the routing rules put you there. Filing 20 proposals does not
  mean 20 items appear in *your* Task Center — they appear in the assigned
  reviewers' queues. This surprises people; it is working as designed.
- **Accepted field tags land in `editableSchemaMetadata`**, not
  `schemaMetadata.globalTags`. `schemaMetadata` holds what ingestion supplied;
  the editable aspect holds human and proposal-applied metadata. Query the
  wrong one and an applied tag looks missing.
- **Rejection is the cleanup path.** Proposals cannot be deleted. The summary
  prints every created URN so a batch can be bulk-rejected via the UI or
  `rejectProposals(urns, note)`.

The confidence score is the point of the exercise: it gives a reviewer a
reason to trust or distrust each suggestion, which is what makes
machine-generated governance reviewable at all rather than a wall of
undifferentiated suggestions.

---

## Warning: no dry-run

Invoking this **writes**. `--count 12` files 12 column-level proposals plus one
asset-level proposal per dataset touched, and re-running **adds more rather
than replacing**. The planned total prints before submission, and every created
`actionRequest` URN prints after so a batch can be rejected in bulk.

A **safety cap of 50 total proposals** (column-level + asset-level) blocks runs
that exceed it, guarding against fat-finger counts like `--count 120` instead
of `12`. Add `--force` to override when a large batch is genuinely intended.

## Credentials

Reads `~/.datahubenv_prolemur` (override with `DATAHUB_ENV_FILE`):

```yaml
gms:
  server: https://pro-lemur-fda34e845.trials.acryl.io/gms
  token: <Personal Access Token>
```

Use a **Personal** token so proposals are attributed to you rather than
`admin`. Generate one in DataHub under Settings -> Access Tokens.

Validated before anything is written: the JWT `exp` claim is decoded locally
and an expired token aborts the run naming the expiry date, rather than
surfacing as a 401 midway through a batch. A non-`https://` server is refused
outright so a Bearer token is never sent in cleartext.

## Usage

Run with the repo virtualenv — `./.venv/bin/python`. System Python lacks
`datahub_actions` and fails at import.

```bash
# list the catalog and exit, writing nothing
./.venv/bin/python classification-proposal/propose_classifications.py

# 12 column-level + 1 asset-level = 13 proposals
./.venv/bin/python classification-proposal/propose_classifications.py --asset banking_accounts

# small and reproducible
./.venv/bin/python classification-proposal/propose_classifications.py \
  --asset banking_loans --count 5 --seed 42

# across all five tables: 20 column + 5 asset = 25
./.venv/bin/python classification-proposal/propose_classifications.py --asset all --count 20
```

| Flag | Meaning |
|---|---|
| `--asset` | `banking_accounts`, `banking_loans`, `banking_customers`, `banking_transactions`, `pattern_analysis`, or `all`. Omit to list and exit. |
| `--count` | Column-level proposals (default 12). Asset-level are **extra**: one per dataset. |
| `--seed` | Reproducible run. |
| `--force` | Bypass the 50-proposal safety cap. This tool writes live; use with care. |

| Command | Column | Asset | Total |
|---|---|---|---|
| `--asset banking_loans` | 12 | 1 | **13** |
| `--asset banking_loans --count 5` | 5 | 1 | **6** |
| `--asset all --count 20` | 20 | 5 | **25** |
| `--asset all` | 12 | 5 | **17** |

Note `--count 0` still files the asset-level proposals — one per dataset — so
`--asset all --count 0` writes 5.

Duplicate `(column, tag)` pairs are **not** deduped. Filing several proposals
against one attribute is intentional: it is what a real classifier does when
successive runs disagree, and it is what makes the review queue realistic.

## Confidence

Each proposal draws one score in `[0.50, 0.99]`, carried two ways:

- **Note** (`description`) — `confidence: 87.342% (High)`. Three decimals,
  zero-padded so a column of notes aligns. This is the only value that
  currently persists.
- **Native** (`inferenceMetadata.confidenceLevel`) — `0` Low `<0.70`, `1`
  Medium `<0.85`, `2` High otherwise. Sent, accepted, and dropped by the
  server; see the limitation above.

Both derive from one draw, so they can never disagree. Bucketing uses the
**unrounded** score: rounding first could make the native field contradict the
score actually drawn, and the native field is what a reviewer would filter on.

## Tests

```bash
./.venv/bin/python -m pytest
```

97 tests, no credentials and no network required. `proposals.py` and the
credential/payload logic are covered directly; the CLI is driven through
`main()` with a fake client. The GraphQL transport itself is not unit-tested —
its only meaningful verification is a live call.

## Verifying what a run did

```bash
./.venv/bin/python - <<'PY'
import json, pathlib, urllib.request, yaml
cfg = yaml.safe_load(pathlib.Path("~/.datahubenv_prolemur").expanduser().read_text())
url = cfg["gms"]["server"].rstrip("/").removesuffix("/gms") + "/api/graphql"
q = """{ listActionRequests(input:{start:0,count:25,allActionRequests:true,
  resourceUrn:"urn:li:dataset:(urn:li:dataPlatform:databricks,testworkspaceforpoc.default.banking_accounts,PROD)"})
  { total actionRequests { urn type status subResource description } } }"""
req = urllib.request.Request(url, json.dumps({"query": q}).encode(),
    {"Authorization": f"Bearer {cfg['gms']['token']}", "Content-Type": "application/json"})
d = json.load(urllib.request.urlopen(req))["data"]["listActionRequests"]
print("total:", d["total"])
for a in d["actionRequests"]:
    print(f'  {a["status"]:8} col={a["subResource"] or "ASSET-LEVEL":16} {a["description"]}')
PY
```

Pass `allActionRequests: true`. Without it `listActionRequests` returns only
requests scoped to the calling user and the totals come back badly understated.
