
import calendar
import json
import logging
import os
import re
import threading
from contextlib import asynccontextmanager
from datetime import date as Date
from datetime import datetime, time, timedelta, timezone
from decimal import ROUND_HALF_EVEN, ROUND_HALF_UP, Decimal
from typing import Annotated, Any, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv() 

log = logging.getLogger("attendance")

# Configuration & indexes

client = MongoClient(
    os.getenv("MONGO_URI") or "mongodb://localhost:27017",
    tz_aware=True,
    serverSelectionTimeoutMS=5000,
)
db = client[os.getenv("MONGO_DB") or "attendance_db"]

EMP_DEPT_INDEX = "emp_dept_code_joined"
ATT_DATE_INDEX = "att_date_emp"

# (collection, keys, options)
INDEXES = [
    ("employees", [("emp_code", 1)], {"name": "emp_code_uq", "unique": True}),
    ("employees", [("department", 1), ("emp_code", 1), ("joined_on", 1)], {"name": EMP_DEPT_INDEX}),
    ("attendance_logs", [("emp_code", 1), ("date", 1)], {"name": "att_emp_date_uq", "unique": True}),
    ("attendance_logs", [("date", -1), ("emp_code", 1)], {"name": ATT_DATE_INDEX}),
    ("attendance_logs", [("status", 1), ("date", -1), ("emp_code", 1)], {"name": "att_status_date_emp"}),
    ("attendance_logs", [("emp_code", 1), ("punch_in", -1)], {"name": "att_emp_punchin"}),
]

_indexes_ready = False
_index_lock = threading.Lock()


def ensure_indexes() -> None:
    """Create all indexes (idempotent). A failure on one index is logged, not fatal."""
    global _indexes_ready
    if _indexes_ready:
        return
    with _index_lock:
        if _indexes_ready:
            return
        ok = True
        for coll, keys, opts in INDEXES:
            try:
                db[coll].create_index(keys, **opts)
            except PyMongoError as exc:
                ok = False
                log.error("could not create index %s on %s: %s", opts["name"], coll, exc)
        _indexes_ready = ok


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        ensure_indexes()
    except Exception as exc:  
        log.error("startup index creation failed: %s", exc)
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


@app.exception_handler(PyMongoError)
def _mongo_error(_, exc: PyMongoError):
    log.error("mongo error: %s", exc)
    return JSONResponse(status_code=503, content={"detail": "database error"})



# 2. Pure helpers

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

PRESENCE = ("PRESENT", "WFH", "ON_DUTY")
GRACE_SECONDS = 10 * 60
MIN_OVERTIME_MINUTES = 30
HALF_DAY_BELOW_HOURS = Decimal("4.50")
MAX_SHIFT_SPAN = timedelta(hours=24)


