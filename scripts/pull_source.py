"""Pull daily partitions of the source into data/raw and record them in the manifest.

    python scripts/pull_source.py --start 2025-01-01 --days 14

The manifest is committed and the CSVs are not, apart from one fixture partition. A
checksum in the repo is what lets someone else find out that their copy of the corpus is
not the one a published number was measured on.
"""

import argparse
import datetime
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ingest import fetch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW = os.path.join(ROOT, "data", "raw")
MANIFEST = os.path.join(ROOT, "data", "manifest.json")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", required=True)
    ap.add_argument("--days", type=int, default=1)
    ap.add_argument("--out", default=RAW)
    ap.add_argument("--manifest", default=MANIFEST)
    args = ap.parse_args(argv)

    manifest = fetch.load_manifest(args.manifest)
    start = datetime.datetime.strptime(args.start, "%Y-%m-%d")

    for i in range(args.days):
        day = (start + datetime.timedelta(days=i)).strftime("%Y-%m-%d")
        entry = fetch.fetch_day(day, args.out)
        fetch.upsert(manifest, entry)
        print(
            "{}  {:>6} rows  {:>9} bytes  {}".format(
                entry["partition"], entry["rows"], entry["bytes"], entry["sha256"][:12]
            )
        )

    fetch.save_manifest(manifest, args.manifest)
    total = sum(e["rows"] for e in manifest["partitions"])
    print("{} partitions, {} rows".format(len(manifest["partitions"]), total))
    return 0


if __name__ == "__main__":
    sys.exit(main())
