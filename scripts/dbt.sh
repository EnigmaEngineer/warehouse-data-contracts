#!/usr/bin/env bash
# Run dbt against this project with its own interpreter path.
#
#   scripts/dbt.sh run
#   scripts/dbt.sh test
#   scripts/dbt.sh snapshot
#   WDC_DUCKDB=/tmp/wh.duckdb scripts/dbt.sh build
#
# dbt gets its own PYTHONPATH and nothing else is on it. Resolving dbt into the Airflow
# install succeeds and moves 21 packages Airflow's constraints file had pinned, so the two
# live in separate directories and a shell that has one set must not have the other. This
# script is how a DAG task or a person reaches dbt without arranging that by hand.

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PREFIX="${PREFIX:-/tmp/wdc}"
DBT_LIBS="${DBT_LIBS:-$PREFIX/dbt-libs}"

if [ ! -x "$DBT_LIBS/bin/dbt" ]; then
  echo "no dbt at $DBT_LIBS/bin/dbt. run scripts/bootstrap-local.sh first" >&2
  exit 2
fi

# Deliberately assigned rather than prepended. Airflow's directory must not leak in here.
export PYTHONPATH="$DBT_LIBS"
export PATH="$DBT_LIBS/bin:$PATH"
export WDC_DUCKDB="${WDC_DUCKDB:-$REPO/data/warehouse.duckdb}"

cd "$REPO/dbt"
exec dbt "$@" --profiles-dir "$REPO/dbt"
