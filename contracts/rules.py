"""Evaluate one contract rule against one column of values.

This lives in the library rather than in the script that prints the profile, because the
numbers it produces are what the design arguments in the README rest on. A figure that
comes out of a report script cannot be killed by a mutant, so nothing is really checking
it.

Everything here works on strings, because that is what a CSV hands you and because the
whole question a contract answers is whether the text coming off a source can be trusted
to mean what the column name says. Parsing is a rule, not a precondition.
"""

import datetime
import re

NULLISH = ("", "NA", "N/A", "null", "NULL", "None")


def is_null(value):
    return value is None or value.strip() in NULLISH


def as_number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def as_integer(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# Socrata writes 2025-01-01T00:51:02.000. Accept the plain second form too, since a
# different extract of the same dataset drops the milliseconds.
TIMESTAMP_FORMATS = ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S")


def as_timestamp(value):
    if value is None:
        return None
    text = value.strip()
    for fmt in TIMESTAMP_FORMATS:
        try:
            return datetime.datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def type_ok(type_, value):
    if type_ == "string":
        return True
    if type_ == "number":
        return as_number(value) is not None
    if type_ == "integer":
        return as_integer(value) is not None
    if type_ == "timestamp":
        return as_timestamp(value) is not None
    raise ValueError("unknown type {}".format(type_))


class Violation:
    def __init__(self, column, rule, count, examples):
        self.column = column
        self.rule = rule
        self.count = count
        self.examples = examples

    def __repr__(self):
        return "Violation({}, {}, {})".format(self.column, self.rule, self.count)


def _collect(values, predicate, limit=3):
    """Count values failing predicate, keeping a few to show a human."""
    count = 0
    examples = []
    for v in values:
        if not predicate(v):
            count += 1
            if len(examples) < limit:
                examples.append(v)
    return count, examples


def evaluate(column, values):
    """Return a Violation per broken rule. An empty list means the column is clean.

    Null handling is deliberate and easy to get wrong. `required` is the only rule that
    judges nulls. Every other rule skips them, because "latitude is between 40.4 and
    40.95 or absent" is what a nullable range means, and folding the two together reports
    one failure as two.
    """
    values = list(values)
    out = []

    present = [v for v in values if not is_null(v)]

    if column.rules.get("required"):
        count, examples = _collect(values, lambda v: not is_null(v))
        if count:
            out.append(Violation(column.name, "required", count, examples))

    count, examples = _collect(present, lambda v: type_ok(column.type, v))
    if count:
        out.append(Violation(column.name, "type:" + column.type, count, examples))

    if column.rules.get("unique"):
        seen = set()
        dupes = set()
        for v in present:
            if v in seen:
                dupes.add(v)
            seen.add(v)
        if dupes:
            out.append(
                Violation(column.name, "unique", len(dupes), sorted(dupes)[:3])
            )

    if "allowed" in column.rules:
        allowed = set(column.rules["allowed"])
        count, examples = _collect(present, lambda v: v.strip() in allowed)
        if count:
            out.append(Violation(column.name, "allowed", count, examples))

    if "max_length" in column.rules:
        limit = column.rules["max_length"]
        count, examples = _collect(present, lambda v: len(v.strip()) <= limit)
        if count:
            out.append(Violation(column.name, "max_length", count, examples))

    if "matches" in column.rules:
        pattern = re.compile(column.rules["matches"])
        count, examples = _collect(present, lambda v: pattern.match(v.strip()))
        if count:
            out.append(Violation(column.name, "matches", count, examples))

    if "min" in column.rules:
        floor = column.rules["min"]
        numeric = [v for v in present if as_number(v) is not None]
        count, examples = _collect(numeric, lambda v: as_number(v) >= floor)
        if count:
            out.append(Violation(column.name, "min", count, examples))

    if "max" in column.rules:
        ceiling = column.rules["max"]
        numeric = [v for v in present if as_number(v) is not None]
        count, examples = _collect(numeric, lambda v: as_number(v) <= ceiling)
        if count:
            out.append(Violation(column.name, "max", count, examples))

    return out


def _fires(check, row):
    """Does the when clause of a conditional check select this row."""
    value = (row.get(check.args["when_column"]) or "").strip()
    if check.args.get("when_equals") is not None:
        return value == check.args["when_equals"]
    return value in check.args["when_in"]


def evaluate_check(check, rows):
    """Cross column check over whole rows. Returns a Violation or None.

    Every kind here skips a row it cannot judge rather than counting it as a failure. An
    ordering check on a row with no closed date is not a violated ordering, it is an open
    request, and folding the two together turns the nullability rule into a second
    ordering failure.
    """
    bad = 0
    examples = []
    considered = 0

    for row in rows:
        if check.kind == "ordering":
            before = as_timestamp(row.get(check.args["before"]))
            after = as_timestamp(row.get(check.args["after"]))
            if before is None or after is None:
                continue
            considered += 1
            ok = after >= before
        elif check.kind == "requires_when":
            if not _fires(check, row):
                continue
            considered += 1
            ok = not is_null(row.get(check.args["then_required"]))
        elif check.kind == "forbids_when":
            if not _fires(check, row):
                continue
            considered += 1
            ok = is_null(row.get(check.args["then_absent"]))
        else:
            raise ValueError("unknown check kind {}".format(check.kind))

        if not ok:
            bad += 1
            if len(examples) < 3:
                examples.append(dict((c, row.get(c)) for c in check.columns()))

    if bad == 0:
        return None, considered
    return Violation(check.name, "check:" + check.kind, bad, examples), considered
