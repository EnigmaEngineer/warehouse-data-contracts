-- The mart and the layer under it have to agree about how many requests exist.
--
-- A group by that drops rows is silent. A null in a grouping key, a join that turned out
-- not to be one to one, a filter somebody added to fix a chart. None of them raise and
-- all of them change a total that a person is about to read off a dashboard.
--
-- Two aggregates over one grain, compared. Both marts are checked because they group by
-- different things, and a bug in the join in one is invisible from the other.

with silver_total as (
    select count(*) as n from {{ ref('slv_service_requests') }}
),

agency_total as (
    select sum(request_count) as n from {{ ref('gold_agency_daily') }}
),

complaint_total as (
    select sum(request_count) as n from {{ ref('gold_complaint_resolution') }}
)

select 'gold_agency_daily' as mart, a.n as mart_rows, s.n as silver_rows
from agency_total a, silver_total s
where a.n is distinct from s.n

union all

select 'gold_complaint_resolution', c.n, s.n
from complaint_total c, silver_total s
where c.n is distinct from s.n
