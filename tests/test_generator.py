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
