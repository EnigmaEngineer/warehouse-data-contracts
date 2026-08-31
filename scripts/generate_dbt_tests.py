"""Write the generated dbt schema tests and print how much of the contract reached them.

    python scripts/generate_dbt_tests.py
    python scripts/generate_dbt_tests.py --check

`--check` regenerates into memory and compares, so a contract edited without regenerating
fails rather than shipping a stale file. That is the same drift problem the hand written
status list had, and generation moves it rather than removing it.
"""

import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from contracts import generate, spec

OUT = os.path.join(ROOT, "dbt", "models", "staging", "_generated.yml")


def build(contract_path):
    contract = spec.load(contract_path)
    return generate.render(contract, os.path.basename(contract_path),
                           "contract", "raw", "nyc311_service_requests")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--contract", default=os.path.join(ROOT, "contracts", "nyc311.yml"))
    ap.add_argument("--out", default=OUT)
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()

    text = build(args.contract)
    contract = spec.load(args.contract)
    mapped = generate.map_contract(contract)
    counts = generate.coverage(mapped)

    total = len(mapped)
    print("{} contract constraints".format(total))
    for outcome in (generate.GENERATED, generate.NO_DBT_EQUIVALENT,
                    generate.NOTHING_TO_GENERATE):
        print("  {:<22} {}".format(outcome, counts[outcome]))
    print("  reaching dbt: {} of {}, {:.1%}".format(
        counts[generate.GENERATED], total, counts[generate.GENERATED] / float(total)))
    print()
    print("what did not make it")
    for m in mapped:
        if m.outcome == generate.NO_DBT_EQUIVALENT:
            print("  {:<28} {:<20} {}".format(m.subject, m.rule, m.why))

    if args.check:
        if not os.path.exists(args.out):
            raise SystemExit("{} does not exist, run without --check".format(args.out))
        with open(args.out) as fh:
            current = fh.read()
        if current != text:
            raise SystemExit(
                "{} is stale, the contract has moved since it was written".format(
                    args.out))
        print()
        print("{} is current".format(os.path.relpath(args.out, ROOT)))
        return

    with open(args.out, "w") as fh:
        fh.write(text)
    print()
    print("wrote {}".format(os.path.relpath(args.out, ROOT)))


if __name__ == "__main__":
    main()
