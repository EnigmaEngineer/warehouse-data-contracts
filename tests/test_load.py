"""Loading a judged partition into the raw layer.

Two fixtures with different row counts, on purpose. A reload check where both partitions
hold the same number of rows passes when the delete takes out the wrong one, because the
count comes back the same either way.

The second rule this file follows is that a check whose subject is "nothing changed" is
weak on its own. Every idempotency check here has a partner where the content really does
move, because otherwise it passes against a load that does nothing at all.
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
from warehouse import load, schema

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
  - name: zip
    type: string
    provenance: asserted
    matches: "^[0-9]{5}$"
  - name: n
    type: integer
    provenance: asserted
    min: 0
"""

# Four rows against two. The gap is what makes a wrong delete visible.
BIG = "day,zip,n\n2025-01-01,10001,1\n2025-01-01,00083,2\n2025-01-01,11201,3\n" \
      "2025-01-01,10453,4\n"
SMALL = "day,zip,n\n2025-01-02,10002,7\n2025-01-02,11202,8\n"
BIG_CHANGED = "day,zip,n\n2025-01-01,99999,1\n2025-01-01,00083,2\n" \
              "2025-01-01,11201,3\n2025-01-01,10453,4\n"

STAMP = datetime.datetime(2025, 6, 1, 12, 0, 0)


def contract():
    return spec.parse(CONTRACT)


def judged(root, name, body, partition, held=0, accepted=None):
    """A quarantine directory the shape the split writes one, plus its report."""
    out = os.path.join(root, name)
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, load.ACCEPTED_FILE), "w") as fh:
        fh.write(body)
    rows = len(body.strip().splitlines()) - 1
    with open(os.path.join(out, load.REPORT_FILE), "w") as fh:
        json.dump({"partition": partition, "rows": rows + held,
                   "accepted": rows if accepted is None else accepted,
                   "held": held}, fh)
    return out


def unjudged(root, name, body):
    """The accepted file with no verdict beside it. What data/raw looks like."""
    out = os.path.join(root, name)
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, load.ACCEPTED_FILE), "w") as fh:
        fh.write(body)
    return out


def opened(contract_):
    con = load.connect(":memory:")
    load.apply_schema(con, contract_)
    return con


def check_a_partition_lands_with_its_metadata_attached():
    c = contract()
    con = opened(c)
    with tempfile.TemporaryDirectory() as tmp:
        result = load.load_partition(
            con, c, "2025-01-01", judged(tmp, "a", BIG, "2025-01-01", held=2),
            "abc123", now=STAMP)
        assert result["rows_loaded"] == 4, result
        assert result["rows_replaced"] == 0, result
        assert result["rows_held"] == 2, result
        row = con.execute(
            "select {}, {}, {} from {} limit 1".format(
                schema.PARTITION_COLUMN, schema.SOURCE_COLUMN,
                schema.LOADED_COLUMN, schema.qualified(c))
        ).fetchone()
        assert row == ("2025-01-01", "abc123", STAMP), row
    con.close()


def check_a_directory_nobody_judged_is_refused():
    """The whole reason the loader takes a directory.

    Handed a bare file it would load `data/raw` complete with every row the contract
    refused, and the only thing stopping that would be each caller remembering.
    """
    c = contract()
    con = opened(c)
    with tempfile.TemporaryDirectory() as tmp:
        try:
            load.load_partition(con, c, "2025-01-01",
                                unjudged(tmp, "raw", BIG), "abc123", now=STAMP)
        except load.UnjudgedPartition as exc:
            assert load.REPORT_FILE in str(exc), str(exc)
        else:
            raise AssertionError("an unjudged directory was loaded")
        assert load.partition_rows(con, c, "2025-01-01") == 0
    con.close()


def check_a_report_about_another_partition_is_refused():
    c = contract()
    con = opened(c)
    with tempfile.TemporaryDirectory() as tmp:
        directory = judged(tmp, "a", BIG, "2025-01-01")
        try:
            load.load_partition(con, c, "2025-01-09", directory, "abc123", now=STAMP)
        except load.WrongPartition as exc:
            assert "2025-01-01" in str(exc) and "2025-01-09" in str(exc), str(exc)
        else:
            raise AssertionError("a report about another partition was accepted")
    con.close()


