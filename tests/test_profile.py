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
from contracts import rules, spec, validate

CONTRACT = """
dataset: t
source:
  kind: socrata
  partition_column: created_date
  partition_grain: day
freshness:
  max_lag_hours: 48
  reference: extract
  applies_to: tail
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


def check_nothing_in_the_contract_format_is_unevaluated_now():
    # freshness and volume used to be here, in the file and read by nothing, which reads
    # like an enforced rule to anyone opening the contract. contracts/feed.py evaluates
    # both. The list stays because the next clause added will start out unread and this is
    # what says so.
    contract = spec.parse(CONTRACT)
    assert profile_mod.unevaluated_clauses(contract) == []


def check_a_clause_with_no_evaluator_is_still_named():
    # The check above passes on an empty list forever once the format stops growing, so it
    # cannot show that the naming works. This adds a clause nothing reads and asserts it
    # comes back.
    contract = spec.parse(CONTRACT)
    profile_mod.ALL_CLAUSES.add("retention")
    contract.retention = {"days": 30}
    try:
        assert profile_mod.unevaluated_clauses(contract) == ["retention"]
    finally:
        profile_mod.ALL_CLAUSES.discard("retention")


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


def check_two_rules_on_one_column_are_two_findings_and_not_two_rows():
    # The column view counts values per rule and has no way to notice that both counts
    # are about the same two rows. Neither 2 nor 4 is the number of bad rows and the
    # profile cannot tell you which. contracts/validate.py is where that question is
    # answered, and the check below pins the disagreement rather than leaving it to prose.
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
        rows = profile_mod.read_partition(path)
    finally:
        os.unlink(path)

    result = [r for r in p.results if r.column.name == "borough"][0]
    assert sorted(v.rule for v in result.violations) == ["allowed", "max_length"]
    assert sum(v.count for v in result.violations) == 4

    v = validate.validate(contract, rows)
    assert v.bad_rows == 2, v.failures
    assert v.sum_of_rule_counts() == 4


def check_the_shipped_contract_runs_against_the_fixture_partition():
    contract = spec.load(os.path.join(ROOT, "contracts", "nyc311.yml"))
    path = os.path.join(HERE, "fixtures", "sample_partition.csv")
    p = profile_mod.profile(contract, path, rules)
    assert p.rows == 500, p.rows
    assert len(p.results) == 11, len(p.results)
    assert len(p.checks) == 3
    assert p.uncontracted == [] and p.missing == []
