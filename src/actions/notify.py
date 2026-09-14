"""Email the people who need to act when a request is rejected, via Resend.

DataHub Cloud already notifies the requester natively, but that notice says only
who rejected the request and links to the task centre. It carries neither the
reason nor the submitted values, so a requester has to open the request and
re-read their own form to learn what to change. This sends both.

A stripped version of the full notifier: no Plan, no EmailEffect, no handler
base class, no per-outcome config blocks. Render, resolve, send.

THE ERROR HANDLING IS NOT DECORATION

Each branch in ``_send`` exists because of a specific failure:

* Empty recipient list is refused locally. Resend answers a 422 whose message
  is about the payload, which sends you looking in the wrong place.
* 5xx is transient and retried; 4xx is permanent and is not. Retrying a
  permanent rejection three times just delays the log line that explains it.
* A 4xx logs the status and Resend's ``message`` field only. Its validation
  errors reflect recipient addresses back, and this log goes to stdout.
* An unparseable 2xx is treated as PERMANENT. The POST completed, so Resend
  almost certainly accepted the message; retrying would send it twice.
* Success logs a count, never the addresses. Recipient lists are personal data.
"""

from __future__ import annotations

import hashlib
import html
import logging
import os
import pathlib
import re
from typing import Iterable

import requests

from actions.client import DataHubClient
from actions.errors import NotificationError, NotificationTransientError
from actions.fields import FormFields

logger = logging.getLogger(__name__)

ENDPOINT = "https://api.resend.com/emails"

# Replay is how a decision made while the listener was down still gets acted on,
# and lookback_days replays the whole window on EVERY restart -- not only after
# an outage. create_domain survives that because it converges; sending does not.
# Without a record of what has already gone out, one restart mails every
# decision in the window again.
#
# So each (request, outcome) is marked once it has been sent, and never sent
# twice. The marker is a file rather than an in-process set because the thing
# being defended against is precisely a restart.
DEFAULT_STATE_DIR = pathlib.Path(os.environ.get("FAILED_EVENTS_DIR", "/tmp")) / "notified"
NO_REASON = "No reason was recorded."

# Tags that imply a line break become a space; every other tag is dropped.
# Substituting a space for all of them separates text from the punctuation that
# follows it -- "<b>reporting</b>." renders as "reporting ."
_BREAKING_TAG = re.compile(r"<\s*(br|/p|/div|/li|/tr|/h[1-6])\b[^>]*>", re.I)
_TAG = re.compile(r"<[^>]*>")
_WHITESPACE = re.compile(r"\s+")


# Copy an operator has not supplied. Kept per outcome because an approval that
# reads like a refusal is worse than no mail at all.
DEFAULTS = {
    "ACCEPTED": {
        "subject": "Request approved: {title}",
        "intro": "<p>Your request <strong>{title}</strong> was <strong>approved</strong>.</p>",
        "by_template": "<p>Approved by {by}.</p>",
        "closing": "See the request in DataHub for the full history.",
    },
    "REJECTED": {
        "subject": "Request rejected: {title}",
        "intro": "<p>Your request <strong>{title}</strong> was <strong>rejected</strong>.</p>",
        "by_template": "<p>Rejected by {by}.</p>",
        "closing": "To proceed, correct the details above and submit a new request.",
    },
}

OUTCOME_BLOCKS = ("accepted", "rejected")


def resolve(config: dict, result: str) -> dict:
    """Shared keys, then the matching outcome block, over the built-in defaults.

    Three layers because the useful settings sit at different scopes:
    from_address is per-pipeline, field_labels are per-workflow, and the subject
    has to differ per outcome or an approval reads like a refusal.
    """
    merged = dict(DEFAULTS.get((result or "").upper(), {}))
    merged.update({k: v for k, v in (config or {}).items() if k not in OUTCOME_BLOCKS})
    block = (config or {}).get((result or "").lower()) or {}
    if isinstance(block, dict):
        merged.update(block)
    return merged


