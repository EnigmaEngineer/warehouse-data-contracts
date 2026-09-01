"""Reload a date range into the raw layer, and say what the range could not fix.

    python3 scripts/backfill.py --start 2025-01-01 --end 2025-01-14
    python3 scripts/backfill.py --start 2025-01-03 --end 2025-01-05 --db /tmp/wh.duckdb

Reads data/quarantine/created_date=*/, so run scripts/quarantine_partition.py --write
first. The range decides the work, not the directory listing, so a day inside the range with
nothing judged is a refusal rather than a partition quietly skipped.

Orphans are printed at the end. Those are partitions sitting in the table that the source no
longer offers, and a backfill cannot remove them because delete then insert only touches the
days it was given.
"""

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from contracts import spec  # noqa: E402
from warehouse import backfill, load  # noqa: E402

QUARANTINE = os.path.join(ROOT, "data", "quarantine")
DB = os.path.join(ROOT, "data", "warehouse.duckdb")


def judged(root):
    out = {}
    if not os.path.isdir(root):
        return out
    for name in sorted(os.listdir(root)):
        if name.startswith("created_date="):
            out[name.split("=")[1]] = os.path.join(root, name)
    return out


def sha_lookup(manifest_path):
    with open(manifest_path) as fh:
        manifest = json.load(fh)
    index = dict((e["partition"], e["sha256"]) for e in manifest["partitions"])
    return lambda partition: index.get(partition)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--contract",
                    default=os.path.join(ROOT, "contracts", "nyc311.yml"))
    ap.add_argument("--quarantine", default=QUARANTINE)
    ap.add_argument("--db", default=DB)
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--twice", action="store_true",
                    help="run the range again and compare fingerprints")
    args = ap.parse_args()

    contract = spec.load(args.contract)
    have = judged(args.quarantine)
    if not have:
        print("nothing judged under {}. run scripts/quarantine_partition.py "
              "--write first".format(args.quarantine))
        return 2

    try:
        work = backfill.plan(have, args.start, args.end)
    except backfill.MissingPartitions as gap:
        print("refused: {}".format(gap))
        return 1

    sha_for = sha_lookup(os.path.join(ROOT, "data", "manifest.json"))
    con = load.connect(args.db)
    load.apply_schema(con, contract)

    print("{:<12} {:>8} {:>9} {:>6}".format("partition", "loaded", "replaced", "held"))
    try:
        summary = backfill.run(con, contract, work, sha_for)
    except backfill.BackfillStopped as stopped:
        print()
        print("stopped at {}".format(stopped.partition))
        print("committed before it: {}".format(
            ", ".join(stopped.completed) or "nothing"))
        print("cause: {}".format(stopped.cause))
        con.close()
        return 1

    for result in summary["results"]:
        print("{:<12} {:>8} {:>9} {:>6}".format(
            result["partition"], result["rows_loaded"], result["rows_replaced"],
            result["rows_held"]))

    first = load.fingerprint(con, contract)
    print()
    print("{} partitions, {} rows loaded, {} replaced".format(
        len(summary["partitions"]), summary["rows_loaded"], summary["rows_replaced"]))
    print("fingerprint {}".format(first))

    if args.twice:
        again = backfill.run(con, contract, work, sha_for)
        second = load.fingerprint(con, contract)
        print()
        print("second pass replaced {} rows".format(again["rows_replaced"]))
        print("fingerprint {}".format(second))
        print("identical" if first == second else "DIFFERENT")

    left = backfill.orphans(con, contract, list(have))
    if left:
        print()
        print("{} partitions in the table the source no longer has: {}".format(
            len(left), ", ".join(left)))
        print("a backfill cannot remove these. it only touches the days it is given")

    drift = load.reconcile(con, contract)
    if drift:
        print()
        print("ledger and table disagree on {} partitions".format(len(drift)))
        con.close()
        return 1

    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