def check_a_report_with_no_accepted_file_beside_it_is_refused():
    c = contract()
    con = opened(c)
    with tempfile.TemporaryDirectory() as tmp:
        directory = judged(tmp, "a", BIG, "2025-01-01")
        os.remove(os.path.join(directory, load.ACCEPTED_FILE))
        try:
            load.load_partition(con, c, "2025-01-01", directory, "abc123", now=STAMP)
        except load.UnjudgedPartition as exc:
            assert load.ACCEPTED_FILE in str(exc), str(exc)
        else:
            raise AssertionError("a directory with no accepted file was loaded")
    con.close()


def check_a_leading_zero_zip_survives_the_load():
    """The reason every column in the raw layer is text.

    The contract says five digits and it validates the text that arrived. A reader left to
    guess turns this value into 83, which the same rule then refuses.
    """
    c = contract()
    con = opened(c)
    with tempfile.TemporaryDirectory() as tmp:
        load.load_partition(con, c, "2025-01-01",
                            judged(tmp, "a", BIG, "2025-01-01"), "abc123", now=STAMP)
        zips = [r[0] for r in con.execute(
            "select zip from {} order by n".format(schema.qualified(c))).fetchall()]
        assert zips == ["10001", "00083", "11201", "10453"], zips
    con.close()


def check_a_hive_shaped_directory_does_not_overwrite_the_column_it_names():
    """The check that was missing, and the reason it was missing is the directory name.

    Every fixture in this file used to live in a directory called `a` or `b`. The real
    quarantine lives in `created_date=2025-01-01`, and read_csv reads a directory of that
    shape as a column and lets it beat the file's own column of the same name. So the
    whole suite was green while the warehouse held the partition string in `day` on every
    row, times of day gone.

    An explicit columns= does not stop it. The path derived column overrides the declared
    type as well as the value.
    """
    c = contract()
    con = opened(c)
    with tempfile.TemporaryDirectory() as tmp:
        # Full timestamps rather than bare dates. A fixture holding '2025-01-01' in `day`
        # cannot show the loss, because the wrong answer and the right one are equal.
        body = ("day,zip,n\n"
                "2025-01-01T00:51:02.000,10001,1\n"
                "2025-01-01T09:14:00.000,00083,2\n"
                "2025-01-01T17:02:59.000,11201,3\n")
        directory = judged(tmp, "day=2025-01-01", body, "2025-01-01")
        load.load_partition(con, c, "2025-01-01", directory, "abc123", now=STAMP)
        days = [r[0] for r in con.execute(
            "select day from {} order by n".format(schema.qualified(c))).fetchall()]
        assert days == ["2025-01-01T00:51:02.000",
                        "2025-01-01T09:14:00.000",
                        "2025-01-01T17:02:59.000"], days
    con.close()


def check_the_content_check_catches_a_value_the_table_does_not_share_with_the_file():
    """verify_partition on its own, against a table that really disagrees.

    The load cannot produce a mismatch any more, so the value is changed by hand. A check
    that can only ever be reached through the fixed code path is a check nobody has run.
    """
    c = contract()
    con = opened(c)
    with tempfile.TemporaryDirectory() as tmp:
        directory = judged(tmp, "a", BIG, "2025-01-01")
        load.load_partition(con, c, "2025-01-01", directory, "abc123", now=STAMP)
        path = os.path.join(directory, load.ACCEPTED_FILE)

        count, problems = load.verify_partition(con, c, "2025-01-01", path)
        assert count == 0, problems

        con.execute(
            "update {} set zip = '99999' where n = '2'".format(schema.qualified(c)))
        count, problems = load.verify_partition(con, c, "2025-01-01", path)
        # Two, not one. The row holding 00083 left the table and a row holding 99999
        # arrived, and a symmetric difference counts both.
        assert count == 2, (count, problems)
        sides = sorted(p["where"] for p in problems)
        assert sides == ["file", "table"], problems
        rows = dict((p["where"], p["row"]) for p in problems)
        assert rows["file"][1] == "00083", problems
        assert rows["table"][1] == "99999", problems
    con.close()


