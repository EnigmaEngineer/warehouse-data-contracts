"""Run the generated dbt tests against the rows the validator refused.

    python scripts/generated_tests_probe.py --db /tmp/wh.duckdb

Run them against the accepted table and they all pass, and that result is worth nothing.
The accepted table is what came out of the quarantine, so a test on it can only ever
confirm that the split did what the splitter said it did. Every generated test sits
downstream of the thing it would have to disagree with.

So point them at the other side. The quarantined rows are the ones the validator held for
breaking a rule. Each generated test names one rule and one column, and a held row that
broke that rule has to make its test fail. If it does not, the two implementations disagree
about the same contract line and one of them is wrong.

This reads dbt's own compiled SQL out of the target directory rather than writing the
queries again. A third implementation graded against the other two would be the problem
this probe exists to look for.
"""

import argparse
import csv
import glob
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import duckdb

from contracts import spec

COMPILED = os.path.join(
    ROOT, "dbt", "target", "compiled", "nyc311_warehouse",
    "models", "staging", "_generated.yml")

# The schema and table half of the relation the generated tests name. The database half is
# whatever WDC_DUCKDB pointed at when dbt compiled, so matching on it would tie this probe
# to one filename.
RELATION = '"raw"."nyc311_service_requests"'
SWAPPED = '"probe"."nyc311_service_requests"'


def held_rows(root, contract):
    """Every quarantined row across every partition, with its reasons."""
    columns = [c.name for c in contract.columns]
    out = []
    pattern = os.path.join(root, "data", "quarantine", "created_date=*",
                           "quarantined.csv")
    for path in sorted(glob.glob(pattern)):
        # The partition is the directory name and the file does not repeat it. Read it off
        # the path deliberately rather than letting a reader infer it, which is the defect
        # warehouse/load.py exists to refuse.
        partition = os.path.basename(os.path.dirname(path)).split("=", 1)[1]
        with open(path, newline="") as fh:
            for row in csv.DictReader(fh):
                row["_partition"] = partition
                out.append(row)
    if not out:
        raise SystemExit(
            "no quarantined rows under data/quarantine, so this probe checked nothing")
    return columns, out


def load_probe_table(con, columns, rows):
    con.execute("create schema if not exists probe")
    con.execute("drop table if exists probe.nyc311_service_requests")
    cols = ", ".join('"{}" VARCHAR'.format(c) for c in columns)
    con.execute("create table probe.nyc311_service_requests ({}, "
                '"_partition" VARCHAR)'.format(cols))
    names = columns + ["_partition"]
    placeholders = ", ".join("?" for _ in names)
    con.executemany(
        "insert into probe.nyc311_service_requests values ({})".format(placeholders),
        [[row.get(c) for c in columns] + [row.get("_partition", "")] for row in rows])


def compiled_tests():
    out = {}
    for path in sorted(glob.glob(os.path.join(COMPILED, "*.sql"))):
        with open(path) as fh:
            out[os.path.basename(path)[:-4]] = fh.read()
    if not out:
        raise SystemExit(
            "no compiled tests under {}. run scripts/dbt.sh build first".format(COMPILED))
    return out


def control(con, tests, columns):
    """Break two rows in the probe table and require the matching tests to fire.

    Named tests rather than a count, because "some test fired" is satisfied by any of them
    and the point is that the swap reaches the specific column being poisoned. Nine tests
    all coming back silent is also what a probe pointed at an empty table looks like, and
    what a swap that quietly did nothing looks like.
    """
    names = columns + ["_partition"]
    blank = dict((c, "x") for c in columns)
    blank["_partition"] = "control"
    rows = [dict(blank), dict(blank)]
    rows[0]["agency"] = None
    rows[1]["borough"] = "ATLANTIS"
    con.executemany(
        "insert into probe.nyc311_service_requests values ({})".format(
            ", ".join("?" for _ in names)),
        [[r.get(c) for c in names] for r in rows])

    fired = {}
    for name, sql in tests.items():
        swapped = re.sub(re.escape(RELATION), SWAPPED, sql)
        fired[name] = con.execute(
            "select count(*) from ({}) t".format(swapped)).fetchone()[0]

    con.execute('delete from probe.nyc311_service_requests '
                "where \"_partition\" = 'control'")

    want = [n for n in tests if n.endswith("_agency")]
    want += [n for n in tests if "accepted_values" in n and "borough" in tests[n]]
    if len(want) != 2:
        raise SystemExit(
            "the control expects two tests to name and found {}".format(len(want)))

    print()
    print("control, two poisoned rows added to the probe table")
    for name in want:
        short = name.replace("source_", "").replace(
            "_contract_nyc311_service_requests", "")
        print("  {:<58} {}".format(short[:58], fired[name]))
        if fired[name] == 0:
            raise SystemExit(
                "control failed: {} did not fire on a poisoned row. The swap is not "
                "reaching the probe table and the silence above means nothing".format(
                    name))
    print("  control passed, so the silence above is the data and not the probe")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.path.join(ROOT, "data", "warehouse.duckdb"))
    ap.add_argument("--contract",
                    default=os.path.join(ROOT, "contracts", "nyc311.yml"))
    args = ap.parse_args()

    contract = spec.load(args.contract)
    columns, rows = held_rows(ROOT, contract)
    tests = compiled_tests()

    con = duckdb.connect(args.db)
    load_probe_table(con, columns, rows)

    reasons = {}
    for row in rows:
        for reason in (row.get("_contract_failures") or "").split("|"):
            if reason:
                reasons[reason] = reasons.get(reason, 0) + 1

    print("{} quarantined rows over {} partitions".format(
        len(rows), len({r.get("_partition") for r in rows})))
    print("reasons the validator gave, most common first")
    for reason, count in sorted(reasons.items(), key=lambda kv: -kv[1])[:8]:
        print("  {:<44} {}".format(reason, count))
    print()

    print("{} generated tests, run against the accepted table and the held rows".format(
        len(tests)))
    # dbt fails a test when its query returns any row, so the number below is that row
    # count and not a count of bad rows. For not_null the two are the same. For
    # accepted_values the query groups, so it returns one row per offending value.
    print("  {:<58} {:>10} {:>8}".format("test", "accepted", "held"))
    summary = []
    for name, sql in sorted(tests.items()):
        accepted = con.execute(
            "select count(*) from ({}) t".format(sql)).fetchone()[0]
        swapped = re.sub(re.escape(RELATION), SWAPPED, sql)
        if swapped == sql:
            raise SystemExit(
                "{} does not name {}, so the swap did nothing".format(name, RELATION))
        held = con.execute("select count(*) from ({}) t".format(swapped)).fetchone()[0]
        short = name.replace("source_", "").replace(
            "_contract_nyc311_service_requests", "")
        print("  {:<58} {:>10} {:>8}".format(short[:58], accepted, held))
        summary.append({"test": name, "accepted": accepted, "held": held})

    silent = [s for s in summary if s["held"] == 0]
    print()
    print("{} of {} generated tests find nothing in the held rows".format(
        len(silent), len(summary)))

    # Every test coming back silent is also what a probe pointed at an empty table looks
    # like, and what a swap that quietly did nothing looks like. So poison the probe table
    # and require two named tests to notice. Without this the headline above is
    # indistinguishable from a broken probe.
    control(con, tests, columns)
    con.close()


if __name__ == "__main__":
    main()
