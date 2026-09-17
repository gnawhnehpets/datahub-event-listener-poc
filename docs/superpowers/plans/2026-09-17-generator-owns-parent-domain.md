# Generator owns `parent_domain` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `generate_form_fields.py` own `parent_domain`, converting it from an unconstrained URN entity picker into a generated `STRING` dropdown of root (Level 1) domain names, so a requester can no longer select a Level 2 or Level 3 parent.

**Architecture:** DataHub offers no way to constrain a URN entity picker (proven — see §Background). The only mechanism that works is `valueType: STRING` with a generated `allowedValues` list, which is exactly how `level_2_*` already works. Converting `parent_domain` to that shape forces three follow-on changes: the five conditions keyed on `parent_domain` must compare **names** rather than URNs, the listener must resolve a name back to a root URN, and the generator gains a duplicate-root-name guard mirroring its existing sibling guard.

**Tech Stack:** Python 3.12, `requests`, pytest. DataHub Cloud GraphQL (`acryl-datahub-actions==1.7.0.7`, GMS v2.1.0-cloud).

## Global Constraints

- `MAX_DEPTH = 3` lives in `src/actions/hierarchy.py`. Never duplicate the rule; call `level_for` / `_can_take_a_child`.
- Generated output must be deterministic: sort every list so re-running produces no diff.
- `--write` must refuse rather than emit a form whose submissions fail after approval. Both existing refusals (removals, sibling name collisions) stay; one is added.
- Tests must not require credentials. Use `DomainNode` fixtures, not live fetches.
- `DomainNode(urn, name, codes, ancestors)` — `ancestors` is immediate-parent-first.
- The listener only sees `COMPLETED` events, so nothing here can fail earlier than the third approval. Prevention at the form is the only lever.
- Do not deploy from a task. Deployment is Task 7, explicitly gated.

## Background: why `STRING` and not a filter

Four mechanisms were tested against the live instance. All rejected:

| Attempt | Server response |
|---|---|
| `filterCondition` field `parentDomain` | `Criterion.field 'parentDomain' is neither a 'formField:<id>' reference nor a known launching-entity attribute [...]` |
| `filterCondition` field `hasParentDomain` | same allowlist rejection |
| `allowedValues` on the URN field | `URN-typed fields cannot use allowedValues — the picker is not value-scoped. Express the curated URN list as a dynamicSource with resolver=LITERAL_ENTITIES` |
| that `dynamicSource` | `Unknown resolver: LITERAL_ENTITIES` — the recommended resolver does not exist |

`filterCondition` is separately not a candidate filter at all: it governs field *visibility*, and the server derives it from `condition`. `STRING` + `allowedValues` **was** accepted, including with conditions rewritten to names.

## Known blocker (affects Task 7 only)

`--write` currently refuses against the live tenant:

```
REFUSING to write: sibling domains share a name, so the submitted value
cannot identify one -- 'Data Contracts' has children sharing a name ['Test2']
```

Two children of `Data Contracts` are both named `Test2`. **Tasks 1-6 are unaffected** — they are unit-tested against fixtures. Task 7 cannot complete until someone renames one of those domains in DataHub.

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `workflows/definitions/generate_form_fields.py` | Derive and write all generated form fields | Modify — add root-domain derivation, `parent_domain` builder, name-keyed conditions, duplicate-root guard |
| `src/actions/client.py` | Sole network boundary; all GraphQL | Modify — add `get_root_domains()` |
| `src/actions/domain.py` | `create_domain` action | Modify — resolve `parent_domain` name to a root URN |
| `tests/test_generator.py` | Unit tests for pure generator functions | **Create** — the generator currently has no tests |
| `tests/test_behaviour.py` | Existing listener + definition guards | Modify — update `parent_domain` guards |

`tests/test_generator.py` is new because `test_behaviour.py` is explicitly about the listener's four behaviour cells and takes no credentials; generator derivation is a different concern and belongs in its own file.

---

### Task 1: Derive root-domain options

**Files:**
- Modify: `workflows/definitions/generate_form_fields.py` (after `selectable_parents`, ~line 163)
- Test: `tests/test_generator.py` (create)

**Interfaces:**
- Consumes: `DomainNode`, `_can_take_a_child` (already in module)
- Produces: `PARENT_FIELD: str`, `root_domains(nodes) -> list[DomainNode]`, `parent_field_values(nodes) -> tuple[list[str], list[str]]` returning `(sorted_names, warnings)`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_generator.py
"""Unit tests for the pure derivation in generate_form_fields.py.

No credentials and no network: every test builds DomainNode fixtures directly,
which is what makes the depth and name rules cheap to pin down.
"""

