"""Credential loading and GraphQL transport for the proposal script.

Reads the standard DataHub CLI config rather than taking an acryl-datahub
dependency just to parse six lines of YAML.

The token's exp claim is decoded locally before anything is written. An
expired credential is the most likely failure here and a bare 401 from the
first mutation is a poor way to discover it.
"""

from __future__ import annotations

import base64
import binascii
import datetime as dt
import json
import logging
import os
import pathlib
from dataclasses import dataclass

import requests
import yaml

from proposals import Proposal

logger = logging.getLogger(__name__)

DEFAULT_ENV_FILE = "~/.datahubenv_prolemur"


class CredentialError(Exception):
    """Config is missing, malformed, or the token has expired."""


@dataclass(frozen=True)
class Credentials:
    graphql_url: str
    token: str


def graphql_url_from_server(server: str) -> str:
    """Turn the GMS server URL into the frontend GraphQL endpoint.

    Mutations are served by the frontend proxy at /api/graphql, not by
    /gms/graphql, which rejects these tokens with a 401.
    """
    return server.rstrip("/").removesuffix("/gms") + "/api/graphql"


def token_expiry(token: str) -> dt.datetime:
    """Read the exp claim. The signature is not verified -- the server does that."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        return dt.datetime.fromtimestamp(claims["exp"], dt.timezone.utc)
    except (IndexError, KeyError, TypeError, ValueError, binascii.Error) as exc:
        raise CredentialError(f"could not read exp claim from token: {exc}") from exc


def load_credentials(
    path: str | pathlib.Path | None = None,
    now: dt.datetime | None = None,
) -> Credentials:
    """Load and validate credentials, or raise CredentialError."""
    raw = path or os.environ.get("DATAHUB_ENV_FILE") or DEFAULT_ENV_FILE
    resolved = pathlib.Path(raw).expanduser()
    if not resolved.is_file():
        raise CredentialError(f"credentials file not found: {resolved}")

    try:
        gms = (yaml.safe_load(resolved.read_text()) or {})["gms"]
        server, token = gms["server"], gms["token"]
    except (KeyError, TypeError, yaml.YAMLError) as exc:
        raise CredentialError(f"{resolved} is missing gms.server or gms.token: {exc}") from exc
    if not token:
        raise CredentialError(f"{resolved} has an empty gms.token")

    expires = token_expiry(token)
    moment = now or dt.datetime.now(dt.timezone.utc)
    if expires <= moment:
        raise CredentialError(
            f"token in {resolved} expired {expires:%Y-%m-%d %H:%M:%S UTC}. "
            f"Generate a new Personal Access Token in DataHub "
            f"(Settings -> Access Tokens) and replace gms.token."
        )

    logger.info("credentials valid until %s", f"{expires:%Y-%m-%d %H:%M:%S UTC}")
    return Credentials(graphql_url=graphql_url_from_server(server), token=token)


_COLUMNS_QUERY = """
query datasetColumns($urn: String!) {
  dataset(urn: $urn) {
    schemaMetadata { fields { fieldPath } }
  }
}
"""

_PROPOSE_TAGS = """
mutation proposeTags($input: ProposeTagsInput!) {
  proposeTags(input: $input)
}
"""


class DataHubError(Exception):
    """A GraphQL call failed or returned errors."""


class DataHubClient:
    """Only the two operations this script needs."""

    def __init__(self, creds: Credentials) -> None:
        self._url = creds.graphql_url
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"Bearer {creds.token}",
                "Content-Type": "application/json",
            }
        )

    def _execute(self, query: str, variables: dict) -> dict:
        try:
            response = self._session.post(
                self._url, json={"query": query, "variables": variables}, timeout=30
            )
        except requests.RequestException as exc:
            raise DataHubError(f"request failed: {exc}") from exc

        if response.status_code == 401:
            raise DataHubError("401 unauthorized -- the token was rejected")
        if response.status_code >= 400:
            raise DataHubError(f"HTTP {response.status_code}: {response.text[:300]}")

        body = response.json()
        if body.get("errors"):
            raise DataHubError("; ".join(e.get("message", str(e)) for e in body["errors"]))
        return body["data"]

    def fetch_columns(self, dataset_urn: str) -> list[str]:
        """Live fieldPaths, so the script does not drift as the POC data changes."""
        data = self._execute(_COLUMNS_QUERY, {"urn": dataset_urn})
        dataset = data.get("dataset")
        if not dataset or not dataset.get("schemaMetadata"):
            raise DataHubError(f"no schema found for {dataset_urn}")
        return [f["fieldPath"] for f in dataset["schemaMetadata"]["fields"]]

    def propose_tag(self, proposal: Proposal) -> str:
        """File one proposal. Returns the created actionRequest URN.

        origin INFERRED requires inferenceMetadata, which is where the native
        confidence bucket goes. The precise score travels in `description`,
        documented by the schema as "also called proposalNote".
        """
        payload: dict = {
            "resourceUrn": proposal.resource_urn,
            "tagUrns": [proposal.tag_urn],
            "description": proposal.note,
            "origin": "INFERRED",
            "inferenceMetadata": {
                "version": 1,
                "confidenceLevel": proposal.confidence_level,
            },
        }
        # Omitting both sub-resource fields is what makes this asset-level.
        if proposal.sub_resource is not None:
            payload["subResourceType"] = "DATASET_FIELD"
            payload["subResource"] = proposal.sub_resource

        return self._execute(_PROPOSE_TAGS, {"input": payload})["proposeTags"]
