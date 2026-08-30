-- The cast layer. Raw is text and this is where the text becomes something with a type.
--
-- Two decisions worth explaining, because both look wrong until you know why.
--
-- incident_zip stays a string. It is an identifier that happens to be written in digits,
-- and three rows in this corpus start with a zero. cast('00083' as bigint) is 83, and the
-- contract's five digit rule then passes on the file and fails on the table.
--
-- Every cast is a try_ cast. A value that will not convert lands as null rather than
-- killing the model, and stg_cast_is_lossless is what turns that null into a failure. A
-- cast that raises tells you a row is bad. A cast that nulls, with a check behind it,
-- tells you which row and lets the rest of the run finish.

select
    unique_key,

    -- strptime with the format the publisher actually writes rather than a bare cast.
    -- A cast would accept several shapes and quietly agree with whichever arrived.
    try_strptime(created_date, '%Y-%m-%dT%H:%M:%S.%f') as created_at,
    try_strptime(closed_date, '%Y-%m-%dT%H:%M:%S.%f') as closed_at,

    agency,
    complaint_type,
    descriptor,

    incident_zip,

    borough,
    status,

    try_cast(latitude as double) as latitude,
    try_cast(longitude as double) as longitude,

    _partition as partition_day,
    _source_sha256 as source_sha256,
    _loaded_at as loaded_at

from {{ source('raw', 'nyc311_service_requests') }}
