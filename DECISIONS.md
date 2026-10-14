# DECISIONS.md

**1. Indexes.** On `attendance_logs`: a **unique `(emp_code, date)`** index (the natural
key — it both addresses a record and makes the punch-in race safe) and a
**`(date ↓, emp_code ↑)`** index that serves the default listing sort and every
month-wide analytics match, where the date range is the index prefix. On
`employees`: unique `emp_code`, `(department, emp_code)` for the filtered employee
list, and `joined_on` for the headcount filter that drives the department summary.
I considered a standalone `status` index and rejected it: status is low-cardinality
and already filtered cheaply after the date-range index scan.

**2. Punch-in race.** Both requests resolve the same `(emp_code, date)` and call
`insert_one`. There is no check-then-insert; the unique `(emp_code, date)` index
lets exactly one insert commit and the other raises `DuplicateKeyError`, which I
map to **409**. So one 201, the rest 409.

**3. Ties.** I use standard competition ranking (`$rank`): tied employees share a
rank and the next rank is skipped (1, 2, 2, 4). `limit` is applied **after**
ranking — I return every row with `rank <= limit` — so a tie straddling the cutoff
returns *all* tied rows, even if that is more than `limit`. Rows are ordered by
`total_late_minutes` desc, then `emp_code` asc.

**4. Headcount.** The department summary starts from `employees` (filtered to
`joined_on <= end of month`, R9), `$lookup`s the month's logs, then
`$unwind ... preserveNullAndEmptyArrays: true`, so employees with zero logs
survive. Headcount is the size of the distinct `emp_code` set per department.

**5. For 100× the data** I would precompute per-employee, per-day rollups (a
materialized daily summary written at punch-out / correction) so the monthly and
department analytics read a few thousand summary rows instead of scanning millions
of raw logs, and partition/shard `attendance_logs` by month.
