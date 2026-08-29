#!/usr/bin/env bash
# Run the DAG end to end against a real partition and assert that it really ran.
#
#   scripts/dag_smoke.sh [partition]
#
# tests/run_all.py deliberately needs nothing but requirements.txt, so nothing in it
# imports Airflow and the DAG is untested there. This is that gap.
#
# The assertion at the bottom is the whole script. `airflow dags test` on a date outside
# the DAG's own start and end window creates a run, executes no task at all, and reports
# state=success. A smoke test that greps for success passes on a run that did nothing, and
# it passes forever once the DAG's end_date falls behind the date the script uses.

set -euo pipefail

PARTITION="${1:-2025-01-14}"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PREFIX="${PREFIX:-/tmp/wdc}"

export AIRFLOW_HOME="${AIRFLOW_HOME:-$PREFIX/airflow-home}"
export PYTHONPATH="$PREFIX/airflow-libs:$PREFIX/libs"
export PATH="$PREFIX/airflow-libs/bin:$PATH"
export AIRFLOW__CORE__DAGS_FOLDER="$REPO/dags"
export AIRFLOW__CORE__LOAD_EXAMPLES=False

if ! command -v airflow >/dev/null 2>&1; then
  echo "airflow is not on PATH. run scripts/bootstrap-local.sh first" >&2
  exit 2
fi

LOG="$(mktemp)"
trap 'rm -f "$LOG"' EXIT

airflow dags reserialize >/dev/null 2>&1
airflow dags test nyc311_contract_check "$PARTITION" >"$LOG" 2>&1 || true

EXPECTED="pull check load_raw"
MISSING=""
for task in $EXPECTED; do
  if ! grep -q "end task task_id=$task" "$LOG"; then
    MISSING="$MISSING $task"
  fi
done

if [ -n "$MISSING" ]; then
  echo "these tasks never ran:$MISSING"
  echo "last 20 lines of the run:"
  tail -20 "$LOG"
  exit 1
fi

if grep -q "state=failed" "$LOG"; then
  echo "a task failed"
  tail -20 "$LOG"
  exit 1
fi

REPORT="$REPO/data/quarantine/created_date=$PARTITION/report.json"
if [ ! -f "$REPORT" ]; then
  echo "the run finished and wrote no report at $REPORT"
  exit 1
fi

echo "ran $PARTITION, tasks: $EXPECTED"
python3 - "$REPORT" <<'PY'
import json
import sys

with open(sys.argv[1]) as fh:
    r = json.load(fh)

# A report that judged nothing is the failure this whole repo keeps finding. Assert it
# here too rather than printing the number and moving on.
if r["rule_evaluations"] == 0:
    sys.exit("the run evaluated no rule")
if r["accepted"] + r["held"] != r["rows"]:
    sys.exit("the split lost rows")

print("{rows} rows, {accepted} accepted, {held} held, "
      "{rule_evaluations} rule evaluations".format(**r))
PY

# The DAG's own load task already refuses a count mismatch. Asking the database again from
# outside the run is what proves the table is really there, rather than that a task
# returned without raising.
PYTHONPATH="$PREFIX/libs" python3 - "$REPO" "$PARTITION" "$REPORT" <<'PY'
import json
import sys

sys.path.insert(0, sys.argv[1])
from contracts import spec
from warehouse import load

with open(sys.argv[3]) as fh:
    report = json.load(fh)

contract = spec.load(sys.argv[1] + "/contracts/nyc311.yml")
con = load.connect(sys.argv[1] + "/data/warehouse.duckdb")
rows = load.partition_rows(con, contract, sys.argv[2])
con.close()

if rows != report["accepted"]:
    sys.exit("the raw table holds {} rows for {}, the report accepted {}".format(
        rows, sys.argv[2], report["accepted"]))

print("raw layer holds {} rows for {}".format(rows, sys.argv[2]))
PY
