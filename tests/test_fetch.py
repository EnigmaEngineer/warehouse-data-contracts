"""The fetch layer, mostly its completeness guard.

Nothing here talks to the network. The API round trip is exercised by
scripts/pull_source.py and by the DAG, and that gap is named in the README.
"""

import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from ingest import fetch


def check_the_window_is_half_open():
    w = fetch.window("2025-01-01")
    assert ">= '2025-01-01T00:00:00'" in w, w
    assert "< '2025-01-02T00:00:00'" in w, w


def check_a_closed_window_would_double_count_the_boundary():
    # Not a hypothetical. Asking the API for `between '2025-01-01' and '2025-01-08'` over
    # this dataset returns 10,878 rows for the first day and the half open form returns
    # 10,873. Those 5 rows land at exactly midnight and a closed window puts each of them
    # in two partitions.
    first = fetch.window("2025-01-01")
    second = fetch.window("2025-01-02")
    assert "< '2025-01-02T00:00:00'" in first
    assert ">= '2025-01-02T00:00:00'" in second


def check_fetch_day_refuses_a_short_read(monkeypatched=None):
    calls = {}

    def fake_expected(day):
        calls["expected"] = day
        return 100

    def fake_rows(day):
        return [{"unique_key": str(i)} for i in range(90)]

    real_expected, real_rows = fetch.expected_rows, fetch.fetch_rows
    fetch.expected_rows, fetch.fetch_rows = fake_expected, fake_rows
    try:
        with tempfile.TemporaryDirectory() as out:
            fetch.fetch_day("2025-01-01", out)
    except fetch.IncompletePartition as exc:
        assert "reports 100" in str(exc) and "returned 90" in str(exc), str(exc)
        return
    finally:
        fetch.expected_rows, fetch.fetch_rows = real_expected, real_rows
    raise AssertionError("a short read has to be refused")


def check_fetch_day_accepts_a_complete_read():
    def fake_expected(day):
        return 3

    def fake_rows(day):
        return [{"unique_key": str(i), "created_date": "2025-01-01T00:00:00.000"}
                for i in range(3)]

    real_expected, real_rows = fetch.expected_rows, fetch.fetch_rows
    fetch.expected_rows, fetch.fetch_rows = fake_expected, fake_rows
    try:
        with tempfile.TemporaryDirectory() as out:
            entry = fetch.fetch_day("2025-01-01", out)
            assert entry["rows"] == 3
            assert entry["expected_rows"] == 3
            assert len(entry["sha256"]) == 64
    finally:
        fetch.expected_rows, fetch.fetch_rows = real_expected, real_rows


def check_a_written_partition_carries_every_contracted_column():
    with tempfile.TemporaryDirectory() as out:
        path = fetch.write_partition("2025-01-01", [{"unique_key": "1"}], out)
        with open(path) as fh:
            header = fh.readline().strip().split(",")
        assert header == fetch.COLUMNS, header


def check_the_line_terminator_is_fixed_so_a_checksum_travels():
    with tempfile.TemporaryDirectory() as out:
        path = fetch.write_partition("2025-01-01", [{"unique_key": "1"}], out)
        with open(path, "rb") as fh:
            raw = fh.read()
        assert b"\r\n" not in raw, "a CRLF here changes the sha256 on windows"


def check_fetch_rows_pages_until_a_short_page():
    # No partition in the current window is anywhere near 50,000 rows, so the paging
    # branch never runs against the real source and nothing would notice if it broke. A
    # mutant turning `len(page) < PAGE` into `<=` stops after the first full page, which
    # is the silent truncation this whole module exists to prevent.
    real_get, real_page = fetch._get, fetch.PAGE
    fetch.PAGE = 3
    served = []

    def fake_get(url, timeout=120):
        offset = int(url.split("%24offset=")[1].split("&")[0])
        served.append(offset)
        remaining = max(0, 7 - offset)
        n = min(fetch.PAGE, remaining)
        head = ",".join(fetch.COLUMNS)
        body = "".join(
            "{},,,,,,,,,,\n".format(offset + i) for i in range(n)
        )
        return head + "\n" + body

    fetch._get = fake_get
    try:
        rows = fetch.fetch_rows("2025-01-01")
    finally:
        fetch._get, fetch.PAGE = real_get, real_page

    assert len(rows) == 7, len(rows)
    assert served == [0, 3, 6], served
    assert [r["unique_key"] for r in rows] == [str(i) for i in range(7)]


