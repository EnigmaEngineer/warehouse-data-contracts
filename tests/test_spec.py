"""The contract loader has to refuse a bad contract.

A malformed contract is more dangerous than an absent one, because the pipeline keeps
running and the rule that was meant to fire silently never does. So most of these checks
are about refusal rather than about a happy path.
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from contracts import spec

GOOD = """
dataset: t
source:
  kind: socrata
  partition_column: created_date
  partition_grain: day
freshness:
  max_lag_hours: 48
  provenance: asserted
volume:
  min_rows_per_partition: 10
  provenance: asserted
columns:
  - name: created_date
    type: timestamp
    provenance: documented
    required: true
  - name: amount
    type: number
    provenance: asserted
    min: 0
    max: 10
"""


def _refuses(text, fragment):
    try:
        spec.parse(text)
    except spec.ContractError as exc:
        assert fragment in str(exc), "wrong reason: {}".format(exc)
        return
    raise AssertionError("expected a refusal mentioning {!r}".format(fragment))


def check_a_good_contract_loads():
    c = spec.parse(GOOD)
    assert c.dataset == "t"
    assert len(c.columns) == 2
    assert c.constraint_count() == 3
    assert c.asserted_count() == 2


def check_an_unknown_rule_name_is_refused():
    # The failure this is really about. `requred: true` is a typo that a lenient loader
    # ignores, and the column then has no required rule at all.
    _refuses(GOOD.replace("required: true", "requred: true"), "unknown rule 'requred'")


def check_a_rule_of_the_wrong_type_is_refused():
    _refuses(GOOD.replace("min: 0", "min: zero"), "should be")


def check_a_column_with_no_constraints_is_refused():
    text = GOOD + "\n  - name: spare\n    type: string\n    provenance: asserted\n"
    _refuses(text, "carries no constraints")


def check_a_missing_provenance_is_refused():
    _refuses(GOOD.replace("    provenance: documented\n", "", 1), "missing 'provenance'")


def check_an_invented_provenance_is_refused():
    _refuses(GOOD.replace("provenance: asserted", "provenance: obvious", 1), "provenance")


def check_min_above_max_is_refused():
    _refuses(GOOD.replace("min: 0", "min: 99"), "min above max")


def check_min_equal_to_max_is_allowed():
    # A column pinned to one value is a real contract, not a mistake. Refusing it here
    # would be the loader having an opinion the format does not.
    c = spec.parse(GOOD.replace("min: 0", "min: 10"))
    amount = [x for x in c.columns if x.name == "amount"][0]
    assert amount.rules["min"] == amount.rules["max"] == 10


def check_a_duplicate_column_is_refused():
    text = GOOD + "\n  - name: amount\n    type: number\n    provenance: asserted\n    min: 1\n"
    _refuses(text, "appears twice")


def check_a_partition_column_outside_the_contract_is_refused():
    _refuses(GOOD.replace("partition_column: created_date", "partition_column: nope"),
             "partition column")


def check_an_unknown_type_is_refused():
    _refuses(GOOD.replace("type: number", "type: decimal"), "unknown type")


def check_a_check_pointed_at_an_undefined_column_is_refused():
    text = GOOD + """
checks:
  - name: c
    kind: ordering
    before: created_date
    after: settled_date
    provenance: asserted
"""
    _refuses(text, "which the contract does not define")


def check_an_unknown_check_kind_is_refused():
    text = GOOD + """
checks:
  - name: c
    kind: sorcery
    before: created_date
    after: amount
    provenance: asserted
"""
    _refuses(text, "unknown kind")


def check_a_conditional_check_needs_a_when_clause():
    text = GOOD + """
checks:
  - name: c
    kind: requires_when
    when_column: created_date
    then_required: amount
    provenance: asserted
"""
    _refuses(text, "needs when_equals or when_in")


def check_a_conditional_check_refuses_both_when_clauses():
    text = GOOD + """
checks:
  - name: c
    kind: requires_when
    when_column: created_date
    when_equals: x
    when_in: [x]
    then_required: amount
    provenance: asserted
"""
    _refuses(text, "both when_equals and when_in")


def check_checks_are_optional():
    c = spec.parse(GOOD)
    assert c.checks == []


def check_the_shipped_contract_loads():
    c = spec.load(os.path.join(ROOT, "contracts", "nyc311.yml"))
    assert c.dataset == "nyc311_service_requests"
    assert len(c.columns) == 11
    assert len(c.checks) == 3
    # If this moves, the numbers in the README move with it. Pinned so a rule added
    # without updating the README fails here rather than making the README wrong.
    assert c.constraint_count() == 20, c.constraint_count()
    assert c.asserted_count() == 13, c.asserted_count()
