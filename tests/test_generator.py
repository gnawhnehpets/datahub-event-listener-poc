"""Unit tests for the pure derivation in generate_form_fields.py.

No credentials and no network: every test builds DomainNode fixtures directly,
which is what makes the depth and name rules cheap to pin down.
"""

from __future__ import annotations

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "workflows" / "definitions"))

from actions.hierarchy import DomainNode  # noqa: E402

import generate_form_fields as g  # noqa: E402


# ---------------------------------------------------------------------------
# Minimal definition factory for --write path tests
# ---------------------------------------------------------------------------

def _minimal_definition(
    parent_domain_values: list[str] | None = None,
    extra_level2: list[dict] | None = None,
) -> dict:
    """Build the smallest JSON document main() will not crash on.

    main() requires at minimum:
      - a ``parent_domain`` field (index-resolved before apply_level_2_fields)
      - an ``l1_gc_domain_code`` field (index-resolved separately)
      - zero or more ``level_2_*`` fields (managed by apply_level_2_fields)
    Every other field in the real definition is irrelevant to the generator.
    """
    parent_field: dict = {
        "id": "parent_domain",
        "name": "Parent domain",
        "description": "hand-written wording that must survive",
        "valueType": "URN",
        "allowedEntityTypes": ["DOMAIN"],
        "allowedValues": (
            [{"stringValue": v} for v in parent_domain_values]
            if parent_domain_values is not None
            else None
        ),
        "cardinality": "SINGLE",
        "required": False,
        "condition": None,
        "valuesSource": None,
    }
    code_field: dict = {
        "id": "l1_gc_domain_code",
        "name": "Level 1 GC Domain Code",
        "description": "pick a code",
        "valueType": "STRING",
        "allowedEntityTypes": None,
        "allowedValues": None,
        "cardinality": "SINGLE",
        "required": False,
        "condition": None,
        "valuesSource": None,
    }
    fields: list[dict] = [parent_field, *(extra_level2 or []), code_field]
    return {
        "input": {
            "trigger": {
                "form": {
                    "fields": fields
                }
            }
        }
    }


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


# ---------------------------------------------------------------------------
# --write path tests: exercise main() offline
# ---------------------------------------------------------------------------

def _fixture_nodes_two_roots() -> dict:
    """Alpha and Beta as root domains, no children, no GC codes."""
    return tree(
        node("urn:li:domain:a", "Alpha"),
        node("urn:li:domain:b", "Beta"),
    )


def test_write_patches_parent_domain_in_place(monkeypatch, tmp_path):
    """A successful --write rewrites parent_domain and only parent_domain.

    This test pins the index-ordering invariant: parent_index is resolved
    BEFORE apply_level_2_fields mutates the list.  If the index were stale,
    some other field would carry the dropdown and this assertion would catch it.
    """
    nodes = _fixture_nodes_two_roots()
    definition = _minimal_definition()
    def_path = tmp_path / "def.json"
    def_path.write_text(json.dumps(definition, indent=2) + "\n")

    monkeypatch.setattr(g, "fetch_domains", lambda client: nodes)
    monkeypatch.setattr(g, "DEFINITION", def_path)
    monkeypatch.setenv("DATAHUB_GMS_URL", "https://example.invalid")
    monkeypatch.setenv("DATAHUB_GMS_TOKEN", "token")
    monkeypatch.setattr("sys.argv", ["generate_form_fields.py", "--write"])

    rc = g.main()
    assert rc == 0

    written = json.loads(def_path.read_text())
    fields = written["input"]["trigger"]["form"]["fields"]

    # The parent_domain field must be patched to a STRING dropdown.
    parent = next(f for f in fields if f["id"] == "parent_domain")
    assert parent["valueType"] == "STRING"
    assert parent["allowedEntityTypes"] is None
    assert parent["allowedValues"] == [
        {"stringValue": "Alpha"},
        {"stringValue": "Beta"},
    ]

    # No OTHER field must have gained those allowedValues -- if parent_index
    # was stale (resolved after apply_level_2_fields), the wrong field would
    # carry the dropdown.
    expected_allowed = [{"stringValue": "Alpha"}, {"stringValue": "Beta"}]
    for f in fields:
        if f["id"] == "parent_domain":
            continue
        assert f.get("allowedValues") != expected_allowed, (
            f"field {f['id']!r} incorrectly received parent_domain's allowedValues; "
            "parent_index was resolved at the wrong point in the list"
        )


