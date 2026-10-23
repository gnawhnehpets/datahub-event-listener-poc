# Approval Workflows — Quick Guide

How to list the approval workflows on a DataHub Cloud instance and pull their full definitions,
using `curl` and `jq`.

## Terminology

In the GraphQL API and the metadata model, an approval workflow is an **action workflow**
(`urn:li:actionWorkflow:<id>`). A request someone submits against one is an **action request** of
type `WORKFLOW_FORM_REQUEST`.

| Concept          | GraphQL                                                 |
| ---------------- | ------------------------------------------------------- |
| List workflows   | `listActionWorkflows(input: ListActionWorkflowsInput!)` |
| Create / update  | `upsertActionWorkflow`                                  |
| Delete           | `deleteActionWorkflow`                                  |
| Submit a request | `createActionWorkflowFormRequestV2` (prefer over v1)    |
| Review a request | `reviewActionWorkflowFormRequest`                       |
| Cancel a request | `cancelActionWorkflowFormRequest`                       |

## 0. Setup (once)

### Install curl and jq

`curl` ships with macOS and most Linux distributions. `jq` usually does not:

```bash
brew install jq          # macOS
sudo apt-get install jq  # Debian / Ubuntu
```

Check both are available:

```bash
curl --version | head -1 && jq --version
```

### Generate a personal access token

1. Open your DataHub instance in a browser and sign in, for example
   `https://your-instance.acryl.io`.
2. Go to **Settings → Access Tokens** and click **Generate new token**.
3. Give it a name, pick an expiry, and click **Create**.
4. Copy the token now. DataHub shows it only once.

If **Access Tokens** is missing or the button is disabled, your account lacks the **Generate
Personal Access Tokens** privilege. Ask a DataHub admin to grant it.

A token only works on the instance that issued it.

### Set two environment variables

Every command below reads the instance URL and token from these variables. Set them in each new
terminal session:

```bash
export DATAHUB_URL=https://your-instance.acryl.io
```

```bash
printf 'Paste token: '; read -rs DATAHUB_TOKEN; echo; export DATAHUB_TOKEN
```

The second command reads the token without echoing it, so it doesn't end up in your shell history
or on screen. Use the base URL with no trailing slash.

### Check the connection

```bash
curl -s -X POST "$DATAHUB_URL/api/graphql" \
  -H "Authorization: Bearer $DATAHUB_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"query":"{ me { corpUser { username } } }"}' | jq
```

