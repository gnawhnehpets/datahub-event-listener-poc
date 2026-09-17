# Resolve Action Workflow URNs by name at startup

**Date:** 2026-09-17
**Status:** approved, not implemented

## Problem

`action-route-workflow-decisions.yaml` keys its `workflows` table by Action
Workflow URN. One of those URNs is instance-specific:

```yaml
"urn:li:actionWorkflow:7110b19c-45b9-4450-b8bb-ba47d9f522ba": &dataset_access
```

Point the listener at a different DataHub deployment and that URN does not exist
there. The consequence is the bad kind: the `workflows` table is **both a filter
and a naming table**, so a decision from an unlisted workflow is *silently
ignored*. No error, no log line, no failed request — approvals simply stop having
effect.

### Why this URN is a GUID and the other is not

The other entry is `urn:li:actionWorkflow:propose-new-domain` — a **chosen slug**,
because this repo deploys that workflow itself via `upsertActionWorkflow`, which
accepts the URN as input. The GUID belongs to a workflow created through the
DataHub UI, which generates one.

So the root cause is not "we hardcoded a URN". It is **we do not own that
workflow's identity**. That distinction matters, because it means there are two
fixes, not one:

1. Resolve by name at runtime — works for workflows we do not control. **This spec.**
2. Own the URNs — redeploy workflows from this repo under chosen slugs, making
   them portable by construction. **Separate follow-on, see Out of scope.**

The owner chose both, sequenced: name resolution now, URN ownership over time.

### Scope of the portability gap

For context on what is *already* portable: the generator covers every
instance-specific reference inside the workflow definition JSON. Of its nine
URNs, eight are generated (four `level_2_*` conditions, four
`l1_gc_domain_code` condition values) and the ninth is the workflow's own slug.
Field ids, option lists and per-parent descriptions are generated too.

The routing YAML is the one place with an instance-specific reference that
nothing regenerates. Closing it removes the only *silent* failure in the
portability story. Two other gaps remain and are out of scope here: the two
`collibra.*` structured property URNs hardcoded in `properties.py` must be
provisioned on the target, and `QUERY_COUNT = 1000` caps the generator's domain
fetch.

## Design

### Where resolution happens

In `WorkflowRouterAction.create()`, after `_normalize` and **before** the email
validation. `create()` is already the startup-validation hook — it fails fast on
missing credentials, on a `notify_` route with no `RESEND_API_KEY`, and on a
missing `from_address`. Resolution belongs with them.

Ordering is deliberate: the email validation reports failures per workflow, so
resolving first means those messages name URNs consistently rather than a mix of
names and URNs.

`_normalize` stays pure. It is reached by tests through
`create.__func__.__globals__["_normalize"]`, and it has no business making
network calls. Resolution is a separate function:

```python
def _resolve_workflow_keys(client: DataHubClient, workflows: dict) -> dict
```

One small refactor is required: `DataHubClient(server=server, token=token)` is
currently constructed inline in the `cls(...)` return at `router.py:139`. Hoist
it above the workflow handling so resolution can reuse the same client.

### Config surface

No new config keys and no migration. A key beginning `urn:li:actionWorkflow:` is
used as-is; anything else is a name to resolve.

```yaml
workflows:
  "urn:li:actionWorkflow:propose-new-domain":   # slug we own -- unchanged
    label: Propose New Domain
    accepted: create_domain
    rejected: notify_rejection

  "Dataset Access Request":                      # UI-created -- resolved by name
    label: Dataset Access Request
    accepted: notify_approval
    rejected: notify_rejection
```

Existing URN-keyed configs keep working: a URN key is never rewritten. But it
**is** checked for existence — see below. That is what makes this safe to ship on
its own, ahead of any URN migration.

A side benefit: `_normalize`'s docstring records that live data really contains
`"urn:li:actionWorkflow: 1fc9daf6-..."` **with a space**, because that is the
string an operator pastes. Keying by name removes that class of paste error.

### URN keys are validated too

Resolving only name-keyed entries would half-solve the problem. Point a
URN-keyed config at a new deployment and the URN still will not exist there, and
the decision is still silently ignored — the exact failure this spec exists to
remove. So every key is checked against the live list, whether it is a name or a
URN. Names are resolved and rewritten; URNs are validated and left alone.

This is safe to adopt now: all three URNs in the shipped config resolve against
the current instance, verified today.

| Configured URN | Live | Name |
|---|---|---|
| `...:propose-new-domain` | present | Propose New Domain |
| `...:7110b19c-45b9-4450-b8bb-ba47d9f522ba` | present | Dataset Access Request |
| `...:zz-dataset-access-request-2` | present | Dataset Access Request-2 |

The consequence is that the lookup is unconditional — there is no
fully-URN-keyed fast path. One call at startup is a fair price for closing the
silent case.

### The client method

Add a minimal method to `DataHubClient`, beside `get_child_domains`:

```python
def list_action_workflows(self) -> list[dict]
```

Returns one dict per workflow with `urn` and `name` only, via:

```graphql
query listActionWorkflows {
  listActionWorkflows(input: {start: 0, count: 1000}) {
    workflows { urn name }
  }
}
```

