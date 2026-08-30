"""Read the type 2 history table dbt writes and say what it caught.

The snapshot is built by dbt. This reads it, and it lives here rather than in the probe
script because the numbers it produces are the ones the README argues from.

One thing worth stating plainly. On a single extract this table has exactly one version
per request and every `dbt_valid_to` is null, which is what a history table looks like
when nothing has happened yet. A summary that only ever reported that would be reporting a
tautology. So `superseded` is the figure to read and the probe is built to make it
non zero.
"""

SNAPSHOT = "history.snap_service_request"


class NoHistory(RuntimeError):
    """The snapshot table is missing or empty."""


def _count(con, sql):
    return con.execute(sql).fetchone()[0]


def version_summary(con, table=SNAPSHOT):
    """Rows, distinct keys, current versions and superseded ones.

    Refuses an empty table. A summary over no rows is four zeros and it reads exactly like
    a history in which nothing has changed.

    The row count and the key count are two statements rather than one. Fused into a
    single select they come back as row[0] and row[1], and on an empty table both are
    zero, so a refusal reading the wrong one of them is invisible.
    """
    versions = _count(con, "select count(*) from {}".format(table))
    if versions == 0:
        raise NoHistory("{} holds no rows".format(table))

    return {
        "versions": versions,
        "keys": _count(con, "select count(distinct unique_key) from {}".format(table)),
        "current": _count(
            con, "select count(*) from {} where dbt_valid_to is null".format(table)),
        "superseded": _count(
            con, "select count(*) from {} where dbt_valid_to is not null".format(table)),
    }


def keys_with_history(con, table=SNAPSHOT):
    """Every key carrying more than one version, with the values on each side.

    Ordered by key then by dbt_valid_from so the pairs read in the order they happened.
    """
    rows = con.execute(
        "select unique_key, status, closed_at, dbt_valid_from, dbt_valid_to "
        "from {0} where unique_key in ("
        "  select unique_key from {0} group by 1 having count(*) > 1) "
        "order by unique_key, dbt_valid_from".format(table)
    ).fetchall()
    out = {}
    for key, status, closed_at, valid_from, valid_to in rows:
        out.setdefault(key, []).append({
            "status": status,
            "closed_at": closed_at,
            "valid_from": valid_from,
            "valid_to": valid_to,
        })
    return out


def one_row_per_key_is_current(con, table=SNAPSHOT):
    """Keys with anything other than exactly one open version. Should always be empty.

    Two open versions for one key means the snapshot did not close the old one, which is
    the failure that makes a history table quietly wrong rather than loudly broken. Zero
    open versions means every version got closed and the key vanished from the present.
    """
    rows = con.execute(
        "select unique_key, count(*) filter (where dbt_valid_to is null) n "
        "from {} group by 1 having n <> 1 order by 1".format(table)
    ).fetchall()
    return [{"unique_key": k, "open_versions": n} for k, n in rows]
