# Generate random classification tag proposals against Databricks POC assets

**Date:** 2026-09-23
**Status:** designed, not yet implemented

## Problem

Demoing DataHub's proposal review flow needs proposals to review. Right now the
only way to get a Task Center populated with classification proposals against
the `testworkspaceforpoc` Databricks assets is to click through the UI one tag
at a time.

We want a script that fabricates a plausible-looking batch: an automated
classifier has scanned some columns, decided each one looks like PII or needs a
sensitivity label, and filed a tag proposal carrying its confidence.

The interesting constraint is **confidence has to survive the round trip in a
form a reviewer can act on**. DataHub models inference confidence natively, but
only as three buckets — `confidenceLevel` 0/1/2 (Low/Medium/High). A demo that
shows `87.342%` needs the precise number somewhere the reviewer can see it,
because the native field cannot hold it.

## API findings

Established by introspecting the live pro-lemur GraphQL schema (v2.1.3).

`proposeTags(input: ProposeTagsInput)` accepts:

| Field | Purpose |
|---|---|
| `resourceUrn` | target dataset |
| `tagUrns` | tags to propose |
| `subResourceType: DATASET_FIELD` | makes it a **column-level** proposal |
| `subResource` | the column's `fieldPath` |
| `description` | documented as *"Optional description providing context for the proposal. Also called proposalNote"* |
| `origin` | `MANUAL` or `INFERRED` |
| `inferenceMetadata` | **required when `origin: INFERRED`** |

`InferenceMetadataInput` carries `version: Int`, `lastInferredAt: Long`, and
`confidenceLevel: Int` where 0=Low, 1=Medium, 2=High.

So both halves exist: `description` is the free-text Note, `confidenceLevel` is
the native signal. Using both means the proposal renders as genuinely inferred
*and* keeps the precise score visible. They are derived from one random draw, so
they cannot disagree.

Nothing in the API prevents multiple proposals on the same (column, tag) pair.

## Scope

**Targets** — the 5 business tables in `testworkspaceforpoc`:

| Dataset | Columns |
|---|---|
| `banking_accounts` | 11 |
| `banking_loans` | 11 |
| `banking_customers` | 12 |
| `banking_transactions` | 11 |
| `pattern_analysis` | 12 |

The other 29 datasets in that workspace are Databricks `information_schema`
system tables. Proposing classification tags on `information_schema.columns` is
noise, so they are excluded.

**Tag pool** — classification levels and PII only, all verified to exist:

| Tag | URN |
|---|---|
| PII | `urn:li:tag:PII` |
| PII_Data | `urn:li:tag:sample_data_PII_Data` |
| Confidential | `urn:li:tag:Confidential` |
| Restricted | `urn:li:tag:Restricted` |
| Public | `urn:li:tag:Public` |

## Design

```
classification-proposal/
  propose_classifications.py   # CLI entrypoint, wiring only
  datahub_client.py            # credential load + GraphQL transport (all I/O)
  proposals.py                 # tag pool + generation (pure, no network)
  README.md
  tests/test_proposals.py
```

`proposals.py` is pure: given assets, columns, tags and a seeded RNG it returns
a list of frozen `Proposal` dataclasses. That is the part with logic worth
testing, and it is testable without a DataHub instance. `datahub_client.py`
owns every network call. The entrypoint wires them together and formats output.

**Flow:** load and validate credentials → resolve the chosen asset's columns
live via GraphQL → generate proposals → submit each via `proposeTags` → print
created `actionRequest` URNs.

Two kinds of proposal are generated:

| Kind | Count | `subResourceType` / `subResource` |
|---|---|---|
| Column-level | `--count` (default 12) | `DATASET_FIELD` / the column's `fieldPath` |
| Asset-level | exactly 1 per dataset touched | both omitted |

The only structural difference is the presence of the sub-resource fields.
Omitting them is what makes DataHub treat the proposal as applying to the
dataset itself — the same distinction visible in existing pro-lemur data, where
`WORKFLOW_FORM_REQUEST` rows carry no `subResource` and term proposals do.

Tag choice, confidence draw and note formatting are identical for both kinds,
so asset-level proposals go through the same code path with the sub-resource
fields left unset.

Columns are fetched live rather than hardcoded so the script does not drift as
the POC datasets change.

### Confidence

One random float per proposal in `[0.50, 0.99]`. It drives both outputs:

| Score | `confidenceLevel` | Label |
|---|---|---|
| `< 0.70` | 0 | Low |
| `0.70 – 0.85` | 1 | Medium |
| `>= 0.85` | 2 | High |

Note text is exactly:

```
confidence: 87.342% (High)
```

Percent is the score as a percentage to **exactly 3 decimal places**, always
padded (`50.000%`, not `50.0%`) so the notes align when read as a column.
Formatted `f"{score * 100:.3f}%"`.

Bucketing uses the **unrounded** score, so a draw of `0.849999` is Medium and
prints `84.9999% → 85.000% (Medium)`. That is a deliberate trade: the displayed
percent can read as `85.000%` next to `(Medium)` about once in every few
thousand draws. Rounding before bucketing would instead make the *native*
`confidenceLevel` disagree with the score actually drawn, which is worse —
the native field is the one a reviewer filters on.

