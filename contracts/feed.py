"""Judge a partition as a whole rather than row by row. Freshness and volume.

Every other rule in this repo reads a value or a row. These two read a partition and they
need things a row does not carry. Freshness needs a clock. Volume needs to know what a
normal partition looks like.

Freshness is the one with the design problem, and the problem is that the clause never says
what it is measuring against. "Under 48 hours" is meaningless on its own. Forty eight hours
between what and what. Three answers are available and they are not close to each other.

    wall_clock     now minus the newest event in the partition
    extract        when the fetch ran, minus the newest event in the partition
    partition_end  the end of the partition's own day, minus the newest event

wall_clock is what people mean and it is a statement about a live feed. Point it at an
archive and it returns the age of the archive. extract is the publisher's lag at the moment
you read it and it does not move while the file sits on disk. partition_end cannot exceed
one grain by construction, so it carries no information at all and it is here to be named
and rejected rather than left as a thing someone might reach for.

The second half of the problem is scope. A freshness rule is a statement about the tail of
a feed. A backfill re-reads history, so every partition it touches is old and a rule applied
to all of them fires on all of them. A guardrail that refuses everything correct gets turned
off, and then it protects nothing. So the clause says which partitions it judges.

Volume is easier and it has its own trap. A floor written by hand has no training set, so
the usual argument about measuring a fire rate out of sample does not apply to it. What it
has instead is a distance to the nearest thing ever observed, and that is what says whether
it can bind.
"""

import csv
import datetime

from contracts import rules

REFERENCES = ("wall_clock", "extract", "partition_end")
SCOPES = ("tail", "every_partition")

OK = "ok"
STALE = "stale"
NOT_APPLICABLE = "not_applicable"
NO_EXTRACT_TIME = "no_extract_time"
NO_EVENTS = "no_events"

BELOW_FLOOR = "below_floor"

HOUR = 3600.0


class Verdict:
    """One partition, one clause, one answer.

    `lag_hours` and `rows` are None whenever the verdict is one of the two that mean the
    check could not run. A number beside a verdict that says nothing was measured is how a
    reader ends up quoting it.
    """

    def __init__(self, partition, clause, verdict, limit,
                 lag_hours=None, rows=None, reference=None):
        self.partition = partition
        self.clause = clause
        self.verdict = verdict
        self.limit = limit
        self.lag_hours = lag_hours
        self.rows = rows
        self.reference = reference

    @property
    def fired(self):
        return self.verdict in (STALE, BELOW_FLOOR)

    @property
    def judged(self):
        return self.verdict in (OK, STALE, BELOW_FLOOR)

    def __repr__(self):
        return "Verdict({}, {}, {})".format(self.partition, self.clause, self.verdict)


def newest_event(path, column):
    """The latest parsable value of the partition column in this file.

    Returns None on a partition with no readable timestamp, which is a real answer and not
    a zero. A partition that cannot say when its newest row happened cannot be judged for
    freshness, and saying so is different from saying it is fresh.
    """
    latest = None
    with open(path, newline="") as fh:
        for row in csv.DictReader(fh):
            when = rules.as_timestamp(row.get(column))
            if when is None:
                continue
            if latest is None or when > latest:
                latest = when
    return latest


def partition_end(partition, grain):
    day = datetime.datetime.strptime(partition, "%Y-%m-%d")
    if grain != "day":
        raise ValueError("only a day grain is implemented, got {}".format(grain))
    return day + datetime.timedelta(days=1)


def _reference_time(reference, entry, partition, grain, now):
    """The clock this partition is measured against, or a verdict saying there is none."""
    if reference == "wall_clock":
        return now, None
    if reference == "partition_end":
        return partition_end(partition, grain), None
    fetched = entry.get("fetched_at")
    if not fetched:
        return None, NO_EXTRACT_TIME
    return rules.as_timestamp(fetched), None


