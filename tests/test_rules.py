"""Rule evaluation, with the fixtures built so a rule can actually fail.

A fixture where every row is clean cannot test a rule about picking bad rows out. Every
fixture here carries at least two rows that disagree, because a one row fixture makes
three different implementations look identical.
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from contracts import rules, spec


def col(name="c", type_="string", provenance="asserted", **kw):
    return spec.Column(name, type_, kw, provenance, "")


def check_required_counts_nulls_and_nothing_else():
    v = rules.evaluate(col(required=True), ["a", "", "b", "   ", None])
    assert len(v) == 1
    assert v[0].rule == "required"
    assert v[0].count == 3, v[0].count


def check_a_nullable_range_does_not_report_a_null_as_out_of_range():
    # The rule this is really about. Folding nullability into the range check reports one
    # absent value as two failures and the rejection report then double counts it.
    v = rules.evaluate(col(type_="number", min=0, max=10), ["5", "", "7"])
    assert v == [], v


def check_min_and_max_are_separate_findings():
    v = rules.evaluate(col(type_="number", min=0, max=10), ["-1", "5", "11"])
    kinds = sorted(x.rule for x in v)
    assert kinds == ["max", "min"], kinds
    assert all(x.count == 1 for x in v)


def check_the_edges_of_a_range_are_inside_it():
    v = rules.evaluate(col(type_="number", min=0, max=10), ["0", "10"])
    assert v == [], v


def check_a_value_that_is_not_a_number_is_a_type_failure_not_a_range_one():
    v = rules.evaluate(col(type_="number", min=0, max=10), ["5", "abc"])
    assert [x.rule for x in v] == ["type:number"], [x.rule for x in v]
    assert v[0].count == 1


def check_unique_counts_duplicated_values_not_duplicate_rows():
    v = rules.evaluate(col(unique=True), ["a", "a", "a", "b"])
    assert len(v) == 1
    assert v[0].count == 1, v[0].count


def check_unique_passes_when_every_value_differs():
    assert rules.evaluate(col(unique=True), ["a", "b", "c"]) == []


def check_unique_keeps_only_three_examples_too():
    # Each rule builds its own example list and they are capped separately, so a cap
    # asserted on one rule says nothing about the others.
    values = ["a", "a", "b", "b", "c", "c", "d", "d", "e"]
    v = rules.evaluate(col(unique=True), values)
    assert v[0].count == 4, v[0].count
    assert v[0].examples == ["a", "b", "c"], v[0].examples


def check_allowed_is_case_sensitive_and_trims():
    v = rules.evaluate(col(allowed=["OPEN", "SHUT"]), ["OPEN", " SHUT ", "open"])
    assert len(v) == 1 and v[0].count == 1, v


def check_max_length_measures_the_trimmed_value():
    v = rules.evaluate(col(max_length=3), ["abc", " abc ", "abcd"])
    assert len(v) == 1 and v[0].count == 1, v


def check_matches_anchors_at_the_start_only_unless_the_pattern_says_otherwise():
    # re.match anchors the start and not the end. A five digit rule written without $
    # would accept '12345-extra', so the shipped contract has to carry the $ itself.
    v = rules.evaluate(col(matches="^[0-9]{5}$"), ["12345", "12345-6789", "1234"])
    assert len(v) == 1 and v[0].count == 2, v


def check_timestamp_accepts_both_socrata_shapes():
    v = rules.evaluate(col(type_="timestamp", required=True),
                       ["2025-01-01T00:00:00.000", "2025-01-01T00:00:00"])
    assert v == [], v


def check_a_timestamp_that_is_a_date_is_refused():
    v = rules.evaluate(col(type_="timestamp", required=True), ["2025-01-01"])
    assert [x.rule for x in v] == ["type:timestamp"], v


def _rows(*triples):
    return [{"created_date": a, "closed_date": b, "status": c} for a, b, c in triples]


def _check(name, kind, **args):
    return spec.Check(name, kind, "asserted", "", args)


def check_ordering_finds_an_out_of_order_pair():
    c = _check("o", "ordering", before="created_date", after="closed_date")
    v, considered = rules.evaluate_check(c, _rows(
        ("2025-01-02T00:00:00.000", "2025-01-03T00:00:00.000", "Closed"),
        ("2025-01-05T00:00:00.000", "2025-01-01T00:00:00.000", "Closed"),
    ))
    assert considered == 2
    assert v.count == 1


def check_ordering_skips_a_row_it_cannot_judge_rather_than_failing_it():
    c = _check("o", "ordering", before="created_date", after="closed_date")
    v, considered = rules.evaluate_check(c, _rows(
        ("2025-01-02T00:00:00.000", "", "Open"),
    ))
    assert considered == 0, considered
    assert v is None


def check_ordering_treats_equal_timestamps_as_in_order():
    # 1,510 rows of the real corpus close in the same second they were created. If this
    # were strict, a real and legitimate shape becomes the biggest violation in the feed.
    c = _check("o", "ordering", before="created_date", after="closed_date")
    v, considered = rules.evaluate_check(c, _rows(
        ("2025-01-02T00:00:00.000", "2025-01-02T00:00:00.000", "Closed"),
    ))
    assert considered == 1 and v is None


def check_requires_when_only_judges_the_rows_its_when_clause_selects():
    # The selected rows are deliberately lopsided, two bad and one good. An even split
    # passes just as happily against a rule that has been inverted, because the count of
    # failures does not move. A mutant that turned `not is_null` into `is_null` survived
    # the first version of this fixture for exactly that reason.
    c = _check("r", "requires_when", when_column="status", when_equals="Closed",
               then_required="closed_date", when_in=None)
    v, considered = rules.evaluate_check(c, _rows(
        ("2025-01-01T00:00:00.000", "", "Closed"),
        ("2025-01-01T00:00:00.000", "", "Closed"),
        ("2025-01-01T00:00:00.000", "", "Open"),
        ("2025-01-01T00:00:00.000", "2025-01-02T00:00:00.000", "Closed"),
    ))
    assert considered == 3, considered
    assert v.count == 2, v.count
    # The example carries the row that failed, not the row that passed.
    assert v.examples[0]["closed_date"] == ""


def check_forbids_when_is_the_mirror_of_requires_when():
    # Lopsided for the same reason as the check above.
    c = _check("f", "forbids_when", when_column="status", when_in=["Open", "Pending"],
               then_absent="closed_date", when_equals=None)
    v, considered = rules.evaluate_check(c, _rows(
        ("2025-01-01T00:00:00.000", "2025-01-02T00:00:00.000", "Open"),
        ("2025-01-01T00:00:00.000", "2025-01-02T00:00:00.000", "Pending"),
        ("2025-01-01T00:00:00.000", "", "Pending"),
        ("2025-01-01T00:00:00.000", "2025-01-02T00:00:00.000", "Closed"),
    ))
    assert considered == 3, considered
    assert v.count == 2, v.count


def check_integer_is_a_different_type_from_number():
    # Three valid against one invalid, not two against two. An even split survives an
    # inverted predicate with the same failure count, which is how the first version of
    # this let a mutant through.
    v = rules.evaluate(col(type_="integer", required=True), ["1", "2", "3", "3.5"])
    assert [x.rule for x in v] == ["type:integer"], v
    assert v[0].count == 1, v[0].count
    assert v[0].examples == ["3.5"], v[0].examples


def check_a_float_shaped_value_is_a_valid_number_and_not_a_valid_integer():
    assert rules.evaluate(col(type_="number", required=True), ["3.5"]) == []
    assert rules.evaluate(col(type_="integer", required=True), ["3.5"]) != []


def check_only_three_examples_are_kept():
    # The rejection report shows these to a human. An unbounded list turns one broken
    # partition into a page of identical rows.
    v = rules.evaluate(col(required=True), ["", "", "", "", "", "ok"])
    assert v[0].count == 5
    assert len(v[0].examples) == 3, len(v[0].examples)


def check_only_three_examples_are_kept_for_a_cross_column_check():
    c = _check("r", "requires_when", when_column="status", when_equals="Closed",
               then_required="closed_date", when_in=None)
    v, considered = rules.evaluate_check(c, _rows(
        *[("2025-01-01T00:00:00.000", "", "Closed")] * 5
    ))
    assert v.count == 5 and len(v.examples) == 3, v


def check_a_when_clause_that_selects_nothing_reports_zero_judged():
    # A check that judged nothing has to be distinguishable from a check that passed.
    # Reporting both as "no violations" is how a rule stops working without anyone
    # noticing.
    c = _check("r", "requires_when", when_column="status", when_equals="Nonexistent",
               then_required="closed_date", when_in=None)
    v, considered = rules.evaluate_check(c, _rows(
        ("2025-01-01T00:00:00.000", "", "Closed"),
    ))
    assert considered == 0 and v is None


def check_an_unknown_check_kind_raises():
    c = _check("x", "ordering", before="created_date", after="closed_date")
    c.kind = "invented"
    try:
        rules.evaluate_check(c, _rows(("2025-01-01T00:00:00.000", "", "Open")))
    except ValueError as exc:
        assert "invented" in str(exc)
        return
    raise AssertionError("expected a refusal")
