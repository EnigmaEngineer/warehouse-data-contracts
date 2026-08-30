-- Requests per agency per day, with resolution time.
--
-- The median is quantile_cont rather than a hand written nearest rank, and it is the only
-- definition used anywhere in this repo. Two implementations of a quantile are two
-- different correct answers, and they meet in a total without anything noticing.
--
-- open_count and closed_count do not have to sum to request_count. A request can be
-- Assigned or Pending, which is neither. Anyone reading a two column split and assuming a
-- partition of the whole gets a wrong number, so the third column is here.

select
    created_date,
    agency,

    count(*) as request_count,
    count(*) filter (where is_closed) as closed_count,
    count(*) filter (where status = 'Open') as open_count,
    count(*) filter (where not is_closed and status <> 'Open') as in_flight_count,

    -- Over the closed rows only. Averaging a null as zero would report an agency that
    -- closes nothing as instant.
    round(median(resolution_hours), 2) as median_resolution_hours,
    round(quantile_cont(resolution_hours, 0.9), 2) as p90_resolution_hours,

    count(*) filter (where has_location) as located_count

from {{ ref('slv_service_requests') }}
group by created_date, agency
