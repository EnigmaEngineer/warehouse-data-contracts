#!/usr/bin/env python3
"""Make the pipeline refuse, once per way it knows how, and show what an operator sees.

    python3 scripts/failing_run.py
    python3 scripts/failing_run.py --keep

The list of refusals is read out of the source rather than typed here, so a class added
later shows up as undemonstrated instead of being quietly missed. That is the only reason
this file is worth having over a paragraph in the README. A hand written list of failure
modes grades what its author remembered.

Everything runs against a copy under a temporary directory. Nothing here touches `data/`.
"""

import argparse
import ast
import csv
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from contracts import profile, quarantine, replay, rules, spec, validate  # noqa: E402
from warehouse import backfill, load  # noqa: E402

RAW = os.path.join(ROOT, "data", "raw")
PACKAGES = ("contracts", "warehouse", "ingest")
BASES = ("ValueError", "RuntimeError", "Exception", "KeyError", "IOError")


def refusal_classes(root):
    """Every exception this codebase defines, found by reading it.

    Returns name -> "package/module.py". A base class listed in BASES is what makes a
    class a refusal here. Subclassing one of our own would need a second pass and there
    are none, which is checked rather than assumed.
    """
    found = {}
    for package in PACKAGES:
        directory = os.path.join(root, package)
        for name in sorted(os.listdir(directory)):
            if not name.endswith(".py"):
                continue
            path = os.path.join(directory, name)
            with open(path, encoding="utf-8") as handle:
                tree = ast.parse(handle.read(), filename=path)
            for node in tree.body:
                if not isinstance(node, ast.ClassDef):
                    continue
                bases = [b.id for b in node.bases if isinstance(b, ast.Name)]
                if any(b in BASES for b in bases):
                    found[node.name] = "{}/{}".format(package, name)
                elif any(b in found for b in bases):
                    raise AssertionError(
                        "{} subclasses one of ours, so BASES is not enough".format(
                            node.name))
    return found


def contract():
    with open(os.path.join(ROOT, "contracts", "nyc311.yml"), encoding="utf-8") as fh:
        return spec.parse(fh.read())


def partition_rows(partition):
    path = os.path.join(RAW, "created_date={}.csv".format(partition))
    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        return reader.fieldnames, list(reader), path


def judged(work, con_contract, partition):
    """Judge one real partition into a quarantine directory under `work`."""
    header, rows, path = partition_rows(partition)
    result = validate.validate(con_contract, rows, header=header)
    outdir = os.path.join(work, "created_date=" + partition)
    quarantine.write(outdir, header, rows, result, partition, path=path)
    return outdir



def without_provenance():
    """The real contract with `provenance` removed from the first column rule.

    Editing the shipped file is the point. A minimal contract typed into this script
    would stop exercising the provenance rule the moment the format gained a required
    field, and it would refuse for that reason instead while the label here still said
    provenance. That is the failure this whole file is written against.
    """
    import yaml

    path = os.path.join(ROOT, "contracts", "nyc311.yml")
    with open(path, encoding="utf-8") as fh:
        document = yaml.safe_load(fh)
    first = document["columns"][0]
    if "provenance" not in first:
        raise AssertionError(
            "{} carries no provenance, so this demonstration proves nothing".format(
                first.get("name")))
    del first["provenance"]
    return yaml.safe_dump(document, sort_keys=False)


