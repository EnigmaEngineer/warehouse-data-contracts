"""Deliberate contract breaks, and the control that says a refusal came from the rule.

The most important check in here is the one that builds a break wired to the wrong rule and
asserts the probe notices. Everything else in this file could pass while the control column
was decorative.
"""

import csv
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from contracts import breaks, spec, validate

FIXTURE = os.path.join(HERE, "fixtures", "sample_partition.csv")
SHIPPED = os.path.join(ROOT, "contracts", "nyc311.yml")


def read_fixture():
    with open(FIXTURE, newline="") as fh:
        reader = csv.DictReader(fh)
        return reader.fieldnames, list(reader)

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
checks:
  - name: closed_after_opened
    kind: ordering
    before: day
    after: closed
    provenance: asserted
columns:
  - name: day
    type: string
    provenance: documented
    required: true
  - name: closed
    type: timestamp
    provenance: asserted
    required: false
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
  - name: only_rule
    type: string
    provenance: asserted
    required: false
"""

HEADER = ["day", "closed", "key", "zone", "only_rule"]


def contract():
    return spec.parse(CONTRACT)


def rows(n=12):
    return [{"day": "2025-03-01", "closed": "", "key": "k{}".format(i),
             "zone": "north" if i % 2 else "south", "only_rule": "x"}
            for i in range(n)]


def check_relaxing_one_rule_leaves_the_others_on_that_column():
    c = breaks.relax_column(contract(), "zone", "allowed")
    zone = [col for col in c.columns if col.name == "zone"][0]
    assert "allowed" not in zone.rules, zone.rules
    assert zone.rules.get("required") is True, zone.rules


def check_relaxing_a_rule_the_column_does_not_have_is_refused():
    try:
        breaks.relax_column(contract(), "zone", "matches")
    except breaks.CannotRelax as exc:
        assert "matches" in str(exc), str(exc)
    else:
        raise AssertionError("relaxed a rule that was not there")


def check_relaxing_the_last_rule_off_a_column_is_refused():
    """The loader refuses a column with no constraints, so the relaxed contract would be
    a shape spec.parse never produces and the control would test a fiction."""
    try:
        breaks.relax_column(contract(), "only_rule", "required")
    except breaks.CannotRelax as exc:
        assert "no constraints" in str(exc), str(exc)
    else:
        raise AssertionError("emptied a column and called it a relaxation")


def check_relaxing_a_column_that_is_not_there_is_refused():
    try:
        breaks.relax_column(contract(), "nope", "required")
    except breaks.CannotRelax:
        pass
    else:
        raise AssertionError("relaxed a column the contract does not define")


def check_dropping_a_column_a_check_reads_is_refused():
    """Dropping it would switch the check off too, so the control would be relaxing two
    rules and the break would be proving less than it claims."""
    try:
        breaks.drop_column(contract(), "closed")
    except breaks.CannotRelax as exc:
        assert "closed_after_opened" in str(exc), str(exc)
    else:
        raise AssertionError("dropped a column a cross column check reads")


def check_dropping_the_partition_column_is_refused():
    try:
        breaks.drop_column(contract(), "day")
    except breaks.CannotRelax as exc:
        assert "partition" in str(exc), str(exc)
    else:
        raise AssertionError("dropped the partition column")


def check_dropping_a_column_leaves_the_rest_of_the_contract_alone():
    c = breaks.drop_column(contract(), "zone")
    assert [col.name for col in c.columns] == ["day", "closed", "key", "only_rule"]
    assert [k.name for k in c.checks] == ["closed_after_opened"]


def check_relaxing_a_check_removes_only_that_check():
    c = breaks.relax_check(contract(), "closed_after_opened")
    assert c.checks == []
    assert len(c.columns) == 5


def check_relaxing_a_check_that_is_not_there_is_refused():
    try:
        breaks.relax_check(contract(), "nope")
    except breaks.CannotRelax:
        pass
    else:
        raise AssertionError("relaxed a check the contract does not have")


def check_targets_are_spread_rather_than_taken_off_the_front():
    picked = breaks.targets(list(range(100)), 3)
    assert picked == [25, 50, 75], picked


def check_targets_refuses_a_count_that_would_change_the_partition():
    try:
        breaks.targets(list(range(10)), 6)
    except ValueError as exc:
        assert "clean rows" in str(exc), str(exc)
    else:
        raise AssertionError("agreed to damage most of the file")


def check_targets_refuses_zero():
    try:
        breaks.targets(list(range(10)), 0)
    except ValueError:
        pass
    else:
        raise AssertionError("a break that damages nothing was accepted")


def check_every_break_in_the_catalogue_has_a_unique_name():
    names = [b.name for b in breaks.CATALOGUE]
    assert len(names) == len(set(names)), names


def check_by_name_refuses_a_name_that_is_not_there():
    try:
        breaks.by_name("no_such_break")
    except KeyError:
        pass
    else:
        raise AssertionError("looked up a break that does not exist")


def check_a_break_holds_the_row_it_damaged_and_cites_the_named_rule():
    c = contract()
    brk = breaks.Break("zone_break", "zone", "allowed",
                       lambda con: breaks.relax_column(con, "zone", "allowed"),
                       lambda rs, i, d: breaks._set(rs, i, {"zone": "east"}),
                       "a value outside the vocabulary")
    out = breaks.run_break(c, rows(), brk, count=1, header=HEADER)
    assert out.caught, out.new_holds
    assert out.by_the_named_rule, out.reasons
    assert out.control_clean, out.control_holds
    assert out.collateral == set(), out.collateral
    assert out.amplification == 1.0, out.amplification


def check_the_control_catches_a_break_wired_to_the_wrong_rule():
    """The check the rest of this file rests on.

    This break damages `zone` and relaxes `key`. The row is still refused under the
    relaxed contract, because the rule that catches it was never removed, and
    `control_clean` has to say so. Without this, the control column could be hard coded to
    clean and every other check here would still pass.
    """
    c = contract()
    miswired = breaks.Break(
        "miswired", "zone", "allowed",
        lambda con: breaks.relax_column(con, "key", "unique"),
        lambda rs, i, d: breaks._set(rs, i, {"zone": "east"}),
        "aimed at zone and relaxes key")
    out = breaks.run_break(c, rows(), miswired, count=1, header=HEADER)
    assert out.caught, out.new_holds
    assert not out.control_clean, "a miswired control reported clean"


def check_a_break_naming_the_wrong_rule_is_reported_as_such():
    """Caught, controlled, and citing a different rule from the one it claims."""
    c = contract()
    mislabelled = breaks.Break(
        "mislabelled", "zone", "max_length",
        lambda con: breaks.relax_column(con, "zone", "allowed"),
        lambda rs, i, d: breaks._set(rs, i, {"zone": "east"}),
        "claims max_length and trips allowed")
    out = breaks.run_break(c, rows(), mislabelled, count=1, header=HEADER)
    assert out.caught, out.new_holds
    assert not out.by_the_named_rule, out.reasons


def check_the_duplicate_key_break_costs_two_rows_for_one_write():
    c = contract()
    brk = breaks.Break("dupe", "key", "unique",
                       lambda con: breaks.relax_column(con, "key", "unique"),
                       lambda rs, i, d: breaks._copy_key(rs, i, d, "key"),
                       "one row takes another's key")
    out = breaks.run_break(c, rows(), brk, count=1, header=HEADER)
    assert out.injected == 1, out.injected
    assert len(out.new_holds) == 2, out.new_holds
    assert out.amplification == 2.0, out.amplification
    assert out.control_clean, out.control_holds


def check_a_break_that_nothing_catches_reports_no_holds():
    c = contract()
    brk = breaks.Break("silent", "only_rule", "nothing", lambda con: con,
                       lambda rs, i, d: breaks._set(rs, i, {"only_rule": "y"}),
                       "no rule reads this value", expects_hold=False)
    out = breaks.run_break(c, rows(), brk, count=1, header=HEADER)
    assert out.caught, out.new_holds
    assert out.new_holds == set(), out.new_holds
    assert out.control_clean


def check_a_silent_break_that_does_get_caught_is_reported_as_not_caught():
    """The partner. Without it the check above passes on a break class that can never
    fail, because `caught` for an uncaught break is just an empty set."""
    c = contract()
    wrong = breaks.Break("noisy", "zone", "nothing", lambda con: con,
                         lambda rs, i, d: breaks._set(rs, i, {"zone": "east"}),
                         "claims nothing reads it and something does",
                         expects_hold=False)
    out = breaks.run_break(c, rows(), wrong, count=1, header=HEADER)
    assert not out.caught, out.new_holds


def check_damaging_more_rows_holds_more_rows():
    c = contract()
    brk = breaks.Break("zone_break", "zone", "allowed",
                       lambda con: breaks.relax_column(con, "zone", "allowed"),
                       lambda rs, i, d: breaks._set(rs, i, {"zone": "east"}),
                       "a value outside the vocabulary")
    out = breaks.run_break(c, rows(20), brk, count=3, header=HEADER)
    assert out.injected == 3, out.injected
    assert len(out.new_holds) == 3, out.new_holds
    # Three rows written and three held is a ratio of one. Multiplying instead of dividing
    # gives nine here and gives the same answer as division whenever one row was written,
    # which is every other case in this file.
    assert out.amplification == 1.0, out.amplification


def check_the_original_rows_are_left_alone_by_a_break():
    """A break that mutated its input in place would make the second call to it measure
    the first one's damage."""
    c = contract()
    original = rows()
    before = [dict(r) for r in original]
    brk = breaks.Break("zone_break", "zone", "allowed",
                       lambda con: breaks.relax_column(con, "zone", "allowed"),
                       lambda rs, i, d: breaks._set(rs, i, {"zone": "east"}),
                       "a value outside the vocabulary")
    breaks.run_break(c, original, brk, count=1, header=HEADER)
    assert original == before, "the break wrote through to the caller's rows"


