"""Rerun a range of partitions, and be honest about the two things a rerun cannot do.

`load.load_partition` already makes one partition safe to reload. It deletes on the
partition key and inserts inside a transaction, so the first load and the tenth leave the
same rows. That is idempotency for one partition and it is not a backfill.

A backfill is a range, and a range brings two problems the single load does not have.

The first is gaps. A loop over whatever directories happen to exist will backfill thirteen
of fourteen days and print a success. So `plan` takes a start and an end, expands the range
day by day, and refuses when a day in it has not been judged. A backfill that silently skips
a day is worse than one that fails, because the hole is discovered by an analyst later.

The second is that a range is not one transaction. Each partition commits on its own, so a
failure at the seventh of fourteen leaves six replaced and eight as they were. That is not
fixable by wrapping the loop, because a single transaction over the whole range would hold
every partition's delete open until the last insert and a big backfill would then be one
long lock with nothing recoverable in it. So the loop keeps its per partition commits and
`BackfillStopped` carries the list of partitions that did land. An operator who knows where
it stopped can resume. An operator who only knows it failed cannot.

There is a third thing and it is the one that surprised me. Delete then insert converges a
partition on its source, and it can never remove a partition the source no longer has. A
range that no longer covers a day leaves that day in the table forever, still reconciling
against the ledger, still feeding the marts. `orphans` is the only thing here that finds it,
and finding it is all it does. Deleting on the strength of an absent directory would be a
pipeline that empties the warehouse the first time a fetch fails.
"""

import datetime

from warehouse import load, schema


class MissingPartitions(ValueError):
    """The range covers a day that nothing has judged."""

    def __init__(self, missing):
        self.missing = list(missing)
        super().__init__(
            "no judged directory for {}, backfilling around a gap would "
            "publish a success with a hole in it".format(self.missing)
        )


class BackfillStopped(RuntimeError):
    """A partition failed. Carries what landed before it, so a resume is possible."""

    def __init__(self, partition, completed, cause):
        self.partition = partition
        self.completed = list(completed)
        self.cause = cause
        super().__init__(
            "stopped at {} after {} partitions committed ({}): {}".format(
                partition, len(self.completed), ", ".join(self.completed) or "none",
                cause)
        )


def days(start, end):
    """Every date from start to end inclusive, as strings. Refuses a reversed range."""
    first = datetime.date.fromisoformat(start)
    last = datetime.date.fromisoformat(end)
    if last < first:
        raise ValueError("end {} is before start {}".format(end, start))
    out = []
    day = first
    while day <= last:
        out.append(day.isoformat())
        day += datetime.timedelta(days=1)
    return out


def plan(judged, start, end):
    """The ordered work for a range, or a refusal. Returns [(partition, directory)].

    `judged` maps partition to directory. The range decides the work, not the directory
    listing, which is the whole reason this function exists.
    """
    wanted = days(start, end)
    missing = [d for d in wanted if d not in judged]
    if missing:
        raise MissingPartitions(missing)

    return [(d, judged[d]) for d in wanted]


def run(con, contract, work, sha_for, now=None):
    """Load every partition in `work`, in order. Returns a summary.

    `sha_for` is a callable taking a partition and giving its source digest, so this does
    not need to know what a manifest looks like.

    Each partition is its own transaction. That is a deliberate shape and the docstring at
    the top of this module says why.
    """
    completed = []
    results = []
    for partition, directory in work:
        try:
            result = load.load_partition(
                con, contract, partition, directory, sha_for(partition), now=now)
        except Exception as cause:
            raise BackfillStopped(partition, completed, cause) from cause
        completed.append(partition)
        results.append(result)

    return {
        "partitions": completed,
        "rows_loaded": sum(r["rows_loaded"] for r in results),
        "rows_replaced": sum(r["rows_replaced"] for r in results),
        "rows_held": sum(r["rows_held"] for r in results),
        "results": results,
    }


def orphans(con, contract, source_partitions):
    """Partitions in the table that the source no longer offers.

    Reports and does nothing else. An automatic delete here would empty the warehouse the
    first day a fetch returns an empty directory, and that failure is far more likely than
    a publisher genuinely withdrawing a day.
    """
    present = set(load.partition_counts(con, contract))
    return sorted(present - set(source_partitions))


def ledger_rows(con):
    """One row per partition from the ledger, newest load first.

    The ledger is where a backfill leaves its trace. A partition loaded twice has one row,
    because the load deletes the old one, so this is the record of the last run rather than
    a history of every run. That is a limitation and it is named in the README.
    """
    part = '"' + schema.PARTITION_COLUMN + '"'
    rows = con.execute(
        "select {}, rows_loaded, rows_held, {} from {}.{} order by 1".format(
            part, '"' + schema.LOADED_COLUMN + '"',
            schema.RAW_SCHEMA, schema.LEDGER_TABLE)
    ).fetchall()
    return [{"partition": p, "rows_loaded": n, "rows_held": h, "loaded_at": t}
            for p, n, h, t in rows]
