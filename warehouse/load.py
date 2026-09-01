"""Load one judged partition into the raw layer, and make a reload cost nothing.

The load takes a quarantine directory rather than a CSV path, and that is the whole design
of this module. A function taking a file would happily be handed the raw partition, all
13,049 rows of it including the ones the contract refused, and the only thing standing
between that and the warehouse would be every caller remembering not to. A directory
carrying no `report.json` is refused, and `data/raw` has none.

So the claim that nothing reaches the warehouse unjudged is a fact about the code rather
than a fact about how the DAG happens to be wired today.

Idempotency is delete then insert on the partition key, inside one transaction. Appending
would double a partition on a rerun, and a warehouse where a backfill is dangerous is a
warehouse nobody backfills. The delete runs whether or not the partition is there, so the
first load and the tenth are the same statement.

The load refuses four ways. The header has to be the contract's columns. The report has to
be about the partition being asked for. The row count in the table afterwards has to match
the count the report claimed was accepted. And the rows in the table have to be the rows
that were in the file. All four refuse before the commit, and the count check did not until
a backfill asked what a failure halfway through a range leaves behind.

That last check is the one that earned its place. The first three are all counts and a
count cannot see a value being rewritten. `read_csv` reads a hive style directory name as
a column and lets it win over the file's own column of the same name. So a partition
living under `created_date=2025-01-01` came back with `created_date` equal to `2025-01-01`
on every row, times of day gone. An explicit `columns=` did not stop it. Row counts
matched and the ledger reconciled, and a reload produced a byte identical fingerprint
because both loads were wrong the same way.

So `verify_partition` reads the file with the standard library and the table with SQL and
compares them. Two readers, one comparison. It costs a second pass over the file and it is
the only check here that could have caught that.
"""

import datetime
import hashlib
import json
import os

from warehouse import schema

ACCEPTED_FILE = "accepted.csv"
REPORT_FILE = "report.json"


class HeaderMismatch(ValueError):
    """The file's columns are not the contract's columns."""


class UnjudgedPartition(ValueError):
    """The directory holds no report, so nothing has decided what is in it."""


class WrongPartition(ValueError):
    """The report in this directory is about a different partition."""


class LoadCountMismatch(RuntimeError):
    """The table disagrees with the report about how many rows were accepted."""


class ContentMismatch(RuntimeError):
    """The table holds a different value from the one in the file."""


def connect(path):
    import duckdb

    return duckdb.connect(path)


def apply_schema(con, contract, dialect="duckdb"):
    for sql in schema.statements(contract, dialect):
        con.execute(sql)


def _quote(name):
    return '"' + name.replace('"', '""') + '"'


def _columns_argument(contract):
    """The literal `columns={...}` read_csv takes, every column typed as text.

    Built here rather than passed as a parameter because read_csv wants a struct literal.
    """
    parts = []
    for name, type_ in schema.read_columns(contract).items():
        parts.append("'{}': '{}'".format(name.replace("'", "''"), type_))
    return "{" + ", ".join(parts) + "}"


def header_of(path):
    import csv

    with open(path, newline="") as fh:
        return csv.reader(fh).__next__()


def check_header(contract, header):
    expected = schema.source_columns(contract)
    if list(header) != expected:
        raise HeaderMismatch(
            "file header {} is not the contract's columns {}".format(
                list(header), expected
            )
        )


def read_verdict(directory, partition):
    """The quarantine's own report, or a refusal. Returns (accepted_path, report)."""
    report_path = os.path.join(directory, REPORT_FILE)
    if not os.path.exists(report_path):
        raise UnjudgedPartition(
            "{} holds no {}, so nothing has judged what is in it".format(
                directory, REPORT_FILE)
        )
    with open(report_path) as fh:
        report = json.load(fh)
    if report.get("partition") != partition:
        raise WrongPartition(
            "{} reports on partition {}, the load was asked for {}".format(
                directory, report.get("partition"), partition)
        )
    accepted = os.path.join(directory, ACCEPTED_FILE)
    if not os.path.exists(accepted):
        raise UnjudgedPartition(
            "{} has a report and no {}".format(directory, ACCEPTED_FILE)
        )
    return accepted, report


