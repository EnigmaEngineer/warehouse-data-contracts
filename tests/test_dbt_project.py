"""Structural checks over the dbt project, with dbt not installed.

Nothing in this directory imports dbt. That is the same rule the rest of the suite follows
and it is why these read files rather than run anything. `dbt build` is the real check and
`scripts/dbt.sh` is how it runs.

The one that matters is the vocabulary check. The contract lists the statuses a request may
hold, and models/silver/_silver.yml lists them again for the dbt test. Two copies of one
list drift, and the drift is silent in the direction that matters: adding a status to the
contract and not to the yml leaves a dbt test refusing rows the contract accepts.

This is not the generated tests. Generating the dbt tests from the contract is the next
piece of work and it carries a trap the README names. Two implementations of one rule
cannot be graded against each other. Comparing two copies of one literal list can.
"""

import os
import re
import sys

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from contracts import spec

DBT = os.path.join(ROOT, "dbt")
MODELS = os.path.join(DBT, "models")


def model_files():
    out = {}
    for base, _, names in os.walk(MODELS):
        for name in names:
            if name.endswith(".sql"):
                out[name[:-len(".sql")]] = os.path.join(base, name)
    return out


def schema_docs():
    """Every model named in a models: block of any yml under models/."""
    named = {}
    for base, _, names in os.walk(MODELS):
        for name in names:
            if not name.endswith((".yml", ".yaml")):
                continue
            path = os.path.join(base, name)
            with open(path) as fh:
                doc = yaml.safe_load(fh) or {}
            for entry in doc.get("models") or []:
                named[entry["name"]] = entry
    return named


def sql_files():
    out = []
    for root in (MODELS, os.path.join(DBT, "snapshots"), os.path.join(DBT, "tests")):
        for base, _, names in os.walk(root):
            for name in names:
                if name.endswith(".sql"):
                    out.append(os.path.join(base, name))
    return out


def check_the_project_has_models_a_snapshot_and_singular_tests():
    """The zero input guard. Every check below passes on an empty directory."""
    models = model_files()
    assert len(models) >= 5, sorted(models)
    assert os.path.isfile(
        os.path.join(DBT, "snapshots", "snap_service_request.sql"))
    assert len(os.listdir(os.path.join(DBT, "tests"))) >= 2


def check_every_ref_points_at_a_model_that_exists():
    models = model_files()
    seen = 0
    for path in sql_files():
        with open(path) as fh:
            body = fh.read()
        for name in re.findall(r"ref\(\s*'([^']+)'\s*\)", body):
            seen += 1
            assert name in models, "{} refs {}, which is not a model".format(path, name)
    assert seen > 0, "no ref() was found anywhere, this check ran on nothing"


def check_every_source_names_a_declared_table():
    with open(os.path.join(MODELS, "staging", "_sources.yml")) as fh:
        doc = yaml.safe_load(fh)
    declared = set()
    for source in doc["sources"]:
        for table in source["tables"]:
            declared.add((source["name"], table["name"]))

    seen = 0
    for path in sql_files():
        with open(path) as fh:
            body = fh.read()
        for source, table in re.findall(
                r"source\(\s*'([^']+)'\s*,\s*'([^']+)'\s*\)", body):
            seen += 1
            assert (source, table) in declared, \
                "{} reads {}.{}, which no source declares".format(path, source, table)
    assert seen > 0, "no source() was found anywhere, this check ran on nothing"


def check_every_model_is_described_somewhere():
    """A model with no yml entry ships with no tests and no description and nobody notices."""
    models = model_files()
    described = schema_docs()
    missing = sorted(set(models) - set(described))
    assert not missing, "models with no schema entry: {}".format(missing)


def check_the_status_list_in_the_dbt_test_is_the_contract_s_list():
    """Two copies of one vocabulary. This is what stops them drifting apart.

    Sorted before comparing, because the order of a set of allowed values carries no
    meaning and a test that failed on reordering would be a test people turn off.
    """
    contract = spec.load(os.path.join(ROOT, "contracts", "nyc311.yml"))
    status = [c for c in contract.columns if c.name == "status"][0]
    from_contract = sorted(status.rules["allowed"])

    entry = schema_docs()["slv_service_requests"]
    values = None
    for column in entry["columns"]:
        if column["name"] != "status":
            continue
        for test in column.get("data_tests") or []:
            if isinstance(test, dict) and "accepted_values" in test:
                values = test["accepted_values"]["arguments"]["values"]
    assert values is not None, "no accepted_values test on slv_service_requests.status"
    assert sorted(values) == from_contract, (sorted(values), from_contract)


def check_the_snapshot_tracks_columns_the_model_it_reads_selects():
    """check_cols naming a column the source model does not have never fires.

    dbt would raise on it at run time. This says so without dbt, which is the point of the
    file, and it is the same shape as a rule pointed at a column the contract lacks.
    """
    path = os.path.join(DBT, "snapshots", "snap_service_request.sql")
    with open(path) as fh:
        body = fh.read()
    cols = re.search(r"check_cols=\[([^\]]+)\]", body).group(1)
    tracked = re.findall(r"'([^']+)'", cols)
    assert tracked, "the snapshot tracks no column"

    with open(model_files()["stg_service_requests"]) as fh:
        model = fh.read()
    for column in tracked:
        assert re.search(r"\b{}\b".format(re.escape(column)), model), \
            "the snapshot tracks {}, which stg_service_requests does not select".format(
                column)
