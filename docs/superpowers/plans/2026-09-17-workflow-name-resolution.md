# Workflow name resolution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let `action-route-workflow-decisions.yaml` key its `workflows` table by workflow **name** as well as URN, resolving names to URNs at listener startup and failing fast when any key does not match a live workflow.

**Architecture:** A new minimal `DataHubClient.list_action_workflows()` returns `{urn, name}` per workflow. A pure-ish `_resolve_workflow_keys(client, workflows)` in `router.py` rewrites name keys to URNs and validates URN keys, raising `ValueError` on anything unresolvable or ambiguous. `create()` calls it after `_normalize` and before the existing email validation. `_normalize` itself stays pure.

**Tech Stack:** Python 3.12, `requests`, pytest, PyYAML. DataHub Cloud GraphQL (`acryl-datahub-actions==1.7.0.7`).

**Spec:** [`docs/superpowers/specs/2026-09-17-workflow-name-resolution-design.md`](../specs/2026-09-17-workflow-name-resolution-design.md)

## Global Constraints

- Every key is checked, name or URN. Names are resolved and rewritten; URNs are validated and left byte-identical.
- Failure mode is fail-fast: raise `ValueError` from `create()`, listing available names. Never warn-and-continue — a key that matches nothing means decisions are silently ignored, which is the failure this change removes.
- `_normalize` must stay pure and keep its current signature. Tests reach it via `WorkflowRouterAction.create.__func__.__globals__["_normalize"]`.
- An empty `workflows` table means "log every workflow" and must short-circuit without an API call.
- Tests must not require credentials or network.
- URN prefix is exactly `urn:li:actionWorkflow:`.
- Only the GUID-keyed entry is converted in the YAML. Slug URNs we own (`propose-new-domain`, `zz-dataset-access-request-2`) stay URN-keyed — they are already portable, and converting them adds churn without benefit.

## Two non-obvious interactions

**1. The YAML entry carries an anchor.** Line 139 is
`"urn:li:actionWorkflow:7110b19c-...": &dataset_access`, and line 189 does
`<<: *dataset_access`. The anchor attaches to the *value*, not the key, so
changing the key preserves it — but do not drop `&dataset_access` while editing
the line, or the `zz-dataset-access-request-2` entry loses its whole body.

**2. Task 4 breaks the suite unless `build()` is updated in the same task.**
`tests/test_behaviour.py`'s `build()` helper bypasses `create()` and calls
`_normalize` directly. Once the YAML is name-keyed, `_normalize` yields
name-keyed entries, `act()` looks up by the event's `workflow_urn`, misses, and
every routing test fails with the workflow treated as unlisted. Task 4 therefore
updates `build()` to run resolution against a local stub. Do not split that.

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `src/actions/client.py` | Sole network boundary; all GraphQL | Modify — add `_ACTION_WORKFLOWS_QUERY` and `list_action_workflows()` |
| `src/actions/router.py` | Action entry point, config validation, dispatch | Modify — add `_resolve_workflow_keys()`, call it in `create()`, hoist client construction |
| `action-route-workflow-decisions.yaml` | Pipeline config | Modify — convert the GUID entry to name-keyed, update comments |
| `tests/test_behaviour.py` | Listener behaviour and config guards | Modify — resolution tests, update `build()` |
| `README.md` | Operator docs | Modify — document name-keying |

---

### Task 1: `DataHubClient.list_action_workflows()`

**Files:**
- Modify: `src/actions/client.py` — add `_ACTION_WORKFLOWS_QUERY` after `_CHILD_DOMAINS_QUERY` (ends line 141); add the method after `get_child_domains` (ends ~line 376)
- Test: `tests/test_behaviour.py`

**Interfaces:**
- Consumes: `DataHubClient.execute(query: str, variables: dict) -> dict`
- Produces: `DataHubClient.list_action_workflows() -> list[dict]`, each dict having `urn` and `name`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_behaviour.py`:

```python
# --------------------------------------------- workflow name resolution


def test_list_action_workflows_returns_urn_and_name(monkeypatch):
    """The router needs two fields to bind its config; nothing more.

    fetch_action_workflows.py exists for full definitions and gets there by
    recursively introspecting the schema. That is the wrong amount of machinery
    for a name lookup, so this is a separate, narrow query.
    """
    from actions.client import DataHubClient

    client = DataHubClient(server="https://example.invalid", token="token")
    monkeypatch.setattr(
        client,
        "execute",
        lambda query, variables: {
            "listActionWorkflows": {
                "workflows": [
                    {"urn": "urn:li:actionWorkflow:a", "name": "Alpha"},
                    {"urn": "urn:li:actionWorkflow:b", "name": "Beta"},
                ]
            }
        },
    )

    assert client.list_action_workflows() == [
        {"urn": "urn:li:actionWorkflow:a", "name": "Alpha"},
        {"urn": "urn:li:actionWorkflow:b", "name": "Beta"},
    ]