**Deliberately not reusing `fetch_action_workflows.py`.** That script recursively
introspects the schema to build a maximal field selection, collapses 13 leaf
types, prunes at depth 6, and retries on `NullValueInNonNullableField`. It is the
right tool for capturing a full definition and the wrong tool for reading two
fields. It stays the capture and backup utility.

`count: 1000` matches the existing convention in `_CHILD_DOMAINS_QUERY`. Unlike
the generator's domain fetch, a short read here cannot invert the output — an
unresolvable name fails loudly rather than being read as a deletion — so this
does not need the `IncompleteFetch` treatment.

### Error handling

Fail fast, listing what is available. This mirrors `_narrow_to_level_2`, which
already reports stale dropdown options the same way.

Unknown name:

```
'Dataset Access Requst' is not an Action Workflow on this instance
(available: Dataset Access Request, Propose New Domain, Propose New Glossary Term).
Check the name in action-route-workflow-decisions.yaml, or use the URN directly.
```

Ambiguous name — two workflows share it, so the key cannot identify one:

```
2 Action Workflows are named 'Dynamic Domain Selection POC'
(urn:li:actionWorkflow:..., urn:li:actionWorkflow:...).
Use the URN directly for this entry.
```

**This guard is load-bearing, not defensive.** Duplicate names already exist on
the current instance: `Dynamic Domain Selection POC` appears twice and
`Data Product Access Request` appears twice, out of 17 workflows. Neither is in
the shipped config today, so nothing breaks — but it means name-keying is not
universally available, and an operator who picks one of those names needs the
URN form instead. The error has to say so.

A stale URN key, now that URNs are validated:

```
urn:li:actionWorkflow:7110b19c-... is not an Action Workflow on this instance.
If this config came from another deployment, key the entry by name instead.
```

Both raise `ValueError` from `create()`, consistent with the other startup
failures, so the pipeline refuses to start rather than failing on the first real
decision — which the framework would retry three times for a permanent
configuration error.

**Why fail fast rather than warn and continue:** an unresolved workflow is
exactly the silent-ignore failure this change exists to prevent. Continuing would
reintroduce it behind a startup warning somebody has to notice. The accepted cost
is that one bad name stops the whole listener, including workflows that resolve
fine.

### Observability

Log each resolution at INFO, before the existing routing table:

```
Resolved workflow 'Dataset Access Request' -> urn:li:actionWorkflow:7110b19c-...
```

The routing table printed by `__init__` then shows resolved URNs, so the startup
log answers "what did my config actually bind to" without a second lookup. Worth
having because `datahub-actions` output is already the only window into whether
the listener is healthy.

### Testing

`FakeClient` in `tests/test_behaviour.py` gains `list_action_workflows`, returning
a small fixed list. Five cases, all credential-free:

1. A name-keyed entry resolves to the matching URN.
2. An unknown name raises `ValueError` and the message lists available names.
3. A name matching two workflows raises `ValueError` naming both URNs.
4. A URN-keyed entry that exists is left byte-identical, not rewritten.
5. A URN-keyed entry that does not exist raises `ValueError`.
6. A config mixing both forms resolves the names and leaves the URNs alone.

Case 4 is the backward-compatibility guarantee: existing configs keep their keys.
Case 5 is the half-solved-problem guard — without it, a config carried to a new
deployment still fails silently, and the whole change would be cosmetic.

## Out of scope

**Owning the URNs.** The second half of the owner's decision — capture
UI-created workflows with `fetch_action_workflows.py`, commit them as definition
files under chosen slug URNs, deploy with `upsert_workflows.py`. That makes URNs
portable by construction and removes the need for runtime resolution entirely.

It is excluded here because it is an operational migration rather than a code
change: it needs a decision per workflow about whether this repo should own it, a
re-point of anything referencing the old GUID, and care about requests in flight
against the old URN. It gets its own spec.

Name resolution does not become redundant once that lands — it remains the answer
for workflows a customer creates in the UI and this repo does not own.

**Also out of scope:** the `collibra.*` structured property URNs in
`properties.py` (a provisioning prerequisite on any target, not a code change),
and the generator's 1000-domain fetch ceiling.

## Risks

**Names are less stable than URNs.** Renaming a workflow in the DataHub UI breaks
startup until the YAML is updated. This is the same trade accepted for the
`level_2_*` dropdowns and for `parent_domain` — names are what the mechanism
gives us. Fail-fast makes the breakage immediate and legible rather than silent,
which is the point.

**Name-keying is not available for every workflow.** Two names on the current
instance are already duplicated, so those entries must stay URN-keyed. The
feature is therefore a portability *option* per entry, not a blanket
replacement — and the docs should say so rather than implying every entry can be
converted.

**Validating URN keys is a behaviour change.** A config whose URN no longer
exists previously started and silently ignored that workflow; it will now refuse
to start. That is the intended fix, but it means an upgrade can surface a
pre-existing misconfiguration as a startup failure. Worth a line in the release
note.

**One extra API call at startup, unconditionally.** Since URN keys are validated
too, there is no fast path. Negligible against a pipeline that then polls
continuously.

**Search-index lag does not apply.** `listActionWorkflows` reads workflow
entities directly, not through the domain search index, so a workflow created
moments earlier is visible. This is unlike `get_root_domains` and
`get_child_domains`, which do lag.