def test_duplicate_root_names_abort_write_and_leave_file_unchanged(monkeypatch, tmp_path, capsys):
    """Duplicate root names must refuse the write AND leave the file byte-identical.

    The refusal must happen BEFORE any write; a partial write is as bad as a
    full one.  The bytes-before/bytes-after comparison is the guard.
    """
    nodes = tree(
        node("urn:li:domain:1", "Finance"),
        node("urn:li:domain:2", "Finance"),
    )
    definition = _minimal_definition()
    def_path = tmp_path / "def.json"
    original_bytes = (json.dumps(definition, indent=2) + "\n").encode()
    def_path.write_bytes(original_bytes)

    monkeypatch.setattr(g, "fetch_domains", lambda client: nodes)
    monkeypatch.setattr(g, "DEFINITION", def_path)
    monkeypatch.setenv("DATAHUB_GMS_URL", "https://example.invalid")
    monkeypatch.setenv("DATAHUB_GMS_TOKEN", "token")
    monkeypatch.setattr("sys.argv", ["generate_form_fields.py", "--write"])

    rc = g.main()
    assert rc == 1

    # File must be byte-identical: no partial write occurred.
    assert def_path.read_bytes() == original_bytes, (
        "the file was modified despite the refusal; the guard fired too late"
    )

    # The refusal message must name the problem.
    captured = capsys.readouterr()
    assert "Finance" in captured.err, (
        "refusal message must name the duplicate root so the operator knows what to fix"
    )
    assert "REFUSING" in captured.err


def test_parent_option_removal_refused_without_flag_then_accepted_with_flag(
    monkeypatch, tmp_path, capsys
):
    """A removed parent option is blocked without --allow-removals, accepted with it.

    Start with a definition that already has 'OldRoot' in parent_domain.allowedValues.
    The live tree has no domain named 'OldRoot', so it is a removal.
    Without --allow-removals: main() returns 1 and the file is unchanged.
    With --allow-removals: main() returns 0 and 'OldRoot' is gone from the file.
    """
    # Live tree: one root, "Alpha" -- "OldRoot" is not present.
    nodes = tree(node("urn:li:domain:a", "Alpha"))

    # Committed definition already lists "OldRoot" as a parent option.
    definition = _minimal_definition(parent_domain_values=["Alpha", "OldRoot"])
    def_path = tmp_path / "def.json"
    original_bytes = (json.dumps(definition, indent=2) + "\n").encode()
    def_path.write_bytes(original_bytes)

    monkeypatch.setattr(g, "fetch_domains", lambda client: nodes)
    monkeypatch.setattr(g, "DEFINITION", def_path)
    monkeypatch.setenv("DATAHUB_GMS_URL", "https://example.invalid")
    monkeypatch.setenv("DATAHUB_GMS_TOKEN", "token")

    # --- Pass 1: without --allow-removals ---
    monkeypatch.setattr("sys.argv", ["generate_form_fields.py", "--write"])
    rc = g.main()
    assert rc == 1, "removal without --allow-removals must return 1"
    assert def_path.read_bytes() == original_bytes, (
        "file must be unchanged when the removal guard fires"
    )
    captured = capsys.readouterr()
    assert "REFUSING" in captured.err
    assert "OldRoot" in captured.err, (
        "refusal message must name the option being removed"
    )

    # --- Pass 2: with --allow-removals ---
    monkeypatch.setattr("sys.argv", ["generate_form_fields.py", "--write", "--allow-removals"])
    rc = g.main()
    assert rc == 0, "--allow-removals must allow the write to proceed"

    written = json.loads(def_path.read_text())
    fields = written["input"]["trigger"]["form"]["fields"]
    parent = next(f for f in fields if f["id"] == "parent_domain")
    allowed_names = [v["stringValue"] for v in (parent.get("allowedValues") or [])]
    assert "OldRoot" not in allowed_names, "removed option must be absent after --allow-removals write"
    assert "Alpha" in allowed_names, "surviving option must still be present"
