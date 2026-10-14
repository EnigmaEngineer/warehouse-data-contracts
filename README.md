# Warehouse Data Contracts

An Airflow and dbt pipeline that refuses to publish data breaking its contract. Contracts
are YAML, they carry the provenance of every rule and they generate both the ingestion
check and the warehouse tests.

```
bash scripts/bootstrap-local.sh
python scripts/pull_source.py --start 2025-01-01 --days 14
python scripts/profile_source.py
python scripts/quarantine_partition.py --write
python scripts/load_raw.py --twice
bash scripts/dbt.sh build
python scripts/break_probe.py
```

The last one breaks the contract eleven ways on purpose and checks that each refusal came
from the rule it names.

The source is the NYC 311 service request feed, dataset `erm2-nwe9` on
`data.cityofnewyork.us`. Real municipal data with real defects in it, rather than a
generator I wrote. That matters more than it sounds and the next section is why.

## The contract found nothing and that was the finding

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
one value at a time cannot see a request closed before it was created and that is most of
what is actually wrong with this feed.

Both groups have a signature, which is what says they are real rather than a parsing
artefact. All 493 requests closed with no closing date are DHS and all 493 are
`Homeless Person Assistance`. All 12 requests closed before they were created are DOT and
several are negative by exactly three or six days with the clock time unchanged, which is a
date component being written wrong somewhere upstream rather than noise.

The rest of the story follows from that. A row that breaks a cross column check is held in
quarantine rather than dropped, the marts are rebuilt from what is left, and a held row can
be re-judged later. `scripts/break_probe.py` then breaks every rule on purpose, each beside a
control, and checks that the refusal came from the rule it names.

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
                                    warehouse/load.py      re-judged by
                                    columns= from the      contracts/replay.py
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
              +----------+----------------------+
              v                                  v
      silver.dim_complaint_type          gold.gold_agency_daily
      type 1 and the reason
      is measured
              |
              +--------> gold.gold_complaint_resolution
                         joined to the dimension for the agency list
```

Every transcript in these docs is checked against the program that prints it. Change a
script's output and the docs fail the suite until they are updated, and the lineage block in
the design notes is compared against the dbt models the same way.

```
python tests/run_all.py
python scripts/readme_check.py
python scripts/lineage.py --check
```

## Known limitations

- No partition level verdict. Rows are split and nothing fails the run.
- The quarantine can be re-judged and not retried or expired.
- A backfill over a range is not atomic. Each partition commits alone.
- Holding a row changes the marts, because they are rebuilt from what is left.
- Nothing fails a run on freshness or volume. Both clauses are evaluated and printed.
- No Snowflake. The warehouse is duckdb through dbt-duckdb.
- One writer. duckdb takes a single write lock, so two DAG runs cannot load at once.
- Nothing tests the network path.

The full list is in [docs/limitations.md](docs/limitations.md).

## More detail

**[Design notes](docs/design-notes.md)**

- [584 is not a number of rows and neither is 493](docs/design-notes.md#584-is-not-a-number-of-rows-and-neither-is-493)
- [Quarantine](docs/design-notes.md#quarantine)
- [The raw layer is all text and three rows are the reason](docs/design-notes.md#the-raw-layer-is-all-text-and-three-rows-are-the-reason)
- [The explicit types did not help and here is what they missed](docs/design-notes.md#the-explicit-types-did-not-help-and-here-is-what-they-missed)
- [Loading a partition twice costs nothing](docs/design-notes.md#loading-a-partition-twice-costs-nothing)
- [A backfill is a range and a directory listing is not one](docs/design-notes.md#a-backfill-is-a-range-and-a-directory-listing-is-not-one)
- [Every rule, broken on purpose, with a control](docs/design-notes.md#every-rule-broken-on-purpose-with-a-control)
- [One replayed job holds a whole partition and 110 rows were the bad ones](docs/design-notes.md#one-replayed-job-holds-a-whole-partition-and-110-rows-were-the-bad-ones)
- [Two of sixteen constraints are the only reason anything is held](docs/design-notes.md#two-of-sixteen-constraints-are-the-only-reason-anything-is-held)
- [The marts are not left intact, they are rebuilt](docs/design-notes.md#the-marts-are-not-left-intact-they-are-rebuilt)
- [Reading the quarantine back](docs/design-notes.md#reading-the-quarantine-back)
- [The source is not immutable and the history table is where that goes](docs/design-notes.md#the-source-is-not-immutable-and-the-history-table-is-where-that-goes)
- [Provenance is a field on every rule](docs/design-notes.md#provenance-is-a-field-on-every-rule)
- [The first version of this reported zero because it checked nothing](docs/design-notes.md#the-first-version-of-this-reported-zero-because-it-checked-nothing)
- [Completeness, not just a fetch](docs/design-notes.md#completeness-not-just-a-fetch)
- [Airflow and dbt cannot share an environment](docs/design-notes.md#airflow-and-dbt-cannot-share-an-environment)
- [Lineage, read out of the models](docs/design-notes.md#lineage-read-out-of-the-models)
- [The layers and one dimension that is deliberately not type 2](docs/design-notes.md#the-layers-and-one-dimension-that-is-deliberately-not-type-2)
- [Every refusal, from a command line](docs/design-notes.md#every-refusal-from-a-command-line)
- [The README's own transcripts are checked](docs/design-notes.md#the-readmes-own-transcripts-are-checked)
- [A lag is a difference and the clause named one side of it](docs/design-notes.md#a-lag-is-a-difference-and-the-clause-named-one-side-of-it)
- [The floor that could not fire and the first live day that tripped it](docs/design-notes.md#the-floor-that-could-not-fire-and-the-first-live-day-that-tripped-it)
- [The contract generates nine of its twenty three constraints](docs/design-notes.md#the-contract-generates-nine-of-its-twenty-three-constraints)

**[Running the checks](docs/running-the-checks.md)**
