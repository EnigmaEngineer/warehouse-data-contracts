"""The raw table's shape, and what a reader guesses when nobody tells it.

The fixtures here are lopsided in the way that matters for a type sniffer. Three files
agree and one does not, so a check counting disagreements cannot pass by symmetry, and the
odd file carries exactly one offending row rather than a column of them. One row in one
file out of four is the situation the real corpus is in.
"""

import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import duckdb

from contracts import spec
from warehouse import schema

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

# Three files whose zips all start with a digit other than zero, and one that does not.
PLAIN = [
    "day,zip,n\n2025-01-01,10001,3\n2025-01-01,11201,4\n2025-01-01,10453,9\n",
    "day,zip,n\n2025-01-02,10002,1\n2025-01-02,11202,2\n",
    "day,zip,n\n2025-01-03,10003,7\n2025-01-03,11203,8\n2025-01-03,10454,5\n"
    "2025-01-03,10455,6\n",
]
ODD = "day,zip,n\n2025-01-04,00083,1\n2025-01-04,10004,2\n2025-01-04,11204,3\n"


def contract():
    return spec.parse(CONTRACT)


def write_files(directory, bodies):
    paths = []
    for i, body in enumerate(bodies):
        path = os.path.join(directory, "part{}.csv".format(i))
        with open(path, "w") as fh:
            fh.write(body)
        paths.append(path)
    return paths


def check_every_source_column_is_text_in_the_duckdb_ddl():
    sql = schema.create_table(contract())
    for name in ("day", "zip", "n"):
        assert "{} VARCHAR".format(name) in sql, sql
    assert "BIGINT" not in sql, sql


def check_the_ddl_carries_the_three_columns_the_load_adds():
    sql = schema.create_table(contract())
    for name in schema.METADATA_COLUMNS:
        assert name in sql, sql
    assert "_loaded_at TIMESTAMP" in sql, sql


def check_snowflake_uses_its_own_timestamp_type():
    duck = schema.create_table(contract(), "duckdb")
    snow = schema.create_table(contract(), "snowflake")
    assert "_loaded_at TIMESTAMP\n" in duck, duck
    assert "_loaded_at TIMESTAMP_NTZ" in snow, snow


def check_the_two_dialects_differ_only_where_they_are_meant_to():
    """The Snowflake statements have never run against Snowflake.

    So the least this can do is pin what makes them different, which is one timestamp type
    in two places. A generator emitting two dialects that quietly drifted apart everywhere
    else would look exactly as correct as this one from the outside.
    """
    c = contract()
    duck = schema.statements(c, "duckdb")
    snow = schema.statements(c, "snowflake")
    assert len(duck) == len(snow) == 3, (duck, snow)
    for a, b in zip(duck, snow):
        assert a == b.replace("TIMESTAMP_NTZ", "TIMESTAMP"), (a, b)
    assert snow[1].count("TIMESTAMP_NTZ") == 1, snow[1]
    assert snow[2].count("TIMESTAMP_NTZ") == 1, snow[2]


def check_an_unknown_dialect_is_refused_by_name():
    try:
        schema.create_table(contract(), "postgres")
    except schema.UnknownDialect as exc:
        assert "postgres" in str(exc), str(exc)
    else:
        raise AssertionError("an unknown dialect was accepted")


def check_a_contract_column_cannot_shadow_a_metadata_column():
    text = CONTRACT.replace("  - name: zip", "  - name: _partition")
    shadowed = spec.parse(text)
    try:
        schema.create_table(shadowed)
    except schema.MetadataColumnClash as exc:
        # The message has to name the column. A clash reported without it sends the
        # reader to a ten column contract to find which one.
        assert "_partition" in str(exc), str(exc)
    else:
        raise AssertionError("a contract shadowing _partition was accepted")


def check_read_columns_are_all_text_and_cover_the_contract():
    columns = schema.read_columns(contract())
    assert sorted(columns) == ["day", "n", "zip"], columns
    assert set(columns.values()) == {"VARCHAR"}, columns


def check_type_disagreements_refuses_an_empty_file_list():
    con = duckdb.connect()
    try:
        schema.type_disagreements(con, [])
    except ValueError as exc:
        assert "no files" in str(exc), str(exc)
    else:
        raise AssertionError("a stable answer was reported over zero files")
    finally:
        con.close()