def check_the_content_check_survives_a_contract_with_no_unique_column():
    """The fixture that killed the first version of this check.

    Every row in this partition carries the same value in the first contract column. A
    comparison keyed on that column collapses four rows into one and then reports six
    differences that do not exist. The real contract has a unique key, so nothing here
    would have shown it.
    """
    c = contract()
    con = opened(c)
    assert not any(col.rules.get("unique") for col in c.columns), \
        "this fixture is only meaningful on a contract with no unique column"
    with tempfile.TemporaryDirectory() as tmp:
        directory = judged(tmp, "a", BIG, "2025-01-01")
        load.load_partition(con, c, "2025-01-01", directory, "abc123", now=STAMP)
        path = os.path.join(directory, load.ACCEPTED_FILE)
        count, problems = load.verify_partition(con, c, "2025-01-01", path)
        assert count == 0, (count, problems)
    con.close()


def check_the_examples_are_capped_and_the_count_is_not():
    """Lopsided on purpose. Three rows rewritten, two examples asked for.

    A limit applied to the count rather than to the examples would report 2, and the
    refusal would understate itself on exactly the partitions where it matters most.
    """
    c = contract()
    con = opened(c)
    with tempfile.TemporaryDirectory() as tmp:
        directory = judged(tmp, "a", BIG, "2025-01-01")
        load.load_partition(con, c, "2025-01-01", directory, "abc123", now=STAMP)
        path = os.path.join(directory, load.ACCEPTED_FILE)
        table = schema.qualified(c)
        con.execute("update {} set zip = '9999' || n where n <> '4'".format(table))

        count, problems = load.verify_partition(con, c, "2025-01-01", path, limit=2)
        # Three rows changed, so six entries in the symmetric difference.
        assert count == 6, (count, problems)
        assert len(problems) == 2, problems
    con.close()


def check_the_default_example_cap_is_the_one_the_load_actually_gets():
    """load_partition calls verify_partition with no limit, so the default is production.

    Every other check here names the argument, which leaves the default untested and
    leaves it free to be anything. Four rows rewritten gives eight entries and the
    refusal message has to stay readable.
    """
    c = contract()
    con = opened(c)
    with tempfile.TemporaryDirectory() as tmp:
        directory = judged(tmp, "a", BIG, "2025-01-01")
        load.load_partition(con, c, "2025-01-01", directory, "abc123", now=STAMP)
        path = os.path.join(directory, load.ACCEPTED_FILE)
        con.execute("update {} set zip = '7777' || n".format(schema.qualified(c)))
        count, problems = load.verify_partition(con, c, "2025-01-01", path)
        assert count == 8, (count, problems)
        assert len(problems) == 5, problems
    con.close()


def check_a_row_in_the_table_that_is_not_in_the_file_at_all_is_counted():
    """The boundary nobody writes. One side longer than the other.

    A loop over the shorter of two lists reports nothing when a row appears from nowhere,
    which is exactly the shape of a load that read the wrong file.
    """
    c = contract()
    con = opened(c)
    with tempfile.TemporaryDirectory() as tmp:
        directory = judged(tmp, "a", BIG, "2025-01-01")
        load.load_partition(con, c, "2025-01-01", directory, "abc123", now=STAMP)
        path = os.path.join(directory, load.ACCEPTED_FILE)
        con.execute(
            "insert into {} values ('2025-01-01', '12345', '9', "
            "'2025-01-01', 'abc123', ?)".format(schema.qualified(c)), [STAMP])
        count, problems = load.verify_partition(con, c, "2025-01-01", path)
        assert count == 1, (count, problems)
        assert problems[0]["where"] == "table", problems
    con.close()


def check_a_row_missing_from_the_table_is_counted_too():
    """The mirror, and the half a one sided comparison misses.

    Only checking that the table's rows are in the file passes a load that dropped rows,
    because everything it did put in is genuine.
    """
    c = contract()
    con = opened(c)
    with tempfile.TemporaryDirectory() as tmp:
        directory = judged(tmp, "a", BIG, "2025-01-01")
        load.load_partition(con, c, "2025-01-01", directory, "abc123", now=STAMP)
        path = os.path.join(directory, load.ACCEPTED_FILE)
        con.execute(
            "delete from {} where n = '4'".format(schema.qualified(c)))
        count, problems = load.verify_partition(con, c, "2025-01-01", path)
        assert count == 1, (count, problems)
        assert problems[0]["where"] == "file", problems
    con.close()


