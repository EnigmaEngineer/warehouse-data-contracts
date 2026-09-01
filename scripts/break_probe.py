"""Break the contract on purpose and check that the refusal came from the contract.

    python3 scripts/break_probe.py
    python3 scripts/break_probe.py --partition 2025-01-06 --count 3
    python3 scripts/break_probe.py --mart /tmp/wh.duckdb

Every break damages rows the contract currently accepts, judges the file, then judges it
again against a contract with the one rule under test removed. A break whose control column
says `held` is not testing the rule its name claims.

The last row of the table gets through on purpose. `wrong_agency` writes a real agency
acronym into the wrong row. Every rule on that column passes, because the contract
constrains the shape of the value and says nothing about whether it is true. That is the
boundary of the claim that the marts are protected.

With --mart the probe goes further and rebuilds. It copies the warehouse at the path given
and replaces one partition with the damaged one. Then it runs dbt and compares the gold
tables against the copy it started from. That arm needs dbt on the machine, and the path
given has to hold a warehouse scripts/load_raw.py has already built.
"""

import argparse
import csv
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from contracts import breaks, quarantine, spec, validate  # noqa: E402
from warehouse import load  # noqa: E402

RAW = os.path.join(ROOT, "data", "raw")


def read_partition(partition):
    path = os.path.join(RAW, "created_date={}.csv".format(partition))
    with open(path, newline="") as fh:
        reader = csv.DictReader(fh)
        return reader.fieldnames, list(reader), path


def gold_digest(con):
    """A hash of every gold table, plus the number of requests they say happened.

    The tables are discovered rather than named, so a mart added later is covered without
    anybody remembering to edit this.

    The request total is carried separately because a changed hash says nothing about the
    direction. Holding a row takes the total down by one. Mislabelling a row leaves the
    total alone and moves it between groups. Those are different failures and the hash puts
    them in the same bucket.
    """
    tables = con.execute(
        "select table_schema, table_name from information_schema.tables "
        "where table_schema like 'gold%' order by 1, 2"
    ).fetchall()
    if not tables:
        raise RuntimeError(
            "no gold tables in this database, so a comparison over them says nothing")
    digest = hashlib.sha256()
    rows = 0
    for schema_name, table in tables:
        got = con.execute(
            'select * from "{}"."{}" order by all'.format(schema_name, table)).fetchall()
        rows += len(got)
        digest.update("{}.{}\x1e".format(schema_name, table).encode("utf-8"))
        for row in got:
            digest.update(("\x1f".join(str(v) for v in row) + "\x1e").encode("utf-8"))
    total = con.execute(
        "select sum(request_count) from gold.gold_agency_daily").fetchone()[0]
    return ("{}:{}".format(rows, digest.hexdigest()[:12]), len(tables), int(total))


def rebuild_mart(baseline_db, contract, partition, directory, sha):
    """Copy the warehouse, load the damaged partition into the copy, run dbt on it.

    A copy rather than the original, because a probe that damages the thing it is measuring
    can only ever be run once.
    """
    workdir = tempfile.mkdtemp(prefix="wdc-break-")
    scratch = os.path.join(workdir, "wh.duckdb")
    shutil.copy(baseline_db, scratch)

    con = load.connect(scratch)
    before, tables, before_total = gold_digest(con)
    load.load_partition(con, contract, partition, directory, sha)
    con.close()

    env = dict(os.environ)
    env["WDC_DUCKDB"] = scratch
    result = subprocess.run(
        ["bash", os.path.join(ROOT, "scripts", "dbt.sh"), "build"],
        env=env, capture_output=True, text=True)

    con = load.connect(scratch)
    after, _, after_total = gold_digest(con)
    con.close()
    shutil.rmtree(workdir, ignore_errors=True)
    return {
        "before": before, "after": after, "tables": tables,
        "before_total": before_total, "after_total": after_total,
        "exit": result.returncode,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--contract",
                    default=os.path.join(ROOT, "contracts", "nyc311.yml"))
    ap.add_argument("--partition", default="2025-01-06")
    ap.add_argument("--count", type=int, default=1,
                    help="rows to damage per break")
    ap.add_argument("--mart", help="path to a built warehouse, runs the dbt arm")
    args = ap.parse_args()

    contract = spec.load(args.contract)
    header, rows, path = read_partition(args.partition)
    clean, baseline = breaks.clean_indexes(contract, rows, header=header)

    print("partition {} holds {} rows, {} of them clean".format(
        args.partition, len(rows), len(clean)))
    print()
    print("{:<22} {:<52} {:>5} {:>4} {:>4} {:<6} {:<10}".format(
        "break", "rule expected to catch it", "wrote", "held", "amp", "named",
        "control"))

    failures = 0
    for brk in breaks.CATALOGUE:
        out = breaks.run_break(contract, rows, brk, count=args.count, header=header)
        ok = (out.caught and out.by_the_named_rule and out.control_clean
              and not out.collateral)
        if not ok:
            failures += 1
        # A break expecting no hold has no rule to remove, so its control is the contract
        # unchanged. Printing `clean` there would show a check that never ran.
        control = "clean" if out.control_clean else "STILL HELD"
        if not brk.expects_hold:
            control = "n/a"
        print("{:<22} {:<52} {:>5} {:>4} {:>4.1f} {:<6} {:<10}".format(
            brk.name, brk.expect if brk.expects_hold else "none, nothing reads it",
            out.injected, len(out.new_holds), out.amplification,
            "yes" if out.by_the_named_rule else "NO", control))
        if out.collateral:
            print("{:<22} and {} rows it did not aim at: {}".format(
                "", len(out.collateral), sorted(out.collateral)[:5]))

    print()
    print("{} of {} breaks behaved as named".format(
        len(breaks.CATALOGUE) - failures, len(breaks.CATALOGUE)))

    # The unbounded version of the duplicate key break. One replayed upstream job.
    doubled = breaks.replay_partition(rows, times=2)
    replayed = validate.validate(contract, doubled, header=header)
    only_key = replayed.held_only_by("unique")
    print()
    print("the partition replayed once:")
    print("  {} rows in, {} held, {:.4f} of the file".format(
        replayed.rows, replayed.bad_rows, replayed.bad_rows / replayed.rows))
    print("  {} of those are held only by a key collision".format(len(only_key)))
    print("  largest group sharing one key is {}".format(replayed.largest_collision()))
    print("  on the untouched partition it is {} and {} held only by a collision".format(
        baseline.largest_collision(), len(baseline.held_only_by("unique"))))

    if args.mart:
        print()
        print("mart arm, rebuilding from {}".format(args.mart))
        for name in ("not_allowed", "wrong_agency"):
            brk = breaks.by_name(name)
            chosen = breaks.targets(clean, args.count)
            working = list(rows)
            for index in chosen:
                brk.damage(working, index, clean[-1])
            result = validate.validate(contract, working, header=header)

            outdir = tempfile.mkdtemp(prefix="wdc-q-")
            quarantine.write(outdir, header, working, result, args.partition, path)
            got = rebuild_mart(args.mart, contract, args.partition, outdir,
                               "break-probe")
            shutil.rmtree(outdir, ignore_errors=True)

            print("  {:<14} held {}  dbt exit {}  gold over {} tables {}".format(
                name, result.bad_rows - baseline.bad_rows, got["exit"], got["tables"],
                "unchanged" if got["before"] == got["after"] else "CHANGED"))
            print("    hash   {} then {}".format(got["before"], got["after"]))
            print("    gold says {} requests, then {}, a difference of {}".format(
                got["before_total"], got["after_total"],
                got["after_total"] - got["before_total"]))

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