def send_notice(
    client: DataHubClient, fields: FormFields, request: dict, config: dict, result: str
) -> None:
    """Render and send the outcome notice.

    Config keys: ``from_address`` (required), ``request_base_url``,
    ``field_labels`` (ordered {id: label}), ``owner_fields``, ``subject``,
    ``title_field``, ``intro``, ``closing``, ``by_template`` -- any of which may
    also sit under an ``accepted:``/``rejected:`` block to apply to one outcome.
    ``RESEND_API_KEY`` comes from the environment.
    """
    config = resolve(config, result)
    api_key = os.environ.get("RESEND_API_KEY", "").strip()
    from_address = (config.get("from_address") or "").strip()
    if not api_key:
        raise NotificationError(
            "RESEND_API_KEY is not set; either set it or route this workflow's "
            "rejected outcome to 'log'"
        )
    if not from_address:
        raise NotificationError("notify.from_address is required to send mail")

    title = _title(config, fields, request)
    requester = ((request.get("created") or {}).get("actor") or {}).get("urn")
    owners = [
        urn for field_id in config.get("owner_fields") or [] for urn in fields.all(field_id)
    ]

    recipients, unresolved = client.resolve_principals(
        [urn for urn in [requester, *owners] if urn]
    )
    if not recipients:
        # Nothing to send to is not an error -- it is a governance gap worth
        # surfacing, but failing here would retry three times and still find
        # nobody.
        logger.warning(
            "%s request %s has no resolvable recipients (%d principal(s) had no "
            "address); no notification sent",
            result,
            request.get("urn"),
            len(unresolved),
        )
        return

    request_urn = request.get("urn") or ""
    subject = config["subject"].format(title=title)
    body = None  # rendered lazily, only if there is someone left to tell

    # One send per recipient, not one send to all of them.
    #
    # Resend rejects the ENTIRE call when any single address is unacceptable, so
    # a batch send means one undeliverable co-owner costs everyone else their
    # notice -- including the requester, who is the person the mail exists for.
    # Seen live: a rejection whose technical_owners was a group of DataHub
    # sample users took down a notice that would have reached the requester.
    #
    # It also stops recipients seeing each other's addresses, which a governance
    # notice has no reason to disclose.
    pending, already = [], 0
    for r in recipients:
        if _marker(config, request_urn, result, r.email).exists():
            already += 1
        else:
            pending.append(r)

    if not pending:
        logger.info(
            "Already notified all %d recipient(s) for %s (%s); nothing to send",
            already, request_urn, result,
        )
        return

    body = _render(title, request, fields, config, result)
    sent: int = 0
    transient: list = []
    undeliverable: list = []
    for r in pending:
        try:
            _send(
                api_key=api_key,
                from_address=from_address,
                to=[r.email],
                subject=subject,
                html_body=body,
            )
        except NotificationTransientError as exc:
            # Worth another attempt later, so nothing is recorded.
            transient.append((r.email, exc))
            continue
        except NotificationError as exc:
            # Permanent for this address -- a 422 for an undeliverable domain
            # will answer 422 forever. Recorded so replay stops re-attempting
            # it, with a reason, because an unexplained marker is worse than
            # the noise it replaces.
            _mark_undeliverable(
                _marker(config, request_urn, result, r.email), request_urn, result, r.email, exc
            )
            undeliverable.append((r.email, exc))
            continue
        _mark_sent(_marker(config, request_urn, result, r.email), request_urn, result, r.email)
        sent += 1

    logger.info(
        "Notified %d recipient(s) for %s (%s)%s%s%s",
        sent, request_urn, result,
        f", {already} already had it" if already else "",
        f", {len(undeliverable)} undeliverable" if undeliverable else "",
        f", {len(transient)} to retry" if transient else "",
    )

    if undeliverable:
        # A warning, not a raise. Retrying cannot help, and raising would make
        # the framework attempt the whole event three more times for nothing --
        # which is what buried the one recipient who DID get the mail under
        # four stack traces. Addresses are grouped by reason so six identical
        # 422s read as one line.
        by_reason: dict[str, list[str]] = {}
        for email, exc in undeliverable:
            by_reason.setdefault(str(exc), []).append(email)
        for reason, emails in by_reason.items():
            logger.warning(
                "Could not notify %d recipient(s) for %s (%s) and will not retry: %s -- %s",
                len(emails), request_urn, result, ", ".join(sorted(emails)), reason,
            )

    if transient:
        # Raised so the framework retries -- these may yet succeed, and the
        # recipients already served are recorded, so a retry will not double-send.
        raise NotificationTransientError(
            f"{len(transient)} of {len(pending)} recipient(s) failed temporarily for "
            f"{request_urn} ({result}): "
            + "; ".join(f"{email}: {exc}" for email, exc in transient)
        )


