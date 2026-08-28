# warehouse-data-contracts

An Airflow and dbt pipeline that refuses to publish data breaking its contract. Contracts
are YAML, they carry the provenance of every rule, and they generate both the ingestion
check and the warehouse tests.

```
bash scripts/bootstrap-local.sh
python scripts/pull_source.py --start 2025-01-01 --days 14
python scripts/profile_source.py
python scripts/quarantine_partition.py --write
```

The source is the NYC 311 service request feed, dataset `erm2-nwe9` on
`data.cityofnewyork.us`. Real municipal data with real defects in it, rather than a
generator I wrote. That matters more than it sounds and the next section is why.

## The contract found nothing, and that was the finding

Twenty single column constraints. Thirteen of them my own assumptions rather than anything
the publisher promised. Four hundred and seventy six rule evaluations across 179,314 rows.

Zero hits.

```
python scripts/profile_source.py

179314 rows over 14 partitions, 476 rule evaluations

single column rules
  nothing fired. 20 constraints, 179314 rows, zero hits.
```

Not one row out of the latitude bounding box. Not one borough outside the five. Not one
zip that is not five digits. Every `unique_key` unique across all fourteen partitions.

Then three checks that read two columns at once:

```
cross column checks
  closed_request_has_a_closed_date   requires_when  judged  178392  failing   493  0.00276
  open_request_has_no_closed_date    forbids_when   judged     886  failing    79  0.08916
  closed_after_created               ordering       judged  178013  failing    12  0.00007
  cross column failures: 584 across the checks, not a row count
  run scripts/quarantine_partition.py for the rows
```

The thresholds were never the problem. The shape of the rule was. A constraint that sees
one value at a time cannot see a request closed before it was created, and that is most of
what is actually wrong with this feed.

Both groups have a signature, which is what says they are real rather than a parsing
artefact. All 493 requests closed with no closing date are DHS, and all 493 are
`Homeless Person Assistance`. All 12 requests closed before they were created are DOT, and
several are negative by exactly three or six days with the clock time unchanged, which is a
date component being written wrong somewhere upstream rather than noise.

## 584 is not a number of rows, and neither is 493

That last line used to read "584 rows". It is a sum over three checks and a row breaking
two of them is in it twice, so it is an upper bound that reads like a count.

The other tempting number is the largest single count, 493. It is a lower bound and it
reads like a count too. Both are one line of arithmetic away from a per rule table and
neither is the answer to the only question a quarantine has, which is which rows to move.

`contracts/validate.py` judges row by row and carries the row identity, so it can just say:

```
python scripts/quarantine_partition.py

partition        rows   accept     held     sum   worst      evals
2025-01-01      10873    10841       32      32      26     303318
2025-01-02      13811    13777       34      34      27     385293
...
2025-01-14      10079    10046       33      33      30     281048

179314 rows over 14 partitions
held        572 rows, 0.00319 of the corpus
sum of the per rule counts 584, which is 12 more than the rows held
12 held rows broke more than one rule
```

572. The upper bound was over by 12 and the lower bound was under by 79, which is 13.8
percent of the answer.

The 12 are not a rounding artefact and they are the same 12 rows every time. Every request
closed before it was created is also a request still marked in flight while carrying a
closing date. One broken date field, showing up under two rules:

```
('closed_after_created:check:ordering',
 'open_request_has_no_closed_date:check:forbids_when')  12
```

So the two views of one contract are kept as two summaries of one implementation.
`rules.value_rules` returns the predicates and `rules.judge_check` judges one row against
one cross column check, and both the column view and the row view read them.
`tests/test_validate.py` grades the two against each other on a fixture built so they have
something to disagree about, because two implementations of one rule can both be correct
and still not match.

## Quarantine

```
data/quarantine/created_date=2025-01-14/
  accepted.csv        10046 rows, the original columns
  quarantined.csv        33 rows, plus _contract_failures
  report.json
```

The held rows keep every column they arrived with and gain one naming each rule they
broke, `closed_request_has_a_closed_date:check:requires_when` and so on. A rejection report
that gives a count is a report nobody can act on.

What is deliberately not here is a threshold that fails a whole partition once too much of
it is bad. Splitting the rows is a fact about the data. What share is too much is a policy,
and any number set today would be one picked by looking at the fourteen partitions it is
about to judge.

## Provenance is a field on every rule

