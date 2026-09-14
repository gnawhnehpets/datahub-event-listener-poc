"""Derivation of the GC Domain Code for a proposed domain.

The formula, as specified by the customer:

    [L1 domain code alpha prefix]-[L2 domain code alpha prefix]-[New domain name]

e.g. a domain "GDPR" proposed under KYC Review (GC009), itself under
Finance (FI004), yields ``FI-GC-GDPR``.

Note for anyone reading demo output: on the current trial instance every
``gcDomainCode`` starts with ``GC``, so real derivations collapse to
``GC-GC-<NAME>``. That is the specified formula applied to the data as it
exists, not a defect here.

Everything in this module is pure. It is the single place the formula lives.
"""

from __future__ import annotations

import re

from actions.errors import DataHubError

_LEADING_ALPHA = re.compile(r"^([A-Za-z]+)")
_NON_ALNUM = re.compile(r"[^A-Za-z0-9]+")

SEPARATOR = "-"


class DomainCodeError(DataHubError):
    """The domain code cannot be derived from the submitted request.

    Permanent, not transient: retrying the same event will fail identically.
    It is still raised rather than swallowed so the approval lands in
    failed_events.log instead of disappearing.
    """


def alpha_prefix(code: str) -> str:
    """Take the leading run of letters from a code such as ``GC004`` -> ``GC``."""
    match = _LEADING_ALPHA.match((code or "").strip())
    if not match:
        raise DomainCodeError(
            f"GC Domain Code {code!r} does not begin with letters, so no alpha "
            f"prefix can be extracted"
        )
    return match.group(1).upper()


def normalize_name_segment(name: str) -> str:
    """Render a display name as a code segment: ``Customer Data`` -> ``CUSTOMER_DATA``."""
    collapsed = _NON_ALNUM.sub("_", (name or "").strip()).strip("_").upper()
    if not collapsed:
        raise DomainCodeError(f"Cannot derive a code segment from name: {name!r}")
    return collapsed


def select_code(domain_name: str, codes: list[str], chosen: str | None) -> str:
    """Pick the one GC Domain Code that applies to a parent domain.

    ``gcDomainCode`` is MULTIPLE-cardinality — Risk & Compliance carries
    GC001, GC002 and GC003 — so when a parent has several codes the request
    must say which one applies. Guessing would silently produce a wrong code
    on a governance artefact, so ambiguity is an error.
    """
    present = [code.strip() for code in (codes or []) if code and code.strip()]

    if not present:
        raise DomainCodeError(
            f"Domain {domain_name!r} has no GC Domain Code, so it cannot be used "
            f"as a parent in the domain code formula"
        )

    if len(present) == 1:
        return present[0]

    if chosen is None or not chosen.strip():
        raise DomainCodeError(
            f"Domain {domain_name!r} has {len(present)} GC Domain Codes "
            f"({', '.join(present)}); the request must state which one applies"
        )

    picked = chosen.strip()
    if picked not in present:
        raise DomainCodeError(
            f"Chosen GC Domain Code {picked!r} is not one of {domain_name!r}'s "
            f"codes ({', '.join(present)})"
        )
    return picked


def build_domain_code(
    new_name: str, l1_code: str | None = None, l2_code: str | None = None
) -> str:
    """Join the available parent prefixes with the new domain's name segment.

    Missing parents are omitted rather than padded, so a root-level proposal
    yields just the name segment and an L2 proposal yields ``<L1>-<NAME>``.
    """
    segments: list[str] = []
    if l1_code:
        segments.append(alpha_prefix(l1_code))
    if l2_code:
        segments.append(alpha_prefix(l2_code))
    segments.append(normalize_name_segment(new_name))
    return SEPARATOR.join(segments)