from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "workflows" / "definitions"))

from actions.hierarchy import DomainNode  # noqa: E402

import generate_form_fields as g  # noqa: E402


def node(urn: str, name: str, codes=None, ancestors=None) -> DomainNode:
    return DomainNode(urn=urn, name=name, codes=codes or [], ancestors=ancestors or [])


def tree(*nodes: DomainNode) -> dict[str, DomainNode]:
    return {n.urn: n for n in nodes}


def test_root_domains_excludes_every_non_root():
    nodes = tree(
        node("urn:li:domain:a", "Alpha"),
        node("urn:li:domain:b", "Beta", ancestors=["urn:li:domain:a"]),
        node("urn:li:domain:c", "Gamma", ancestors=["urn:li:domain:b", "urn:li:domain:a"]),
    )
    assert [n.name for n in g.root_domains(nodes)] == ["Alpha"]


def test_parent_field_values_returns_sorted_names_and_no_warning():
    nodes = tree(node("urn:li:domain:z", "Zulu"), node("urn:li:domain:a", "Alpha"))
    names, warnings = g.parent_field_values(nodes)
    assert names == ["Alpha", "Zulu"]
    assert warnings == []


def test_parent_field_values_warns_when_two_roots_share_a_name():
    nodes = tree(node("urn:li:domain:1", "Finance"), node("urn:li:domain:2", "Finance"))
    names, warnings = g.parent_field_values(nodes)
    assert names == ["Finance"]
    assert len(warnings) == 1
    assert "Finance" in warnings[0]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_generator.py -v`
Expected: FAIL — `AttributeError: module 'generate_form_fields' has no attribute 'root_domains'`

- [ ] **Step 3: Write minimal implementation**

Insert after `selectable_parents` (about line 163):

```python
PARENT_FIELD = "parent_domain"


def root_domains(nodes: dict[str, DomainNode]) -> list[DomainNode]:
    """Domains offerable as ``parent_domain``: roots that can still take a child.

    Only roots, because the Level 2 and Level 3 tiers are reached through the
    ``level_2_*`` dropdowns instead. Offering a deeper domain here produced a
    proposal the listener rejected for depth -- after all three approvals -- or,
    worse, one that succeeded while bypassing the narrowing dropdowns entirely.
    """
    return sorted(
        (n for n in nodes.values() if not n.ancestors and _can_take_a_child(n)),
        key=lambda n: n.name,
    )


def parent_field_values(nodes: dict[str, DomainNode]) -> tuple[list[str], list[str]]:
    """Return (allowedValues names, warnings) for ``parent_domain``.

    Names rather than URNs because ``allowedValues`` is rejected outright on a
    URN-typed field: "the picker is not value-scoped". A STRING dropdown is the
    only shape this instance accepts, which is why ``level_2_*`` already works
    this way.

    Root names must be globally unique for the submitted value to identify one
    domain -- a stricter requirement than the sibling-scoped uniqueness the
    Level 2 dropdowns need, because this field is scoped to nothing.
    """
    roots = root_domains(nodes)
    names = [n.name for n in roots]
    dupes = sorted({n for n in names if names.count(n) > 1})
    warnings = [
        f"root domains share the name {d!r}; the submitted value would be ambiguous"
        for d in dupes
    ]
    return sorted(set(names)), warnings
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_generator.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add tests/test_generator.py workflows/definitions/generate_form_fields.py
git commit -m "feat: derive root-domain options for parent_domain"
```

---

### Task 2: Build the `parent_domain` field

**Files:**
- Modify: `workflows/definitions/generate_form_fields.py` (after `parent_field_values`)
- Test: `tests/test_generator.py`

**Interfaces:**
- Consumes: `parent_field_values` from Task 1
- Produces: `build_parent_field(existing: dict, names: list[str]) -> dict`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_generator.py`:

```python
def test_build_parent_field_converts_urn_picker_to_string_dropdown():
    existing = {
        "id": "parent_domain",
        "name": "Parent domain",
        "description": "hand-written wording that must survive",
        "valueType": "URN",
        "allowedEntityTypes": ["DOMAIN"],
        "allowedValues": None,
        "cardinality": "SINGLE",
        "required": False,
        "condition": None,
        "valuesSource": None,
    }
    field = g.build_parent_field(existing, ["Alpha", "Beta"])

    assert field["valueType"] == "STRING"
    assert field["allowedEntityTypes"] is None
    assert field["allowedValues"] == [{"stringValue": "Alpha"}, {"stringValue": "Beta"}]
    # hand-written wording and the optionality are not the generator's business
    assert field["description"] == "hand-written wording that must survive"
    assert field["required"] is False
    assert existing["valueType"] == "URN", "must not mutate the input"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_generator.py::test_build_parent_field_converts_urn_picker_to_string_dropdown -v`
