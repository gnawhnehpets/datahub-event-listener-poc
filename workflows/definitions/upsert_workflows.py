"""Upsert DataHub Action Workflow definitions via the upsertActionWorkflow
GraphQL mutation, either in batch or one at a time.

Batch mode reads every file matching --pattern in --dir (default: *_v2.json,
each shaped as {"input": <UpsertActionWorkflowInput>}). Pass --file to upsert
just one. See https://docs.datahub.com/docs/managed-datahub/workflows/action-workflows-reference
for the full input schema.

Usage:
    # batch
    python upsert_workflows.py --server https://.trials.acryl.io --dir .

    # single file
    python upsert_workflows.py --server https://[DATAHUB-SUBDOMAIN].trials.acryl.io \\
        --file action-workflow-propose-new-glossary-term_v2.json

    # print the equivalent curl command(s) instead of calling the API
    python upsert_workflows.py --server https://[DATAHUB-SUBDOMAIN].trials.acryl.io \\
        --file action-workflow-propose-new-glossary-term_v2.json --print-curl

Generate the token from the target instance's UI: Settings > Access Tokens >
Generate New Token. Then either:
    export DATAHUB_TOKEN=<token>   # picked up via --token-env (default)
or pass --token <token> directly (visible in shell history/ps — avoid on shared machines).
"""

import argparse
import glob
import json
import logging
import os
import re
import shlex
import sys

import requests

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

UPSERT_MUTATION = """
mutation upsertActionWorkflow($input: UpsertActionWorkflowInput!) {
  upsertActionWorkflow(input: $input) {
    urn
  }
}
"""

# Field sets per GraphQL input type, taken from live introspection against
# [DATAHUB-SUBDOMAIN].acryl.io (GMS v2.1.0-cloud). Older GMS versions (e.g. v1.1.3-cloud
# on the trial) may lack some of these fields entirely -- used to identify
# which dict in the payload a "field X not defined for type Y" error refers
# to, so it can be stripped and the request retried.
INPUT_TYPE_SIGNATURES = {
    "UpsertActionWorkflowInput": {"urn", "name", "category", "customCategory", "description", "trigger", "steps"},
    "ActionWorkflowTriggerInput": {"type", "form"},
    "ActionWorkflowFormInput": {"entrypoints", "entityTypes", "fields", "excludedDefaultFields"},
    "ActionWorkflowEntrypointInput": {"type", "label", "filter"},
    "ActionWorkflowFieldInput": {
        "id", "name", "description", "valueType", "allowedEntityTypes", "allowedValues",
        "cardinality", "required", "condition", "filterCondition", "valuesSource",
        "validation", "dynamicSource",
    },
    "ActionWorkflowFieldConditionInput": {"type", "singleFieldValueCondition"},
    "ActionWorkflowSingleFieldValueConditionInput": {"field", "values", "condition", "negated"},
    "FieldValidationInput": {"pattern", "errorMessage"},
    "PropertyValueInput": {"stringValue", "numberValue"},
    "DataHubViewFilterInput": {"operator", "filters"},
    "FacetFilterInput": {"field", "values", "negated", "condition"},
    "ActionWorkflowStepInput": {"id", "type", "description", "actors", "condition", "quorum"},
    "ActionWorkflowStepActorsInput": {"userUrns", "groupUrns", "roleUrns", "dynamicAssignment", "dynamicSource"},
    "ActionWorkflowStepDynamicAssignmentInput": {"type", "ownershipTypeUrns"},
    "UpsertDynamicSourceInput": {"resolvers", "filter"},
    "DynamicResolverInput": {"resolver", "source"},
    "QuorumPolicyInput": {"anyOf", "allOf", "nofM"},
    "NOfMInput": {"n"},
}

_UNKNOWN_FIELD_RE = re.compile(
    r"field name '(?P<field>\w+)' that is not defined for input object type '(?P<type>\w+)'"
)


def load_payload_file(path: str) -> dict:
    with open(path) as f:
        data = json.load(f)
    if "input" not in data:
        raise ValueError(f"{path} is missing the top-level 'input' key")
    return data


def load_payloads(directory: str, pattern: str) -> list[tuple[str, dict]]:
    paths = sorted(glob.glob(os.path.join(directory, pattern)))
    return [(path, load_payload_file(path)) for path in paths]


def build_curl_command(graphql_url: str, variables: dict, token_env: str) -> str:
    """Render the exact equivalent curl command for a single upsert, using
    $<token_env> as a placeholder rather than embedding the real token."""
    body = json.dumps({"query": UPSERT_MUTATION, "variables": variables})
    return (
        f"curl -X POST {shlex.quote(graphql_url)} \\\n"
        f'  -H "Authorization: Bearer ${token_env}" \\\n'
        f'  -H "Content-Type: application/json" \\\n'
        f"  -d {shlex.quote(body)}"
    )


