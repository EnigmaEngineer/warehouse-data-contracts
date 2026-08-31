"""Run the two partition level clauses and print what they could and could not answer.

    python scripts/feed_checks.py
    python scripts/feed_checks.py --reference wall_clock --applies-to every_partition

The overrides are there so the readings can be compared. They change the run and they do
not change the contract, because a clause you can edit from a command line is not a clause.

Two things this deliberately does not do. It does not average a lag across partitions,
because a mean over one judged partition and thirteen skipped ones is a number about the
scope rather than about the feed. And it does not choose a volume floor. It prints the
distance between the floor in the contract and the smallest thing ever observed, and what a
fitted floor would cost on a corpus this short.
"""

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from contracts import feed, spec


def newest_events(contract, entries, root):
    column = contract.source["partition_column"]
    out = {}
    for entry in entries:
        path = os.path.join(root, "data", entry["path"])
        if not os.path.exists(path):
            continue
        out[entry["partition"]] = feed.newest_event(path, column)
    return out


def tally(verdicts):
    counts = {}
    for v in verdicts:
        counts[v.verdict] = counts.get(v.verdict, 0) + 1
    return counts


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--contract", default=os.path.join(ROOT, "contracts", "nyc311.yml"))
    ap.add_argument("--manifest", default=os.path.join(ROOT, "data", "manifest.json"))
    ap.add_argument("--reference", choices=feed.REFERENCES)
    ap.add_argument("--applies-to", choices=feed.SCOPES)
    ap.add_argument("--holdout", type=int, default=5)
    args = ap.parse_args()

    contract = spec.load(args.contract)
    if args.reference:
        contract.freshness["reference"] = args.reference
    if args.applies_to:
        contract.freshness["applies_to"] = args.applies_to

    with open(args.manifest) as fh:
        entries = json.load(fh)["partitions"]
    if not entries:
        raise SystemExit("the manifest holds no partitions, so nothing was checked")

    print("{} partitions from {}".format(len(entries), os.path.basename(args.manifest)))
    print()

    newest = newest_events(contract, entries, ROOT)
    verdicts = feed.freshness(contract, entries, newest)
    reference = contract.freshness["reference"]
    print("freshness  reference={}  applies_to={}  limit={}h".format(
        reference, contract.freshness["applies_to"],
        contract.freshness["max_lag_hours"]))
    for name, count in sorted(tally(verdicts).items()):
        print("  {:<18} {}".format(name, count))
    for v in verdicts:
        if v.lag_hours is not None:
            print("  {} lag {:.1f}h -> {}".format(v.partition, v.lag_hours, v.verdict))
    fetched = sum(1 for e in entries if e.get("fetched_at"))
    print("  partitions carrying a fetched_at: {} of {}".format(fetched, len(entries)))
    print()

    vol = feed.volume(contract, entries)
    print("volume  floor={}".format(contract.volume["min_rows_per_partition"]))
    for name, count in sorted(tally(vol).items()):
        print("  {:<18} {}".format(name, count))

    room = feed.headroom(contract, entries)
    print("  smallest partition {}, largest {}".format(room.smallest, room.largest))
    print("  floor sits {} rows below the smallest, a ratio of {:.2f}".format(
        room.gap, room.ratio))
    print("  can this floor ever fire on data like this: {}".format(room.can_bind))
    print()

    counts = [e["rows"] for e in sorted(entries, key=lambda e: e["partition"])]
    fit = feed.fit_floor(counts, holdout=args.holdout)
    print("a fitted floor, {} train and {} held out".format(
        len(fit.train), len(fit.test)))
    print("  median {:.0f}  mad {:.0f}  floor at median minus {}x mad {:.0f}".format(
        fit.centre, fit.spread, fit.k, fit.floor))
    print("  degenerate band (mad of zero): {}".format(fit.degenerate))
    print("  out of sample fires {} of {}, rate {:.3f}".format(
        len(fit.fires()), len(fit.test), fit.out_of_sample_rate))
    print("  smallest rate this holdout can report at all: {:.3f}".format(
        fit.rate_resolution))
    print("  so any gate below {:.3f} is unmeasurable on this corpus".format(
        fit.rate_resolution))


if __name__ == "__main__":
    main()
