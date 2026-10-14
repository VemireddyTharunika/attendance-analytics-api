# REVIEW.md

Defects found in the starter `app/main.py` while reviewing it as a pull request.
Everything below is fixed in the submitted `app/main.py` (rewritten as one file).
Line numbers refer to the original starter.

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | module, `MongoClient(...)` (L22) | Client is not `tz_aware`, so PyMongo returns **naive** datetimes. Every epoch-ms conversion is then ambiguous/off. | Read a seeded record; `punch_in` round-trips to the wrong millisecond vs the sample. | `MongoClient(..., tz_aware=True, tzinfo=timezone.utc)`; one `to_ms`/`from_ms` pair does all conversion. |
| 2 | startup | No indexes are created. Uniqueness is not enforced and list/analytics would collection-scan. | `GET /admin/explain/*` shows `COLLSCAN`; duplicate `emp_code`/day records can be inserted. | `ensure_indexes()` in the lifespan: unique `emp_code`, unique `(emp_code,date)`, `(date,-1 emp_code,1)`, `(department,emp_code)`, `joined_on`. |
| 3 | `health` (L72) | Returns `{"status":"ok"}` unconditionally; never touches Mongo. | Stop Mongo, call `/health` → still 200; contract wants 503. | `db.command("ping")` inside try/except → 503 on failure. |
| 4 | `compute_late_minutes` (L33) | Builds shift start with `punch_in.replace(hour,minute)` in the punch-in's own (UTC/naive) clock, but `shift_start` is **IST** (R1). Wrong by 5:30. | Punch-in 09:28 IST on a 09:30 shift reports large `late_minutes`. | Build the scheduled start as an IST instant on the attendance date, subtract in UTC. |
| 5 | `compute_late_minutes` (L37) | Threshold `minutes > 10` with `minutes = int(sec/60)` under-reports. R2 is "strictly more than 10:00 (600 s), then floor of minutes **since shift_start**". | Shift 09:30, punch 09:40:30 (630 s) → helper returns 0; correct is 10. | `if elapsed > 600: return elapsed // 60 else 0` on whole seconds. |
| 6 | `compute_work_hours` (L41) | `round(x, 2)` is **banker's** rounding on a float; R4 requires half-up. | `work_hours == 9.125` rounds to 9.12 (banker) not 9.13 (half-up). | `Decimal(secs)/Decimal(3600)` quantized `ROUND_HALF_UP`. |
| 7 | `compute_overtime` (L44-47) | Builds `shift_end` on the **same** calendar day; for an overnight shift (22:00-06:00) the end is the next day (R1/R3). Also no "≥30 min" floor (R3) and mixes IST-aware `end` with a naive `punch_out`. | Overnight sample EMP0005: overtime comes out hugely wrong or raises `TypeError`. | `scheduled_end_instant` adds a day for overnight shifts; only count minutes when `>= 30`. |
| 8 | `create_employee` (L79) | Check-then-insert duplicate detection is a race and is not backed by a unique index. | Two concurrent creates of the same `emp_code` both succeed → two documents. | Rely on the unique index; catch `DuplicateKeyError` → 409. |
| 9 | `create_employee` (L82) | `datetime.now()` is naive local time; stored (and later read) as the wrong instant. | `created_at` drifts by the server's UTC offset. | `datetime.now(timezone.utc)` truncated to whole seconds. |
| 10 | `create_employee` (L85) | Returns the raw doc with `created_at` as a datetime (serialized as an ISO string), not epoch ms. | Response `created_at` is `"2026-..."`, contract wants an integer. | `serialize_employee` returns `created_at` as epoch ms. |
| 11 | `create_employee` | No validation of `emp_code` pattern, `email`, `HH:MM`, or `shift_start != shift_end`. | `POST` with `shift_start == shift_end` (or email `"x"`) returns 201. | Pydantic patterns + a model validator reject these with 422. |
| 12 | `list_employees` (L93) | `skip = page * page_size` is off by one; page 1 skips the first page (R10: page starts at 1). | `?page=1` returns the *second* page. | `skip = (page-1)*page_size`. |
| 13 | `list_employees` (L94) | `total = count_documents({})` ignores the `department` filter. | `?department=Sales` returns `total` for the whole collection. | `count_documents(query)` with the same filter. |
| 14 | `list_employees` (L95) | Not sorted; contract requires `emp_code` ascending, and `created_at` is returned as a datetime. | Order is arbitrary; `created_at` is a string. | `.sort(emp_code asc)`; `serialize_employee` for the shape. Also `page/page_size` bounds (`ge/le`). |
| 15 | `punch_in` (L101) | `emp` is never checked for `None`; an unknown employee hits `emp["shift_start"]` → `TypeError` → **500**. | `POST /attendance/punch-in {"emp_code":"NOPE"}` → 500, should be 404. | Explicit `if emp is None: raise HTTPException(404, ...)`. |
| 16 | `punch_in` (L102) | `datetime.fromtimestamp(ms/1000)` is **local-time naive**; float division. | Stored `punch_in` is off by the server offset. | `from_ms` → UTC, truncated to whole seconds (R1). |
| 17 | `punch_in` (L103) | `d = ts.date()` uses the UTC/local date, not the **IST attendance date**, and ignores the overnight rule (R1). | A 21:55 IST punch on a 22:00 overnight shift lands on the wrong `date`. | `attendance_date()` implements R1 incl. overnight. |
| 18 | `punch_in` (L104-105) | Check-then-insert for the day's record is a race and not index-backed. | Two simultaneous punch-ins → two records for the same day. | Unique `(emp_code,date)` + catch `DuplicateKeyError` → exactly one 201, rest 409. |
| 19 | `punch_in` (L100, model) | `status` is an unchecked `str`; `ABSENT`/`LEAVE` are accepted at punch-in. `punched_at` has no range check (seconds/floats pass). | `{"status":"LEAVE"}` creates a bogus record; `punched_at: 1.0e12` accepted. | `Literal[PRESENT,WFH,ON_DUTY]`; strict int `ge/le` epoch bounds → 422. |
| 20 | `punch_in` (L119-121) | Response adds `"id"` (not in contract) and returns `punch_in` as a datetime. | Response shape fails the schema. | `serialize_attendance` returns the exact contract shape with epoch ms. |
| 21 | `list_attendance` (L144) | `list(find(q))` loads the **entire** filtered set into memory, then sorts/slices in Python. Fails at 100k. | Large dataset: slow, high memory; `explain` would show no server-side limit. | Server-side `.sort().skip().limit()` + `count_documents`. |
| 22 | `list_attendance` (L145) | Sort is `date` desc only; missing the `emp_code` asc tiebreak. | Rows with equal `date` come back in arbitrary order. | `[("date",-1),("emp_code",1)]`, matching the index. |
| 23 | `list_attendance` | No `date_from <= date_to` check; records returned with datetimes + `id`. | `date_from=2026-02-01&date_to=2026-01-01` → 200 with empty/garbage instead of 422. | Explicit 422; `serialize_attendance` for the shape. |

## Examined and decided **not** a defect
- **`emp_code` as the external identifier** and `_id` kept internal — correct per the data model; kept.
- **`status` defaulting to `PRESENT`** at punch-in — matches the contract default; kept.
- **`IST = timezone(timedelta(hours=5, minutes=30))`** — the offset is correct; reused.
- **Projection idea `{"_id": 0}`** in the starter list — harmless, but I drop `_id` in the serializer instead so one function owns the output shape.
- The starter helpers were **not** reused as-is: each had a correctness bug (4-7), so they were rewritten rather than called.
