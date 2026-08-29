"""Load every quarantined partition into the raw layer.

    python3 scripts/load_raw.py
    python3 scripts/load_raw.py --partition 2025-01-06
    python3 scripts/load_raw.py --twice

Reads data/quarantine/created_date=*/accepted.csv, so run scripts/quarantine_partition.py
--write first. --twice runs the whole load a second time and prints both fingerprints,
because a load that has only ever been run once has not been shown to be idempotent.
"""

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from contracts import spec  # noqa: E402
from warehouse import load  # noqa: E402

QUARANTINE = os.path.join(ROOT, "data", "quarantine")
DB = os.path.join(ROOT, "data", "warehouse.duckdb")


def partitions():
    if not os.path.isdir(QUARANTINE):
        return []
    out = []
    for name in sorted(os.listdir(QUARANTINE)):
        if not name.startswith("created_date="):
            continue
        out.append((name.split("=")[1], os.path.join(QUARANTINE, name)))
    return out


def sha_for(manifest, partition):
    for entry in manifest.get("partitions", []):
        if entry["partition"] == partition:
            return entry["sha256"]
    return None


def run(con, contract, todo, manifest, verbose=True):
    total = 0
    replaced = 0
    for name, directory in todo:
        result = load.load_partition(
            con, contract, name, directory, sha_for(manifest, name))
        total += result["rows_loaded"]
        replaced += result["rows_replaced"]
        if verbose:
            print("{:<12} {:>8} loaded {:>8} replaced {:>6} held".format(
                name, result["rows_loaded"], result["rows_replaced"],
                result["rows_held"]))
    return total, replaced


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--contract",
                    default=os.path.join(ROOT, "contracts", "nyc311.yml"))
    ap.add_argument("--db", default=DB)
    ap.add_argument("--partition")
    ap.add_argument("--twice", action="store_true",
                    help="load everything a second time and compare fingerprints")
    args = ap.parse_args()

    contract = spec.load(args.contract)
    todo = partitions()
    if args.partition:
        todo = [t for t in todo if t[0] == args.partition]
    if not todo:
        print("nothing under {}. run scripts/quarantine_partition.py --write "
              "first".format(QUARANTINE))
        return 2

    with open(os.path.join(ROOT, "data", "manifest.json")) as fh:
        manifest = json.load(fh)

    con = load.connect(args.db)
    load.apply_schema(con, contract)

    total, replaced = run(con, contract, todo, manifest)
    first = load.fingerprint(con, contract)
    print()
    print("{} rows over {} partitions, {} replaced".format(total, len(todo), replaced))
    print("fingerprint {}".format(first))

    if args.twice:
        second_total, second_replaced = run(con, contract, todo, manifest, verbose=False)
        second = load.fingerprint(con, contract)
        print()
        print("second pass {} rows, {} replaced".format(second_total, second_replaced))
        print("fingerprint {}".format(second))
        print("identical" if first == second else "DIFFERENT")

    drift = load.reconcile(con, contract)
    if drift:
        print()
        print("ledger and table disagree on {} partitions".format(len(drift)))
        for d in drift:
            print("  {} table {} ledger {}".format(
                d["partition"], d["in_table"], d["in_ledger"]))
        con.close()
        return 1

    print("ledger agrees with the table on all {} partitions".format(
        len(load.partition_counts(con, contract))))
    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
