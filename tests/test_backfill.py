"""Backfilling a range.

The fixtures give the three partitions different row counts. A range check where every
partition holds the same number passes when the loop loads one of them twice, because the
total comes back the same either way.
"""

import datetime
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from contracts import spec
from warehouse import backfill, load, schema

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
  - name: n
    type: integer
    provenance: asserted
    min: 0
"""

BODIES = {
    "2025-03-01": "day,n\n2025-03-01,1\n",
    "2025-03-02": "day,n\n2025-03-02,1\n2025-03-02,2\n",
    "2025-03-03": "day,n\n2025-03-03,1\n2025-03-03,2\n2025-03-03,3\n",
}

STAMP = datetime.datetime(2025, 6, 1, 12, 0, 0)


def contract():
    return spec.parse(CONTRACT)


def judged(root, partition, body=None, accepted=None):
    out = os.path.join(root, "created_date=" + partition)
    os.makedirs(out, exist_ok=True)
    body = BODIES[partition] if body is None else body
    with open(os.path.join(out, load.ACCEPTED_FILE), "w") as fh:
        fh.write(body)
    rows = len(body.strip().splitlines()) - 1
    with open(os.path.join(out, load.REPORT_FILE), "w") as fh:
        json.dump({"partition": partition, "rows": rows,
                   "accepted": rows if accepted is None else accepted,
                   "held": 0}, fh)
    return out


def three(root):
    return dict((p, judged(root, p)) for p in sorted(BODIES))


def opened(c):
    con = load.connect(":memory:")
    load.apply_schema(con, c)
    return con


def sha_for(partition):
    return "sha-" + partition


def check_a_range_expands_to_every_day_in_it():
    assert backfill.days("2025-03-01", "2025-03-03") == [
        "2025-03-01", "2025-03-02", "2025-03-03"]


def check_a_single_day_range_is_one_day():
    assert backfill.days("2025-03-01", "2025-03-01") == ["2025-03-01"]


def check_a_reversed_range_is_refused():
    try:
        backfill.days("2025-03-03", "2025-03-01")
    except ValueError as exc:
        assert "before" in str(exc), str(exc)
    else:
        raise AssertionError("a range running backwards was accepted")


def check_the_plan_follows_the_range_and_not_the_directory_listing():
    with tempfile.TemporaryDirectory() as tmp:
        have = three(tmp)
        work = backfill.plan(have, "2025-03-01", "2025-03-02")
        assert [p for p, _ in work] == ["2025-03-01", "2025-03-02"], work


def check_a_gap_in_the_range_is_refused_by_name():
    with tempfile.TemporaryDirectory() as tmp:
        have = three(tmp)
        del have["2025-03-02"]
        try:
            backfill.plan(have, "2025-03-01", "2025-03-03")
        except backfill.MissingPartitions as exc:
            assert exc.missing == ["2025-03-02"], exc.missing
        else:
            raise AssertionError("a range with a hole in it produced a plan")


def check_every_missing_day_is_named_and_not_just_the_first():
    with tempfile.TemporaryDirectory() as tmp:
        have = three(tmp)
        del have["2025-03-01"]
        del have["2025-03-03"]
        try:
            backfill.plan(have, "2025-03-01", "2025-03-03")
        except backfill.MissingPartitions as exc:
            assert exc.missing == ["2025-03-01", "2025-03-03"], exc.missing
        else:
            raise AssertionError("two missing days produced a plan")


def check_the_plan_is_ordered_by_date_whatever_the_mapping_order():
    with tempfile.TemporaryDirectory() as tmp:
        have = three(tmp)
        shuffled = dict(reversed(list(have.items())))
        work = backfill.plan(shuffled, "2025-03-01", "2025-03-03")
        assert [p for p, _ in work] == sorted(BODIES), work


def check_a_range_loads_every_partition_once():
    c = contract()
    con = opened(c)
    with tempfile.TemporaryDirectory() as tmp:
        work = backfill.plan(three(tmp), "2025-03-01", "2025-03-03")
        summary = backfill.run(con, c, work, sha_for, now=STAMP)
    assert summary["rows_loaded"] == 6, summary
    assert summary["rows_replaced"] == 0, summary
    assert load.partition_counts(con, c) == {
        "2025-03-01": 1, "2025-03-02": 2, "2025-03-03": 3}
    con.close()


def check_the_same_range_twice_replaces_rather_than_appends():
    c = contract()
    con = opened(c)
    with tempfile.TemporaryDirectory() as tmp:
        work = backfill.plan(three(tmp), "2025-03-01", "2025-03-03")
        backfill.run(con, c, work, sha_for, now=STAMP)
        first = load.fingerprint(con, c)
        again = backfill.run(con, c, work, sha_for, now=STAMP)
    assert again["rows_replaced"] == 6, again
    assert load.fingerprint(con, c) == first
    con.close()


def check_a_rerun_over_changed_data_really_does_move_the_table():
    """The partner to the check above. Without it that one passes against a load that
    silently does nothing on a second pass."""
    c = contract()
    con = opened(c)
    with tempfile.TemporaryDirectory() as tmp:
        have = three(tmp)
        work = backfill.plan(have, "2025-03-01", "2025-03-01")
        backfill.run(con, c, work, sha_for, now=STAMP)
        first = load.fingerprint(con, c)
        judged(tmp, "2025-03-01", body="day,n\n2025-03-01,99\n")
        backfill.run(con, c, work, sha_for, now=STAMP)
    assert load.fingerprint(con, c) != first
    con.close()


def check_a_failure_names_the_partition_and_what_landed_before_it():
    c = contract()
    con = opened(c)
    with tempfile.TemporaryDirectory() as tmp:
        have = three(tmp)
        judged(tmp, "2025-03-02", accepted=99)
        work = backfill.plan(have, "2025-03-01", "2025-03-03")
        try:
            backfill.run(con, c, work, sha_for, now=STAMP)
        except backfill.BackfillStopped as stopped:
            assert stopped.partition == "2025-03-02", stopped.partition
            assert stopped.completed == ["2025-03-01"], stopped.completed
            assert isinstance(stopped.cause, load.LoadCountMismatch), stopped.cause
        else:
            raise AssertionError("a broken partition did not stop the range")
    # Everything before it committed and everything from it did not. That is the shape
    # this loop has and the message is the only thing that makes it recoverable.
    assert load.partition_counts(con, c) == {"2025-03-01": 1}
    con.close()


def check_the_stopped_message_says_none_when_the_first_partition_fails():
    c = contract()
    con = opened(c)
    with tempfile.TemporaryDirectory() as tmp:
        have = three(tmp)
        judged(tmp, "2025-03-01", accepted=99)
        work = backfill.plan(have, "2025-03-01", "2025-03-02")
        try:
            backfill.run(con, c, work, sha_for, now=STAMP)
        except backfill.BackfillStopped as stopped:
            assert stopped.completed == [], stopped.completed
            assert "none" in str(stopped), str(stopped)
        else:
            raise AssertionError("a broken first partition did not stop the range")
    con.close()


def check_orphans_finds_a_partition_the_source_no_longer_has():
    c = contract()
    con = opened(c)
    with tempfile.TemporaryDirectory() as tmp:
        have = three(tmp)
        backfill.run(con, c, backfill.plan(have, "2025-03-01", "2025-03-03"),
                     sha_for, now=STAMP)
        del have["2025-03-03"]
        backfill.run(con, c, backfill.plan(have, "2025-03-01", "2025-03-02"),
                     sha_for, now=STAMP)
        assert backfill.orphans(con, c, list(have)) == ["2025-03-03"]
    # And the point of the check. Nothing else notices.
    assert load.reconcile(con, c) == []
    con.close()


def check_orphans_is_empty_when_the_source_still_has_everything():
    c = contract()
    con = opened(c)
    with tempfile.TemporaryDirectory() as tmp:
        have = three(tmp)
        backfill.run(con, c, backfill.plan(have, "2025-03-01", "2025-03-03"),
                     sha_for, now=STAMP)
        assert backfill.orphans(con, c, list(have)) == []
    con.close()


def check_orphans_reads_the_table_and_not_the_ledger():
    """A row written around the load would be an orphan too, and the ledger would not
    know about it. This is the case that separates the two sources."""
    c = contract()
    con = opened(c)
    con.execute("insert into {} values ('2025-04-01', 5, '2025-04-01', 'x', ?)".format(
        schema.qualified(c)), [STAMP])
    assert backfill.orphans(con, c, ["2025-03-01"]) == ["2025-04-01"]
    con.close()


def check_the_ledger_carries_one_row_per_partition_after_a_rerun():
    c = contract()
    con = opened(c)
    with tempfile.TemporaryDirectory() as tmp:
        work = backfill.plan(three(tmp), "2025-03-01", "2025-03-03")
        backfill.run(con, c, work, sha_for, now=STAMP)
        backfill.run(con, c, work, sha_for, now=STAMP)
    rows = backfill.ledger_rows(con)
    assert len(rows) == 3, rows
    assert [r["partition"] for r in rows] == sorted(BODIES), rows
    assert [r["rows_loaded"] for r in rows] == [1, 2, 3], rows
    con.close()