def test_list_action_workflows_tolerates_an_instance_with_none(monkeypatch):
    """An empty list is a valid answer, not an error.

    A fresh deployment has no workflows yet. Returning [] lets the resolver
    produce a useful "available: none" message instead of a KeyError.
    """
    from actions.client import DataHubClient

    client = DataHubClient(server="https://example.invalid", token="token")
    monkeypatch.setattr(
        client, "execute", lambda query, variables: {"listActionWorkflows": {"workflows": None}}
    )

    assert client.list_action_workflows() == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_behaviour.py -k list_action_workflows -v`
Expected: FAIL — `AttributeError: 'DataHubClient' object has no attribute 'list_action_workflows'`

- [ ] **Step 3: Write minimal implementation**

In `src/actions/client.py`, after `_CHILD_DOMAINS_QUERY` (line 141):

```python
_ACTION_WORKFLOWS_QUERY = """
query listActionWorkflows {
  listActionWorkflows(input: {start: 0, count: 1000}) {
    workflows {
      urn
      name
    }
  }
}
"""
```

And after `get_child_domains`:

```python
    def list_action_workflows(self) -> list[dict]:
        """Every Action Workflow on the instance as ``{urn, name}``.

        Two fields only: this exists so the router can bind config keys to live
        URNs, not to reproduce a definition. ``fetch_action_workflows.py`` is the
        tool for that, and it gets there by recursively introspecting the schema.

        Reads workflow entities directly rather than through the domain search
        index, so unlike ``get_child_domains`` this does not lag a recent write.

        ``count: 1000`` matches the convention in ``_CHILD_DOMAINS_QUERY``. A
        short read cannot invert the caller's decision the way a short *domain*
        fetch can -- a missing name fails loudly instead of being read as a
        deletion -- so this needs no ``IncompleteFetch`` equivalent.
        """
        result = self.execute(_ACTION_WORKFLOWS_QUERY, {}) or {}
        listing = result.get("listActionWorkflows") or {}
        return [w for w in (listing.get("workflows") or []) if w.get("urn")]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_behaviour.py -k list_action_workflows -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add src/actions/client.py tests/test_behaviour.py
git commit -m "feat: add client.list_action_workflows for config binding"
```

---

### Task 2: `_resolve_workflow_keys()`

**Files:**
- Modify: `src/actions/router.py` — add after `_normalize` (ends line 377)
- Test: `tests/test_behaviour.py`

**Interfaces:**
- Consumes: `DataHubClient.list_action_workflows()` from Task 1
- Produces: `_WORKFLOW_URN_PREFIX: str`, `_resolve_workflow_keys(client, workflows: dict) -> dict`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_behaviour.py`:

