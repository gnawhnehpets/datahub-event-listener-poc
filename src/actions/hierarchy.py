"""Domain hierarchy parsing and the rules the trigger form does not enforce.

The v2 form asks for a single ``parent_domain`` and the rest of the chain is
derived upward (see ``DomainHandler.build_plan``), which makes a mismatched
L1/L2 pair structurally impossible. An earlier two-picker form could not
filter the Level 2 list by the Level 1 choice, and these checks were written
for it.

That "cannot filter" claim is now only half true, and the correction matters
if the form is ever revised. ``ActionWorkflowFieldValuesSourceType`` does
still enumerate only CURRENT_USER_GROUPS — but it is not the only mechanism.
``ActionWorkflowFieldInput.dynamicSource`` is a separate slot of shape
``{resolvers: [{resolver, source}], filter}`` whose ``source`` accepts
``"field:<FORM_FIELD_ID>"``, so cross-field option filtering does exist.
Verified by schema introspection 2026-09-01. Whether any resolver traverses
domain parent -> child is NOT confirmed: the resolver allowlist is
server-side and not introspectable.

These checks run after approval but before anything is created regardless,
because the code derivation depends on the parents' structured properties,
which no form can read.
"""

from __future__ import annotations

from dataclasses import dataclass

from actions.errors import DataHubError
from actions.properties import GC_DOMAIN_CODE

MAX_DEPTH = 3


class HierarchyError(DataHubError):
    """The proposed parent hierarchy is invalid. Permanent, not transient."""


@dataclass(frozen=True)
class DomainNode:
    """A domain reduced to the four things the derivation needs.

    ``ancestors`` is the full chain with the *immediate* parent first — that
    is the order DataHub's ``parentDomains.domains`` returns, verified against
    the live instance (KYC Review -> [Finance, Risk & Compliance]).
    """

    urn: str
    name: str
    codes: list[str]
    ancestors: list[str]


def domain_node_from_entity(entity: dict) -> DomainNode:
    """Reduce a ``Domain`` GraphQL payload to a DomainNode. Pure."""
    properties = entity.get("properties") or {}
    parents = (entity.get("parentDomains") or {}).get("domains") or []

    codes: list[str] = []
    structured = (entity.get("structuredProperties") or {}).get("properties") or []
    for prop in structured:
        if (prop.get("structuredProperty") or {}).get("urn") != GC_DOMAIN_CODE:
            continue
        for value in prop.get("values") or []:
            string_value = value.get("stringValue")
            if string_value:
                codes.append(string_value)

    return DomainNode(
        urn=entity.get("urn") or "",
        name=properties.get("name") or "",
        codes=codes,
        ancestors=[parent.get("urn") for parent in parents if parent.get("urn")],
    )


def validate_hierarchy(
    l1: DomainNode | None, l2: DomainNode | None
) -> DomainNode | None:
    """Check the submitted parents and return the new domain's direct parent.

    Returns None for a root-level proposal. Raises rather than silently
    correcting, because a wrong parent produces a wrong domain code and a
    misfiled governance artefact.
    """
    if l2 is not None and l1 is None:
        raise HierarchyError(
            f"A Level 2 parent ({l2.name!r}) was selected without a Level 1 parent. "
            f"Select both, or neither for a root-level domain."
        )

    if l1 is None:
        return None

    if l1.ancestors:
        raise HierarchyError(
            f"{l1.name!r} is not a Level 1 domain — it sits under "
            f"{l1.ancestors[0]}. The Level 1 field must hold a root domain."
        )

    if l2 is None:
        return l1

    if not l2.ancestors or l2.ancestors[0] != l1.urn:
        raise HierarchyError(
            f"{l2.name!r} is not a child of {l1.name!r} "
            f"(its parent chain is {l2.ancestors or 'empty'}). "
            f"The Level 2 selection must belong to the chosen Level 1."
        )

    return l2


def level_for(parent: DomainNode | None) -> int:
    """The level the new domain will occupy, given its direct parent."""
    level = 1 if parent is None else len(parent.ancestors) + 2
    if level > MAX_DEPTH:
        raise HierarchyError(
            f"Creating a domain under {parent.name!r} would place it at level "
            f"{level}; the taxonomy supports at most {MAX_DEPTH} levels."
        )
    return level
