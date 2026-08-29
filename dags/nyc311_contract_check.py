"""Pull one daily partition, judge it against the contract, then load what passed.

This is the smallest DAG that exercises real code in this repo. It exists so that the local
Airflow setup is a thing that has run rather than a thing described in a README. The dbt
half and the published mart are not here yet.

The load reads the accepted file the previous task wrote rather than the fetched partition,
so nothing reaches the warehouse without going through the judgement first.

TODO: nothing here fails the run on the strength of the data. Rows that break the contract
are held and the clean ones are loaded, which is the part that can be decided from the data.
When a partition is bad enough to reject outright is a policy nobody has argued yet, and a
threshold invented here would be one chosen by looking at the fourteen partitions it would
judge.
"""

import os
import sys

import pendulum
from airflow.sdk import dag, task

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)


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
    def load_raw(judged, entry):
        from contracts import spec
        from warehouse import load

        contract = spec.load(os.path.join(REPO, "contracts", "nyc311.yml"))
        con = load.connect(os.path.join(REPO, "data", "warehouse.duckdb"))
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

    pulled = pull()
    load_raw(check(pulled), pulled)


nyc311_contract_check()
