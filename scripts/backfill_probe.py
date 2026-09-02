"""Measure the four things a backfill does that a single load does not.

    python3 scripts/backfill_probe.py --db /tmp/backfill-probe.duckdb

Everything runs against a scratch database the probe creates and deletes, because three of
the four arms damage the warehouse on purpose.

1. Rerunning the same range. The fingerprints have to match and the second pass has to
   report every row as replaced rather than as new.
2. A range covering a day nothing has judged. The plan refuses instead of backfilling
   around the hole.
3. A partition that fails halfway through a range. What committed before it and what did
   not, since the loop is not one transaction.
4. A day the source has dropped. Delete then insert converges the days it is given and
   cannot remove one it is not.
"""

import argparse
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from contracts import spec  # noqa: E402
from warehouse import backfill, load  # noqa: E402

QUARANTINE = os.path.join(ROOT, "data", "quarantine")


def judged(root):
    out = {}
    for name in sorted(os.listdir(root)):
        if name.startswith("created_date="):
            out[name.split("=")[1]] = os.path.join(root, name)
    return out


def sha_lookup():
    with open(os.path.join(ROOT, "data", "manifest.json")) as fh:
        manifest = json.load(fh)
    index = dict((e["partition"], e["sha256"]) for e in manifest["partitions"])
    return lambda p: index.get(p)


def fresh(path, contract):
    if os.path.exists(path):
        os.remove(path)
    con = load.connect(path)
    load.apply_schema(con, contract)
    return con


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--contract",
                    default=os.path.join(ROOT, "contracts", "nyc311.yml"))
    ap.add_argument("--quarantine", default=QUARANTINE)
    ap.add_argument("--db", default=os.path.join(
        tempfile.gettempdir(), "wdc-backfill-probe.duckdb"))
    args = ap.parse_args()

    contract = spec.load(args.contract)
    have = judged(args.quarantine)
    sha_for = sha_lookup()
    days = sorted(have)
    start, end = days[0], days[-1]

    print("{} judged partitions, {} to {}".format(len(have), start, end))

    print()
    print("1. the same range twice")
    con = fresh(args.db, contract)
    work = backfill.plan(have, start, end)
    first = backfill.run(con, contract, work, sha_for)
    fp_first = load.fingerprint(con, contract)
    second = backfill.run(con, contract, work, sha_for)
    fp_second = load.fingerprint(con, contract)
    print("   pass one   {} loaded, {} replaced, fingerprint {}".format(
        first["rows_loaded"], first["rows_replaced"], fp_first))
    print("   pass two   {} loaded, {} replaced, fingerprint {}".format(
        second["rows_loaded"], second["rows_replaced"], fp_second))
    print("   {}".format("identical" if fp_first == fp_second else "DIFFERENT"))
    print("   ledger rows after two passes: {}".format(
        len(backfill.ledger_rows(con))))
    con.close()

    print()
    print("2. a range with a gap in it")
    missing_day = days[len(days) // 2]
    with_hole = dict(have)
    del with_hole[missing_day]
    try:
        backfill.plan(with_hole, start, end)
        print("   NOT REFUSED, which is the bug this check exists for")
    except backfill.MissingPartitions as gap:
        print("   refused, naming {}".format(gap.missing))
    listing = sorted(with_hole)
    print("   a loop over the directory listing would have loaded {} days "
          "and printed a success".format(len(listing)))

    print()
    print("3. a partition that fails in the middle of the range")
    broken_root = tempfile.mkdtemp(prefix="wdc-broken-")
    for day, directory in have.items():
        shutil.copytree(directory, os.path.join(broken_root, "created_date=" + day))
    broken = judged(broken_root)
    victim = days[6]
    report_path = os.path.join(broken[victim], "report.json")
    with open(report_path) as fh:
        report = json.load(fh)
    honest = report["accepted"]
    report["accepted"] = honest + 1
    with open(report_path, "w") as fh:
        json.dump(report, fh)

    con = fresh(args.db, contract)
    try:
        backfill.run(con, contract, backfill.plan(broken, start, end), sha_for)
        print("   NOT STOPPED, which means the count check did not fire")
    except backfill.BackfillStopped as stopped:
        print("   stopped at {} after {} partitions".format(
            stopped.partition, len(stopped.completed)))
        print("   committed: {}".format(", ".join(stopped.completed)))
        print("   cause: {}".format(type(stopped.cause).__name__))
    counts = load.partition_counts(con, contract)
    print("   the table holds {} partitions and {} is {}".format(
        len(counts), victim,
        "in it" if victim in counts else "absent, so that one rolled back"))
    print("   ledger holds {} rows".format(len(backfill.ledger_rows(con))))
    con.close()
    shutil.rmtree(broken_root, ignore_errors=True)

    print()
    print("4. a day the source no longer offers")
    con = fresh(args.db, contract)
    backfill.run(con, contract, backfill.plan(have, start, end), sha_for)
    shorter = dict(have)
    dropped = days[-1]
    del shorter[dropped]
    backfill.run(con, contract, backfill.plan(shorter, start, days[-2]), sha_for)
    left = backfill.orphans(con, contract, list(shorter))
    print("   backfilled {} to {} over a source that no longer has {}".format(
        start, days[-2], dropped))
    print("   orphans: {}".format(", ".join(left) or "none"))
    print("   the table still holds {} rows for it and the ledger agrees, so "
          "reconcile is silent".format(load.partition_rows(con, contract, dropped)))
    print("   drift reported by reconcile: {}".format(
        len(load.reconcile(con, contract))))
    con.close()

    os.remove(args.db)
    return 0


if __name__ == "__main__":
    sys.exit(main())
