# Domain-scoped approvers for Dataset Access Requests

DataHub Cloud — Action Workflows
Setup, rationale, and validated demo · 23 September 2026

---

## 1. The requirement

Approvals and notifications must reach the approver responsible for the affected
domain. Sending a request to every approver in the organisation does not meet the
operating model.

The reported symptom: assigning an approver group such as Data Steward caused
tasks to be assigned to **all** Data Stewards, rather than only those associated
with the domain being modified.

The question asked was whether the workflow can dynamically identify the relevant
domain and route approvals to the appropriate domain-specific approvers.

**Yes.** The mechanism is built into DataHub, it is now configured on the Dataset
Access Request workflow, and it has been verified end to end. This document
records how it works and how to demonstrate it.

## 2. Why the original configuration routed organisation-wide

The shipped approval step combined two settings, each of which widened the
audience independently:

```json
"users": [{ "urn": "urn:li:corpuser:data.steward" }],
"dynamicAssignment": {
    "type": "ENTITY_OWNERS",
    "ownershipTypes": []
}
```

- A **hardcoded user** was attached to the step, so that person was assigned to
  every request regardless of domain.
- **`ENTITY_OWNERS`** resolves the owners of the *dataset itself*, not the owners
  of its domain.
- **`ownershipTypes: []` matches every ownership type.** An empty list is not a
  filter — it is the absence of one. This is the most likely source of the
  reported "assigned to all Data Stewards" behaviour.

This was confirmed against live data rather than inferred. All 23 Dataset Access
Requests raised before the change were assigned to the same three people, on
every dataset:

```
users: ['urn:li:corpuser:data.steward',
        'urn:li:corpuser:stephen.hwang@datahub.com',
        'urn:li:corpuser:<a second dataset owner>']
```

## 3. The mechanism

DataHub provides a dynamic assignment type for exactly this purpose.
Introspecting the live GraphQL schema returns three options, with the platform's
own descriptions:

| `ActionWorkflowStepDynamicAssignmentType` | Description |
|---|---|
| `ENTITY_OWNERS` | Assign the entity owners. |
| `ENTITY_DOMAIN_OWNERS` | Assign the entity domain owners. |
| `ENTITY_DATA_PRODUCT_OWNERS` | Assign the entity data product owners. |

`ENTITY_DOMAIN_OWNERS` resolves the **domain of the entity the request was raised
against**, then takes that domain's owners. Pairing it with `ownershipTypeUrns`
narrows those owners to a single ownership type — here, Data Steward.

The result: a request raised against a dataset routes to the Data Steward of that
dataset's domain, and to nobody else. No per-domain configuration is required —
the workflow reads the ownership already recorded on each domain, so it scales
across the taxonomy without further setup.

## 4. The change applied

| | Before | After |
|---|---|---|
| Static actors | `users: [data.steward]` | `userUrns: []` |
| Assignment type | `ENTITY_OWNERS` | `ENTITY_DOMAIN_OWNERS` |
| Ownership filter | `ownershipTypes: []` (every type) | `__system__data_steward` |

```json
"actors": {
  "userUrns": [], "groupUrns": [], "roleUrns": [],
  "dynamicAssignment": {
    "type": "ENTITY_DOMAIN_OWNERS",
    "ownershipTypeUrns": ["urn:li:ownershipType:__system__data_steward"]
  }
}
```

The hardcoded user was removed deliberately. Leaving it would assign that person
to every request regardless of domain, which is the behaviour being eliminated.

Deployed to the Dataset Access Request workflow and verified live. The definition
is version-controlled at
`workflows/definitions/action-workflow-dataset-access-request_v2.json`.

## 5. Prerequisite: domain ownership must exist

**This determines whether the feature works in practice.** The routing is only as
good as the ownership recorded on each domain.

For a request to reach anyone, two things must be true: the dataset must be
assigned to a domain, and that domain must have an owner carrying the Data
Steward ownership type.

Current coverage:

| Measure | Count | Consequence |
|---|---:|---|
| Datasets total | 499 | |
| Datasets assigned to a domain | 29 | 470 have no domain — no approver can be resolved |
| Of those, in a domain with a Data Steward | 22 | 7 resolve to nobody |

Assigning a single organisation-wide Data Stewards group directly to the step is
the anti-pattern that produces the original symptom. Domain-scoped approval
instead requires each domain to carry its own steward as an owner. Populating
that ownership is a data exercise, not a configuration change.