Expected: FAIL — `AttributeError: ... has no attribute 'build_parent_field'`

- [ ] **Step 3: Write minimal implementation**

```python
def build_parent_field(existing: dict, names: list[str]) -> dict:
    """Patch only the generated parts, preserving hand-written wording.

    The type change is the point: a URN picker cannot be narrowed on this
    instance, so the field becomes a STRING dropdown over root names.
    ``allowedEntityTypes`` is cleared because it is meaningless once the field
    no longer holds a URN, and leaving it set invites the reader to think the
    picker is still entity-backed.
    """
    field = json.loads(json.dumps(existing))
    field["valueType"] = "STRING"
    field["allowedEntityTypes"] = None
    field["allowedValues"] = [{"stringValue": n} for n in names]
    return field
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_generator.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add tests/test_generator.py workflows/definitions/generate_form_fields.py
git commit -m "feat: build parent_domain as a generated STRING dropdown"
```

---

### Task 3: Key the dependent conditions on names

**Files:**
- Modify: `workflows/definitions/generate_form_fields.py` — `code_field_values` (~line 165), `build_code_field` (~line 187), `level_2_fields` (~line 292)
- Test: `tests/test_generator.py`

**Interfaces:**
- Consumes: `root_domains` from Task 1
- Produces: `code_field_values(nodes) -> tuple[list[str], list[str]]` — second element becomes **names**; `build_code_field(existing, values, names)` — third parameter renamed from `urns` to `names`

**Why:** five fields gate on `parent_domain` — four `level_2_*` plus `l1_gc_domain_code`. Each compares against URNs today. Once the field holds a name, a URN comparison can never match, so every dropdown silently stops appearing. The server **accepts** this broken half-migration, so nothing but a test catches it.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_generator.py`:

```python
def test_code_field_condition_uses_root_names_not_urns():
    nodes = tree(
        node("urn:li:domain:rc", "Risk & Compliance", codes=["GC001", "GC002"]),
        node("urn:li:domain:kid", "Finance", ancestors=["urn:li:domain:rc"]),
    )
    values, names = g.code_field_values(nodes)

    assert values == ["GC001", "GC002"]
    assert names == ["Risk & Compliance"], "condition must name the root, not its urn"
    assert not any(n.startswith("urn:") for n in names)


def test_level_2_condition_uses_parent_name_not_urn():
    nodes = tree(
        node("urn:li:domain:rc", "Risk & Compliance"),
        node("urn:li:domain:fin", "Finance", ancestors=["urn:li:domain:rc"]),
    )
    fields, warnings = g.level_2_fields(nodes)

    assert warnings == []
    assert len(fields) == 1
    condition = fields[0]["condition"]["singleFieldValueCondition"]
    assert condition["field"] == "parent_domain"
    assert condition["values"] == ["Risk & Compliance"]


def test_build_code_field_writes_names_into_the_condition():
    existing = {"id": "l1_gc_domain_code", "description": "keep me",
                "allowedValues": None, "condition": None}
    field = g.build_code_field(existing, ["GC001"], ["Risk & Compliance"])
    assert field["condition"]["singleFieldValueCondition"]["values"] == ["Risk & Compliance"]
    assert field["description"] == "keep me"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_generator.py -v -k "condition or names"`
Expected: FAIL — `assert ['urn:li:domain:rc'] == ['Risk & Compliance']`

- [ ] **Step 3: Write minimal implementation**

Replace the body of `code_field_values` (keep the docstring, add the final paragraph):

```python
def code_field_values(nodes: dict[str, DomainNode]) -> tuple[list[str], list[str]]:
    """Return (allowedValues, condition names) for the GC code field.

    The field is needed whenever the *derived Level 1* carries more than one
    code, which is a property of the ancestor, not of the selected parent.

    allowedValues is a single flat list, so when several distinct Level 1s are
    ambiguous it is their union and therefore over-offers. That is safe: the
    listener re-checks the choice against the selected parent's own codes and
    rejects a mismatch by name.

    Only roots are considered, because ``parent_domain`` now offers only roots.
    A deeper domain's name in this condition could never match the submitted
    value, so including it would be dead weight that reads like coverage.
    """
    needs_field: list[str] = []
    codes: set[str] = set()
    for node in root_domains(nodes):
        level_1 = derived_level_1(node, nodes)
        if len(level_1.codes) > 1:
            needs_field.append(node.name)
            codes.update(level_1.codes)
    return sorted(codes), sorted(set(needs_field))