```python
class _WorkflowListing:
    """Minimal stand-in exposing only what the resolver calls.

    Deliberately not FakeClient: that models the listener's runtime reads, and
    resolution happens at startup before any event exists.
    """

    def __init__(self, pairs: list[tuple[str, str]]):
        self._pairs = pairs

    def list_action_workflows(self) -> list[dict]:
        return [{"urn": urn, "name": name} for urn, name in self._pairs]


def _resolver():
    return WorkflowRouterAction.create.__func__.__globals__["_resolve_workflow_keys"]


def test_a_name_key_resolves_to_the_live_urn():
    listing = _WorkflowListing([(DOMAIN_WF, "Propose New Domain")])
    resolved = _resolver()(listing, {"Propose New Domain": {"label": "x"}})
    assert list(resolved) == [DOMAIN_WF]
    assert resolved[DOMAIN_WF] == {"label": "x"}


def test_an_existing_urn_key_is_left_exactly_as_it_was():
    """The backward-compatibility guarantee: existing configs keep their keys."""
    listing = _WorkflowListing([(DOMAIN_WF, "Propose New Domain")])
    spec = {"label": "x"}
    resolved = _resolver()(listing, {DOMAIN_WF: spec})
    assert resolved == {DOMAIN_WF: spec}


def test_a_urn_that_does_not_exist_here_is_rejected():
    """Without this, a config carried to another deployment still fails silently.

    The workflows table is also a filter, so an unmatched key means decisions
    from that workflow do nothing -- no error, no log line. Resolving only names
    would leave that hole open for URN-keyed entries.
    """
    listing = _WorkflowListing([(DOMAIN_WF, "Propose New Domain")])
    with pytest.raises(ValueError, match="not an Action Workflow on this instance"):
        _resolver()(listing, {"urn:li:actionWorkflow:from-another-deployment": {"label": "x"}})


def test_an_unknown_name_lists_what_is_available():
    listing = _WorkflowListing([(DOMAIN_WF, "Propose New Domain")])
    with pytest.raises(ValueError, match="Propose New Domain"):
        _resolver()(listing, {"Propose New Domian": {"label": "x"}})


def test_a_duplicated_name_is_ambiguous_and_refused():
    """Not hypothetical: two names are already duplicated on the live instance.

    'Dynamic Domain Selection POC' and 'Data Product Access Request' each appear
    twice, so name-keying is a per-entry option rather than a blanket
    replacement. The error has to point at the URN form.
    """
    listing = _WorkflowListing([
        ("urn:li:actionWorkflow:one", "Dataset Access Request"),
        ("urn:li:actionWorkflow:two", "Dataset Access Request"),
    ])
    with pytest.raises(ValueError, match="2 Action Workflows are named"):
        _resolver()(listing, {"Dataset Access Request": {"label": "x"}})


def test_naming_the_same_workflow_twice_is_refused():
    """A name and its own URN both present would silently drop one entry."""
    listing = _WorkflowListing([(DOMAIN_WF, "Propose New Domain")])
    with pytest.raises(ValueError, match="twice"):
        _resolver()(listing, {
            DOMAIN_WF: {"label": "by urn"},
            "Propose New Domain": {"label": "by name"},
        })


def test_an_empty_table_short_circuits_without_a_lookup():
    """No workflows means log everything, so there is nothing to bind."""

    class Exploding:
        def list_action_workflows(self):
            raise AssertionError("must not call the API for an empty table")

    assert _resolver()(Exploding(), {}) == {}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_behaviour.py -k "resolve or name_key or urn_key or empty_table or ambiguous" -v`
Expected: FAIL — `KeyError: '_resolve_workflow_keys'`

- [ ] **Step 3: Write minimal implementation**

In `src/actions/router.py`, after `_normalize`:

```python
_WORKFLOW_URN_PREFIX = "urn:li:actionWorkflow:"


def _resolve_workflow_keys(client: DataHubClient, workflows: dict) -> dict:
    """Bind every configured key to a live Action Workflow URN.

    A key already shaped like a URN is validated and kept as-is; anything else is
    a workflow NAME and is resolved. Both directions fail fast, because the
    ``workflows`` table is also a FILTER -- a key matching nothing means
    decisions from that workflow are silently ignored, with no error anywhere.
    That silence is the whole reason this function exists, so it must not be
    reintroduced as a warning.

    URN keys are validated too, not just names. Resolving only names would leave
    the hole open for any config carried from another deployment.

    An empty table means "log every workflow", so it short-circuits rather than
    spending a call to bind nothing.
    """
    if not workflows:
        return workflows

    live = client.list_action_workflows()
    urns = {w["urn"] for w in live}

    by_name: dict[str, list[str]] = {}
    for workflow in live:
        name = (workflow.get("name") or "").strip()
        if name:
            by_name.setdefault(name, []).append(workflow["urn"])

    resolved: dict[str, dict] = {}
    for key, spec in workflows.items():
        if key.startswith(_WORKFLOW_URN_PREFIX):
            if key not in urns:
                raise ValueError(
                    f"{key} is not an Action Workflow on this instance. If this "
                    f"config came from another deployment, key the entry by name "
                    f"instead."
                )
            target = key
        else:
            matches = by_name.get(key) or []
            if not matches:
                available = ", ".join(sorted(by_name)) or "none"
                raise ValueError(
                    f"{key!r} is not an Action Workflow on this instance "
                    f"(available: {available}). Check the name in "
                    f"action-route-workflow-decisions.yaml, or use the URN directly."
                )
            if len(matches) > 1:
                raise ValueError(
                    f"{len(matches)} Action Workflows are named {key!r} "
                    f"({', '.join(sorted(matches))}). Use the URN directly for "
                    f"this entry."
                )
            target = matches[0]
            logger.info("Resolved workflow %r -> %s", key, target)

        if target in resolved:
            # Two keys naming one workflow: dict assignment would drop one spec
            # and the operator would never know which routing won.
            raise ValueError(
                f"{target} is configured twice, once by URN and once by name. "
                f"Keep one entry."
            )
        resolved[target] = spec

    return resolved
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_behaviour.py -v`
Expected: PASS — all previously-passing tests plus the 7 new ones

