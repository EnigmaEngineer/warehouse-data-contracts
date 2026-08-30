"""Reading the type 2 history table.

dbt writes the table and this reads it, so the fixture builds the table by hand. That is
deliberate. A test that ran dbt would be testing dbt, and it would need dbt installed,
which nothing in this directory does.

The fixture is lopsided. One key has two versions, one has three and two have one. So a
count of keys and a count of versions and a count of superseded rows come out as four
different numbers, and no two of them can stand in for each other.
"""

import datetime
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from warehouse import history, load

T0 = datetime.datetime(2026, 8, 27, 9, 0, 0)
T1 = datetime.datetime(2026, 8, 28, 9, 0, 0)
T2 = datetime.datetime(2026, 8, 29, 9, 0, 0)

# Column order matches the create statement below.
ROWS = [
    ("a", "In Progress", None, T0, T1),
    ("a", "Closed", T1, T1, None),
    ("b", "Open", None, T0, T1),
    ("b", "Assigned", None, T1, T2),
    ("b", "Closed", T2, T2, None),
    ("c", "Closed", T0, T0, None),
    ("d", "Open", None, T0, None),
]


def table(rows=ROWS):
    con = load.connect(":memory:")
    con.execute("create schema history")
    con.execute(
        "create table history.snap_service_request ("
        "  unique_key VARCHAR, status VARCHAR, closed_at TIMESTAMP,"
        "  dbt_valid_from TIMESTAMP, dbt_valid_to TIMESTAMP)")
    for row in rows:
        con.execute(
            "insert into history.snap_service_request values (?, ?, ?, ?, ?)", list(row))
    return con


def check_the_summary_separates_versions_keys_current_and_superseded():
    con = table()
    s = history.version_summary(con)
    assert s == {"versions": 7, "keys": 4, "current": 4, "superseded": 3}, s
    con.close()


def check_an_empty_history_is_refused_rather_than_reported_as_quiet():
    """Four zeros read exactly like a history in which nothing has happened.

    This is the zero input case, and a summary that returns it is the reason a report can
    say a source is stable having looked at nothing.
    """
    con = table(rows=[])
    try:
        history.version_summary(con)
    except history.NoHistory as e:
        assert "holds no rows" in str(e), e
    else:
        raise AssertionError("an empty history table was summarised")
    con.close()


def check_only_the_keys_with_more_than_one_version_come_back():
    con = table()
    got = history.keys_with_history(con)
    assert sorted(got) == ["a", "b"], sorted(got)
    assert len(got["a"]) == 2 and len(got["b"]) == 3, got
    con.close()


def check_the_versions_of_one_key_are_in_the_order_they_happened():
    """Ordered by valid_from, not by status or by insertion.

    A history read out of order is worse than no history, because it reads fine.
    """
    con = table()
    statuses = [v["status"] for v in history.keys_with_history(con)["b"]]
    assert statuses == ["Open", "Assigned", "Closed"], statuses
    con.close()


def check_a_clean_history_has_exactly_one_open_version_per_key():
    con = table()
    assert history.one_row_per_key_is_current(con) == []
    con.close()


def check_two_open_versions_for_one_key_are_reported():
    """The failure that makes a history quietly wrong. The old version never closed."""
    rows = ROWS + [("c", "Open", None, T2, None)]
    con = table(rows)
    got = history.one_row_per_key_is_current(con)
    assert got == [{"unique_key": "c", "open_versions": 2}], got
    con.close()


def check_a_key_with_no_open_version_is_reported_too():
    """The other side. Every version closed and the key vanished from the present.

    A check that only looks for too many passes on a key that has none, which is what a
    snapshot does when it closes a row and fails to open its replacement.
    """
    rows = [r for r in ROWS if r[0] != "d"] + [("d", "Open", None, T0, T2)]
    con = table(rows)
    got = history.one_row_per_key_is_current(con)
    assert got == [{"unique_key": "d", "open_versions": 0}], got
    con.close()