def plain(value: str) -> str:
    """A submitted value as safe, readable text.

    RICH_TEXT fields arrive as HTML. Escaping wholesale shows the reader literal
    ``&lt;p&gt;``; rendering trusts attacker-influenced input in mail sent to
    owners. So strip the tags, then escape what is left -- markup is discarded
    rather than rendered, and text inside a stripped <script> survives only as
    inert escaped text.
    """
    without_tags = _TAG.sub("", _BREAKING_TAG.sub(" ", value or ""))
    # Entities first, so "&lt;b&gt;" in the source cannot survive the strip and
    # be re-emitted as live markup by the escape below.
    unescaped = html.unescape(without_tags)
    return html.escape(_WHITESPACE.sub(" ", unescaped).strip())


def _title(config: dict, fields: FormFields, request: dict) -> str:
    field_id = config.get("title_field")
    if field_id:
        value = (fields.one(field_id) or "").strip()
        if value:
            return value
    # No title field, or it was left blank: fall back to the entity the request
    # was raised against. A dataset-access form has no name field at all.
    entity = request.get("entity") or {}
    return (entity.get("name") or "").strip() or entity.get("urn") or "(untitled)"


def _render(
    title: str, request: dict, fields: FormFields, config: dict, result: str
) -> str:
    """``field_labels`` drives both which fields appear and their order.

    A field the operator has not labelled is omitted rather than shown with a
    raw id, so the form can grow fields without leaking them into email.
    """
    rows = []
    for field_id, label in (config.get("field_labels") or {}).items():
        values = fields.all(field_id)
        if not values:
            continue
        rendered = ", ".join(plain(v) for v in values)
        rows.append(
            f"<tr><td style='padding:4px 12px 4px 0;vertical-align:top'>"
            f"<strong>{html.escape(label)}</strong></td>"
            f"<td style='padding:4px 0'>{rendered}</td></tr>"
        )

    reason = (request.get("resultNote") or "").strip() or NO_REASON
    by = decided_by(request, result)
    by_line = config["by_template"].format(by=html.escape(by)) if by else ""

    base_url = (config.get("request_base_url") or "").rstrip("/")
    link = (
        f"<p><a href='{html.escape(base_url)}/actionrequests/"
        f"{html.escape(request.get('urn') or '')}'>View the request in DataHub</a></p>"
        if base_url
        else ""
    )

    return (
        f"{config['intro'].format(title=html.escape(title))}"
        f"<p><strong>Reason:</strong> {html.escape(reason)}</p>"
        f"{by_line}"
        f"<h3>Submitted details</h3><table>{''.join(rows)}</table>"
        f"{link}"
        f"<p>{config['closing']}</p>"
    )