def check_a_duplicated_row_is_not_hidden_by_the_multiset():
    """A set would swallow this. Two identical rows against one is a difference of one.

    The boundary that matters for a comparison built on Counter rather than on set.
    """
    c = contract()
    con = opened(c)
    with tempfile.TemporaryDirectory() as tmp:
        directory = judged(tmp, "a", BIG, "2025-01-01")
        load.load_partition(con, c, "2025-01-01", directory, "abc123", now=STAMP)
        path = os.path.join(directory, load.ACCEPTED_FILE)
        con.execute(
            "insert into {} values ('2025-01-01', '10001', '1', "
            "'2025-01-01', 'abc123', ?)".format(schema.qualified(c)), [STAMP])
        count, problems = load.verify_partition(con, c, "2025-01-01", path)
        assert count == 1, (count, problems)
        assert problems[0]["where"] == "table", problems
    con.close()


def check_an_empty_field_in_the_file_matches_a_null_in_the_table():
    """CSV cannot tell an empty string from a missing value and the load writes null.

    Without this the cell check would report every nullable empty field as a mismatch,
    which is a check that fires on every partition and therefore fires on none.
    """
    c = contract()
    con = opened(c)
    with tempfile.TemporaryDirectory() as tmp:
        body = "day,zip,n\n2025-01-01,,1\n2025-01-01,10001,2\n"
        directory = judged(tmp, "a", body, "2025-01-01")
        load.load_partition(con, c, "2025-01-01", directory, "abc123", now=STAMP)
        path = os.path.join(directory, load.ACCEPTED_FILE)
        count, problems = load.verify_partition(con, c, "2025-01-01", path)
        assert count == 0, problems
        stored = con.execute(
            "select zip from {} order by n".format(schema.qualified(c))).fetchall()
        assert stored == [(None,), ("10001",)], stored
    con.close()


def check_the_load_reports_how_many_cells_it_compared():
    """A check that reports nothing about what it checked can pass on nothing.

    Four rows and three columns is twelve, and the number is in the result rather than
    only in an assertion, because the driver prints it.
    """
    c = contract()
    con = opened(c)
    with tempfile.TemporaryDirectory() as tmp:
        result = load.load_partition(
            con, c, "2025-01-01", judged(tmp, "a", BIG, "2025-01-01"),
            "abc123", now=STAMP)
        assert result["cells_checked"] == 12, result
    con.close()


def check_loading_the_same_partition_twice_replaces_it():
    c = contract()
    con = opened(c)
    with tempfile.TemporaryDirectory() as tmp:
        directory = judged(tmp, "a", BIG, "2025-01-01")
        load.load_partition(con, c, "2025-01-01", directory, "abc123", now=STAMP)
        first = load.fingerprint(con, c)
        again = load.load_partition(con, c, "2025-01-01", directory, "abc123",
                                    now=STAMP + datetime.timedelta(days=1))
        assert again["rows_replaced"] == 4, again
        assert load.partition_rows(con, c, "2025-01-01") == 4
        assert load.fingerprint(con, c) == first
    con.close()


def check_a_reload_from_changed_data_really_does_change_the_table():
    """The partner to the check above.

    A load that quietly did nothing would pass the idempotency check perfectly.
    """
    c = contract()
    con = opened(c)
    with tempfile.TemporaryDirectory() as tmp:
        load.load_partition(con, c, "2025-01-01",
                            judged(tmp, "a", BIG, "2025-01-01"), "abc123", now=STAMP)
        before = load.fingerprint(con, c)
        load.load_partition(con, c, "2025-01-01",
                            judged(tmp, "b", BIG_CHANGED, "2025-01-01"), "def456",
                            now=STAMP)
        assert load.fingerprint(con, c) != before
        zips = set(r[0] for r in con.execute(
            "select zip from {}".format(schema.qualified(c))).fetchall())
        assert "99999" in zips and "10001" not in zips, zips
    con.close()


def check_reloading_one_partition_leaves_the_other_alone():
    c = contract()
    con = opened(c)
    with tempfile.TemporaryDirectory() as tmp:
        big = judged(tmp, "a", BIG, "2025-01-01")
        small = judged(tmp, "b", SMALL, "2025-01-02")
        load.load_partition(con, c, "2025-01-01", big, "abc123", now=STAMP)
        load.load_partition(con, c, "2025-01-02", small, "def456", now=STAMP)
        load.load_partition(con, c, "2025-01-01", big, "abc123", now=STAMP)
        assert load.partition_counts(con, c) == {"2025-01-01": 4, "2025-01-02": 2}
    con.close()