```yaml
  - name: borough
    type: string
    provenance: asserted
    note: "the publisher never enumerates these. list taken from the five boroughs"
    required: true
    allowed: [BRONX, BROOKLYN, MANHATTAN, QUEENS, STATEN ISLAND, Unspecified]
```

`documented` means the publisher says so, in a column description or a declared type.
`asserted` means I decided it and the source never promised it. `contracts/spec.py` refuses
to load a rule that carries neither.

The split exists because a contract written by profiling the data cannot be violated by the
data. It describes what is there. Writing this one from the publisher's column metadata
first, before looking at a single value, is the only reason the zero above means anything.
It also means the thirteen asserted rules were thirteen chances to be wrong, and none of
them was, which is a stronger statement than a passing test.

Worth knowing about the documented half: the publisher's metadata gives a type and a
sentence of prose per column and nothing else. No nullability and no ranges. No
vocabularies and no freshness commitment. Seven of the twenty constraints could be sourced
from it. A data contract is a thing the consumer asserts, and the format should make that
visible rather than let it hide behind a schema.

## The first version of this reported zero because it checked nothing

The clean result arrived before the cross column checks existed, and it was wrong. The
profiler worked out which contracted columns were absent from a partition by comparing
`Column` objects against header strings:

```python
missing = [c.name for c in contract.columns if c not in present]
```

Every column read as absent. Every rule was skipped. The report printed zero violations over
179,314 rows and nothing in the output said it had evaluated nothing at all.

The tell was that it was too good. A contract with thirteen guesses in it, matching real
municipal data perfectly, is not a contract that passed.

Two things changed. `contracts/profile.py` raises `NothingChecked` when no contracted column
is present in a partition that has rows, and `scripts/profile_source.py` prints the number
of rule evaluations beside the result. A report that can say "clean" without saying how much
it looked at will eventually say "clean" having looked at nothing.

## Completeness, not just a fetch

Socrata caps a response at `$limit` and returns 200 either way. Asking for 25,000 rows of a
window holding 90,570 gives 25,000 rows and no error, and the corpus is then defined by the
limit rather than by the window.

`ingest/fetch.py` asks a second question, `select count(1)` over the same predicate, and
refuses to write a partition whose row count disagrees:

```
2025-01-01   10873 rows    1672370 bytes  9cdce16f2bd7
2025-01-02   13811 rows    2119078 bytes  af3e7dcc8aec
...
14 partitions, 179314 rows
```

The window is half open, `>= day` and `< day+1`. A closed window returns 10,878 rows for
the first day against 10,873, and those 5 rows sit at exactly midnight. A closed window puts
each of them in two partitions and the volume check then argues with itself.

`data/raw` is not committed. `data/manifest.json` is, with a row count and a sha256 for
every partition, so a copy that does not match the numbers above is something you can find
out rather than something you have to trust.

## Airflow and dbt cannot share an environment

Measured with pip's own resolver rather than assumed:

```
pip install --dry-run --target <airflow env> dbt-core==1.12.3 dbt-duckdb
```

It succeeds. It also moves 21 packages that Airflow's constraints file had pinned, including
`protobuf` from 4.25.8 to 6.33.6, `pydantic-core` from 2.41.5 to 2.46.4 and
`opentelemetry-api` from 1.27.0 to 1.44.0. pip reports no conflict and Airflow is no longer
the thing its own constraints describe.

So `scripts/bootstrap-local.sh` installs into two directories and nothing imports across
them. Airflow 3.1.8 with the matching constraints file, dbt-core 1.12.3 with dbt-duckdb
1.9.4. No Docker, no root, no virtualenv tooling.

`requirements.txt` holds one line, PyYAML. Airflow lives in `requirements-airflow.txt` and
is needed only by `dags/`. A clone can run every check in `tests/` with the first file
alone.

## Architecture

```
   Socrata API                         contracts/nyc311.yml
        |                                      |
        |  ingest/fetch.py                     |  contracts/spec.py
        |  count(1) guard                      |  refuses a malformed contract
        v                                      v
   data/raw/created_date=YYYY-MM-DD.csv --> contracts/rules.py
   data/manifest.json                          |  value_rules, judge_check
   rows + sha256 per partition                 |
                                       +-------+-------+
                                       |               |
                                       v               v
                            contracts/profile.py  contracts/validate.py
                            counts per rule       counts per row
                                                       |
                                                       v
                                              contracts/quarantine.py
                                                       |
                                            +----------+----------+
                                            v                     v
                                      accepted.csv         quarantined.csv
                                            |              + report.json
                                            v
                                     dbt  ->  mart
                                     (not built yet)
```

