"""Splitting a judged partition and writing the rejection report.

Every fixture here is lopsided, and the two output files are checked against each other
rather than each on its own. A split that dropped a row would leave both files looking
reasonable.
"""

import csv
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from contracts import quarantine, spec, validate

FIELDS = ["k", "n", "state"]

CONTRACT = """
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
  - name: n
    type: integer
    provenance: asserted
    min: 0
    max: 10
  - name: state
    type: string
    provenance: asserted
    allowed: [open, shut]
"""


def contract():
    return spec.parse(CONTRACT)


def rows(*items):
    return [dict(zip(FIELDS, i)) for i in items]


def dirty():
    # One bad row in six, and it is bad twice. An inverted rule would hold five.
    return rows(
        ("a", "1", "open"), ("b", "2", "open"), ("c", "99", "sideways"),
        ("d", "4", "open"), ("e", "5", "open"), ("f", "6", "shut"),
    )


def check_the_split_keeps_every_row_exactly_once():
    data = dirty()
    v = validate.validate(contract(), data)
    accepted, held = quarantine.split(data, v)
    assert len(accepted) == 5 and len(held) == 1, (len(accepted), len(held))
    keys = sorted([r["k"] for r in accepted] + [r["k"] for r in held])
    assert keys == sorted(r["k"] for r in data), keys


def check_an_accepted_row_does_not_carry_the_reason_column():
    data = dirty()
    v = validate.validate(contract(), data)
    accepted, held = quarantine.split(data, v)
    assert all(quarantine.REASON_COLUMN not in r for r in accepted)
    assert all(quarantine.REASON_COLUMN in r for r in held)


def check_splitting_does_not_mutate_the_rows_it_was_given():
    data = dirty()
    v = validate.validate(contract(), data)
    quarantine.split(data, v)
    assert all(quarantine.REASON_COLUMN not in r for r in data), data


def check_a_partition_where_everything_passes_writes_an_empty_quarantine_file():
    data = rows(("a", "1", "open"), ("b", "2", "open"), ("c", "3", "shut"))
    v = validate.validate(contract(), data)
    out = tempfile.mkdtemp(prefix="wdc-clean-")
    try:
        summary = quarantine.write(out, FIELDS, data, v, "2025-01-01")
        assert summary["held"] == 0 and summary["accepted"] == 3
        with open(os.path.join(out, "quarantined.csv"), newline="") as fh:
            written = list(csv.DictReader(fh))
        assert written == [], written
    finally:
        shutil.rmtree(out)


def check_a_partition_where_everything_fails_still_writes_a_usable_accepted_header():
    # A downstream reader opening a headerless file is a different failure from an empty
    # batch, and the second one is the truth here.
    data = rows(("a", "99", "sideways"), ("b", "98", "sideways"))
    v = validate.validate(contract(), data)
    out = tempfile.mkdtemp(prefix="wdc-allbad-")
    try:
        quarantine.write(out, FIELDS, data, v, "2025-01-01")
        with open(os.path.join(out, "accepted.csv"), newline="") as fh:
            reader = csv.DictReader(fh)
            assert reader.fieldnames == FIELDS, reader.fieldnames
            assert list(reader) == []
    finally:
        shutil.rmtree(out)


def check_the_written_files_account_for_every_input_row():
    data = dirty()
    v = validate.validate(contract(), data)
    out = tempfile.mkdtemp(prefix="wdc-split-")
    try:
        summary = quarantine.write(out, FIELDS, data, v, "2025-01-06", "somewhere.csv")
        with open(os.path.join(out, "accepted.csv"), newline="") as fh:
            accepted = list(csv.DictReader(fh))
        with open(os.path.join(out, "quarantined.csv"), newline="") as fh:
            held = list(csv.DictReader(fh))
        assert len(accepted) + len(held) == len(data)
        assert summary["accepted"] == len(accepted)
        assert summary["held"] == len(held)
        assert held[0][quarantine.REASON_COLUMN].count(":") == 2, held[0]
    finally:
        shutil.rmtree(out)


def check_the_report_carries_all_three_row_counts_and_they_disagree():
    # If the three ever came back equal on this fixture the check would pass while
    # measuring nothing, so the fixture is built to make them differ.
    data = dirty()
    v = validate.validate(contract(), data)
    summary = quarantine.report(v, "2025-01-06")
    assert summary["held"] == 1
    assert summary["sum_of_rule_counts"] == 2
    assert summary["largest_rule_count"] == 1
    assert summary["rows_breaking_more_than_one_rule"] == 1


