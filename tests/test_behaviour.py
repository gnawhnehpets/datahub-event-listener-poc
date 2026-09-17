"""Proves the four cells of the behaviour table, against the shipped config.

    Workflow                 ACCEPTED                REJECTED
    Propose New Domain       creates the domain      emails requester + form owners
    Dataset Access Request   emails the requester    emails the requester

These run the real router, the real config file and the real derivation. Only
the two edges are faked: GraphQL and Resend. That is deliberate -- a test that
also stubbed the router or the config would pass while the shipped YAML said
something else entirely, which is the failure worth catching here.

No credentials needed: `pytest`.
"""

from __future__ import annotations

import json
import os
import pathlib
import tempfile

import pytest
import yaml
from expandvars import expandvars

from actions.router import WorkflowRouterAction

ROOT = pathlib.Path(__file__).resolve().parents[1]
RECIPE = ROOT / "action-route-workflow-decisions.yaml"

DOMAIN_WF = "urn:li:actionWorkflow:propose-new-domain"
# TARGET URN for the YAML's name-keyed "Dataset Access Request" entry.
# The _WorkflowListing stub in build() maps that name to this URN so the suite
# can resolve it offline. Delete this constant and every routing test that
# exercises the access-request path passes vacuously — the workflow becomes
# unlisted and its decisions are silently ignored rather than routed.
ACCESS_WF = "urn:li:actionWorkflow:7110b19c-45b9-4450-b8bb-ba47d9f522ba"

REQUESTER = "urn:li:corpuser:requester@x.com"
OWNER = "urn:li:corpuser:owner@x.com"
PARENT = "urn:li:domain:collibra_lob_risk_and_compliance"
DATASET = "urn:li:dataset:(urn:li:dataPlatform:databricks,samples.forecast,PROD)"


# --------------------------------------------------------------------- config


def recipe_config() -> dict:
    """The shipped recipe, expanded exactly as the pipeline loader does."""
    env = {
        "DATAHUB_GMS_URL": "https://example.invalid",
        "DATAHUB_GMS_TOKEN": "token",
        "CONSUMER_ID": "test",
        # gettempdir(), not "/tmp": the literal resolves to C:\tmp on Windows,
        # so the suite would pass there while exercising the wrong path.
        "FAILED_EVENTS_DIR": os.environ.get("FAILED_EVENTS_DIR", tempfile.gettempdir()),
    }
    previous = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    try:
        return yaml.safe_load(expandvars(RECIPE.read_text()))["action"]["config"]
    finally:
        for k, v in previous.items():
            os.environ.pop(k, None) if v is None else os.environ.update({k: v})


# ---------------------------------------------------------------------- fakes


class FakeClient:
    """Only what the router and the two actions call.

    Mutations are recorded rather than sent; reads answer from a small fixed
    domain tree. Nothing here reaches a network.
    """

    def __init__(self, request: dict, exists: bool = False):
        self._request = request
        self._exists = exists
        self.mutations: list[tuple[str, dict]] = []

    # -- reads
    def get_action_request(self, urn):
        return self._request

    def get_action_request_detail(self, urn):
        return self._request

    def entity_exists(self, urn):
        return self._exists

    def get_domain(self, urn):
        return {
            "urn": urn,
            "properties": {"name": "Risk & Compliance"},
            "parentDomains": {"domains": []},
            "structuredProperties": {
                "properties": [
                    {
                        "structuredProperty": {"urn": "urn:li:structuredProperty:collibra.gcDomainCode"},
                        "values": [{"stringValue": "GC002"}],
                    }
                ]
            },
        }

    def get_child_domains(self, parent_urn):
        return []

    def get_root_domains(self):
        return [
            {
                "urn": PARENT,
                "properties": {"name": "Risk & Compliance"},
                "parentDomains": {"domains": []},
                "structuredProperties": {"properties": []},
            }
        ]

    def resolve_principals(self, urns):
        from actions.recipients import Recipient

        return [Recipient(u, u.split(":")[-1], u.split(":")[-1]) for u in urns], []

    # -- writes
    def execute(self, query, variables=None):
        op = query.strip().split("(")[0].split()[-1]
        self.mutations.append((op, variables or {}))
        return {op: None}

    def run(self, mutation):
        self.mutations.append((mutation.label, mutation.variables))
        return {}


@pytest.fixture(autouse=True)
def _fresh_state(monkeypatch, tmp_path):
    """A clean send-once store per test.

    Every test here reuses one request urn, so without this the guard from the
    first test that sends would suppress every later one -- and the suite would
    fail for the right reason in the wrong place.

    Both paths are redirected: the recipe's state_dir expands from
    FAILED_EVENTS_DIR, and DEFAULT_STATE_DIR covers a config that sets neither.
    """
    monkeypatch.setenv("FAILED_EVENTS_DIR", str(tmp_path))
    monkeypatch.setattr("actions.notify.DEFAULT_STATE_DIR", tmp_path / "notified")


@pytest.fixture(autouse=True)
def _resend(monkeypatch):
    """Capture the send instead of performing it."""
    sent: list[dict] = []
    monkeypatch.setenv("RESEND_API_KEY", "test-key")

    class Response:
        status_code = 200

        @staticmethod
        def json():
            return {"id": "test-message-id"}

    def fake_post(url, json=None, headers=None, timeout=None):
        sent.append(json)
        return Response()

    monkeypatch.setattr("actions.notify.requests.post", fake_post)
    return sent