def check_type_disagreements_is_empty_when_the_files_agree():
    con = duckdb.connect()
    with tempfile.TemporaryDirectory() as tmp:
        paths = write_files(tmp, PLAIN)
        assert schema.type_disagreements(con, paths) == {}
    con.close()


def check_one_odd_file_out_of_four_is_enough_to_split_the_guess():
    con = duckdb.connect()
    with tempfile.TemporaryDirectory() as tmp:
        paths = write_files(tmp, PLAIN + [ODD])
        found = schema.type_disagreements(con, paths)
        assert list(found) == ["zip"], found
        types = dict((t, len(f)) for t, f in found["zip"].items())
        assert types == {"BIGINT": 3, "VARCHAR": 1}, types
    con.close()


def check_inference_damage_needs_more_than_one_file():
    con = duckdb.connect()
    with tempfile.TemporaryDirectory() as tmp:
        paths = write_files(tmp, PLAIN[:1])
        try:
            schema.inference_damage(con, contract(), paths)
        except ValueError as exc:
            assert "two files" in str(exc), str(exc)
        else:
            raise AssertionError("a schema decided by one file was said to disagree")
    con.close()


def check_exactly_two_files_is_enough_to_ask():
    """The boundary. One file is refused and three are accepted, and two is the case
    nothing was exercising, so both halves of `len(paths) < 2` were free to be wrong."""
    con = duckdb.connect()
    with tempfile.TemporaryDirectory() as tmp:
        paths = write_files(tmp, [PLAIN[0], ODD])
        damage = schema.inference_damage(con, contract(), paths)
        assert damage["rows"] == 6, damage["rows"]
        assert damage["changed"] == {"zip": 1}, damage["changed"]
    con.close()


def check_the_report_names_the_file_that_decided_the_schema():
    """`typed_from` is the whole claim. Pointing it at the wrong file is invisible unless
    something reads it, and the probe prints it as the headline."""
    con = duckdb.connect()
    with tempfile.TemporaryDirectory() as tmp:
        paths = write_files(tmp, PLAIN + [ODD])
        damage = schema.inference_damage(con, contract(), paths)
        assert damage["typed_from"] == paths[0], damage["typed_from"]
        flipped = schema.inference_damage(con, contract(), list(reversed(paths)))
        assert flipped["typed_from"] == paths[-1], flipped["typed_from"]
    con.close()


def check_the_cell_count_is_rows_times_columns():
    con = duckdb.connect()
    with tempfile.TemporaryDirectory() as tmp:
        paths = write_files(tmp, PLAIN + [ODD])
        damage = schema.inference_damage(con, contract(), paths)
        assert damage["cells"] == 12 * 3, damage["cells"]
    con.close()


def check_a_leading_zero_is_lost_when_an_earlier_file_typed_the_column():
    con = duckdb.connect()
    with tempfile.TemporaryDirectory() as tmp:
        paths = write_files(tmp, PLAIN + [ODD])
        damage = schema.inference_damage(con, contract(), paths)
        assert damage["types"]["zip"] == "BIGINT", damage["types"]
        # One row in twelve, and it is the only cell that moves.
        assert damage["changed"] == {"zip": 1}, damage["changed"]
        assert damage["rows"] == 12, damage["rows"]
    con.close()


def check_the_odd_file_going_first_costs_nothing():
    """The mirror. Same twelve rows, different order, and the damage is zero.

    Without this the check above passes on a function that always reports one changed
    cell, and the point of the whole probe is that the answer depends on the order.
    """
    con = duckdb.connect()
    with tempfile.TemporaryDirectory() as tmp:
        paths = write_files(tmp, [ODD] + PLAIN)
        damage = schema.inference_damage(con, contract(), paths)
        assert damage["types"]["zip"] == "VARCHAR", damage["types"]
        assert damage["changed"] == {}, damage["changed"]
        assert damage["rows"] == 12, damage["rows"]
    con.close()


def check_the_probe_table_does_not_survive_the_probe():
    con = duckdb.connect()
    with tempfile.TemporaryDirectory() as tmp:
        paths = write_files(tmp, PLAIN)
        schema.inference_damage(con, contract(), paths)
        left = con.execute(
            "select count(*) from information_schema.tables where table_name = ?",
            [schema.PROBE_TABLE],
        ).fetchone()[0]
        assert left == 0, "the probe left its table behind"
    con.close()
