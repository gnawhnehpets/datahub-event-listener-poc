#!/usr/bin/env python3
"""File randomised classification tag proposals against the POC datasets.

There is no dry-run, by explicit choice: invoking this writes. The planned
total is printed before anything is submitted so the arithmetic is visible
first, and every created actionRequest URN is printed after so a batch can be
found and rejected in bulk.

    python classification-proposal/propose_classifications.py --asset banking_accounts
"""

from __future__ import annotations

import argparse
import logging
import random
import sys

from datahub_client import CredentialError, DataHubClient, DataHubError, load_credentials
from proposals import ASSETS, generate_proposals

logger = logging.getLogger("propose")


def _print_urns(created: list[tuple[str, str]]) -> None:
    """Print the actionRequest URN block, or nothing if the list is empty."""
    if created:
        print("\nactionRequest URNs (use these to reject the batch):")
        for _, urn in created:
            print(f"  {urn}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--asset",
        help="short name from the catalog, or 'all'. Omit to list and exit.",
    )
    parser.add_argument(
        "--count",
        type=int,
        default=12,
        help="column-level proposals (default 12). Asset-level are extra: one per dataset.",
    )
    parser.add_argument("--seed", type=int, help="make a run reproducible")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args = parse_args(argv)

    # No --asset is not an error; it is the safe default for a tool that writes
    # on invocation. Guessing a target here would be worse than asking.
    if not args.asset:
        print("Choose a target with --asset. Available:\n")
        for name in sorted(ASSETS):
            print(f"  {name}")
        print("  all\n")
        return 1

    if args.asset == "all":
        chosen = sorted(ASSETS)
    elif args.asset in ASSETS:
        chosen = [args.asset]
    else:
        print(f"unknown asset {args.asset!r}. Known: {', '.join(sorted(ASSETS))}, all")
        return 1

    if args.count < 0:
        print("--count must be zero or greater")
        return 1

    try:
        creds = load_credentials()
    except CredentialError as exc:
        print(f"credentials: {exc}")
        return 1

    client = DataHubClient(creds)

    try:
        asset_columns = {name: client.fetch_columns(ASSETS[name]) for name in chosen}
    except DataHubError as exc:
        print(f"could not read schemas: {exc}")
        return 1

    empty = [name for name, cols in asset_columns.items() if not cols]
    if empty:
        print(f"no columns found for: {', '.join(empty)}")
        return 1

    rng = random.Random(args.seed)
    proposals = generate_proposals(asset_columns, count=args.count, rng=rng)

    asset_level = sum(1 for p in proposals if p.sub_resource is None)
    print(
        f"\nSubmitting {len(proposals)} proposals "
        f"({len(proposals) - asset_level} column-level + {asset_level} asset-level) "
        f"across {len(chosen)} dataset(s). No dry-run: these are live writes.\n"
    )

    created: list[tuple[str, str]] = []
    failed: list[tuple[str, str]] = []
    try:
        for proposal in proposals:
            target = proposal.sub_resource or "<asset-level>"
            label = f"{proposal.resource_urn.split(',')[1]}.{target} -> {proposal.tag_name}"
            try:
                # One failure must not cost the rest of the batch: a partial run
                # whose successes went unrecorded is the worst outcome here.
                urn = client.propose_tag(proposal)
                # Record the URN immediately after it is returned, before any
                # other work (including KeyboardInterrupt) can execute. If we
                # did anything between the return value and the append, an
                # interrupt could leave a proposal on the server with no URN
                # printed -- an orphan nobody can find to clean up.
                created.append((label, urn))
            except DataHubError as exc:
                failed.append((label, str(exc)))
                print(f"  FAIL  {label}  {proposal.note}  -- {exc}")
            else:
                print(f"  OK    {label}  {proposal.note}")
    except KeyboardInterrupt:
        # Print whatever we collected before the interrupt so the caller has
        # the URN list needed to reject a partial batch. A raw traceback is not
        # useful here; the URNs are.
        print(f"\n[interrupted] {len(created)} created, {len(failed)} failed before Ctrl-C")
        _print_urns(created)
        return 1

    print(f"\n{len(created)} created, {len(failed)} failed")
    _print_urns(created)
    # Exit 1 whenever any proposal failed, even if others succeeded.
    # "Per-proposal failures are never fatal" means do not abort mid-batch;
    # it does not mean hide failures from the caller. A script checking
    # `... && echo done` must not get a false all-clear on a partial run.
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
