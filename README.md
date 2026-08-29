# warehouse-data-contracts

An Airflow and dbt pipeline that refuses to publish data breaking its contract. Contracts
are YAML, they carry the provenance of every rule, and they generate both the ingestion
check and the warehouse tests.

```
bash scripts/bootstrap-local.sh
python scripts/pull_source.py --start 2025-01-01 --days 14
python scripts/profile_source.py
python scripts/quarantine_partition.py --write
python scripts/load_raw.py --twice
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

## The raw layer is all text, and three rows are the reason

Everything the load writes is `VARCHAR`. That is a decision, and this is the measurement
behind it.

Let duckdb read a partition without telling it anything and it types the columns for you.
Run that over all fourteen and one column comes back two different ways:

```
python scripts/sniff_probe.py

14 files, 11 columns, 1 where the guess is not stable
  incident_zip   BIGINT    11 files
    2025-01-01, 2025-01-02, 2025-01-03, 2025-01-04, 2025-01-05, 2025-01-06,
    2025-01-07, 2025-01-09, 2025-01-10, 2025-01-11, 2025-01-13
  incident_zip   VARCHAR   3 files
    2025-01-08, 2025-01-12, 2025-01-14
```

Three partitions out of fourteen. The cause is one row each. `07307` and `00083` and
`07105` are the only zips in 179,314 rows that start with a zero. A leading zero
disqualifies an integer, so three rows decide the type of a column in the warehouse.

**The dangerous path is the one that looks safe.** Reading all fourteen at once refuses:

```
reading all 14 at once:
  refused, Invalid Input Error: Schema mismatch between globbed files.
```

Reading them one at a time and appending, which is exactly what a daily DAG does, does
not refuse. The first partition to arrive decides the schema and every later one is cast
into it without complaint:

```
first partition decides the schema, the rest are appended into it:
  typed from created_date=2025-01-01.csv
  179314 rows, 1972454 cells
  closed_date        178013 cells differ from the text they arrived as
  created_date       179314 cells differ from the text they arrived as
  incident_zip            3 cells differ from the text they arrived as
  longitude               2 cells differ from the text they arrived as
```

The timestamp columns are the loud number and the least interesting one.
`2025-01-01T00:51:02.000` becomes `2025-01-01 00:51:02`, which is a different string
carrying the same instant. `-74` becoming `-74.0` is the same kind of thing twice.

The three zips are not that. `cast('00083' as bigint)` is `83`. The contract says
`incident_zip` matches `^[0-9]{5}$`, the ingestion check reads the text off the file and
passes it, and the same rule run against the table would refuse it. One clause, two
answers, and neither layer is wrong on its own.

Load the partitions in the other order and the damage is zero, because the first file then
types the column as text. A defect whose existence depends on which day the backfill
started is not one anybody finds by reading the code.

So `warehouse/schema.py` builds the `columns=` argument from the contract and hands it to
`read_csv` every time. Nothing in the load path infers anything. Casting is a decision that
belongs downstream in dbt, where somebody can argue with it.

## Loading a partition twice costs nothing

```
python scripts/load_raw.py --twice

2025-01-01      10841 loaded        0 replaced     32 held
...
2025-01-14      10046 loaded        0 replaced     33 held

178742 rows over 14 partitions, 0 replaced
fingerprint 178742:9b087c3a9d00

second pass 178742 rows, 178742 replaced
fingerprint 178742:9b087c3a9d00
identical
ledger agrees with the table on all 14 partitions
```

Delete then insert on the partition key, inside one transaction. Not an append, because a
warehouse where a rerun doubles a day is a warehouse nobody dares backfill. The delete runs
whether or not the partition is there, so the first load and the tenth are the same
statement.

The fingerprint covers every source column plus the partition and the source checksum. It
leaves out `_loaded_at`, which is wall clock and would make any two loads look different
for a reason nobody cares about.

`raw.load_ledger` carries one row per load. It is not the truth about what is in the table,
it is what a load claimed, and `warehouse/load.reconcile` is the thing that compares the
two. A check that has never fired is still worth having when the alternative is trusting
the writer to be honest about itself.

`load_partition` takes a quarantine directory rather than a CSV path, and that is the one
design decision in the module worth arguing about. A function taking a file would happily
be handed `data/raw`, all 13,049 rows of it including the 46 the contract refused, and the
only thing between that and the warehouse would be every caller remembering not to. A
directory with no `report.json` is refused, and `data/raw` has none:

```
load.load_partition(con, contract, "2025-01-13", "data/raw", sha)
UnjudgedPartition: data/raw holds no report.json, so nothing has judged what is in it
```

The counts come out of that report rather than from the caller, so there is no argument to
get wrong and no default to leave at zero.

Four refusals in total. `UnjudgedPartition` for a directory nobody judged.
`WrongPartition` when the report is about a different day. `HeaderMismatch` when the
columns are not the contract's. And `LoadCountMismatch` when the table ends up holding a
different number of rows than the report said it accepted. That last one is two artefacts
written by different code having to agree.

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
                                            |                     |
                                            v                     v
                                    warehouse/load.py      nothing reads
                                    columns= from the      these back yet
                                    contract, never
                                    inferred
                                            |
                                            v
                                  raw.nyc311_service_requests
                                  raw.load_ledger
                                            |
                                            v
                                     dbt  ->  mart
                                     (not built yet)
```