def decided_by(request: dict, result: str) -> str:
    """The actor whose decision produced *result*.

    Filtering by result is load-bearing in both directions. A chain ending in
    rejection still carries ACCEPTED decisions from the gates that passed, so an
    unfiltered scan credits the wrong person with the refusal. And an approval
    passes several gates, where the one that completed the request is the LAST
    -- so the two outcomes read from opposite ends.
    """
    form = (request.get("params") or {}).get("workflowFormRequest") or {}
    wanted = (result or "").upper()
    urns = [
        (d.get("decidedBy") or {}).get("urn")
        for d in form.get("decisions") or []
        if (d.get("result") or "").upper() == wanted
    ]
    urns = [u for u in urns if u]
    if not urns:
        return ""
    return urns[0] if wanted == "REJECTED" else urns[-1]


def _send(
    *, api_key: str, from_address: str, to: Iterable[str], subject: str, html_body: str
) -> str | None:
    recipients = list(to)
    if not recipients:
        raise NotificationError("Refusing to send with no recipients")

    try:
        response = requests.post(
            ENDPOINT,
            json={"from": from_address, "to": recipients, "subject": subject, "html": html_body},
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            timeout=10,
        )
    except requests.RequestException as exc:
        raise NotificationTransientError(f"Request to {ENDPOINT} failed: {exc}") from exc

    if response.status_code >= 500:
        raise NotificationTransientError(f"Resend returned {response.status_code}")
    if response.status_code >= 400:
        try:
            detail = response.json().get("message", "")
        except Exception:
            detail = ""
        raise NotificationError(
            f"Resend returned {response.status_code}{f': {detail}' if detail else ''}"
        )

    try:
        body = response.json()
    except Exception as exc:
        raise NotificationError(
            f"Resend returned a non-JSON 2xx (status={response.status_code}); "
            "the send probably succeeded -- not retrying to avoid a duplicate"
        ) from exc

    message_id = body.get("id")
    logger.info("Sent notification %s to %d recipient(s)", message_id, len(recipients))
    return message_id


def _marker(config: dict, request_urn: str, result: str, address: str = "") -> pathlib.Path:
    """One path per (request, outcome, recipient).

    Per recipient rather than per request because a send can now partly succeed:
    one co-owner may be undeliverable while the requester is fine. Keying on the
    request alone would either re-mail the people who already got it, or mark
    the whole thing done and never retry the one who did not.

    Hashed because an action-request urn contains characters a filename cannot
    carry, and because dataset urns are long enough to exceed the limit. The urn
    is written inside the file so the directory stays readable by a human.
    """
    state_dir = pathlib.Path(config.get("state_dir") or DEFAULT_STATE_DIR)
    key = hashlib.sha256(
        f"{request_urn}|{result.upper()}|{address.lower()}".encode()
    ).hexdigest()[:32]
    return state_dir / f"{key}.sent"


def _mark_sent(marker: pathlib.Path, request_urn: str, result: str, address: str = "") -> None:
    """Record a delivery, after it succeeded.

    A failure here means the mail went out but was not recorded, so a later
    replay sends it twice. That is the safe direction to fail -- the alternative
    is marking first and losing a notification to a transient write error -- but
    it is logged loudly rather than passed over.
    """
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(f"{request_urn}\n{result.upper()}\n{address}\n")
    except OSError as exc:
        logger.error(
            "Sent the notification for %s (%s) but could not record it at %s: %s. "
            "A replay will send it again.",
            request_urn, result, marker, exc,
        )


def _mark_undeliverable(
    marker: pathlib.Path, request_urn: str, result: str, address: str, reason: Exception
) -> None:
    """Record an address that can never receive this notice.

    Written to the same store as a delivery because the effect wanted is the
    same -- do not attempt this again -- but labelled differently so the
    directory distinguishes "they got it" from "they never will".
    """
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(
            f"{request_urn}\n{result.upper()}\n{address}\nUNDELIVERABLE\nreason: {reason}\n"
        )
    except OSError as exc:
        logger.error("Could not record the undeliverable address %s: %s", address, exc)