- [ ] **Step 5: Commit**

```bash
git add src/actions/router.py tests/test_behaviour.py
git commit -m "feat: resolve workflow config keys by name, validating urns too"
```

---

### Task 3: Wire resolution into `create()`

**Files:**
- Modify: `src/actions/router.py:103` (client hoist + resolution), `src/actions/router.py:139-141` (reuse the hoisted client)
- Test: `tests/test_behaviour.py`

**Interfaces:**
- Consumes: `_resolve_workflow_keys` from Task 2, `_normalize` (unchanged)
- Produces: no new names; `create()` now raises `ValueError` for an unresolvable key

- [ ] **Step 1: Write the failing test**

Append to `tests/test_behaviour.py`:

```python
def test_create_refuses_to_start_on_an_unresolvable_workflow(monkeypatch):
    """Fail at startup, not on the first real decision.

    The framework retries an action three times, so a permanent config error
    surfaces as three identical failures while the person waiting on the
    approval gets nothing. create() already fails fast on missing credentials
    and on a notify_ route with no RESEND_API_KEY; this joins them.
    """
    import actions.router as router_mod

    monkeypatch.setenv("DATAHUB_GMS_URL", "https://example.invalid")
    monkeypatch.setenv("DATAHUB_GMS_TOKEN", "token")
    monkeypatch.setattr(
        router_mod,
        "DataHubClient",
        lambda server, token: _WorkflowListing([(DOMAIN_WF, "Propose New Domain")]),
    )

    with pytest.raises(ValueError, match="No Such Workflow"):
        router_mod.WorkflowRouterAction.create(
            {"workflows": {"No Such Workflow": "just a label"}}, ctx=None
        )


def test_create_binds_a_name_keyed_config_to_urns(monkeypatch):
    """The routing table the listener runs on must be urn-keyed.

    act() looks up the spec by the event's workflowUrn, so a name left in place
    would miss and the workflow would be treated as unlisted -- i.e. ignored.
    """
    import actions.router as router_mod

    monkeypatch.setenv("DATAHUB_GMS_URL", "https://example.invalid")
    monkeypatch.setenv("DATAHUB_GMS_TOKEN", "token")
    monkeypatch.setattr(
        router_mod,
        "DataHubClient",
        lambda server, token: _WorkflowListing([(DOMAIN_WF, "Propose New Domain")]),
    )

    action = router_mod.WorkflowRouterAction.create(
        {"workflows": {"Propose New Domain": "just a label"}}, ctx=None
    )
    assert list(action.workflows) == [DOMAIN_WF]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_behaviour.py -k create_refuses -v`
Expected: FAIL — no exception raised, because `create()` does not resolve yet. The second test fails with `list(action.workflows) == ['Propose New Domain']`.

- [ ] **Step 3: Write minimal implementation**

Replace `src/actions/router.py:103`:

```python
        workflows = _normalize(config.get("workflows") or {})
```

with the client hoist plus resolution:

```python
        # Hoisted out of the cls(...) return below so resolution can reuse it.
        client = DataHubClient(server=server, token=token)

        # Before the email validation, so those messages report resolved URNs
        # rather than a mix of names and URNs.
        workflows = _resolve_workflow_keys(client, _normalize(config.get("workflows") or {}))
```

Then change the return at lines 139-141 to reuse it:

```python
        return cls(client=client,
                   workflows=workflows, notify=notify, ctx=ctx,
                   log_progress=log_progress)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_behaviour.py -v`
Expected: PASS — all tests including the two new ones

- [ ] **Step 5: Commit**

```bash
git add src/actions/router.py tests/test_behaviour.py
git commit -m "feat: resolve workflow keys at startup in create()"
```

---

### Task 4: Convert the GUID entry and document it

**Files:**
- Modify: `action-route-workflow-decisions.yaml:112-139` (comment block and the GUID key)
- Modify: `tests/test_behaviour.py` — `build()` helper (~line 154)
- Modify: `README.md` — the Layout or Before-you-run section
- Test: `tests/test_behaviour.py`

