"""Comparing two extracts of the same partition.

Fixtures are lopsided everywhere. One row moves two cells and another moves one, so a
counter that counts rows says 2 and a counter that counts cells says 3, and the two cannot
be confused for each other by a test that happens to have symmetric input.
"""

import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from ingest import compare

COLUMNS = ["unique_key", "status", "closed_date", "agency"]

# Four rows. Row 1 moves two columns, row 2 moves one, rows 3 and 4 do not move.
BEFORE = (
    "unique_key,status,closed_date,agency\n"
    "1,In Progress,,DPR\n"
    "2,Closed,2025-01-03T12:29:42.000,DOT\n"
    "3,Closed,2025-01-04T08:00:00.000,NYPD\n"
    "4,Open,,DSNY\n"
)
AFTER = (
    "unique_key,status,closed_date,agency\n"
    "1,Closed,2026-08-27T14:56:33.000,DPR\n"
    "2,Closed,2026-08-28T14:27:34.000,DOT\n"
    "3,Closed,2025-01-04T08:00:00.000,NYPD\n"
    "4,Open,,DSNY\n"
)


def written(root, name, body):
    path = os.path.join(root, name)
    with open(path, "w", newline="") as fh:
        fh.write(body)
    return path


def diffed(root, before=BEFORE, after=AFTER, partition="2025-01-01"):
    return compare.diff_extracts(
        partition,
        written(root, "before.csv", before),
        written(root, "after.csv", after),
        COLUMNS,
    )


def check_a_changed_cell_is_found_with_both_of_its_values():
    with tempfile.TemporaryDirectory() as tmp:
        d = diffed(tmp)
        assert d.changed["2"] == {
            "closed_date": ["2025-01-03T12:29:42.000", "2026-08-28T14:27:34.000"]
        }, d.changed


def check_rows_and_cells_are_different_numbers():
    """The whole reason the fixture is lopsided.

    Two rows moved and three cells did. Equal counts would let one function stand in for
    the other and nothing would notice until a report quoted the wrong one.
    """
    with tempfile.TemporaryDirectory() as tmp:
        d = diffed(tmp)
        assert d.changed_rows() == 2, d.changed
        assert d.changed_cells() == 3, d.changed


def check_the_columns_that_moved_are_counted_per_column():
    with tempfile.TemporaryDirectory() as tmp:
        d = diffed(tmp)
        assert d.columns_that_moved() == {"status": 1, "closed_date": 2}, \
            d.columns_that_moved()


def check_a_row_that_did_not_move_is_not_in_the_diff():
    """A diff reporting every row is a diff nobody reads."""
    with tempfile.TemporaryDirectory() as tmp:
        d = diffed(tmp)
        assert "3" not in d.changed and "4" not in d.changed, d.changed


def check_two_identical_extracts_are_empty_rather_than_absent():
    with tempfile.TemporaryDirectory() as tmp:
        d = diffed(tmp, after=BEFORE)
        assert d.is_empty(), d.as_dict()
        assert d.rows_before == 4 and d.rows_after == 4, d.as_dict()


def check_two_empty_extracts_are_refused():
    """The zero input case. Two empty files agree about nothing.

    Reported as clean, this is the shape that lets a broken fetch look like a stable
    source. The message is asserted rather than the type, because ValueError is what half
    the standard library raises.
    """
    header = "unique_key,status,closed_date,agency\n"
    with tempfile.TemporaryDirectory() as tmp:
        try:
            diffed(tmp, before=header, after=header)
        except ValueError as e:
            assert "not a clean comparison" in str(e), e
        else:
            raise AssertionError("two empty extracts were compared")


def check_one_empty_side_is_a_diff_rather_than_a_refusal():
    """The asymmetric case, and the one a surviving mutant found.

    Both sides empty is a comparison of nothing and gets refused. One side empty is a
    partition that arrived or vanished entirely, which is the loudest thing this could
    possibly report, and refusing it would throw the finding away.
    """
    header = "unique_key,status,closed_date,agency\n"
    with tempfile.TemporaryDirectory() as tmp:
        d = diffed(tmp, before=header, after=AFTER)
        assert d.rows_before == 0 and d.rows_after == 4, d.as_dict()
        assert len(d.added) == 4, d.added
        assert not d.is_empty()

        e = diffed(tmp, before=BEFORE, after=header)
        assert len(e.removed) == 4, e.removed
        assert not e.is_empty()


