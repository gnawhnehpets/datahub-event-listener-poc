#!/usr/bin/env python
"""Regenerate the parts of the Propose New Domain form that depend on live data.

Two things in the definition are derived from the domain tree and go stale the
moment it changes:

  * ``l1_gc_domain_code.allowedValues`` -- the GC codes a requester may pick.
  * ``l1_gc_domain_code.condition``     -- which parent selections show the field.

Both were hand-written, and both had already drifted: Risk & Compliance gained a
fourth code that no requester could select, and Level 2 domains added after the
definition was written did not show the field at all, so those requests failed
only after all three approvals.

Staleness in the first is the dangerous one. The listener validates the chosen
code against the parent's live codes, so an *absent* option is not caught -- the
requester picks a wrong-but-valid code and it is stamped onto a governance
artifact silently. That is exactly what ``domain_code.select_code`` refuses to
guess at.

Usage:
    set -a && . ./.env && set +a

    # show drift, change nothing (default)
    python workflows/definitions/generate_form_fields.py

    # fail with exit code 1 if the committed definition is stale -- for CI
    python workflows/definitions/generate_form_fields.py --check

    # rewrite the definition in place, then upsert it
    python workflows/definitions/generate_form_fields.py --write

    # ...including any removals, once you have confirmed the domains are gone
    python workflows/definitions/generate_form_fields.py --write --allow-removals


After --write, deploy with upsert_workflows.py or the datahub CLI; this script
never writes to DataHub.

``--check`` is the safe one to run unattended: it is read-only and exits 1 on
drift, so it suits a cron job or a CI step.
``--write`` deliberately is not: it refuses to drop an option without
--allow-removals, because a truncated fetch and a deleted domain look identical
from here, and only a human can tell them apart.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from actions.client import DataHubClient  # noqa: E402
from actions.fields import slugify  # noqa: E402
from actions.hierarchy import (  # noqa: E402
    MAX_DEPTH,
    DomainNode,
    domain_node_from_entity,
    level_for,
)

DEFINITION = Path(__file__).with_name("action-workflow-propose-new-domain_v2.json")
CODE_FIELD = "l1_gc_domain_code"

QUERY_COUNT = 1000


class IncompleteFetch(RuntimeError):
    """The domain fetch cannot be trusted to represent the whole taxonomy.

    Distinct from a DataHub error: the call succeeded, but what came back is
    not a complete view, and every derivation here reads a missing domain as a
    deleted one.
    """


# GraphQL is brace-dense, so the count is substituted rather than f-stringed --
# doubling every brace to interpolate one number makes the query unreadable.
DOMAINS_QUERY = """
query domains {
  search(input: {type: DOMAIN, query: "*", start: 0, count: __COUNT__}) {
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
""".replace("__COUNT__", str(QUERY_COUNT))


def fetch_domains(client: DataHubClient) -> dict[str, DomainNode]:
    """Every domain with its full ancestor chain, in one query.

    ``parentDomains`` returns the whole chain rather than just the immediate
    parent, so a subtree is a filter over this dict -- no traversal, and no
    dependence on DESCENDANTS_INCL, which does not expand on the domain index.

    Raises ``IncompleteFetch`` rather than returning a partial view. Every
    derivation below reads an absent domain as a deleted one, so a short answer
    does not degrade the output -- it inverts it, proposing removals for
    domains that are simply missing from the response.
    """
    result = client.execute(DOMAINS_QUERY, {})["search"]
    hits = result["searchResults"]

    if result["total"] > len(hits):
        raise IncompleteFetch(
            f"DataHub reports {result['total']} domains but returned {len(hits)}. "
            f"Raise QUERY_COUNT (currently {QUERY_COUNT}) or page the query; "
            f"form fields will not be derived from a partial view."
        )
    if not hits:
        raise IncompleteFetch(
            "DataHub returned no domains. Every generated field would be emptied, "
            "so this is treated as a failed fetch rather than an empty taxonomy."
        )

    nodes: dict[str, DomainNode] = {}
    for hit in hits:
        node = domain_node_from_entity(hit["entity"])
        if node.urn:
            nodes[node.urn] = node
    return nodes


def derived_level_1(node: DomainNode, nodes: dict[str, DomainNode]) -> DomainNode:
    """The Level 1 the handler will derive for a given parent selection.

    Mirrors ``DomainHandler.build_plan``: the chain is ordered immediate-parent
    first, so the root is the last entry that still resolves. Dangling ancestor
    URNs are skipped rather than raising -- an instance can reference a domain
    that search does not return.
    """
    for ancestor in reversed(node.ancestors):
        if ancestor in nodes:
            return nodes[ancestor]
    return node


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


LEVEL_2_PREFIX = "level_2_"



def _can_take_a_child(node: DomainNode) -> bool:
    """Whether a new domain may be created directly under *node*.

    ``level_for`` raises when the result would exceed MAX_DEPTH, so it is the
    single source of truth for the limit -- the generator must not carry its
    own copy of the rule and let the two drift.
    """
    try:
        level_for(node)
        return True
    except Exception:
        return False

def level_2_fields(nodes: dict[str, DomainNode]) -> tuple[list[dict], list[str]]:
    """One Level 2 narrowing dropdown per domain that has children.

    ``parent_domain`` stays the Level 1 choice. These fields let the requester
    narrow to a child of it, and each is shown only when ``parent_domain`` is
    the domain it belongs to -- so a parent with no children surfaces no second
    dropdown at all.

    A single field cannot serve every parent: ``allowedValues`` is one static
    list, so the options have to be split across one field per parent and
    selected by condition. That is the only mechanism verified to work here.

    Values are display names because ``allowedValues`` is not used on URN-typed
    fields anywhere on this instance. Names only have to be unique *among one
    parent's children* for the choice to be unambiguous, since the field is
    scoped to that parent -- a far weaker requirement than global uniqueness.
    Sibling collisions are reported rather than silently emitted.
    """
    warnings: list[str] = []
    fields: list[dict] = []

    parents = sorted(
        (n for n in nodes.values() if any(n.urn in m.ancestors for m in nodes.values())),
        key=lambda n: (len(n.ancestors), n.name),
    )

    for parent in parents:
        children = sorted(
            (n for n in nodes.values() if n.ancestors and n.ancestors[0] == parent.urn),
            key=lambda n: n.name,
        )
        if not children:
            continue  # descendants exist but no direct child: nothing to narrow to

        # A child that cannot itself take a child is not a narrowing option --
        # selecting it produces a proposal the listener rejects for depth, and
        # only AFTER all three approvals. Offering it is exactly the failure
        # this generator exists to prevent, so it is dropped here.
        #
        # This removes every option under a parent that is already Level 2,
        # because its children are Level 3 and a new domain beneath them would
        # be Level 4. Such a parent then emits no dropdown at all, which is
        # correct: there is nothing valid to narrow to.
        usable = [c for c in children if _can_take_a_child(c)]
        if not usable:
            continue
        children = usable

        names = [c.name for c in children]
        dupes = {n for n in names if names.count(n) > 1}
        if dupes:
            warnings.append(
                f"{parent.name!r} has children sharing a name {sorted(dupes)}; "
                f"the submitted value would be ambiguous"
            )

        fields.append({
            "id": f"{LEVEL_2_PREFIX}{slugify(parent.name)}",
            "name": f"Level 2 domain under {parent.name}",
            "description": (
                f"Optional. Narrow the parent to a domain directly beneath "
                f"{parent.name}. Leave blank to propose directly under {parent.name}."
            ),
            "valueType": "STRING",
            "allowedEntityTypes": None,
            "allowedValues": [{"stringValue": n} for n in names],
            "cardinality": "SINGLE",
            "required": False,
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
            "valuesSource": None,
        })
    return fields, warnings


def apply_level_2_fields(fields: list[dict], generated: list[dict]) -> tuple[int, int, int]:
    """Replace the generated Level 2 block in place, just after ``parent_domain``.

    Existing ``level_2_*`` fields are dropped first, so a domain that loses its
    children loses its dropdown instead of lingering with stale options.
    Returns (added, removed, kept) for reporting.
    """
    old = {f["id"] for f in fields if f["id"].startswith(LEVEL_2_PREFIX)}
    new = {f["id"] for f in generated}

    fields[:] = [f for f in fields if not f["id"].startswith(LEVEL_2_PREFIX)]
    anchor = next(i for i, f in enumerate(fields) if f["id"] == PARENT_FIELD)
    fields[anchor + 1 : anchor + 1] = generated

    return len(new - old), len(old - new), len(old & new)




def describe_removals(
    fields: list[dict],
    generated: list[dict],
    removed_values: list[str],
    removed_urns: list[str],
) -> list[str]:
    """Everything a --write would take away, as human-readable lines.

    Options vanishing inside a *surviving* dropdown count too: a renamed child
    domain leaves the dropdown in place and silently drops one of its options,
    which is the same loss as deleting the dropdown outright.
    """
    out = [f"{CODE_FIELD} option {v!r}" for v in removed_values]
    out += [f"{CODE_FIELD} shown for {u}" for u in removed_urns]

    by_id = {f["id"]: f for f in generated}
    for field in fields:
        fid = field["id"]
        if not fid.startswith(LEVEL_2_PREFIX):
            continue
        was = {v["stringValue"] for v in field.get("allowedValues") or []}
        if fid not in by_id:
            out.append(f"dropdown {fid} entirely ({len(was)} option(s))")
            continue
        now = {v["stringValue"] for v in by_id[fid]["allowedValues"]}
        out += [f"{fid} option {name!r}" for name in sorted(was - now)]
    return out


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--write", action="store_true", help="rewrite the definition in place")
    ap.add_argument("--check", action="store_true", help="exit 1 if the definition is stale")
    ap.add_argument(
        "--allow-removals",
        action="store_true",
        help="permit --write to drop options or dropdowns (required for any removal)",
    )
    args = ap.parse_args()

    server = os.environ.get("DATAHUB_GMS_URL", "").strip()
    token = os.environ.get("DATAHUB_GMS_TOKEN", "").strip()
    if not server or not token:
        sys.exit("Set DATAHUB_GMS_URL and DATAHUB_GMS_TOKEN (set -a && . ./.env && set +a)")

    nodes = fetch_domains(DataHubClient(server=server, token=token))
    print(f"fetched {len(nodes)} domains")

    values, names = code_field_values(nodes)
    parent_names, parent_warnings = parent_field_values(nodes)

    document = json.loads(DEFINITION.read_text())
    fields = document["input"]["trigger"]["form"]["fields"]
    index = next(i for i, f in enumerate(fields) if f["id"] == CODE_FIELD)
    current = fields[index]

    current_values = [v["stringValue"] for v in current.get("allowedValues") or []]
    current_names = ((current.get("condition") or {}).get("singleFieldValueCondition") or {}).get("values") or []

    added_v = sorted(set(values) - set(current_values))
    removed_v = sorted(set(current_values) - set(values))
    added_n = sorted(set(names) - set(current_names))
    removed_n = sorted(set(current_names) - set(names))
    stale = bool(added_v or removed_v or added_n or removed_n)

    print(f"\n{CODE_FIELD}.allowedValues")
    print(f"  committed : {current_values}")
    print(f"  live      : {values}")
    for v in added_v:
        print(f"  + {v}   MISSING today -- a requester cannot select it, and the listener "
              f"accepts any other valid code, so a wrong code is stamped silently")
    for v in removed_v:
        print(f"  - {v}   no longer on any ambiguous Level 1")

    print(f"\n{CODE_FIELD}.condition ({len(current_names)} committed, {len(names)} live)")
    for n in added_n:
        print(f"  + {n}  -- field is required here but "
              f"is not shown, so the request fails after all three approvals")
    for n in removed_n:
        print(f"  - {n}")

    parent_index = next(i for i, f in enumerate(fields) if f["id"] == PARENT_FIELD)
    parent_current = fields[parent_index]
    current_parents = [v["stringValue"] for v in parent_current.get("allowedValues") or []]

    added_p = sorted(set(parent_names) - set(current_parents))
    removed_p = sorted(set(current_parents) - set(parent_names))
    parent_stale = (
        bool(added_p or removed_p)
        or parent_current.get("valueType") != "STRING"
        or parent_current.get("allowedEntityTypes") is not None
    )

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

    stale = stale or parent_stale

    level_2, l2_warnings = level_2_fields(nodes)
    current_l2 = [f["id"] for f in fields if f["id"].startswith(LEVEL_2_PREFIX)]

    # Compare options, not just which dropdowns exist. A renamed or moved child
    # leaves the dropdown in place and changes only its contents, so an
    # id-only comparison reports "up to date" while the form offers a name that
    # no longer resolves.
    def l2_options(source: list[dict]) -> dict[str, list[str]]:
        return {
            f["id"]: sorted(v["stringValue"] for v in f.get("allowedValues") or [])
            for f in source
            if f["id"].startswith(LEVEL_2_PREFIX)
        }

    l2_stale = l2_options(level_2) != l2_options(fields)
    stale = stale or l2_stale

    print(f"\nLevel 2 dropdowns ({len(current_l2)} committed, {len(level_2)} live)")
    committed_options = l2_options(fields)
    for f in level_2:
        vals = [v["stringValue"] for v in f["allowedValues"]]
        mark = "" if f["id"] in current_l2 else "  + new"
        print(f"  {f['id']:<34} {len(vals)} option(s){mark}")
        # Itemise option-level drift. Without this the run exits 1 while every
        # line above it looks unchanged -- the field count is identical when a
        # parent merely gains or loses a child, and a bare "FAIL" with nothing
        # to point at is what makes people distrust the check.
        was = set(committed_options.get(f["id"], []))
        now = set(v["stringValue"] for v in f["allowedValues"])
        for added in sorted(now - was):
            print(f"    + {added!r} is selectable live but not in the definition")
        for dropped in sorted(was - now):
            print(f"    - {dropped!r} is in the definition but no longer a child")
    for gone in sorted(set(current_l2) - {f["id"] for f in level_2}):
        # "No children" and "no children that can themselves take a child" are
        # different problems with the same symptom, and an operator deciding
        # whether a removal is safe needs to know which one this is.
        print(
            f"  {gone:<34} - dropped: no selectable children "
            f"(none, or all already at the depth limit)"
        )
    for w in l2_warnings:
        print(f"  WARNING: {w}")

    # Print the all-clear only after every staleness contribution has been
    # folded in. Printing it earlier (e.g. after the GC-code section) produces
    # a report that says "up to date" and then lists additions and exits 1 --
    # a contradiction that makes people distrust the check.
    if not stale:
        print("\nup to date; nothing to regenerate")

    removals = describe_removals(fields, level_2, removed_v, removed_n)
    removals += [f"parent_domain option {p!r}" for p in removed_p]

    if args.write and stale:
        # Additions and removals are not equally risky. A missing option is the
        # dangerous staleness -- the requester cannot pick it and the listener
        # accepts any other valid code -- so additions apply freely. A removal
        # is the destructive direction and is also what a bad fetch looks like,
        # so it needs a human to have read the report above.
        if removals and not args.allow_removals:
            print(f"\nREFUSING to write: {len(removals)} removal(s) --", file=sys.stderr)
            for r in removals:
                print(f"  - {r}", file=sys.stderr)
            # Not always "the domain is gone": an option is also dropped when
            # the domain still exists but can no longer take a child. Saying
            # only the former sends the operator to check the wrong thing.
            print("Confirm each removal is intended -- the domain may be deleted, "
                  "or may still exist but no longer be a valid parent -- then "
                  "re-run with --allow-removals.", file=sys.stderr)
            return 1
        # Emitting these anyway produces a form whose submissions the handler
        # rejects as ambiguous -- after all three approvals. Renaming a sibling
        # is the fix, and it has to happen before the options are written.
        if l2_warnings:
            print("\nREFUSING to write: sibling domains share a name, so the "
                  "submitted value cannot identify one --", file=sys.stderr)
            for w in l2_warnings:
                print(f"  - {w}", file=sys.stderr)
            print("Rename the duplicates in DataHub, then re-run.", file=sys.stderr)
            return 1
        if parent_warnings:
            print("\nREFUSING to write: root domains share a name, so the "
                  "submitted value cannot identify one --", file=sys.stderr)
            for w in parent_warnings:
                print(f"  - {w}", file=sys.stderr)
            print("Rename the duplicates in DataHub, then re-run.", file=sys.stderr)
            return 1

        fields[index] = build_code_field(current, values, names)
        fields[parent_index] = build_parent_field(parent_current, parent_names)
        added, removed, kept = apply_level_2_fields(fields, level_2)
        DEFINITION.write_text(json.dumps(document, indent=2) + "\n")
        print(f"\nrewrote {DEFINITION.name}: Level 2 dropdowns "
              f"+{added} -{removed} ={kept}; deploy it with upsert_workflows.py")

    if args.check and stale:
        print("\nFAIL: committed definition does not match live DataHub", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
