"""Measure a contract against real data, column by column.

This is not the validator. It does not decide whether to publish anything and it does not
quarantine a row. It answers one question: how much of the real feed does this contract
refuse, and which half of the contract is doing the refusing.

The split by provenance is the whole reason it exists. If the documented rules pass and
the asserted ones fail, the contract is telling you about your own assumptions rather than
about the data, and that is worth knowing on the day the contract is written rather than
on the day a DAG starts quarantining half a partition.
"""

import csv


class NothingChecked(RuntimeError):
    """The profile ran and evaluated no rule. That is a failure, not a pass."""


# Top level contract clauses that something in this repo actually evaluates. The list is
# here rather than in a comment because a clause sitting in a YAML file with no evaluator
# behind it reads to anyone opening the contract like a rule that is being enforced. It is
# decoration, and the only honest way to carry one is to say out loud that nothing reads
# it.
EVALUATED_CLAUSES = {"columns", "checks", "freshness", "volume"}
ALL_CLAUSES = {"columns", "checks", "freshness", "volume"}


def unevaluated_clauses(contract):
    out = []
    for name in sorted(ALL_CLAUSES - EVALUATED_CLAUSES):
        if getattr(contract, name, None):
            out.append(name)
    return out


def read_partition(path):
    with open(path, newline="") as fh:
        return list(csv.DictReader(fh))


def columns_in(rows):
    return list(rows[0].keys()) if rows else []


class ColumnResult:
    """What one column did against its rules. Counts per rule, never a row count.

    There used to be a `worst` property here returning the largest single rule count and
    calling it a lower bound on affected rows. `contracts/validate.py` now answers that
    question exactly, so the approximation had no caller and went.
    """

    def __init__(self, column, rows, violations):
        self.column = column
        self.rows = rows
        self.violations = violations


class CheckResult:
    def __init__(self, check, considered, violation):
        self.check = check
        self.considered = considered
        self.violation = violation

    @property
    def count(self):
        return self.violation.count if self.violation else 0


class Profile:
    def __init__(self, contract, path, rows, results, checks, uncontracted, missing):
        self.contract = contract
        self.path = path
        self.rows = rows
        self.results = results
        self.checks = checks
        self.uncontracted = uncontracted
        self.missing = missing

    def violated(self):
        return [r for r in self.results if r.violations]


def profile(contract, path, rules):
    rows = read_partition(path)
    present = columns_in(rows)

    contracted = {c.name for c in contract.columns}
    uncontracted = [c for c in present if c not in contracted]
    missing = [c.name for c in contract.columns if c.name not in present]

    # Evaluating nothing is not a clean partition. The first version of this module
    # compared Column objects against header strings. Every column read as absent and
    # every rule was skipped, so the profile printed zero violations over 179,314 rows.
    # Nothing in the output said it had checked nothing.
    if rows and len(missing) == len(contract.columns):
        raise NothingChecked(
            "no contracted column is present in {}, headers are {}".format(path, present)
        )

    results = []
    for column in contract.columns:
        if column.name in missing:
            continue
        values = [r.get(column.name) for r in rows]
        results.append(ColumnResult(column, len(rows), rules.evaluate(column, values)))

    checks = []
    for check in contract.checks:
        if any(c in missing for c in check.columns()):
            continue
        violation, considered = rules.evaluate_check(check, rows)
        checks.append(CheckResult(check, considered, violation))

    return Profile(contract, path, len(rows), results, checks, uncontracted, missing)
