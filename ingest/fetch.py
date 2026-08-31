"""Pull one daily partition of NYC 311 out of the Socrata API and prove it is complete.

The completeness check is the reason this file is longer than a curl call. Socrata caps a
response at whatever `$limit` says and returns a 200 either way, so a request that asks
for 25,000 rows of a day holding 90,570 gets 25,000 rows and no error. The corpus is then
defined by the limit rather than by the window, and every figure measured on it is a
figure about an arbitrary prefix.

So the fetch asks the API a second question, `select count(1)` over the same predicate,
and refuses to write a partition whose row count disagrees. That second query is cheap and
it is the only thing standing between this repo and a silently truncated corpus.
"""

import csv
import datetime
import hashlib
import io
import json
import os
import urllib.parse
import urllib.request

DOMAIN = "data.cityofnewyork.us"
RESOURCE = "erm2-nwe9"

# Kept in one place because the contract, the fetch and the manifest all have to agree on
# what a partition contains. A column added here and not to contracts/nyc311.yml shows up
# as an uncontracted column in the profile rather than passing quietly.
COLUMNS = [
    "unique_key",
    "created_date",
    "closed_date",
    "agency",
    "complaint_type",
    "descriptor",
    "incident_zip",
    "borough",
    "status",
    "latitude",
    "longitude",
]

# Socrata's default page cap. Above this it pages, and paging is what the row count check
# exists to catch when it goes wrong.
PAGE = 50000


class IncompletePartition(RuntimeError):
    """The API returned fewer rows than it says the window holds."""


def _url(select, where, order=None, limit=None, offset=None):
    params = {"$select": select, "$where": where}
    if order:
        params["$order"] = order
    if limit is not None:
        params["$limit"] = limit
    if offset is not None:
        params["$offset"] = offset
    query = urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
    return "https://{}/resource/{}.csv?{}".format(DOMAIN, RESOURCE, query)


def _get(url, timeout=120):
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return resp.read().decode("utf-8")


def window(day):
    """Half open [day, day+1). Half open matters. Closed on both ends double counts the
    midnight row into two partitions and the volume check then argues with itself."""
    start = datetime.datetime.strptime(day, "%Y-%m-%d")
    end = start + datetime.timedelta(days=1)
    return (
        "created_date >= '{}' and created_date < '{}'".format(
            start.strftime("%Y-%m-%dT%H:%M:%S"), end.strftime("%Y-%m-%dT%H:%M:%S")
        )
    )


def expected_rows(day):
    text = _get(_url("count(1)", window(day)))
    reader = csv.DictReader(io.StringIO(text))
    row = next(reader)
    return int(row["count_1"])


def fetch_rows(day):
    """Page through the whole window. Returns a list of dict rows in unique_key order."""
    where = window(day)
    select = ",".join(COLUMNS)
    rows = []
    offset = 0
    while True:
        text = _get(_url(select, where, order="unique_key", limit=PAGE, offset=offset))
        page = list(csv.DictReader(io.StringIO(text)))
        rows.extend(page)
        if len(page) < PAGE:
            break
        offset += PAGE
    return rows


def write_partition(day, rows, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "created_date={}.csv".format(day))
    # newline="" and \n rather than the platform default, so the checksum of a partition
    # written on Windows matches one written in the sandbox.
    with open(path, "w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=COLUMNS, lineterminator="\n")
        writer.writeheader()
        for r in rows:
            writer.writerow({c: r.get(c, "") for c in COLUMNS})
    return path


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def fetch_day(day, out_dir, now=None):
    """Fetch the window and refuse it unless the API agrees on how many rows it holds.

    `fetched_at` is recorded because a freshness clause reading `extract` needs it and
    nothing else in the pipeline can recover it later. A file's mtime is a fact about the
    filesystem rather than about the fetch, and the fourteen partitions written before this
    line existed have no honest way to get one. They report no_extract_time instead.

    Taken before the request rather than after, so a slow page does not get counted as
    publisher lag.
    """
    started = now or datetime.datetime.now()
    expected = expected_rows(day)
    rows = fetch_rows(day)
    if len(rows) != expected:
        raise IncompletePartition(
            "{}: API reports {} rows, fetch returned {}".format(day, expected, len(rows))
        )
    path = write_partition(day, rows, out_dir)
    return {
        "partition": day,
        "rows": len(rows),
        "expected_rows": expected,
        "bytes": os.path.getsize(path),
        "sha256": sha256(path),
        "fetched_at": started.strftime("%Y-%m-%dT%H:%M:%S"),
        "path": os.path.relpath(path, os.path.dirname(out_dir)),
    }


def load_manifest(path):
    if not os.path.exists(path):
        return {"resource": RESOURCE, "columns": COLUMNS, "partitions": []}
    with open(path) as fh:
        return json.load(fh)


def save_manifest(manifest, path):
    manifest["partitions"].sort(key=lambda e: e["partition"])
    with open(path, "w") as fh:
        json.dump(manifest, fh, indent=2)
        fh.write("\n")


def upsert(manifest, entry):
    kept = [e for e in manifest["partitions"] if e["partition"] != entry["partition"]]
    kept.append(entry)
    manifest["partitions"] = kept
    return manifest
