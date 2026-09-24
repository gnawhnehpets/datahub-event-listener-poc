"""Credential parsing and expiry checks.

Pure: no network. The point of testing this at all is that an expired token
must fail before the first write, not as a bare 401 midway through a batch.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import pathlib

import pytest
import yaml

from datahub_client import (
    CredentialError,
    graphql_url_from_server,
    load_credentials,
    token_expiry,
)


def make_token(exp: dt.datetime) -> str:
    """A JWT with only the fields the loader reads. Signature is never checked."""
    header = base64.urlsafe_b64encode(b'{"alg":"HS256"}').decode().rstrip("=")
    body = json.dumps({"actorId": "stephen.hwang@datahub.com", "exp": int(exp.timestamp())})
    payload = base64.urlsafe_b64encode(body.encode()).decode().rstrip("=")
    return f"{header}.{payload}.signature"


def write_env(tmp_path: pathlib.Path, token: str) -> pathlib.Path:
    path = tmp_path / "datahubenv"
    path.write_text(
        yaml.safe_dump(
            {"gms": {"server": "https://pro-lemur-fda34e845.trials.acryl.io/gms", "token": token}}
        )
    )
    return path


def test_graphql_url_replaces_gms_suffix() -> None:
    assert (
        graphql_url_from_server("https://pro-lemur-fda34e845.trials.acryl.io/gms")
        == "https://pro-lemur-fda34e845.trials.acryl.io/api/graphql"
    )


def test_graphql_url_tolerates_trailing_slash() -> None:
    assert (
        graphql_url_from_server("https://x.acryl.io/gms/")
        == "https://x.acryl.io/api/graphql"
    )


def test_token_expiry_is_utc_aware() -> None:
    when = dt.datetime(2026, 9, 2, 20, 9, 27, tzinfo=dt.timezone.utc)
    got = token_expiry(make_token(when))
    assert got == when
    assert got.tzinfo is not None


def test_valid_token_loads(tmp_path: pathlib.Path) -> None:
    now = dt.datetime(2026, 9, 23, tzinfo=dt.timezone.utc)
    path = write_env(tmp_path, make_token(now + dt.timedelta(days=1)))
    creds = load_credentials(path, now=now)
    assert creds.graphql_url.endswith("/api/graphql")
    assert creds.token


def test_expired_token_is_rejected_with_its_date(tmp_path: pathlib.Path) -> None:
    now = dt.datetime(2026, 9, 23, tzinfo=dt.timezone.utc)
    expired = dt.datetime(2026, 9, 2, 20, 9, 27, tzinfo=dt.timezone.utc)
    path = write_env(tmp_path, make_token(expired))
    with pytest.raises(CredentialError) as err:
        load_credentials(path, now=now)
    assert "2026-09-02" in str(err.value)


def test_missing_file_names_the_path(tmp_path: pathlib.Path) -> None:
    missing = tmp_path / "nope.yaml"
    with pytest.raises(CredentialError) as err:
        load_credentials(missing)
    assert "nope.yaml" in str(err.value)


def test_malformed_yaml_is_rejected(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text("gms:\n  server: https://x/gms\n")  # no token key
    with pytest.raises(CredentialError):
        load_credentials(path)


def test_unparseable_token_is_rejected(tmp_path: pathlib.Path) -> None:
    path = write_env(tmp_path, "not-a-jwt")
    with pytest.raises(CredentialError):
        load_credentials(path)


def test_token_with_string_exp_claim_raises_credential_error(tmp_path: pathlib.Path) -> None:
    """A malformed token with exp as a string (not numeric) raises CredentialError."""
    header = base64.urlsafe_b64encode(b'{"alg":"HS256"}').decode().rstrip("=")
    body = json.dumps({"actorId": "stephen.hwang@datahub.com", "exp": "2026-09-02"})
    payload = base64.urlsafe_b64encode(body.encode()).decode().rstrip("=")
    token = f"{header}.{payload}.signature"
    path = write_env(tmp_path, token)
    with pytest.raises(CredentialError):
        load_credentials(path)


@pytest.mark.parametrize(
    "body_dict",
    [
        {"actorId": "stephen.hwang@datahub.com"},  # exp missing
        {"actorId": "stephen.hwang@datahub.com", "exp": [2026, 9, 2]},  # exp as list
    ],
)
def test_token_with_missing_or_malformed_exp(tmp_path: pathlib.Path, body_dict: dict) -> None:
    """Tokens with missing or non-numeric exp raise CredentialError."""
    header = base64.urlsafe_b64encode(b'{"alg":"HS256"}').decode().rstrip("=")
    body = json.dumps(body_dict)
    payload = base64.urlsafe_b64encode(body.encode()).decode().rstrip("=")
    token = f"{header}.{payload}.signature"
    path = write_env(tmp_path, token)
    with pytest.raises(CredentialError):
        load_credentials(path)


def test_token_payload_valid_base64_but_invalid_json(tmp_path: pathlib.Path) -> None:
    """Payload that decodes to non-JSON raises CredentialError."""
    header = base64.urlsafe_b64encode(b'{"alg":"HS256"}').decode().rstrip("=")
    # Valid base64, but not valid JSON
    payload = base64.urlsafe_b64encode(b"this is not json").decode().rstrip("=")
    token = f"{header}.{payload}.signature"
    path = write_env(tmp_path, token)
    with pytest.raises(CredentialError):
        load_credentials(path)


def test_token_payload_json_but_not_object(tmp_path: pathlib.Path) -> None:
    """Payload that is JSON but not an object (e.g., array or string) raises CredentialError."""
    header = base64.urlsafe_b64encode(b'{"alg":"HS256"}').decode().rstrip("=")
    # Valid JSON, but not a dict
    payload = base64.urlsafe_b64encode(b'["not", "a", "dict"]').decode().rstrip("=")
    token = f"{header}.{payload}.signature"
    path = write_env(tmp_path, token)
    with pytest.raises(CredentialError):
        load_credentials(path)
