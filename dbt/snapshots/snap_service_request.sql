{% snapshot snap_service_request %}

{{
    config(
      unique_key='unique_key',
      strategy='check',
      check_cols=['status', 'closed_at'],
      invalidate_hard_deletes=False,
    )
}}

-- Type 2 history on the request, not on a lookup table, because the request is the only
-- thing in this feed whose attributes were measured changing.
--
-- The check strategy rather than timestamp. There is no updated_at column in the source.
-- closed_date moves when a request closes and it also moves when somebody corrects a
-- closing that already happened, so it is a tracked column rather than the thing driving
-- the comparison.
--
-- Why this exists at all. The raw layer replaces a partition on reload, delete then
-- insert on the partition key. That makes a backfill safe and it also means the previous
-- value of a row is gone the moment the same partition is fetched again. A second extract
-- of the same fourteen days, taken three days after the first, differs on four rows out
-- of 179,314. Two closed dates were rewritten nineteen months after the fact and two
-- requests moved out of In Progress and Assigned into Closed. Without this snapshot the
-- raw table would now hold the new values and nothing anywhere would record that they had
-- ever been anything else.
--
-- invalidate_hard_deletes is off because a row missing from a later extract is not a
-- deletion here. The window is a fixed archive and a partition that came back short is a
-- broken fetch, which ingest/fetch.py already refuses by asking the API for its own row
-- count. Turning this on would let a truncated response close every row it failed to
-- return.

select
    unique_key,
    created_at,
    closed_at,
    status,
    agency,
    complaint_type,
    partition_day,
    source_sha256
from {{ ref('stg_service_requests') }}

{% endsnapshot %}