**Interfaces:**
- Consumes: `_resolve_workflow_keys` from Task 2
- Produces: no new names

**Read the two non-obvious interactions at the top of this plan before starting.** The YAML anchor must survive, and `build()` must be updated in this same task or every routing test fails.

- [ ] **Step 1: Update `build()` so the suite can resolve offline**

In `tests/test_behaviour.py`, replace the `build()` helper:

```python
def build(request: dict, exists: bool = False) -> tuple[WorkflowRouterAction, FakeClient]:
    config = recipe_config()
    client = FakeClient(request, exists)
    globals_ = WorkflowRouterAction.create.__func__.__globals__
    # The shipped recipe keys one entry by NAME, so the helper has to resolve the
    # same way create() does -- act() looks specs up by urn. Without this every
    # routing test would silently treat that workflow as unlisted.
    workflows = globals_["_resolve_workflow_keys"](
        _WorkflowListing([
            (DOMAIN_WF, "Propose New Domain"),
            (ACCESS_WF, "Dataset Access Request"),
            ("urn:li:actionWorkflow:zz-dataset-access-request-2", "Dataset Access Request-2"),
        ]),
        globals_["_normalize"](config["workflows"]),
    )
    action = WorkflowRouterAction(
        client=client,
        workflows=workflows,
        notify=config.get("notify") or {},
        ctx=None,
    )
    return action, client
```

- [ ] **Step 2: Run the suite to confirm it is green before the YAML changes**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS. The helper now resolves a still-URN-keyed config, which is a no-op, so nothing should change yet.

- [ ] **Step 3: Convert the GUID key in the YAML**

In `action-route-workflow-decisions.yaml`, replace line 139's key while keeping the anchor:

```yaml
      "Dataset Access Request": &dataset_access
```

Then replace the comment block at lines 112-116 with:

```yaml
    # BOTH a filter and a naming table -- a decision from a workflow not listed
    # here is ignored. Comment the whole block out to log every workflow, which
    # is what you want before you know what exists.
    #
    # Keys may be a workflow URN or a workflow NAME. Names are resolved to URNs
    # at startup and are the portable choice: a URN created through the DataHub
    # UI is a per-instance GUID, so a URN-keyed entry does not survive being
    # pointed at another deployment. URNs this repo deploys itself are slugs we
    # choose, so those stay URN-keyed below.
    #
    # Every key is checked at startup, name or URN. An unknown or ambiguous key
    # stops the listener rather than being ignored -- being ignored is silent,
    # and that is the failure worth refusing.
    #
    # Two live workflows currently share a name, so name-keying is not available
    # for every entry; use the URN for those.
    #
    # Discover both with:
    #   .venv/bin/python workflows/definitions/fetch_action_workflows.py
```

- [ ] **Step 4: Run the suite and confirm the YAML still parses**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS. Then confirm the anchor survived, since a broken merge key would leave the second entry without routing:

```bash
.venv/bin/python -c "
import os, yaml
from expandvars import expandvars
for k, v in {'DATAHUB_GMS_URL':'https://x.invalid','DATAHUB_GMS_TOKEN':'t','FAILED_EVENTS_DIR':'/tmp/x'}.items():
    os.environ.setdefault(k, v)
wf = yaml.safe_load(expandvars(open('action-route-workflow-decisions.yaml').read()))['action']['config']['workflows']
for key, spec in wf.items():
    print(f'{key!r:60} accepted={spec.get(\"accepted\")} rejected={spec.get(\"rejected\")}')
"
```

Expected: three entries — `Dataset Access Request` keyed by name, the two slug URNs unchanged, and the merged entry still carrying `accepted`/`rejected` from the anchor.

- [ ] **Step 5: Document it in the README**

Add to `README.md`, immediately before the `## Known upstream issues` heading:

```markdown
## Pointing at another deployment

`generate_form_fields.py --write` regenerates every instance-specific reference
inside the form definition — the `level_2_*` conditions and options, the
`l1_gc_domain_code` values, and the field ids derived from domain names.

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
```

- [ ] **Step 6: Commit**

```bash
git add action-route-workflow-decisions.yaml tests/test_behaviour.py README.md
git commit -m "feat: key the UI-created workflow by name for portability"
```

---

### Task 5: Verify against the live instance

**Files:** none — verification only

**Interfaces:**
- Consumes: everything from Tasks 1-4

- [ ] **Step 1: Confirm the config binds against the real instance**

