# Running the checks

Moved out of the README so the front page stays short. Back to the [README](../README.md).

## Running the checks

```
python tests/run_all.py
317 passed, 0 failed
```

Plain functions named `check_*`, no framework. Nothing in `tests/` needs Airflow or dbt
installed. `tests/test_dbt_project.py` reads the dbt project rather than running it and
`scripts/dbt.sh build` is the real check. The DAG is the other gap and
`scripts/dag_smoke.sh` is that one.

A mutation pass over the library modules, with the control clean before and after every
call. `mutate.py` is a small harness I keep outside this repo. It rewrites one AST node at
a time and uses `tests/run_all.py` as the oracle, so any mutation tool pointed at the same
suite reproduces these counts.

```
python mutate.py --repo . \
  --module contracts/rules.py --module contracts/spec.py \
  --module contracts/profile.py --module contracts/validate.py \
  --module contracts/quarantine.py --module ingest/fetch.py \
  --module ingest/compare.py --module warehouse/schema.py \
  --module warehouse/load.py --module warehouse/history.py

221 killed, 6 survived

python mutate.py --repo . --module contracts/generate.py
17 killed, 1 survived

python mutate.py --repo . \
  --module contracts/feed.py --slice 0:22
python mutate.py --repo . \
  --module contracts/feed.py --slice 22:43
42 killed, 1 survived
```

The slices are there because a 43 site module against a 4.3 second suite is 185 seconds and
the machine this runs on kills a call before that. The control runs at both ends of every
call, so a split pass is worth the same as a whole one and the counts add up by hand.

The first pass over `contracts/feed.py` went 38 of 43 and four of the five survivors were
real. A ratio asserted as `> 2.5` survived its division becoming a multiplication. A floor
compared against the smallest partition survived `>` becoming `>=`, because no fixture put
one exactly on the other. The fitted floor survived its band being added instead of
subtracted, because the fixture was a series where a majority share one value, so the MAD is
zero and both signs give the median. And the default holdout was never asserted, only the
default `k` beside it. Fixed, then re-run to 42 of 43.

The two that survive are constants a check cannot reach. `>` against `>=` while scanning for
a maximum returns the same maximum and a yaml line width of 100 or 101 produces the same
document when no line is that long.

The two modules under `docs/` were run last and killed 67 of 71:

```
python mutate.py --repo . \
  --module docs/blocks.py --slice 0:26
python mutate.py --repo . \
  --module docs/blocks.py --slice 26:51
49 killed, 1 survived

python mutate.py --repo . --module docs/lineage.py
18 killed, 3 survived
```

The first pass over `docs/blocks.py` went 43 of 53 and six survivors were real. Every
threshold in the module survived, because the fixtures sat well clear of each one rather
than either side of it. The block start line and the shell template line number were never
asserted, so both could be off by one. And no fixture used an f-string, so the branch that
reads one had never run.

Two of the ten wanted deleting rather than testing. One was a `lineno` default that no
parsed node ever reaches. The other asserted that a piece of an f-string is a string when
the line above had already established it is a `Constant`, and inside an f-string a
`Constant` is always a string.

The one that survives is `or` against `and` in the filter that skips a command's flags and
environment assignments. Telling them apart needs a word that starts with a dash, carries no
equals sign and ends in `.py`. No command in this README looks like that, so the two read
the same here and the difference is untested rather than absent.

The three in `docs/lineage.py` are the depth arithmetic and they are equivalent, measured
rather than argued. `render` groups the nodes and then numbers the groups with `enumerate`,
so the depths themselves are thrown away and only the grouping survives. Shifting the base
from 0 to 1, doubling the step, and changing the default for a node with no parents all
produce a byte identical diagram.

The three modules that were carrying a figure about an older tree have been re-run.
`contracts/spec.py` killed 37 of 37 and `contracts/profile.py` 9 of 9. `ingest/fetch.py`
killed 12 of 17 and all five survivors are the constants described below.

The newest three modules were run whole, `warehouse/backfill.py` at 11 of 11,
`contracts/replay.py` at 17 of 17 and `contracts/breaks.py` at 50 of 50 across two slices.
The first pass over the last two went 29 of 36 and every survivor was real. Six of them sat
in the two functions that produce the constraint table above, which had been put in the
library precisely so a mutant could reach them and then given no test. The other four were
in the break catalogue itself, where a mutation could flip the value `wrong_agency` writes
or flip its flag from accepted to held, because every check exercised a break written in the
test file rather than a shipped one.

The modules changed by that work were re-run too. `contracts/validate.py` 38 of 38,
`contracts/rules.py` 65 of 65 across two slices, `warehouse/load.py` 21 of 21 and
`contracts/quarantine.py` 8 of 8.

Three survivors were real and all three are closed. `ingest/compare.py` refused a
comparison when either side was empty rather than when both were, which would have thrown
away the loudest thing it could report, a partition that arrived or vanished whole.
`warehouse/history.py` fused a row count and a key count into one select and on an empty
table both are zero, so a refusal reading the wrong one of them was invisible. And
`verify_partition`'s example cap was never exercised at its default, which is the value the
load itself gets, because every check named the argument.

The five that survive are all constants in the fetcher. A page size and an HTTP timeout, a
read block size in the checksum loop and a JSON indent. Reading a file in 1 MB or 2 MB
blocks produces the same sha256 and a page size of 50,000 or 50,001 produces the same rows,
so none of them can change an answer. Pinning them would be a test asserting that a number
is the number.

One survivor was deleted rather than tested. `warehouse/history` had a query helper taking
a parameter list nobody ever passed and a mutant flipping its `or` to an `and` changed
nothing under any input. An argument no caller uses is not a branch missing a test.