The boundaries are half-open on the upper end: 0.70 is Medium, 0.85 is High.

### CLI

```
propose_classifications.py --asset banking_accounts [--count 12] [--seed 42]
```

- `--asset` takes a short name from the 5 tables, or `all`.
- Invoked with no `--asset`, it prints the available assets and exits without
  writing. Guessing a target for a mutating call is worse than asking.
- `--count` is the number of **column-level** proposals for the run, not per
  asset. With `--asset all` the target dataset is drawn at random per proposal,
  so counts land unevenly across the 5 tables rather than in fixed shares.
- Asset-level proposals are **additional to `--count`** — one per dataset the
  run touches. So `--asset banking_accounts --count 12` files 13 proposals, and
  `--asset all --count 12` files 17 (12 column + 5 asset).
- The run prints its total before submitting, so the arithmetic is never a
  surprise in a tool with no dry-run.
- `--seed` makes a run reproducible.
- Duplicate (column, tag) pairs are **allowed, not deduped** — multiple
  proposals on one attribute is a requirement, not an accident.

There is no dry-run mode, by explicit choice of the owner. The script writes on
invocation.

### Re-run behaviour

Re-running accumulates additional proposals of both kinds; nothing is updated
or replaced. A second run against the same dataset adds another `--count`
column proposals and another asset-level one.

Stacking is confirmed against live data: `loans_banking.risk_category`
currently holds three distinct PENDING term proposals simultaneously.

What is **not** confirmed is whether an *identical* proposal — same dataset,
same column (or same dataset at asset level), same tag — stacks or is rejected
as a duplicate. A sweep of all 170 action requests on pro-lemur found no exact
triple occurring twice, consistent either with server-side deduplication or
simply with nobody having done it by hand.

Asset-level proposals make this markedly more likely than column-level ones.
There are only 5 tags, so a second run against the same dataset has a 1-in-5
chance of redrawing the same asset-level tag, and by the fourth run a repeat is
more likely than not. Column-level collisions are rarer per pair but there are
more draws: 5 tags across ~11 columns is 55 combinations, so `--count 12`
already has a fair chance of repeating a pair within one run.

Either way this is answered by running, not by reasoning. Per-proposal failures
are recorded rather than fatal, so a duplicate rejection shows up in the
summary without costing the rest of the batch.

### Credentials

Read from `~/.datahubenv_prolemur`, the standard DataHub CLI config:

```yaml
gms:
  server: https://pro-lemur-fda34e845.trials.acryl.io/gms
  token: <PERSONAL token>
```

Parsed with `yaml`, six lines, rather than taking an `acryl-datahub` dependency
for config loading alone. `DATAHUB_ENV_FILE` overrides the path.

The GraphQL endpoint is derived by replacing the `/gms` suffix with
`/api/graphql`, because the mutations are served by the frontend proxy.

**The token is validated before any write.** Its `exp` claim is decoded locally
and an expired token aborts the run with the expiry date and a pointer to
regenerate — not a bare HTTP 401 from the first mutation.

> At time of writing the stored token expired **2026-09-02** and returns 401.
> It must be refreshed before the script can run. It is a `PERSONAL` token for
> `stephen.hwang@datahub.com`, which is the identity we want — proposals are
> attributed to a real user rather than `admin`.

### Error handling

A failed proposal is caught, recorded, and the run continues. The summary
distinguishes succeeded from failed with the server's error message. Aborting
mid-batch would leave an unknown number of proposals filed with no record of
which — the same "writes happened but nothing shows it" failure mode that made
the original Task Center investigation hard.

### Testing

pytest against `proposals.py`, no network:

- seeded generation is deterministic
- confidence buckets are correct **at** the 0.70 and 0.85 boundaries
- percent renders to exactly 3 decimals and is zero-padded (`50.000%`)
- note text agrees with its own bucket (no High note on a level-1 proposal)
- duplicate (column, tag) pairs are permitted
- `--count` yields exactly that many **column-level** proposals
- exactly one asset-level proposal per dataset touched — one for a single
  `--asset`, five for `--asset all`
- asset-level proposals omit `subResource`/`subResourceType`; column-level ones
  always set both
- every generated tag URN is in the pool; every column belongs to the asset

`datahub_client.py` is not unit-tested — it is thin I/O whose only real
verification is a live call.

## Risks

`proposeTags` has **not** been exercised end-to-end; the shape above comes from
schema introspection. The first real run doubles as the integration test.

With no dry-run, `--count 12` files 12 live Task Center entries, and re-running
doubles that. Created URNs are printed so a batch can be found and cleaned up.

## Out of scope

- Term proposals (`proposeTerms`) — an earlier request, superseded by tags.
- The `information_schema` tables.
- Cleanup/rollback tooling. Proposals can be rejected in bulk in the UI, or via
  `rejectProposals(urns, note)`, which the summary output gives you the URNs for.
