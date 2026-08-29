"""What duckdb decides each column is, one partition at a time.

    python3 scripts/sniff_probe.py

Nothing in the load path uses type inference. This exists to show why. The reader is
allowed to guess here and the table it prints is the argument for not letting it guess
anywhere else.
"""

import argparse
import glob
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from contracts import spec  # noqa: E402
from warehouse import load, schema  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--contract",
                    default=os.path.join(ROOT, "contracts", "nyc311.yml"))
    ap.add_argument("--raw", default=os.path.join(ROOT, "data", "raw"))
    args = ap.parse_args()

    contract = spec.load(args.contract)
    paths = sorted(glob.glob(os.path.join(args.raw, "*.csv")))
    if not paths:
        print("no partitions under {}".format(args.raw))
        return 2

    con = load.connect(":memory:")
    columns = schema.source_columns(contract)

    print("{:<12} {}".format(
        "partition", " ".join(c[:9].rjust(9) for c in columns)))
    for path in paths:
        types = schema.inferred_types(con, path)
        name = os.path.basename(path).split("=")[1][:-4]
        print("{:<12} {}".format(
            name, " ".join((types.get(c) or "-")[:9].rjust(9) for c in columns)))

    disagreements = schema.type_disagreements(con, paths)
    print()
    print("{} files, {} columns, {} where the guess is not stable".format(
        len(paths), len(columns), len(disagreements)))
    for column in sorted(disagreements):
        for type_, files in sorted(disagreements[column].items()):
            print("  {:<14} {:<9} {} files".format(column, type_, len(files)))
            print("    {}".format(", ".join(
                os.path.basename(f).split("=")[1][:-4] for f in files)))

    print()
    print("reading all {} at once:".format(len(paths)))
    try:
        con.execute("select count(*) from read_csv(?, header=true)",
                    [os.path.join(args.raw, "*.csv")]).fetchone()
        print("  accepted")
    except Exception as exc:
        print("  refused, {}".format(str(exc).splitlines()[0]))

    damage = schema.inference_damage(con, contract, paths)
    print()
    print("first partition decides the schema, the rest are appended into it:")
    print("  typed from {}".format(os.path.basename(damage["typed_from"])))
    print("  {} rows, {} cells".format(damage["rows"], damage["cells"]))
    if damage["changed"]:
        for column in sorted(damage["changed"]):
            print("  {:<16} {:>8} cells differ from the text they arrived as".format(
                column, damage["changed"][column]))
    else:
        print("  nothing changed")

    print()
    print("the load passes columns= explicitly, so none of this reaches the table")
    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
