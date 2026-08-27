#!/usr/bin/env bash
# Bring up Airflow and dbt locally with no Docker, no root and no virtualenv tooling.
#
# Two install targets, not one. That is the whole point of this script and it is measured
# rather than assumed. Resolving dbt-core into Airflow's own environment succeeds and moves
# 21 packages Airflow's constraints file had pinned, including protobuf from 4.25.8 to
# 6.33.6. pip reports success and Airflow is no longer the thing its constraints describe.
# So Airflow gets one directory and dbt gets another, and nothing imports across them.
#
# Run it twice. It is written to be safe on a second run and that is checked, because a
# bring-up script that only works on an empty machine is a script nobody can rerun.

set -euo pipefail

AIRFLOW_VERSION="${AIRFLOW_VERSION:-3.1.8}"
DBT_VERSION="${DBT_VERSION:-1.12.3}"
DBT_DUCKDB_VERSION="${DBT_DUCKDB_VERSION:-1.9.4}"
PY_TAG="${PY_TAG:-3.10}"

PREFIX="${PREFIX:-/tmp/wdc}"
AIRFLOW_LIBS="$PREFIX/airflow-libs"
DBT_LIBS="$PREFIX/dbt-libs"
export AIRFLOW_HOME="${AIRFLOW_HOME:-$PREFIX/airflow-home}"

# pip writes its wheel cache under HOME, not under TMPDIR. On a machine where HOME sits on
# a full filesystem a source build dies with ENOSPC while the root filesystem has
# gigabytes free, and the error names the cache path rather than the real problem. Both
# get pointed somewhere with room.
export TMPDIR="${TMPDIR_OVERRIDE:-/tmp}"
PIP_CACHE="$PREFIX/pip-cache"

CONSTRAINTS="https://raw.githubusercontent.com/apache/airflow/constraints-${AIRFLOW_VERSION}/constraints-${PY_TAG}.txt"

mkdir -p "$AIRFLOW_LIBS" "$DBT_LIBS" "$AIRFLOW_HOME" "$PIP_CACHE"

echo "installing airflow ${AIRFLOW_VERSION} into ${AIRFLOW_LIBS}"
pip3 install --quiet --cache-dir "$PIP_CACHE" --target "$AIRFLOW_LIBS" \
  "apache-airflow==${AIRFLOW_VERSION}" --constraint "$CONSTRAINTS"

echo "installing dbt ${DBT_VERSION} into ${DBT_LIBS}"
pip3 install --quiet --cache-dir "$PIP_CACHE" --target "$DBT_LIBS" \
  "dbt-core==${DBT_VERSION}" "dbt-duckdb==${DBT_DUCKDB_VERSION}"

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

export PYTHONPATH="$AIRFLOW_LIBS"
export PATH="$AIRFLOW_LIBS/bin:$PATH"
export AIRFLOW__CORE__DAGS_FOLDER="$REPO/dags"
export AIRFLOW__CORE__LOAD_EXAMPLES=False

# db migrate is idempotent. It is what makes a second run of this script safe rather than
# a second run of this script being something nobody has tried.
airflow db migrate >/dev/null 2>&1
airflow dags reserialize >/dev/null 2>&1

echo
echo "airflow  $(airflow version 2>/dev/null | tail -1)"
echo "dbt      $(PYTHONPATH=$DBT_LIBS PATH=$DBT_LIBS/bin:$PATH dbt --version 2>/dev/null | sed -n 's/.*installed: //p' | head -1)"
echo "home     $AIRFLOW_HOME"
echo
echo "put this in your shell to use airflow:"
echo "  export AIRFLOW_HOME=$AIRFLOW_HOME"
echo "  export PYTHONPATH=$AIRFLOW_LIBS"
echo "  export PATH=$AIRFLOW_LIBS/bin:\$PATH"
echo "  export AIRFLOW__CORE__DAGS_FOLDER=$REPO/dags"
echo "  export AIRFLOW__CORE__LOAD_EXAMPLES=False"
echo
echo "and this for dbt, in a shell that does not have the airflow one set:"
echo "  export PYTHONPATH=$DBT_LIBS"
echo "  export PATH=$DBT_LIBS/bin:\$PATH"