def now_utc() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def dt_from_ms(ms: int) -> datetime:
    """Epoch milliseconds -> aware UTC datetime, truncated to whole seconds (R1)."""
    return datetime.fromtimestamp(ms // 1000, tz=UTC)


def as_utc(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is not None and dt.tzinfo is None:  # defensive: seeded/naive values are UTC
        return dt.replace(tzinfo=UTC)
    return dt


def ms_from_dt(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    return (as_utc(dt) - EPOCH) // timedelta(milliseconds=1)


def round_half_up(value: Any, places: int) -> float:
    """Round half-up (not banker's, not binary-float rounding)."""
    if isinstance(value, Decimal):
        d = value
    elif isinstance(value, float):
        # repr() gives the shortest decimal that round-trips; strip float noise first
        d = Decimal(repr(value)).quantize(Decimal("1e-12"), rounding=ROUND_HALF_EVEN)
    else:
        d = Decimal(value)
    return float(d.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


def hm_to_time(hm: str) -> time:
    h, m = hm.split(":")
    return time(int(h), int(m))


def is_overnight(shift_start: str, shift_end: str) -> bool:
    return hm_to_time(shift_end) <= hm_to_time(shift_start)


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> Date:
    """R1: IST calendar date of the punch-in; an overnight shift's early-morning punch-in
    belongs to the previous day's shift."""
    local = as_utc(punch_in).astimezone(IST)
    d = local.date()
    if is_overnight(shift_start, shift_end) and local.time() < hm_to_time(shift_end):
        d -= timedelta(days=1)
    return d


def shift_start_dt(att_date: Date, shift_start: str) -> datetime:
    return datetime.combine(att_date, hm_to_time(shift_start), tzinfo=IST)


def shift_end_dt(att_date: Date, shift_start: str, shift_end: str) -> datetime:
    end_date = att_date + timedelta(days=1) if is_overnight(shift_start, shift_end) else att_date
    return datetime.combine(end_date, hm_to_time(shift_end), tzinfo=IST)


def _whole_seconds(delta: timedelta) -> int:
    return delta.days * 86400 + delta.seconds


def compute_late_minutes(punch_in: datetime, att_date: Date, shift_start: str) -> int:
    """R2: late only if strictly more than 10:00 after shift start; then floor minutes since start."""
    secs = _whole_seconds(as_utc(punch_in).replace(microsecond=0) - shift_start_dt(att_date, shift_start))
    return secs // 60 if secs > GRACE_SECONDS else 0


def compute_overtime(punch_out: datetime, att_date: Date, shift_start: str, shift_end: str) -> int:
    """R3: floor minutes from shift end to punch-out, counted only when >= 30."""
    secs = _whole_seconds(as_utc(punch_out).replace(microsecond=0) - shift_end_dt(att_date, shift_start, shift_end))
    minutes = secs // 60
    return minutes if minutes >= MIN_OVERTIME_MINUTES else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: seconds / 3600, 2 decimals half-up."""
    secs = _whole_seconds(as_utc(punch_out).replace(microsecond=0) - as_utc(punch_in).replace(microsecond=0))
    return round_half_up(Decimal(secs) / Decimal(3600), 2)


def derive_fields(status: str, punch_in: Optional[datetime], punch_out: Optional[datetime],
                  att_date: Date, shift_start: str, shift_end: str) -> dict:
    """All stored derived values for a record (see DATA_MODEL.md, section 3)."""
    out = {"late_minutes": 0, "work_hours": None, "overtime_minutes": 0, "half_day": False}
    if status not in PRESENCE or punch_in is None:
        return out
    out["late_minutes"] = compute_late_minutes(punch_in, att_date, shift_start)
    if punch_out is not None:
        wh = compute_work_hours(punch_in, punch_out)
        out["work_hours"] = wh
        out["overtime_minutes"] = compute_overtime(punch_out, att_date, shift_start, shift_end)
        out["half_day"] = Decimal(repr(wh)) < HALF_DAY_BELOW_HOURS  
    return out


DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
MONTH_PATTERN = r"^[0-9]{4}-(0[1-9]|1[0-2])$"


def parse_date(value: str, name: str = "date") -> Date:
    if not DATE_RE.fullmatch(value or ""):
        raise HTTPException(422, f"{name} must be YYYY-MM-DD")
    try:
        return Date.fromisoformat(value)
    except ValueError:
        raise HTTPException(422, f"{name} is not a valid calendar date")


def parse_month(value: str) -> tuple[Date, Date]:
    """'YYYY-MM' -> (first day, last day)."""
    try:
        y, m = int(value[:4]), int(value[5:7])
        return Date(y, m, 1), Date(y, m, calendar.monthrange(y, m)[1])
    except (ValueError, IndexError):
        raise HTTPException(422, "month must be YYYY-MM")


def working_days(first: Date, last: Date, joined_on: str) -> int:
    """R7: Mon-Fri between max(first, joined_on) and last, inclusive."""
    start = max(first, Date.fromisoformat(joined_on))
    n, d = 0, start
    while d <= last:
        if d.weekday() < 5:
            n += 1
        d += timedelta(days=1)
    return n


# 3. Request models & shared types

EpochMs = Annotated[int, Field(strict=True, ge=100_000_000_000, le=4_102_444_800_000)]
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]
HHMM = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"
MonthQ = Annotated[str, Query(pattern=MONTH_PATTERN)]
PageQ = Annotated[int, Query(ge=1, le=10**9)]
PageSizeQ = Annotated[int, Query(ge=1, le=100)]
LimitQ = Annotated[int, Query(ge=1, le=50)]


class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP[0-9]{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=HHMM)
    shift_end: str = Field("18:30", pattern=HHMM)
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def _joined_on_is_date(cls, v: str) -> str:
        if not DATE_RE.fullmatch(v):
            raise ValueError("joined_on must be YYYY-MM-DD")
        Date.fromisoformat(v)  
        return v

    @model_validator(mode="after")
    def _shift_times_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str = Field(min_length=1)
    punched_at: Optional[EpochMs] = None
    status: PresenceStatus = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str = Field(min_length=1)
    punched_at: Optional[EpochMs] = None


class RegularizeIn(BaseModel):
    status: Optional[Status] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


# 4. Serializers

def employee_to_api(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": ms_from_dt(doc.get("created_at")),
    }


def _history_to_api(entry: dict) -> dict:
    changes = {}
    for key, ch in (entry.get("changes") or {}).items():
        if key in ("punch_in", "punch_out"): 
            ch = {"from": ms_from_dt(ch.get("from")), "to": ms_from_dt(ch.get("to"))}
        changes[key] = ch
    return {"at": ms_from_dt(entry.get("at")), "by": entry.get("by"),
            "reason": entry.get("reason"), "changes": changes}


def record_to_api(doc: dict) -> dict:
    """Never exposes _id; applies the defaults for legacy documents (missing fields)."""
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": ms_from_dt(doc.get("punch_in")),
        "punch_out": ms_from_dt(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day", False)),
        "history": [_history_to_api(h) for h in (doc.get("history") or [])],
    }


def get_employee_or_404(emp_code: str) -> dict:
    emp = db.employees.find_one({"emp_code": emp_code}, {"_id": 0})
    if emp is None:
        raise HTTPException(404, "employee not found")
    return emp



# 5. Employees & attendance endpoints

@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        ensure_indexes() 
    except PyMongoError:
        raise HTTPException(503, "MongoDB unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    now = now_utc()
    doc = body.model_dump()
    doc["created_at"] = now
    try:
        db.employees.insert_one(doc) 
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_to_api(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: PageQ = 1, page_size: PageSizeQ = 20):
    q = {} if department is None else {"department": department}
    total = db.employees.count_documents(q)
    cursor = db.employees.find(q, {"_id": 0}).sort("emp_code", 1).skip((page - 1) * page_size).limit(page_size)
    return {"items": [employee_to_api(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = get_employee_or_404(body.emp_code)
    ts = dt_from_ms(body.punched_at) if body.punched_at is not None else now_utc()
    att_date = attendance_date(ts, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": att_date.isoformat(),
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        **derive_fields(body.status, ts, None, att_date, emp["shift_start"], emp["shift_end"]),
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc) 
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return record_to_api(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = get_employee_or_404(body.emp_code)
    ts = dt_from_ms(body.punched_at) if body.punched_at is not None else now_utc()
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}}, sort=[("punch_in", -1)]
    )
    if rec is None:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    p_in = as_utc(rec["punch_in"])
    if ts <= p_in or ts - p_in > MAX_SHIFT_SPAN:
        raise HTTPException(422, "punched_at must be after punch_in and within 24 hours of it")
    d = derive_fields(rec["status"], p_in, ts, Date.fromisoformat(rec["date"]),
                      emp["shift_start"], emp["shift_end"])
    updated = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None}, 
        {"$set": {"punch_out": ts, "work_hours": d["work_hours"],
                  "overtime_minutes": d["overtime_minutes"], "half_day": d["half_day"]}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "already punched out")
    return record_to_api(updated)


def build_attendance_query(emp_code, date_from, date_to, status, page, page_size) -> dict:
    """Single source of truth for GET /attendance and its explain."""
    f_from = parse_date(date_from, "date_from").isoformat() if date_from is not None else None
    f_to = parse_date(date_to, "date_to").isoformat() if date_to is not None else None
    if f_from and f_to and f_from > f_to:
        raise HTTPException(422, "date_from must not be after date_to")
    q: dict = {}
    if emp_code is not None:
        q["emp_code"] = emp_code
    if f_from or f_to:
        q["date"] = {}
        if f_from:
            q["date"]["$gte"] = f_from
        if f_to:
            q["date"]["$lte"] = f_to
    if status is not None:
        q["status"] = status
    return {"filter": q, "sort": [("date", -1), ("emp_code", 1)],
            "skip": (page - 1) * page_size, "limit": page_size}


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    page: PageQ = 1,
    page_size: PageSizeQ = 20,
):
    spec = build_attendance_query(emp_code, date_from, date_to, status, page, page_size)
    coll = db.attendance_logs
    total = coll.count_documents(spec["filter"]) if spec["filter"] else coll.estimated_document_count()
    cursor = (coll.find(spec["filter"], {"_id": 0}).sort(spec["sort"])
              .skip(spec["skip"]).limit(spec["limit"]))
    return {"items": [record_to_api(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeIn):
    att_date = parse_date(date, "date")
    emp = get_employee_or_404(emp_code)
    coll = db.attendance_logs
    for _ in range(5): 
        rec = coll.find_one({"emp_code": emp_code, "date": att_date.isoformat()})
        if rec is None:
            raise HTTPException(404, "no attendance record for that date")
        set_fields, entry = _plan_regularization(rec, emp, att_date, body)
        history = rec.get("history")
        update: dict = {"$set": set_fields}
        if isinstance(history, list):
            update["$push"] = {"history": entry}
        else:  # legacy: field missing (or null)
            set_fields["history"] = [entry]
       
        pinned = {
            "_id": rec["_id"],
            "status": rec["status"],
            "punch_in": rec.get("punch_in"),
            "punch_out": rec.get("punch_out"),
            "$expr": {"$eq": [{"$size": {"$ifNull": ["$history", []]}},
                              len(history) if isinstance(history, list) else 0]},
        }
        updated = coll.find_one_and_update(pinned, update, return_document=ReturnDocument.AFTER)
        if updated is not None:
            return record_to_api(updated)
    raise HTTPException(409, "record was modified concurrently; retry")


def _plan_regularization(rec: dict, emp: dict, att_date: Date, body: RegularizeIn) -> tuple[dict, dict]:
    old_status = rec["status"]
    old_in, old_out = as_utc(rec.get("punch_in")), as_utc(rec.get("punch_out"))
    new_status = body.status or old_status

    if new_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, f"punch times cannot be supplied with status {new_status}")
        new_in = new_out = None
    else:
        new_in = dt_from_ms(body.punch_in) if body.punch_in is not None else old_in
        new_out = dt_from_ms(body.punch_out) if body.punch_out is not None else old_out
        if new_in is None:
            raise HTTPException(422, f"status {new_status} requires a punch_in")
        if attendance_date(new_in, emp["shift_start"], emp["shift_end"]) != att_date:
            raise HTTPException(422, "punch_in must stay on the record's attendance date")
        if new_out is not None and not (new_in < new_out <= new_in + MAX_SHIFT_SPAN):
            raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")

    if (new_status, new_in, new_out) == (old_status, old_in, old_out):
        raise HTTPException(422, "request changes nothing")

    derived = derive_fields(new_status, new_in, new_out, att_date, emp["shift_start"], emp["shift_end"])
    new_vals = {"status": new_status, "punch_in": new_in, "punch_out": new_out, **derived}
    old_vals = {
        "status": old_status, "punch_in": old_in, "punch_out": old_out,
        "work_hours": rec.get("work_hours"),
        "late_minutes": rec.get("late_minutes") or 0,
        "overtime_minutes": rec.get("overtime_minutes") or 0,
        "half_day": bool(rec.get("half_day", False)),
    }
    changes = {k: {"from": old_vals[k], "to": new_vals[k]} for k in new_vals if old_vals[k] != new_vals[k]}
    entry = {"at": now_utc(), "by": body.regularized_by, "reason": body.reason, "changes": changes}
    return dict(new_vals), entry



# 6. Aggregation pipelines & analytics endpoints

_IS_PRESENT = {"$in": ["$status", list(PRESENCE)]}
_IS_WEEKDAY = {"$lte": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}}}, 5]}
_HALF_DAY = {"$ifNull": ["$half_day", False]}
_PRESENT_DAYS = {"$cond": [{"$and": [_IS_PRESENT, _IS_WEEKDAY]}, {"$cond": [_HALF_DAY, 0.5, 1]}, 0]}
_PRESENT_ANY_DAY = {"$cond": [_IS_PRESENT, {"$cond": [_HALF_DAY, 0.5, 1]}, 0]}
_LATE_MIN = {"$ifNull": ["$late_minutes", 0]}
_IS_LATE = {"$cond": [{"$gt": [_LATE_MIN, 0]}, 1, 0]}


def _count_if(cond: dict) -> dict:
    return {"$sum": {"$cond": [cond, 1, 0]}}


def pipeline_employee_monthly(emp_code: str, first: Date, last: Date) -> dict:
    return {
        "collection": "attendance_logs",
        "hint": None, 
        "pipeline": [
            {"$match": {"emp_code": emp_code, "date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
            {"$group": {
                "_id": None,
                "present_days": {"$sum": _PRESENT_DAYS},
                "leave_days": _count_if({"$eq": ["$status", "LEAVE"]}),
                "late_count": {"$sum": _IS_LATE},
                "total_late_minutes": {"$sum": _LATE_MIN},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
            }},
        ],
    }


def pipeline_department_summary(first: Date, last: Date, department: Optional[str]) -> dict:
    """Starts from employees (so people with zero logs are counted), joins each employee's logs
    for the month, then folds everything per department."""
    match: dict = {"joined_on": {"$lte": last.isoformat()}}  # R9
    if department is not None:
        match["department"] = department
    has_wh = {"$and": [_IS_PRESENT, {"$isNumber": "$work_hours"}]}
    return {
        "collection": "employees",
        "hint": EMP_DEPT_INDEX,
        "pipeline": [
            {"$match": match},
            {"$lookup": {
                "from": "attendance_logs",
                "let": {"c": "$emp_code"},
                "pipeline": [
                    {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()},
                                "$expr": {"$eq": ["$emp_code", "$$c"]}}},
                    {"$group": {
                        "_id": None,
                        "present_days": {"$sum": _PRESENT_DAYS},
                        
                        "wh_hundredths": {"$sum": {"$cond": [has_wh, {"$round": [{"$multiply": ["$work_hours", 100]}, 0]}, 0]}},
                        "wh_count": _count_if(has_wh),
                        "late_count": {"$sum": _IS_LATE},
                        "total_late_minutes": {"$sum": _LATE_MIN},
                        "leave_count": _count_if({"$eq": ["$status", "LEAVE"]}),
                        "on_duty_count": _count_if({"$eq": ["$status", "ON_DUTY"]}),
                    }},
                ],
                "as": "s",
            }},
            {"$set": {"s": {"$ifNull": [{"$first": "$s"}, {}]}}},  
            {"$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "present_days": {"$sum": "$s.present_days"},
                "wh_hundredths": {"$sum": "$s.wh_hundredths"},
                "wh_count": {"$sum": "$s.wh_count"},
                "late_count": {"$sum": "$s.late_count"},
                "total_late_minutes": {"$sum": "$s.total_late_minutes"},
                "leave_count": {"$sum": "$s.leave_count"},
                "on_duty_count": {"$sum": "$s.on_duty_count"},
            }},
            {"$sort": {"_id": 1}},
        ],
    }


def pipeline_late_leaderboard(first: Date, last: Date, limit: int, department: Optional[str]) -> dict:
    pipeline: list = [
        {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"}, 
    ]
    if department is not None:
        pipeline.append({"$match": {"emp.department": department}})  
    pipeline += [
        
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}}, 
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$emp.name",
                      "department": "$emp.department", "total_late_minutes": 1, "late_count": 1}},
    ]
    return {"collection": "attendance_logs", "hint": ATT_DATE_INDEX, "pipeline": pipeline}


def pipeline_department_trend(department: str, d_from: Date, d_to: Date) -> dict:
    """One document per department -> one row per calendar day built with $range (gap filling happens
    in the database) -> a 7-row $setWindowFields moving average."""
    n_days = (d_to - d_from).days + 1
    start = datetime(d_from.year, d_from.month, d_from.day, tzinfo=UTC)
    day_row = {"$let": {
        "vars": {"dt": {"$add": [start, {"$multiply": ["$$i", 86_400_000]}]}},
        "in": {"$let": {
            "vars": {"day": {"$dateToString": {"format": "%Y-%m-%d", "date": "$$dt"}}},
            "in": {
                "date": "$$day",
                "dow": {"$isoDayOfWeek": "$$dt"},
                "headcount": {"$size": {"$filter": {"input": "$joins", "as": "j", "cond": {"$lte": ["$$j", "$$day"]}}}},
                "stat": {"$arrayElemAt": [{"$filter": {"input": "$stats", "as": "s", "cond": {"$eq": ["$$s._id", "$$day"]}}}, 0]},
            },
        }},
    }}
    return {
        "collection": "employees",
        "hint": EMP_DEPT_INDEX,
        "pipeline": [
            {"$match": {"department": department}},
            {"$group": {"_id": None, "codes": {"$push": "$emp_code"}, "joins": {"$push": "$joined_on"}}},
            {"$lookup": {  
                "from": "attendance_logs", "localField": "codes", "foreignField": "emp_code",
                "pipeline": [
                    {"$match": {"date": {"$gte": d_from.isoformat(), "$lte": d_to.isoformat()}}},
                    {"$group": {"_id": "$date", "present": {"$sum": _PRESENT_ANY_DAY}, "late": {"$sum": _IS_LATE}}},
                ],
                "as": "stats",
            }},
            {"$project": {"_id": 0, "rows": {"$map": {"input": {"$range": [0, n_days]}, "as": "i", "in": day_row}}}},
            {"$unwind": "$rows"},
            {"$replaceRoot": {"newRoot": "$rows"}},
            {"$set": {"present_count": {"$ifNull": ["$stat.present", 0]},
                      "late_count": {"$ifNull": ["$stat.late", 0]},
                      "is_working_day": {"$lte": ["$dow", 5]}}},
            {"$set": {"rate": {"$cond": [{"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                                         {"$divide": ["$present_count", "$headcount"]}, None]}}},
            {"$setWindowFields": {"sortBy": {"date": 1},
                                  "output": {"moving_avg": {"$avg": "$rate", "window": {"documents": [-6, 0]}}}}},
            {"$project": {"_id": 0, "date": 1, "is_working_day": 1, "headcount": 1,
                          "present_count": 1, "late_count": 1, "rate": 1, "moving_avg": 1}},
            {"$sort": {"date": 1}},
        ],
    }


def run_pipeline(spec: dict) -> list:
    kwargs = {"hint": spec["hint"]} if spec["hint"] else {}
    return list(db[spec["collection"]].aggregate(spec["pipeline"], **kwargs))


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: MonthQ):
    first, last = parse_month(month)
    emp = get_employee_or_404(emp_code)
    rows = run_pipeline(pipeline_employee_monthly(emp_code, first, last))
    r = rows[0] if rows else {}
    wd = working_days(first, last, emp["joined_on"])
    present = float(r.get("present_days", 0))
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": wd,
        "present_days": present,
        "leave_days": int(r.get("leave_days", 0)),
        "late_count": int(r.get("late_count", 0)),
        "total_late_minutes": int(r.get("total_late_minutes", 0)),
        "total_overtime_minutes": int(r.get("total_overtime_minutes", 0)),
        "attendance_pct": round_half_up(Decimal(repr(present)) * 100 / wd, 2) if wd else None,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: MonthQ, department: Optional[str] = None):
    first, last = parse_month(month)
    items = []
    for r in run_pipeline(pipeline_department_summary(first, last, department)):
        cnt = int(r["wh_count"])
        avg = round_half_up(Decimal(int(round(r["wh_hundredths"]))) / Decimal(cnt) / 100, 2) if cnt else None
        items.append({
            "department": r["_id"],
            "headcount": int(r["headcount"]),
            "present_days": float(r["present_days"]),
            "avg_work_hours": avg,
            "late_count": int(r["late_count"]),
            "total_late_minutes": int(r["total_late_minutes"]),
            "leave_count": int(r["leave_count"]),
            "on_duty_count": int(r["on_duty_count"]),
        })
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: MonthQ, limit: LimitQ = 10, department: Optional[str] = None):
    first, last = parse_month(month)
    rows = run_pipeline(pipeline_late_leaderboard(first, last, limit, department))
    return {"month": month, "items": [
        {"rank": int(r["rank"]), "emp_code": r["emp_code"], "name": r["name"], "department": r["department"],
         "total_late_minutes": int(r["total_late_minutes"]), "late_count": int(r["late_count"])}
        for r in rows
    ]}


def _validated_trend_range(from_: str, to: str) -> tuple[Date, Date]:
    d_from, d_to = parse_date(from_, "from"), parse_date(to, "to")
    if d_to < d_from:
        raise HTTPException(422, "to must not be before from")
    if (d_to - d_from).days + 1 > 92:
        raise HTTPException(422, "range must not exceed 92 days")
    return d_from, d_to


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_: Annotated[str, Query(alias="from")], to: str):
    d_from, d_to = _validated_trend_range(from_, to)
    if db.employees.find_one({"department": department}, {"_id": 1}) is None:
        raise HTTPException(404, "unknown department")
    items = []
    for r in run_pipeline(pipeline_department_trend(department, d_from, d_to)):
        present, headcount = float(r["present_count"]), int(r["headcount"])
        rate = None
        if r.get("rate") is not None: 
            rate = round_half_up(Decimal(repr(present)) / Decimal(headcount), 4)
        items.append({
            "date": r["date"],
            "is_working_day": bool(r["is_working_day"]),
            "headcount": headcount,
            "present_count": present,
            "late_count": int(r["late_count"]),
            "attendance_rate": rate,
            "moving_avg_7d": None if r.get("moving_avg") is None else round_half_up(float(r["moving_avg"]), 4),
        })
    return {"department": department, "items": items}


def _require(**params):
    missing = [k for k, v in params.items() if v is None]
    if missing:
        raise HTTPException(422, f"missing required parameter(s): {', '.join(missing)}")


@app.get("/admin/explain/{endpoint}")
def explain(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],
    emp_code: Optional[str] = None,
    month: Annotated[Optional[str], Query(pattern=MONTH_PATTERN)] = None,
    department: Optional[str] = None,
    limit: LimitQ = 10,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    from_: Annotated[Optional[str], Query(alias="from")] = None,
    to: Optional[str] = None,
    page: PageQ = 1,
    page_size: PageSizeQ = 20,
):
    """executionStats explain of the endpoint's main query, built by the same functions the endpoint uses."""
    if endpoint == "attendance_list":
        spec = build_attendance_query(emp_code, date_from, date_to, status, page, page_size)
        collection = "attendance_logs"
        command: dict = {"find": collection, "filter": spec["filter"], "sort": dict(spec["sort"]),
                         "skip": spec["skip"], "limit": spec["limit"]}
    else:
        if endpoint == "employee_monthly":
            _require(emp_code=emp_code, month=month)
            built = pipeline_employee_monthly(emp_code, *parse_month(month))
        elif endpoint == "department_summary":
            _require(month=month)
            built = pipeline_department_summary(*parse_month(month), department)
        elif endpoint == "late_leaderboard":
            _require(month=month)
            built = pipeline_late_leaderboard(*parse_month(month), limit, department)
        else:  # department_trend
            _require(department=department, **{"from": from_, "to": to})
            built = pipeline_department_trend(department, *_validated_trend_range(from_, to))
        collection = built["collection"]
        command = {"aggregate": collection, "pipeline": built["pipeline"], "cursor": {}}
        if built["hint"]:
            command["hint"] = built["hint"]
    raw = db.command({"explain": command, "verbosity": "executionStats"})
    return {"endpoint": endpoint, "collection": collection,
            "explain": json.loads(json_util.dumps(raw, json_options=json_util.RELAXED_JSON_OPTIONS))}