def _file_rows(contract, path):
    """The file's rows as tuples, read with the standard library rather than with read_csv.

    That is the whole point. One side of the comparison has to come from something other
    than the reader whose behaviour is in question, or both sides are wrong together.

    An empty CSV field becomes None, matching what the load writes into the table, because
    CSV cannot tell an empty string from a missing value and the table has had to choose.
    """
    import csv

    columns = schema.source_columns(contract)
    out = []
    with open(path, newline="") as fh:
        for row in csv.DictReader(fh):
            out.append(tuple(
                (row[c] if row[c] != "" else None) for c in columns))
    return out


def verify_partition(con, contract, partition, path, limit=5):
    """Compare the loaded partition against the file it came from. Returns (count, examples).

    No key. A contract is not required to carry a unique column, and keying on whichever
    column happens to be first is an assumption that is silently wrong on a contract that
    does not have one. The first version of this did exactly that, and on a fixture whose
    rows all shared a first column it collapsed four rows into one and reported six
    differences that were not there.

    So this is a multiset comparison and `count` is the size of the symmetric difference.
    One rewritten value counts twice, once for the row that is no longer in the table and
    once for the row that appeared. A row dropped counts once. Order does not enter into
    it, which matters because the table's order is whatever the reader produced.

    `limit` caps the examples. It does not cap the count.
    """
    from collections import Counter

    columns = schema.source_columns(contract)
    from_file = Counter(_file_rows(contract, path))

    selected = ", ".join(_quote(c) for c in columns)
    from_table = Counter(con.execute(
        "select {} from {} where {} = ?".format(
            selected, schema.qualified(contract), _quote(schema.PARTITION_COLUMN)),
        [partition],
    ).fetchall())

    only_table = from_table - from_file
    only_file = from_file - from_table

    examples = []
    for row in sorted(only_table.elements(), key=repr)[:limit]:
        examples.append({"where": "table", "row": row})
    for row in sorted(only_file.elements(), key=repr)[:limit]:
        examples.append({"where": "file", "row": row})

    count = sum(only_table.values()) + sum(only_file.values())
    return count, examples[:limit]


