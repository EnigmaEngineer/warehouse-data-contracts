#!/usr/bin/env python3
"""Print the warehouse lineage, or check the README's copy of it.

    python scripts/lineage.py
    python scripts/lineage.py --check
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from docs import lineage

MARKER = "## Lineage, read out of the models"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", default="dbt")
    parser.add_argument("--doc", default="README.md")
    parser.add_argument("--check", action="store_true",
                        help="compare the README block against the models and exit 1 on a difference")
    args = parser.parse_args()

    graph = lineage.nodes(args.project)
    drawn = lineage.render(graph, lineage.sources(args.project))

    if not args.check:
        print(drawn)
        return 0

    with open(args.doc, encoding="utf-8") as handle:
        text = handle.read()
    block = lineage.published(text, MARKER)
    if block is None:
        print("no block under '{}' in {}".format(MARKER, args.doc))
        return 1
    if block == drawn:
        print("the published lineage matches the models, {} nodes".format(len(graph)))
        return 0

    print("the published lineage no longer matches the models")
    for left, right in zip(block.split("\n"), drawn.split("\n")):
        if left != right:
            print("  published: {}".format(left))
            print("  models:    {}".format(right))
    if len(block.split("\n")) != len(drawn.split("\n")):
        print("  the two blocks are different lengths, {} against {}".format(
            len(block.split("\n")), len(drawn.split("\n"))))
    return 1


if __name__ == "__main__":
    sys.exit(main())
