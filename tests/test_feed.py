"""Checks over the two partition level clauses.

The one that matters most is that a check with no input refuses rather than passes. A
freshness clause reading `extract` needs a fetch time, thirteen of the fourteen partitions
in this repo have none, and the wrong answer there is `ok`. Every other check in this file
exists so that one cannot be reached by accident.

Fixtures are lopsided on purpose. A partition list split evenly between above and below the
floor survives an inverted comparison, because the count of failures does not move.
"""

import datetime
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from contracts import feed, spec

CONTRACT = os.path.join(ROOT, "contracts", "nyc311.yml")

NOW = datetime.datetime(2025, 1, 15, 12, 0, 0)


def contract():
    return spec.load(CONTRACT)


def entries(counts, fetched=None):
    """A manifest slice. Partitions are named the way the real ones are."""
    out = []
    for i, rows in enumerate(counts):
        day = (datetime.date(2025, 1, 1) + datetime.timedelta(days=i)).isoformat()
        entry = {"partition": day, "rows": rows,
                 "path": "raw/created_date={}.csv".format(day)}
        if fetched:
            entry["fetched_at"] = fetched
        out.append(entry)
    return out


def newest_for(entries_, hour=23):
    out = {}
    for e in entries_:
        day = datetime.date.fromisoformat(e["partition"])
        out[e["partition"]] = datetime.datetime(day.year, day.month, day.day, hour, 59)
    return out


def check_newest_event_reads_the_latest_parsable_value():
    body = ("unique_key,created_date\n"
            "1,2025-01-01T05:00:00.000\n"
            "2,2025-01-01T22:15:30.000\n"
            "3,2025-01-01T09:00:00.000\n")
    with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False) as fh:
        fh.write(body)
        path = fh.name
    try:
        assert feed.newest_event(path, "created_date") == \
            datetime.datetime(2025, 1, 1, 22, 15, 30)
    finally:
        os.unlink(path)


def check_a_partition_with_no_readable_timestamp_gives_none_not_a_zero():
    """None is a real answer. A zero here would read as an event at the epoch."""
    body = "unique_key,created_date\n1,\n2,not a date\n"
    with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False) as fh:
        fh.write(body)
        path = fh.name
    try:
        assert feed.newest_event(path, "created_date") is None
    finally:
        os.unlink(path)


def check_partition_end_is_the_start_of_the_next_day():
    assert feed.partition_end("2025-01-31", "day") == datetime.datetime(2025, 2, 1)


def check_a_grain_that_is_not_implemented_raises_rather_than_guessing():
    try:
        feed.partition_end("2025-01-31", "hour")
    except ValueError as exc:
        assert "day grain" in str(exc), str(exc)
    else:
        raise AssertionError("an hour grain was accepted")


def check_extract_reference_refuses_a_partition_with_no_fetch_time():
    """The whole point of the module. Missing input is not a pass.

    Written against the real contract rather than a stub, because `extract` is what the
    shipped contract asks for and a stub would let that change without failing.
    """
    c = contract()
    assert c.freshness["reference"] == "extract"
    es = entries([10000, 11000, 12000])
    for e in es:
        assert "fetched_at" not in e
    out = feed.freshness(c, es, newest_for(es), now=NOW)
    tail = [v for v in out if v.verdict != feed.NOT_APPLICABLE]
    assert len(tail) == 1, [v.verdict for v in out]
    assert tail[0].verdict == feed.NO_EXTRACT_TIME, tail[0].verdict
    assert tail[0].lag_hours is None, tail[0].lag_hours


def check_extract_reference_measures_from_the_fetch_when_it_has_one():
    c = contract()
    es = entries([10000], fetched="2025-01-01T18:00:00")
    out = feed.freshness(c, es, {"2025-01-01": datetime.datetime(2025, 1, 1, 6, 0)},
                         now=NOW)
    assert out[0].verdict == feed.OK, out[0].verdict
    assert abs(out[0].lag_hours - 12.0) < 1e-9, out[0].lag_hours