```

In `build_code_field`, rename the third parameter and the value it writes:

```python
def build_code_field(existing: dict, values: list[str], names: list[str]) -> dict:
    """Patch only the generated parts, preserving hand-written wording.

    ``names`` not URNs: ``parent_domain`` is a STRING dropdown of root names, so
    the condition has to compare like for like.
    """
    field = json.loads(json.dumps(existing))
    field["allowedValues"] = [{"stringValue": v} for v in values]
    field["condition"] = {
        "type": "SINGLE_FIELD_VALUE",
        "singleFieldValueCondition": {
            "field": "parent_domain",
            "values": names,
            "condition": "EQUAL",
            "negated": False,
        },
    }
    return field
```

In `level_2_fields`, change the condition values (about line 292) from the URN to the name:

```python
            "condition": {
                "type": "SINGLE_FIELD_VALUE",
                "singleFieldValueCondition": {
                    "field": "parent_domain",
                    # The name, not the urn: parent_domain is a STRING dropdown.
                    # A urn here silently never matches, so the dropdown would
                    # never render and the form would look merely incomplete.
                    "values": [parent.name],
                    "condition": "EQUAL",
                    "negated": False,
                },
            },
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_generator.py -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Commit**

```bash
git add tests/test_generator.py workflows/definitions/generate_form_fields.py
git commit -m "fix: key parent_domain conditions on names, not urns"
```

---

### Task 4: Report `parent_domain` drift and refuse ambiguous roots

**Files:**
- Modify: `workflows/definitions/generate_form_fields.py` — `main()` (~line 350-490)
- Test: `tests/test_generator.py`

**Interfaces:**
- Consumes: `parent_field_values`, `build_parent_field` from Tasks 1-2
- Produces: no new public names; `main()` gains a `parent_domain` diff, a duplicate-root refusal, and a `--write` branch that patches the field

- [ ] **Step 1: Write the failing test**

Append to `tests/test_generator.py`:

```python
def test_duplicate_root_names_are_refused_not_emitted():
    """Emitting them yields a form whose submission cannot identify a domain.

    Mirrors the existing sibling-collision refusal. The failure it prevents is
    post-approval: the handler cannot tell which root was meant, so the request
    dies after three gates rather than at submission.
    """
    nodes = tree(node("urn:li:domain:1", "Finance"), node("urn:li:domain:2", "Finance"))
    _, warnings = g.parent_field_values(nodes)
    assert warnings, "duplicate root names must produce a warning main() can refuse on"


def test_parent_options_are_deterministic():
    """Re-running must not produce a diff, or --check reports phantom drift."""
    nodes = tree(node("urn:li:domain:b", "Beta"), node("urn:li:domain:a", "Alpha"))
    assert g.parent_field_values(nodes)[0] == g.parent_field_values(nodes)[0] == ["Alpha", "Beta"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_generator.py -v -k "duplicate_root or deterministic"`
Expected: PASS for `deterministic`, PASS for `duplicate_root` — **these pass on Task 1's code.** They exist to pin the contract `main()` depends on. If both pass, proceed; the failing part of this task is the `main()` wiring, verified by Step 4's manual run.

- [ ] **Step 3: Write the implementation**

In `main()`, immediately after `values, urns = code_field_values(nodes)` (line 371), rename and add:

```python
    values, names = code_field_values(nodes)
    parent_names, parent_warnings = parent_field_values(nodes)
```

After the code-field diff block and before `level_2, l2_warnings = level_2_fields(nodes)` (line 405), insert the `parent_domain` report:

```python
    parent_index = next(i for i, f in enumerate(fields) if f["id"] == PARENT_FIELD)
    parent_current = fields[parent_index]
    current_parents = [v["stringValue"] for v in parent_current.get("allowedValues") or []]

    added_p = sorted(set(parent_names) - set(current_parents))
    removed_p = sorted(set(current_parents) - set(parent_names))
    parent_stale = bool(added_p or removed_p) or parent_current.get("valueType") != "STRING"

    print(f"\n{PARENT_FIELD}.allowedValues ({len(current_parents)} committed, "
          f"{len(parent_names)} live)")
    for p in added_p:
        print(f"  + {p}   a root domain nobody can currently propose under")
    for p in removed_p:
        print(f"  - {p}   no longer a root, or gone from the instance")
    for w in parent_warnings:
        print(f"  WARNING: {w}")
    if parent_current.get("valueType") != "STRING":
        print(f"  valueType is {parent_current.get('valueType')!r}, not 'STRING' -- "
              f"the picker is unconstrained and offers every depth")
```

