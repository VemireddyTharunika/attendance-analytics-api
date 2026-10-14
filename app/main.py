"""
Employee Attendance & Analytics API
===================================

A single-file FastAPI + MongoDB service. Run from the repository root:

    uvicorn app.main:app --port 8000

Configuration (environment variables; a local ``.env`` is loaded for convenience,
but real environment variables always win):

    MONGO_URI   MongoDB connection string   (default: mongodb://localhost:27017)
    MONGO_DB    Database name                (default: attendance_db)

Design notes that apply across the whole file
---------------------------------------------
* **Identity.** Employees are addressed by ``emp_code`` (a customer code such as
  ``EMP0001``), attendance by the natural key ``(emp_code, date)``. MongoDB's
  ``_id`` is never accepted or returned.
* **Instants** cross the API boundary as integer **epoch milliseconds** and are
  stored as BSON datetimes (UTC). The MongoClient is ``tz_aware`` so reads come
  back as timezone-aware UTC datetimes; conversion lives in :func:`to_ms` /
  :func:`from_ms`. Per R1 every instant is truncated to whole seconds before it
  is stored or returned.
* **Calendar values** (``date`` = YYYY-MM-DD, ``month`` = YYYY-MM,
  ``shift_start`` / ``shift_end`` = HH:MM) are IST strings, stored verbatim.
* **Derived fields** (``work_hours``, ``late_minutes``, ``overtime_minutes``,
  ``half_day``) are computed here and never accepted from a client. Analytics
  read the *stored* derived values; they never recompute them from punch times.
* **Business rules R1-R10** are implemented in the helper functions and the
  aggregation builders; each is referenced by number where it is applied.
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from datetime import date as date_cls
from datetime import datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Literal, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import DuplicateKeyError

# --------------------------------------------------------------------------- #
# Configuration and database handle
# --------------------------------------------------------------------------- #
# ``load_dotenv`` does not override variables that already exist in the real
# environment, so injected MONGO_URI / MONGO_DB win over any local .env.
load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

# tz_aware=True -> PyMongo returns timezone-aware UTC datetimes instead of naive
# ones, which keeps the epoch-millisecond conversion unambiguous.
client: MongoClient = MongoClient(MONGO_URI, tz_aware=True, tzinfo=timezone.utc)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

PRESENCE_STATUSES = ("PRESENT", "WFH", "ON_DUTY")
ALL_STATUSES = ("PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY")

# Epoch-millisecond bounds shared by the schema and validators (see EpochMs).
MS_MIN = 100_000_000_000          # anything smaller looks like seconds -> 422
MS_MAX = 4_102_444_800_000        # 2100-01-01, a generous upper sanity bound


# --------------------------------------------------------------------------- #
# Time / instant helpers
# --------------------------------------------------------------------------- #
def to_ms(dt: Optional[datetime]) -> Optional[int]:
    """BSON datetime -> epoch milliseconds (integer). ``None`` passes through.

    Uses integer arithmetic so there is no floating-point rounding. Naive
    datetimes are assumed to be UTC (a safety net; the client is tz-aware).
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    delta = dt - EPOCH
    return delta.days * 86_400_000 + delta.seconds * 1_000 + delta.microseconds // 1_000


