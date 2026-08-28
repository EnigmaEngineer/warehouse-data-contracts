"""Split a judged partition into what publishes and what gets held back, with a reason
attached to every held row.

A rejection report that gives a count is a report nobody can act on. The question an
analyst asks is which rows, and the only useful answer is a file they can open. So the
held rows keep their original columns and gain one more naming every rule they broke.

What this does not do is decide the fate of the partition. There is no threshold here that
fails a whole batch once too much of it is bad, because any number I picked today would be
picked by looking at the corpus it is about to judge. Splitting the rows is a fact. What
share of a partition is too much is a policy and it needs to be argued somewhere other
than inside the thing measuring it.
"""

import csv
import json
import os

REASON_COLUMN = "_contract_failures"
REASON_SEPARATOR = " and "


class ReasonColumnClash(ValueError):
    """The source already has a column named like the one the report adds."""


def reasons(failures):
    """One string naming every rule a row broke, in the order they were judged."""
    return REASON_SEPARATOR.join(f.describe() for f in failures)


def split(rows, validation):
    """Return (accepted, held). Held rows carry the reason column, accepted ones do not.

    Accepted rows are returned unchanged rather than copied with a blank reason column,
    because a published table should not carry a column about the thing that judged it.
    """
    accepted = []
    held = []
    for index, row in enumerate(rows):
        failures = validation.failures.get(index)
        if failures is None:
            accepted.append(row)
        else:
            marked = dict(row)
            marked[REASON_COLUMN] = reasons(failures)
            held.append(marked)
    return accepted, held


def report(validation, partition, path=None):
    """The numbers a person needs to decide what to do, plus the arithmetic behind them.

    All three row counts are here on purpose. The exact figure is the only one a
    quarantine can act on, and publishing it beside the two approximations is what stops
    someone reading a per rule table and adding it up.
    """
    counts = validation.rule_counts()
    return {
        "partition": partition,
        "path": path,
        "rows": validation.rows,
        "accepted": validation.clean_rows,
        "held": validation.bad_rows,
        "held_share": round(validation.bad_rows / validation.rows, 6)
        if validation.rows else 0.0,
        "rows_breaking_more_than_one_rule": validation.rows_breaking_more_than_one_rule(),
        "sum_of_rule_counts": validation.sum_of_rule_counts(),
        "largest_rule_count": validation.largest_rule_count(),
        "rule_evaluations": validation.evaluations,
        "by_rule": sorted(
            ({"subject": s, "rule": r, "rows": n} for (s, r), n in counts.items()),
            key=lambda d: (-d["rows"], d["subject"], d["rule"]),
        ),
        "rows_each_check_could_judge": dict(sorted(validation.considered.items())),
        "contracted_columns_absent": validation.missing,
        "columns_with_no_contract": validation.uncontracted,
    }


def _write_csv(path, fieldnames, rows):
    with open(path, "w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def write(outdir, fieldnames, rows, validation, partition, path=None):
    """Write accepted.csv, quarantined.csv and report.json under outdir. Returns the report.

    The source header is passed in rather than read off the first row, so a partition where
    every row is held still writes an accepted file with the right columns instead of an
    empty one downstream cannot read.
    """
    if REASON_COLUMN in fieldnames:
        raise ReasonColumnClash(
            "source already has a column named {}, the report would overwrite it".format(
                REASON_COLUMN
            )
        )

    accepted, held = split(rows, validation)
    os.makedirs(outdir, exist_ok=True)

    _write_csv(os.path.join(outdir, "accepted.csv"), list(fieldnames), accepted)
    _write_csv(os.path.join(outdir, "quarantined.csv"),
               list(fieldnames) + [REASON_COLUMN], held)

    summary = report(validation, partition, path)
    with open(os.path.join(outdir, "report.json"), "w") as fh:
        json.dump(summary, fh, indent=2, sort_keys=True)
        fh.write("\n")
    return summary
