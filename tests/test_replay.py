"""Reading the quarantine back and judging it again.

The check that matters most here is the one where nothing changes. A re-judge against an
unchanged contract has to recover zero rows, and a row it does recover is not evidence about
the contract. It is a value that did not survive the trip through the quarantine file.
"""

import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from contracts import breaks, quarantine, replay, spec, validate

CONTRACT = """
dataset: widgets
source:
  kind: socrata
  domain: d
  resource: r
  partition_column: day
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
  - name: day
    type: string
    provenance: documented
    required: true
  - name: key
    type: string
    provenance: documented
    required: true
    unique: true
  - name: zone
    type: string
    provenance: asserted
    required: true
    allowed: [north, south]
"""

HEADER = ["day", "key", "zone"]


def contract():
    return spec.parse(CONTRACT)


def rows():
    out = []
    for i in range(8):
        out.append({"day": "2025-03-01", "key": "k{}".format(i),
                    "zone": "north" if i % 2 else "south"})
    # Two rows the contract refuses, for two different reasons.
    out[2] = dict(out[2], zone="east")
    out[5] = dict(out[5], key="k4")
    return out


def written(root, contract_, data):
    result = validate.validate(contract_, data, header=HEADER)
    quarantine.write(root, HEADER, data, result, "2025-03-01")
    return result


def check_an_unchanged_contract_recovers_nothing():
    c = contract()
    with tempfile.TemporaryDirectory() as tmp:
        written(tmp, c, rows())
        out = replay.rejudge(c, tmp)
    assert out["recovered"] == 0, out
    assert out["still_held"] == out["held"], out
    assert out["rows_held_for_a_different_reason"] == [], out


def check_relaxing_the_rule_recovers_exactly_the_rows_it_held():
    c = contract()
    with tempfile.TemporaryDirectory() as tmp:
        written(tmp, c, rows())
        relaxed = breaks.relax_column(c, "zone", "allowed")
        out = replay.rejudge(relaxed, tmp)
    assert out["recovered"] == 1, out
    assert out["reasons_that_stopped_firing"] == {"zone:allowed": 1}, out


def check_a_uniqueness_hold_is_judged_inside_the_whole_partition():
    """The held rows are judged against the accepted ones, not on their own.

    Both copies of a duplicated key are held today, so a re-judge reading only the held
    file would come out right by luck. This directory is written by hand with one copy on
    each side, which is the shape any policy that let a copy through would produce. Reading
    only the held rows recovers that row. Reading both keeps it.
    """
    c = contract()
    with tempfile.TemporaryDirectory() as tmp:
        with open(os.path.join(tmp, replay.ACCEPTED), "w") as fh:
            fh.write("day,key,zone\n2025-03-01,k9,north\n")
        with open(os.path.join(tmp, replay.HELD), "w") as fh:
            fh.write("day,key,zone,{}\n2025-03-01,k9,east,zone:allowed\n".format(
                quarantine.REASON_COLUMN))
        relaxed = breaks.relax_column(c, "zone", "allowed")
        out = replay.rejudge(relaxed, tmp)
    assert out["held"] == 1 and out["accepted"] == 1, out
    assert out["recovered"] == 0, out
    assert out["rows_held_for_a_different_reason"][0]["now"] == ["key:unique"], out


def check_a_row_whose_reason_changed_is_reported():
    c = contract()
    with tempfile.TemporaryDirectory() as tmp:
        written(tmp, c, rows())
        # Same intent, different rule. `east` fails the pattern and the two real zones
        # pass it, so exactly one row changes the reason it is held for.
        reworded = spec.parse(CONTRACT.replace(
            "    allowed: [north, south]",
            '    matches: "^(north|south)$"'))
        out = replay.rejudge(reworded, tmp)
    changed = out["rows_held_for_a_different_reason"]
    assert len(changed) == 1, out
    assert changed[0]["was"] == ["zone:allowed"], changed
    assert changed[0]["now"] == ["zone:matches"], changed


