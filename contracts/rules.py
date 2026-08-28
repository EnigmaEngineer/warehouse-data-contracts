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


def value_rules(column):
    """Every rule on this column that can be answered from one value, as (name, predicate).

    Two things read this list. The profile aggregates it down a column and the validator
    aggregates it across a row, so a column view and a row view of the same contract are
    two summaries of one implementation rather than two implementations. Writing the range
    check twice is how you end up with two numbers and nothing comparing them.

    `required` is not here because it is the only rule that judges a null, and `unique` is
    not here because no single value answers it. Both are handled by the caller.

    Every predicate assumes a value that is present. A value that is absent is the
    nullability rule's business.
    """
    out = [("type:" + column.type, lambda v: type_ok(column.type, v))]

    if "allowed" in column.rules:
        allowed = set(column.rules["allowed"])
        out.append(("allowed", lambda v: v.strip() in allowed))

    if "max_length" in column.rules:
        limit = column.rules["max_length"]
        out.append(("max_length", lambda v: len(v.strip()) <= limit))

    if "matches" in column.rules:
        pattern = re.compile(column.rules["matches"])
        out.append(("matches", lambda v: pattern.match(v.strip()) is not None))

    if "min" in column.rules:
        floor = column.rules["min"]
        # A value that is not a number has already failed the type rule. Failing it here
        # too reports one bad value as two, which is the same mistake as folding
        # nullability into a range.
        out.append(("min", lambda v: as_number(v) is None or as_number(v) >= floor))

    if "max" in column.rules:
        ceiling = column.rules["max"]
        out.append(("max", lambda v: as_number(v) is None or as_number(v) <= ceiling))

    return out


def duplicated(values):
    """The set of values that appear more than once among the ones present."""
    seen = set()
    dupes = set()
    for v in values:
        if is_null(v):
            continue
        if v in seen:
            dupes.add(v)
        seen.add(v)
    return dupes


def evaluate(column, values):
    """Return a Violation per broken rule. An empty list means the column is clean.

    Null handling is deliberate and easy to get wrong. `required` is the only rule that
    judges nulls. Every other rule skips them, because "latitude is between 40.4 and
    40.95 or absent" is what a nullable range means, and folding the two together reports
    one failure as two.

    Every count here is a count of values, including the one for `unique`. It used to be
    the number of distinct keys that collided, which is a different quantity wearing the
    same field name, and the profile then took a max across the two.
    """
    values = list(values)
    out = []

    present = [v for v in values if not is_null(v)]

    if column.rules.get("required"):
        count, examples = _collect(values, lambda v: not is_null(v))
        if count:
            out.append(Violation(column.name, "required", count, examples))

    for name, predicate in value_rules(column):
        count, examples = _collect(present, predicate)
        if count:
            out.append(Violation(column.name, name, count, examples))

    if column.rules.get("unique"):
        dupes = duplicated(present)
        count, examples = _collect(present, lambda v: v not in dupes)
        if count:
            out.append(Violation(column.name, "unique", count, examples))

    return out


def _fires(check, row):
    """Does the when clause of a conditional check select this row."""
    value = (row.get(check.args["when_column"]) or "").strip()
    if check.args.get("when_equals") is not None:
        return value == check.args["when_equals"]
    return value in check.args["when_in"]


def judge_check(check, row):
    """Judge one row against one cross column check. True, False, or None for silent.

    None is the important one. Every kind skips a row it cannot judge rather than counting
    it as a failure. An ordering check on a row with no closed date is not a violated
    ordering, it is an open request, and folding the two together turns the nullability
    rule into a second ordering failure.

    The column view and the row view both call this, for the reason in `value_rules`.
    """
    if check.kind == "ordering":
        before = as_timestamp(row.get(check.args["before"]))
        after = as_timestamp(row.get(check.args["after"]))
        if before is None or after is None:
            return None
        return after >= before

    if check.kind == "requires_when":
        if not _fires(check, row):
            return None
        return not is_null(row.get(check.args["then_required"]))

    if check.kind == "forbids_when":
        if not _fires(check, row):
            return None
        return is_null(row.get(check.args["then_absent"]))

    raise ValueError("unknown check kind {}".format(check.kind))


def evaluate_check(check, rows):
    """Cross column check over whole rows. Returns a Violation or None, plus a count of
    the rows the check was able to say anything about."""
    bad = 0
    examples = []
    considered = 0

    for row in rows:
        ok = judge_check(check, row)
        if ok is None:
            continue
        considered += 1
        if not ok:
            bad += 1
            if len(examples) < 3:
                examples.append(dict((c, row.get(c)) for c in check.columns()))

    if bad == 0:
        return None, considered
    return Violation(check.name, "check:" + check.kind, bad, examples), considered
