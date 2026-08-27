"""Pull one daily partition and measure the contract against it.

This is the smallest DAG that exercises real code in this repo. It exists so that the local
Airflow setup is a thing that has run rather than a thing described in a README. The full
sequence through contract check and dbt to a published mart is not here yet.

TODO: the profile task reports and does not decide. Nothing quarantines a bad batch and
nothing stops a downstream task, because the validator that makes that call does not exist
yet. Wiring a failing task in before then would give the DAG an opinion the code cannot
back up.
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
    def profile(entry):
        from contracts import profile as profile_mod
        from contracts import rules, spec

        contract = spec.load(os.path.join(REPO, "contracts", "nyc311.yml"))
        path = os.path.join(REPO, "data", "raw",
                            "created_date={}.csv".format(entry["partition"]))
        result = profile_mod.profile(contract, path, rules)

        column_hits = sum(len(r.violations) for r in result.results)
        check_hits = sum(c.count for c in result.checks)
        return {
            "partition": entry["partition"],
            "rows": result.rows,
            "column_rules_broken": column_hits,
            "rows_failing_cross_column_checks": check_hits,
        }

    profile(pull())


nyc311_contract_check()