def build(request: dict, exists: bool = False) -> tuple[WorkflowRouterAction, FakeClient]:
    config = recipe_config()
    client = FakeClient(request, exists)
    globals_ = WorkflowRouterAction.create.__func__.__globals__
    # The shipped recipe keys one entry by NAME, so the helper has to resolve the
    # same way create() does -- act() looks specs up by urn. Without this every
    # routing test would silently treat that workflow as unlisted.
    workflows = globals_["_resolve_workflow_keys"](
        _WorkflowListing([
            (DOMAIN_WF, "Propose New Domain"),
            (ACCESS_WF, "Dataset Access Request"),
            ("urn:li:actionWorkflow:zz-dataset-access-request-2", "Dataset Access Request-2"),
        ]),
        globals_["_normalize"](config["workflows"]),
    )
    action = WorkflowRouterAction(
        client=client,
        workflows=workflows,
        notify=config.get("notify") or {},
        ctx=None,
    )
    return action, client


class Event:
    """The shape the router reads off an EventEnvelope."""

    def __init__(self, urn: str, result: str):
        self.event = type(
            "MCL",
            (),
            {
                "entityUrn": urn,
                "aspect": type(
                    "A",
                    (),
                    {"value": json.dumps({"status": "COMPLETED", "result": result})},
                )(),
            },
        )()


def request_for(workflow: str, result: str, fields: list[dict], entity=None) -> dict:
    return {
        "urn": "urn:li:actionRequest:test",
        "type": "WORKFLOW_FORM_REQUEST",
        "status": "COMPLETED",
        "result": result,
        "resultNote": "a decision note",
        "created": {"actor": {"urn": REQUESTER}},
        "entity": entity,
        "params": {
            "workflowFormRequest": {
                "workflowUrn": workflow,
                "fields": fields,
                "decisions": [{"result": result, "decidedBy": {"urn": "urn:li:corpuser:approver@x.com"}}],
            }
        },
    }


DOMAIN_FIELDS = [
    {"id": "new_domain_name", "values": [{"stringValue": "Payments"}]},
    {"id": "parent_domain", "values": [{"stringValue": "Risk & Compliance"}]},
    {"id": "technical_owners", "values": [{"stringValue": OWNER}]},
]
ACCESS_FIELDS = [
    {"id": "business_justification", "values": [{"stringValue": "<p>Reporting.</p>"}]},
    {"id": "access_duration", "values": [{"stringValue": "90_DAYS"}]},
    # Not on the real dataset-access form. Present so the requester-only
    # assertion has something to catch: without a principal field in the
    # payload, adding owner_fields to that workflow's config changes nothing
    # and the test passes while the behaviour it claims to pin has changed.
    {"id": "technical_owners", "values": [{"stringValue": OWNER}]},
]


# ------------------------------------------------- Propose New Domain: ACCEPTED


def test_domain_approval_creates_the_domain():
    action, client = build(request_for(DOMAIN_WF, "ACCEPTED", DOMAIN_FIELDS))
    action.act(Event("urn:li:actionRequest:test", "ACCEPTED"))

    ops = [op for op, _ in client.mutations]
    assert "createDomain" in ops, f"no domain was created; ran {ops}"


def test_domain_approval_also_sets_owners_and_properties():
    """Creating the entity and leaving it unlabelled is not 'creates the domain'."""
    action, client = build(request_for(DOMAIN_WF, "ACCEPTED", DOMAIN_FIELDS))
    action.act(Event("urn:li:actionRequest:test", "ACCEPTED"))

    ops = [op for op, _ in client.mutations]
    assert "addOwners" in ops
    assert "upsertStructuredProperties" in ops


def test_domain_approval_sends_no_email(_resend):
    action, _ = build(request_for(DOMAIN_WF, "ACCEPTED", DOMAIN_FIELDS))
    action.act(Event("urn:li:actionRequest:test", "ACCEPTED"))

    assert _resend == [], "approval of a domain proposal must not email"


# ------------------------------------------------- Propose New Domain: REJECTED


def test_domain_rejection_emails_requester_and_form_owners(_resend):
    action, client = build(request_for(DOMAIN_WF, "REJECTED", DOMAIN_FIELDS))
    action.act(Event("urn:li:actionRequest:test", "REJECTED"))

    # One send per recipient, so assert on who was reached, not on call count.
    reached = {addr for call in _resend for addr in call["to"]}
    assert "requester@x.com" in reached
    assert "owner@x.com" in reached, "the form-named owners must be notified too"


def test_each_recipient_is_sent_their_own_message(_resend):
    """Resend rejects the whole call if any one address is unacceptable.

    Batching means a single undeliverable co-owner costs everyone else their
    notice -- including the requester, who is who the mail is for. It also stops
    a governance notice disclosing recipients to each other.
    """
    action, _ = build(request_for(DOMAIN_WF, "REJECTED", DOMAIN_FIELDS))
    action.act(Event("urn:li:actionRequest:test", "REJECTED"))

    assert len(_resend) == 2, "expected one call per recipient"
    assert all(len(call["to"]) == 1 for call in _resend)


def test_one_undeliverable_recipient_does_not_block_the_others(_resend, monkeypatch):
    """The failure observed live: a group of sample users with no real address."""
    import actions.notify as notify
    from actions.errors import NotificationError

    class Rejected:
        status_code = 422
        @staticmethod
        def json(): return {"message": "Invalid `to` field"}

    class Accepted:
        status_code = 200
        @staticmethod
        def json(): return {"id": "ok"}

    def picky(url, json=None, headers=None, timeout=None):
        if json["to"] == ["owner@x.com"]:
            return Rejected()
        _resend.append(json)
        return Accepted()

    monkeypatch.setattr(notify.requests, "post", picky)
    action, _ = build(request_for(DOMAIN_WF, "REJECTED", DOMAIN_FIELDS))

    # No raise: a 422 will answer 422 forever, and raising would make the
    # framework retry the whole event three more times -- which is what buried
    # the recipient who DID get the mail under four stack traces.
    action.act(Event("urn:li:actionRequest:test", "REJECTED"))

    reached = {a for call in _resend for a in call["to"]}
    assert reached == {"requester@x.com"}, "the deliverable recipient must still be served"