Fold `parent_stale` into the existing staleness flag. Replace `stale = bool(added_v or removed_v or added_u or removed_u)` at line 385 with the code-field-only version, then after the parent block add:

```python
    stale = stale or parent_stale
```

Add the refusal alongside the existing `l2_warnings` refusal (after line 478):

```python
        if parent_warnings:
            print("\nREFUSING to write: root domains share a name, so the "
                  "submitted value cannot identify one --", file=sys.stderr)
            for w in parent_warnings:
                print(f"  - {w}", file=sys.stderr)
            print("Rename the duplicates in DataHub, then re-run.", file=sys.stderr)
            return 1
```

In the `--write` block, patch the parent field. Order matters: `apply_level_2_fields` re-indexes the list, so resolve `parent_index` **before** it and write **after** both index lookups:

```python
        fields[index] = build_code_field(current, values, names)
        fields[parent_index] = build_parent_field(parent_current, parent_names)
        added, removed, kept = apply_level_2_fields(fields, level_2)
```

Also include parent removals in the existing removal guard by extending `removed_values`:

```python
        removals = describe_removals(fields, level_2, removed_v, removed_u)
        removals += [f"parent_domain option {p!r}" for p in removed_p]
```

- [ ] **Step 4: Verify against the live tenant, read-only**

```bash
set -a && . ./.env && set +a
.venv/bin/python workflows/definitions/generate_form_fields.py
```

Expected: a new `parent_domain.allowedValues (0 committed, 20 live)` section listing 20 additions, and the line `valueType is 'URN', not 'STRING'`. Then:

```bash
.venv/bin/python workflows/definitions/generate_form_fields.py --check; echo "exit=$?"
```

Expected: `exit=1`.

- [ ] **Step 5: Commit**

```bash
git add tests/test_generator.py workflows/definitions/generate_form_fields.py
git commit -m "feat: report parent_domain drift and refuse duplicate root names"
```

---

### Task 5: Resolve a root name back to a URN in the listener

**Files:**
- Modify: `src/actions/client.py` — add `_ROOT_DOMAINS_QUERY` beside `_CHILD_DOMAINS_QUERY` (~line 120), add `get_root_domains` beside `get_child_domains` (~line 364)
- Modify: `src/actions/domain.py` — `create_domain` line 75, plus a new `_resolve_root` helper beside `_load_parent`
- Test: `tests/test_behaviour.py`

**Interfaces:**
- Consumes: `DomainClient.execute`, `domain_node_from_entity`, `HierarchyError`
- Produces: `DataHubClient.get_root_domains() -> list[dict]`; `domain._resolve_root(client, value) -> str | None`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_behaviour.py`. `FakeClient` needs the new method, so add it to the class first (beside `get_child_domains`, line 99):

```python
    def get_root_domains(self):
        return [
            {
                "urn": PARENT,
                "properties": {"name": "Risk & Compliance"},
                "parentDomains": {"domains": []},
                "structuredProperties": {"properties": []},
            }
        ]
```

Then the test:

```python
def test_parent_domain_is_submitted_as_a_name_and_resolved_to_a_root_urn():
    """parent_domain is a STRING dropdown now, so the payload carries a name.

    The URN picker could not be constrained to Level 1, so the field became a
    generated list of root names. The listener therefore has to map that name
    back to a urn before anything else -- and scope the lookup to roots, so a
    Level 2 that happens to share a name cannot be selected by accident.
    """
    fields = [
        {"id": "new_domain_name", "values": [{"stringValue": "Payments"}]},
        {"id": "parent_domain", "values": [{"stringValue": "Risk & Compliance"}]},
        {"id": "technical_owners", "values": [{"stringValue": OWNER}]},
        {"id": "data_stewards", "values": [{"stringValue": OWNER}]},
    ]
    action, client = build(request_for(DOMAIN_WF, "ACCEPTED", fields))
    action.act(Event("urn:li:actionRequest:test", "ACCEPTED"))

    created = next(v for op, v in client.mutations if op == "createDomain")
    assert created["input"]["parentDomain"] == PARENT


