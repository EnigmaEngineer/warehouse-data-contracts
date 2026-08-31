"""Checks over the dbt test generation and, mostly, over its coverage number.

The coverage figure is the point of the module and it is the easiest thing here to fake
without meaning to. A denominator counted off a list kept beside the mapper grades what its
author remembered. So the first check below drives itself off `spec.COLUMN_RULES`, which is
the list the loader uses to decide whether a rule exists at all. Add a rule to the contract
format and forget the mapper, and that check fails rather than the coverage quietly
improving.
"""

import os
import sys

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from contracts import generate, spec

CONTRACT = os.path.join(ROOT, "contracts", "nyc311.yml")
GENERATED = os.path.join(ROOT, "dbt", "models", "staging", "_generated.yml")

STUB = """
dataset: stub
source:
  kind: socrata
  domain: example.invalid
  resource: aaaa-bbbb
  partition_column: created_date
  partition_grain: day
freshness:
  max_lag_hours: 48
  reference: extract
  applies_to: tail
  provenance: asserted
volume:
  min_rows_per_partition: 10
  provenance: asserted
columns:
  - name: created_date
    type: timestamp
    provenance: documented
    required: true
  - name: subject
    type: string
    provenance: asserted
    {rule}
"""


def contract():
    return spec.load(CONTRACT)


def check_every_rule_the_contract_format_allows_has_an_outcome():
    """Driven off the loader's own rule list, not off a list of fixtures beside it."""
    samples = {
        "required": "required: true",
        "unique": "unique: true",
        "allowed": "allowed: [a, b]",
        "min": "min: 1",
        "max": "max: 9",
        "max_length": "max_length: 4",
        "matches": "matches: '^a'",
    }
    assert set(samples) == set(spec.COLUMN_RULES), (
        "the contract format has moved and this list has not: {}".format(
            sorted(set(samples) ^ set(spec.COLUMN_RULES))))

    for rule, line in samples.items():
        c = spec.parse(STUB.format(rule=line))
        mapped = [m for m in generate.map_contract(c) if m.subject == "subject"]
        assert len(mapped) == 1, (rule, mapped)
        assert mapped[0].outcome in (
            generate.GENERATED, generate.NO_DBT_EQUIVALENT,
            generate.NOTHING_TO_GENERATE), (rule, mapped[0].outcome)


def check_the_denominator_is_every_constraint_and_every_check():
    c = contract()
    mapped = generate.map_contract(c)
    assert len(mapped) == c.constraint_count() + len(c.checks)
    assert len(mapped) == 23, len(mapped)


def check_the_three_outcomes_partition_the_constraints():
    """If they did not sum, a constraint would be falling out of the count unseen."""
    mapped = generate.map_contract(contract())
    counts = generate.coverage(mapped)
    assert sum(counts.values()) == len(mapped), counts


def check_a_nullable_column_generates_no_not_null_test():
    """`required: false` asserts nothing. Emitting not_null for it would refuse every open
    request in the feed, which is the shape of guardrail that gets switched off."""
    c = spec.parse(STUB.format(rule="required: false"))
    mapped = [m for m in generate.map_contract(c) if m.subject == "subject"]
    assert mapped[0].outcome == generate.NOTHING_TO_GENERATE, mapped[0].outcome
    assert mapped[0].test is None

    doc = generate.schema_document(c, "contract", "raw", "t", generate.map_contract(c))
    names = [col["name"] for col in doc["sources"][0]["tables"][0]["columns"]]
    assert "subject" not in names, names


def check_a_required_column_generates_not_null():
    c = spec.parse(STUB.format(rule="required: true"))
    mapped = [m for m in generate.map_contract(c) if m.subject == "subject"]
    assert mapped[0].outcome == generate.GENERATED
    assert mapped[0].test == "not_null"


def check_a_range_rule_is_reported_as_having_no_equivalent_rather_than_dropped():
    c = spec.parse(STUB.format(rule="min: 1"))
    mapped = [m for m in generate.map_contract(c) if m.subject == "subject"]
    assert mapped[0].outcome == generate.NO_DBT_EQUIVALENT
    assert mapped[0].why, "a constraint that did not make it says nothing about why"


def check_every_cross_column_check_lands_in_the_unmapped_pile():
    c = contract()
    assert c.checks, "the contract has no checks, so this ran on nothing"
    mapped = generate.map_contract(c)
    names = {k.name for k in c.checks}
    for m in mapped:
        if m.subject in names:
            assert m.outcome == generate.NO_DBT_EQUIVALENT, (m.subject, m.outcome)


def check_the_accepted_values_test_carries_the_contract_s_list():
    c = contract()
    status = [col for col in c.columns if col.name == "status"][0]
    doc = generate.schema_document(c, "contract", "raw", "t", generate.map_contract(c))
    columns = {col["name"]: col for col in doc["sources"][0]["tables"][0]["columns"]}
    values = None
    for test in columns["status"]["data_tests"]:
        if isinstance(test, dict):
            values = test["accepted_values"]["arguments"]["values"]
    assert values == status.rules["allowed"], (values, status.rules["allowed"])


def check_no_generated_column_carries_an_empty_test_list():
    """A column entry with no tests is a description dbt will not act on, and it makes the
    generated file look like it covers more than it does."""
    c = contract()
    doc = generate.schema_document(c, "contract", "raw", "t", generate.map_contract(c))
    for col in doc["sources"][0]["tables"][0]["columns"]:
        assert col["data_tests"], col["name"]


def check_the_generated_source_does_not_reuse_the_hand_written_source_name():
    """dbt refuses one source name declared twice. Sharing it would mean the generator
    owning the ledger table and the descriptions, in a file whose header says do not edit.
    """
    text = generate.render(contract(), "nyc311.yml", "contract", "raw", "t")
    doc = yaml.safe_load(text)
    assert doc["sources"][0]["name"] == "contract"
    assert doc["sources"][0]["schema"] == "raw"

    with open(os.path.join(ROOT, "dbt", "models", "staging", "_sources.yml")) as fh:
        hand = yaml.safe_load(fh)
    hand_names = {s["name"] for s in hand["sources"]}
    assert "contract" not in hand_names, hand_names


def check_the_header_quotes_the_coverage_the_mapper_computed():
    """The header is prose in a committed file and prose is where a stale number hides."""
    c = contract()
    counts = generate.coverage(generate.map_contract(c))
    text = generate.render(c, "nyc311.yml", "contract", "raw", "t")
    head = text.split("version: 2")[0]
    assert "{} of {}".format(counts[generate.GENERATED], 23) in head, head


def check_the_committed_file_matches_what_the_contract_generates_now():
    """The drift check. Generation moves the two copies problem rather than removing it,
    and this is where it now lives."""
    with open(GENERATED) as fh:
        current = fh.read()
    fresh = generate.render(contract(), "nyc311.yml", "contract", "raw",
                            "nyc311_service_requests")
    assert current == fresh, "dbt/models/staging/_generated.yml is stale"
