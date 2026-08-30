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
bash scripts/dbt.sh build
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

## The explicit types did not help, and here is what they missed

The section above is the defence against a reader guessing. It was not enough, and what
got past it is worse than what it stopped.

The quarantine writes each partition into a directory named for it,
`data/quarantine/created_date=2025-01-01/`. That is hive layout, `read_csv` recognises it,
and it reads the directory name as a column. When the file already has a column of that
name, the one off the path wins.

```
same file, read three ways

read_csv(path)                                   created_date = 2025-01-01      DATE
read_csv(path, columns={... 'created_date': 'VARCHAR' ...})
                                                 created_date = 2025-01-01      DATE
read_csv(path, hive_partitioning=false, columns={...})
                                                 created_date = 2025-01-01T00:51:02.000
```

The middle line is the one that matters. An explicit `columns=` naming the type does not
stop it. The path derived column overrides the declared type as well as the value, so the
defence built against the type inference problem did not cover the column it mattered most
for.

**Every check in the load passed.** The row count matched what the contract's report said
it accepted, on all fourteen partitions. The ledger reconciled. Loading twice produced a
byte identical fingerprint, because both loads were wrong the same way. Nothing anywhere
compared a value in the table against the same value in the file.

The damage was one column, on every row of it:

```
178742 rows, 1966162 cells
  created_date   178742 cells differ from the cell in the file
```

Times of day gone. `2025-01-01T00:51:02.000` became `2025-01-01` on all 178,742 rows. The
value it became is the partition it lives in, so the table reads exactly as a person
expects a daily table to read.

**The contract passes on the file and fails on the table**, which is the same sentence as
the leading zero problem two sections up, at four orders of magnitude more rows. The
contract types `created_date` as a timestamp. `as_timestamp` on the value that arrived
returns an instant. On the value the warehouse stored it returns nothing, because
`2025-01-01` matches none of the formats the publisher writes.

What caught it was the staging model refusing to cast. `try_strptime` with the publisher's
own format returned null on all 178,742 rows and `stg_cast_is_lossless` failed. A plain
`cast(created_date as timestamp)` would have accepted `2025-01-01` and produced midnight,
silently, and the marts would have looked fine. This is what that costs:

```
resolution hours over 177,934 closed requests

created at the real time     median 11.57   p90 277.62   mean 204.30
created at midnight          median 28.28   p90 293.05   mean 217.36
```

The median is out by 144 percent and the mean by 6.4. A dashboard watching the average
would have shown nothing at all.

So the load now passes `hive_partitioning=false`. And `warehouse/load.verify_partition`
reads the file with the standard library and the table with SQL, then compares the two. It
is the only check here that is not a count, and a count is exactly what this defect
satisfies.

```
python scripts/load_raw.py --twice

178742 rows over 14 partitions, 0 replaced
fingerprint 178742:2213f6d310b3

second pass 178742 rows, 178742 replaced
fingerprint 178742:2213f6d310b3
identical
```

That fingerprint is not the one this file used to publish. The row count did not move and
the contents did.

## Loading a partition twice costs nothing

```
python scripts/load_raw.py --twice

2025-01-01      10841 loaded        0 replaced     32 held
...
2025-01-14      10046 loaded        0 replaced     33 held

178742 rows over 14 partitions, 0 replaced
fingerprint 178742:2213f6d310b3

second pass 178742 rows, 178742 replaced
fingerprint 178742:2213f6d310b3
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

Five refusals in total. `UnjudgedPartition` for a directory nobody judged.
`WrongPartition` when the report is about a different day. `HeaderMismatch` when the
columns are not the contract's. `LoadCountMismatch` when the table ends up holding a
different number of rows than the report said it accepted. And `ContentMismatch` when the
rows in the table are not the rows in the file.

The first four are counts and the fifth is not. That distinction is the whole content of
the section above.

## The source is not immutable, and the history table is where that goes

Fetch the same fourteen days again three days after the first extract and four rows out of
179,314 come back different. Nothing was added and nothing was removed. Every partition
returned exactly the row count it returned before, so the completeness guard in
`ingest/fetch.py` is satisfied by all fourteen. That guard asks the API for its own
`count(1)`. Three of the fourteen checksums moved.

```
python scripts/refetch_probe.py --into /tmp/second --write

