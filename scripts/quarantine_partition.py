"""Validate every fetched partition row by row and quarantine what the contract refuses.

    python3 scripts/quarantine_partition.py
    python3 scripts/quarantine_partition.py --partition 2025-01-06 --write
    python3 scripts/quarantine_partition.py --rejudge

Without --write nothing is written and the table is the output. With it, each partition
gets accepted.csv, quarantined.csv and report.json under data/quarantine/.

The three row counts in the summary are the point. Two of them are wrong in opposite
directions and both are easy to reach by accident.

--rejudge reads the quarantine back instead of writing it. Against the contract that wrote
those files it has to recover nothing, and a row it recovers is a value that did not survive
the round trip to disk. Point it at a changed contract with --contract to see which refusals
that change would lift.
"""

import argparse
import csv
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from contracts import quarantine, replay, spec, validate  # noqa: E402

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


def rejudge(contract, only=None):
    """Judge every held row again. Returns 1 if anything was recovered.

    Nonzero on a recovery is deliberate when the contract has not changed, because that is
    the only case this command is run in by default and a recovery there is a defect. Pass
    a changed contract and read the table rather than the exit code.
    """
    names = sorted(n for n in os.listdir(OUT) if n.startswith("created_date=")) \
        if os.path.isdir(OUT) else []
    if only:
        names = [n for n in names if n.endswith("=" + only)]
    if not names:
        print("nothing under {}. run this with --write first".format(OUT))
        return 2

    print("{:<12} {:>8} {:>10} {:>10}".format(
        "partition", "held", "recovered", "reworded"))
    total_held = 0
    total_recovered = 0
    lifted = {}
    for name in names:
        out = replay.rejudge(contract, os.path.join(OUT, name))
        total_held += out["held"]
        total_recovered += out["recovered"]
        for rule, n in out["reasons_that_stopped_firing"].items():
            lifted[rule] = lifted.get(rule, 0) + n
        print("{:<12} {:>8} {:>10} {:>10}".format(
            name.split("=")[1], out["held"], out["recovered"],
            len(out["rows_held_for_a_different_reason"])))

    print()
    print("{} held rows re-judged over {} partitions".format(total_held, len(names)))
    print("{} would now be accepted".format(total_recovered))
    for rule, n in sorted(lifted.items()):
        print("  {} stopped firing on {} rows".format(rule, n))
    if not total_recovered:
        print("nothing recovered, which is what an unchanged contract has to say")
    return 1 if total_recovered else 0


def quarantine_dirs(only=None):
    if not os.path.isdir(OUT):
        return []
    names = sorted(n for n in os.listdir(OUT) if n.startswith("created_date="))
    if only:
        names = [n for n in names if n.endswith("=" + only)]
    return [os.path.join(OUT, n) for n in names]


def marginal(contract, only=None):
    """Print what each constraint is the sole reason for, over the whole quarantine."""
    dirs = quarantine_dirs(only)
    if not dirs:
        print("nothing under {}. run this with --write first".format(OUT))
        return 2

    rows_held = sum(replay.rejudge(contract, d)["held"] for d in dirs)
    table = replay.marginal(contract, dirs)

    print("{:<52} {:>10}".format("constraint", "sole reason"))
    carrying = 0
    for label, recovered, reason in table:
        if recovered is None:
            print("{:<52} {:>10}  {}".format(label, "n/a", reason))
            continue
        if recovered:
            carrying += 1
        print("{:<52} {:>10}".format(label, recovered))

    removable = [t for t in table if t[1] is not None]
    print()
    print("{} constraints, {} of them removable one at a time".format(
        len(table), len(removable)))
    print("{} of those {} are the only thing holding at least one row".format(
        carrying, len(removable)))
    print("{} held rows over {} partitions".format(rows_held, len(dirs)))
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--contract",
                    default=os.path.join(ROOT, "contracts", "nyc311.yml"))
    ap.add_argument("--partition", help="one partition, by date. default is all of them")
    ap.add_argument("--write", action="store_true",
                    help="write the split and the report to data/quarantine")
    ap.add_argument("--rejudge", action="store_true",
                    help="read the quarantine back and judge it against the contract")
    ap.add_argument("--marginal", action="store_true",
                    help="how many held rows each constraint is the only reason for")
    args = ap.parse_args()

    contract = spec.load(args.contract)

    if args.rejudge:
        return rejudge(contract, args.partition)

    if args.marginal:
        return marginal(contract, args.partition)

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