def test_an_undeliverable_address_is_not_retried_on_replay(_resend, monkeypatch):
    """A permanent failure is recorded, or it burns four retries every restart."""
    import actions.notify as notify

    attempts = []

    class Rejected:
        status_code = 422
        @staticmethod
        def json(): return {"message": "Invalid `to` field"}

    class Accepted:
        status_code = 200
        @staticmethod
        def json(): return {"id": "ok"}

    def picky(url, json=None, headers=None, timeout=None):
        attempts.append(json["to"][0])
        return Rejected() if json["to"] == ["owner@x.com"] else Accepted()

    monkeypatch.setattr(notify.requests, "post", picky)
    request = request_for(DOMAIN_WF, "REJECTED", DOMAIN_FIELDS)

    for _ in range(3):                      # three replays of the same window
        action, _ = build(request)
        action.act(Event("urn:li:actionRequest:test", "REJECTED"))

    assert attempts.count("owner@x.com") == 1, (
        f"the undeliverable address was retried {attempts.count('owner@x.com')} times"
    )
    assert attempts.count("requester@x.com") == 1


def test_a_transient_failure_still_raises_so_the_framework_retries(monkeypatch):
    """5xx may yet succeed, so it must not be recorded as settled."""
    import actions.notify as notify
    from actions.errors import NotificationTransientError

    class Down:
        status_code = 503
        @staticmethod
        def json(): return {}

    monkeypatch.setattr(notify.requests, "post", lambda *a, **k: Down())
    action, _ = build(request_for(DOMAIN_WF, "REJECTED", DOMAIN_FIELDS))

    with pytest.raises(NotificationTransientError, match="failed temporarily"):
        action.act(Event("urn:li:actionRequest:test", "REJECTED"))


def test_domain_rejection_creates_nothing():
    action, client = build(request_for(DOMAIN_WF, "REJECTED", DOMAIN_FIELDS))
    action.act(Event("urn:li:actionRequest:test", "REJECTED"))

    assert client.mutations == [], f"a rejection must not write: {client.mutations}"


# ---------------------------------------------- Dataset Access Request: both


@pytest.mark.parametrize("result", ["ACCEPTED", "REJECTED"])
def test_dataset_access_emails_the_requester_only(_resend, result):
    request = request_for(
        ACCESS_WF, result, ACCESS_FIELDS, entity={"urn": DATASET, "name": "forecast"}
    )
    action, client = build(request)
    action.act(Event("urn:li:actionRequest:test", result))

    assert len(_resend) == 1, f"{result} should send exactly one email"
    assert _resend[0]["to"] == ["requester@x.com"], (
        "dataset-access approvers ARE the dataset owners; mailing them would send "
        "people their own decision"
    )
    assert client.mutations == [], "dataset access must create nothing"


def test_dataset_access_subjects_differ_by_outcome(_resend):
    """An approval that reads like a refusal is worse than no mail at all."""
    subjects = {}
    for result in ("ACCEPTED", "REJECTED"):
        _resend.clear()
        request = request_for(
            ACCESS_WF, result, ACCESS_FIELDS, entity={"urn": DATASET, "name": "forecast"}
        )
        action, _ = build(request)
        action.act(Event("urn:li:actionRequest:test", result))
        subjects[result] = _resend[0]["subject"]

    assert subjects["ACCEPTED"] != subjects["REJECTED"]
    assert "approved" in subjects["ACCEPTED"].lower()
    assert "rejected" in subjects["REJECTED"].lower()


def test_dataset_access_approval_says_it_grants_no_access(_resend):
    """The listener grants no permission anywhere and must not imply it does."""
    request = request_for(
        ACCESS_WF, "ACCEPTED", ACCESS_FIELDS, entity={"urn": DATASET, "name": "forecast"}
    )
    action, _ = build(request)
    action.act(Event("urn:li:actionRequest:test", "ACCEPTED"))

    body = _resend[0]["html"].lower()
    assert "does not itself grant access" in body


def test_dataset_access_email_titles_itself_from_the_target_dataset(_resend):
    """That form has no name field -- the dataset IS the subject of the request."""
    request = request_for(
        ACCESS_WF, "REJECTED", ACCESS_FIELDS, entity={"urn": DATASET, "name": "forecast"}
    )
    action, _ = build(request)
    action.act(Event("urn:li:actionRequest:test", "REJECTED"))

    assert "forecast" in _resend[0]["subject"]


def test_dataset_access_email_shows_every_submitted_field(_resend):
    """A field without a label is omitted silently, so the labels must cover the form."""
    request = request_for(
        ACCESS_WF, "REJECTED", ACCESS_FIELDS, entity={"urn": DATASET, "name": "forecast"}
    )
    action, _ = build(request)
    action.act(Event("urn:li:actionRequest:test", "REJECTED"))

    body = _resend[0]["html"]
    assert "Business justification" in body
    assert "Access duration" in body


# ------------------------------------------------------------- shared invariants


def test_pending_decisions_are_ignored():
    """A multi-step workflow sits PENDING between gates; acting then is premature."""
    action, client = build(request_for(DOMAIN_WF, "ACCEPTED", DOMAIN_FIELDS))
    event = Event("urn:li:actionRequest:test", "ACCEPTED")
    event.event.aspect.value = json.dumps({"status": "PENDING", "result": None})
    action.act(event)

    assert client.mutations == []


def test_an_unconfigured_workflow_is_ignored(_resend):
    action, client = build(request_for("urn:li:actionWorkflow:something-else", "ACCEPTED", []))
    action.act(Event("urn:li:actionRequest:test", "ACCEPTED"))

    assert client.mutations == []
    assert _resend == []