2025-01-01  rows  10873  changed rows   2  cells   2  added  0  removed  0  sha same False
2025-01-03  rows  11684  changed rows   1  cells   2  added  0  removed  0  sha same False
2025-01-09  rows  14906  changed rows   1  cells   2  added  0  removed  0  sha same False

14 partitions, 179314 rows
3 partitions changed
4 rows changed, 6 cells
columns that moved: {'closed_date': 4, 'status': 2}
```

Two shapes, and they are not the same problem.

```
2025-01-01 63591237 closed_date: '2025-01-03T12:29:42.000' -> '2026-08-28T14:27:34.000'
2025-01-01 63592286 closed_date: '2025-01-03T12:30:25.000' -> '2026-08-28T14:27:34.000'
2025-01-03 63618281 closed_date: '' -> '2026-08-27T14:56:33.000'
                    status:      'In Progress' -> 'Closed'
2025-01-09 63704984 closed_date: '' -> '2026-08-28T13:48:00.000'
                    status:      'Assigned' -> 'Closed'
```

The first two are DOT sidewalk complaints that closed in January 2025 and had their closing
time rewritten nineteen months later, both to the same instant, so one operation touched
both. That is a correction to a historical fact. The other two are requests that were still
in flight and have now finished, which is the workflow arriving very late.

**The contract cannot see any of it.** Every one of those rows satisfies the contract before
and after. `open_request_has_no_closed_date` is happy with `In Progress` and no closing
date, and `closed_request_has_a_closed_date` is happy with `Closed` and one. A contract
judges a partition against a rule, and the thing that changed here is the partition against
itself.

**Nor can the raw layer.** The load is delete then insert on the partition key, which is
what makes a backfill safe, and it is also what destroys the previous value. Reload those
three partitions and the row counts are identical, the ledger reconciles, and the old
closing dates are gone with nothing recording that they existed.

So `snapshots/snap_service_request.sql` is a dbt snapshot on the `check` strategy over
`status` and `closed_at`. The source has no `updated_at` column, so a timestamp strategy
has nothing to read.

```
python scripts/scd2_probe.py --db /tmp/wh.duckdb

first extract
  178742 versions over 178742 keys, 178742 current, 0 superseded

second extract, 3 of 14 partitions changed
  2025-01-01  10841 loaded, 10841 replaced, 119251 cells checked
  2025-01-03  11643 loaded, 11643 replaced, 128073 cells checked
  2025-01-09  14827 loaded, 14827 replaced, 163097 cells checked
  178746 versions over 178742 keys, 178742 current, 4 superseded

keys carrying history: 4
  63618281  status In Progress  closed_at None                 valid_to 2026-08-30 ...
  63618281  status Closed       closed_at 2026-08-27 14:56:33  valid_to None

raw fingerprint after the second load: 178742:a83cd8566c8a
```

Four superseded rows out of 178,746. That is the whole yield and it is the honest size of
it. The point is not the four. It is that the raw table's fingerprint moved while its row
count did not, and without the snapshot there would be no record anywhere that anything
had happened.

The second extract is replayed from `data/extract_diff.json` rather than fetched. That file
is committed and the raw partitions are not, because the live source keeps moving and
nobody can fetch the extract measured here, including a later run of this repo.
`ingest/compare.apply_diff` refuses to replay onto an input whose values have already
moved, so a replay against the wrong file fails rather than producing a plausible one.

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

A count is not a checksum, and the section on the second extract is what that costs. All
fourteen partitions returned the same count on a later fetch and three of them returned
different bytes.

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
                                  every column VARCHAR
                                            |
                                            v
                                  stg.stg_service_requests
                                  try_ casts, zip stays text
                                            |
                         +------------------+------------------+
                         v                                     v
              silver.slv_service_requests            history.snap_service_request
              derived columns, no dedupe             type 2, check strategy
                         |
              +----------+----------+
              v                     v
      silver.dim_complaint_type   gold.gold_agency_daily
      type 1, and the reason      gold.gold_complaint_resolution
      is measured
```

