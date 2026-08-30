"""Fetch every partition again and record what the source changed underneath us.

    python3 scripts/refetch_probe.py --into /tmp/second
    python3 scripts/refetch_probe.py --into /tmp/second --write

`--write` saves the result to data/extract_diff.json. That file is committed, because the
live source keeps moving and nobody can fetch the extract this repo measured. That includes
a later run of this script. The diff is the record and scripts/scd2_probe.py replays it.

This talks to the network. It is not in tests/ for that reason and the comparison it uses
is, in ingest/compare.py.
"""

import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from ingest import compare, fetch  # noqa: E402

RAW = os.path.join(ROOT, "data", "raw")
DIFF = os.path.join(ROOT, "data", "extract_diff.json")


def partitions():
    out = []
    for name in sorted(os.listdir(RAW)):
        if name.startswith("created_date=") and name.endswith(".csv"):
            out.append(name[len("created_date="):-len(".csv")])
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--into", required=True, help="where to put the second extract")
    ap.add_argument("--write", action="store_true",
                    help="save the diff to data/extract_diff.json")
    ap.add_argument("--partition", action="append",
                    help="one partition, repeatable. default is all of them")
    args = ap.parse_args()

    days = args.partition or partitions()
    if not days:
        print("no partitions under {}".format(RAW))
        return 2

    os.makedirs(args.into, exist_ok=True)
    diffs = []
    for day in days:
        before = os.path.join(RAW, "created_date={}.csv".format(day))
        entry = fetch.fetch_day(day, args.into)
        after = os.path.join(args.into, "created_date={}.csv".format(day))
        diff = compare.diff_extracts(day, before, after, fetch.COLUMNS)
        diffs.append(diff)
        print("{}  rows {:>6}  changed rows {:>3}  cells {:>3}  added {:>2}  "
              "removed {:>2}  sha same {}".format(
                  day, diff.rows_after, diff.changed_rows(), diff.changed_cells(),
                  len(diff.added), len(diff.removed),
                  entry["sha256"] == fetch.sha256(before)))

    moved = {}
    for d in diffs:
        for column, n in d.columns_that_moved().items():
            moved[column] = moved.get(column, 0) + n

    print()
    print("{} partitions, {} rows".format(
        len(diffs), sum(d.rows_after for d in diffs)))
    print("{} partitions changed".format(sum(1 for d in diffs if not d.is_empty())))
    print("{} rows changed, {} cells".format(
        sum(d.changed_rows() for d in diffs), sum(d.changed_cells() for d in diffs)))
    print("columns that moved: {}".format(moved or "none"))

    for d in diffs:
        for key, cells in sorted(d.changed.items()):
            for column, (was, now) in sorted(cells.items()):
                print("  {} {} {}: {!r} -> {!r}".format(
                    d.partition, key, column, was, now))

    if args.write:
        compare.save(diffs, DIFF)
        print()
        print("wrote {}".format(os.path.relpath(DIFF, ROOT)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
