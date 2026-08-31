"""Load a contract file and refuse it if it is malformed.

A contract is the only place in this repo that says what good data looks like. Everything
downstream reads it. So the loader is strict on purpose. A contract with a typo in a rule
name is worse than an absent contract. The pipeline keeps running and the rule silently
never fires.

The one design rule worth explaining. Every constraint carries a `provenance` field and the
loader refuses a constraint without one. `documented` means the publisher of the source
says so. `asserted` means I decided it and the source never promised it. Without that split
there is no way to tell a contract that describes the data from one written by looking at
the data, and the second kind cannot be violated by definition.
"""

import yaml

from contracts import feed

TYPES = {"string", "integer", "number", "timestamp"}
PROVENANCE = {"documented", "asserted"}

# Rules a column may carry, and the python type each one's value must have. Anything not
# in here is a typo and gets refused rather than ignored.
COLUMN_RULES = {
    "required": bool,
    "unique": bool,
    "min": (int, float),
    "max": (int, float),
    "allowed": list,
    "max_length": int,
    "matches": str,
}


class ContractError(ValueError):
    """The contract document itself is wrong. Not the data."""


# Cross column checks. A column rule can only ever see one value at a time, and the
# defects in a real feed are usually relationships. Each kind names the columns it reads
# so the loader can refuse a check pointed at a column the contract does not have.
CHECK_KINDS = {
    "ordering": ("before", "after"),
    "requires_when": ("when_column", "then_required"),
    "forbids_when": ("when_column", "then_absent"),
}


class Check:
    def __init__(self, name, kind, provenance, note, args):
        self.name = name
        self.kind = kind
        self.provenance = provenance
        self.note = note
        self.args = args

    def columns(self):
        out = []
        for key in CHECK_KINDS[self.kind]:
            out.append(self.args[key])
        return out

    def __repr__(self):
        return "Check({!r}, {})".format(self.name, self.kind)


class Column:
    def __init__(self, name, type_, rules, provenance, note):
        self.name = name
        self.type = type_
        self.rules = rules
        self.provenance = provenance
        self.note = note

    def __repr__(self):
        return "Column({!r}, {}, {})".format(self.name, self.type, sorted(self.rules))


class Contract:
    def __init__(self, dataset, source, freshness, volume, columns, checks):
        self.dataset = dataset
        self.source = source
        self.freshness = freshness
        self.volume = volume
        self.columns = columns
        self.checks = checks

    def constraint_count(self):
        return sum(len(c.rules) for c in self.columns)

    def asserted_count(self):
        return sum(len(c.rules) for c in self.columns if c.provenance == "asserted")


def _require(mapping, key, where):
    if key not in mapping:
        raise ContractError("{} is missing '{}'".format(where, key))
    return mapping[key]


def _column(raw, index):
    where = "column {}".format(index)
    if not isinstance(raw, dict):
        raise ContractError("{} is not a mapping".format(where))

    name = _require(raw, "name", where)
    where = "column '{}'".format(name)

    type_ = _require(raw, "type", where)
    if type_ not in TYPES:
        raise ContractError("{} has unknown type '{}'".format(where, type_))

    provenance = _require(raw, "provenance", where)
    if provenance not in PROVENANCE:
        raise ContractError(
            "{} has provenance '{}', expected one of {}".format(
                where, provenance, sorted(PROVENANCE)
            )
        )

    note = raw.get("note", "")
    rules = {}
    for key, value in raw.items():
        if key in ("name", "type", "provenance", "note"):
            continue
        if key not in COLUMN_RULES:
            raise ContractError("{} has unknown rule '{}'".format(where, key))
        expected = COLUMN_RULES[key]
        if not isinstance(value, expected):
            raise ContractError(
                "{} rule '{}' should be {}, got {}".format(
                    where, key, expected, type(value).__name__
                )
            )
        rules[key] = value

    if "min" in rules and "max" in rules and rules["min"] > rules["max"]:
        raise ContractError("{} has min above max".format(where))

    if not rules:
        # A column with no rules is a column nobody has thought about. It reads as covered
        # in a table of contents and it is not.
        raise ContractError("{} carries no constraints".format(where))

    return Column(name, type_, rules, provenance, note)


