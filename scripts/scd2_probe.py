"""Load two extracts of the same fourteen days and show what the history table keeps.

    python3 scripts/scd2_probe.py --db /tmp/wh.duckdb

Builds the warehouse from the first extract and snapshots it. Then it replays the recorded
diff to produce the second extract, puts that through the contract and the load, and
snapshots again. Then it prints what survived.

The point of the arm is that the raw table cannot answer this. A load is delete then
insert on the partition key, so the second extract overwrites the first and the previous
value is gone. Every count check in the load still passes, because the row counts are
identical. Only the snapshot holds both.

The second extract is replayed from data/extract_diff.json rather than fetched, so this
reproduces. The diff itself was measured against the live API by
scripts/refetch_probe.py and the source has kept moving since.
"""

import argparse
import csv
import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from contracts import quarantine, spec, validate  # noqa: E402
from ingest import compare, fetch  # noqa: E402
from warehouse import history, load  # noqa: E402


def dbt(command, db):
    env = dict(os.environ)
    env["WDC_DUCKDB"] = db
    result = subprocess.run(
        ["bash", os.path.join(HERE, "dbt.sh"), command],
        env=env, capture_output=True, text=True)
    if result.returncode != 0:
        sys.stdout.write(result.stdout[-3000:])
        sys.stderr.write(result.stderr[-2000:])
        raise SystemExit("dbt {} failed".format(command))
    return result.stdout


def judge_and_load(con, contract, partition, csv_path, workdir, sha):
    """Put one file through the contract and into the warehouse, the way the DAG does."""
    with open(csv_path, newline="") as fh:
        reader = csv.DictReader(fh)
        fieldnames = reader.fieldnames
        rows = list(reader)

    result = validate.validate(contract, rows, header=fieldnames)
    outdir = os.path.join(workdir, "created_date={}".format(partition))
    quarantine.write(outdir, fieldnames, rows, result, partition, csv_path)
    return load.load_partition(con, contract, partition, outdir, sha)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.path.join(ROOT, "data", "warehouse.duckdb"))
    ap.add_argument("--workdir", default="/tmp/wdc-scd2")
    args = ap.parse_args()

    contract = spec.load(os.path.join(ROOT, "contracts", "nyc311.yml"))
    diffs = compare.load(os.path.join(ROOT, "data", "extract_diff.json"))
    moving = [d for d in diffs if not d.is_empty()]
    if not moving:
        raise SystemExit(
            "the recorded diff has no changed partition, there is nothing to demonstrate")

    if os.path.isdir(args.workdir):
        shutil.rmtree(args.workdir)
    os.makedirs(args.workdir)

    print("first extract")
    subprocess.run(
        [sys.executable, os.path.join(HERE, "load_raw.py"), "--db", args.db],
        check=True, stdout=subprocess.DEVNULL)
    # build rather than snapshot. The snapshot reads the staging model, so it needs the
    # models built first, and build runs the tests in the same pass. That means the second
    # load has to leave the marts passing as well as leaving a history behind.
    dbt("build", args.db)

    con = load.connect(args.db)
    before = history.version_summary(con)
    print("  {versions} versions over {keys} keys, {current} current, "
          "{superseded} superseded".format(**before))

    print()
    print("second extract, {} of {} partitions changed".format(len(moving), len(diffs)))
    manifest = {e["partition"]: e for e in json.load(
        open(os.path.join(ROOT, "data", "manifest.json")))["partitions"]}

    for diff in moving:
        source = os.path.join(ROOT, "data", "raw",
                              "created_date={}.csv".format(diff.partition))
        replayed = os.path.join(args.workdir, "second-{}.csv".format(diff.partition))
        compare.apply_diff(diff, source, replayed, fetch.COLUMNS)
        result = judge_and_load(con, contract, diff.partition, replayed,
                                args.workdir, manifest[diff.partition]["sha256"])
        print("  {}  {} loaded, {} replaced, {} cells checked".format(
            diff.partition, result["rows_loaded"], result["rows_replaced"],
            result["cells_checked"]))
    con.close()

    # build rather than snapshot. The snapshot reads the staging model, so it needs the
    # models built first, and build runs the tests in the same pass. That means the second
    # load has to leave the marts passing as well as leaving a history behind.
    dbt("build", args.db)

    con = load.connect(args.db)
    after = history.version_summary(con)
    print("  {versions} versions over {keys} keys, {current} current, "
          "{superseded} superseded".format(**after))

    broken = history.one_row_per_key_is_current(con)
    if broken:
        print()
        print("keys without exactly one open version: {}".format(broken[:5]))
        con.close()
        return 1

    print()
    print("keys carrying history: {}".format(len(history.keys_with_history(con))))
    for key, versions in sorted(history.keys_with_history(con).items()):
        for v in versions:
            print("  {}  status {:<12} closed_at {}  valid_to {}".format(
                key, v["status"], v["closed_at"], v["valid_to"]))

    fingerprint = load.fingerprint(con, contract)
    print()
    print("raw fingerprint after the second load: {}".format(fingerprint))
    print("the raw table now holds only the second extract, "
          "{} superseded rows live in the snapshot alone".format(after["superseded"]))
    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