def test_an_unknown_parent_domain_name_fails_loudly():
    """A stale dropdown option must name the problem, not resolve to nothing.

    Silently treating it as a root proposal would create a top-level domain the
    requester never asked for, and three approvers already signed off on.
    """
    from actions.errors import DataHubError

    fields = [
        {"id": "new_domain_name", "values": [{"stringValue": "Payments"}]},
        {"id": "parent_domain", "values": [{"stringValue": "No Such Domain"}]},
        {"id": "technical_owners", "values": [{"stringValue": OWNER}]},
        {"id": "data_stewards", "values": [{"stringValue": OWNER}]},
    ]
    action, client = build(request_for(DOMAIN_WF, "ACCEPTED", fields))
    with pytest.raises(DataHubError, match="No Such Domain"):
        action.act(Event("urn:li:actionRequest:test", "ACCEPTED"))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_behaviour.py -k "submitted_as_a_name or unknown_parent" -v`
Expected: FAIL — the name is passed to `get_domain` as if it were a URN, so `_load_parent` raises `Parent domain Risk & Compliance does not exist`

- [ ] **Step 3: Write minimal implementation**

In `src/actions/client.py`, after `_CHILD_DOMAINS_QUERY` (line 141):

```python
_ROOT_DOMAINS_QUERY = """
query rootDomains {
  searchAcrossEntities(input: {types: [DOMAIN], query: "*", start: 0, count: 1000,
                               orFilters: [{and: [{field: "hasParentDomain",
                                                   condition: EQUAL,
                                                   negated: false,
                                                   values: ["false"]}]}]}) {
    total
    searchResults {
      entity {
        urn
        ... on Domain {
          properties { name }
          parentDomains { domains { urn } }
          structuredProperties {
            properties {
              structuredProperty { urn }
              values { ... on StringValue { stringValue } }
            }
          }
        }
      }
    }
  }
}
"""
```

And the method, beside `get_child_domains`:

```python
    def get_root_domains(self) -> list[dict]:
        """Every domain with no parent, i.e. the Level 1 tier.

        ``hasParentDomain: "false"`` is the indexed predicate for this; verified
        against the live instance, where it partitions the tree exactly (20 root,
        26 non-root, 46 total). Reads through search, so it lags writes by
        seconds -- a root created moments ago may not appear yet.
        """
        result = self.execute(_ROOT_DOMAINS_QUERY, {})["searchAcrossEntities"]
        return [hit["entity"] for hit in (result.get("searchResults") or []) if hit.get("entity")]
```

In `src/actions/domain.py`, add beside `_load_parent`:

```python
def _resolve_root(client: DataHubClient, value: str | None) -> str | None:
    """Map a submitted ``parent_domain`` name to a root domain URN.

    The field is a generated STRING dropdown of root names, because a URN picker
    cannot be constrained to Level 1 on this instance. Resolution is scoped to
    roots so a deeper domain sharing a name cannot be chosen by accident.

    Blank means "propose a new Level 1", which is a valid request, so it maps to
    None rather than raising.
    """
    if not value or not value.strip():
        return None
    wanted = value.strip()

    roots = [domain_node_from_entity(e) for e in client.get_root_domains()]
    matches = [r for r in roots if r.name == wanted]

    if not matches:
        available = ", ".join(sorted(r.name for r in roots)) or "none"
        raise HierarchyError(
            f"{wanted!r} is not a root (Level 1) domain (available: {available}). "
            f"The parent dropdown is generated from the domain tree and may be "
            f"stale; re-run generate_form_fields.py and re-submit."
        )
    if len(matches) > 1:
        raise HierarchyError(
            f"{len(matches)} root domains are named {wanted!r}, so the selection "
            f"is ambiguous. Rename one of them."
        )
    return matches[0].urn