def check_the_held_share_is_held_over_rows_and_keeps_six_places():
    # One in six is the fixture on purpose. It needs every one of the six places to come
    # out right, so a rounding change shows and so does a division the wrong way up.
    data = dirty()
    v = validate.validate(contract(), data)
    assert quarantine.report(v, "x")["held_share"] == 0.166667


def check_the_held_share_of_a_partition_with_no_rows_is_zero_rather_than_a_crash():
    v = validate.validate(contract(), [], header=FIELDS)
    assert quarantine.report(v, "x")["held_share"] == 0.0


def check_the_report_file_has_sorted_keys_so_two_runs_can_be_diffed():
    data = dirty()
    v = validate.validate(contract(), data)
    out = tempfile.mkdtemp(prefix="wdc-order-")
    try:
        quarantine.write(out, FIELDS, data, v, "2025-01-06")
        with open(os.path.join(out, "report.json")) as fh:
            text = fh.read()
        keys = [line.split('"')[1] for line in text.splitlines()
                if line.startswith("  \"")]
        assert keys == sorted(keys), keys
        # The indent is part of the claim. A diff between two runs is only readable if
        # the layout is the same in both, and the loop above finds nothing at all if the
        # top level keys move to a different column.
        assert len(keys) > 5, keys
    finally:
        shutil.rmtree(out)


def check_the_load_and_the_report_agree_about_the_field_names():
    """The loader reads `accepted` and `held` out of this file by name. Renaming either
    one here would break the load and nothing in this file would notice."""
    data = dirty()
    v = validate.validate(contract(), data)
    out = tempfile.mkdtemp(prefix="wdc-fields-")
    try:
        quarantine.write(out, FIELDS, data, v, "2025-01-06")
        with open(os.path.join(out, "report.json")) as fh:
            report = json.load(fh)
        for key in ("partition", "accepted", "held"):
            assert key in report, (key, sorted(report))
    finally:
        shutil.rmtree(out)


def check_the_report_is_json_serialisable():
    data = dirty()
    v = validate.validate(contract(), data)
    text = json.dumps(quarantine.report(v, "2025-01-06"))
    assert "sum_of_rule_counts" in text


def check_a_source_column_named_like_the_reason_column_is_refused():
    fields = FIELDS + [quarantine.REASON_COLUMN]
    data = [dict(zip(fields, ("a", "1", "open", "whatever")))]
    v = validate.validate(contract(), data)
    out = tempfile.mkdtemp(prefix="wdc-clash-")
    try:
        quarantine.write(out, fields, data, v, "2025-01-01")
    except quarantine.ReasonColumnClash:
        return
    finally:
        shutil.rmtree(out)
    raise AssertionError("the report would have overwritten a source column")


def check_the_reasons_string_lists_the_rules_rather_than_a_count():
    data = dirty()
    v = validate.validate(contract(), data)
    _, held = quarantine.split(data, v)
    text = held[0][quarantine.REASON_COLUMN]
    assert "n:max" in text and "state:allowed" in text, text
    assert quarantine.REASON_SEPARATOR in text, text


# k carries no uniqueness rule in the contract above, on purpose, because most checks in
# this file are about the split rather than about keys. The two below need one.
KEYED = CONTRACT.replace("  - name: k\n    type: string\n    provenance: documented\n"
                         "    required: true\n",
                         "  - name: k\n    type: string\n    provenance: documented\n"
                         "    required: true\n    unique: true\n")


def keyed():
    return spec.parse(KEYED)


def check_the_report_separates_the_bystanders_from_the_bad_rows():
    """A held count on its own cannot tell one replayed job from a partition of bad data.

    The fixture has one row that is genuinely out of range and a pair sharing a key. Three
    rows held, one of them bad and two of them bystanders.
    """
    data = rows(("a", "1", "open"), ("a", "2", "open"), ("c", "99", "open"),
                ("d", "4", "open"), ("e", "5", "open"))
    v = validate.validate(keyed(), data)
    summary = quarantine.report(v, "2025-01-01")
    assert summary["held"] == 3, summary
    assert summary["held_only_by_a_key_collision"] == 2, summary
    assert summary["largest_key_collision"] == 2, summary


def check_the_report_says_one_when_no_key_collided():
    data = rows(("a", "1", "open"), ("b", "2", "open"), ("c", "99", "open"),
                ("d", "4", "open"), ("e", "5", "open"))
    summary = quarantine.report(validate.validate(keyed(), data), "2025-01-01")
    assert summary["held"] == 1, summary
    assert summary["held_only_by_a_key_collision"] == 0, summary
    assert summary["largest_key_collision"] == 1, summary
