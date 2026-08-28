"""Validate every fetched partition row by row and quarantine what the contract refuses.

    python3 scripts/quarantine_partition.py
    python3 scripts/quarantine_partition.py --partition 2025-01-06 --write

Without --write nothing is written and the table is the output. With it, each partition
gets accepted.csv, quarantined.csv and report.json under data/quarantine/.

The three row counts in the summary are the point. Two of them are wrong in opposite
directions and both are easy to reach by accident.
"""

import argparse
import csv
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from contracts import quarantine, spec, validate  # noqa: E402

RAW = os.path.join(ROOT, "data", "raw")
OUT = os.path.join(ROOT, "data", "quarantine")


def partitions():
    if not os.path.isdir(RAW):
        return []
    names = sorted(n for n in os.listdir(RAW) if n.endswith(".csv"))
    return [(n.split("=")[1][:-4], os.path.join(RAW, n)) for n in names]


def read(path):
    with open(path, newline="") as fh:
        reader = csv.DictReader(fh)
        return reader.fieldnames, list(reader)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--contract",
                    default=os.path.join(ROOT, "contracts", "nyc311.yml"))
    ap.add_argument("--partition", help="one partition, by date. default is all of them")
    ap.add_argument("--write", action="store_true",
                    help="write the split and the report to data/quarantine")
    args = ap.parse_args()

    contract = spec.load(args.contract)

    todo = partitions()
    if args.partition:
        todo = [(p, path) for p, path in todo if p == args.partition]
    if not todo:
        print("no partitions under {}. run scripts/pull_source.py first".format(RAW))
        return 2

    print("{:<12} {:>8} {:>8} {:>8} {:>7} {:>7} {:>10}".format(
        "partition", "rows", "accept", "held", "sum", "worst", "evals"))

    total_rows = 0
    total_held = 0
    total_sum = 0
    total_multi = 0

    for name, path in todo:
        fieldnames, rows = read(path)
        result = validate.validate(contract, rows, header=fieldnames)
        summary = quarantine.report(result, name, path)

        if args.write:
            quarantine.write(os.path.join(OUT, "created_date=" + name),
                             fieldnames, rows, result, name, path)

        print("{:<12} {:>8} {:>8} {:>8} {:>7} {:>7} {:>10}".format(
            name, summary["rows"], summary["accepted"], summary["held"],
            summary["sum_of_rule_counts"], summary["largest_rule_count"],
            summary["rule_evaluations"]))

        total_rows += summary["rows"]
        total_held += summary["held"]
        total_sum += summary["sum_of_rule_counts"]
        total_multi += summary["rows_breaking_more_than_one_rule"]

    print()
    print("{} rows over {} partitions".format(total_rows, len(todo)))
    print("held        {} rows, {:.5f} of the corpus".format(
        total_held, total_held / total_rows if total_rows else 0))
    print("sum of the per rule counts {}, which is {} more than the rows held".format(
        total_sum, total_sum - total_held))
    print("{} held rows broke more than one rule".format(total_multi))

    if args.write:
        print()
        print("written under {}".format(OUT))
    return 0


if __name__ == "__main__":
    sys.exit(main())
