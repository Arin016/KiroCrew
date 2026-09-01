#!/usr/bin/env python3
"""Merge per-shard pytest-split duration files into one whole-suite ledger.

Why this exists
---------------
``ci.yml`` shards the backend suite with pytest-split, which balances shards by
*recorded* per-test runtime when a durations file is present and otherwise falls
back to an even split by test count. The file used to be a committed ~78k-line
JSON blob, refreshed by a weekly workflow that opened a pull request -- a PR this
org's Actions policy forbids Actions from opening, so it was never created and the
file never landed. The ledger is carried in the Actions cache now, and each shard
records only the tests it ran, so the pieces have to be stitched back together.

Disjointness is pytest-split's own contract: every collected test belongs to
exactly one group. That is what makes a plain dict merge sound, and it is checked
rather than assumed -- an overlap means the split changed shape and the merge is
no longer valid.

Refusing a partial merge is deliberate. A ledger missing one shard's tests is
worse than no ledger at all: pytest-split would assign every absent test the
average duration and rebalance every future run on that fiction, which is a
silent, self-perpetuating skew. A missing shard exits 0 with a warning instead --
the caller simply does not cache anything and the next run keeps the count-based
split, which is the documented fallback.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

SHARD_GLOB = ".test_durations_shard_*"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--shard-dir",
        required=True,
        type=pathlib.Path,
        help="Directory holding the downloaded per-shard duration files.",
    )
    ap.add_argument(
        "--expected",
        required=True,
        type=int,
        help="Number of shard files that must be present (SHARD_COUNT).",
    )
    ap.add_argument(
        "--out",
        required=True,
        type=pathlib.Path,
        help="Path to write the merged ledger to.",
    )
    args = ap.parse_args(argv)

    if args.expected < 1:
        print(f"::error::--expected must be >= 1, got {args.expected}", file=sys.stderr)
        return 2

    files = sorted(args.shard_dir.glob(SHARD_GLOB))
    if len(files) != args.expected:
        # Not an error: see the module docstring. No ledger is written, so the
        # caller's `hashFiles` guard skips the cache save and the next run keeps
        # pytest-split's count-based fallback.
        print(
            f"::warning::{len(files)}/{args.expected} shard duration files present "
            "-- not writing a partial ledger; shards keep the count-based split."
        )
        return 0

    merged: dict[str, float] = {}
    for path in files:
        try:
            with path.open(encoding="utf-8") as fh:
                part = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            print(f"::error::cannot read {path.name}: {exc}", file=sys.stderr)
            return 1
        if not isinstance(part, dict):
            print(
                f"::error::{path.name} is {type(part).__name__}, expected a JSON object",
                file=sys.stderr,
            )
            return 1
        overlap = merged.keys() & part.keys()
        if overlap:
            sample = sorted(overlap)[:3]
            print(
                f"::error::shards overlap on {len(overlap)} test id(s), e.g. {sample} "
                "-- pytest-split's groups are meant to be disjoint, so refusing to merge.",
                file=sys.stderr,
            )
            return 1
        merged.update(part)

    if not merged:
        print("::error::merged ledger is empty -- refusing to cache it.", file=sys.stderr)
        return 1

    # sort_keys so a rerun over identical input produces an identical file, which
    # keeps the cache entry stable and makes any real change visible in a diff.
    with args.out.open("w", encoding="utf-8") as fh:
        json.dump(merged, fh, indent=2, sort_keys=True)

    total = sum(merged.values())
    print(
        f"ledger: {len(merged)} tests from {len(files)} shards, "
        f"{total / 60:.1f} min recorded -> {args.out}"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
