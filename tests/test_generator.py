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