def show(name, what, action):
    """Run something that should refuse, and check the refusal is the named one.

    Catching any exception and calling it a demonstration is how a probe reports a
    protection that did nothing. The first version of this file did exactly that. Its
    NothingChecked arm raised AttributeError, because the call was wrong, and the run
    still exited 0 with the arm listed as demonstrated.
    """
    print()
    print("--- {} ---".format(name))
    print("    {}".format(what))
    try:
        action()
    except Exception as raised:
        got = type(raised).__name__
        print("    {}: {}".format(got, str(raised).strip()))
        if got != name:
            print("    WRONG REFUSAL, this arm asked for {} and got {}".format(name, got))
            return None
        return got
    print("    NOTHING RAISED, which means this demonstration is broken")
    return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--partition", default="2025-01-13")
    parser.add_argument("--keep", action="store_true", help="leave the scratch directory")
    args = parser.parse_args()

    known = refusal_classes(ROOT)
    work = tempfile.mkdtemp(prefix="wdc-failing-")
    seen = []
    try:
        book = contract()
        good = judged(work, book, args.partition)
        db = os.path.join(work, "wh.duckdb")
        con = load.connect(db)
        load.apply_schema(con, book)
        sha = "0" * 64

        # A directory nobody has judged. This is the one that matters most, because the
        # path it refuses is data/raw itself, which holds the rows the contract rejected.
        seen.append(show(
            "UnjudgedPartition",
            "load data/raw directly, skipping the contract",
            lambda: load.load_partition(con, book, args.partition, RAW, sha)))

        # A report about a different day than the directory it sits in.
        wrong = os.path.join(work, "wrong")
        shutil.copytree(good, wrong)
        report_path = os.path.join(wrong, "report.json")
        with open(report_path, encoding="utf-8") as fh:
            report = json.load(fh)
        report["partition"] = "2025-01-01"
        with open(report_path, "w", encoding="utf-8") as fh:
            json.dump(report, fh)
        seen.append(show(
            "WrongPartition",
            "load a directory whose report is about another day",
            lambda: load.load_partition(con, book, args.partition, wrong, sha)))

        # A report claiming more rows than the file carries.
        miscounted = os.path.join(work, "miscounted")
        shutil.copytree(good, miscounted)
        report_path = os.path.join(miscounted, "report.json")
        with open(report_path, encoding="utf-8") as fh:
            report = json.load(fh)
        report["accepted"] = report["accepted"] + 1
        with open(report_path, "w", encoding="utf-8") as fh:
            json.dump(report, fh)
        seen.append(show(
            "LoadCountMismatch",
            "load a directory whose report claims one row more than the file has",
            lambda: load.load_partition(con, book, args.partition, miscounted, sha)))

        # A file whose header is not the contract's.
        renamed = os.path.join(work, "renamed")
        shutil.copytree(good, renamed)
        accepted = os.path.join(renamed, "accepted.csv")
        with open(accepted, encoding="utf-8") as fh:
            text = fh.read()
        first, rest = text.split("\n", 1)
        with open(accepted, "w", encoding="utf-8") as fh:
            fh.write(first.replace("agency", "agency_name", 1) + "\n" + rest)
        seen.append(show(
            "HeaderMismatch",
            "load a file where one column has been renamed",
            lambda: load.load_partition(con, book, args.partition, renamed, sha)))

        # A backfill over a range with a day missing from it.
        seen.append(show(
            "MissingPartitions",
            "backfill 2025-01-01 to 2025-01-14 with only one day judged",
            lambda: backfill.plan({args.partition: good}, "2025-01-01", "2025-01-14")))

        # A rule with its provenance taken off. Built by editing the real contract
        # rather than by typing a small one, because a hand written contract goes stale
        # the day the format gains a field and then this demonstrates the wrong refusal.
        seen.append(show(
            "ContractError",
            "load the real contract with the provenance stripped off one rule",
            lambda: spec.parse(without_provenance())))

        # A quarantine directory with nothing in it.
        seen.append(show(
            "NoQuarantine",
            "re-judge held rows in a directory that holds no quarantine",
            lambda: replay.rejudge(book, work)))

        # A partition holding none of the contract's columns.
        blank = os.path.join(work, "blank.csv")
        with open(blank, "w", newline="", encoding="utf-8") as fh:
            fh.write("a,b\n1,2\n")
        seen.append(show(
            "NothingChecked",
            "profile a file carrying none of the contract's columns",
            lambda: profile.profile(book, blank, rules)))

        con.close()
    finally:
        if args.keep:
            print()
            print("scratch left at {}".format(work))
        else:
            shutil.rmtree(work, ignore_errors=True)

    demonstrated = sorted(set(n for n in seen if n))
    missing = sorted(set(known) - set(demonstrated))
    print()
    print("{} of {} refusals demonstrated".format(len(demonstrated), len(known)))
    for name in missing:
        print("   not shown here: {:<22} {}".format(name, known[name]))
    print()
    print("The ones not shown are covered in tests/ rather than from a command line.")
    print("IncompletePartition needs the live API and ContentMismatch needs a reader that")
    print("rewrites a value, which is the hive partitioning defect and is fixed.")
    return 0 if len(demonstrated) == len(seen) else 1


if __name__ == "__main__":
    sys.exit(main())
