-- One row per request with the things an analyst would otherwise write by hand.
--
-- There is no deduplication step here and that is deliberate. unique_key is unique across
-- all 178,742 accepted rows, the contract enforces it, and the quarantine holds both
-- copies of anything that collides. A distinct or a row_number filter would be a no-op
-- that looks like rigour, and the day it stops being a no-op it would hide the collision
-- rather than report it.
--
-- resolution_hours is null for an open request and that is not the same as zero. Every
-- aggregate downstream has to decide what to do with the nulls rather than being handed a
-- number that already decided for it.
--
-- There is one resolution column and there used to be two. The minutes version had no
-- guard against a closing time before a creation time and this one does, so on the row
-- where that happens the two would have disagreed while looking like the same quantity in
-- different units. Nothing read the minutes one.

select
    unique_key,
    created_at,
    closed_at,
    cast(created_at as date) as created_date,
    partition_day,

    agency,
    complaint_type,
    descriptor,
    borough,
    status,
    incident_zip,
    latitude,
    longitude,

    status = 'Closed' as is_closed,

    -- The contract refuses a closed date before the created date, so this cannot be
    -- negative in the accepted set. It is written as a guard anyway because the day this
    -- model runs against a partition loaded around the contract is the day it matters.
    case
        when closed_at is null then null
        when closed_at < created_at then null
        else date_diff('minute', created_at, closed_at) / 60.0
    end as resolution_hours,

    latitude is not null and longitude is not null as has_location,

    source_sha256,
    loaded_at

from {{ ref('stg_service_requests') }}