def strip_unsupported_field(payload: dict, field: str, type_name: str) -> bool:
    """Remove `field` from every dict in `payload` whose keys are consistent
    with `type_name`'s known signature. Returns True if anything was stripped."""
    signature = INPUT_TYPE_SIGNATURES.get(type_name)
    stripped = False

    def walk(node):
        nonlocal stripped
        if isinstance(node, dict):
            if signature is not None and field in node and set(node.keys()) <= signature:
                del node[field]
                stripped = True
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(payload)
    return stripped


def upsert_workflow(session: requests.Session, graphql_url: str, variables: dict) -> tuple[str | None, list[dict]]:
    response = session.post(
        graphql_url, json={"query": UPSERT_MUTATION, "variables": variables}, timeout=30
    )
    response.raise_for_status()
    body = response.json()
    if "errors" in body:
        return None, body["errors"]
    return body["data"]["upsertActionWorkflow"]["urn"], []


def upsert_with_schema_healing(
    session: requests.Session, graphql_url: str, variables: dict, name: str, max_attempts: int = 8
) -> str:
    for attempt in range(1, max_attempts + 1):
        urn, errors = upsert_workflow(session, graphql_url, variables)
        if urn is not None:
            return urn

        matches = [m for e in errors for m in [_UNKNOWN_FIELD_RE.search(e.get("message", ""))] if m]
        if not matches:
            raise RuntimeError(json.dumps(errors, indent=2))

        any_stripped = False
        for m in matches:
            field, type_name = m.group("field"), m.group("type")
            if strip_unsupported_field(variables, field, type_name):
                any_stripped = True
                logger.warning(
                    "%-45s: target GMS version lacks '%s' on %s -- dropping it and retrying (attempt %d)",
                    name, field, type_name, attempt,
                )
        if not any_stripped:
            raise RuntimeError(json.dumps(errors, indent=2))

    raise RuntimeError(f"Gave up after {max_attempts} schema-healing attempts")


def resolve_token(args: argparse.Namespace) -> str:
    if args.token_file:
        with open(args.token_file) as f:
            return f.read().strip()
    if args.token:
        logger.warning("Passing --token directly may leak it into shell history/ps output.")
        return args.token
    token = os.environ.get(args.token_env)
    if not token:
        raise SystemExit(
            f"No token found: set ${args.token_env}, or pass --token/--token-file."
        )
    return token


def main() -> int:
    parser = argparse.ArgumentParser(description="Batch upsert DataHub Action Workflow definitions.")
    parser.add_argument(
        "--server",
        required=True,
        help="DataHub frontend URL, e.g. https://[DATAHUB-SUBDOMAIN].trials.acryl.io",
    )
    parser.add_argument("--token", help="Personal access token (PAT). Prefer --token-env or --token-file.")
    parser.add_argument("--token-file", help="Path to a file containing the PAT.")
    parser.add_argument(
        "--token-env",
        default="DATAHUB_TOKEN",
        help="Env var to read the PAT from (default: DATAHUB_TOKEN).",
    )
    parser.add_argument("--dir", default=".", help="Directory containing payload files (batch mode).")
    parser.add_argument("--pattern", default="*_v2.json", help="Glob pattern for payload files (batch mode).")
    parser.add_argument(
        "--file", help="Upsert a single payload file instead of batch-processing --dir/--pattern."
    )
    parser.add_argument("--dry-run", action="store_true", help="List what would be sent without calling the API.")
    parser.add_argument(
        "--print-curl",
        action="store_true",
        help="Print the equivalent curl command for each selected file instead of calling the API.",
    )
    args = parser.parse_args()

    token = resolve_token(args)
    graphql_url = args.server.rstrip("/").removesuffix("/gms") + "/api/v2/graphql"

    if args.file:
        payloads = [(args.file, load_payload_file(args.file))]
    else:
        payloads = load_payloads(args.dir, args.pattern)
    if not payloads:
        logger.error("No files matched %s in %s", args.pattern, args.dir)
        return 1

    if args.print_curl:
        for path, variables in payloads:
            name = variables.get("input", {}).get("name", path)
            logger.info("# %s (%s)", name, path)
            print(build_curl_command(graphql_url, variables, args.token_env))
            print()
        return 0

    session = requests.Session()
    session.headers.update({"Authorization": f"Bearer {token}", "Content-Type": "application/json"})

    failures = []
    for path, variables in payloads:
        name = variables.get("input", {}).get("name", path)
        if args.dry_run:
            logger.info("[dry-run] would upsert %-45s (%s)", name, path)
            continue
        try:
            urn = upsert_with_schema_healing(session, graphql_url, variables, name)
            logger.info("upserted %-45s -> %s", name, urn)
        except Exception as exc:
            logger.error("FAILED %-45s (%s): %s", name, path, exc)
            failures.append(path)

    if args.dry_run:
        return 0
    if failures:
        logger.error("%d/%d upserts failed: %s", len(failures), len(payloads), failures)
        return 1
    logger.info("All %d workflow(s) upserted successfully.", len(payloads))
    return 0


if __name__ == "__main__":
    sys.exit(main())