def check_the_fingerprint_ignores_when_the_rows_arrived():
    c = contract()
    con = opened(c)
    with tempfile.TemporaryDirectory() as tmp:
        directory = judged(tmp, "a", BIG, "2025-01-01")
        load.load_partition(con, c, "2025-01-01", directory, "abc123", now=STAMP)
        early = load.fingerprint(con, c)
        load.load_partition(con, c, "2025-01-01", directory, "abc123",
                            now=STAMP + datetime.timedelta(days=400))
        assert load.fingerprint(con, c) == early
    con.close()


def check_the_fingerprint_does_not_ignore_where_the_rows_came_from():
    """`_source_sha256` is in the fingerprint and `_loaded_at` is not.

    Without this, a fingerprint that ignored everything would pass the check above.
    """
    c = contract()
    con = opened(c)
    with tempfile.TemporaryDirectory() as tmp:
        directory = judged(tmp, "a", BIG, "2025-01-01")
        load.load_partition(con, c, "2025-01-01", directory, "abc123", now=STAMP)
        first = load.fingerprint(con, c)
        load.load_partition(con, c, "2025-01-01", directory, "a-different-file",
                            now=STAMP)
        assert load.fingerprint(con, c) != first
    con.close()


def check_the_fingerprint_keeps_its_published_shape():
    """A count, a colon and twelve hex characters.

    The length is pinned because a fingerprint is only useful against an older one. Change
    the width and every figure ever published under it becomes uncomparable, silently.
    """
    c = contract()
    con = opened(c)
    with tempfile.TemporaryDirectory() as tmp:
        load.load_partition(con, c, "2025-01-01",
                            judged(tmp, "a", BIG, "2025-01-01"), "abc123", now=STAMP)
        count, digest = load.fingerprint(con, c).split(":")
        assert count == "4", count
        assert len(digest) == 12, digest
        assert all(ch in "0123456789abcdef" for ch in digest), digest
    con.close()


def check_a_header_that_is_not_the_contract_is_refused_by_name():
    c = contract()
    con = opened(c)
    with tempfile.TemporaryDirectory() as tmp:
        directory = judged(tmp, "a", "day,postcode,n\n2025-01-01,10001,1\n",
                           "2025-01-01")
        try:
            load.load_partition(con, c, "2025-01-01", directory, "abc123", now=STAMP)
        except load.HeaderMismatch as exc:
            assert "postcode" in str(exc), str(exc)
        else:
            raise AssertionError("a file with the wrong columns was loaded")
    con.close()


def check_a_reordered_header_is_refused_too():
    """Column order, not just column names.

    read_csv is told the columns by name, so a reordered file would load correctly. The
    check is still worth having for the day a column is renamed into another one's place.
    """
    c = contract()
    con = opened(c)
    with tempfile.TemporaryDirectory() as tmp:
        directory = judged(tmp, "a", "zip,day,n\n10001,2025-01-01,1\n", "2025-01-01")
        try:
            load.load_partition(con, c, "2025-01-01", directory, "abc123", now=STAMP)
        except load.HeaderMismatch:
            pass
        else:
            raise AssertionError("a reordered header was accepted")
    con.close()


def check_a_count_the_report_disagrees_with_is_refused():
    c = contract()
    con = opened(c)
    with tempfile.TemporaryDirectory() as tmp:
        directory = judged(tmp, "a", BIG, "2025-01-01", accepted=99)
        try:
            load.load_partition(con, c, "2025-01-01", directory, "abc123", now=STAMP)
        except load.LoadCountMismatch as exc:
            assert "99" in str(exc) and "4" in str(exc), str(exc)
        else:
            raise AssertionError("a load disagreeing with its report was accepted")
    con.close()


def check_a_count_mismatch_rolls_the_partition_back():
    """The refusal above used to happen after the commit.

    So the rows were in the table and the ledger while the caller was handling an
    exception saying the load had failed. It is only visible from outside when something
    asks what the table holds afterwards, which is what a backfill does when it reports
    where it stopped. Reverting the check to its old place outside the try block leaves
    the check above green and fails this one.
    """
    c = contract()
    con = opened(c)
    with tempfile.TemporaryDirectory() as tmp:
        directory = judged(tmp, "a", BIG, "2025-01-01", accepted=99)
        try:
            load.load_partition(con, c, "2025-01-01", directory, "abc123", now=STAMP)
        except load.LoadCountMismatch:
            pass
        else:
            raise AssertionError("a load disagreeing with its report was accepted")
        assert load.partition_rows(con, c, "2025-01-01") == 0, "rows survived the refusal"
        ledger = con.execute("select count(*) from {}.{}".format(
            schema.RAW_SCHEMA, schema.LEDGER_TABLE)).fetchone()[0]
        assert ledger == 0, "the ledger recorded a load that was refused"
    con.close()