def load_partition(con, contract, partition, directory, source_sha256, now=None):
    """Replace one partition in the raw table from a judged directory. Returns a summary.

    `directory` is a quarantine directory, not a CSV. The counts come out of the report
    sitting beside the data rather than from the caller, so there is no argument a caller
    can get wrong and no default it can leave at zero.
    """
    path, report = read_verdict(directory, partition)
    rows_held = report["held"]
    expected_rows = report["accepted"]

    check_header(contract, header_of(path))

    table = schema.qualified(contract)
    ledger = "{}.{}".format(schema.RAW_SCHEMA, schema.LEDGER_TABLE)
    columns = schema.source_columns(contract)
    selected = ", ".join(_quote(c) for c in columns)
    target = selected + ", {}, {}, {}".format(
        _quote(schema.PARTITION_COLUMN),
        _quote(schema.SOURCE_COLUMN),
        _quote(schema.LOADED_COLUMN),
    )
    stamp = now if now is not None else datetime.datetime.now()

    before = partition_rows(con, contract, partition)

    con.execute("begin transaction")
    try:
        con.execute(
            "delete from {} where {} = ?".format(
                table, _quote(schema.PARTITION_COLUMN)),
            [partition],
        )
        con.execute(
            "delete from {} where {} = ?".format(
                ledger, _quote(schema.PARTITION_COLUMN)),
            [partition],
        )
        con.execute(
            # hive_partitioning=false is load bearing. The accepted file lives under
            # created_date=2025-01-01, read_csv reads that directory name as a column, and
            # a column read off the path beats the file's own column of the same name.
            # It also beats columns=, so the explicit type is ignored for that column too.
            "insert into {} ({}) select {}, ?, ?, ? from read_csv(?, header=true, "
            "hive_partitioning=false, columns={})".format(
                table, target, selected, _columns_argument(contract)),
            [partition, source_sha256, stamp, path],
        )
        loaded = partition_rows(con, contract, partition)
        # Inside the transaction, and it was outside it until a backfill went looking for
        # what a mid range failure leaves behind. A check that raises after the commit is
        # not one of the ways this function refuses, it is a complaint about a partition
        # that is already in the table. The other three were always inside.
        if loaded != expected_rows:
            raise LoadCountMismatch(
                "{} loaded {} rows, the report said {} were accepted".format(
                    partition, loaded, expected_rows
                )
            )
        con.execute(
            "insert into {} values (?, ?, ?, ?, ?, ?)".format(ledger),
            [partition, contract.dataset, loaded, rows_held, source_sha256, stamp],
        )
        differing, examples = verify_partition(con, contract, partition, path)
        if differing:
            raise ContentMismatch(
                "{}: {} rows in the table are not the rows in the file, first {}".format(
                    partition, differing, examples)
            )
        con.execute("commit")
    except Exception:
        con.execute("rollback")
        raise

    return {
        "partition": partition,
        "rows_loaded": loaded,
        "rows_replaced": before,
        "rows_held": rows_held,
        "cells_checked": loaded * len(columns),
        "source_sha256": source_sha256,
    }


def partition_rows(con, contract, partition):
    row = con.execute(
        "select count(*) from {} where {} = ?".format(
            schema.qualified(contract), _quote(schema.PARTITION_COLUMN)),
        [partition],
    ).fetchone()
    return row[0]


def partition_counts(con, contract):
    rows = con.execute(
        "select {0}, count(*) from {1} group by {0} order by {0}".format(
            _quote(schema.PARTITION_COLUMN), schema.qualified(contract))
    ).fetchall()
    return dict(rows)


def reconcile(con, contract):
    """Every partition where the ledger and the table disagree about the row count.

    They are written in one transaction so they should never differ. A check that cannot
    fail today is still the check that catches the day someone loads around the ledger.
    """
    table = schema.qualified(contract)
    ledger = "{}.{}".format(schema.RAW_SCHEMA, schema.LEDGER_TABLE)
    part = _quote(schema.PARTITION_COLUMN)
    rows = con.execute(
        "with t as (select {0} p, count(*) n from {1} group by 1), "
        "l as (select {0} p, sum(rows_loaded) n from {2} group by 1) "
        "select coalesce(t.p, l.p), coalesce(t.n, 0), coalesce(l.n, 0) "
        "from t full outer join l on t.p = l.p "
        "where coalesce(t.n, 0) <> coalesce(l.n, 0) order by 1".format(
            part, table, ledger)
    ).fetchall()
    return [{"partition": p, "in_table": t, "in_ledger": l} for p, t, l in rows]


def fingerprint(con, contract):
    """A hash of the table's content, ignoring when it was loaded.

    `_loaded_at` is wall clock and moves on every run, so a fingerprint that included it
    could never say two loads produced the same table. Everything else is in.
    """
    columns = schema.source_columns(contract) + [
        schema.PARTITION_COLUMN, schema.SOURCE_COLUMN]
    selected = ", ".join("coalesce({}, '')".format(_quote(c)) for c in columns)
    rows = con.execute(
        "select {} from {} order by all".format(selected, schema.qualified(contract))
    ).fetchall()
    digest = hashlib.sha256()
    for row in rows:
        digest.update(("\x1f".join(row) + "\x1e").encode("utf-8"))
    return "{}:{}".format(len(rows), digest.hexdigest()[:12])