def test_rich_text_is_stripped_not_rendered(_resend):
    """Submitted values are attacker-influenced and RICH_TEXT arrives as HTML."""
    request = request_for(
        ACCESS_WF, "REJECTED", ACCESS_FIELDS, entity={"urn": DATASET, "name": "forecast"}
    )
    action, _ = build(request)
    action.act(Event("urn:li:actionRequest:test", "REJECTED"))

    body = _resend[0]["html"]
    assert "Reporting." in body
    assert "&lt;p&gt;" not in body


# ----------------------------------------------------- read-after-write lag
#
# A decision event can arrive before the write behind it is readable. Observed
# live: an approval was lost entirely because all four framework retries fired
# within 468ms, and a rejection sent no mail because the detail came back
# without a requester and "no resolvable recipients" looks like a governance
# gap rather than a transient.


class LaggingClient(FakeClient):
    """Returns nothing for the first `lag` reads, as a replica catching up does."""

    def __init__(self, request, lag: int, lag_detail_actor: bool = False):
        super().__init__(request)
        self.lag = lag
        self.lag_detail_actor = lag_detail_actor
        self.reads = 0
        self.detail_reads = 0

    def get_action_request(self, urn):
        self.reads += 1
        return None if self.reads <= self.lag else self._request

    def get_action_request_detail(self, urn):
        self.detail_reads += 1
        if self.lag_detail_actor and self.detail_reads <= self.lag:
            # Present but incomplete -- the shape that produced the silent
            # no-send, since the requester is where recipients come from.
            return {**self._request, "created": None}
        return self._request


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """Keep the retry logic, drop the wall-clock."""
    monkeypatch.setattr("actions.router.time.sleep", lambda _s: None)


def test_a_lagging_request_read_is_retried_not_lost():
    action, _ = build(request_for(DOMAIN_WF, "ACCEPTED", DOMAIN_FIELDS))
    client = LaggingClient(request_for(DOMAIN_WF, "ACCEPTED", DOMAIN_FIELDS), lag=3)
    action.client = client

    action.act(Event("urn:li:actionRequest:test", "ACCEPTED"))

    assert "createDomain" in [op for op, _ in client.mutations], (
        "an approval must survive a read that lags behind its own event"
    )


def test_a_request_that_never_appears_still_raises():
    """Retrying forever would wedge the pipeline; giving up must stay loud."""
    from actions.errors import DataHubError

    action, _ = build(request_for(DOMAIN_WF, "ACCEPTED", DOMAIN_FIELDS))
    action.client = LaggingClient(request_for(DOMAIN_WF, "ACCEPTED", DOMAIN_FIELDS), lag=99)

    with pytest.raises(DataHubError, match="not found after reaching"):
        action.act(Event("urn:li:actionRequest:test", "ACCEPTED"))


def test_a_detail_without_a_requester_is_retried_rather_than_emailed_to_nobody(_resend):
    request = request_for(
        ACCESS_WF, "REJECTED", ACCESS_FIELDS, entity={"urn": DATASET, "name": "forecast"}
    )
    action, _ = build(request)
    action.client = LaggingClient(request, lag=2, lag_detail_actor=True)

    action.act(Event("urn:li:actionRequest:test", "REJECTED"))

    assert len(_resend) == 1, (
        "a detail that has not caught up yet must be re-read, not treated as a "
        "request with no requester"
    )
    assert _resend[0]["to"] == ["requester@x.com"]


# ------------------------------------------------------------ multi-select fields


def test_a_multi_select_field_shows_every_selected_value(_resend):
    """cardinality: MULTIPLE means several values under one field id.

    `fields.one()` returns only the first, so a renderer written against it
    would drop the rest silently -- the value would simply look shorter than
    what the requester chose.
    """
    fields = DOMAIN_FIELDS + [
        {
            "id": "data_classification",
            "values": [
                {"stringValue": "Confidential"},
                {"stringValue": "PII"},
                {"stringValue": "Restricted"},
            ],
        }
    ]
    action, _ = build(request_for(DOMAIN_WF, "REJECTED", fields))
    action.act(Event("urn:li:actionRequest:test", "REJECTED"))

    body = _resend[0]["html"]
    assert "Confidential, PII, Restricted" in body


def test_every_labelled_field_the_form_offers_can_appear(_resend):
    """A form field absent from field_labels is dropped from the email silently.

    Adding a field to the definition and forgetting its label is the failure
    this catches -- nothing errors, the value just never reaches the reader.
    """
    import json

    definition = json.loads(
        (ROOT / "workflows" / "definitions" / "action-workflow-propose-new-domain_v2.json").read_text()
    )
    form_ids = {f["id"] for f in definition["input"]["trigger"]["form"]["fields"]}
    labels = set(
        recipe_config()["workflows"][DOMAIN_WF]["notify"]["field_labels"]
    )

    # level_2_* are generated per parent domain and deliberately unlabelled:
    # they narrow the parent rather than describing the proposal.
    unlabelled = {i for i in form_ids - labels if not i.startswith("level_2_")}
    assert not unlabelled, f"form fields with no label, silently dropped from email: {unlabelled}"