def check_a_report_saying_nothing_was_held_records_nothing_held():
    c = contract()
    con = opened(c)
    with tempfile.TemporaryDirectory() as tmp:
        result = load.load_partition(con, c, "2025-01-01",
                                     judged(tmp, "a", BIG, "2025-01-01"), "abc123",
                                     now=STAMP)
        assert result["rows_held"] == 0, result
        held = con.execute("select rows_held from {}.{}".format(
            schema.RAW_SCHEMA, schema.LEDGER_TABLE)).fetchone()[0]
        assert held == 0, held
    con.close()


def check_a_load_that_dies_leaves_the_partition_as_it_was():
    c = contract()
    con = opened(c)
    with tempfile.TemporaryDirectory() as tmp:
        load.load_partition(con, c, "2025-01-01",
                            judged(tmp, "a", BIG, "2025-01-01"), "abc123", now=STAMP)
        before = load.fingerprint(con, c)
        # A good header and a row carrying two fields too many. The header check reads it
        # happily and read_csv refuses it, so the failure lands inside the transaction,
        # which is the only place the rollback is worth anything. A missing file would
        # have raised before the delete and this check would pass having tested nothing.
        directory = judged(tmp, "b", "day,zip,n\n2025-01-01,10001,1,EXTRA,MORE\n",
                           "2025-01-01", accepted=4)
        assert load.header_of(
            os.path.join(directory, load.ACCEPTED_FILE)) == ["day", "zip", "n"]
        try:
            load.load_partition(con, c, "2025-01-01", directory, "abc123", now=STAMP)
        except Exception as exc:
            assert not isinstance(exc, (load.HeaderMismatch, load.UnjudgedPartition)), exc
        else:
            raise AssertionError("a load of an unreadable file succeeded")
        assert load.partition_rows(con, c, "2025-01-01") == 4
        assert load.fingerprint(con, c) == before
    con.close()


def check_the_ledger_agrees_with_the_table_after_a_normal_load():
    c = contract()
    con = opened(c)
    with tempfile.TemporaryDirectory() as tmp:
        load.load_partition(con, c, "2025-01-01",
                            judged(tmp, "a", BIG, "2025-01-01"), "abc123", now=STAMP)
        load.load_partition(con, c, "2025-01-02",
                            judged(tmp, "b", SMALL, "2025-01-02"), "def456", now=STAMP)
        assert load.reconcile(con, c) == []
    con.close()


def check_reconcile_finds_rows_written_around_the_ledger():
    """The arm that can fail. A reconciliation nothing has ever broken is decoration."""
    c = contract()
    con = opened(c)
    with tempfile.TemporaryDirectory() as tmp:
        load.load_partition(con, c, "2025-01-01",
                            judged(tmp, "a", BIG, "2025-01-01"), "abc123", now=STAMP)
        con.execute(
            "insert into {} values ('2025-01-01', '10001', '9', '2025-01-01', "
            "'abc123', ?)".format(schema.qualified(c)), [STAMP])
        drift = load.reconcile(con, c)
        assert drift == [{"partition": "2025-01-01", "in_table": 5, "in_ledger": 4}], drift
    con.close()


def check_reconcile_finds_a_ledger_entry_with_no_rows_behind_it():
    c = contract()
    con = opened(c)
    with tempfile.TemporaryDirectory() as tmp:
        load.load_partition(con, c, "2025-01-01",
                            judged(tmp, "a", BIG, "2025-01-01"), "abc123", now=STAMP)
        con.execute(
            "insert into {}.{} values ('2025-01-09', 'widgets', 11, 0, 'zzz', ?)".format(
                schema.RAW_SCHEMA, schema.LEDGER_TABLE), [STAMP])
        drift = load.reconcile(con, c)
        assert drift == [{"partition": "2025-01-09", "in_table": 0, "in_ledger": 11}], drift
    con.close()


def check_partition_rows_is_zero_for_a_partition_nobody_loaded():
    c = contract()
    con = opened(c)
    assert load.partition_rows(con, c, "1999-01-01") == 0
    assert load.partition_counts(con, c) == {}
    con.close()
