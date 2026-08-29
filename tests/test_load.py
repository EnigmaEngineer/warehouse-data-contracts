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