def check_the_extract_reading_does_not_move_when_now_moves():
    """This is what `extract` buys over `wall_clock` and nothing else does."""
    c = contract()
    es = entries([10000], fetched="2025-01-01T18:00:00")
    newest = {"2025-01-01": datetime.datetime(2025, 1, 1, 6, 0)}
    early = feed.freshness(c, es, newest, now=NOW)[0].lag_hours
    late = feed.freshness(c, es, newest,
                          now=NOW + datetime.timedelta(days=400))[0].lag_hours
    assert early == late, (early, late)


def check_wall_clock_moves_when_now_moves():
    """The mirror of the check above. Without it that one passes on a frozen clock."""
    c = contract()
    c.freshness["reference"] = "wall_clock"
    es = entries([10000])
    newest = {"2025-01-01": datetime.datetime(2025, 1, 1, 6, 0)}
    early = feed.freshness(c, es, newest, now=NOW)[0].lag_hours
    late = feed.freshness(c, es, newest,
                          now=NOW + datetime.timedelta(days=400))[0].lag_hours
    assert late > early + 9000, (early, late)


def check_tail_scope_judges_the_newest_partition_and_skips_the_rest():
    c = contract()
    c.freshness["reference"] = "wall_clock"
    es = entries([10000, 11000, 12000, 13000, 14000])
    out = feed.freshness(c, es, newest_for(es), now=NOW)
    skipped = [v.partition for v in out if v.verdict == feed.NOT_APPLICABLE]
    judged = [v.partition for v in out if v.verdict != feed.NOT_APPLICABLE]
    assert judged == ["2025-01-05"], judged
    assert len(skipped) == 4, skipped


def check_every_partition_scope_judges_all_of_them():
    c = contract()
    c.freshness["reference"] = "wall_clock"
    c.freshness["applies_to"] = "every_partition"
    es = entries([10000, 11000, 12000, 13000, 14000])
    out = feed.freshness(c, es, newest_for(es), now=NOW)
    assert not [v for v in out if v.verdict == feed.NOT_APPLICABLE]
    assert len(out) == 5


def check_a_partition_whose_newest_event_is_unknown_is_not_called_fresh():
    c = contract()
    c.freshness["reference"] = "wall_clock"
    es = entries([10000])
    out = feed.freshness(c, es, {"2025-01-01": None}, now=NOW)
    assert out[0].verdict == feed.NO_EVENTS, out[0].verdict
    assert out[0].lag_hours is None


def check_the_boundary_is_over_the_limit_rather_than_at_it():
    c = contract()
    c.freshness["reference"] = "wall_clock"
    limit = c.freshness["max_lag_hours"]
    es = entries([10000])
    event = NOW - datetime.timedelta(hours=limit)
    assert feed.freshness(c, es, {"2025-01-01": event}, now=NOW)[0].verdict == feed.OK
    event = NOW - datetime.timedelta(hours=limit, seconds=1)
    assert feed.freshness(c, es, {"2025-01-01": event}, now=NOW)[0].verdict == feed.STALE


def check_volume_fires_on_the_one_partition_below_the_floor():
    """Lopsided on purpose. Four above and one below, so inverting the comparison moves
    the count from one to four rather than leaving it where it was."""
    c = contract()
    floor = c.volume["min_rows_per_partition"]
    es = entries([floor + 5000, floor + 6000, floor - 1, floor + 7000, floor + 8000])
    out = feed.volume(c, es)
    fired = [v.partition for v in out if v.fired]
    assert fired == ["2025-01-03"], fired
    assert len(out) == 5


def check_a_partition_exactly_on_the_floor_passes():
    c = contract()
    floor = c.volume["min_rows_per_partition"]
    out = feed.volume(c, entries([floor]))
    assert out[0].verdict == feed.OK, out[0].verdict
    assert out[0].rows == floor


def check_headroom_says_the_shipped_floor_cannot_bind():
    """The floor is 4,000 and nothing in the corpus is close to it. That is a property of
    the contract as written and it is the reason the number is not quietly raised."""
    c = contract()
    room = feed.headroom(c, entries([10079, 12000, 15023]))
    assert room.can_bind is False
    assert room.gap == 10079 - c.volume["min_rows_per_partition"]
    # The exact value rather than a bound. `> 2.5` is satisfied by a multiplication as
    # easily as by a division, and the ratio is published.
    assert abs(room.ratio - 10079 / 4000.0) < 1e-12, room.ratio
    assert room.largest == 15023


