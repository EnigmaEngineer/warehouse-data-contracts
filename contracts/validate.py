"""Judge a contract row by row, so a bad batch can be split instead of described.

The profile answers how much of a feed the contract refuses. This answers which rows, and
those are different questions with different arithmetic.

Adding up the per rule counts overstates the answer, because a row breaking two rules is
counted twice. Taking the largest single count understates it, because every row that
broke some other rule is invisible. The profile's `worst` is that second number and it is
the honest cheap answer to a question the quarantine cannot use. A quarantine has to move
exactly the bad rows and no others, so it needs the count that carries row identity.

Every predicate here comes from `rules.value_rules` and every cross column judgement from
`rules.judge_check`. Two implementations of one rule would give two answers and nothing
would be comparing them.
"""

from contracts import rules
from contracts.profile import NothingChecked, columns_in, read_partition


class RowFailure:
    """One rule, broken by one row.

    `subject` is a column name for a column rule and a check name for a cross column one.
    They share a field because a rejection report is read by someone asking what is wrong
    with this row, not by someone asking which half of the contract caught it.
    """

    def __init__(self, subject, rule, value):
        self.subject = subject
        self.rule = rule
        self.value = value

    def __repr__(self):
        return "RowFailure({}, {})".format(self.subject, self.rule)

    def describe(self):
        return "{}:{}".format(self.subject, self.rule)


class Validation:
    def __init__(self, rows, failures, considered, evaluations, missing, uncontracted,
                 collisions=None):
        self.rows = rows
        # Sparse on purpose. A clean partition of 179,314 rows should not build 179,314
        # empty lists to say so.
        self.failures = failures
        self.considered = considered
        self.evaluations = evaluations
        self.missing = missing
        self.uncontracted = uncontracted
        # {column: {value: count}} for every value that repeated. Empty on a clean
        # partition and the whole file on a replayed one.
        self.collisions = collisions or {}

    @property
    def bad_rows(self):
        return len(self.failures)

    @property
    def clean_rows(self):
        return self.rows - self.bad_rows

    def rule_counts(self):
        """Rows broken by each rule, keyed by (subject, rule).

        This is the same shape the profile reports, which is what makes the two gradeable
        against each other.
        """
        counts = {}
        for failures in self.failures.values():
            for f in failures:
                key = (f.subject, f.rule)
                counts[key] = counts.get(key, 0) + 1
        return counts

    def sum_of_rule_counts(self):
        return sum(self.rule_counts().values())

    def largest_rule_count(self):
        return max(self.rule_counts().values(), default=0)

    def rows_breaking_more_than_one_rule(self):
        return sum(1 for f in self.failures.values() if len(f) > 1)

    def held_only_by(self, rule):
        """Rows whose every failure is this rule, as a set of indexes.

        Written for `unique`, and the reason is that a uniqueness failure is not a
        statement about the row. Two rows share a value, the contract says the value
        identifies one request, and nothing in it says which copy is real. Both are held
        and at least one of them was fine. Folded into a single held count that is
        invisible, so one replayed upstream job reads as a partition full of bad data.
        """
        return set(i for i, fs in self.failures.items()
                   if fs and all(f.rule == rule for f in fs))

    def largest_collision(self):
        """The size of the biggest group sharing one value on a unique column.

        One is the answer when nothing repeated. This is the blast radius of a single
        collision and it has no ceiling. A whole partition replayed puts every row in a
        group of two.
        """
        largest = 1
        for counts in self.collisions.values():
            for n in counts.values():
                largest = max(largest, n)
        return largest


def _unique_columns(contract, present):
    return [c for c in contract.columns
            if c.rules.get("unique") and c.name in present]


def validate(contract, rows, header=None):
    """Judge every row and return a Validation.

    `rows` is a list of dicts as `csv.DictReader` gives them. `header` matters when the
    list is empty, because an empty partition still has columns and the alternative is
    concluding that a contract covers nothing.
    """
    rows = list(rows)
    present = set(header if header is not None else columns_in(rows))

    contracted = {c.name for c in contract.columns}
    missing = sorted(contracted - present)
    uncontracted = sorted(present - contracted)

    if rows and len(missing) == len(contract.columns):
        raise NothingChecked(
            "no contracted column is present, headers are {}".format(sorted(present))
        )

    judged = [c for c in contract.columns if c.name in present]
    checks = [k for k in contract.checks
              if all(c in present for c in k.columns())]

    # Uniqueness is the one rule no single row can answer, so it needs a pass over the
    # column before any row is judged. Both copies of a duplicated key are held. The
    # contract says the value identifies one request and says nothing about which copy is
    # the real one, so letting one through would be picking by file order.
    collisions = {}
    dupes = {}
    for column in _unique_columns(contract, present):
        counts = rules.duplicate_counts(r.get(column.name) for r in rows)
        if counts:
            collisions[column.name] = counts
        dupes[column.name] = set(counts)

    predicates = [(c, rules.value_rules(c)) for c in judged]

    failures = {}
    considered = {}
    evaluations = 0

    for index, row in enumerate(rows):
        broken = []

        for column, value_rules in predicates:
            value = row.get(column.name)
            absent = rules.is_null(value)

            if column.rules.get("required"):
                evaluations += 1
                if absent:
                    broken.append(RowFailure(column.name, "required", value))

            if absent:
                continue

            for name, predicate in value_rules:
                evaluations += 1
                if not predicate(value):
                    broken.append(RowFailure(column.name, name, value))

            if column.name in dupes:
                evaluations += 1
                if value in dupes[column.name]:
                    broken.append(RowFailure(column.name, "unique", value))

        for check in checks:
            verdict = rules.judge_check(check, row)
            if verdict is None:
                continue
            evaluations += 1
            considered[check.name] = considered.get(check.name, 0) + 1
            if not verdict:
                broken.append(
                    RowFailure(check.name, "check:" + check.kind,
                               dict((c, row.get(c)) for c in check.columns()))
                )

        if broken:
            failures[index] = broken

    # A pass that evaluated nothing is not a clean partition, and the only tell on day one
    # was that the answer looked too good. Rows with no contracted column present get
    # here rather than through the missing column branch above.
    if rows and evaluations == 0:
        raise NothingChecked(
            "{} rows judged and no rule was evaluated".format(len(rows))
        )

    return Validation(len(rows), failures, considered, evaluations,
                      missing, uncontracted, collisions)


def validate_file(contract, path):
    rows = read_partition(path)
    return validate(contract, rows)