The Airflow side runs end to end. `dags/nyc311_contract_check.py` pulls a partition and
judges it, then writes the split and loads what passed:

```
bash scripts/dag_smoke.sh 2025-01-13

ran 2025-01-13, tasks: pull check load_raw
13049 rows, 13003 accepted, 46 held, 364107 rule evaluations
raw layer holds 13003 rows for 2025-01-13
```

The last line is asked of the database from outside the run. The load task already refuses
a count it disagrees with, and a task returning without raising is not the same fact as the
rows being there.

That script exists because `airflow dags test` on a date outside the DAG's own start and
end window creates a run, executes no task at all, and reports `state=success`. A smoke
test that greps for success passes on a run that did nothing, and it keeps passing forever
once the DAG's `end_date` falls behind the date it uses. So the script asserts each task
name appears in the log and that the report says how many rules it evaluated.

## Running the checks

```
python tests/run_all.py
137 passed, 0 failed
```

Plain functions named `check_*`, no framework. Nothing in `tests/` needs Airflow or dbt
installed. The DAG is the gap and `scripts/dag_smoke.sh` is that gap.

A mutation pass over the eight library modules kills 193 of 199, with the control clean
before and after each call:

```
python ../portfolio-program/scripts/mutate.py --repo . \
  --module contracts/rules.py --module contracts/spec.py \
  --module contracts/profile.py --module contracts/validate.py \
  --module contracts/quarantine.py --module ingest/fetch.py \
  --module warehouse/schema.py --module warehouse/load.py

193 killed, 6 survived
```

Seven of the eight were run against the tree above. `contracts/rules.py` carries a figure
of 61 of 61 from the last time it was run, and its file has not changed since. Adding
checks to a suite can kill more mutants and never fewer, so a carried number of that shape
is a floor rather than a claim. Saying which is which is the point.

The two new modules went 30 of 36 on the first pass and the six survivors were the useful
part. Four of them lived in the two fields the type probe prints as its headline. Nothing
read the name of the file that decided the schema, so pointing it at a different file
changed no answer. Nothing read the cell count either, so multiplying rows by columns could
have been dividing them. That figure is published two sections up.

The other two were defaults. `rows_held` falls back to zero and every check named it
explicitly, which is a test helper hiding a value from the thing it is meant to test. And
the fingerprint's twelve hex characters were unpinned, which matters because a fingerprint
is only ever useful against an older one. Six checks later the pass is 36 of 36.

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
- **No dbt project.** dbt installs and runs here and nothing has been modelled yet, so the
  raw layer is currently a table nothing reads.
- **No Snowflake.** The warehouse is duckdb through dbt-duckdb. `warehouse/schema.py`
  writes the Snowflake DDL alongside the duckdb one and it has never been executed. The
  only thing it does differently is `TIMESTAMP_NTZ`, which is a small enough difference
  that trusting it would be the point at which it turns out to be wrong.
- **One writer.** duckdb takes a single write lock on the file, so two DAG runs loading two
  partitions at once will not both get in. Catchup is off and nothing here runs in
  parallel, so this has never been hit. It is the first thing a real backfill would find.
- **The load reads a file the previous task wrote.** That is a real handoff and it is also
  a shared filesystem assumption. Two tasks on two workers do not have one, and the answer
  is object storage rather than a bigger XCom.
- **An empty CSV field becomes NULL in the table.** Measured on one partition. Each of the
  five nullable columns holds exactly as many nulls as the source held empty strings.
  `latitude` and `longitude` are 117 each. CSV cannot tell an empty string from a missing
  value, so something has to choose. The contract's own null test already treats the two
  the same. Worth knowing it is a choice rather than a passthrough.
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