def test_parent_domain_carries_no_filter_condition():
    """A filterCondition here makes the whole definition undeployable.

    We wanted the opposite of this test: restrict the picker to root (Level 1)
    domains, because the unfiltered picker offers every depth and both wrong
    choices are expensive. Picking a Level 3 puts the proposal at level 4, which
    level_for refuses -- but only after all three approvals, since router.act
    ignores anything not COMPLETED. Picking a Level 2 succeeds while bypassing
    the level_2_* dropdowns entirely.

    It cannot be done. upsertActionWorkflow validates filterCondition against a
    fixed allowlist and rejects the whole mutation otherwise:

        Criterion.field '<x>' is neither a 'formField:<id>' reference nor a
        known launching-entity attribute [owners, entityType, subTypes, tags,
        platform, domain, applications, dataProducts, container, glossaryTerms]

    Three candidates were tested against the live instance. All rejected:

        parentDomain EXISTS negated      -- correct field, 19/26 split, rejected
        hasParentDomain EQUAL "false"    -- correct field, 20/26 split, rejected
        structuredProperties.collibra.lobLevel EQUAL "1"  -- rejected

    The first two are real, working search-index fields. filterCondition simply
    does not consult the index. And every field it DOES accept is absent from the
    Domain type -- `tags` is not even a field on Domain -- so a tag workaround
    would validate and then silently match nothing, emptying the picker.

    So this test guards deployability, not behaviour: adding a filterCondition
    back makes upsert_workflows.py fail with a 400 while the rest of the suite
    stays green. Note that GraphQL introspection does NOT catch it, because
    FacetFilterInput.field is typed String -- the constraint is semantic and
    enforced only at upsert. See README "Known upstream issues".
    """
    definition = json.loads(
        (ROOT / "workflows" / "definitions" / "action-workflow-propose-new-domain_v2.json").read_text()
    )
    parent = next(
        f
        for f in definition["input"]["trigger"]["form"]["fields"]
        if f["id"] == "parent_domain"
    )

    assert parent.get("filterCondition") is None, (
        "parent_domain has a filterCondition, which upsertActionWorkflow rejects "
        f"with a 400: {parent.get('filterCondition')}. The definition cannot be "
        "deployed. See this test's docstring for the three candidates already ruled out."
    )


def test_parent_domain_description_admits_the_list_is_unfiltered():
    """The description is the only thing steering requesters, so it must be true.

    Since filterCondition cannot restrict this picker (see the test above), the
    wording carries the whole burden. Two ways to get it wrong, and this repo has
    now made both:

      * The original said "or a Level 2 domain to create a Level 3", which
        bypasses the level_2_* dropdowns the generator maintains.
      * A later revision claimed "Only Level 1 domains are listed" -- written
        while the filter was expected to work. It shipped to the live form after
        the filter was reverted, so the form actively lied: every depth is still
        listed.

    A description that promises filtering nobody implements is worse than one
    that admits the gap, because a requester who trusts it has no reason to look
    at what they picked. So this asserts the wording tells the truth: it must not
    claim the list is restricted, and it must warn that a wrong pick is caught
    only after every approval has completed.
    """
    definition = json.loads(
        (ROOT / "workflows" / "definitions" / "action-workflow-propose-new-domain_v2.json").read_text()
    )
    parent = next(
        f
        for f in definition["input"]["trigger"]["form"]["fields"]
        if f["id"] == "parent_domain"
    )

    description = parent["description"].lower()

    assert "level 2 domain to create a level 3" not in description, (
        "description tells the requester to pick a Level 2 directly, which bypasses "
        "the level_2_* dropdowns that generate_form_fields.py keeps in sync"
    )
    assert "only level 1 domains are listed" not in description, (
        "description claims the picker is filtered to Level 1. It is not -- "
        "filterCondition cannot express that, so this promise is false"
    )
    assert "not filtered" in description, (
        "description must state that the list is unfiltered, since nothing in the "
        "form prevents selecting a Level 2 or Level 3"
    )
    assert "after all three approvals" in description, (
        "description must warn that a wrong parent is only rejected after every "
        "approval gate, which is what makes the mistake expensive"
    )


# ------------------------------------------------- state dir is cross-platform


def test_default_state_dir_is_the_system_temp_dir():
    """A hardcoded "/tmp" default silently relocates the send-once store.

    On Windows, pathlib resolves "/tmp" against the current drive root, so the
    listener does not fail -- it writes its dedup markers to C:\\tmp\\notified
    instead. That store is what stops lookback_days: 7 re-mailing a week of
    decisions on every restart, so losing track of it means duplicate
    notifications rather than a visible error.

    Asserting against tempfile.gettempdir() rather than a literal keeps this
    honest on every OS.
    """
    import importlib
    import tempfile

    import actions.notify

    # Reimport with the env var absent so the module-level default is exercised,
    # not whatever FAILED_EVENTS_DIR the suite's fixtures happen to have set.
    previous = os.environ.pop("FAILED_EVENTS_DIR", None)
    try:
        reloaded = importlib.reload(actions.notify)
        default = reloaded.DEFAULT_STATE_DIR
    finally:
        if previous is not None:
            os.environ["FAILED_EVENTS_DIR"] = previous
        importlib.reload(actions.notify)

    assert default.parent == pathlib.Path(tempfile.gettempdir()), (
        f"default state dir is {default}, not under the system temp dir "
        f"{tempfile.gettempdir()}; a POSIX literal here lands in C:\\tmp on Windows"
    )


# --------------------------------------------------- owners may be groups


GROUP = "urn:li:corpGroup:sample_data_1e0398a3-113f-475e-b6fc-32ab72a634d2"


def test_a_group_owner_is_declared_as_a_group():
    """addOwners rejects the whole call when the declared type mismatches.

    "Expected a corp user urn, found urn:li:corpGroup:..." lands AFTER the
    domain is created, so the domain exists with no owners and no structured
    properties, and every retry hits the same wall.
    """
    fields = [
        {"id": "new_domain_name", "values": [{"stringValue": "Payments"}]},
        {"id": "parent_domain", "values": [{"stringValue": "Risk & Compliance"}]},
        {"id": "technical_owners", "values": [{"stringValue": GROUP}]},
        {"id": "data_stewards", "values": [{"stringValue": OWNER}]},
    ]
    action, client = build(request_for(DOMAIN_WF, "ACCEPTED", fields))
    action.act(Event("urn:li:actionRequest:test", "ACCEPTED"))

    owners = next(v["input"]["owners"] for op, v in client.mutations if op == "addOwners")
    by_urn = {o["ownerUrn"]: o["ownerEntityType"] for o in owners}
    assert by_urn[GROUP] == "CORP_GROUP"
    assert by_urn[OWNER] == "CORP_USER"