def check_headroom_can_bind_when_the_floor_is_above_the_smallest():
    c = contract()
    c.volume["min_rows_per_partition"] = 11000
    assert feed.headroom(c, entries([10079, 12000, 15023])).can_bind is True


def check_a_floor_equal_to_the_smallest_partition_still_cannot_bind():
    """The boundary, and it has to agree with the comparison `volume` really makes.

    A partition exactly on the floor passes, so a floor sitting exactly on the smallest
    thing ever seen has still never refused anything. Without this the strict comparison
    and a loose one are indistinguishable.
    """
    c = contract()
    c.volume["min_rows_per_partition"] = 10079
    room = feed.headroom(c, entries([10079, 12000, 15023]))
    assert room.can_bind is False, room.floor
    assert room.gap == 0
    assert feed.volume(c, entries([10079]))[0].verdict == feed.OK


def check_headroom_over_no_partitions_raises_rather_than_reporting_a_gap():
    try:
        feed.headroom(contract(), [])
    except ValueError:
        return
    raise AssertionError("headroom answered on an empty manifest")


def check_median_handles_both_parities():
    assert feed.median([3, 1, 2]) == 2.0
    assert feed.median([4, 1, 2, 3]) == 2.5


def check_mad_is_zero_when_a_majority_share_one_value():
    """A robust spread with a fifty percent breakdown point collapses here, and a band of
    zero width fires on everything rather than on nothing."""
    assert feed.mad([7, 7, 7, 1, 99]) == 0
    assert feed.fit_floor([7, 7, 7, 7, 1, 99], holdout=2).degenerate is True


def check_the_fit_holds_back_the_newest_partitions_not_a_random_sample():
    fit = feed.fit_floor([1, 2, 3, 4, 5, 6, 7], holdout=3)
    assert fit.train == [1, 2, 3, 4], fit.train
    assert fit.test == [5, 6, 7], fit.test


def check_a_holdout_that_leaves_no_training_data_raises():
    try:
        feed.fit_floor([1, 2, 3], holdout=3)
    except ValueError as exc:
        assert "nothing to fit" in str(exc), str(exc)
        return
    raise AssertionError("a fit with no training partitions was allowed")


def check_the_resolution_is_one_over_the_holdout():
    """A rate measured on five partitions comes in fifths, so a gate below a fifth cannot
    be measured against it at any effect size."""
    fit = feed.fit_floor(list(range(1, 20)), holdout=5)
    assert abs(fit.rate_resolution - 0.2) < 1e-9
    assert feed.fit_floor(list(range(1, 20)), holdout=10).rate_resolution == 0.1


def check_the_fitted_floor_uses_the_production_defaults():
    """Constructed with no keyword. A helper that names every argument leaves the default
    untested, and the default is what a caller who has not read the source gets.

    The spread has to be non zero here. On a series where a majority share one value the
    MAD is exactly 0, and then the floor is the median whether the band is subtracted or
    added, so the fixture would agree with a sign error.
    """
    counts = [10, 20, 30, 40, 50, 60, 70, 80, 90, 100, 110, 120, 130, 140, 150]
    fit = feed.fit_floor(counts)
    assert fit.k == 3.0
    assert fit.spread > 0, fit.spread
    # The default holdout is 5, and the README publishes a nine and five split off it.
    assert len(fit.test) == 5, fit.test
    assert fit.test == counts[-5:]
    assert fit.floor < fit.centre, (fit.floor, fit.centre)
    assert fit.floor == fit.centre - 3.0 * fit.spread


def check_out_of_sample_rate_counts_the_held_out_partitions_that_fall_through():
    counts = [100] * 9 + [1, 1, 100, 100, 100]
    fit = feed.fit_floor(counts, holdout=5)
    assert fit.spread == 0
    assert len(fit.fires()) == 2, fit.fires()
    assert abs(fit.out_of_sample_rate - 0.4) < 1e-9


def check_an_unjudged_verdict_carries_no_number():
    """A count or a lag printed beside a verdict meaning nothing was measured is the thing
    a reader quotes."""
    v = feed.Verdict("2025-01-01", "freshness", feed.NO_EXTRACT_TIME, 48)
    assert v.lag_hours is None
    assert v.rows is None
    assert v.judged is False
    assert v.fired is False
