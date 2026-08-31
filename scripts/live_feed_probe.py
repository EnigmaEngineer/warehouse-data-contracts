"""Point both partition level clauses at the live feed instead of at the archive.

    python scripts/live_feed_probe.py
    python scripts/live_feed_probe.py --days 15 --recent 2026-08-30

Two arms. The first fetches one recent day and one archive day into a temporary directory
and prints what the freshness clause reads on each, which is the only way to see the
reference doing work. The second asks the API for a row count and a newest event per day
over a window, which is cheap enough to sweep and is what the volume floor gets pointed at.

Nothing here touches data/raw or the manifest. The corpus is a fixed fourteen days and a
probe that rewrote it would move numbers this repo publishes.

The reason both arms exist in one script is that the archive cannot answer either question.
Fourteen consecutive complete January days contain no example of a day that is still being
published, so the floor cannot fire on any of them and the freshness clause fails on all of
them for a reason that has nothing to do with the feed.
"""

import argparse
import datetime
import json
import os
import shutil
import sys
import tempfile
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from contracts import feed, spec
from ingest import fetch

RESOURCE = "https://data.cityofnewyork.us/resource/erm2-nwe9.json"


def _ask(select, where, timeout=60):
    url = RESOURCE + "?" + urllib.parse.urlencode({"$select": select, "$where": where})
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.load(response)


def day_summary(day):
    """Rows and newest event for one day, from the API rather than from a download.

    The newest event is the tell that separates a quiet day from an unfinished one. A
    settled day ends at 23:59 and a day still being published stops wherever the publisher
    got to. A row count alone cannot tell those apart.
    """
    start = day.isoformat()
    end = (day + datetime.timedelta(days=1)).isoformat()
    where = "created_date >= '{}T00:00:00' AND created_date < '{}T00:00:00'".format(
        start, end)
    rows = int(_ask("count(1)", where)[0]["count_1"])
    newest = _ask("max(created_date)", where)[0].get("max_created_date")
    return {"partition": start, "rows": rows, "newest": newest}


def reference_arm(contract, recent, archive):
    scratch = tempfile.mkdtemp(prefix="live-feed-")
    try:
        for label, day in (("recent ", recent), ("archive", archive)):
            entry = fetch.fetch_day(day, scratch)
            path = os.path.join(scratch, "created_date={}.csv".format(day))
            newest = feed.newest_event(path, contract.source["partition_column"])
            verdict = feed.freshness(contract, [entry], {day: newest})[0]
            print("  {} {}  rows {:>6}  newest {}  lag {:>9.2f}h  {}".format(
                label, day, entry["rows"], newest, verdict.lag_hours, verdict.verdict))
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def sweep_arm(contract, days, today):
    floor = contract.volume["min_rows_per_partition"]
    print("  {:<12} {:>7}  {:<21} {:<4} {}".format(
        "day", "rows", "newest event", "dow", "volume"))
    fired = 0
    for offset in range(days - 1, -1, -1):
        summary = day_summary(today - datetime.timedelta(days=offset))
        verdict = feed.volume(contract, [summary])[0]
        if verdict.fired:
            fired += 1
        stamp = datetime.date.fromisoformat(summary["partition"])
        print("  {:<12} {:>7}  {:<21} {:<4} {}".format(
            summary["partition"], summary["rows"], summary["newest"] or "none",
            stamp.strftime("%a"), verdict.verdict))
    print()
    print("  floor {} fired on {} of {} live days".format(floor, fired, days))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--contract", default=os.path.join(ROOT, "contracts", "nyc311.yml"))
    ap.add_argument("--days", type=int, default=15)
    ap.add_argument("--recent", help="defaults to yesterday")
    ap.add_argument("--archive", default="2025-01-14")
    args = ap.parse_args()

    contract = spec.load(args.contract)
    today = datetime.date.today()
    recent = args.recent or (today - datetime.timedelta(days=1)).isoformat()

    print("freshness  limit {}h  reference {}".format(
        contract.freshness["max_lag_hours"], contract.freshness["reference"]))
    reference_arm(contract, recent, args.archive)
    print()
    print("volume over the last {} days of the live feed".format(args.days))
    sweep_arm(contract, args.days, today)


if __name__ == "__main__":
    main()
