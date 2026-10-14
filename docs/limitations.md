# Limitations

Moved out of the README so the front page stays short. Back to the [README](../README.md).

## What is not built

- **No partition level verdict.** Rows are split and nothing fails the run. When a
  partition is bad enough to reject outright is a policy nobody has argued yet and a
  threshold invented here would be one chosen by looking at the fourteen partitions it
  would judge. The DAG carries a TODO saying so rather than a task that pretends.
- **The quarantine can be re-judged and not retried or expired.** Held rows never re-enter
  the accepted file and nothing ages them out. The reasons are in the section on reading the
  quarantine back and both are policy questions rather than missing code.
- **A backfill converges the days it is given and cannot remove one it is not.** A partition
  the source has dropped stays in the table, reconciles cleanly and keeps feeding the
  marts. `backfill.orphans` reports them and deliberately deletes nothing.
- **A backfill over a range is not atomic.** Each partition commits alone, so a failure
  halfway leaves the earlier ones in and the later ones out. `BackfillStopped` names where
  it stopped, which makes it resumable by hand and not automatically.
- **Holding a row changes the marts.** They are rebuilt from what is left, so a withheld row
  shows up in gold as one fewer request and nothing distinguishes that from a quieter day.
  The rejection report is the only place the difference is recorded.
- **No cap on how much of a partition one key collision can take.** A replayed upstream job
  holds the whole file. The report separates the bystanders from the genuinely bad rows and
  a threshold on top of that would be a number chosen by looking at this corpus.
- **The dbt arm of `scripts/break_probe.py` is not covered by any check.** It needs dbt on
  the machine and a warehouse that is already built, so it sits in the same gap as
  `scripts/dag_smoke.sh`. It also copies the whole duckdb file, which is fine at this size
  and would not be later.
- **`backfill.orphans` reads the table and never the ledger.** A partition in the ledger
  with no rows behind it is `load.reconcile`'s job. Between them the two states are covered
  and neither function says so on its own.
- **Nine of twenty three contract constraints reach dbt and the other fourteen stay in the
  validator.** That is not a gap to close. dbt core has four generic tests and no package is
  installed here, so a range bound or a regex or a two column rule has nowhere to go. The
  silver models still carry hand written tests for the same reason.
- **Nothing fails a run on freshness or volume.** Both clauses are evaluated and both print
  a verdict and the DAG carries on either way. A live day that is still being published
  trips the volume floor legitimately, so what a `below_floor` verdict should do to a run is
  the same policy question the row level split has and inventing a threshold here would be
  choosing one by looking at the partitions it judges.
- **`applies_to: tail` means newest in the manifest, not newest in the feed.** A backfill
  still reads stale on its last partition. It stops the other thirteen alerts and it does
  not tell a backfill apart from a broken feed, which needs the DAG to say which one it is
  doing.
- **`data/warehouse.duckdb` is gitignored and can be older than the loader.** The copy on
  this machine was built before the hive partitioning fix and still held the overwritten
  column and `scripts/dag_smoke.sh` defaults to exactly that path. Rebuild with
  `python scripts/load_raw.py` rather than trusting a database that is not in the history.
- **The snapshot is judged on four rows.** Two extracts three days apart moved four rows
  out of 179,314, so `superseded` is 4. The mechanism is exercised and the sample is tiny,
  and a wider window is a longer wait rather than more code.
- **`invalidate_hard_deletes` is off on the snapshot.** A row missing from a later extract
  is a broken fetch here rather than a deletion and the completeness guard already refuses
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
  a shared filesystem assumption. Two tasks on two workers do not have one and the answer
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
  against stubs. The real round trip runs in `scripts/pull_source.py` and in the DAG and
  neither is in `tests/`.
