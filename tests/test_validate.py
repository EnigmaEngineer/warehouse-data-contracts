"""Row level validation.

Fixtures here are lopsided on purpose. A fixture split evenly between good and bad rows
survives an inverted rule, because the failure count does not move when the predicate
flips. Ask of every fixture below what the count would be if the rule ran backwards.

The other rule this file follows is that a fixture exercising one row per case cannot test
anything about choosing between rows. The uniqueness fixtures carry a real collision.
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from contracts import profile as profile_mod
from contracts import quarantine, rules, spec, validate

FIXTURE = os.path.join(HERE, "fixtures", "sample_partition.csv")
SHIPPED = os.path.join(ROOT, "contracts", "nyc311.yml")

BASE = """
dataset: t
source:
  kind: socrata
  domain: d
  resource: r
  partition_column: k
  partition_grain: day
freshness:
  max_lag_hours: 1
  reference: extract
  applies_to: tail
  provenance: asserted
volume:
  min_rows_per_partition: 1
  provenance: asserted
columns:
  - name: k
    type: string
    provenance: documented
    required: true
    unique: true
  - name: n
    type: integer
    provenance: asserted
    min: 0
    max: 10
  - name: state
    type: string
    provenance: asserted
    allowed: [open, shut]
{checks}
"""

CHECKS = """
checks:
  - name: shut_has_a_number
    kind: requires_when
    when_column: state
    when_equals: shut
    then_required: n
    provenance: asserted