def check_clean_indexes_leaves_out_a_row_that_was_already_bad():
    c = contract()
    dirty = rows()
    dirty[3] = dict(dirty[3])
    dirty[3]["zone"] = "east"
    clean, result = breaks.clean_indexes(c, dirty, header=HEADER)
    assert 3 not in clean, clean
    assert len(clean) == len(dirty) - 1
    assert result.bad_rows == 1


def check_a_replayed_partition_holds_every_row():
    c = contract()
    doubled = breaks.replay_partition(rows(6), times=2)
    result = validate.validate(c, doubled, header=HEADER)
    assert result.rows == 12, result.rows
    assert result.bad_rows == 12, result.bad_rows
    assert len(result.held_only_by("unique")) == 12
    assert result.largest_collision() == 2


def check_a_replay_of_one_is_refused():
    try:
        breaks.replay_partition(rows(), times=1)
    except ValueError:
        pass
    else:
        raise AssertionError("a replay of one copy was accepted")


def check_a_replay_copies_the_rows_rather_than_aliasing_them():
    original = rows(3)
    doubled = breaks.replay_partition(original, times=2)
    doubled[0]["zone"] = "east"
    assert doubled[3]["zone"] != "east", "the two copies share a dict"
    assert original[0]["zone"] != "east", "the replay wrote through to the input"