## 6. Demo walkthrough

This sequence uses **one dataset and changes one variable**, so the causal link is
unambiguous: the workflow, the form, and the entity stay fixed; only the domain's
ownership changes, and the approver changes with it.

Setup used: the domain **Finance Division**, whose Data Steward was Fiona Green
(`urn:li:corpuser:sample_data_marty@example.com`). Note that this sample user's
display name does not match its username, which is momentarily confusing during a
demo.

### Step 0 — Capture the baseline

```bash
set -a && . ./.env && set +a
.venv/bin/python workflows/definitions/show_request_assignees.py
```

Every historical request lists three approvers. Screenshot this; it is the
"before".

### Step 1 — Raise a request on a domain you do not steward

1. Open the dataset `ORDER_HISTORY` (snowflake), which belongs to Finance Division.
2. Confirm the right-hand panel shows the domain **Finance Division**.
3. Choose **Request Access**, complete the justification, and submit.
4. Observe that the task is assigned to Fiona Green and **does not** appear in
   your Task Center — even though you are a Data Steward on other domains.

*This is the half that matters most. Being a steward somewhere does not place you
in every queue.*

### Step 2 — Add yourself as Data Steward of that domain

1. Open the **Finance Division** domain.
2. Add yourself as an owner with the ownership type **Data Steward**.

### Step 3 — Raise the same request again

1. Return to `ORDER_HISTORY` and submit another access request.
2. The task now lands in your Task Center.

Same dataset, same workflow, same form. Only the domain ownership changed, and
the routing followed it.

### Step 4 — Show the objective record

```bash
.venv/bin/python workflows/definitions/show_request_assignees.py
```

The Task Center can only show a reviewer their own queue, which cannot
demonstrate that a request did *not* also go to everyone else. Reading the
assignees off each request shows both halves at once.

## 7. Evidence from the validated run

| Date | Dataset / platform | Domain | Assigned to |
|---|---|---|---|
| 17 Sep | `banking_loans` (databricks) | — | `data.steward` + 2 dataset owners |
| 17 Sep | `loans_banking` (databricks) | — | `data.steward` + 2 dataset owners |
| 23 Sep | `customers` (dbt) | Data Platform Team | Stephen Hwang only |
| 23 Sep | `ORDER_HISTORY` (snowflake) | Finance Division | Fiona Green, then Stephen after ownership was added |

Two conclusions are visible in that table. Requests after the change carry a
single domain-specific approver rather than a fixed trio, and the hardcoded
`data.steward` account no longer appears.

## 8. Limitations and things to watch

- **A dataset with no domain, or a domain with no Data Steward, resolves to no
  approver at all.** Seven domain-assigned datasets and 470 domainless datasets
  are currently in that position. Avoid demonstrating on those.
- **Adding a second steward widens the audience for that domain.** After the demo
  above, Finance Division has two stewards, so repeating Step 1 will no longer
  exclude you until one is removed.
- **The corp group `urn:li:corpGroup:6bb71640-…` ("Data Stewards") does not exist
  as an entity** — `entityExists` returns `false`. It retains membership edges, so
  it appears in some views, but it cannot be used as an owner and DataHub rejects
  the attempt. Any workflow assigning it should be reviewed.
- **The form can also be launched from Home** rather than a dataset profile.
  Domain resolution needs a launching entity. All 23 historical requests carried
  one, so no failure was observed, but the path has not been explicitly tested.
- **Definitions exported by `fetch_action_workflows.py` cannot be re-submitted
  unmodified.** The server derives `filterCondition` (v2) from `condition` (v1),
  returns both, then rejects input containing both. The derived value must be
  omitted.

## 9. Reference

| Item | Value |
|---|---|
| Workflow | `urn:li:actionWorkflow:7110b19c-45b9-4450-b8bb-ba47d9f522ba` |
| Definition | `workflows/definitions/action-workflow-dataset-access-request_v2.json` |
| Validation helper | `workflows/definitions/show_request_assignees.py` |
| Deploy | `upsert_workflows.py --server $DATAHUB_GMS_URL --token-env DATAHUB_GMS_TOKEN --file <definition>` |
| Ownership type | `urn:li:ownershipType:__system__data_steward` |
