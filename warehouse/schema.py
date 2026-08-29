"""The raw table, its columns, and the types it refuses to guess.

Everything in the raw layer is text. That is a decision and not laziness.

A CSV reader that infers types is deciding the schema from the bytes it happened to look
at. duckdb reads `incident_zip` as BIGINT on eleven of the fourteen partitions here and as
VARCHAR on the other three, because those three each hold one zip with a leading zero and a
leading zero disqualifies an integer. So the type of a column in the warehouse would depend
on which day got loaded first. Three rows out of 179,314 deciding a schema is not a schema.

Worse, the contract says `incident_zip` matches five digits and it validates the text that
arrived. `cast('00083' as bigint)` is 83. Once the load has typed the column the same rule
reads the same value two ways, and the ingestion check and the warehouse test disagree
without either of them being wrong.

So the raw layer stores what arrived and the casting happens downstream where it can be
argued about. `columns=` is passed to `read_csv` explicitly for the same reason. An
inference this code does not ask for is an inference nobody reviewed.
"""

RAW_SCHEMA = "raw"
LEDGER_TABLE = "load_ledger"

# Every row carries where it came from. `_partition` is the delete key that makes a reload
# idempotent, and it exists as its own column because `created_date` is a timestamp inside
# the day rather than the day itself.
PARTITION_COLUMN = "_partition"
SOURCE_COLUMN = "_source_sha256"
LOADED_COLUMN = "_loaded_at"
METADATA_COLUMNS = (PARTITION_COLUMN, SOURCE_COLUMN, LOADED_COLUMN)

DIALECTS = ("duckdb", "snowflake")

# Text in both. VARCHAR is unbounded in duckdb and 16 MB in Snowflake, and neither needs a
# length here.
_TEXT = {"duckdb": "VARCHAR", "snowflake": "VARCHAR"}
_TIMESTAMP = {"duckdb": "TIMESTAMP", "snowflake": "TIMESTAMP_NTZ"}


class MetadataColumnClash(ValueError):
    """A contract column is named like one of the columns the load adds."""


class UnknownDialect(ValueError):
    pass


def qualified(contract):
    return "{}.{}".format(RAW_SCHEMA, contract.dataset)


def source_columns(contract):
    return [c.name for c in contract.columns]


def _check_clash(contract):
    clashing = sorted(set(source_columns(contract)) & set(METADATA_COLUMNS))
    if clashing:
        raise MetadataColumnClash(
            "contract columns {} collide with the columns the load adds".format(clashing)
        )


def read_columns(contract):
    """The `columns=` argument for read_csv, every source column as text.

    Passing this is the whole point. Without it duckdb sniffs, and what it sniffs depends
    on the partition.
    """
    _check_clash(contract)
    return dict((name, "VARCHAR") for name in source_columns(contract))


def create_table(contract, dialect="duckdb"):
    if dialect not in DIALECTS:
        raise UnknownDialect("no such dialect: {}".format(dialect))
    _check_clash(contract)

    lines = ["  {} {}".format(name, _TEXT[dialect]) for name in source_columns(contract)]
    lines.append("  {} {}".format(PARTITION_COLUMN, _TEXT[dialect]))
    lines.append("  {} {}".format(SOURCE_COLUMN, _TEXT[dialect]))
    lines.append("  {} {}".format(LOADED_COLUMN, _TIMESTAMP[dialect]))
    return "create table if not exists {} (\n{}\n)".format(
        qualified(contract), ",\n".join(lines)
    )


def create_ledger(contract, dialect="duckdb"):
    """One row per load, so a partition can say when it arrived and from which file.

    The ledger is not the source of truth about what is in the table. It records what a
    load claimed and the reconciliation between the two is a thing that can then fail.
    """
    if dialect not in DIALECTS:
        raise UnknownDialect("no such dialect: {}".format(dialect))
    return (
        "create table if not exists {}.{} (\n"
        "  {} {},\n"
        "  dataset {},\n"
        "  rows_loaded BIGINT,\n"
        "  rows_held BIGINT,\n"
        "  {} {},\n"
        "  {} {}\n"
        ")".format(
            RAW_SCHEMA, LEDGER_TABLE,
            PARTITION_COLUMN, _TEXT[dialect],
            _TEXT[dialect],
            SOURCE_COLUMN, _TEXT[dialect],
            LOADED_COLUMN, _TIMESTAMP[dialect],
        )
    )