```

Change line 75 to resolve first:

```python
    parent = _load_parent(client, _resolve_root(client, fields.one("parent_domain")), "Parent")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_behaviour.py -v`
Expected: PASS — all previously-passing tests plus the two new ones

- [ ] **Step 5: Commit**

```bash
git add src/actions/client.py src/actions/domain.py tests/test_behaviour.py
git commit -m "feat: resolve parent_domain name to a root domain urn"
```

---

### Task 6: Update the definition guards

**Files:**
- Modify: `tests/test_behaviour.py` — `test_parent_domain_carries_no_filter_condition`, `test_parent_domain_description_admits_the_list_is_unfiltered`
- Test: same file

**Interfaces:**
- Consumes: nothing new
- Produces: nothing new

**Why:** two existing guards encode the *old* world. One asserts no `filterCondition` (still correct, keep it). The other asserts the description says the list is "not filtered" — which becomes false once the list is curated, and would leave the form telling requesters the opposite of the truth.

- [ ] **Step 1: Write the failing test**

Replace `test_parent_domain_description_admits_the_list_is_unfiltered` entirely with:

```python
def test_parent_domain_is_a_curated_string_dropdown():
    """The field must be STRING with options, or nothing constrains the picker.

    A URN-typed parent_domain is an unconstrained entity picker: it offers every
    domain at every depth, and a Level 3 choice is rejected only after all three
    approvals. allowedValues is rejected on URN fields, so STRING is the only
    shape that can carry a curated list.
    """
    definition = json.loads(
        (ROOT / "workflows" / "definitions" / "action-workflow-propose-new-domain_v2.json").read_text()
    )
    fields = definition["input"]["trigger"]["form"]["fields"]
    parent = next(f for f in fields if f["id"] == "parent_domain")

    assert parent["valueType"] == "STRING", (
        f"parent_domain is {parent['valueType']!r}; a URN picker cannot be "
        "constrained to Level 1 on this instance"
    )
    assert parent["allowedEntityTypes"] is None, parent["allowedEntityTypes"]
    options = [v["stringValue"] for v in parent.get("allowedValues") or []]
    assert options, "parent_domain has no options, so the dropdown would be empty"
    assert not any(o.startswith("urn:") for o in options), (
        f"options must be display names, not urns: {options[:3]}"
    )


def test_every_parent_keyed_condition_compares_names_not_urns():
    """A urn here never matches a name, so the field silently never renders.

    The server accepts this mismatch, and the form then looks merely incomplete
    rather than broken -- no error anywhere. Five fields gate on parent_domain.
    """
    definition = json.loads(
        (ROOT / "workflows" / "definitions" / "action-workflow-propose-new-domain_v2.json").read_text()
    )
    fields = definition["input"]["trigger"]["form"]["fields"]
    parent_options = {
        v["stringValue"]
        for v in next(f for f in fields if f["id"] == "parent_domain").get("allowedValues") or []
    }

    gated = [
        f for f in fields
        if ((f.get("condition") or {}).get("singleFieldValueCondition") or {}).get("field")
        == "parent_domain"
    ]
    assert gated, "expected level_2_* and l1_gc_domain_code to gate on parent_domain"

    for field in gated:
        values = field["condition"]["singleFieldValueCondition"]["values"]
        assert not any(v.startswith("urn:") for v in values), (
            f"{field['id']} compares parent_domain against urns {values}; "
            "parent_domain holds a name, so this can never match"
        )
        unknown = set(values) - parent_options
        assert not unknown, (
            f"{field['id']} gates on {sorted(unknown)}, which is not an option in "
            "parent_domain, so the field can never render"
        )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_behaviour.py -k "curated_string or compares_names" -v`
Expected: FAIL — `parent_domain is 'URN'; a URN picker cannot be constrained`. This stays failing until Task 7 regenerates the definition.

- [ ] **Step 3: No implementation in this task**

These two guards fail by design until Task 7 writes the new definition. That is the intended red state: the tests describe the target, and Task 7 turns them green. Do not hand-edit the JSON to satisfy them — the generator owns it now, which is the whole point.

- [ ] **Step 4: Confirm the rest of the suite is unaffected**

Run: `.venv/bin/python -m pytest tests/test_behaviour.py -v --deselect tests/test_behaviour.py::test_parent_domain_is_a_curated_string_dropdown --deselect tests/test_behaviour.py::test_every_parent_keyed_condition_compares_names_not_urns`
Expected: PASS — every other test still green

- [ ] **Step 5: Commit**

```bash
git add tests/test_behaviour.py
git commit -m "test: pin parent_domain as a curated name-keyed dropdown"
```

---

### Task 7: Regenerate and deploy — GATED

**Files:**
- Modify: `workflows/definitions/action-workflow-propose-new-domain_v2.json` (generated, do not hand-edit)

**Interfaces:**
- Consumes: everything from Tasks 1-6

**BLOCKED until the tenant is cleaned.** `--write` refuses while two children of `Data Contracts` are both named `Test2`. Confirm with the operator which to rename before starting; the generator will not proceed and should not be bypassed.

- [ ] **Step 1: Confirm the blocker is cleared**

```bash
set -a && . ./.env && set +a
.venv/bin/python workflows/definitions/generate_form_fields.py 2>&1 | grep -i 'WARNING\|REFUS' || echo "no warnings -- clear to write"
```

Expected: `no warnings -- clear to write`. If `Test2` still appears, stop and escalate.

- [ ] **Step 2: Regenerate**

```bash
.venv/bin/python workflows/definitions/generate_form_fields.py --write
git diff workflows/definitions/action-workflow-propose-new-domain_v2.json
```

Review as **data**: `parent_domain` should be STRING with one option per real root domain, and every `parent_domain`-keyed condition should hold names. If the diff shows removals, the command will have refused — confirm each removal is intended before re-running with `--allow-removals`.

- [ ] **Step 3: Verify the guards go green**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS, including the two Task 6 guards that were red

- [ ] **Step 4: Deploy and confirm**

```bash
.venv/bin/python workflows/definitions/upsert_workflows.py \
  --server "$DATAHUB_GMS_URL" --token-env DATAHUB_GMS_TOKEN \
  --file workflows/definitions/action-workflow-propose-new-domain_v2.json
