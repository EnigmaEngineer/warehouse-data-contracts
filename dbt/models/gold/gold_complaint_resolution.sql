-- Resolution time by complaint type, joined to the dimension so the agency list travels
-- with it.
--
-- unresolved_count is here because a complaint type with a fast median and a large
-- unresolved count is not fast. The median only ever sees the rows that closed, so a
-- table without that column reports survivor bias as performance.

with requests as (
    select * from {{ ref('slv_service_requests') }}
),

dim as (
    select * from {{ ref('dim_complaint_type') }}
)

select
    d.complaint_type,
    d.agencies,
    d.agency_count,

    count(*) as request_count,
    count(*) filter (where r.is_closed) as closed_count,
    count(*) filter (where r.resolution_hours is null) as unresolved_count,

    round(median(r.resolution_hours), 2) as median_resolution_hours,
    round(quantile_cont(r.resolution_hours, 0.9), 2) as p90_resolution_hours,
    round(max(r.resolution_hours), 2) as slowest_resolution_hours

from requests r
join dim d on d.complaint_type = r.complaint_type
group by d.complaint_type, d.agencies, d.agency_count
