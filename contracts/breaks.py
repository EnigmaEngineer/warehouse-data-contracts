"""Break the contract on purpose, and prove the refusal came from the contract.

A rejection report is easy to believe and hard to check. Every rule here can be shown
refusing something, and a demonstration that stops there is worth very little. A rule can
look like it fired for two other reasons. The row it caught might already have been bad
before anything was injected. Or the row might be refused by a different rule that catches
the same damage, in which case deleting the rule under test would change nothing.

So every break carries two things a demonstration usually skips.

The first is that targets are chosen from the rows the contract currently accepts. Watching
a random row get held proves nothing when part of the file is held already. The clean set is
computed first and the injection lands inside it.

The second is a control. Each break names the one rule it aims at, and the probe judges the
same damaged rows again against a contract with exactly that rule removed. If the damaged
row is still refused, the refusal came from somewhere else and the break is not testing what
its name says.

One break in the catalogue is here to get through on purpose. `wrong_agency` writes a real
agency acronym into the wrong row. Every rule on that column passes, because the contract
constrains the shape of the value and has nothing to say about whether it is true. It is
accepted, it reaches the marts, and it moves them. That is the boundary of the claim that
the marts are protected.

Nothing here runs in the pipeline.
"""

import copy

from contracts import spec, validate


class CannotRelax(ValueError):
    """Removing this rule would leave a column carrying nothing.

    The loader refuses such a contract, so a relaxed one built in memory would be a shape
    `spec.parse` never produces and the control would be testing a fiction.
    """


def _rebuild(contract, columns=None, checks=None):
    return spec.Contract(
        contract.dataset, contract.source, contract.freshness, contract.volume,
        contract.columns if columns is None else columns,
        contract.checks if checks is None else checks,
    )


def relax_column(contract, column_name, rule):
    """A copy of the contract with one rule removed from one column."""
    columns = []
    found = False
    for column in contract.columns:
        if column.name != column_name:
            columns.append(column)
            continue
        if rule not in column.rules:
            raise CannotRelax(
                "column {} carries no rule {}".format(column_name, rule))
        rules = dict(column.rules)
        del rules[rule]
        if not rules:
            raise CannotRelax(
                "removing {} leaves column {} with no constraints, and the loader "
                "refuses that contract".format(rule, column_name))
        found = True
        columns.append(spec.Column(column.name, column.type, rules,
                                   column.provenance, column.note))
    if not found:
        raise CannotRelax("contract has no column {}".format(column_name))
    return _rebuild(contract, columns=columns)


def drop_column(contract, column_name):
    """A copy of the contract with a whole column removed.

    This is the control for the type rule and only for that. Every other rule has a key in
    the YAML that can be deleted. `type` does not. It is implied by the column existing, so
    the only way to switch it off is to stop describing the column at all.

    Refused for a column any check reads, since removing it would silently disable that
    check too and the control would then be relaxing two rules.
    """
    if column_name == contract.source["partition_column"]:
        raise CannotRelax("the partition column cannot be dropped")
    for check in contract.checks:
        if column_name in check.columns():
            raise CannotRelax(
                "check {} reads {}, so dropping it would relax two rules".format(
                    check.name, column_name))
    columns = [c for c in contract.columns if c.name != column_name]
    if len(columns) == len(contract.columns):
        raise CannotRelax("contract has no column {}".format(column_name))
    return _rebuild(contract, columns=columns)


def relax_check(contract, check_name):
    """A copy of the contract with one cross column check removed."""
    kept = [c for c in contract.checks if c.name != check_name]
    if len(kept) == len(contract.checks):
        raise CannotRelax("contract has no check {}".format(check_name))
    return _rebuild(contract, checks=kept)


