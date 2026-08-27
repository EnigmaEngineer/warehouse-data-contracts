"""Measure the contract against every partition in data/raw and print the result.

    python scripts/profile_source.py
    python scripts/profile_source.py --markdown

Everything printed here comes out of contracts/profile.py and contracts/rules.py, both of
which carry tests. This file reads paths, loops and formats.
"""

import argparse
import glob
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from contracts import profile as profile_mod
from contracts import rules, spec

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW = os.path.join(ROOT, "data", "raw")
CONTRACT = os.path.join(ROOT, "contracts", "nyc311.yml")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw", default=RAW)
    ap.add_argument("--contract", default=CONTRACT)
    ap.add_argument("--markdown", action="store_true")
    args = ap.parse_args(argv)

    contract = spec.load(args.contract)
    paths = sorted(glob.glob(os.path.join(args.raw, "created_date=*.csv")))
    if not paths:
        print("no partitions under {}".format(args.raw))
        print("run scripts/pull_source.py first")
        return 2

    print("contract {} carries {} constraints over {} columns, {} of them asserted".format(
        contract.dataset,
        contract.constraint_count(),
        len(contract.columns),
        contract.asserted_count(),
    ))
    print("{} partitions under {}".format(len(paths), os.path.relpath(args.raw, ROOT)))
    print()

    # rule -> total rows across every partition that failed it
    totals = {}
    check_totals = {}
    total_rows = 0
    uncontracted = set()
    rules_run = 0

    for path in paths:
        p = profile_mod.profile(contract, path, rules)
        total_rows += p.rows
        rules_run += sum(len(r.column.rules) + 1 for r in p.results) + len(p.checks)
        uncontracted |= set(p.uncontracted)
        for result in p.results:
            for v in result.violations:
                key = (result.column.name, v.rule, result.column.provenance)
                entry = totals.setdefault(key, {"rows": 0, "examples": []})
                entry["rows"] += v.count
                for e in v.examples:
                    if len(entry["examples"]) < 3 and e not in entry["examples"]:
                        entry["examples"].append(e)
        for cr in p.checks:
            entry = check_totals.setdefault(
                cr.check.name, {"rows": 0, "considered": 0, "kind": cr.check.kind}
            )
            entry["rows"] += cr.count
            entry["considered"] += cr.considered

    # Say what was evaluated, always. A report that prints "clean" without saying how much
    # it looked at will eventually print "clean" having looked at nothing.
    print("{} rows over {} partitions, {} rule evaluations".format(
        total_rows, len(paths), rules_run))
    print()
    print("single column rules")

    if args.markdown:
        print("| column | rule | provenance | rows | rate | examples |")
        print("|---|---|---|---|---|---|")
    ordered = sorted(totals.items(), key=lambda kv: -kv[1]["rows"])
    for (column, rule, provenance), entry in ordered:
        rate = entry["rows"] / total_rows
        examples = ", ".join(repr(e) for e in entry["examples"]) or "-"
        if args.markdown:
            print("| {} | {} | {} | {} | {:.4f} | {} |".format(
                column, rule, provenance, entry["rows"], rate, examples))
        else:
            print("{:16s} {:14s} {:10s} {:>8} {:>8.4f}  {}".format(
                column, rule, provenance, entry["rows"], rate, examples))

    if not totals:
        print("  nothing fired. {} constraints, {} rows, zero hits.".format(
            contract.constraint_count(), total_rows))

    print()
    print("cross column checks")
    if args.markdown:
        print("| check | kind | rows judged | rows failing | rate |")
        print("|---|---|---|---|---|")
    for name, entry in sorted(check_totals.items(), key=lambda kv: -kv[1]["rows"]):
        rate = entry["rows"] / entry["considered"] if entry["considered"] else 0.0
        if args.markdown:
            print("| {} | {} | {} | {} | {:.5f} |".format(
                name, entry["kind"], entry["considered"], entry["rows"], rate))
        else:
            print("  {:34s} {:14s} judged {:>7}  failing {:>5}  {:.5f}".format(
                name, entry["kind"], entry["considered"], entry["rows"], rate))
    check_rows = sum(e["rows"] for e in check_totals.values())
    print("  cross column failures: {} rows, {:.5f} of the corpus".format(
        check_rows, check_rows / total_rows))

    print()
    asserted = sum(e["rows"] for (c, r, p), e in totals.items() if p == "asserted")
    documented = sum(e["rows"] for (c, r, p), e in totals.items() if p == "documented")
    print("rule hits by provenance, counted as rows:")
    print("  documented {}".format(documented))
    print("  asserted   {}".format(asserted))
    print("distinct rules that fired: {} of {}".format(
        len(totals), contract.constraint_count()))
    if uncontracted:
        print("columns in the data with no contract: {}".format(sorted(uncontracted)))

    unevaluated = profile_mod.unevaluated_clauses(contract)
    if unevaluated:
        print()
        print("contract clauses nothing here evaluates: {}".format(", ".join(unevaluated)))
        print("  they are in the file and they are not enforced by anything yet")
    return 0


if __name__ == "__main__":
    sys.exit(main())
