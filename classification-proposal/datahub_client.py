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

import yaml

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
    except (IndexError, KeyError, ValueError, binascii.Error) as exc:
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
