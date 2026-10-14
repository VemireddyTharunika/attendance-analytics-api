# Employee Attendance & Analytics API

A FastAPI + MongoDB service for employee attendance: punch in/out with the
business rules R1–R10, manual corrections with an audit trail, and analytics
built on MongoDB aggregation pipelines. All application code lives in
[`app/main.py`](app/main.py).

## Run

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env             # set MONGO_URI / MONGO_DB
python sample_seed.py            # optional: load the 9 sample documents

uvicorn app.main:app --port 8000
```

`MONGO_URI` and `MONGO_DB` are read from the environment (a local `.env` is
loaded for convenience, but real environment variables win). Requires
**MongoDB 6.0+** (the trend endpoint uses `$densify` / `$setWindowFields`).
Interactive docs at `http://localhost:8000/docs`.

Indexes are created **idempotently at startup**, so the app can be pointed at a
database that already holds data with no manual steps.

## What's implemented

All endpoints in the contract:

| Method | Path | Notes |
|---|---|---|
| GET | `/health` | 200 only if MongoDB answers a ping, else 503 |
| POST / GET | `/employees` | create (unique `emp_code`, 409 on dup) / list |
| POST | `/attendance/punch-in` | R1 date + R2 late; race-safe via unique `(emp_code,date)` |
| POST | `/attendance/punch-out` | R3/R4/R5; works across midnight; race-safe |
| GET | `/attendance` | filters + pagination, index-backed sort/skip/limit |
| PATCH | `/attendance/{emp_code}/{date}` | regularize + append to `history`; optimistic-concurrency safe |
| GET | `/analytics/employees/{emp_code}/monthly` | aggregation |
| GET | `/analytics/departments/summary` | cross-collection aggregation; zero-log employees counted |
| GET | `/analytics/leaderboard/late` | `$rank` competition ranking; `limit` after ranking |
| GET | `/analytics/departments/{department}/trend` | `$densify` gap-fill + 7-day moving average |
| GET | `/admin/explain/{endpoint}` | raw `explain(executionStats)` for each endpoint's main query |

## Design highlights

- **Money of this exercise is time.** Instants cross the API as integer epoch
  **milliseconds** and are stored as BSON UTC datetimes; every instant is
  truncated to whole seconds before storing/returning (R1). Calendar dates,
  months and shift times stay IST strings.
- **Derived fields** (`work_hours`, `late_minutes`, `overtime_minutes`,
  `half_day`) are computed on write and **read as stored** by analytics — never
  recomputed from punch times.
- **Half-up rounding** (R4/R8) uses `Decimal`; the trend rates are rounded
  half-up *inside* the pipeline so the moving average is computed from the
  rounded values.
- **Concurrency:** unique indexes make duplicate punch-ins/creates resolve to a
  single success; punch-out uses a conditional update on `punch_out: null`; and
  regularize uses an optimistic guard on the `history` length so concurrent
  corrections cannot drop each other's audit entries.
- **Indexes & speed:** see [`DECISIONS.md`](DECISIONS.md). `/admin/explain`
  proves each main query/pipeline uses an `IXSCAN` and no `COLLSCAN`.

See [`REVIEW.md`](REVIEW.md) for the Part-A defects fixed in the starter and
[`DECISIONS.md`](DECISIONS.md) for the five design answers.

## Nothing is knowingly omitted

Every endpoint and rule in the contract is implemented. The analytics were
tested against the nine samples plus hand-built edge cases (ties, weekend
records, a mid-month joiner, an overnight shift, an employee with no logs, and a
missing weekday).
