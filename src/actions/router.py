"""A minimal custom DataHub Action that routes Action Workflow decisions.

Written against https://docs.datahub.com/docs/actions/guides/developing-an-action
-- the framework asks an Action for three things and nothing else: ``create``
to build it from config, ``act`` per event, ``close`` on shutdown.

WHY THIS EXISTS

A config-only pipeline cannot tell which workflow a decision belongs to. The
``actionRequestStatus`` aspect carries status, result, note and lastModified --
and no workflowUrn. That identity lives on the actionRequest entity, so reading
it takes one GraphQL call, and one GraphQL call takes code. This file is that
code and, deliberately, almost nothing else.

WHAT IT DOES

Resolves the workflow, then dispatches on the outcome. Configured actions:

    log            record the decision and the submitted values (default)
    create_domain  create the proposed domain -- see actions/domain.py

WHAT IT IS NOT

The full listener on dev/actions-config-minimal adds a handler registry, a Plan
of create/enrich/effect steps, auto-discovery and email notifiers. None of that
is here: dispatch is a dict lookup, and the domain action runs its mutations
directly.

It does import actions for the shared library -- the GraphQL client,
form-field parsing, and the hierarchy and domain-code rules. Those are the parts
that must not be reimplemented; they encode the taxonomy invariants.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datahub_actions.action.action import Action
from datahub_actions.event.event_envelope import EventEnvelope
from datahub_actions.pipeline.pipeline_context import PipelineContext

from actions.domain import create_domain
from actions.notify import send_notice
from actions.client import DataHubClient
from actions.errors import DataHubError
from actions.fields import FormFields

logger = logging.getLogger(__name__)

# A decision event can arrive before the write behind it is readable. Measured
# on a live instance: four framework retries fired 468ms apart and every one saw
# a null request, so retry_count cannot absorb this -- it has no backoff. The
# wait belongs here, where it can actually be longer than the lag.
READ_ATTEMPTS = 5
READ_DELAY_SECONDS = 2

COMPLETED = "COMPLETED"
PENDING = "PENDING"
ROUTABLE_RESULTS = ("ACCEPTED", "REJECTED")

# What a workflow outcome can be mapped to. Deliberately a closed set: an
# unknown name fails at startup rather than silently doing nothing on the first
# real approval.
ACTIONS = ("log", "create_domain", "notify_approval", "notify_rejection")


class WorkflowRouterAction(Action):
    """Resolve the workflow behind a decision, then dispatch on the outcome.

    Config::

        workflows:
          "urn:li:actionWorkflow:propose-new-domain":
            label: Propose New Domain
            accepted: create_domain      # default: log
            rejected: log
          "urn:li:actionWorkflow:other": Some Other Workflow   # short form: log both

    ``workflows`` is both a filter and a naming table -- a decision from a
    workflow not listed is ignored. Omit it to log every workflow, which is what
    you want before you know the URNs.

    ``server``/``token`` override DATAHUB_GMS_URL / DATAHUB_GMS_TOKEN. Prefer the
    environment; a token in a committed YAML is a leak.
    """

    @classmethod
    def create(cls, config_dict: dict, ctx: PipelineContext) -> "Action":
        config = config_dict or {}

        server = (config.get("server") or os.environ.get("DATAHUB_GMS_URL", "")).strip()
        token = (config.get("token") or os.environ.get("DATAHUB_GMS_TOKEN", "")).strip()
        if not server or not token:
            # Fail at startup rather than on the first real decision, which
            # would be retried three times for a permanent configuration error.
            raise ValueError(
                "WorkflowRouterAction needs DATAHUB_GMS_URL and DATAHUB_GMS_TOKEN "
                "in the environment, or 'server' and 'token' in the action config"
            )

        workflows = _normalize(config.get("workflows") or {})
        notify = config.get("notify") or {}
        # Costs one read per PENDING event, for every workflow on the instance,
        # since the event does not say which workflow it belongs to. Off makes
        # the listener silent until a request is decided.
        log_progress = config.get("log_progress", True)

        # Fail at startup if a workflow routes to notify_rejection without the
        # means to send, rather than on the first real rejection -- which would
        # be retried three times for a permanent configuration error, while the
        # person waiting on the email gets nothing and no clear reason.
        sending = [
            (urn, result)
            for urn, spec in workflows.items()
            for result in ROUTABLE_RESULTS
            if spec[result].startswith("notify_")
        ]
        if sending:
            where = ", ".join(f"{urn} on {result}" for urn, result in sending)
            if not os.environ.get("RESEND_API_KEY", "").strip():
                raise ValueError(
                    f"these route to an email action but RESEND_API_KEY is not set "
                    f"({where}); set it, or route those outcomes to 'log'"
                )
            # from_address may come from the shared block or a per-workflow one,
            # so check the merged view rather than either alone.
            for urn, result in sending:
                merged = {**notify, **(workflows[urn]["notify"] or {})}
                block = merged.get(result.lower()) or {}
                if not ((block.get("from_address") or merged.get("from_address") or "").strip()):
                    raise ValueError(
                        f"{urn} routes {result.lower()} to an email action but no "
                        f"from_address is set, in either action.config.notify or "
                        f"the workflow's own notify block"
                    )

        return cls(client=DataHubClient(server=server, token=token),
                   workflows=workflows, notify=notify, ctx=ctx,
                   log_progress=log_progress)

    def __init__(self, client: DataHubClient, workflows: dict, notify: dict,
                 ctx: PipelineContext, log_progress: bool = True):
        self.ctx = ctx
        self.client = client
        self.workflows = workflows
        self.notify = notify
        self.log_progress = log_progress

        if self.workflows:
            for urn, spec in sorted(self.workflows.items()):
                logger.info(
                    "Routing %s (%s) on ACCEPTED -> %s, REJECTED -> %s",
                    urn, spec["label"], spec["ACCEPTED"], spec["REJECTED"],
                )
        else:
            logger.info("No 'workflows' configured; logging decisions from every workflow")

    # ---------------------------------------------------------------- framework

    def act(self, event: EventEnvelope) -> None:
        aspect = getattr(event.event, "aspect", None)
        if aspect is None or not getattr(aspect, "value", None):
            return

        value = aspect.value
        if isinstance(value, bytes):
            value = value.decode("utf-8")
        status_aspect = json.loads(value)

        status = status_aspect.get("status")
        result = status_aspect.get("result")

        # A multi-step workflow leaves the request PENDING between gates. Acting
        # then is premature -- but staying silent means a submission and every
        # intermediate approval pass unseen, so the first sign of a request is
        # the moment it is already final.
        if status != COMPLETED or result not in ROUTABLE_RESULTS:
            if status == PENDING and self.log_progress:
                self._log_progress(event.event.entityUrn)
            else:
                logger.debug(
                    "Ignoring %s (status=%s result=%s)", event.event.entityUrn, status, result
                )
            return

        request_urn = event.event.entityUrn
        request = self._read(self.client.get_action_request, request_urn, "request")
        if request is None:
            # Raising lets the framework retry. Swallowing would lose the
            # decision permanently -- a fresh consumer starts at the latest
            # offset and cannot replay history.
            raise DataHubError(
                f"Action request {request_urn} not found after reaching "
                f"{COMPLETED}/{result}; check permissions or read-after-write lag"
            )

        form = (request.get("params") or {}).get("workflowFormRequest") or {}
        workflow_urn = (form.get("workflowUrn") or "").strip()

        spec = self.workflows.get(workflow_urn) if self.workflows else None
        if self.workflows and spec is None:
            logger.debug(
                "Ignoring %s: workflow %s is not configured", request_urn, workflow_urn
            )
            return

        label = spec["label"] if spec else workflow_urn
        action = spec[result] if spec else "log"
        fields = FormFields(form.get("fields"))

        # The routing query carries neither the requester, the note, the target
        # entity nor the per-step decisions -- a second round trip does. Kept
        # separate and tolerant of failure so a schema drift in the richer query
        # can never break an acceptance, which is the path that creates things.
        # Retried on the same grounds, and with an extra condition: a detail
        # that comes back without created.actor is lag too, not a request with
        # no requester. Accepting it produces "no resolvable recipients" and a
        # silent no-send -- which reads as a governance gap rather than a
        # transient, and is how a rejection notice goes missing unnoticed.
        detail = self._read(
            self.client.get_action_request_detail,
            request_urn,
            "detail",
            ready=lambda d: ((d.get("created") or {}).get("actor") or {}).get("urn"),
        ) or {}
        context = {**request, **detail}

        logger.info(
            "%s %s | workflow=%s | request=%s -> %s",
            label, result, workflow_urn, request_urn, action,
        )
        self._log_context(context, fields)

        if action == "create_domain":
            entity_urn = create_domain(self.client, fields, request_urn)
            logger.info("Request %s handled; %s is up to date", request_urn, entity_urn)
        elif action.startswith("notify_"):
            # Pipeline-wide settings first, then the workflow's own. send_notice
            # layers the accepted:/rejected: block on top of that.
            config = {**self.notify, **(spec["notify"] if spec else {})}
            send_notice(self.client, fields, context, config, result)

    def close(self) -> None:
        return None

    # ------------------------------------------------------------------ private

    def _log_progress(self, request_urn: str) -> None:
        """One line for a submission or a gate that has just passed.

        Deliberately compact -- no field dump. A three-gate workflow emits three
        of these per request, and repeating the whole form each time is how a
        log stops being read.

        Best-effort: a request that cannot be read yet is skipped rather than
        retried, because nothing depends on this and blocking the pipeline for
        a log line is the wrong trade.

        The gate count comes from the request as it is NOW, not as it was when
        the event fired. Live those are the same moment. Replaying old events is
        where they diverge -- a long-finished request narrates its early gates
        with its final count -- so do not read a lookback run as a timeline.
        """
        detail = self.client.get_action_request_detail(request_urn)
        if not detail:
            logger.debug("Progress for %s not readable yet; skipping", request_urn)
            return

        form = (detail.get("params") or {}).get("workflowFormRequest") or {}
        workflow_urn = (form.get("workflowUrn") or "").strip()
        spec = self.workflows.get(workflow_urn) if self.workflows else None
        if self.workflows and spec is None:
            return  # another workflow's request; not ours to narrate

        label = spec["label"] if spec else workflow_urn
        decided = len(form.get("decisions") or [])
        requester = ((detail.get("created") or {}).get("actor") or {}).get("urn")

        if decided == 0:
            logger.info(
                "%s SUBMITTED | request=%s | by=%s | awaiting first approval",
                label, request_urn, requester,
            )
        else:
            last = (form.get("decisions") or [])[-1]
            logger.info(
                "%s IN PROGRESS | request=%s | %s %s by %s | %d gate(s) passed",
                label, request_urn, last.get("stepId"), (last.get("result") or "").lower(),
                (last.get("decidedBy") or {}).get("urn"), decided,
            )

    def _read(self, fetch, urn: str, what: str, ready=None):
        """Read something that may not be readable yet.

        ``ready`` lets a caller say more than "not null" -- the detail query can
        return a record whose requester has not landed, which is the same lag
        wearing a different face.

        Blocks the pipeline for up to READ_ATTEMPTS * READ_DELAY_SECONDS. That
        is deliberate: events are processed one at a time anyway, and a few
        seconds of delay is cheaper than losing an approval.
        """
        for attempt in range(1, READ_ATTEMPTS + 1):
            try:
                result = fetch(urn)
            except Exception as exc:
                logger.warning("Reading %s for %s failed: %s", what, urn, exc)
                result = None

            if result and (ready is None or ready(result)):
                if attempt > 1:
                    logger.info("Read %s for %s on attempt %d", what, urn, attempt)
                return result

            if attempt < READ_ATTEMPTS:
                time.sleep(READ_DELAY_SECONDS)

        logger.warning(
            "Gave up reading %s for %s after %d attempts over %ds",
            what, urn, READ_ATTEMPTS, READ_ATTEMPTS * READ_DELAY_SECONDS,
        )
        return None

    @staticmethod
    def _log_context(request: dict, fields: FormFields) -> None:
        requester = ((request.get("created") or {}).get("actor") or {}).get("urn")
        entity = (request.get("entity") or {}).get("urn")
        note = (request.get("resultNote") or "").strip()
        logger.info(
            "    requester=%s%s | note=%s",
            requester,
            f" | entity={entity}" if entity else "",
            note or "(none)",
        )
        for field_id in fields.ids():
            values = fields.all(field_id)
            if values:
                logger.info("    %s = %s", field_id, ", ".join(values))


def _normalize(raw: dict) -> dict:
    """{urn: {label, ACCEPTED, REJECTED}} from either config form.

    Keys are stripped because live data really contains
    "urn:li:actionWorkflow: 1fc9daf6-..." with a space, and that is the string
    an operator pastes into the YAML.
    """
    out: dict[str, dict] = {}
    for urn, spec in raw.items():
        key = str(urn).strip()
        if isinstance(spec, str):
            out[key] = {"label": spec, "ACCEPTED": "log", "REJECTED": "log"}
            continue
        if not isinstance(spec, dict):
            raise ValueError(
                f"workflows[{urn!r}] must be a label or a mapping, got {type(spec).__name__}"
            )
        entry = {
            "label": str(spec.get("label") or key),
            "ACCEPTED": str(spec.get("accepted") or "log"),
            "REJECTED": str(spec.get("rejected") or "log"),
            # Per-workflow notify settings. Layered over the pipeline-wide
            # block at dispatch, because field_labels and the subject are
            # properties of the FORM -- one shared block cannot serve two
            # workflows whose forms have nothing in common.
            "notify": spec.get("notify") or {},
        }
        for result in ROUTABLE_RESULTS:
            if entry[result] not in ACTIONS:
                raise ValueError(
                    f"workflows[{urn!r}][{result.lower()}] is {entry[result]!r}; "
                    f"expected one of {', '.join(ACTIONS)}"
                )
        out[key] = entry
    return out


_WORKFLOW_URN_PREFIX = "urn:li:actionWorkflow:"


def _resolve_workflow_keys(client: DataHubClient, workflows: dict) -> dict:
    """Bind every configured key to a live Action Workflow URN.

    A key already shaped like a URN is validated and kept as-is; anything else is
    a workflow NAME and is resolved. Both directions fail fast, because the
    ``workflows`` table is also a FILTER -- a key matching nothing means
    decisions from that workflow are silently ignored, with no error anywhere.
    That silence is the whole reason this function exists, so it must not be
    reintroduced as a warning.

    URN keys are validated too, not just names. Resolving only names would leave
    the hole open for any config carried from another deployment.

    An empty table means "log every workflow", so it short-circuits rather than
    spending a call to bind nothing.
    """
    if not workflows:
        return workflows

    live = client.list_action_workflows()
    urns = {w["urn"] for w in live}

    by_name: dict[str, list[str]] = {}
    for workflow in live:
        name = (workflow.get("name") or "").strip()
        if name:
            by_name.setdefault(name, []).append(workflow["urn"])

    resolved: dict[str, dict] = {}
    for key, spec in workflows.items():
        if key.startswith(_WORKFLOW_URN_PREFIX):
            if key not in urns:
                raise ValueError(
                    f"{key} is not an Action Workflow on this instance. If this "
                    f"config came from another deployment, key the entry by name "
                    f"instead."
                )
            target = key
        else:
            matches = by_name.get(key) or []
            if not matches:
                available = ", ".join(sorted(by_name)) or "none"
                raise ValueError(
                    f"{key!r} is not an Action Workflow on this instance "
                    f"(available: {available}). Check the name in "
                    f"action-route-workflow-decisions.yaml, or use the URN directly."
                )
            if len(matches) > 1:
                raise ValueError(
                    f"{len(matches)} Action Workflows are named {key!r} "
                    f"({', '.join(sorted(matches))}). Use the URN directly for "
                    f"this entry."
                )
            target = matches[0]
            logger.info("Resolved workflow %r -> %s", key, target)

        if target in resolved:
            # Two keys naming one workflow: dict assignment would drop one spec
            # and the operator would never know which routing won.
            raise ValueError(
                f"{target} is configured twice, once by URN and once by name. "
                f"Keep one entry."
            )
        resolved[target] = spec

    return resolved
