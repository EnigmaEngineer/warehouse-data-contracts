"""Turn a contract into dbt schema tests, and say what could not be turned.

The obvious objection to generating these is that the validator already implements every
rule, so a generated dbt test is a second implementation of the same rule and grading one
against the other proves nothing.

That objection is wrong here and the reason is the subject rather than the rule. The
validator reads the CSV before the load. dbt reads the table after it. This repo has already
lost two arguments in the gap between those two. A zip starting with a zero passes the five
digit rule on the file and fails it in a table whose reader chose an integer, and a hive
directory name overwrote the column it names on every row without a single count moving. So
one rule evaluated at two points is a check with something to say, and the honest name for
it is a file against table agreement check rather than redundancy.

The other half of the answer is the coverage number below. dbt core ships four generic
tests. This contract carries twenty three constraints and nine of them survive the trip.
Every rule that reads the shape of a value has no equivalent at all, and neither does any
rule that reads two columns. "The contract generates the tests" is a sentence that would
have been true of nine of twenty three, and the count is the reason it is not written that
way anywhere in this repo.

The denominator is derived from the contract rather than from a list kept beside it. A
coverage figure counted off a hand written list grades what its author remembered.
"""

GENERATED = "generated"
NO_DBT_EQUIVALENT = "no_dbt_equivalent"
NOTHING_TO_GENERATE = "nothing_to_generate"

# dbt core's generic tests, in full. Nothing here installs a package, so this is the whole
# vocabulary available. relationships is in core too and no contract rule produces one,
# because the contract has no foreign key concept.
CORE_TESTS = ("not_null", "unique", "accepted_values", "relationships")


class Mapped:
    """One contract constraint and what became of it."""

    def __init__(self, subject, rule, outcome, test=None, why=""):
        self.subject = subject
        self.rule = rule
        self.outcome = outcome
        self.test = test
        self.why = why

    def __repr__(self):
        return "Mapped({}.{}, {})".format(self.subject, self.rule, self.outcome)


def _column_rule(column, rule, value):
    if rule == "required":
        if not value:
            return Mapped(column.name, rule, NOTHING_TO_GENERATE,
                          why="the column is nullable, so there is nothing to assert")
        return Mapped(column.name, rule, GENERATED, test="not_null")

    if rule == "unique":
        if not value:
            return Mapped(column.name, rule, NOTHING_TO_GENERATE,
                          why="uniqueness is not claimed")
        return Mapped(column.name, rule, GENERATED, test="unique")

    if rule == "allowed":
        return Mapped(column.name, rule, GENERATED,
                      test={"accepted_values": {"arguments": {"values": list(value)}}})

    return Mapped(column.name, rule, NO_DBT_EQUIVALENT,
                  why="dbt core has no generic test that reads the shape of a value")


def map_contract(contract):
    """Every constraint in the contract, with its outcome. This is the denominator."""
    out = []
    for column in contract.columns:
        for rule in sorted(column.rules):
            out.append(_column_rule(column, rule, column.rules[rule]))
    for check in contract.checks:
        out.append(Mapped(check.name, "check:" + check.kind, NO_DBT_EQUIVALENT,
                          why="a generic test reads one column and this reads two"))
    return out


def coverage(mapped):
    counts = {GENERATED: 0, NO_DBT_EQUIVALENT: 0, NOTHING_TO_GENERATE: 0}
    for m in mapped:
        counts[m.outcome] += 1
    return counts


def schema_document(contract, source_name, schema, table_name, mapped):
    """The dbt sources document, as plain data ready for yaml.safe_dump.

    Tests land on the source rather than on a model on purpose. The source table holds the
    contract's own column names and every value is still text, so it is the same subject the
    validator judged. A staging model has already cast and renamed, and a test there would
    be reading the cast rather than the data.

    The source gets its own name over the same physical table rather than adding tests to
    the hand written one. dbt refuses a source name declared in two files, so sharing it
    would mean this generator owning descriptions and a ledger table that nothing generates.
    A generated file that carries hand written content is a file people edit.
    """
    by_column = {}
    for m in mapped:
        if m.outcome != GENERATED:
            continue
        by_column.setdefault(m.subject, []).append(m.test)

    columns = []
    for column in contract.columns:
        tests = by_column.get(column.name)
        if not tests:
            continue
        columns.append({"name": column.name, "data_tests": tests})

    return {
        "version": 2,
        "sources": [{
            "name": source_name,
            "schema": schema,
            "tables": [{"name": table_name, "columns": columns}],
        }],
    }


HEADER = """# Generated from contracts/{contract} by scripts/generate_dbt_tests.py. Do not edit.
#
# {generated} of {total} contract constraints reach dbt. {no_equivalent} have no generic
# test in dbt core and stay in the validator, and {nothing} assert nothing to begin with.
# The ones that made it are presence, uniqueness and vocabulary. Every rule that reads the
# shape of a value and every rule that reads two columns is in the other pile.
#
# These run against the table and the validator ran against the file. Same rules, two
# subjects, and this repo has already found two defects living in the gap between them.
"""


def render(contract, contract_filename, source_name, schema, table_name):
    import yaml

    mapped = map_contract(contract)
    counts = coverage(mapped)
    doc = schema_document(contract, source_name, schema, table_name, mapped)

    header = HEADER.format(
        contract=contract_filename,
        generated=counts[GENERATED],
        total=len(mapped),
        no_equivalent=counts[NO_DBT_EQUIVALENT],
        nothing=counts[NOTHING_TO_GENERATE],
    )
    body = yaml.safe_dump(doc, sort_keys=False, default_flow_style=False, width=100)
    return header + "\n" + body