class Break:
    """One named way to damage a row, and the one rule that should catch it.

    `damage` takes the working row list, the index to damage and a clean donor index it may
    use. It returns the indexes that should end up held. Almost every break returns the one
    it damaged. The duplicate key break returns two, and that is the point of it.

    `expects_hold` is False for the break that nothing catches.
    """

    def __init__(self, name, subject, rule, relax, damage, note,
                 expects_hold=True):
        self.name = name
        self.subject = subject
        self.rule = rule
        self.relax = relax
        self.damage = damage
        self.note = note
        self.expects_hold = expects_hold

    @property
    def expect(self):
        return "{}:{}".format(self.subject, self.rule)

    def __repr__(self):
        return "Break({!r}, {})".format(self.name, self.expect)


def _set(rows, index, values):
    row = dict(rows[index])
    row.update(values)
    rows[index] = row
    return {index}


def _copy_key(rows, index, donor, column):
    """Give one row another row's key. Both copies are then held and that is the finding."""
    _set(rows, index, {column: rows[donor][column]})
    return {index, donor}


CATALOGUE = [
    Break(
        "required_null", "agency", "required",
        lambda c: relax_column(c, "agency", "required"),
        lambda rows, i, d: _set(rows, i, {"agency": ""}),
        "a required column arrives empty",
    ),
    Break(
        "not_allowed", "borough", "allowed",
        lambda c: relax_column(c, "borough", "allowed"),
        lambda rows, i, d: _set(rows, i, {"borough": "BROOKLYNN"}),
        "a value outside the vocabulary, one letter off a real one",
    ),
    Break(
        "too_long", "agency", "max_length",
        lambda c: relax_column(c, "agency", "max_length"),
        lambda rows, i, d: _set(rows, i, {"agency": "DEPARTMENTOFEVERYTHING"}),
        "an acronym column carrying a full department name",
    ),
    Break(
        "bad_pattern", "incident_zip", "matches",
        lambda c: relax_column(c, "incident_zip", "matches"),
        lambda rows, i, d: _set(rows, i, {"incident_zip": "1121-4"}),
        "a zip with the shape of a postcode from somewhere else",
    ),
    Break(
        "out_of_range", "latitude", "max",
        lambda c: relax_column(c, "latitude", "max"),
        lambda rows, i, d: _set(rows, i, {"latitude": "51.5072"}),
        "a coordinate outside the city, in this case London",
    ),
    Break(
        "not_a_number", "longitude", "type:number",
        lambda c: drop_column(c, "longitude"),
        lambda rows, i, d: _set(rows, i, {"longitude": "west a bit"}),
        "text where a number belongs. the only rule with no key to delete",
    ),
    Break(
        "duplicate_key", "unique_key", "unique",
        lambda c: relax_column(c, "unique_key", "unique"),
        lambda rows, i, d: _copy_key(rows, i, d, "unique_key"),
        "a replayed upstream job, in miniature",
    ),
    Break(
        "closed_before_created", "closed_after_created", "check:ordering",
        lambda c: relax_check(c, "closed_after_created"),
        lambda rows, i, d: _set(rows, i, {
            "status": "Closed", "closed_date": "2024-06-01T09:00:00.000"}),
        "a request closed before it was raised",
    ),
    Break(
        "closed_with_no_date", "closed_request_has_a_closed_date",
        "check:requires_when",
        lambda c: relax_check(c, "closed_request_has_a_closed_date"),
        lambda rows, i, d: _set(rows, i, {"status": "Closed", "closed_date": ""}),
        "status says closed and the date that would prove it is absent",
    ),
    Break(
        "open_with_a_date", "open_request_has_no_closed_date",
        "check:forbids_when",
        lambda c: relax_check(c, "open_request_has_no_closed_date"),
        lambda rows, i, d: _set(rows, i, {
            "status": "Open", "closed_date": "2025-02-20T10:00:00.000"}),
        "still open and already closed",
    ),
    Break(
        "wrong_agency", "agency", "nothing",
        lambda c: c,
        lambda rows, i, d: _set(rows, i, {
            "agency": "DSNY" if rows[i]["agency"] == "NYPD" else "NYPD"}),
        "a real acronym in the wrong row. required and max_length both pass",
        expects_hold=False,
    ),
]


def by_name(name):
    for b in CATALOGUE:
        if b.name == name:
            return b
    raise KeyError(name)