```bash
set -a && . ./.env && set +a
.venv/bin/python -c "
import os, yaml
from expandvars import expandvars
from actions.client import DataHubClient
from actions.router import WorkflowRouterAction, _normalize, _resolve_workflow_keys

cfg = yaml.safe_load(expandvars(open('action-route-workflow-decisions.yaml').read()))['action']['config']
client = DataHubClient(server=os.environ['DATAHUB_GMS_URL'], token=os.environ['DATAHUB_GMS_TOKEN'])
resolved = _resolve_workflow_keys(client, _normalize(cfg['workflows']))
for urn, spec in sorted(resolved.items()):
    print(f'{urn}\n    {spec[\"label\"]}  ACCEPTED={spec[\"ACCEPTED\"]} REJECTED={spec[\"REJECTED\"]}')
"
```

Expected: three URN-keyed entries, including `urn:li:actionWorkflow:7110b19c-45b9-4450-b8bb-ba47d9f522ba` resolved from the name, plus a `Resolved workflow 'Dataset Access Request' -> ...` log line.

- [ ] **Step 2: Confirm the listener starts**

```bash
set -a && . ./.env && set +a
timeout 25 .venv/bin/datahub-actions actions run -c action-route-workflow-decisions.yaml 2>&1 | head -20
```

Expected: the `Resolved workflow ...` line, then one `Routing <urn> (<label>) on ACCEPTED -> ..., REJECTED -> ...` line per workflow, then `Action Pipeline ... is now running.` The `timeout` is intentional — this is a startup check, not a soak.

- [ ] **Step 3: Confirm a bad name actually stops it**

```bash
set -a && . ./.env && set +a
.venv/bin/python -c "
import os
from actions.client import DataHubClient
from actions.router import _normalize, _resolve_workflow_keys
client = DataHubClient(server=os.environ['DATAHUB_GMS_URL'], token=os.environ['DATAHUB_GMS_TOKEN'])
try:
    _resolve_workflow_keys(client, _normalize({'Dataset Access Requst': 'typo'}))
except ValueError as exc:
    print('correctly refused:'); print(exc)
"
```

Expected: the refusal, listing the real workflow names. This is the failure the change exists to make loud, so see it once rather than trusting it.

- [ ] **Step 4: Commit nothing**

Verification only. If anything here fails, return to the task that owns it.

---

## Self-Review

**Spec coverage:**

| Spec section | Task |
|---|---|
| Where resolution happens (`create()`, after `_normalize`, before email validation) | 3 |
| `_normalize` stays pure | 2, 3 — resolution is a separate function |
| Client hoist out of the `cls(...)` return | 3 |
| Config surface: URN or name keys | 2, 4 |
| URN keys validated too | 2 (test), 2 (implementation) |
| `list_action_workflows()`, urn+name only | 1 |
| Not reusing `fetch_action_workflows.py` | 1 — docstring records why |
| Fail-fast errors listing available names | 2 |
| Ambiguous-name refusal | 2 |
| Stale-URN refusal | 2 |
| Observability: log `name -> urn` | 2 |
| Empty table short-circuits | 2 |
| Six test cases | 1 (2) + 2 (7) + 3 (2) |
| Docs | 4 |

No spec requirement is unassigned. Two items go beyond the spec and are
deliberate: the duplicate-key refusal in Task 2 (a name and its own URN both
present would silently drop one spec), and Task 5's live verification.

**Placeholder scan:** No TBDs. Every code step carries real code. Task 5 has no
implementation because it is verification, and says so.

**Type consistency:** `list_action_workflows() -> list[dict]` defined in Task 1,
called in Task 2, stubbed by `_WorkflowListing` in Tasks 2-4.
`_resolve_workflow_keys(client, workflows) -> dict` defined in Task 2, called in
Task 3's `create()` and Task 4's `build()`. `_WorkflowListing` is defined once in
Task 2 and reused in Tasks 3-4 — it must land in Task 2 or those tasks will not
run. `DOMAIN_WF` and `ACCESS_WF` are existing module constants.

**Known gaps, deliberate:**
- Renaming a workflow in the UI breaks startup until the YAML is updated. Names
  are less stable than URNs; fail-fast makes that immediate rather than silent.
- Validating URN keys is a behaviour change: a config whose URN no longer exists
  previously started and ignored that workflow, and will now refuse to start.
  That is the intended fix, but an upgrade can surface a pre-existing
  misconfiguration as a startup failure.
- Owning URNs via chosen slugs is out of scope — a separate operational
  migration, and name resolution stays useful regardless for UI-created
  workflows.
