# Classification tag proposals

Files randomised classification/PII tag proposals against the five
`testworkspaceforpoc` Databricks business tables on pro-lemur, to populate the
Task Center for demos.

## Warning: no dry-run

Invoking this **writes**. `--count 12` files 12 column-level proposals plus one
asset-level proposal per dataset touched, and re-running adds more rather than
replacing. The planned total prints before submission, and every created
`actionRequest` URN prints after so a batch can be rejected in bulk.

## Credentials

Reads `~/.datahubenv_prolemur` (override with `DATAHUB_ENV_FILE`):

```yaml
gms:
  server: https://pro-lemur-fda34e845.trials.acryl.io/gms
  token: <Personal Access Token>
```

Use a **Personal** token so proposals are attributed to you rather than
`admin`. Generate one in DataHub under Settings -> Access Tokens. The token's
expiry is checked locally before anything is written.

## Usage

```bash
python classification-proposal/propose_classifications.py               # list assets, write nothing
python classification-proposal/propose_classifications.py --asset banking_accounts
python classification-proposal/propose_classifications.py --asset all --count 20 --seed 7
```

| Flag | Meaning |
|---|---|
| `--asset` | `banking_accounts`, `banking_loans`, `banking_customers`, `banking_transactions`, `pattern_analysis`, or `all`. Omit to list and exit. |
| `--count` | Column-level proposals (default 12). Asset-level are **extra**: one per dataset. |
| `--seed` | Reproducible run. |

So `--asset all --count 12` files 17 proposals: 12 column-level + 5 asset-level.

## Confidence

Each proposal draws a score in `[0.50, 0.99]`, carried two ways:

- **Note** (`description`, DataHub's proposalNote): `confidence: 87.342% (High)`
- **Native** (`inferenceMetadata.confidenceLevel`): `0` Low `<0.70`, `1` Medium
  `<0.85`, `2` High otherwise

Both derive from one draw, so they cannot disagree. The native field only has
three buckets, which is why the precise score also goes in the note.

## Tests

`pytest` from the repo root runs these alongside the listener suite. The pure
module is covered; the GraphQL client is not, since its only real verification
is a live call.