The Airflow side runs end to end. `dags/nyc311_contract_check.py` pulls a partition and
judges it. Then it writes the split, loads what passed and builds the models:

```
bash scripts/dag_smoke.sh 2025-01-13

ran 2025-01-13, tasks: pull check load_raw transform
13049 rows, 13003 accepted, 46 held, 364107 rule evaluations
raw layer holds 13003 rows for 2025-01-13
marts: gold.gold_agency_daily 187, gold.gold_complaint_resolution 156,
       silver.slv_service_requests 178742
history: 178742 versions over 178742 keys, 0 superseded
```

Everything after the task list is asked of the database from outside the run. The load task
already refuses a count it disagrees with, and a task returning without raising is not the
same fact as the rows being there. The marts are asked the same way, because a `dbt build`
that exits zero having compiled nothing looks identical from the exit code.

The `transform` task shells out to `scripts/dbt.sh`. That is not a shortcut. Airflow and
dbt cannot share an install here, so an Airflow worker process cannot import dbt, and a
subprocess is the only shape available.

That script exists because `airflow dags test` on a date outside the DAG's own start and
end window creates a run, executes no task at all, and reports `state=success`. A smoke
test that greps for success passes on a run that did nothing, and it keeps passing forever
once the DAG's `end_date` falls behind the date it uses. So the script asserts each task
name appears in the log and that the report says how many rules it evaluated.

## The layers, and one dimension that is deliberately not type 2

```
bash scripts/dbt.sh build

Found 5 models, 1 snapshot, 23 data tests, 2 sources
Done. PASS=29 WARN=0 ERROR=0 SKIP=0 TOTAL=29
```

Staging is a view and everything below it is a table. Staging is a cast and a rename, so
materialising it buys nothing.

`stg_service_requests` puts types on the text. Every cast is a `try_` cast and
`stg_cast_is_lossless` is what makes that safe. A row fails it when the raw text is present
and the typed value is not, which is the signature of a cast that lost something. The
contract already refused the values that would not convert. It did that upstream and in
Python, before the load, so a hit here means either the contract missed a case or the cast
disagrees with the rule that judged it. It has fired once and the section above is what it
caught.

`incident_zip` stays a string through every layer.

`slv_service_requests` adds `resolution_minutes`, `resolution_hours`, `is_closed` and
`has_location`. There is no deduplication step. `unique_key` is unique across all 178,742
accepted rows and the contract enforces it. The quarantine holds both copies of anything
that collides, so a `distinct` here would be a no-op that reads as rigour.

`gold_agency_daily` is per agency per day. Its three status counts do not sum to the row
count, on purpose. A request can be Assigned or Pending, which is neither open nor closed,
and a two column split invites a reader to assume a partition of the whole. 808 of 178,742
requests have no resolution time.

`gold_complaint_resolution` carries `unresolved_count` beside the median for the same
reason. The median only ever sees the rows that closed, so a complaint type with a fast
median and a large unresolved count is not fast, and a table without that column publishes
survivor bias as performance.

**`dim_complaint_type` is type 1 and the reason is a measurement.** Four of the 156
complaint types are answered by two different agencies, which reads exactly like a routing
change an SCD2 would capture. It is not one:

```
Encampment          DHS and NYPD, both agencies on all 14 days
Graffiti            DSNY and NYPD, both on 13 of 14
Highway Condition   DOT and DSNY, both on 10 of 14
Asbestos            DEP and DOHMH, both on 5 of 10 days it appears
```

They are concurrent, not sequential. A type 2 dimension keyed on `complaint_type` would
open and close a version every time the load order happened to put one agency ahead of the
other, and every version would be an artefact of sort order rather than a fact about the
city. So the agencies are a sorted list on one row. A reader can see there are two and ask
why. A version history would have told them there was a change, which is false.

The type 2 table is on the request instead, because that is the only thing in this feed
whose attributes were measured changing.

## Running the checks

```
python tests/run_all.py
175 passed, 0 failed
```

Plain functions named `check_*`, no framework. Nothing in `tests/` needs Airflow or dbt
installed. `tests/test_dbt_project.py` reads the dbt project rather than running it, and
`scripts/dbt.sh build` is the real check. The DAG is the other gap and
`scripts/dag_smoke.sh` is that one.

A mutation pass over the nine library modules kills 221 of 227, with the control clean
before and after each call:

```
python ../portfolio-program/scripts/mutate.py --repo . \
  --module contracts/rules.py --module contracts/spec.py \
  --module contracts/profile.py --module contracts/validate.py \
  --module contracts/quarantine.py --module ingest/fetch.py \
  --module ingest/compare.py --module warehouse/schema.py \
  --module warehouse/load.py --module warehouse/history.py

221 killed, 6 survived
```

Every module was measured against the tree above. Nothing is carried from an earlier run.

Three survivors were real and all three are closed. `ingest/compare.py` refused a
comparison when either side was empty rather than when both were, which would have thrown
away the loudest thing it could report, a partition that arrived or vanished whole.
`warehouse/history.py` fused a row count and a key count into one select, and on an empty
table both are zero, so a refusal reading the wrong one of them was invisible. And
`verify_partition`'s example cap was never exercised at its default, which is the value the
load itself gets, because every check named the argument.

The six that survive are all constants. The page size and the HTTP timeout in the fetcher,
the read block size in the checksum loop, and two JSON indents. Two are equivalent mutants,
since reading a file in 1 MB or 2 MB blocks produces the same sha256. The rest are values
no offline check can distinguish, and pinning them would be a test asserting that a number
is the number.

One survivor was deleted rather than tested. `warehouse/history` had a query helper taking
a parameter list nobody ever passed, and a mutant flipping its `or` to an `and` changed
nothing under any input. An argument no caller uses is not a branch missing a test.

## What is not built

- **No partition level verdict.** Rows are split and nothing fails the run. When a
  partition is bad enough to reject outright is a policy nobody has argued yet, and a
  threshold invented here would be one chosen by looking at the fourteen partitions it
  would judge. The DAG carries a TODO saying so rather than a task that pretends.
- **Nothing reads the quarantine back.** The held rows are written and no later step
  re-judges them, retries them, or expires them. A quarantine you never empty is a folder.
- **The dbt tests are hand written and the contract is not generating them.** The status
  vocabulary exists twice, once in `contracts/nyc311.yml` and once in
  `models/silver/_silver.yml`, and `tests/test_dbt_project.py` asserts the two lists match.
  That stops them drifting. It is not validation, and the difference matters. Generating
  the dbt tests from the contract makes them a second implementation of one rule, and two
  implementations of one rule cannot be graded against each other. Comparing two copies of
  one literal list can.
- **The snapshot is judged on four rows.** Two extracts three days apart moved four rows
  out of 179,314, so `superseded` is 4. The mechanism is exercised and the sample is tiny,
  and a wider window is a longer wait rather than more code.
- **`invalidate_hard_deletes` is off on the snapshot.** A row missing from a later extract
  is a broken fetch here rather than a deletion, and the completeness guard already refuses
  those. Turning it on would let a truncated response close every row it failed to return.
  On a source where deletions are real, that is the wrong default.
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