def test_mixed_user_and_group_owners_are_sent_in_one_call():
    """Splitting them would half-apply if the second call failed."""
    fields = [
        {"id": "new_domain_name", "values": [{"stringValue": "Payments"}]},
        {"id": "parent_domain", "values": [{"stringValue": "Risk & Compliance"}]},
        {"id": "technical_owners", "values": [{"stringValue": GROUP}]},
        {"id": "data_stewards", "values": [{"stringValue": OWNER}]},
    ]
    action, client = build(request_for(DOMAIN_WF, "ACCEPTED", fields))
    action.act(Event("urn:li:actionRequest:test", "ACCEPTED"))

    assert len([op for op, _ in client.mutations if op == "addOwners"]) == 1


# ------------------------------------------------ submission / gate progress


def _pending(urn="urn:li:actionRequest:test"):
    e = Event(urn, "ACCEPTED")
    e.event.aspect.value = json.dumps({"status": "PENDING", "result": None})
    return e


def test_a_submission_is_logged_before_any_gate(caplog):
    request = request_for(DOMAIN_WF, "ACCEPTED", DOMAIN_FIELDS)
    request["params"]["workflowFormRequest"]["decisions"] = []
    action, client = build(request)

    with caplog.at_level("INFO"):
        action.act(_pending())

    assert any("SUBMITTED" in r.getMessage() for r in caplog.records)
    assert client.mutations == [], "a submission must not act"


def test_an_intermediate_gate_is_logged_as_progress(caplog):
    request = request_for(DOMAIN_WF, "ACCEPTED", DOMAIN_FIELDS)
    request["params"]["workflowFormRequest"]["decisions"] = [
        {"stepId": "data-steward-review", "result": "ACCEPTED",
         "decidedBy": {"urn": "urn:li:corpuser:approver@x.com"}}
    ]
    action, _ = build(request)

    with caplog.at_level("INFO"):
        action.act(_pending())

    msgs = " ".join(r.getMessage() for r in caplog.records)
    assert "IN PROGRESS" in msgs and "1 gate(s) passed" in msgs


def test_progress_for_an_unconfigured_workflow_is_not_narrated(caplog):
    request = request_for("urn:li:actionWorkflow:something-else", "ACCEPTED", [])
    request["params"]["workflowFormRequest"]["decisions"] = []
    action, _ = build(request)

    with caplog.at_level("INFO"):
        action.act(_pending())

    assert not any("SUBMITTED" in r.getMessage() for r in caplog.records)


def test_progress_logging_can_be_switched_off(caplog):
    request = request_for(DOMAIN_WF, "ACCEPTED", DOMAIN_FIELDS)
    request["params"]["workflowFormRequest"]["decisions"] = []
    action, _ = build(request)
    action.log_progress = False

    with caplog.at_level("INFO"):
        action.act(_pending())

    assert not any("SUBMITTED" in r.getMessage() for r in caplog.records)


# --------------------------------------------------------- replay safety
#
# lookback_days replays the window on EVERY restart, not only after an outage.
# That is what makes a missed approval recoverable -- and what would mail every
# decision in the window again, were it not for the send-once marker.


def test_the_same_decision_is_not_emailed_twice():
    request = request_for(DOMAIN_WF, "REJECTED", DOMAIN_FIELDS)
    sent = []

    for _ in range(3):                      # three restarts over one window
        action, _ = build(request)
        with _capture(sent):
            action.act(Event("urn:li:actionRequest:test", "REJECTED"))

    # Two recipients, each mailed once across three replays.
    assert len(sent) == 2, f"a replay re-sent the notification ({len(sent)} sends)"


def test_approval_and_rejection_of_the_same_request_are_tracked_apart():
    """The marker keys on outcome too, or a later rejection would be swallowed."""
    sent = []
    for result in ("ACCEPTED", "REJECTED"):
        request = request_for(
            ACCESS_WF, result, ACCESS_FIELDS, entity={"urn": DATASET, "name": "forecast"}
        )
        action, _ = build(request)
        with _capture(sent):
            action.act(Event("urn:li:actionRequest:test", result))

    assert len(sent) == 2


def test_different_requests_are_tracked_apart():
    sent = []
    for n in (1, 2):
        request = request_for(DOMAIN_WF, "REJECTED", DOMAIN_FIELDS)
        request["urn"] = f"urn:li:actionRequest:test-{n}"
        action, _ = build(request)
        with _capture(sent):
            action.act(Event(f"urn:li:actionRequest:test-{n}", "REJECTED"))

    # Two requests x two recipients, none suppressed by the other's marker.
    assert len(sent) == 4


def test_a_failed_send_is_not_marked_and_can_be_retried(monkeypatch):
    """Marking before sending would lose a notification to a transient error."""
    from actions.errors import NotificationTransientError

    request = request_for(DOMAIN_WF, "REJECTED", DOMAIN_FIELDS)

    class Boom:
        status_code = 500
        @staticmethod
        def json(): return {}

    monkeypatch.setattr("actions.notify.requests.post", lambda *a, **k: Boom())
    action, _ = build(request)
    with pytest.raises(NotificationTransientError):
        action.act(Event("urn:li:actionRequest:test", "REJECTED"))

    # The retry now succeeds, and must actually send -- to both recipients,
    # since a 5xx marks nobody.
    sent = []
    action, _ = build(request)
    with _capture(sent):
        action.act(Event("urn:li:actionRequest:test", "REJECTED"))
    assert len(sent) == 2


