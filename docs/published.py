"""The documents this repo publishes, read as one text.

The README used to hold everything, so every check over published figures opened that one
file. The long sections moved into docs/ to keep the front page short, and a check that
kept reading only the README would have quietly stopped grading most of what it graded.
So the checks read all four files, in the order the old README ran.

A missing file raises rather than being skipped. A grader that reads less than it thinks
it does reports a clean pass over nothing, which is the failure this package exists for.
"""

import os

FILES = ("README.md", "docs/design-notes.md", "docs/running-the-checks.md",
         "docs/limitations.md")


def read(root):
    parts = []
    for name in FILES:
        with open(os.path.join(root, name), encoding="utf-8") as handle:
            parts.append(handle.read())
    return "".join(parts)