def check_a_directory_with_no_split_is_refused():
    with tempfile.TemporaryDirectory() as tmp:
        try:
            replay.rejudge(contract(), tmp)
        except replay.NoQuarantine as exc:
            assert "quarantined.csv" in str(exc), str(exc)
        else:
            raise AssertionError("re-judged a directory holding nothing")


def check_a_held_file_with_no_reason_column_is_refused():
    c = contract()
    with tempfile.TemporaryDirectory() as tmp:
        written(tmp, c, rows())
        with open(os.path.join(tmp, replay.HELD), "w") as fh:
            fh.write("day,key,zone\n2025-03-01,k9,east\n")
        try:
            replay.rejudge(c, tmp)
        except replay.NoQuarantine as exc:
            assert quarantine.REASON_COLUMN in str(exc), str(exc)
        else:
            raise AssertionError("re-judged a file that never said why")


def check_the_reason_column_is_stripped_before_judging():
    """Left in, it is an uncontracted column and the counts would still come out right,
    which is why this asserts on the value rather than on the totals."""
    c = contract()
    with tempfile.TemporaryDirectory() as tmp:
        written(tmp, c, rows())
        held, reasons, accepted = replay.read_split(tmp)
    assert all(quarantine.REASON_COLUMN not in r for r in held), held
    assert sorted(reasons) == ["key:unique", "key:unique", "zone:allowed"], reasons
    assert len(accepted) == 5, accepted


def check_an_empty_partition_is_refused_rather_than_reported_clean():
    c = contract()
    with tempfile.TemporaryDirectory() as tmp:
        result = validate.validate(c, [], header=HEADER)
        quarantine.write(tmp, HEADER, [], result, "2025-03-01")
        try:
            replay.rejudge(c, tmp)
        except replay.NoQuarantine as exc:
            assert "no rows" in str(exc), str(exc)
        else:
            raise AssertionError("a directory with nothing in it reported a re-judge")


# A separate contract for the marginal checks, carrying one column of each shape the
# enumeration has to treat differently. Kept apart from the one above so a change here
# cannot quietly move the counts every other check in this file asserts.
SHAPES = """
dataset: widgets
source:
  kind: socrata
  domain: d
  resource: r
  partition_column: day
  partition_grain: day
freshness:
  max_lag_hours: 1
  reference: extract
  applies_to: tail
  provenance: asserted
volume:
  min_rows_per_partition: 1
  provenance: asserted
checks:
  - name: shut_needs_a_zone
    kind: requires_when
    when_column: state
    when_equals: shut
    then_required: zone
    provenance: asserted
columns:
  - name: day
    type: string
    provenance: documented
    required: true
  - name: key
    type: string
    provenance: documented
    required: true
    unique: true
  - name: memo
    type: string
    provenance: asserted
    required: false
  - name: state
    type: string
    provenance: asserted
    required: true
    allowed: [open, shut]
  - name: zone
    type: string
    provenance: asserted
    required: false
    max_length: 5
"""

SHAPE_HEADER = ["day", "key", "memo", "state", "zone"]


def shapes():
    return spec.parse(SHAPES)


def labels(contract_):
    return [label for label, _, _ in replay.constraints(contract_)]


def check_every_constraint_in_the_contract_appears_once():
    c = shapes()
    got = labels(c)
    assert len(got) == c.constraint_count() + len(c.checks), got
    assert len(got) == len(set(got)), got


def check_a_column_carrying_one_rule_cannot_lose_it():
    """The loader refuses a column with no constraints, so relaxing that one would build
    a contract spec.parse never produces."""
    entry = [e for e in replay.constraints(shapes()) if e[0] == "day:required"][0]
    assert entry[1] is None, entry
    assert "only rule" in entry[2], entry