import contextlib


@contextlib.contextmanager
def _capture(sink):
    """Collect sends across several action instances in one test."""
    import actions.notify as notify

    original = notify.requests.post

    class Response:
        status_code = 200
        @staticmethod
        def json(): return {"id": "test-message-id"}

    def fake(url, json=None, headers=None, timeout=None):
        sink.append(json)
        return Response()

    notify.requests.post = fake
    try:
        yield
    finally:
        notify.requests.post = original


# --------------------------------------------- workflow name resolution


def test_list_action_workflows_returns_urn_and_name(monkeypatch):
    """The router needs two fields to bind its config; nothing more.

    fetch_action_workflows.py exists for full definitions and gets there by
    recursively introspecting the schema. That is the wrong amount of machinery
    for a name lookup, so this is a separate, narrow query.
    """
    from actions.client import DataHubClient

    client = DataHubClient(server="https://example.invalid", token="token")
    monkeypatch.setattr(
        client,
        "execute",
        lambda query, variables: {
            "listActionWorkflows": {
                "workflows": [
                    {"urn": "urn:li:actionWorkflow:a", "name": "Alpha"},
                    {"urn": "urn:li:actionWorkflow:b", "name": "Beta"},
                ]
            }
        },
    )

    assert client.list_action_workflows() == [
        {"urn": "urn:li:actionWorkflow:a", "name": "Alpha"},
        {"urn": "urn:li:actionWorkflow:b", "name": "Beta"},
    ]


def test_list_action_workflows_tolerates_an_instance_with_none(monkeypatch):
    """An empty list is a valid answer, not an error.

    A fresh deployment has no workflows yet. Returning [] lets the resolver
    produce a useful "available: none" message instead of a KeyError.
    """
    from actions.client import DataHubClient

    client = DataHubClient(server="https://example.invalid", token="token")
    monkeypatch.setattr(
        client, "execute", lambda query, variables: {"listActionWorkflows": {"workflows": None}}
    )

    assert client.list_action_workflows() == []


def test_list_action_workflows_drops_entries_with_no_urn(monkeypatch):
    """A workflow with no urn cannot be routed to, so it must not be offered.

    Without this case the happy-path test passes whether or not the filter
    exists, which makes the filter free to delete by accident.
    """
    from actions.client import DataHubClient

    client = DataHubClient(server="https://example.invalid", token="token")
    monkeypatch.setattr(
        client,
        "execute",
        lambda query, variables: {
            "listActionWorkflows": {
                "workflows": [
                    {"urn": "urn:li:actionWorkflow:a", "name": "Alpha"},
                    {"urn": "", "name": "Ghost"},
                    {"name": "NoUrn"},
                ]
            }
        },
    )

    assert client.list_action_workflows() == [
        {"urn": "urn:li:actionWorkflow:a", "name": "Alpha"},
    ]


class _WorkflowListing:
    """Minimal stand-in exposing only what the resolver calls.

    Deliberately not FakeClient: that models the listener's runtime reads, and
    resolution happens at startup before any event exists.
    """

    def __init__(self, pairs: list[tuple[str, str]]):
        self._pairs = pairs

    def list_action_workflows(self) -> list[dict]:
        return [{"urn": urn, "name": name} for urn, name in self._pairs]


def _resolver():
    return WorkflowRouterAction.create.__func__.__globals__["_resolve_workflow_keys"]


def test_a_name_key_resolves_to_the_live_urn():
    listing = _WorkflowListing([(DOMAIN_WF, "Propose New Domain")])
    resolved = _resolver()(listing, {"Propose New Domain": {"label": "x"}})
    assert list(resolved) == [DOMAIN_WF]
    assert resolved[DOMAIN_WF] == {"label": "x"}


def test_an_existing_urn_key_is_left_exactly_as_it_was():
    """The backward-compatibility guarantee: existing configs keep their keys."""
    listing = _WorkflowListing([(DOMAIN_WF, "Propose New Domain")])
    spec = {"label": "x"}
    resolved = _resolver()(listing, {DOMAIN_WF: spec})
    assert resolved == {DOMAIN_WF: spec}


def test_a_urn_that_does_not_exist_here_is_rejected():
    """Without this, a config carried to another deployment still fails silently.

    The workflows table is also a filter, so an unmatched key means decisions
    from that workflow do nothing -- no error, no log line. Resolving only names
    would leave that hole open for URN-keyed entries.
    """
    listing = _WorkflowListing([(DOMAIN_WF, "Propose New Domain")])
    with pytest.raises(ValueError, match="not an Action Workflow on this instance"):
        _resolver()(listing, {"urn:li:actionWorkflow:from-another-deployment": {"label": "x"}})


def test_an_unknown_name_lists_what_is_available():
    listing = _WorkflowListing([(DOMAIN_WF, "Propose New Domain")])
    with pytest.raises(ValueError, match="Propose New Domain"):
        _resolver()(listing, {"Propose New Domian": {"label": "x"}})


def test_a_duplicated_name_is_ambiguous_and_refused():
    """Not hypothetical: two names are already duplicated on the live instance.

    'Dynamic Domain Selection POC' and 'Data Product Access Request' each appear
    twice, so name-keying is a per-entry option rather than a blanket
    replacement. The error has to name both URNs so the operator knows which to
    use without having to run discovery themselves.
    """
    listing = _WorkflowListing([
        ("urn:li:actionWorkflow:one", "Dataset Access Request"),
        ("urn:li:actionWorkflow:two", "Dataset Access Request"),
    ])
    with pytest.raises(ValueError, match="2 Action Workflows are named") as exc_info:
        _resolver()(listing, {"Dataset Access Request": {"label": "x"}})
    msg = str(exc_info.value)
    assert "urn:li:actionWorkflow:one" in msg, f"first URN missing from error: {msg}"
    assert "urn:li:actionWorkflow:two" in msg, f"second URN missing from error: {msg}"