def check_the_boundary_of_the_target_guard_is_where_it_is_written():
    """Five of ten is allowed and six is not.

    Without a case sitting exactly on the line, the guard can be widened or narrowed by
    one and every other check here still passes.
    """
    assert len(breaks.targets(list(range(10)), 5)) == 5
    try:
        breaks.targets(list(range(10)), 6)
    except ValueError:
        pass
    else:
        raise AssertionError("six of ten clean rows was accepted")


def check_every_shipped_break_behaves_as_its_name_says():
    """The catalogue itself, against the real contract and a real partition.

    Every other check in this file builds its own break, so the shipped ones were only
    ever exercised by a script nothing runs in the suite. A mutation flipping the value
    `wrong_agency` writes, or flipping its `expects_hold` flag, survived until this
    existed.
    """
    contract_ = spec.load(SHIPPED)
    header, data = read_fixture()
    for brk in breaks.CATALOGUE:
        out = breaks.run_break(contract_, data, brk, count=1, header=header)
        assert out.caught, (brk.name, out.new_holds, out.reasons)
        assert out.by_the_named_rule, (brk.name, out.reasons)
        assert out.control_clean, (brk.name, out.control_holds)


def check_the_break_that_gets_through_really_changes_the_row():
    """`wrong_agency` is the only break expected to be accepted, so `caught` for it is an
    empty set and would stay empty if the mutation did nothing at all."""
    contract_ = spec.load(SHIPPED)
    header, data = read_fixture()
    brk = breaks.by_name("wrong_agency")
    index = breaks.targets(breaks.clean_indexes(contract_, data, header=header)[0], 1)[0]
    working = list(data)
    brk.damage(working, index, 0)
    assert working[index]["agency"] != data[index]["agency"], working[index]
    assert working[index]["agency"] in ("NYPD", "DSNY"), working[index]