```

A failed upsert is atomic and safe, so this doubles as the only real validation — `--dry-run` cannot catch a server-side rejection. Then re-query to confirm what landed:

```bash
.venv/bin/python - <<'PY'
import json, os, urllib.request
url = os.environ["DATAHUB_GMS_URL"].rstrip("/") + "/api/v2/graphql"
q = """query{ listActionWorkflows(input:{start:0,count:60}){ workflows{ urn
 trigger{ form{ fields{ id valueType allowedValues{ ... on StringValue{stringValue} }
 condition{ singleFieldValueCondition{ field values } } } } } } } }"""
r = urllib.request.Request(url, data=json.dumps({"query": q}).encode(),
    headers={"Authorization": f"Bearer {os.environ['DATAHUB_GMS_TOKEN']}",
             "Content-Type": "application/json"})
w = [x for x in json.load(urllib.request.urlopen(r, timeout=30))
     ["data"]["listActionWorkflows"]["workflows"]
     if x["urn"] == "urn:li:actionWorkflow:propose-new-domain"][0]
for f in w["trigger"]["form"]["fields"]:
    if f["id"] == "parent_domain":
        print("valueType:", f["valueType"])
        print("options  :", [v["stringValue"] for v in f["allowedValues"] or []])
    sv = (f.get("condition") or {}).get("singleFieldValueCondition")
    if sv and sv["field"] == "parent_domain":
        print(f"{f['id']:28} gates on {sv['values']}")
PY
```

Expected: `valueType: STRING`, one option per root domain, and every gate listing **names**.

- [ ] **Step 5: Open the form and check the chaining by hand**

Validation is not behaviour. Load Propose New Domain in DataHub and confirm the parent dropdown lists only root domains, and that choosing one with children reveals its `level_2_*` dropdown. The name-keyed conditions are the part most likely to be accepted-but-wrong, and only the UI shows it.

- [ ] **Step 6: Commit**

```bash
git add workflows/definitions/action-workflow-propose-new-domain_v2.json
git commit -m "feat: regenerate parent_domain as a curated root-domain dropdown"
```

---

## Self-Review

**Spec coverage:** Generator owns `parent_domain` (Tasks 1, 2, 4). Five dependent conditions rewritten to names (Task 3). Listener name→URN resolution (Task 5). Drift detection and guards (Task 4). Definition guards (Task 6). Deploy (Task 7). No requirement left unassigned.

**Placeholder scan:** No TBDs. Every code step carries real code. Task 6 Step 3 deliberately has no implementation and says why.

**Type consistency:** `parent_field_values -> (list[str], list[str])` used consistently in Tasks 1, 2, 4. `build_code_field(existing, values, names)` — third parameter renamed in Task 3 and called with `names` in Task 4. `code_field_values` returns names from Task 3 onward; Task 4's `main()` unpacks it as `values, names`. `get_root_domains()` defined in Task 5 and stubbed on `FakeClient` in the same task.

**Known gaps, deliberate:**
- Renaming a root domain breaks in-flight requests and requires regeneration. Names are not stable identifiers; URNs are. This is a real robustness regression accepted to get enforcement.
- A new root domain is invisible until regeneration and redeploy. The daily drift job bounds that to a day. The direction is benign — a requester who cannot find their domain says so, rather than silently getting a wrong one.
- The curated list will carry whatever junk is in the tenant. `KUMARTEST`, `test_urn`, `Manju_Test` and `POC_Parent Domian` are currently roots. A curated dropdown *endorses* them in a way an entity picker does not; cleaning them is out of scope here but should precede Task 7.
