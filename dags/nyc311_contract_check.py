"""Pull one daily partition and judge it. Then load what passed and model it.

This is the smallest DAG that exercises real code in this repo. It exists so that the local
Airflow setup is a thing that has run rather than a thing described in a README.

The load reads the accepted file the previous task wrote rather than the fetched partition,
so nothing reaches the warehouse without going through the judgement first.

The transform task shells out to scripts/dbt.sh rather than importing dbt. It has to.
Resolving dbt into Airflow's own install succeeds and moves 21 packages Airflow pinned, so
the two live in separate directories and this process cannot see the other one. A subprocess
is not a workaround here, it is the only correct shape.

TODO: nothing here fails the run on the strength of the data. Rows that break the contract
are held and the clean ones are loaded, which is the part that can be decided from the data.
When a partition is bad enough to reject outright is a policy nobody has argued yet, and a
threshold invented here would be one chosen by looking at the fourteen partitions it would
judge.
"""

import os
import subprocess
import sys

import pendulum
from airflow.sdk import dag, task

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

# The same variable the dbt profile reads, so the load and the models cannot end up
# pointed at two different databases.
def warehouse_path():
    return os.environ.get("WDC_DUCKDB", os.path.join(REPO, "data", "warehouse.duckdb"))


@dag(
    dag_id="nyc311_contract_check",
    schedule="@daily",
    # The source is a 2025 archive window rather than a live feed, so a start date of
    # today would give this DAG nothing to do on its first run.
    start_date=pendulum.datetime(2025, 1, 1, tz="UTC"),
    end_date=pendulum.datetime(2025, 1, 14, tz="UTC"),
    catchup=False,
    tags=["nyc311", "contracts"],
)
def nyc311_contract_check():

    @task
    def pull(data_interval_start=None):
        from ingest import fetch

        day = data_interval_start.strftime("%Y-%m-%d")
        raw = os.path.join(REPO, "data", "raw")
        manifest_path = os.path.join(REPO, "data", "manifest.json")

        entry = fetch.fetch_day(day, raw)
        manifest = fetch.load_manifest(manifest_path)
        fetch.upsert(manifest, entry)
        fetch.save_manifest(manifest, manifest_path)
        return entry

    @task
    def check(entry):
        import csv

        from contracts import quarantine, spec, validate

        contract = spec.load(os.path.join(REPO, "contracts", "nyc311.yml"))
        partition = entry["partition"]
        path = os.path.join(REPO, "data", "raw",
                            "created_date={}.csv".format(partition))

        with open(path, newline="") as fh:
            reader = csv.DictReader(fh)
            fieldnames = reader.fieldnames
            rows = list(reader)

        result = validate.validate(contract, rows, header=fieldnames)
        outdir = os.path.join(REPO, "data", "quarantine",
                              "created_date={}".format(partition))
        quarantine.write(outdir, fieldnames, rows, result, partition, path)
        return {"partition": partition, "directory": outdir}

    @task
    def feed_checks(entry):
        """Freshness and volume. The clauses that read a partition rather than a row.

        Runs on the entry the pull just wrote, so `fetched_at` is present and the
        `extract` reading has its input. The fourteen partitions already in the manifest
        were written before that field existed and return no_extract_time instead.

        Nothing here fails the run either, for the same reason the row level split does
        not. What a below_floor verdict should do to a DAG is a policy, and a live day
        that is still being published trips it legitimately.
        """
        import json

        from contracts import feed, spec

        contract = spec.load(os.path.join(REPO, "contracts", "nyc311.yml"))
        partition = entry["partition"]
        path = os.path.join(REPO, "data", "raw",
                            "created_date={}.csv".format(partition))
        newest = feed.newest_event(path, contract.source["partition_column"])

        with open(os.path.join(REPO, "data", "manifest.json")) as fh:
            entries = json.load(fh)["partitions"]

        fresh = feed.freshness(contract, entries, {partition: newest})
        mine = [v for v in fresh if v.partition == partition][0]
        vol = [v for v in feed.volume(contract, entries) if v.partition == partition][0]

        print("freshness {} lag {}".format(mine.verdict, mine.lag_hours))
        print("volume {} rows {} floor {}".format(vol.verdict, vol.rows, vol.limit))
        return {"freshness": mine.verdict, "volume": vol.verdict}

    @task
    def load_raw(judged, entry):
        from contracts import spec
        from warehouse import load

        contract = spec.load(os.path.join(REPO, "contracts", "nyc311.yml"))
        con = load.connect(warehouse_path())
        try:
            load.apply_schema(con, contract)
            # The directory rather than the accepted file. The loader reads the report
            # itself and refuses a directory nobody judged, so this task cannot be pointed
            # at data/raw by a later edit that looked reasonable.
            return load.load_partition(
                con, contract, judged["partition"], judged["directory"],
                entry.get("sha256"),
            )
        finally:
            con.close()

    @task
    def transform(loaded):
        """dbt build. Models, tests and the snapshot, in one pass and in that order.

        build rather than run then test. run leaves a mart that has been written and not
        checked, and on a failure the untested mart is already published.
        """
        env = dict(os.environ)
        env["WDC_DUCKDB"] = warehouse_path()
        result = subprocess.run(
            ["bash", os.path.join(REPO, "scripts", "dbt.sh"), "build"],
            env=env, capture_output=True, text=True)
        # dbt's own summary line, kept because the task log is where anyone debugging a
        # failed run looks first and the exit code alone does not say which model died.
        tail = result.stdout.strip().splitlines()[-15:]
        print("\n".join(tail))
        if result.returncode != 0:
            print(result.stderr[-2000:])
            raise RuntimeError("dbt build failed for {}".format(loaded["partition"]))
        return {"partition": loaded["partition"], "rows_loaded": loaded["rows_loaded"]}

    pulled = pull()
    # feed_checks hangs off the pull rather than sitting in the line, because it judges the
    # partition as a whole and the row split does not depend on it.
    feed_checks(pulled)
    transform(load_raw(check(pulled), pulled))


nyc311_contract_check()
