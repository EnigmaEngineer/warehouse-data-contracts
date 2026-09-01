"""Read the quarantine back and judge it again.

Held rows were written to disk and nothing had ever read them. A quarantine nobody empties
is a folder, and the README has said so for days without doing anything about it.

This does one of the three things a replay path could do. It re-judges. It does not retry
and it does not expire, and those two are deliberately absent rather than pending. Retrying
means writing rows into `accepted.csv` after the load has already read it, so a partition
would have two accepted files with different contents and no rule for which one the marts
were built from. Expiring means deleting data on a timer, which is the one operation in this
repo nobody could undo. Both need a policy argued somewhere other than inside the code that
would carry it out.

Re-judging is the part that has an answer today. When a contract changes, the question is
which of the rows already refused would now be allowed, and that is a function over data
this repo already has on disk.

Two things about the shape.

The held rows are judged inside the whole partition rather than on their own. Uniqueness is
a property of a set, so a row held for a duplicated key that is judged alone comes back
clean. Both copies are held today so the answer would come out right by luck, and it would
stop being right the first time a policy let one copy through.

And a re-judge against an unchanged contract has to recover nothing. That is the control.
Anything it recovers is not good news about the contract, it is a value that did not survive
the trip through the quarantine file.
"""

import csv
import os

# breaks knows how to build a contract with one rule taken out. That is the same operation
# whether you are testing a refusal or asking what a refusal is worth, so it lives in one
# place and this module reads it rather than growing a second copy.
from contracts import breaks, quarantine, validate
from warehouse import schema

ACCEPTED = "accepted.csv"
HELD = "quarantined.csv"


class NoQuarantine(ValueError):
    """The directory holds no split to re-judge."""


def _read(path):
    with open(path, newline="") as fh:
        reader = csv.DictReader(fh)
        return reader.fieldnames, list(reader)


def read_split(directory):
    """The held rows with the reason column removed, plus the accepted ones.

    Returns (held, original_reasons, accepted). The reasons come back separately because
    they are what the previous judgement said and comparing against them is the whole
    point.
    """
    held_path = os.path.join(directory, HELD)
    accepted_path = os.path.join(directory, ACCEPTED)
    for path in (held_path, accepted_path):
        if not os.path.exists(path):
            raise NoQuarantine("{} does not exist".format(path))

    fields, held_rows = _read(held_path)
    if fields is None or quarantine.REASON_COLUMN not in fields:
        raise NoQuarantine(
            "{} has no {} column, so nothing says why these rows were held".format(
                held_path, quarantine.REASON_COLUMN))

    reasons = []
    held = []
    for row in held_rows:
        row = dict(row)
        reasons.append(row.pop(quarantine.REASON_COLUMN))
        held.append(row)

    _, accepted = _read(accepted_path)
    return held, reasons, accepted


def rejudge(contract, directory):
    """Judge the held rows again against `contract`, inside their own partition.

    Returns a summary. `recovered` is the count that would now be accepted, and against an
    unchanged contract it has to be zero.
    """
    held, reasons, accepted = read_split(directory)
    header = schema.source_columns(contract)

    # Held first, so the indexes 0 to len(held) minus one are the rows being asked about.
    combined = held + accepted
    if not combined:
        raise NoQuarantine("{} holds no rows at all".format(directory))

    result = validate.validate(contract, combined, header=header)

    recovered = []
    still_held = []
    for index in range(len(held)):
        if index in result.failures:
            still_held.append(index)
        else:
            recovered.append(index)

    gone = {}
    for index in recovered:
        for reason in reasons[index].split(quarantine.REASON_SEPARATOR):
            gone[reason] = gone.get(reason, 0) + 1

    changed = []
    for index in still_held:
        was = set(reasons[index].split(quarantine.REASON_SEPARATOR))
        now = set(f.describe() for f in result.failures[index])
        if was != now:
            changed.append({"row": index, "was": sorted(was), "now": sorted(now)})

    return {
        "directory": directory,
        "held": len(held),
        "accepted": len(accepted),
        "recovered": len(recovered),
        "still_held": len(still_held),
        "reasons_that_stopped_firing": dict(sorted(gone.items())),
        "rows_held_for_a_different_reason": changed,
    }


def constraints(contract):
    """Every constraint in the contract, with a way to take it out or a reason it cannot.

    Returns [(label, relax, reason)]. `relax` is None when the constraint cannot be removed
    on its own, and `reason` says why. Three things end up in that state and they are not
    the same kind of problem.

    A column carrying one rule cannot lose it, because the loader refuses a column with no
    constraints. A `required: false` entry is a permission rather than a refusal, so
    removing it changes nothing and reporting zero beside a real zero would be misleading.
    And `type` has no key in the YAML at all.

    Driven off the contract rather than off a list beside it, so a rule added to the format
    appears here without anybody remembering to.
    """
    out = []
    for column in contract.columns:
        for rule in sorted(column.rules):
            label = "{}:{}".format(column.name, rule)
            value = column.rules[rule]
            if value is False:
                out.append((label, None, "permits rather than refuses"))
                continue
            if len(column.rules) == 1:
                out.append((label, None,
                            "the only rule on this column, and a column with none is a "
                            "contract the loader refuses"))
                continue
            out.append((label, _column_relaxer(column.name, rule), None))
    for check in contract.checks:
        out.append(("{}:check:{}".format(check.name, check.kind),
                    _check_relaxer(check.name), None))
    return out


def _column_relaxer(name, rule):
    return lambda c: breaks.relax_column(c, name, rule)


def _check_relaxer(name):
    return lambda c: breaks.relax_check(c, name)


def marginal(contract, directories):
    """How many held rows each constraint is the sole reason for.

    Take one constraint out, re-judge, and count what comes back. A constraint that
    recovers nothing is either never broken or never the only thing broken, and in both
    cases removing it would not free a single row. That is a different question from how
    often it fires, and it is the one that says whether a rule is carrying anything.

    Returns [(label, recovered, reason)] in contract order. `recovered` is None for a
    constraint that cannot be removed on its own.
    """
    if not directories:
        raise ValueError(
            "no partitions to re-judge, and a marginal count over nothing is not one")
    out = []
    for label, relax, reason in constraints(contract):
        if relax is None:
            out.append((label, None, reason))
            continue
        relaxed = relax(contract)
        recovered = sum(rejudge(relaxed, d)["recovered"] for d in directories)
        out.append((label, recovered, None))
    return out
