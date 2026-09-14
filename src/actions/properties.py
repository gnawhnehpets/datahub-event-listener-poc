"""GraphQL documents and helper functions shared by handlers.

These constants and utilities are used to build mutations that add owners,
set domains, and upsert structured properties on DataHub entities.
"""

from __future__ import annotations


def owner_entity_type(urn: str) -> str:
    """DataHub requires the owner's entity type alongside the URN."""
    return "CORP_GROUP" if urn.startswith("urn:li:corpGroup:") else "CORP_USER"


def structured_property_values(pairs: list[tuple[str, object]]) -> list[dict]:
    """Build ``structuredPropertyInputParams``, dropping empty values.

    Empty values are dropped rather than written as blanks so that a re-run
    after a partially filled form does not erase previously set values.
    """
    params = []
    for property_urn, value in pairs:
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        params.append({"structuredPropertyUrn": property_urn, "values": [{"stringValue": str(value)}]})
    return params


ADD_OWNERS = """
mutation addOwners($input: AddOwnersInput!) {
  addOwners(input: $input)
}
"""

UPSERT_PROPERTIES = """
mutation upsertStructuredProperties($input: UpsertStructuredPropertiesInput!) {
  upsertStructuredProperties(input: $input) { properties { structuredProperty { urn } } }
}
"""

# Provisioned on the instance by the Collibra ingestion, not by us. Both are
# scoped to the ``domain`` entity type. gcDomainCode is MULTIPLE-cardinality.
GC_DOMAIN_CODE = "urn:li:structuredProperty:collibra.gcDomainCode"
LOB_LEVEL = "urn:li:structuredProperty:collibra.lobLevel"