def clean_indexes(contract, rows, header=None):
    """The rows this contract accepts, as a list of indexes, plus the validation."""
    result = validate.validate(contract, rows, header=header)
    return [i for i in range(len(rows)) if i not in result.failures], result


def targets(clean, count):
    """`count` indexes spread across the clean rows rather than taken off the front.

    Spread rather than random because a seed is one more thing that has to be recorded for
    a number to be reproducible. The first n would share a fetch order and possibly an
    upstream batch.
    """
    if count < 1:
        raise ValueError("a break has to damage at least one row")
    if count * 2 > len(clean):
        raise ValueError(
            "asked to damage {} of {} clean rows, which is enough of the partition "
            "to change what the answer means".format(count, len(clean)))
    step = len(clean) // (count + 1)
    return [clean[step * (n + 1)] for n in range(count)]


class Outcome:
    """What one break did. Every field is a count or a set of row indexes."""

    def __init__(self, break_, mutated, damaged, new_holds, reasons, control_holds,
                 baseline_held):
        self.break_ = break_
        # Rows the mutation actually wrote to. The duplicate key break writes to one row
        # and expects two to be held, and keeping these apart is the only way the
        # amplification figure means anything.
        self.mutated = mutated
        self.damaged = damaged
        self.new_holds = new_holds
        self.reasons = reasons
        self.control_holds = control_holds
        self.baseline_held = baseline_held

    @property
    def injected(self):
        return len(self.mutated)

    @property
    def caught(self):
        if not self.break_.expects_hold:
            return not self.new_holds
        return self.damaged <= self.new_holds

    @property
    def collateral(self):
        """Rows held that were clean before and that the break did not aim at."""
        return self.new_holds - self.damaged

    @property
    def by_the_named_rule(self):
        """Did every row this break expected to lose cite the rule it aims at."""
        if not self.break_.expects_hold:
            return not self.new_holds
        if not self.caught:
            return False
        return all(self.break_.expect in self.reasons.get(i, ())
                   for i in self.damaged)

    @property
    def control_clean(self):
        """With the rule removed, are the damaged rows accepted again.

        False means the refusal did not come from the rule under test, so the break is
        proving something other than what it claims.
        """
        if not self.break_.expects_hold:
            return True
        return not (self.damaged & self.control_holds)

    @property
    def amplification(self):
        """Rows lost for every row written to. One for most breaks, two for a duplicate."""
        return len(self.new_holds) / self.injected if self.injected else 0.0


def run_break(contract, rows, break_, count=1, header=None):
    """Damage `count` clean rows, judge, then judge again with the rule removed."""
    clean, baseline = clean_indexes(contract, rows, header=header)
    chosen = targets(clean, count)
    # The donor for the duplicate key break, ignored by every other one. It has to be clean
    # too, or the second held row would have been held anyway and the amplification figure
    # would be counting a row the injection did not cost.
    donor = clean[-1]
    if donor in chosen:
        raise ValueError("the donor row is also a target, pick a smaller count")

    working = list(rows)
    damaged = set()
    for index in chosen:
        damaged |= break_.damage(working, index, donor)

    after = validate.validate(contract, working, header=header)
    new_holds = set(after.failures) - set(baseline.failures)
    reasons = dict((i, tuple(f.describe() for f in fs))
                   for i, fs in after.failures.items())

    relaxed = break_.relax(contract)
    control_before = validate.validate(relaxed, rows, header=header)
    control_after = validate.validate(relaxed, working, header=header)
    control_holds = set(control_after.failures) - set(control_before.failures)

    return Outcome(break_, set(chosen), damaged, new_holds, reasons, control_holds,
                   set(baseline.failures))


def replay_partition(rows, times=2):
    """The whole partition concatenated with itself. The unbounded duplicate.

    A deep copy rather than a second reference to the same dicts, so a caller mutating one
    row cannot change both copies and turn the measurement into one about aliasing.
    """
    if times < 2:
        raise ValueError("a replay of one is the original")
    out = []
    for _ in range(times):
        out.extend(copy.deepcopy(rows))
    return out
