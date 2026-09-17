"""Recursively introspect the ActionWorkflow GraphQL type, build a full
field-selection query, and dump every configured workflow to JSON.

Use this to capture an instance's live workflow definitions -- for a backup
before an upsert, or to seed a definition file for a new environment.

Credentials come from the same environment this repo uses everywhere else:

    set -a && . ./.env && set +a
    python workflows/definitions/fetch_action_workflows.py > workflows.json

``~/.datahubenv`` is accepted as a fallback so an existing ``datahub init``
setup keeps working, but the environment wins. It used to be the only source,
via a hardcoded absolute path, which made this script unusable on any machine
but its author's.
"""

import json
import os
import re
import sys

import requests


def _credentials() -> tuple[str, str]:
    server = os.environ.get("DATAHUB_GMS_URL", "").strip()
    token = os.environ.get("DATAHUB_GMS_TOKEN", "").strip()
    if server and token:
        return server, token

    fallback = os.path.expanduser("~/.datahubenv")
    if os.path.exists(fallback):
        try:
            # Imported lazily: PyYAML arrives transitively with acryl-datahub
            # rather than being pinned here, so the primary env-var path must
            # not depend on it being present.
            import yaml
        except ImportError:
            yaml = None
        if yaml is not None:
            with open(fallback) as handle:
                cfg = (yaml.safe_load(handle) or {}).get("gms") or {}
            if cfg.get("server") and cfg.get("token"):
                return cfg["server"], cfg["token"]

    sys.exit(
        "Set DATAHUB_GMS_URL and DATAHUB_GMS_TOKEN (set -a && . ./.env && set +a), "
        "or run `datahub init` to populate ~/.datahubenv"
    )


SERVER, TOKEN = _credentials()
# Tolerate both a bare host and one with the /gms suffix, and a trailing slash.
GRAPHQL_URL = re.sub(r"/gms/?$", "", SERVER.rstrip("/")) + "/api/v2/graphql"

session = requests.Session()
session.headers.update({"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"})

_type_cache = {}

# Broad, relationally-heavy entity types referenced from workflow actors/steps.
# We only need the urn to rebuild the workflow elsewhere, not their full profile graph.
LEAF_TYPES = {
    "CorpUser",
    "CorpGroup",
    "DataHubRole",
    "Tag",
    "GlossaryTerm",
    "GlossaryNode",
    "Domain",
    "Owner",
    "DataPlatform",
    "DataPlatformInstance",
    "Dataset",
    "Container",
    "DataProduct",
}


def introspect_type(name):
    if name in _type_cache:
        return _type_cache[name]
    query = """
    query($name: String!) {
      __type(name: $name) {
        kind
        possibleTypes { name }
        fields {
          name
          args { name type { kind ofType { kind } } defaultValue }
          type {
            kind name
            ofType { kind name ofType { kind name ofType { kind name ofType { kind name } } } }
          }
        }
      }
    }
    """
    resp = session.post(GRAPHQL_URL, json={"query": query, "variables": {"name": name}}, timeout=30)
    resp.raise_for_status()
    body = resp.json()
    if "errors" in body:
        raise RuntimeError(body["errors"])
    result = body["data"]["__type"]
    _type_cache[name] = result
    return result


def unwrap(type_ref):
    """Peel NON_NULL/LIST wrappers, return (named_type, kind)."""
    while type_ref:
        if type_ref["kind"] in ("LIST", "NON_NULL"):
            type_ref = type_ref["ofType"]
        elif type_ref["name"]:
            return type_ref["name"], type_ref["kind"]
        else:
            type_ref = type_ref["ofType"]
    return None, None


def has_required_arg(field):
    for arg in field.get("args") or []:
        if arg.get("defaultValue") is not None:
            continue
        if arg["type"]["kind"] == "NON_NULL":
            return True
    return False


SCALAR_KINDS = {"SCALAR", "ENUM"}
MAX_DEPTH = 6


def build_selection(type_name, depth=0, type_path=(), field_path=(), excluded_paths=frozenset()):
    if depth > MAX_DEPTH or type_name in type_path:
        return ""
    if type_name in LEAF_TYPES:
        return "urn"
    info = introspect_type(type_name)
    if info is None:
        return ""
    if info["kind"] == "UNION":
        parts = []
        for pt in info.get("possibleTypes") or []:
            sub = build_selection(
                pt["name"], depth + 1, type_path + (type_name,), field_path, excluded_paths
            )
            if sub:
                parts.append(f"... on {pt['name']} {{ {sub} }}")
        return " ".join(parts)

    lines = []
    for f in info.get("fields") or []:
        if has_required_arg(f):
            continue
        fname = f["name"]
        this_field_path = field_path + (fname,)
        if this_field_path in excluded_paths:
            continue
        named, kind = unwrap(f["type"])
        if named is None:
            continue
        if kind in SCALAR_KINDS:
            lines.append(fname)
            continue
        sub = build_selection(
            named, depth + 1, type_path + (type_name,), this_field_path, excluded_paths
        )
        if sub:
            lines.append(f"{fname} {{ {sub} }}")
        # else: object-typed field with no viable subselection -> omit entirely,
        # a bare object field with no braces is invalid GraphQL.
    return " ".join(lines)