def _check(raw, index, column_names):
    where = "check {}".format(index)
    if not isinstance(raw, dict):
        raise ContractError("{} is not a mapping".format(where))

    name = _require(raw, "name", where)
    where = "check '{}'".format(name)

    kind = _require(raw, "kind", where)
    if kind not in CHECK_KINDS:
        raise ContractError(
            "{} has unknown kind '{}', expected one of {}".format(
                where, kind, sorted(CHECK_KINDS)
            )
        )

    provenance = _require(raw, "provenance", where)
    if provenance not in PROVENANCE:
        raise ContractError("{} has provenance '{}'".format(where, provenance))

    args = {}
    for key in CHECK_KINDS[kind]:
        args[key] = _require(raw, key, where)

    if kind in ("requires_when", "forbids_when"):
        args["when_equals"] = raw.get("when_equals")
        args["when_in"] = raw.get("when_in")
        if args["when_equals"] is None and args["when_in"] is None:
            raise ContractError(
                "{} needs when_equals or when_in".format(where)
            )
        if args["when_equals"] is not None and args["when_in"] is not None:
            raise ContractError("{} sets both when_equals and when_in".format(where))
        if args["when_in"] is not None and not isinstance(args["when_in"], list):
            raise ContractError("{} when_in should be a list".format(where))

    for col in (args[k] for k in CHECK_KINDS[kind]):
        if col not in column_names:
            raise ContractError(
                "{} reads column '{}' which the contract does not define".format(
                    where, col
                )
            )

    return Check(name, kind, provenance, raw.get("note", ""), args)


def parse(text):
    raw = yaml.safe_load(text)
    if not isinstance(raw, dict):
        raise ContractError("contract is not a mapping")

    dataset = _require(raw, "dataset", "contract")
    source = _require(raw, "source", "contract")
    for key in ("kind", "partition_column", "partition_grain"):
        _require(source, key, "source")

    freshness = _require(raw, "freshness", "contract")
    _require(freshness, "max_lag_hours", "freshness")
    _require(freshness, "provenance", "freshness")
    if freshness["provenance"] not in PROVENANCE:
        raise ContractError("freshness provenance is not documented or asserted")

    # A lag is a difference and a clause naming only one side of it is not a rule. These
    # two fields are refused when absent for the same reason provenance is. A contract that
    # omits them still reads like it says something.
    reference = _require(freshness, "reference", "freshness")
    if reference not in feed.REFERENCES:
        raise ContractError(
            "freshness reference is '{}', expected one of {}".format(
                reference, sorted(feed.REFERENCES)))

    applies_to = _require(freshness, "applies_to", "freshness")
    if applies_to not in feed.SCOPES:
        raise ContractError(
            "freshness applies_to is '{}', expected one of {}".format(
                applies_to, sorted(feed.SCOPES)))

    volume = _require(raw, "volume", "contract")
    _require(volume, "min_rows_per_partition", "volume")
    _require(volume, "provenance", "volume")
    if volume["provenance"] not in PROVENANCE:
        raise ContractError("volume provenance is not documented or asserted")

    raw_columns = _require(raw, "columns", "contract")
    if not raw_columns:
        raise ContractError("contract has no columns")

    columns = [_column(c, i) for i, c in enumerate(raw_columns)]

    seen = set()
    for c in columns:
        if c.name in seen:
            raise ContractError("column '{}' appears twice".format(c.name))
        seen.add(c.name)

    partition_column = source["partition_column"]
    if partition_column not in seen:
        raise ContractError(
            "partition column '{}' is not in the contract".format(partition_column)
        )

    raw_checks = raw.get("checks") or []
    checks = [_check(c, i, seen) for i, c in enumerate(raw_checks)]
    check_names = set()
    for c in checks:
        if c.name in check_names:
            raise ContractError("check '{}' appears twice".format(c.name))
        check_names.add(c.name)

    return Contract(dataset, source, freshness, volume, columns, checks)


def load(path):
    with open(path) as fh:
        return parse(fh.read())