def create_schema(dialect="duckdb"):
    if dialect not in DIALECTS:
        raise UnknownDialect("no such dialect: {}".format(dialect))
    return "create schema if not exists {}".format(RAW_SCHEMA)


def statements(contract, dialect="duckdb"):
    return [create_schema(dialect),
            create_table(contract, dialect),
            create_ledger(contract, dialect)]


def inferred_types(con, path):
    """What duckdb would decide this file's columns are, left to itself.

    Nothing in the load path calls this. It exists because a claim about a reader guessing
    wrong should be a measurement rather than a sentence in a README.
    """
    rows = con.execute(
        "describe select * from read_csv(?, header=true)", [path]
    ).fetchall()
    return dict((r[0], r[1]) for r in rows)


def type_disagreements(con, paths):
    """Columns whose inferred type is not the same across every file given.

    Returns {column: {type: [paths]}} and only for columns where more than one type came
    back. An empty result means the guess is stable across these files, which is a weaker
    statement than it looks and is why the paths are kept.
    """
    if not paths:
        raise ValueError("no files to compare, a stable answer over nothing is not one")

    seen = {}
    for path in paths:
        for column, type_ in inferred_types(con, path).items():
            seen.setdefault(column, {}).setdefault(type_, []).append(path)
    return dict((c, t) for c, t in seen.items() if len(t) > 1)


PROBE_TABLE = "_inference_probe"


def _text_read(contract, paths):
    files = ", ".join("'{}'".format(p.replace("'", "''")) for p in paths)
    spec = ", ".join(
        "'{}': 'VARCHAR'".format(n.replace("'", "''")) for n in source_columns(contract))
    return "read_csv([{}], header=true, columns={{{}}})".format(files, spec)


def inference_damage(con, contract, paths):
    """Load these files the way a daily pipeline does and count what the types cost.

    The first file decides the schema, every later one is appended into it, and the result
    is compared cell for cell against reading the same files as text. This is the operation
    an orchestrated pipeline performs without anybody choosing it, one partition at a time.

    Reading the whole set at once is the safe one. duckdb refuses a glob whose files
    disagree. It is the partition at a time path that accepts them quietly.
    """
    if len(paths) < 2:
        raise ValueError(
            "need at least two files, a schema decided by one file cannot disagree"
        )

    columns = source_columns(contract)
    con.execute("drop table if exists {}".format(PROBE_TABLE))
    con.execute("create table {} as select * from read_csv('{}', header=true)".format(
        PROBE_TABLE, paths[0].replace("'", "''")))
    for path in paths[1:]:
        con.execute("insert into {} select * from read_csv('{}', header=true)".format(
            PROBE_TABLE, path.replace("'", "''")))

    text = _text_read(contract, paths)
    changed = {}
    for column in columns:
        quoted = '"' + column.replace('"', '""') + '"'
        row = con.execute(
            "with a as (select {0} v, count(*) n from {1} group by 1), "
            "b as (select cast({0} as varchar) v, count(*) n from {2} group by 1) "
            "select coalesce(sum(abs(coalesce(a.n, 0) - coalesce(b.n, 0))), 0) "
            "from a full outer join b on a.v is not distinct from b.v".format(
                quoted, text, PROBE_TABLE)
        ).fetchone()
        # Every changed cell shows up twice, once missing from each side.
        if row[0]:
            changed[column] = int(row[0]) // 2

    rows = con.execute("select count(*) from {}".format(PROBE_TABLE)).fetchone()[0]
    typed_from = inferred_types(con, paths[0])
    con.execute("drop table {}".format(PROBE_TABLE))
    return {
        "rows": rows,
        "typed_from": paths[0],
        "types": dict((c, typed_from.get(c)) for c in columns),
        "changed": changed,
        "cells": rows * len(columns),
    }