def check_the_saved_diff_has_its_keys_in_a_stable_order():
    """The file is committed, so an unsorted write makes every later save a noisy diff.

    A mutant flipping sort_keys changes nothing any other check reads, and the cost of it
    lands on whoever reviews the next commit rather than on the suite.
    """
    with tempfile.TemporaryDirectory() as tmp:
        d = diffed(tmp)
        path = compare.save([d], os.path.join(tmp, "diff.json"))
        with open(path) as fh:
            body = fh.read()
        keys = [line.strip().split('"')[1] for line in body.splitlines()
                if line.strip().startswith('"') and ": " in line]
        top = [k for k in keys if k in
               ("added", "changed", "partition", "removed", "rows_after", "rows_before")]
        assert top == sorted(top), top
        assert body.endswith("\n"), repr(body[-20:])
        # Two spaces per level. A committed file that reindents itself turns every later
        # save into a whole file diff nobody reads.
        indented = [line for line in body.splitlines()
                    if line.strip().startswith('"partition"')]
        assert indented and indented[0].startswith("      \""), indented


def check_a_file_with_no_key_column_is_refused():
    with tempfile.TemporaryDirectory() as tmp:
        path = written(tmp, "nokey.csv", "a,b\n1,2\n")
        try:
            compare.read_keyed(path)
        except ValueError as e:
            assert "unique_key" in str(e), e
        else:
            raise AssertionError("a file with no key column was read")


def check_added_and_removed_keys_are_kept_apart():
    """Lopsided again. One key arrives and two leave."""
    after = (
        "unique_key,status,closed_date,agency\n"
        "1,In Progress,,DPR\n"
        "5,Open,,DOT\n"
    )
    with tempfile.TemporaryDirectory() as tmp:
        d = diffed(tmp, after=after)
        assert d.added == ["5"], d.added
        assert d.removed == ["2", "3", "4"], d.removed
        assert not d.is_empty()


def check_replaying_the_diff_reproduces_the_later_extract():
    """The claim the whole reproducibility argument rests on, checked rather than asserted."""
    with tempfile.TemporaryDirectory() as tmp:
        d = diffed(tmp)
        out = os.path.join(tmp, "replayed.csv")
        compare.apply_diff(d, os.path.join(tmp, "before.csv"), out, COLUMNS)
        with open(out) as fh:
            produced = fh.read()
        assert produced == AFTER, produced


def check_replaying_against_the_wrong_file_is_refused_by_row_count():
    short = "unique_key,status,closed_date,agency\n1,In Progress,,DPR\n"
    with tempfile.TemporaryDirectory() as tmp:
        d = diffed(tmp)
        wrong = written(tmp, "short.csv", short)
        try:
            compare.apply_diff(d, wrong, os.path.join(tmp, "out.csv"), COLUMNS)
        except ValueError as e:
            assert "the diff was taken against" in str(e), e
        else:
            raise AssertionError("a diff was replayed against the wrong file")


def check_replaying_against_a_changed_value_is_refused_by_the_value():
    """The row count check alone would pass this. Same four rows, one value edited.

    A diff replayed onto an input that has already moved produces a plausible file and a
    wrong answer, and nothing downstream can tell.
    """
    edited = BEFORE.replace("2,Closed,2025-01-03T12:29:42.000,DOT",
                            "2,Closed,2025-01-03T23:59:59.000,DOT")
    with tempfile.TemporaryDirectory() as tmp:
        d = diffed(tmp)
        wrong = written(tmp, "edited.csv", edited)
        try:
            compare.apply_diff(d, wrong, os.path.join(tmp, "out.csv"), COLUMNS)
        except ValueError as e:
            assert "the diff expected" in str(e), e
        else:
            raise AssertionError("a diff was replayed onto a value that had moved")


def check_a_saved_diff_reads_back_the_same():
    with tempfile.TemporaryDirectory() as tmp:
        d = diffed(tmp)
        path = compare.save([d], os.path.join(tmp, "diff.json"))
        back = compare.load(path)
        assert len(back) == 1, back
        assert back[0].partition == d.partition
        assert back[0].changed == d.changed
        assert back[0].changed_cells() == 3, back[0].changed