def check_the_duplicate_key_break_ships_costing_two_rows():
    """The amplification figure comes off the shipped catalogue rather than off a break
    written in this file."""
    contract_ = spec.load(SHIPPED)
    header, data = read_fixture()
    out = breaks.run_break(contract_, data, breaks.by_name("duplicate_key"),
                           count=1, header=header)
    assert out.injected == 1, out.injected
    assert out.amplification == 2.0, out.amplification


def check_a_break_that_catches_nothing_does_not_claim_the_named_rule():
    """A break expecting a hold and getting none. Without this the `not caught` branch of
    by_the_named_rule can return either answer and nothing notices."""
    c = contract()
    hopeful = breaks.Break(
        "hopeful", "only_rule", "required",
        lambda con: breaks.relax_column(con, "zone", "allowed"),
        lambda rs, i, d: breaks._set(rs, i, {"only_rule": "still fine"}),
        "aims at a rule that permits everything")
    out = breaks.run_break(c, rows(), hopeful, count=1, header=HEADER)
    assert not out.caught, out.new_holds
    assert not out.by_the_named_rule, out.reasons


def check_one_row_is_damaged_when_no_count_is_given():
    c = contract()
    brk = breaks.Break("zone_break", "zone", "allowed",
                       lambda con: breaks.relax_column(con, "zone", "allowed"),
                       lambda rs, i, d: breaks._set(rs, i, {"zone": "east"}),
                       "a value outside the vocabulary")
    out = breaks.run_break(c, rows(), brk, header=HEADER)
    assert out.injected == 1, out.injected


def check_the_donor_is_the_last_clean_row():
    """An arbitrary choice, pinned because the guard against a donor that is also a target
    depends on it. The spread never reaches the end of the clean list, so the last row is
    the one that cannot collide."""
    c = contract()
    data = rows()
    clean, _ = breaks.clean_indexes(c, data, header=HEADER)
    brk = breaks.Break("dupe", "key", "unique",
                       lambda con: breaks.relax_column(con, "key", "unique"),
                       lambda rs, i, d: breaks._copy_key(rs, i, d, "key"),
                       "one row takes another's key")
    out = breaks.run_break(c, data, brk, count=1, header=HEADER)
    assert max(out.new_holds) == clean[-1], (out.new_holds, clean[-1])


def check_a_replay_with_no_count_given_makes_two_copies():
    original = rows(4)
    assert len(breaks.replay_partition(original)) == 8