def check_a_rule_that_permits_is_not_counted_as_one_that_refuses():
    entry = [e for e in replay.constraints(shapes()) if e[0] == "memo:required"][0]
    assert entry[1] is None, entry
    assert "permits" in entry[2], entry


def check_a_column_with_two_rules_can_lose_either_of_them():
    """The partner to the two above. Without it, an enumeration that marked everything
    unremovable would pass both of them."""
    entries = dict((e[0], e) for e in replay.constraints(shapes()))
    for label, rule, column in (("key:unique", "unique", "key"),
                                ("state:allowed", "allowed", "state"),
                                ("zone:max_length", "max_length", "zone")):
        relax = entries[label][1]
        assert relax is not None, entries[label]
        relaxed = relax(shapes())
        target = [c for c in relaxed.columns if c.name == column][0]
        assert rule not in target.rules, target.rules


def check_a_check_can_be_taken_out_and_leaves_the_columns_alone():
    entries = dict((e[0], e) for e in replay.constraints(shapes()))
    relax = entries["shut_needs_a_zone:check:requires_when"][1]
    relaxed = relax(shapes())
    assert relaxed.checks == []
    assert len(relaxed.columns) == 5


def shape_rows():
    out = []
    for i in range(8):
        out.append({"day": "2025-03-01", "key": "k{}".format(i), "memo": "",
                    "state": "open", "zone": "north"})
    # One row breaks the vocabulary and nothing else.
    out[1] = dict(out[1], state="sideways")
    # One pair shares a key and is otherwise fine.
    out[6] = dict(out[6], key="k5")
    # One row breaks two rules at once, so no single constraint frees it.
    out[3] = dict(out[3], state="sideways", zone="a much longer zone")
    return out


def written_shapes(root):
    data = shape_rows()
    result = validate.validate(shapes(), data, header=SHAPE_HEADER)
    quarantine.write(root, SHAPE_HEADER, data, result, "2025-03-01")
    return result


def check_marginal_counts_only_the_rows_a_constraint_alone_is_holding():
    with tempfile.TemporaryDirectory() as tmp:
        result = written_shapes(tmp)
        table = dict((label, n) for label, n, _ in replay.marginal(shapes(), [tmp]))
    # Four rows held. Two for the key, and two for the vocabulary, one of which is also
    # too long in another column.
    assert result.bad_rows == 4, result.failures
    assert table["key:unique"] == 2, table
    assert table["state:allowed"] == 1, table
    assert table["zone:max_length"] == 0, table
    assert table["key:required"] == 0, table


def check_marginal_marks_the_constraints_it_cannot_remove_rather_than_scoring_them_zero():
    """A zero and an n/a mean different things. A constraint that cannot be taken out has
    not been measured, and printing 0 for it would read as a rule carrying nothing."""
    with tempfile.TemporaryDirectory() as tmp:
        written_shapes(tmp)
        table = replay.marginal(shapes(), [tmp])
    by_label = dict((label, (n, reason)) for label, n, reason in table)
    assert by_label["day:required"][0] is None, by_label["day:required"]
    assert by_label["memo:required"][0] is None, by_label["memo:required"]
    assert by_label["key:unique"][0] == 2, by_label["key:unique"]
    assert by_label["key:unique"][1] is None, by_label["key:unique"]


def check_marginal_over_no_partitions_is_refused():
    try:
        replay.marginal(shapes(), [])
    except ValueError as exc:
        assert "no partitions" in str(exc), str(exc)
    else:
        raise AssertionError("a marginal count over nothing was reported as one")


def check_marginal_adds_up_across_two_partitions():
    """One partition would pass against a loop that ignores everything after the first."""
    with tempfile.TemporaryDirectory() as one:
        with tempfile.TemporaryDirectory() as two:
            written_shapes(one)
            written_shapes(two)
            table = dict((l, n) for l, n, _ in replay.marginal(shapes(), [one, two]))
    assert table["key:unique"] == 4, table
    assert table["state:allowed"] == 2, table
