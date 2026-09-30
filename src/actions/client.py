"""Minimal GraphQL client for DataHub.

Deliberately not a general-purpose SDK — it exposes only the handful of
operations this listener needs, and it classifies failures into transient
versus permanent so the router can decide whether to let the pipeline retry.
"""

from __future__ import annotations

import logging
from typing import Any

import requests

from actions.errors import DataHubError, DataHubTransientError
from actions.recipients import Recipient, address_for, recipients_from_entities

logger = logging.getLogger(__name__)

_ACTION_REQUEST_QUERY = """
query actionRequest($urn: String!) {
  actionRequest(urn: $urn) {
    urn
    type
    status
    result
    params {
      workflowFormRequest {
        workflowUrn
        fields {
          id
          values {
            __typename
            ... on StringValue { stringValue }
            ... on NumberValue { numberValue }
          }
        }
      }
    }
  }
}
"""

_ENTITY_EXISTS_QUERY = """
query entity($urn: String!) {
  entity(urn: $urn) {
    urn
    # status { removed } is selected for every typed fragment so soft-deleted
    # entities are detected.  Every handler that creates a new entity type MUST
    # add a matching typed fragment here — see entity_exists() for why.
    ... on Domain {
      properties { name }
      status { removed }
    }
    ... on GlossaryTerm {
      properties { name }
      status { removed }
    }
  }
}
"""

_DOMAIN_QUERY = """
query domain($urn: String!) {
  entity(urn: $urn) {
    ... on Domain {
      urn
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
"""

# Deliberately separate from _ACTION_REQUEST_QUERY. These fields validate on
# the instance in .env, but the deployment target runs a different version and
# Action Workflows is Private Beta -- keeping them here means a schema
# mismatch cannot break the working acceptance path.
_ACTION_REQUEST_DETAIL_QUERY = """
query actionRequestDetail($urn: String!) {
  actionRequest(urn: $urn) {
    urn
    type
    status
    result
    resultNote
    created { time actor { urn } }
    # The entity the request was raised against. Present when the workflow's
    # form declares entityTypes -- Dataset Access Request is raised from a
    # dataset's profile, and the dataset is the subject of the request rather
    # than a form field. Null for workflows with no target, such as Propose New
    # Domain, so every consumer must tolerate it being absent.
    entity {
      urn
      type
      ... on Dataset { name }
    }
    params {
      workflowFormRequest {
        workflowUrn
        decisions {
          stepId
          result
          note
          timestamp
          decidedBy { urn }
        }
      }
    }
  }
}
"""