The Airflow side runs. `dags/nyc311_contract_check.py` pulls a partition, judges it and
writes the split, end to end:

```
bash scripts/dag_smoke.sh 2025-01-13

ran 2025-01-13, tasks: pull check
13049 rows, 13003 accepted, 46 held, 364107 rule evaluations
```

That script exists because `airflow dags test` on a date outside the DAG's own start and
end window creates a run, executes no task at all, and reports `state=success`. A smoke
test that greps for success passes on a run that did nothing, and it keeps passing forever
once the DAG's `end_date` falls behind the date it uses. So the script asserts each task
name appears in the log and that the report says how many rules it evaluated.

## Running the checks

```
python tests/run_all.py
100 passed, 0 failed
```

Plain functions named `check_*`, no framework. Nothing in `tests/` needs Airflow or dbt
installed. The DAG is the gap and `scripts/dag_smoke.sh` is that gap.

A mutation pass over the six library modules kills 157 of 163, with the control clean
before and after:

```
python ../portfolio-program/scripts/mutate.py --repo . \
  --module contracts/rules.py --module contracts/spec.py \
  --module contracts/profile.py --module contracts/validate.py \
  --module contracts/quarantine.py --module ingest/fetch.py

157 killed, 6 survived
```

The validator and the quarantine went 34 of 42 on the first pass. What the eight survivors
found is worth more than the number. Nothing was asking what the largest rule count is when
there are no rule counts, so the default the max falls back to was free to be anything.
Nothing pushed a row that breaks exactly one rule through the "more than one rule" counter,
so a comparison of one or more read the same as a comparison of more than one. And the
cross column branch of the evaluation counter was unpinned, which matters because that
counter is what a clean report leans on to prove it looked at something. Seven checks later
the pass is 41 of 42.

The six survivors across the whole repo are all constants. The page size and the HTTP
timeout in the fetcher, the read block size in the checksum loop, and two JSON indents. Two
are equivalent mutants, since reading a file in 1 MB or 2 MB blocks produces the same
sha256. The rest are values no offline check can distinguish, and pinning them would be a
test asserting that a number is the number.

## What is not built

- **No partition level verdict.** Rows are split and nothing fails the run. When a
  partition is bad enough to reject outright is a policy nobody has argued yet, and a
  threshold invented here would be one chosen by looking at the fourteen partitions it
  would judge. The DAG carries a TODO saying so rather than a task that pretends.
- **Nothing reads the quarantine back.** The held rows are written and no later step
  re-judges them, retries them, or expires them. A quarantine you never empty is a folder.
- **No dbt project.** dbt installs and runs here and nothing has been modelled yet.
- **No Snowflake.** The warehouse is duckdb through dbt-duckdb. The Snowflake statements
  will be written alongside and they will not have run.
- **The cross column checks are three of a possible many.** They were added because they
  found something, not because the list is complete. A referential check between
  `complaint_type` and `descriptor` is the obvious next one and it needs a vocabulary that
  does not exist in the metadata.
- **The generated tests are the risk I can see coming.** A contract validator and a set of
  dbt tests generated from the same YAML are two implementations of one rule, and grading
  either against the other proves nothing. They have to be graded against the data.
- **Nothing tests the network path.** `fetch_rows` and `expected_rows` are exercised
  against stubs. The real round trip runs in `scripts/pull_source.py` and in the DAG, and
  neither is in `tests/`.

## Two clauses in the contract that nothing reads

```
contract clauses nothing here evaluates: freshness, volume
  they are in the file and they are not enforced by anything yet
```

`freshness.max_lag_hours` and `volume.min_rows_per_partition` are in every contract this
repo loads, `contracts/spec.py` refuses a contract that omits either, and no code path
evaluates them. They read to anyone opening the file like rules being enforced. So the
report names them, because the alternative is a YAML file that overstates what runs.

Both are wrong in an instructive way as well as unimplemented. The volume floor is 4,000
and the smallest partition here holds 10,079, so a rule sitting 6,079 rows below anything
observed cannot bind. The freshness clause is worse. It says 48 hours, the corpus is a
January 2025 archive, and the newest partition is about 14,160 hours old. Implemented as
written it would fail every partition in the repo on the day it shipped.

The clause describes the live 311 feed and the corpus is a fixed archive of it. Those are
two different things and the format currently has no way to say which one a rule is about.
That is the design problem to solve before the freshness check gets built, rather than
after.