def _extract_broken_field_path(error):
    """Turn a NullValueInNonNullableField error's `path` into a field-name-only
    tuple relative to the ActionWorkflow root (drops list indices and the
    listActionWorkflows/workflows prefix)."""
    path = tuple(p for p in error.get("path", []) if isinstance(p, str))
    if path[:2] == ("listActionWorkflows", "workflows"):
        path = path[2:]
    return path


def fetch_workflows_resilient():
    excluded_paths = set()
    for attempt in range(6):
        selection = build_selection("ActionWorkflow", excluded_paths=excluded_paths)
        query = f"""
        query listAll($input: ListActionWorkflowsInput!) {{
          listActionWorkflows(input: $input) {{
            total
            workflows {{ {selection} }}
          }}
        }}
        """
        resp = session.post(
            GRAPHQL_URL,
            json={"query": query, "variables": {"input": {"start": 0, "count": 100}}},
            timeout=30,
        )
        resp.raise_for_status()
        body = resp.json()
        errors = body.get("errors") or []
        null_field_errors = [
            e for e in errors if e.get("extensions", {}).get("classification") == "NullValueInNonNullableField"
        ]
        if not errors:
            return body["data"]["listActionWorkflows"]
        if not null_field_errors:
            print(json.dumps(errors, indent=2))
            return None

        new_excludes = {
            fp for e in null_field_errors if (fp := _extract_broken_field_path(e))
        } - excluded_paths
        if not new_excludes:
            print(json.dumps(errors, indent=2))
            return None
        excluded_paths |= new_excludes
        print(f"retry {attempt + 1}: excluding broken null fields {sorted(new_excludes)}")

    print("gave up after repeated retries; last errors:")
    print(json.dumps(errors, indent=2))
    return None


def main():
    result = fetch_workflows_resilient()
    if result is None:
        return
    print(f"total={result['total']} fetched={len(result['workflows'])}")
    # Defaults to /tmp so a casual run cannot clobber the checked-in
    # definitions. Point FETCH_OUT_DIR at workflows/definitions to refresh them
    # deliberately, then diff before committing -- the fetched shape is the
    # live workflow, not necessarily what this repo intends to deploy.
    out_dir = os.environ.get("FETCH_OUT_DIR", "/tmp")
    os.makedirs(out_dir, exist_ok=True)

    # Names are NOT unique -- two workflows called "Data Product Access Request"
    # is a real, current state of the reference instance -- so a filename built
    # from the name alone silently loses every duplicate but the last. The
    # failure is invisible: the loop still prints one "wrote" line per workflow,
    # each with the correct urn, so the log reads like a complete backup while
    # the directory is short. This script is documented as the PRE-DEPLOY
    # BACKUP, which makes a silently partial result worse than none.
    #
    # Collisions are resolved by suffixing the urn, and only for the names that
    # actually collide, so the common case keeps a readable filename.
    slugs = [
        re.sub(r"[^a-zA-Z0-9_-]+", "-", wf["name"]).strip("-").lower() or "workflow"
        for wf in result["workflows"]
    ]
    duplicated = {s for s in slugs if slugs.count(s) > 1}

    written = set()
    for wf, slug in zip(result["workflows"], slugs):
        if slug in duplicated:
            # The urn is the only thing guaranteed unique.
            suffix = re.sub(r"[^a-zA-Z0-9_-]+", "-", wf["urn"].split(":")[-1]).strip("-").lower()
            slug = f"{slug}--{suffix}"
        path = os.path.join(out_dir, f"action-workflow-{slug}.json")
        with open(path, "w") as out:
            json.dump(wf, out, indent=2)
        written.add(path)
        print(f"wrote {path}  (urn={wf['urn']})")

    if duplicated:
        print(
            f"\nNOTE: {len(duplicated)} workflow name(s) are not unique "
            f"({', '.join(sorted(duplicated))}); those files carry a urn suffix."
        )

    # A count check rather than trust: if these ever disagree, a definition was
    # lost and the operator must not treat this directory as a backup.
    if len(written) != len(result["workflows"]):
        print(
            f"\nERROR: fetched {len(result['workflows'])} workflow(s) but wrote "
            f"{len(written)} file(s) -- {len(result['workflows']) - len(written)} "
            f"definition(s) were overwritten. Do not use this as a backup.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main() or 0)