def check_fetch_rows_stops_on_an_exactly_empty_final_page():
    # The boundary the paging rule gets wrong in the other direction. A window whose size
    # is an exact multiple of the page needs one more request that comes back empty.
    real_get, real_page = fetch._get, fetch.PAGE
    fetch.PAGE = 3
    served = []

    def fake_get(url, timeout=120):
        offset = int(url.split("%24offset=")[1].split("&")[0])
        served.append(offset)
        n = min(fetch.PAGE, max(0, 6 - offset))
        head = ",".join(fetch.COLUMNS)
        return head + "\n" + "".join("{},,,,,,,,,,\n".format(offset + i) for i in range(n))

    fetch._get = fake_get
    try:
        rows = fetch.fetch_rows("2025-01-01")
    finally:
        fetch._get, fetch.PAGE = real_get, real_page

    assert len(rows) == 6, len(rows)
    assert served == [0, 3, 6], served


def check_the_url_omits_limit_and_offset_when_they_are_not_given():
    plain = fetch._url("count(1)", "created_date >= '2025-01-01T00:00:00'")
    assert "limit" not in plain and "offset" not in plain, plain
    paged = fetch._url("a", "b", order="a", limit=5, offset=10)
    assert "%24limit=5" in paged and "%24offset=10" in paged, paged


def check_offset_zero_is_still_sent():
    # `if offset is not None` rather than `if offset`. Zero is a real offset and dropping
    # it would make the first page come back unordered relative to the rest.
    first = fetch._url("a", "b", limit=5, offset=0)
    assert "%24offset=0" in first, first


def check_upsert_replaces_rather_than_appends():
    m = fetch.load_manifest("/nonexistent/manifest.json")
    fetch.upsert(m, {"partition": "2025-01-01", "rows": 1})
    fetch.upsert(m, {"partition": "2025-01-01", "rows": 2})
    assert len(m["partitions"]) == 1, m["partitions"]
    assert m["partitions"][0]["rows"] == 2


def check_the_manifest_matches_whatever_partitions_are_on_disk():
    # data/raw is not committed, so a clone has the manifest and no CSVs, and this loop
    # legitimately verifies nothing. The first version of this check printed the count it
    # had verified and asserted nothing, which meant it passed identically whether it had
    # checked fourteen partitions or zero. Printing a number is not a gate. The count is
    # returned and the check below is the one that cannot be satisfied by an empty loop.
    manifest_path = os.path.join(ROOT, "data", "manifest.json")
    m = fetch.load_manifest(manifest_path)
    assert m["partitions"], "the manifest is empty"
    raw = os.path.join(ROOT, "data", "raw")
    checked = 0
    for entry in m["partitions"]:
        path = os.path.join(raw, "created_date={}.csv".format(entry["partition"]))
        if not os.path.exists(path):
            continue
        assert fetch.sha256(path) == entry["sha256"], entry["partition"]
        checked += 1
    print("  manifest: {} of {} partitions present and matching".format(
        checked, len(m["partitions"])))


def check_the_committed_fixture_matches_its_recorded_checksum():
    # The half that always has something to check. tests/fixtures/sample_partition.csv is
    # committed, so this runs on a clone with no data/raw and it fails if the fixture is
    # edited without the manifest moving with it.
    manifest_path = os.path.join(ROOT, "data", "manifest.json")
    m = fetch.load_manifest(manifest_path)
    fixture = m.get("fixture")
    assert fixture, "the manifest has no fixture entry"
    path = os.path.join(ROOT, fixture["path"])
    assert os.path.exists(path), path
    assert fetch.sha256(path) == fixture["sha256"], path
    with open(path, newline="") as fh:
        lines = sum(1 for _ in fh)
    assert lines - 1 == fixture["rows"], lines