"""


def contract(with_checks=False):
    return spec.parse(BASE.format(checks=CHECKS if with_checks else ""))


def rows(*items):
    return [dict(zip(("k", "n", "state"), i)) for i in items]


def check_a_clean_partition_holds_nothing_and_still_says_how_much_it_looked_at():
    v = validate.validate(contract(), rows(
        ("a", "1", "open"), ("b", "2", "open"), ("c", "3", "shut"),
        ("d", "4", "open"), ("e", "5", "open"),
    ))
    assert v.bad_rows == 0, v.failures
    assert v.clean_rows == 5
    # k carries three rules and so does n. state carries two. Eight per row.
    assert v.evaluations == 5 * 8, v.evaluations


def check_one_bad_row_among_many_good_ones_is_found():
    # Lopsided. Inverting the range rule would fail four rows here, not one.
    v = validate.validate(contract(), rows(
        ("a", "1", "open"), ("b", "2", "open"), ("c", "99", "open"),
        ("d", "4", "open"), ("e", "5", "open"),
    ))
    assert v.bad_rows == 1, v.failures
    assert [f.describe() for f in v.failures[2]] == ["n:max"]


def check_a_row_breaking_two_rules_is_one_row_and_two_rule_counts():
    # The whole reason this module exists. Four clean rows and one row that is bad twice.
    v = validate.validate(contract(), rows(
        ("a", "1", "open"), ("b", "2", "open"), ("c", "99", "sideways"),
        ("d", "4", "open"), ("e", "5", "open"),
    ))
    assert v.bad_rows == 1, v.failures
    assert v.sum_of_rule_counts() == 2, v.rule_counts()
    assert v.largest_rule_count() == 1
    assert v.rows_breaking_more_than_one_rule() == 1


def check_both_copies_of_a_duplicated_key_are_held():
    # The collision is the point. With one row per key the rule cannot be exercised at
    # all, and a validator that held neither copy would look identical here.
    v = validate.validate(contract(), rows(
        ("a", "1", "open"), ("dup", "2", "open"), ("dup", "3", "open"),
        ("d", "4", "open"), ("e", "5", "open"),
    ))
    assert v.bad_rows == 2, v.failures
    assert sorted(v.failures) == [1, 2]
    assert v.rule_counts() == {("k", "unique"): 2}, v.rule_counts()


def check_a_missing_key_is_a_required_failure_and_not_a_uniqueness_one():
    # Two rows with an absent key would collide on the empty string under a naive
    # implementation and report as duplicates on top of being absent.
    v = validate.validate(contract(), rows(
        ("a", "1", "open"), ("", "2", "open"), ("", "3", "open"),
        ("d", "4", "open"), ("e", "5", "open"),
    ))
    assert v.rule_counts() == {("k", "required"): 2}, v.rule_counts()


def check_an_absent_optional_value_is_not_a_range_failure():
    v = validate.validate(contract(), rows(
        ("a", "", "open"), ("b", "2", "open"), ("c", "3", "open"),
        ("d", "4", "open"), ("e", "5", "open"),
    ))
    assert v.bad_rows == 0, v.failures


def check_a_cross_column_check_only_judges_the_rows_it_selects():
    v = validate.validate(contract(with_checks=True), rows(
        ("a", "1", "open"), ("b", "", "open"), ("c", "", "shut"),
        ("d", "4", "open"), ("e", "5", "open"),
    ))
    assert v.considered == {"shut_has_a_number": 1}, v.considered
    assert v.rule_counts() == {("shut_has_a_number", "check:requires_when"): 1}


def check_a_clean_partition_reports_no_largest_rule_count():
    # Nothing was asking what the largest count is when there are no counts, so the
    # default the max falls back to was free to be anything.
    v = validate.validate(contract(), rows(("a", "1", "open"), ("b", "2", "open")))
    assert v.largest_rule_count() == 0, v.largest_rule_count()
    assert v.sum_of_rule_counts() == 0
    assert v.rows_breaking_more_than_one_rule() == 0


def check_a_row_breaking_exactly_one_rule_is_not_counted_as_breaking_several():
    # Both kinds of bad row in one fixture. With only the two rule row present, a
    # comparison of one or more reads the same as a comparison of more than one.
    v = validate.validate(contract(), rows(
        ("a", "1", "open"), ("b", "99", "open"), ("c", "99", "sideways"),
        ("d", "4", "open"), ("e", "5", "open"),
    ))
    assert v.bad_rows == 2, v.failures
    assert v.rows_breaking_more_than_one_rule() == 1


def check_the_cross_column_pass_is_counted_as_work_done():
    # The evaluation count is what a clean report leans on to say it looked at something.
    # Three of its four branches were pinned and the cross column one was not.
    plain = validate.validate(contract(), rows(("a", "1", "shut"), ("b", "2", "shut")))
    withchecks = validate.validate(contract(with_checks=True),
                                   rows(("a", "1", "shut"), ("b", "2", "shut")))
    assert withchecks.evaluations == plain.evaluations + 2, (
        withchecks.evaluations, plain.evaluations)


def check_rows_that_are_all_optional_and_all_absent_raise_rather_than_pass():
    # The branch nothing could reach through the missing column path. Every contracted
    # column is present in the header and every value is empty, so no predicate runs and
    # the answer would otherwise be a clean partition of two rows.
    optional = spec.parse(BASE.format(checks="").replace(
        "    required: true\n    unique: true\n", "    max_length: 40\n"))
    data = [dict(k="", n="", state=""), dict(k="", n="", state="")]
    try:
        validate.validate(optional, data)
    except profile_mod.NothingChecked as exc:
        assert "no rule was evaluated" in str(exc), exc
        return
    raise AssertionError("a pass that evaluated nothing reported a clean partition")


def check_a_partition_with_no_contracted_column_raises():
    bad = [{"other": "1"}, {"other": "2"}]
    try:
        validate.validate(contract(), bad)
    except profile_mod.NothingChecked:
        return
    raise AssertionError("a validation that evaluated nothing reported a clean partition")


def check_a_column_the_contract_does_not_cover_is_reported_and_not_judged():
    data = [dict(k="a", n="1", state="open", extra="x"),
            dict(k="b", n="2", state="open", extra="y")]
    v = validate.validate(contract(), data)
    assert v.uncontracted == ["extra"], v.uncontracted
    assert v.bad_rows == 0


def check_a_contracted_column_absent_from_the_data_is_reported_and_skipped():
    data = [dict(k="a", state="open"), dict(k="b", state="open"),
            dict(k="c", state="open")]
    v = validate.validate(contract(), data)
    assert v.missing == ["n"], v.missing
    assert v.bad_rows == 0
    # k gets required, type and unique. state gets type and allowed. n gets nothing.
    assert v.evaluations == 3 * 5, v.evaluations


def check_a_check_pointed_at_an_absent_column_is_skipped_rather_than_failing_every_row():
    data = [dict(k="a", n="1"), dict(k="b", n="2"), dict(k="c", n="3")]
    v = validate.validate(contract(with_checks=True), data)
    assert v.considered == {}, v.considered
    assert v.bad_rows == 0


def check_an_empty_partition_is_not_a_failure():
    v = validate.validate(contract(), [], header=["k", "n", "state"])
    assert v.rows == 0 and v.bad_rows == 0
    assert v.missing == [], v.missing


def _profile_counts(p):
    out = {}
    for result in p.results:
        for v in result.violations:
            out[(v.column, v.rule)] = v.count
    for result in p.checks:
        if result.violation:
            out[(result.check.name, result.violation.rule)] = result.violation.count
    return out


def check_the_column_view_and_the_row_view_agree_on_the_shipped_contract():
    """Two summaries of one contract over the same rows have to give the same per rule
    numbers. Grading each against the truth separately lets both be correct and still
    disagree, which is the failure this check exists for."""
    contract_ = spec.load(SHIPPED)
    rows_ = profile_mod.read_partition(FIXTURE)
    p = profile_mod.profile(contract_, FIXTURE, rules)
    v = validate.validate(contract_, rows_)

    assert v.rule_counts() == _profile_counts(p), (
        v.rule_counts(), _profile_counts(p))


def check_the_two_views_agree_when_something_is_actually_broken():
    # The check above runs against a fixture the contract may well pass cleanly, and two
    # empty dictionaries compare equal. This one guarantees there is something to
    # disagree about.
    c = contract(with_checks=True)
    data = rows(
        ("a", "1", "open"), ("dup", "99", "open"), ("dup", "3", "sideways"),
        ("d", "", "shut"), ("e", "5", "open"), ("f", "6", "open"),
    )
    p_results = [
        (col.name, v.rule, v.count)
        for col in c.columns
        for v in rules.evaluate(col, [r.get(col.name) for r in data])
    ]
    for chk in c.checks:
        violation, _ = rules.evaluate_check(chk, data)
        if violation:
            p_results.append((chk.name, violation.rule, violation.count))

    v = validate.validate(c, data)
    assert v.rule_counts() == {(s, r): n for s, r, n in p_results}, (
        v.rule_counts(), p_results)
    assert len(p_results) >= 4, p_results


def check_the_reason_column_names_every_rule_a_row_broke():
    v = validate.validate(contract(), rows(
        ("a", "1", "open"), ("b", "2", "open"), ("c", "99", "sideways"),
        ("d", "4", "open"), ("e", "5", "open"),
    ))
    text = quarantine.reasons(v.failures[2])
    assert "n:max" in text and "state:allowed" in text, text


def check_a_row_held_only_by_a_key_collision_is_separated_from_a_bad_row():
    """A uniqueness failure is not a statement about the row.

    Two rows share a value, the contract cannot say which is real, and both go. Folded
    into one held count that is invisible, so a replayed upstream job reads as a partition
    full of bad data. The fixture carries one genuinely bad row so the two numbers differ.
    """
    v = validate.validate(contract(), rows(
        ("a", "1", "open"), ("a", "2", "open"), ("c", "99", "open"),
        ("d", "4", "open"), ("e", "5", "open"),
    ))
    assert v.bad_rows == 3, v.failures
    assert v.held_only_by("unique") == {0, 1}, v.held_only_by("unique")


def check_a_row_breaking_a_rule_as_well_as_the_key_is_not_a_bystander():
    """The row at index 1 collides and is also out of range. It is a bad row that happens
    to collide, not a row held only because another one exists."""
    v = validate.validate(contract(), rows(
        ("a", "1", "open"), ("a", "99", "open"), ("c", "3", "open"),
        ("d", "4", "open"), ("e", "5", "open"),
    ))
    assert v.held_only_by("unique") == {0}, v.held_only_by("unique")


def check_held_only_by_is_empty_when_nothing_collided():
    v = validate.validate(contract(), rows(
        ("a", "1", "open"), ("b", "99", "open"), ("c", "3", "open"),
        ("d", "4", "open"), ("e", "5", "open"),
    ))
    assert v.held_only_by("unique") == set(), v.held_only_by("unique")


def check_the_largest_collision_is_the_size_of_the_biggest_group():
    v = validate.validate(contract(), rows(
        ("a", "1", "open"), ("a", "2", "open"), ("a", "3", "open"),
        ("d", "4", "open"), ("d", "5", "open"), ("f", "6", "open"),
    ))
    assert v.largest_collision() == 3, v.collisions
    assert v.collisions == {"k": {"a": 3, "d": 2}}, v.collisions


def check_the_largest_collision_on_a_clean_partition_is_one():
    """One rather than zero. Every key appears once, and a floor of zero would read as a
    partition with no keys in it."""
    v = validate.validate(contract(), rows(
        ("a", "1", "open"), ("b", "2", "open"), ("c", "3", "open"),
        ("d", "4", "open"), ("e", "5", "open"),
    ))
    assert v.largest_collision() == 1, v.collisions
    assert v.collisions == {}, v.collisions


def check_a_null_key_is_not_a_collision():
    """Two absent keys are not two rows claiming the same identity. Both are held by the
    required rule and neither is a bystander."""
    v = validate.validate(contract(), rows(
        ("", "1", "open"), ("", "2", "open"), ("c", "3", "open"),
        ("d", "4", "open"), ("e", "5", "open"),
    ))
    assert v.collisions == {}, v.collisions
    assert v.held_only_by("unique") == set(), v.held_only_by("unique")
    assert v.bad_rows == 2, v.failures
