-- A type 1 dimension, and the reason it is type 1 is the interesting part.
--
-- complaint_type looks like it has a slowly changing attribute. Four of the 156 types in
-- this corpus are answered by two different agencies, which reads exactly like a routing
-- change that an SCD2 would capture.
--
-- It is not one. Encampment is handled by DHS and by NYPD on all fourteen days, Graffiti
-- by DSNY and NYPD on thirteen of them. The two agencies are concurrent, not sequential.
-- An SCD2 keyed on complaint_type would open and close a version every time the load
-- order happened to put one agency before the other, and every one of those versions
-- would be an artefact of sort order rather than a fact about the city.
--
-- So the agencies are a list on one row. A reader can see there are two and ask why. A
-- version history would have told them there was a change, which is false.

with requests as (
    select * from {{ ref('slv_service_requests') }}
)

-- Four columns. It had seven and the other three were first_seen_at, last_seen_at and a
-- closed count. Over a fixed fourteen day archive the two timestamps are the bounds of the
-- corpus rather than a fact about the complaint type, and the closed count is already in
-- gold_complaint_resolution beside the median it belongs with.
select
    complaint_type,
    list_sort(list(distinct agency)) as agencies,
    count(distinct agency) as agency_count,
    count(*) as request_count
from requests
group by complaint_type
