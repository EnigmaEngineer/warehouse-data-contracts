# warehouse-data-contracts

An Airflow and dbt pipeline that refuses to publish data breaking its contract. Contracts
are YAML, they carry the provenance of every rule, and they generate both the ingestion
check and the warehouse tests.

```
bash scripts/bootstrap-local.sh
python scripts/pull_source.py --start 2025-01-01 --days 14
python scripts/profile_source.py
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
  cross column failures: 584 rows, 0.00326 of the corpus
```

584 rows. The thresholds were never the problem. The shape of the rule was. A constraint
that sees one value at a time cannot see a request closed before it was created, and that
is most of what is actually wrong with this feed.

Both groups have a signature, which is what says they are real rather than a parsing
artefact. All 493 requests closed with no closing date are DHS, and all 493 are
`Homeless Person Assistance`. All 12 requests closed before they were created are DOT, and
several are negative by exactly three or six days with the clock time unchanged, which is a
date component being written wrong somewhere upstream rather than noise.

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
   data/manifest.json                          |
   rows + sha256 per partition                 |  per column and cross column
                                               v
                                        contracts/profile.py
                                               |
                                               v
                                    quarantine  ->  raw  ->  dbt  ->  mart
                                    (not built yet)
```

The Airflow side runs. `dags/nyc311_contract_check.py` pulls a partition and profiles it,
end to end:

```
airflow dags test nyc311_contract_check 2025-01-14

pull    -> {'partition': '2025-01-14', 'rows': 10079, 'expected_rows': 10079, ...}
profile -> {'rows': 10079, 'column_rules_broken': 0,
            'rows_failing_cross_column_checks': 33}
state=success
```

## Running the checks

```
python tests/run_all.py
69 passed, 0 failed
```

Plain functions named `check_*`, no framework. Nothing in `tests/` needs Airflow or dbt
installed.

A mutation pass over the four library modules kills 115 of 120, with the control clean
before and after:

```
python ../portfolio-program/scripts/mutate.py --repo . \
  --module contracts/rules.py --module contracts/spec.py \
  --module contracts/profile.py --module ingest/fetch.py

115 killed, 5 survived
```

The first pass killed 100 of 124 and the survivors are the reason the check count moved.
Three were code nobody called, an accessor on `Contract` and two methods on `Profile`, and
they were deleted rather than tested. Two were fixtures split evenly between good and bad
rows, which pass just as happily against an inverted rule, because the count of failures
does not move. One was the paging loop in the fetcher, where turning `len(page) < PAGE` into
`<=` stops after the first full page. No partition here is near 50,000 rows so that branch
never runs against the real source and nothing would have noticed.

The five survivors are all constants in `ingest/fetch.py`. The page size and the HTTP
timeout. The read block size in the checksum loop and the manifest's JSON indent. Two are
equivalent mutants, since reading a file in 1 MB or 2 MB blocks produces the same sha256.
The other three are values no offline check can distinguish.

## What is not built

- **No quarantine.** The profiler reports and does not decide. Nothing routes a bad batch
  aside and nothing stops a downstream task, because the row level validator that makes
  that call does not exist. The DAG carries a TODO saying so rather than a task that
  pretends.
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