def from_ms(ms: Optional[int]) -> Optional[datetime]:
    """Epoch milliseconds -> UTC datetime, truncated to whole seconds (R1)."""
    if ms is None:
        return None
    return EPOCH + timedelta(seconds=ms // 1_000)


def parse_hhmm(hhmm: str) -> time:
    h, m = hhmm.split(":")
    return time(int(h), int(m))


def is_overnight(shift_start: str, shift_end: str) -> bool:
    """Overnight shift: shift_end <= shift_start (e.g. 22:00-06:00). (R1)"""
    return parse_hhmm(shift_end) <= parse_hhmm(shift_start)


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    """The IST calendar date a punch-in belongs to (R1).

    Normal shift: the IST date of the punch-in. Overnight shift: a punch-in whose
    IST time of day is *earlier than* ``shift_end`` belongs to the shift that
    started the previous day.
    """
    ist_dt = punch_in.astimezone(IST)
    d = ist_dt.date()
    if is_overnight(shift_start, shift_end) and ist_dt.time() < parse_hhmm(shift_end):
        d = d - timedelta(days=1)
    return d.isoformat()


def scheduled_start_instant(att_date: str, shift_start: str) -> datetime:
    """UTC instant of ``shift_start`` on the attendance date, in IST. (R1/R2)"""
    d = date_cls.fromisoformat(att_date)
    return datetime.combine(d, parse_hhmm(shift_start), tzinfo=IST).astimezone(UTC)


def scheduled_end_instant(att_date: str, shift_start: str, shift_end: str) -> datetime:
    """UTC instant of ``shift_end``. For an overnight shift the end falls on the
    next calendar day. (R1/R3)"""
    d = date_cls.fromisoformat(att_date)
    if is_overnight(shift_start, shift_end):
        d = d + timedelta(days=1)
    return datetime.combine(d, parse_hhmm(shift_end), tzinfo=IST).astimezone(UTC)


# --------------------------------------------------------------------------- #
# Derived-field rules R2-R5
# --------------------------------------------------------------------------- #
def compute_late_minutes(punch_in: datetime, att_date: str, shift_start: str) -> int:
    """R2 - late only if strictly more than 10:00 after shift_start; then the
    floor of whole minutes elapsed *since shift_start* (not since grace end)."""
    start = scheduled_start_instant(att_date, shift_start)
    elapsed = int((punch_in - start).total_seconds())
    if elapsed > 600:  # strictly more than 10 minutes
        return elapsed // 60
    return 0


def compute_overtime_minutes(
    punch_out: datetime, att_date: str, shift_start: str, shift_end: str
) -> int:
    """R3 - floor of whole minutes from shift_end to punch_out, counted only
    when that is >= 30; otherwise 0."""
    end = scheduled_end_instant(att_date, shift_start, shift_end)
    delta = int((punch_out - end).total_seconds())
    if delta < 0:
        return 0
    minutes = delta // 60
    return minutes if minutes >= 30 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4 - (punch_out - punch_in) seconds / 3600, 2 decimals half-up."""
    secs = int((punch_out - punch_in).total_seconds())
    return float((Decimal(secs) / Decimal(3600)).quantize(Decimal("0.01"), ROUND_HALF_UP))


def compute_half_day(work_hours: float) -> bool:
    """R5 - true when the rounded work_hours < 4.50."""
    return work_hours < 4.50


def round_half_up(value: Optional[float], places: int) -> Optional[float]:
    """R8 - round to ``places`` decimals, half-up. ``None`` passes through."""
    if value is None:
        return None
    q = Decimal(1).scaleb(-places)  # 10**-places
    return float(Decimal(str(value)).quantize(q, ROUND_HALF_UP))


# --------------------------------------------------------------------------- #
# Calendar helpers
# --------------------------------------------------------------------------- #
def month_bounds(month: str) -> tuple[str, str]:
    """``YYYY-MM`` -> (first_day, last_day) as YYYY-MM-DD strings."""
    year, mon = int(month[:4]), int(month[5:7])
    first = date_cls(year, mon, 1)
    nxt = date_cls(year + 1, 1, 1) if mon == 12 else date_cls(year, mon + 1, 1)
    last = nxt - timedelta(days=1)
    return first.isoformat(), last.isoformat()


def working_days_in_range(start: date_cls, end: date_cls) -> int:
    """Count Mon-Fri days in [start, end] inclusive (R7). 0 if start > end."""
    if start > end:
        return 0
    total = 0
    cur = start
    while cur <= end:
        if cur.weekday() < 5:  # Mon=0 .. Fri=4
            total += 1
        cur += timedelta(days=1)
    return total


# --------------------------------------------------------------------------- #
# Serialization: Mongo document -> API shape
# --------------------------------------------------------------------------- #
def serialize_employee(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": to_ms(doc["created_at"]),
    }


def _serialize_history(history: list) -> list:
    """Convert stored history entries to the API shape. The from/to values of
    punch_in / punch_out are BSON dates in Mongo and epoch ms in the API."""
    out = []
    for entry in history or []:
        changes = {}
        for field, pair in (entry.get("changes") or {}).items():
            if field in ("punch_in", "punch_out"):
                changes[field] = {"from": to_ms(pair.get("from")), "to": to_ms(pair.get("to"))}
            else:
                changes[field] = {"from": pair.get("from"), "to": pair.get("to")}
        out.append(
            {
                "at": to_ms(entry["at"]),
                "by": entry["by"],
                "reason": entry["reason"],
                "changes": changes,
            }
        )
    return out


def serialize_attendance(doc: dict) -> dict:
    """Map an attendance document to the contract shape, defaulting the fields
    that legacy/seeded records may omit (``half_day`` -> false, missing
    ``late_minutes`` / ``overtime_minutes`` -> 0, missing ``history`` -> [])."""
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": to_ms(doc.get("punch_in")),
        "punch_out": to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": _serialize_history(doc.get("history", [])),
    }


# --------------------------------------------------------------------------- #
# Pydantic request models (validation -> 422 automatically)
# --------------------------------------------------------------------------- #
# Instants are validated as strict integers in epoch-millisecond range, which
# rejects strings, floats and seconds-looking / out-of-range numbers with 422.
# (Declared inline per field below via Field(strict=True, ge=MS_MIN, le=MS_MAX).)

DATE_RE = r"^\d{4}-\d{2}-\d{2}$"
HHMM_RE = r"^([01]\d|2[0-3]):[0-5]\d$"
EMP_CODE_RE = r"^EMP\d{4,6}$"
EMAIL_RE = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"


def _validate_calendar_date(value: str) -> str:
    """Ensure a YYYY-MM-DD string is a real calendar date."""
    try:
        date_cls.fromisoformat(value)
    except ValueError:
        raise ValueError("must be a valid YYYY-MM-DD date")
    return value


class EmployeeCreate(BaseModel):
    model_config = {"extra": "ignore"}

    emp_code: str = Field(pattern=EMP_CODE_RE)
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=EMAIL_RE)
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=HHMM_RE)
    shift_end: str = Field(default="18:30", pattern=HHMM_RE)
    joined_on: str = Field(pattern=DATE_RE)

    @field_validator("joined_on")
    @classmethod
    def _check_joined_on(cls, v: str) -> str:
        return _validate_calendar_date(v)

    @model_validator(mode="after")
    def _shift_differs(self) -> "EmployeeCreate":
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInRequest(BaseModel):
    model_config = {"extra": "ignore"}

    emp_code: str
    punched_at: Optional[int] = Field(default=None, strict=True, ge=MS_MIN, le=MS_MAX)
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutRequest(BaseModel):
    model_config = {"extra": "ignore"}

    emp_code: str
    punched_at: Optional[int] = Field(default=None, strict=True, ge=MS_MIN, le=MS_MAX)


class RegularizeRequest(BaseModel):
    # Derived fields can never be sent; unknown keys are ignored.
    model_config = {"extra": "ignore"}

    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[int] = Field(default=None, strict=True, ge=MS_MIN, le=MS_MAX)
    punch_out: Optional[int] = Field(default=None, strict=True, ge=MS_MIN, le=MS_MAX)
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# Shared query-parameter validation
# --------------------------------------------------------------------------- #
def validate_date_param(value: Optional[str], name: str) -> Optional[str]:
    if value is None:
        return None
    try:
        date_cls.fromisoformat(value)
    except ValueError:
        raise HTTPException(422, f"{name} must be a valid YYYY-MM-DD date")
    return value


# --------------------------------------------------------------------------- #
# Index creation at startup (idempotent)
# --------------------------------------------------------------------------- #
def ensure_indexes() -> None:
    # employees ---------------------------------------------------------------
    # Unique emp_code: business identity + concurrent-create guard (one 201).
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="uniq_emp_code")
    # department + emp_code: list filter by department sorted by emp_code.
    db.employees.create_index(
        [("department", ASCENDING), ("emp_code", ASCENDING)], name="dept_empcode"
    )
    # joined_on: headcount filter (R9) that drives the department summary.
    db.employees.create_index([("joined_on", ASCENDING)], name="joined_on")

    # attendance_logs ---------------------------------------------------------
    # Natural key: at most one record per (emp_code, date) and the punch-in race
    # guard. Also serves emp_code-scoped date-range queries (monthly, trend).
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uniq_emp_date"
    )
    # date desc, emp_code asc: the default attendance listing sort and every
    # month-wide analytics match (date range is the index prefix).
    db.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)], name="date_empcode"
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_indexes()
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan
)


# --------------------------------------------------------------------------- #
# System
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    """200 only if MongoDB answers a ping, otherwise 503."""
    try:
        db.command("ping")
    except Exception:
        raise HTTPException(503, "database unavailable")
    return {"status": "ok"}


# --------------------------------------------------------------------------- #
# Employees
# --------------------------------------------------------------------------- #
@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    doc = body.model_dump()
    doc["created_at"] = datetime.now(UTC).replace(microsecond=0)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        # Uniqueness is enforced by the index, so concurrent duplicate creates
        # yield exactly one 201 and the rest 409 (no check-then-insert race).
        raise HTTPException(409, "emp_code already exists")
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    query: dict[str, Any] = {}
    if department is not None:
        query["department"] = department  # exact, case-sensitive
    total = db.employees.count_documents(query)  # reflects the filter (R10)
    cursor = (
        db.employees.find(query)
        .sort([("emp_code", ASCENDING)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    items = [serialize_employee(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# Attendance: punch-in
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "unknown employee")

    ts = from_ms(body.punched_at) if body.punched_at is not None else datetime.now(UTC).replace(microsecond=0)
    att_date = attendance_date(ts, emp["shift_start"], emp["shift_end"])

    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, att_date, emp["shift_start"]),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        # The unique (emp_code, date) index makes the second concurrent punch-in
        # fail here instead of silently creating a duplicate -> exactly one 201.
        raise HTTPException(409, "already punched in for this date")
    return serialize_attendance(doc)


# --------------------------------------------------------------------------- #
# Attendance: punch-out
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-out")
def punch_out(body: PunchOutRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "unknown employee")

    ts = from_ms(body.punched_at) if body.punched_at is not None else datetime.now(UTC).replace(microsecond=0)

    # Most recent record whose punch_in is at or before punched_at. Works across
    # midnight: we order by punch_in, not by the calendar date string.
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$ne": None, "$lte": ts}},
        sort=[("punch_in", DESCENDING)],
    )
    if rec is None:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "record already punched out")

    pin: datetime = rec["punch_in"]
    if ts <= pin:
        raise HTTPException(422, "punch_out must be after punch_in")
    if (ts - pin) > timedelta(hours=24):
        raise HTTPException(422, "punch_out must be within 24 hours of punch_in")

    work_hours = compute_work_hours(pin, ts)
    overtime = compute_overtime_minutes(ts, rec["date"], emp["shift_start"], emp["shift_end"])
    half_day = compute_half_day(work_hours)

    # Conditional update on punch_out:None makes two simultaneous punch-outs
    # resolve to exactly one 200; the loser matches nothing -> 409.
    result = db.attendance_logs.update_one(
        {"_id": rec["_id"], "punch_out": None},
        {"$set": {"punch_out": ts, "work_hours": work_hours,
                  "overtime_minutes": overtime, "half_day": half_day}},
    )
    if result.matched_count == 0:
        raise HTTPException(409, "record already punched out")

    updated = db.attendance_logs.find_one({"_id": rec["_id"]})
    return serialize_attendance(updated)


# --------------------------------------------------------------------------- #
# Attendance: list
# --------------------------------------------------------------------------- #
def build_attendance_query(
    emp_code: Optional[str],
    date_from: Optional[str],
    date_to: Optional[str],
    status: Optional[str],
) -> dict:
    """Shared by the list endpoint and /admin/explain so both build the query
    the same way."""
    query: dict[str, Any] = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    if date_from is not None or date_to is not None:
        rng: dict[str, Any] = {}
        if date_from is not None:
            rng["$gte"] = date_from
        if date_to is not None:
            rng["$lte"] = date_to
        query["date"] = rng
    if status is not None:
        query["status"] = status
    return query


ATTENDANCE_SORT = [("date", DESCENDING), ("emp_code", ASCENDING)]


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = Query(None, pattern=DATE_RE),
    date_to: Optional[str] = Query(None, pattern=DATE_RE),
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    validate_date_param(date_from, "date_from")
    validate_date_param(date_to, "date_to")
    if date_from is not None and date_to is not None and date_from > date_to:
        raise HTTPException(422, "date_from must not be after date_to")

    query = build_attendance_query(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(query)
    # Sort/skip/limit in MongoDB (index-backed); never load the collection.
    cursor = (
        db.attendance_logs.find(query)
        .sort(ATTENDANCE_SORT)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    items = [serialize_attendance(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# Attendance: regularize (manual correction with audit trail)
# --------------------------------------------------------------------------- #
@app.patch("/attendance/{emp_code}/{date}")
def regularize(
    body: RegularizeRequest,
    emp_code: str = Path(...),
    date: str = Path(..., pattern=DATE_RE),
):
    validate_date_param(date, "date")
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "unknown employee")
    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if rec is None:
        raise HTTPException(404, "no record for that date")

    sent = body.model_fields_set
    new_status = body.status if "status" in sent else rec["status"]

    # --- Resolve the final punch times under the business constraints -------- #
    if new_status in ("ABSENT", "LEAVE"):
        # These statuses clear both punch times; sending either is invalid.
        if "punch_in" in sent or "punch_out" in sent:
            raise HTTPException(422, "ABSENT/LEAVE cannot carry punch times")
        new_pin: Optional[datetime] = None
        new_pout: Optional[datetime] = None
    else:  # a presence status
        new_pin = from_ms(body.punch_in) if "punch_in" in sent else rec.get("punch_in")
        new_pout = from_ms(body.punch_out) if "punch_out" in sent else rec.get("punch_out")
        if new_pin is None:
            raise HTTPException(422, "a presence status requires a punch_in")
        # punch_in must stay on the record's attendance date (R1).
        if attendance_date(new_pin, emp["shift_start"], emp["shift_end"]) != date:
            raise HTTPException(422, "punch_in must stay on the record's attendance date")
        if new_pout is not None:
            if new_pout <= new_pin:
                raise HTTPException(422, "punch_out must be after punch_in")
            if (new_pout - new_pin) > timedelta(hours=24):
                raise HTTPException(422, "punch_out must be within 24 hours of punch_in")

    # --- Recompute derived fields from the final times (R2-R5) --------------- #
    if new_status in ("ABSENT", "LEAVE"):
        late = 0
        work_hours = None
        overtime = 0
        half_day = False
    else:
        late = compute_late_minutes(new_pin, date, emp["shift_start"])
        if new_pout is not None:
            work_hours = compute_work_hours(new_pin, new_pout)
            overtime = compute_overtime_minutes(new_pout, date, emp["shift_start"], emp["shift_end"])
            half_day = compute_half_day(work_hours)
        else:
            work_hours = None
            overtime = 0
            half_day = False

    final = {
        "status": new_status,
        "punch_in": new_pin,
        "punch_out": new_pout,
        "work_hours": work_hours,
        "late_minutes": late,
        "overtime_minutes": overtime,
        "half_day": half_day,
    }
    old = {
        "status": rec["status"],
        "punch_in": rec.get("punch_in"),
        "punch_out": rec.get("punch_out"),
        "work_hours": rec.get("work_hours"),
        "late_minutes": rec.get("late_minutes", 0),
        "overtime_minutes": rec.get("overtime_minutes", 0),
        "half_day": rec.get("half_day", False),
    }

    # --- Build the changes object for fields that actually changed ----------- #
    changes: dict[str, Any] = {}
    for fld in ("status", "punch_in", "punch_out", "work_hours",
                "late_minutes", "overtime_minutes", "half_day"):
        if old[fld] != final[fld]:
            if fld in ("punch_in", "punch_out"):
                changes[fld] = {"from": to_ms(old[fld]), "to": to_ms(final[fld])}
            else:
                changes[fld] = {"from": old[fld], "to": final[fld]}
    if not changes:
        raise HTTPException(422, "request changes nothing")

    entry = {
        "at": datetime.now(UTC).replace(microsecond=0),
        "by": body.regularized_by,
        "reason": body.reason,
        # Stored history keeps punch_in/out from/to as BSON dates.
        "changes": {
            fld: ({"from": old[fld], "to": final[fld]} if fld in ("punch_in", "punch_out")
                  else pair)
            for fld, pair in changes.items()
        },
    }

    # Optimistic concurrency: the update only applies if history has not grown
    # since we read it, so concurrent edits cannot silently drop each other's
    # entries -> the loser gets 409.
    result = db.attendance_logs.update_one(
        {"_id": rec["_id"], "history": {"$size": len(rec.get("history", []))}},
        {"$set": final, "$push": {"history": entry}},
    )
    if result.matched_count == 0:
        raise HTTPException(409, "record was modified concurrently")

    updated = db.attendance_logs.find_one({"_id": rec["_id"]})
    return serialize_attendance(updated)


# --------------------------------------------------------------------------- #
# Aggregation-pipeline builders (shared by analytics endpoints and /admin/explain)
# --------------------------------------------------------------------------- #
# Reusable expression fragments ------------------------------------------------
def _date_obj(field: str = "$date"):
    """Parse a YYYY-MM-DD string into a UTC date for $dayOfWeek / $densify."""
    return {"$dateFromString": {"dateString": field, "format": "%Y-%m-%d", "timezone": "UTC"}}


def _is_weekday(date_expr):
    # $dayOfWeek: 1=Sun .. 7=Sat; Mon..Fri is 2..6.
    dow = {"$dayOfWeek": {"date": date_expr, "timezone": "UTC"}}
    return {"$and": [{"$gte": [dow, 2]}, {"$lte": [dow, 6]}]}


def _presence_weight(date_expr, status_field="$status", half_day_field="$half_day"):
    """1 for a weekday presence record, 0.5 if it is a half day, else 0 (R5-R7).

    ``status_field`` / ``half_day_field`` are parameterised so the same logic
    works on top-level fields (monthly, trend) and on ``$logs.*`` after the
    ``$unwind`` in the department summary.
    """
    is_presence = {"$in": [status_field, list(PRESENCE_STATUSES)]}
    return {
        "$cond": [
            {"$and": [is_presence, _is_weekday(date_expr)]},
            {"$cond": [{"$ifNull": [half_day_field, False]}, 0.5, 1]},
            0,
        ]
    }


def employee_monthly_pipeline(emp_code: str, month: str) -> list:
    first, last = month_bounds(month)
    date_expr = _date_obj("$date")
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first, "$lte": last}}},
        {
            "$group": {
                "_id": None,
                # present_days: weekday presence only (R7); half day = 0.5.
                "present_days": {"$sum": _presence_weight(date_expr)},
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "late_count": {
                    "$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}
                },
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
            }
        },
    ]


def department_summary_pipeline(month: str, department: Optional[str]) -> list:
    first, last = month_bounds(month)

    emp_match: dict[str, Any] = {"joined_on": {"$lte": last}}  # headcount eligibility (R9)
    if department is not None:
        emp_match["department"] = department

    return [
        {"$match": emp_match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"ec": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$ec"]},
                                    {"$gte": ["$date", first]},
                                    {"$lte": ["$date", last]},
                                ]
                            }
                        }
                    }
                ],
                "as": "logs",
            }
        },
        # Keep employees with zero logs so headcount counts them (R9).
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {
            "$group": {
                "_id": "$department",
                "emp_codes": {"$addToSet": "$emp_code"},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {"$ifNull": ["$logs", False]},
                            _presence_weight(
                                _date_obj("$logs.date"),
                                status_field="$logs.status",
                                half_day_field="$logs.half_day",
                            ),
                            0,
                        ]
                    }
                },
                # avg over records that are present-status AND have work_hours.
                "work_hours_vals": {
                    "$push": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", list(PRESENCE_STATUSES)]},
                                    {"$ne": [{"$ifNull": ["$logs.work_hours", None]}, None]},
                                ]
                            },
                            "$logs.work_hours",
                            None,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]}
                },
                "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
                "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},
                "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}},
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": {"$size": "$emp_codes"},
                "present_days": 1,
                # mean of the non-null work_hours values ($avg ignores nulls).
                "avg_work_hours": {
                    "$avg": {
                        "$filter": {
                            "input": "$work_hours_vals",
                            "cond": {"$ne": ["$$this", None]},
                        }
                    }
                },
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },
        {"$match": {"headcount": {"$gt": 0}}},
        {"$sort": {"department": 1}},
    ]


def late_leaderboard_pipeline(month: str, emp_codes: Optional[list[str]]) -> list:
    first, last = month_bounds(month)
    match: dict[str, Any] = {"date": {"$gte": first, "$lte": last}, "late_minutes": {"$gt": 0}}
    if emp_codes is not None:
        match["emp_code"] = {"$in": emp_codes}
    return [
        {"$match": match},
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        # Standard competition ranking (1,2,2,4) over total_late_minutes.
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {"rank": {"$rank": {}}},
            }
        },
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
    ]


def department_trend_pipeline(
    emp_codes: list[str], joined_on_list: list[str], first: str, last: str
) -> list:
    first_d = date_cls.fromisoformat(first)
    last_d = date_cls.fromisoformat(last)
    lower = datetime(first_d.year, first_d.month, first_d.day, tzinfo=UTC)
    upper = datetime(last_d.year, last_d.month, last_d.day, tzinfo=UTC) + timedelta(days=1)

    # half-up rounding to 4 dp for non-negative values, done in-pipeline so the
    # rounded attendance_rate is what the moving average is computed from.
    def round4(expr):
        return {"$divide": [{"$floor": {"$add": [{"$multiply": [expr, 10000]}, 0.5]}}, 10000]}

    return [
        {"$match": {"emp_code": {"$in": emp_codes}, "date": {"$gte": first, "$lte": last}}},
        {
            "$group": {
                "_id": "$date",
                "present_count": {"$sum": _presence_weight(_date_obj("$date"))},
                "late_count": {
                    "$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}
                },
            }
        },
        {"$set": {"d": _date_obj("$_id")}},
        # Gap-fill: exactly one row per calendar day in [first, last].
        {
            "$densify": {
                "field": "d",
                "range": {"step": 1, "unit": "day", "bounds": [lower, upper]},
            }
        },
        {
            "$set": {
                "date": {"$dateToString": {"date": "$d", "format": "%Y-%m-%d", "timezone": "UTC"}},
                "present_count": {"$ifNull": ["$present_count", 0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
            }
        },
        {
            "$set": {
                "is_working_day": _is_weekday("$d"),
                # headcount: dept employees who had joined on or before this day.
                "headcount": {
                    "$size": {
                        "$filter": {
                            "input": joined_on_list,
                            "cond": {"$lte": ["$$this", "$date"]},
                        }
                    }
                },
            }
        },
        {
            "$set": {
                "attendance_rate": {
                    "$cond": [
                        {"$and": [{"$eq": ["$is_working_day", True]}, {"$gt": ["$headcount", 0]}]},
                        round4({"$divide": ["$present_count", "$headcount"]}),
                        None,
                    ]
                }
            }
        },
        {"$sort": {"d": 1}},
        {
            "$setWindowFields": {
                "sortBy": {"d": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, 0]},
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": {
                    "$cond": [
                        {"$eq": ["$moving_avg_7d", None]},
                        None,
                        round4("$moving_avg_7d"),
                    ]
                },
            }
        },
        {"$sort": {"date": 1}},
    ]


# --------------------------------------------------------------------------- #
# Analytics: employee monthly
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str = Path(...),
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "unknown employee")

    first, last = month_bounds(month)
    # working_days (R7): Mon-Fri from max(joined_on, month start) to month end.
    start = max(date_cls.fromisoformat(first), date_cls.fromisoformat(emp["joined_on"]))
    working_days = working_days_in_range(start, date_cls.fromisoformat(last))

    agg = list(db.attendance_logs.aggregate(employee_monthly_pipeline(emp_code, month)))
    row = agg[0] if agg else {}
    present_days = row.get("present_days", 0)
    attendance_pct = (
        round_half_up(present_days / working_days * 100, 2) if working_days > 0 else None
    )
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": row.get("leave_days", 0),
        "late_count": row.get("late_count", 0),
        "total_late_minutes": row.get("total_late_minutes", 0),
        "total_overtime_minutes": row.get("total_overtime_minutes", 0),
        "attendance_pct": attendance_pct,
    }


# --------------------------------------------------------------------------- #
# Analytics: department summary
# --------------------------------------------------------------------------- #
@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    rows = list(db.employees.aggregate(department_summary_pipeline(month, department)))
    items = []
    for r in rows:
        items.append(
            {
                "department": r["department"],
                "headcount": r["headcount"],
                "present_days": r.get("present_days", 0),
                "avg_work_hours": round_half_up(r.get("avg_work_hours"), 2),
                "late_count": r.get("late_count", 0),
                "total_late_minutes": r.get("total_late_minutes", 0),
                "leave_count": r.get("leave_count", 0),
                "on_duty_count": r.get("on_duty_count", 0),
            }
        )
    return {"month": month, "items": items}


# --------------------------------------------------------------------------- #
# Analytics: late leaderboard
# --------------------------------------------------------------------------- #
@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    emp_codes = None
    emp_by_code: dict[str, dict] = {}
    if department is not None:
        emps = list(db.employees.find({"department": department}))
        emp_codes = [e["emp_code"] for e in emps]
        emp_by_code = {e["emp_code"]: e for e in emps}
        if not emp_codes:
            return {"month": month, "items": []}

    ranked = list(db.attendance_logs.aggregate(late_leaderboard_pipeline(month, emp_codes)))

    # Resolve employee name/department; logs for unknown employees are ignored.
    needed = {r["_id"] for r in ranked}
    missing = needed - set(emp_by_code)
    if missing:
        for e in db.employees.find({"emp_code": {"$in": list(missing)}}):
            emp_by_code[e["emp_code"]] = e

    items = []
    for r in ranked:
        emp = emp_by_code.get(r["_id"])
        if emp is None:  # log references a non-existent employee -> ignore
            continue
        if r["rank"] > limit:  # limit applies AFTER ranking
            continue
        items.append(
            {
                "rank": r["rank"],
                "emp_code": r["_id"],
                "name": emp["name"],
                "department": emp["department"],
                "total_late_minutes": r["total_late_minutes"],
                "late_count": r["late_count"],
            }
        )
    return {"month": month, "items": items}


# --------------------------------------------------------------------------- #
# Analytics: department trend
# --------------------------------------------------------------------------- #
def _load_department(department: str) -> list[dict]:
    emps = list(db.employees.find({"department": department}))
    if not emps:
        raise HTTPException(404, "unknown department")
    return emps


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str = Path(...),
    date_from: str = Query(..., alias="from", pattern=DATE_RE),
    date_to: str = Query(..., alias="to", pattern=DATE_RE),
):
    validate_date_param(date_from, "from")
    validate_date_param(date_to, "to")
    d_from = date_cls.fromisoformat(date_from)
    d_to = date_cls.fromisoformat(date_to)
    if d_to < d_from:
        raise HTTPException(422, "to must not be before from")
    if (d_to - d_from).days + 1 > 92:
        raise HTTPException(422, "range exceeds 92 days")

    emps = _load_department(department)
    emp_codes = [e["emp_code"] for e in emps]
    joined_on_list = [e["joined_on"] for e in emps]

    items = list(
        db.attendance_logs.aggregate(
            department_trend_pipeline(emp_codes, joined_on_list, date_from, date_to)
        )
    )
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Admin: explain
# --------------------------------------------------------------------------- #
def _explain_aggregate(collection: str, pipeline: list) -> dict:
    return db.command(
        "explain",
        {"aggregate": collection, "pipeline": pipeline, "cursor": {}},
        verbosity="executionStats",
    )


def _explain_find(collection: str, filt: dict, sort: list, skip: int, limit: int) -> dict:
    return db.command(
        "explain",
        {"find": collection, "filter": filt, "sort": dict(sort), "skip": skip, "limit": limit},
        verbosity="executionStats",
    )


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal[
        "attendance_list", "employee_monthly", "department_summary",
        "late_leaderboard", "department_trend",
    ] = Path(...),
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = Query(None, pattern=DATE_RE),
    date_to: Optional[str] = Query(None, pattern=DATE_RE),
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    date_from_trend: Optional[str] = Query(None, alias="from", pattern=DATE_RE),
    date_to_trend: Optional[str] = Query(None, alias="to", pattern=DATE_RE),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """Run MongoDB's explain (executionStats) for an endpoint's main query/
    pipeline, built exactly as that endpoint builds it."""
    if endpoint == "attendance_list":
        query = build_attendance_query(emp_code, date_from, date_to, status)
        explain = _explain_find(
            "attendance_logs", query, ATTENDANCE_SORT,
            (page - 1) * page_size, page_size,
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}

    if endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, "emp_code and month are required")
        explain = _explain_aggregate("attendance_logs", employee_monthly_pipeline(emp_code, month))
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}

    if endpoint == "department_summary":
        if not month:
            raise HTTPException(422, "month is required")
        explain = _explain_aggregate("employees", department_summary_pipeline(month, department))
        return {"endpoint": endpoint, "collection": "employees", "explain": explain}

    if endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, "month is required")
        emp_codes = None
        if department is not None:
            emp_codes = [e["emp_code"] for e in db.employees.find({"department": department})]
        explain = _explain_aggregate("attendance_logs", late_leaderboard_pipeline(month, emp_codes))
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}

    # department_trend
    if not department or not date_from_trend or not date_to_trend:
        raise HTTPException(422, "department, from and to are required")
    emps = list(db.employees.find({"department": department}))
    if not emps:
        raise HTTPException(404, "unknown department")
    emp_codes = [e["emp_code"] for e in emps]
    joined_on_list = [e["joined_on"] for e in emps]
    explain = _explain_aggregate(
        "attendance_logs",
        department_trend_pipeline(emp_codes, joined_on_list, date_from_trend, date_to_trend),
    )
    return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}