_CHILD_DOMAINS_QUERY = """
query childDomains($filters: [FacetFilterInput!]) {
  searchAcrossEntities(input: {types: [DOMAIN], query: "*", start: 0, count: 1000,
                               orFilters: [{and: $filters}]}) {
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

_PRINCIPALS_QUERY = """
query principals($urns: [String!]!) {
  entities(urns: $urns) {
    urn
    ... on CorpUser {
      username
      properties { email displayName }
      editableProperties { email }
    }
    ... on CorpGroup {
      name
      properties { email displayName }
    }
  }
}
"""

# Both types are required. Groups created in the DataHub UI use
# IsMemberOfNativeGroup; groups ingested externally typically use
# IsMemberOfGroup. Querying only the latter returns 0 members for
# natively-created groups — the omission that caused the original defect.
_GROUP_MEMBERSHIP_TYPES = ["IsMemberOfNativeGroup", "IsMemberOfGroup"]

_GROUP_MEMBERS_QUERY = """
query groupMembers($urn: String!, $types: [String!]!) {
  corpGroup(urn: $urn) {
    urn
    relationships(input: {types: $types, direction: INCOMING, start: 0, count: 100}) {
      total
      relationships {
        entity {
          urn
          ... on CorpUser {
            username
            properties { email displayName }
            editableProperties { email }
          }
        }
      }
    }
  }
}
"""


class DataHubClient:
    def __init__(
        self,
        server: str,
        token: str,
        timeout: float = 30.0,
        session: Any | None = None,
    ) -> None:
        self._endpoint = f"{server.rstrip('/')}/api/graphql"
        self._token = token
        self._timeout = timeout
        self._session = session or requests.Session()

    def execute(self, query: str, variables: dict) -> dict:
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }
        try:
            response = self._session.post(
                self._endpoint,
                json={"query": query, "variables": variables},
                headers=headers,
                timeout=self._timeout,
            )
        except requests.RequestException as exc:
            raise DataHubTransientError(f"Request to {self._endpoint} failed: {exc}") from exc

        if response.status_code >= 500:
            raise DataHubTransientError(
                f"DataHub returned {response.status_code} for {self._endpoint}"
            )
        if response.status_code >= 400:
            raise DataHubError(
                f"DataHub returned {response.status_code} for {self._endpoint}"
            )

        try:
            payload = response.json()
        except Exception as exc:
            # A proxy error page or other non-JSON 200 is treated as transient
            # so the pipeline retries rather than permanently abandoning the event.
            raise DataHubTransientError(
                f"DataHub returned a non-JSON response from {self._endpoint}: {exc}"
            ) from exc
        # DataHub reports GraphQL errors with HTTP 200, so status alone is not
        # sufficient to conclude success.
        if payload.get("errors"):
            messages = "; ".join(e.get("message", "?") for e in payload["errors"])
            raise DataHubError(f"GraphQL error: {messages}")
        return payload.get("data") or {}

    def get_action_request(self, urn: str) -> dict | None:
        return self.execute(_ACTION_REQUEST_QUERY, {"urn": urn}).get("actionRequest")

    def entity_exists(self, urn: str) -> bool:
        entity = self.execute(_ENTITY_EXISTS_QUERY, {"urn": urn}).get("entity")
        if entity is None:
            return False
        # DataHub resolves any well-formed URN, returning {"urn": "..."} with no
        # properties for entities that do not actually exist or have been deleted.
        # A Domain or GlossaryTerm that exists will have properties set; one that
        # does not will have properties: null.
        if "properties" not in entity:
            # No typed fragment matched this URN.  Returning True here would
            # silently skip every create for the next handler added without a
            # matching fragment — which is precisely the failure that made the
            # first demo silently create nothing.  Raise loudly instead so the
            # omission is caught immediately during development.
            # Fragment list in _ENTITY_EXISTS_QUERY must cover every entity type
            # any handler creates.
            raise DataHubError(
                f"entity_exists has no typed fragment for {urn}; "
                "add one to _ENTITY_EXISTS_QUERY"
            )
        if entity["properties"] is None:
            return False
        # The entity has properties — check whether it has been soft-deleted.
        # We do NOT return False for soft-deleted: calling createDomain against
        # a soft-deleted URN whose key aspect still exists would collide and
        # produce a hard failure.  Instead, log a warning so the operator knows.
        status = entity.get("status") or {}
        if status.get("removed"):
            logger.warning(
                "Entity %s exists but is soft-deleted (removed=true); "
                "create will be skipped and enrichment will target an invisible entity. "
                "Hard-delete or restore the entity before re-submitting.",
                urn,
            )
        return True

    def get_domain(self, urn: str) -> dict | None:
        """Fetch a domain's name, parent chain, and structured properties.

        ``parentDomains.domains`` comes back as the full ancestor chain with
        the immediate parent first.

        Returns ``None`` when the URN resolves to nothing or when the entity
        exists but has no properties (soft-deleted, non-Domain type, or the
        well-known DataHub behaviour of returning ``{"urn": "...", "properties":
        null}`` for URNs that have been tombstoned or never fully created).
        Mirrors the ``entity_exists`` pattern so that ``_load_parent`` in
        ``DomainHandler`` can raise ``HierarchyError`` with a useful message.
        """
        entity = self.execute(_DOMAIN_QUERY, {"urn": urn}).get("entity")
        if not entity or entity.get("properties") is None:
            return None
        return entity

    def get_action_request_detail(self, urn: str) -> dict | None:
        """Fetch the rejection context: who asked, why it was refused.

        A second round trip rather than extra fields on the main query, so a
        schema difference on the deployment target cannot break acceptances.
        """
        return self.execute(_ACTION_REQUEST_DETAIL_QUERY, {"urn": urn}).get("actionRequest")

    def _group_member_entities(self, group_urn: str) -> list[dict]:
        """Fetch the member entity dicts for a corpGroup, returning [] when empty.

        Uses both IsMemberOfNativeGroup and IsMemberOfGroup so that groups
        created in the DataHub UI (which use the native type) are covered.
        Querying only IsMemberOfGroup silently returns 0 for them.
        """
        data = self.execute(
            _GROUP_MEMBERS_QUERY,
            {"urn": group_urn, "types": _GROUP_MEMBERSHIP_TYPES},
        )
        corp_group = (data.get("corpGroup") or {})
        relationships = (corp_group.get("relationships") or {}).get("relationships") or []
        return [r["entity"] for r in relationships if r and r.get("entity")]

    def resolve_principals(self, urns) -> tuple[list[Recipient], list[str]]:
        """Resolve corpUser and corpGroup URNs to email addresses.

        Returns ``(resolved, unresolved_urns)``. ``entities`` comes back in
        request order, and a URN that does not exist arrives with
        ``properties: null`` -- the same shape ``entity_exists`` documents.

        Groups created in the DataHub UI carry no email of their own -- 6 of 9
        groups on the target instance are like this -- so naming one as an owner
        would otherwise notify nobody. When a group has no email, we expand to
        its members instead. A group that DOES have an address keeps it, since
        that may be a real distribution list.
        """
        unique = list(dict.fromkeys(u.strip() for u in (urns or []) if u and u.strip()))
        if not unique:
            return [], []

        entities = self.execute(_PRINCIPALS_QUERY, {"urns": unique}).get("entities") or []

        # A group created in the DataHub UI carries no email of its own -- 6 of 9
        # groups on the target instance are like this -- so naming one as an owner
        # would otherwise notify nobody. Fall back to its members. A group that
        # DOES have an address keeps it, since that may be a real distribution list.
        expanded: set[str] = set()
        members: list[dict] = []
        for entity in entities:
            urn = (entity or {}).get("urn") or ""
            if not urn.startswith("urn:li:corpGroup:") or address_for(entity):
                continue
            found = self._group_member_entities(urn)
            if found:
                expanded.add(urn)
                members.extend(found)

        if not members:
            return recipients_from_entities(entities)

        # Drop the expanded groups themselves so they are neither warned about nor
        # reported unresolved, then resolve everything in ONE pass so ordering and
        # de-duplication stay correct across both sources.
        remaining = [e for e in entities if ((e or {}).get("urn") or "") not in expanded]
        return recipients_from_entities(remaining + members)

    def get_child_domains(self, parent_urn: str) -> list[dict]:
        """Domains whose *immediate* parent is ``parent_urn``.

        Filters on ``parentDomain``, which indexes only the direct parent, so
        this is one level rather than the subtree. DESCENDANTS_INCL is not used
        because it does not expand on the domain index -- it silently behaves
        as EQUAL there.
        """
        result = self.execute(
            _CHILD_DOMAINS_QUERY,
            {"filters": [{"field": "parentDomain", "values": [parent_urn], "condition": "EQUAL"}]},
        )["searchAcrossEntities"]
        return [hit["entity"] for hit in (result.get("searchResults") or []) if hit.get("entity")]

    def get_root_domains(self) -> list[dict]:
        """Every domain with no parent, i.e. the Level 1 tier.

        ``hasParentDomain: "false"`` is the indexed predicate for this; verified
        against the live instance, where it partitions the tree exactly (20 root,
        26 non-root, 46 total). Reads through search, so it lags writes by
        seconds -- a root created moments ago may not appear yet.

        A short read is treated as a hard error rather than a silent truncation.
        Past the query's count limit, a missing root domain would make
        ``_resolve_root`` report a real root name as "not a root domain", sending
        the operator to re-run the generator rather than pointing at the real
        cause. The same logic applies here as in the generator's
        ``IncompleteFetch``: a partial view inverts the answer rather than merely
        degrading it, so a loud failure is safer than a quiet one.
        """
        result = self.execute(_ROOT_DOMAINS_QUERY, {})["searchAcrossEntities"]
        hits = result.get("searchResults") or []
        total = result.get("total") or 0
        if total > len(hits):
            raise DataHubError(
                f"Root-domain query reports {total} domains but returned {len(hits)}. "
                f"Raise the count limit in _ROOT_DOMAINS_QUERY (currently 1000) or "
                f"page the query; _resolve_root cannot be trusted against a partial view."
            )
        return [hit["entity"] for hit in hits if hit.get("entity")]

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