def freshness(contract, entries, newest, now=None):
    """One Verdict per partition in `entries`.

    `newest` maps a partition to the newest event in it. It is passed in rather than read
    here because reading fourteen files to answer a scope that judges one of them is work
    nobody asked for, and because a caller with the value in a manifest should not have to
    go back to the CSV.
    """
    now = now or datetime.datetime.now()
    clause = contract.freshness
    limit = clause["max_lag_hours"]
    reference = clause["reference"]
    grain = contract.source["partition_grain"]

    entries = sorted(entries, key=lambda e: e["partition"])
    if clause["applies_to"] == "tail":
        judged = {entries[-1]["partition"]} if entries else set()
    else:
        judged = {e["partition"] for e in entries}

    out = []
    for entry in entries:
        partition = entry["partition"]
        if partition not in judged:
            out.append(Verdict(partition, "freshness", NOT_APPLICABLE, limit,
                               reference=reference))
            continue

        event = newest.get(partition)
        if event is None:
            out.append(Verdict(partition, "freshness", NO_EVENTS, limit,
                               reference=reference))
            continue

        against, refusal = _reference_time(reference, entry, partition, grain, now)
        if refusal is not None:
            out.append(Verdict(partition, "freshness", refusal, limit,
                               reference=reference))
            continue

        lag = (against - event).total_seconds() / HOUR
        verdict = STALE if lag > limit else OK
        out.append(Verdict(partition, "freshness", verdict, limit,
                           lag_hours=lag, reference=reference))
    return out


def volume(contract, entries):
    floor = contract.volume["min_rows_per_partition"]
    out = []
    for entry in sorted(entries, key=lambda e: e["partition"]):
        count = entry["rows"]
        verdict = BELOW_FLOOR if count < floor else OK
        out.append(Verdict(entry["partition"], "volume", verdict, floor, rows=count))
    return out


class Headroom:
    """How far the floor sits from anything that has ever been seen.

    A floor below every observation cannot fire, and it reads in the contract file exactly
    like one that can. The ratio is the number worth quoting, because the gap in rows means
    nothing without knowing the size of a partition.
    """

    def __init__(self, floor, smallest, largest, partitions):
        self.floor = floor
        self.smallest = smallest
        self.largest = largest
        self.partitions = partitions

    @property
    def gap(self):
        return self.smallest - self.floor

    @property
    def ratio(self):
        return self.smallest / float(self.floor)

    @property
    def can_bind(self):
        return self.floor > self.smallest


def headroom(contract, entries):
    counts = [e["rows"] for e in entries]
    if not counts:
        raise ValueError("no partitions, so there is nothing for the floor to miss")
    return Headroom(contract.volume["min_rows_per_partition"],
                    min(counts), max(counts), len(counts))


def median(values):
    values = sorted(values)
    n = len(values)
    if not n:
        raise ValueError("median of nothing")
    mid = n // 2
    if n % 2:
        return float(values[mid])
    return (values[mid - 1] + values[mid]) / 2.0


def mad(values):
    """Median absolute deviation. Zero whenever more than half the values agree."""
    centre = median(values)
    return median([abs(v - centre) for v in values])


class Fit:
    """What a fitted floor would cost on this corpus, before one is fitted.

    The two numbers that decide it are the width of the held out set and the width of the
    band. A held out set of five partitions can only report a fire rate in fifths, so a
    gate set anywhere below a fifth cannot be measured against it at any effect size. And a
    band whose deviation is zero has no width at all, which fires on everything rather than
    on nothing.
    """

    def __init__(self, train, test, centre, spread, k):
        self.train = train
        self.test = test
        self.centre = centre
        self.spread = spread
        self.k = k

    @property
    def floor(self):
        return self.centre - self.k * self.spread

    @property
    def rate_resolution(self):
        return 1.0 / len(self.test)

    @property
    def degenerate(self):
        return self.spread == 0

    def fires(self):
        return [v for v in self.test if v < self.floor]

    @property
    def out_of_sample_rate(self):
        return len(self.fires()) / float(len(self.test))


def fit_floor(counts, holdout=5, k=3.0):
    """Fit a robust floor on the older partitions and hold the newest ones back.

    Split by position rather than at random, because a volume series is ordered and a
    random split lets the fit see partitions on both sides of anything it should not have
    known about.
    """
    counts = list(counts)
    if len(counts) <= holdout:
        raise ValueError(
            "{} partitions and a holdout of {} leaves nothing to fit on".format(
                len(counts), holdout))
    train = counts[:-holdout]
    test = counts[-holdout:]
    return Fit(train, test, median(train), mad(train), k)
