"""Profiling a partition, and the failure mode that made the first run look perfect.

The check worth reading here is check_a_profile_that_evaluates_nothing_raises. The first
version of contracts/profile.py compared Column objects against header strings when
working out which contracted columns were absent, so every column read as missing, every
rule was skipped and the report printed zero violations over 179,314 rows of real
municipal data. The output looked like a clean feed. Nothing in it said the profile had
checked nothing.
"""

import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from contracts import profile as profile_mod
from contracts import rules, spec

CONTRACT = """
dataset: t
source:
  kind: socrata
  partition_column: created_date
  partition_grain: day
freshness:
  max_lag_hours: 48
  provenance: asserted
volume:
  min_rows_per_partition: 1
  provenance: asserted
checks:
  - name: closed_after_created
    kind: ordering
    before: created_date
    after: closed_date
    provenance: asserted
columns:
  - name: created_date
    type: timestamp
    provenance: documented
    required: true
  - name: closed_date
    type: timestamp
    provenance: asserted
    required: false
  - name: borough
    type: string
    provenance: asserted
    allowed: [BRONX, QUEENS]
"""

CLEAN = """created_date,closed_date,borough
2025-01-01T00:00:00.000,2025-01-02T00:00:00.000,BRONX
2025-01-01T01:00:00.000,,QUEENS
"""

DIRTY = """created_date,closed_date,borough
2025-01-01T00:00:00.000,2025-01-02T00:00:00.000,BRONX
2025-01-05T00:00:00.000,2025-01-02T00:00:00.000,ATLANTIS
"""

WRONG_HEADERS = """CreatedDate,ClosedDate,Borough
2025-01-01T00:00:00.000,2025-01-02T00:00:00.000,BRONX
"""


def _write(text):
    fh = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, newline="")
    fh.write(text)
    fh.close()
    return fh.name


def _profile(csv_text):
    contract = spec.parse(CONTRACT)
    path = _write(csv_text)
    try:
        return profile_mod.profile(contract, path, rules)
    finally:
        os.unlink(path)


def check_a_clean_partition_reports_no_violations():
    p = _profile(CLEAN)
    assert p.rows == 2
    assert p.violated() == []
    assert all(c.count == 0 for c in p.checks)


def check_a_clean_partition_still_evaluated_every_column():
    # The pair to the check above. "No violations" and "no columns" print the same and
    # mean opposite things, so the count of what ran is asserted separately.
    p = _profile(CLEAN)
    assert len(p.results) == 3, len(p.results)
    assert p.missing == [], p.missing
    assert len(p.checks) == 1


def check_a_dirty_partition_reports_the_column_rule_and_the_cross_column_check():
    p = _profile(DIRTY)
    broken = {r.column.name: [v.rule for v in r.violations] for r in p.violated()}
    assert broken == {"borough": ["allowed"]}, broken
    assert sum(c.count for c in p.checks) == 1


def check_a_profile_that_evaluates_nothing_raises():
    try:
        _profile(WRONG_HEADERS)
    except profile_mod.NothingChecked as exc:
        assert "no contracted column" in str(exc)
        return
    raise AssertionError("a profile over zero columns has to be a failure")


def check_the_unenforced_clauses_are_named():
    # freshness and volume are in every contract this repo loads and nothing reads either
    # of them. A clause with no evaluator behind it reads like an enforced rule to anyone
    # opening the file, so the report has to say so.
    contract = spec.parse(CONTRACT)
    assert profile_mod.unevaluated_clauses(contract) == ["freshness", "volume"]


def check_the_clause_list_covers_every_top_level_clause():
    # The pair to the check above. If a clause is added to the contract format and not to
    # ALL_CLAUSES, it becomes invisible to the report rather than being named as
    # unenforced, which is worse than the problem this is meant to solve.
    contract = spec.load(os.path.join(ROOT, "contracts", "nyc311.yml"))
    described = profile_mod.ALL_CLAUSES
    for name in ("columns", "checks", "freshness", "volume"):
        assert getattr(contract, name), name
        assert name in described, name
    assert profile_mod.EVALUATED_CLAUSES <= described


def check_a_column_in_the_data_with_no_contract_is_reported():
    p = _profile(CLEAN.replace("borough", "borough,extra").replace(
        ",BRONX", ",BRONX,1").replace(",QUEENS", ",QUEENS,2"))
    assert p.uncontracted == ["extra"], p.uncontracted


def check_worst_is_zero_on_a_column_with_no_violations():
    # max() over an empty sequence needs a default and the default has to be 0. A default
    # of 1 makes every clean column report one bad row, which reads as a feed that is
    # slightly broken everywhere.
    p = _profile(CLEAN)
    assert all(r.worst == 0 for r in p.results), [(r.column.name, r.worst) for r in p.results]


def check_worst_is_the_largest_single_rule_and_not_a_sum():
    # Two rules failing on one column can be the same row twice. Summing them overstates
    # how many rows are bad, and the honest cheap answer is a lower bound.
    contract = spec.parse(CONTRACT.replace(
        "    allowed: [BRONX, QUEENS]",
        "    allowed: [BRONX, QUEENS]\n    max_length: 6"))
    path = _write("""created_date,closed_date,borough
2025-01-01T00:00:00.000,,ATLANTIS
2025-01-01T00:00:00.000,,LEMURIA
2025-01-01T00:00:00.000,,BRONX
""")
    try:
        p = profile_mod.profile(contract, path, rules)
    finally:
        os.unlink(path)
    result = [r for r in p.results if r.column.name == "borough"][0]
    assert sorted(v.rule for v in result.violations) == ["allowed", "max_length"]
    assert result.worst == 2, result.worst


def check_the_shipped_contract_runs_against_the_fixture_partition():
    contract = spec.load(os.path.join(ROOT, "contracts", "nyc311.yml"))
    path = os.path.join(HERE, "fixtures", "sample_partition.csv")
    p = profile_mod.profile(contract, path, rules)
    assert p.rows == 500, p.rows
    assert len(p.results) == 11, len(p.results)
    assert len(p.checks) == 3
    assert p.uncontracted == [] and p.missing == []
