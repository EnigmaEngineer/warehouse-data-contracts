-- Every cast in staging is a try_ cast, so a value that will not convert becomes null
-- instead of stopping the run. That is only safe if something looks at the nulls.
--
-- This is the something. A row fails when the raw text is present and the typed value is
-- not, which is the exact signature of a cast that lost information.
--
-- It is worth having because the contract already refused the values that would not
-- convert. That happened upstream and in Python and before the load. So a hit here is one
-- of two things. Either the contract missed a case, or the cast disagrees with the rule
-- that judged it. Both are real and neither is findable by reading the model.

with typed as (
    select
        s.unique_key,
        r.created_date as raw_created,
        r.closed_date as raw_closed,
        r.latitude as raw_latitude,
        r.longitude as raw_longitude,
        s.created_at,
        s.closed_at,
        s.latitude,
        s.longitude
    from {{ ref('stg_service_requests') }} s
    join {{ source('raw', 'nyc311_service_requests') }} r
      on r.unique_key = s.unique_key
)

select unique_key, 'created_date' as column_name, raw_created as raw_value
from typed where raw_created is not null and created_at is null

union all
select unique_key, 'closed_date', raw_closed
from typed where raw_closed is not null and closed_at is null

union all
select unique_key, 'latitude', raw_latitude
from typed where raw_latitude is not null and latitude is null

union all
select unique_key, 'longitude', raw_longitude
from typed where raw_longitude is not null and longitude is null