def test_naming_the_same_workflow_twice_is_refused():
    """A name and its own URN both present would silently drop one entry."""
    listing = _WorkflowListing([(DOMAIN_WF, "Propose New Domain")])
    with pytest.raises(ValueError, match="twice"):
        _resolver()(listing, {
            DOMAIN_WF: {"label": "by urn"},
            "Propose New Domain": {"label": "by name"},
        })


def test_an_empty_table_short_circuits_without_a_lookup():
    """No workflows means log everything, so there is nothing to bind."""

    class Exploding:
        def list_action_workflows(self):
            raise AssertionError("must not call the API for an empty table")

    assert _resolver()(Exploding(), {}) == {}


def test_create_refuses_to_start_on_an_unresolvable_workflow(monkeypatch):
    """Fail at startup, not on the first real decision.

    The framework retries an action three times, so a permanent config error
    surfaces as three identical failures while the person waiting on the
    approval gets nothing. create() already fails fast on missing credentials
    and on a notify_ route with no RESEND_API_KEY; this joins them.
    """
    import actions.router as router_mod

    monkeypatch.setenv("DATAHUB_GMS_URL", "https://example.invalid")
    monkeypatch.setenv("DATAHUB_GMS_TOKEN", "token")
    monkeypatch.setattr(
        router_mod,
        "DataHubClient",
        lambda server, token: _WorkflowListing([(DOMAIN_WF, "Propose New Domain")]),
    )

    with pytest.raises(ValueError, match="No Such Workflow"):
        router_mod.WorkflowRouterAction.create(
            {"workflows": {"No Such Workflow": "just a label"}}, ctx=None
        )


def test_create_binds_a_name_keyed_config_to_urns(monkeypatch):
    """The routing table the listener runs on must be urn-keyed.

    act() looks up the spec by the event's workflowUrn, so a name left in place
    would miss and the workflow would be treated as unlisted -- i.e. ignored.
    """
    import actions.router as router_mod

    monkeypatch.setenv("DATAHUB_GMS_URL", "https://example.invalid")
    monkeypatch.setenv("DATAHUB_GMS_TOKEN", "token")
    monkeypatch.setattr(
        router_mod,
        "DataHubClient",
        lambda server, token: _WorkflowListing([(DOMAIN_WF, "Propose New Domain")]),
    )

    action = router_mod.WorkflowRouterAction.create(
        {"workflows": {"Propose New Domain": "just a label"}}, ctx=None
    )
    assert list(action.workflows) == [DOMAIN_WF]


def test_the_shipped_recipe_binds_its_name_keyed_entry_to_a_urn():
    """The YAML ships one name-keyed entry; act() looks specs up by urn.

    Routing tests reach this path indirectly, so a typo in the YAML key would
    surface as an opaque construction failure rather than naming the problem.
    This catches that: it verifies the YAML name key matches the fixture name
    in build()'s _WorkflowListing, confirming the entry resolves to ACCESS_WF
    offline. It does not verify the name against a live instance, which would
    be required to confirm portability to another deployment.
    """
    action, _ = build(request_for(ACCESS_WF, "ACCEPTED", ACCESS_FIELDS, entity=None))

    assert ACCESS_WF in action.workflows, (
        f"the shipped recipe's name-keyed entry did not resolve to {ACCESS_WF}; "
        f"got keys {sorted(action.workflows)}"
    )
    assert not any(k.startswith("Dataset") for k in action.workflows), (
        "a name survived into the routing table, so act() would never match it"
    )


def test_parent_domain_is_submitted_as_a_name_and_resolved_to_a_root_urn():
    """parent_domain is a STRING dropdown now, so the payload carries a name.

    The URN picker could not be constrained to Level 1, so the field became a
    generated list of root names. The listener therefore has to map that name
    back to a urn before anything else -- and scope the lookup to roots, so a
    Level 2 that happens to share a name cannot be selected by accident.
    """
    fields = [
        {"id": "new_domain_name", "values": [{"stringValue": "Payments"}]},
        {"id": "parent_domain", "values": [{"stringValue": "Risk & Compliance"}]},
        {"id": "technical_owners", "values": [{"stringValue": OWNER}]},
        {"id": "data_stewards", "values": [{"stringValue": OWNER}]},
    ]
    action, client = build(request_for(DOMAIN_WF, "ACCEPTED", fields))
    action.act(Event("urn:li:actionRequest:test", "ACCEPTED"))

    created = next(v for op, v in client.mutations if op == "createDomain")
    assert created["input"]["parentDomain"] == PARENT


def test_an_unknown_parent_domain_name_fails_loudly():
    """A stale dropdown option must name the problem, not resolve to nothing.

    Silently treating it as a root proposal would create a top-level domain the
    requester never asked for, and three approvers already signed off on.
    """
    from actions.errors import DataHubError

    fields = [
        {"id": "new_domain_name", "values": [{"stringValue": "Payments"}]},
        {"id": "parent_domain", "values": [{"stringValue": "No Such Domain"}]},
        {"id": "technical_owners", "values": [{"stringValue": OWNER}]},
        {"id": "data_stewards", "values": [{"stringValue": OWNER}]},
    ]
    action, client = build(request_for(DOMAIN_WF, "ACCEPTED", fields))
    with pytest.raises(DataHubError, match="No Such Domain"):
        action.act(Event("urn:li:actionRequest:test", "ACCEPTED"))