You should see your username. If not, see [Troubleshooting](#troubleshooting).

## 1. List all workflows

```bash
curl -s -X POST "$DATAHUB_URL/api/graphql" \
  -H "Authorization: Bearer $DATAHUB_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{
    "query": "query listWorkflows($input: ListActionWorkflowsInput!) { listActionWorkflows(input: $input) { start count total workflows { urn name category customCategory description trigger { type form { entityTypes entrypoints { type label } } } steps { id type description actors { users { urn username } groups { urn name } roles { urn name } } } } } }",
    "variables": { "input": { "start": 0, "count": 100 } }
  }' | jq
```

### Paging

`count` defaults to 20. Compare `total` with `start + count`. If more remain, rerun with `start`
increased by `count`.

### Filters

Add any of these to `input`:

| Field                | Example                      | Effect                                                          |
| -------------------- | ---------------------------- | --------------------------------------------------------------- |
| `category`           | `"ACCESS"` or `"CUSTOM"`     | Top-level category                                              |
| `customCategory`     | `"GOVERNANCE"`               | Only with `category: "CUSTOM"`                                  |
| `entrypointType`     | `"HOME"`, `"ENTITY_PROFILE"` | Workflows exposing that entry point                             |
| `entityType`         | `"DATASET"`                  | Workflows applicable to that entity type                        |
| `launchingEntityUrn` | `"urn:li:dataset:(...)"`     | With `ENTITY_PROFILE`, drops workflows whose condition is false |

### Useful jq one-liners

Pipe the curl output into these instead of plain `jq`:

```bash
# Name, URN and step count per workflow
jq -r '.data.listActionWorkflows.workflows[] | [.category, (.customCategory // "-"), .name, .urn, (.steps | length)] | @tsv'

# Workflows with no steps (cannot be approved)
jq '.data.listActionWorkflows.workflows[] | select(.steps | length == 0) | {name, urn}'

# Steps with no static actors (dynamic routing, or no approver at all)
jq '.data.listActionWorkflows.workflows[] | {name, steps: [.steps[] | select((.actors.users + .actors.groups + .actors.roles) | length == 0) | .id]} | select(.steps | length > 0)'
```

## 2. Pull the full workflow definitions

The GraphQL list above is a summary. It leaves out form fields, step routing (`dynamicAssignment`,
`dynamicSource`), `quorum` and `condition`. To get the exact definition as stored, read the
`actionWorkflowInfo` aspect through OpenAPI v3. The entity name in the path is lowercase,
`actionworkflow`.

### One workflow

```bash
URN=$(jq -rn --arg u 'urn:li:actionWorkflow:<workflow-id>' '$u|@uri')
curl -s -H "Authorization: Bearer $DATAHUB_TOKEN" \
  "$DATAHUB_URL/openapi/v3/entity/actionworkflow/$URN?aspects=actionWorkflowInfo&systemMetadata=false" \
  | jq '.actionWorkflowInfo.value'
```

Replace `<workflow-id>` with an ID from the list in section 1. The URN must be URL-encoded, which
the `jq @uri` line does.

### All workflows, one JSON file each

The list endpoint pages with a `scrollId`. Keep calling until the response has none.

```bash
OUT=workflows; mkdir -p "$OUT"; SCROLL=""; PAGE=$(mktemp)
while :; do
  ARGS=(--data-urlencode count=10 --data-urlencode aspects=actionWorkflowInfo --data-urlencode systemMetadata=false)
  [ -n "$SCROLL" ] && ARGS+=(--data-urlencode "scrollId=$SCROLL")
  curl -s -G -H "Authorization: Bearer $DATAHUB_TOKEN" "$DATAHUB_URL/openapi/v3/entity/actionworkflow" "${ARGS[@]}" > "$PAGE"
  for id in $(jq -r '.entities[].urn | sub("^urn:li:actionWorkflow:"; "")' "$PAGE"); do
    jq --arg u "urn:li:actionWorkflow:$id" \
      '.entities[] | select(.urn == $u) | {urn, definition: .actionWorkflowInfo.value}' \
      "$PAGE" > "$OUT/$id.json"
  done
  SCROLL=$(jq -r '.scrollId // empty' "$PAGE"); [ -z "$SCROLL" ] && break
done
rm -f "$PAGE"; ls "$OUT" | wc -l
```

The script works in both bash and zsh and prints the number of files written. Two zsh pitfalls it
avoids, in case you adapt it:

- `echo "$json" | jq` corrupts JSON that contains backslashes, because zsh's `echo` interprets
  escapes. The script writes each page to a file and runs `jq` on the file.
- `${SCROLL:+--data-urlencode scrollId=$SCROLL}` is not split into two arguments in zsh, so curl
  fails after the first page. The script builds the arguments in an array.

### What a definition contains

```text
definition
├─ name, description, category, customCategory, created, lastModified
├─ trigger.form
│  ├─ entityTypes            entity types the form applies to
│  ├─ entrypoints[]          type (HOME | ENTITY_PROFILE), label, optional filter
│  └─ fields[]               id, name, valueType, cardinality, required,
│                            allowedEntityTypes, allowedValues, condition, dynamicSource …
└─ steps[]                   run in order
   ├─ id, type (APPROVAL), description
   ├─ actors
   │  ├─ users[], groups[], roles[]         static approvers (URNs)
   │  ├─ dynamicAssignment                  e.g. ENTITY_DOMAIN_OWNERS + ownershipTypeUrns
   │  └─ dynamicSource.resolvers[]          e.g. OWNERS_OF field:domain, IDENTITY field:<id>
   ├─ quorum                                ANY_OF (default), ALL_OF, or N of M
   └─ condition                             step is skipped when this filter is false
```

### Useful queries over the dumped files

```bash
# Steps and how each one routes
jq -c '.definition.steps[] | {id, users: .actors.users, groups: .actors.groups,
  dynamicAssignment: .actors.dynamicAssignment, dynamicSource: .actors.dynamicSource,
  quorum, condition}' workflows/<workflow-id>.json

# Steps with no approver of any kind (no static actors, no dynamic routing)
jq -r '.urn as $u | .definition.steps[]?
  | select((.actors.users + .actors.groups + .actors.roles | length) == 0
           and (.actors | has("dynamicAssignment") or has("dynamicSource") | not))
  | "\($u)  \(.id)"' workflows/*.json

# Form fields of a workflow
jq -r '.definition.trigger.form.fields[] | [.id, .valueType, .cardinality, .required] | @tsv' \
  workflows/<workflow-id>.json
```

The files hold the stored aspect as is, so you can also keep them as a backup or diff them over
time.

## 3. Reading the result

- **`trigger.form.entrypoints`**: where users start the request. `HOME` is the home-page launcher;
  `ENTITY_PROFILE` is a button on the profile page of the types in `entityTypes`.
- **`steps`**: run in order. Each `APPROVAL` step needs a decision from one of its actors.
- **Empty `actors` in the GraphQL list**: not always a bug. Approvers may be resolved at request
  time from ownership or from a form field. Check the full definition (section 2) before you
  conclude a step has no approver.
- **`quorum`**: when unset, the first approval advances the step (ANY_OF).
- **Descriptions are not configuration.** A step's `description` is free text. It can promise
  routing ("all owners must approve", "only runs on PII datasets") that the stored `actors`,
  `quorum` and `condition` don't implement. Trust the stored fields.
- **Workflows with no steps** can be started but never approved. Look for `"steps": []`.
- **Duplicate names** are allowed. Two workflows can share a name under different URNs, and both
  show up in the launcher.

## Troubleshooting

| Symptom                                  | Cause / fix                                                                                                                                                   |
| ---------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `"status":401`, `"error":"Unauthorized"` | Token missing, expired, revoked, or issued by a different instance. Run `echo ${#DATAHUB_TOKEN}` (should not be 0), or generate a new token on `$DATAHUB_URL`. |
| Empty output, or `jq: parse error`       | The response was not JSON. Rerun the curl without `\| jq` to see the raw response.                                                                            |
| `curl: (3)` URL error                    | `DATAHUB_URL` is not set in this terminal. Run `echo "$DATAHUB_URL"` and set it again.                                                                        |
| `404`                                    | `DATAHUB_URL` ends with `/`. Remove the trailing slash.                                                                                                       |
| `Proxy connection failed`, `502`         | A local sandbox or proxy blocks the host; run from a normal terminal.                                                                                         |
| Field error in `errors[]`                | Field not in the deployed schema version; drop it from the query.                                                                                             |
| `command not found: jq`                  | Install jq (see Setup).                                                                                                                                       |
