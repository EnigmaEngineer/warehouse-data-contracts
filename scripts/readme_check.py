#!/usr/bin/env python3
"""Check the README's transcripts against the programs that print them.

Exits 1 when a published output line no longer matches anything its script can say.
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from docs import blocks


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--doc", default="README.md")
    parser.add_argument("--root", default=".")
    parser.add_argument("--show-ungraded", action="store_true",
                        help="list the output lines nothing claimed")
    parser.add_argument("--holdout", action="store_true",
                        help="damage every graded line and count what comes back")
    args = parser.parse_args()

    with open(args.doc, encoding="utf-8") as handle:
        text = handle.read()

    if args.holdout:
        held = blocks.holdout(text, args.root)
        print("{} damaged lines, {} caught".format(held["total"], held["caught"]))
        print("{} landed inside an argument, so the damaged line is still printable".format(
            held["still_legal"]))
        print("{} detectable, {} caught, rate {:.3f}".format(
            held["detectable"], held["caught"],
            held["caught"] / float(held["detectable"])))
        for label, original, broken in held["missed"]:
            print("   missed, {}: {}".format(label, broken[:88]))
        return 0

    result = blocks.report(text, args.root)

    print("{} fenced blocks, {} of them transcripts".format(
        result["blocks"], result["transcripts"]))
    print("{} run a script in this repo, {} run something else".format(
        len(result["verdicts"]), len(result["unreadable"])))
    for block in result["unreadable"]:
        print("   not gradable, line {}: {}".format(block.start_line, block.command))
    print("{} output lines, {} graded against a print in the source".format(
        result["output_lines"], result["graded_lines"]))

    if args.show_ungraded:
        print()
        for verdict in result["verdicts"]:
            for line in verdict.ungraded:
                print("   ungraded  L{}  {}".format(
                    verdict.block.start_line, line.strip()[:88]))

    print()
    if not result["drifted"]:
        print("no drift, {} graded lines all match".format(result["graded_lines"]))
        return 0

    print("{} published lines no longer match their source".format(
        len(result["drifted"])))
    for verdict, line, template in result["drifted"]:
        print()
        print("   README line under '{}'".format(verdict.block.heading))
        print("     published: {}".format(line.strip()))
        print("     {} line {} prints: {}".format(
            os.path.relpath(template.source, args.root), template.line,
            template.literal.strip()))
    return 1


if __name__ == "__main__":
    sys.exit(main())
