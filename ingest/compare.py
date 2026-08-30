"""Compare two extracts of the same partition and record what moved.

The source is not immutable and the whole repo was written as though it were. A partition
is fetched once, checksummed, and treated from then on as a fixed thing. Fetch the same
fourteen days again three days later and four rows out of 179,314 come back different.

Two shapes, and they are not the same problem. Two closed dates were rewritten nineteen
months after the request closed, which is a correction to a historical fact. Two requests
moved out of In Progress and Assigned into Closed, which is a state change arriving very
late. The first is the publisher fixing something. The second is the workflow finishing.

This lives in the library rather than in the script that prints the report, because the
diff it produces is what the history table is judged against.

`diff_extracts` is deliberately not a checksum comparison. The manifest already holds a
sha256 per partition and it says a file changed. It cannot say which row or which column,
and the answer to what a warehouse should do about it depends entirely on that.
"""

import csv
import json


class ExtractDiff:
    """What moved between two extracts of one partition."""

    def __init__(self, partition, rows_before, rows_after, changed, added, removed):
        self.partition = partition
        self.rows_before = rows_before
        self.rows_after = rows_after
        # {key: {column: [before, after]}}
        self.changed = changed
        self.added = added
        self.removed = removed

    def changed_rows(self):
        return len(self.changed)

    def changed_cells(self):
        return sum(len(v) for v in self.changed.values())

    def columns_that_moved(self):
        out = {}
        for cells in self.changed.values():
            for column in cells:
                out[column] = out.get(column, 0) + 1
        return out

    def is_empty(self):
        return not (self.changed or self.added or self.removed)

    def as_dict(self):
        return {
            "partition": self.partition,
            "rows_before": self.rows_before,
            "rows_after": self.rows_after,
            "changed": self.changed,
            "added": sorted(self.added),
            "removed": sorted(self.removed),
        }


def read_keyed(path, key="unique_key"):
    with open(path, newline="") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames is None or key not in reader.fieldnames:
            raise ValueError("{} has no {} column".format(path, key))
        return dict((row[key], row) for row in reader)


def diff_extracts(partition, before_path, after_path, columns, key="unique_key"):
    """Compare two files of the same partition, cell by cell.

    Raises on an empty pair rather than reporting no change. Two empty files agree about
    nothing and calling that a clean comparison is the mistake this repo keeps finding.
    """
    before = read_keyed(before_path, key)
    after = read_keyed(after_path, key)

    if not before and not after:
        raise ValueError(
            "{}: both extracts are empty, that is not a clean comparison".format(
                partition)
        )

    changed = {}
    for k in sorted(set(before) & set(after)):
        moved = {}
        for column in columns:
            if before[k].get(column) != after[k].get(column):
                moved[column] = [before[k].get(column), after[k].get(column)]
        if moved:
            changed[k] = moved

    return ExtractDiff(
        partition,
        len(before),
        len(after),
        changed,
        sorted(set(after) - set(before)),
        sorted(set(before) - set(after)),
    )


def apply_diff(diff, before_path, out_path, columns, key="unique_key"):
    """Rebuild the later extract from the earlier one plus a recorded diff.

    The point of this is reproducibility. The live source moves, so nobody can fetch the
    second extract this repo measured, including a later run of it. The diff is small and
    it is committed, so the history table's behaviour can be reproduced exactly by anyone
    who has the first extract.

    It refuses a diff that does not describe the file in front of it, because replaying a
    diff against the wrong input produces a plausible file and a wrong answer.
    """
    before = read_keyed(before_path, key)
    if len(before) != diff.rows_before:
        raise ValueError(
            "{} holds {} rows, the diff was taken against {}".format(
                before_path, len(before), diff.rows_before)
        )

    for k, cells in diff.changed.items():
        if k not in before:
            raise ValueError("{} is not in {}".format(k, before_path))
        for column, (was, now) in cells.items():
            if before[k].get(column) != was:
                raise ValueError(
                    "{} {} is {!r}, the diff expected {!r}".format(
                        k, column, before[k].get(column), was)
                )
            before[k][column] = now

    for k in diff.removed:
        before.pop(k, None)

    with open(out_path, "w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns, lineterminator="\n")
        writer.writeheader()
        for k in sorted(before):
            writer.writerow(dict((c, before[k].get(c, "")) for c in columns))
    return out_path


def save(diffs, path):
    payload = {"partitions": [d.as_dict() for d in diffs]}
    with open(path, "w") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)
        fh.write("\n")
    return path


def load(path):
    with open(path) as fh:
        payload = json.load(fh)
    return [
        ExtractDiff(d["partition"], d["rows_before"], d["rows_after"],
                    d["changed"], d["added"], d["removed"])
        for d in payload["partitions"]
    ]
