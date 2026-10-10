
import calendar
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import date as date_cls
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Any, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()  # override=False, so real environment variables take priority over .env
log = logging.getLogger("attendance")

# --------------------------------------------------------------------------- #
# 1. Configuration
# --------------------------------------------------------------------------- #
# tz_aware=True: PyMongo hands back aware UTC datetimes instead of naive ones.
client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    serverSelectionTimeoutMS=5000,
    maxPoolSize=100,
    appname="hrone-attendance",
)
db = client[os.getenv("MONGO_DB", "attendance_db")]
EMP = db["employees"]
LOGS = db["attendance_logs"]

# <rules>
# --------------------------------------------------------------------------- #
# 2. Business rules - pure functions, no database
# --------------------------------------------------------------------------- #
IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
PRESENCE = ("PRESENT", "WFH", "ON_DUTY")  # R6
GRACE_SECONDS = 10 * 60  # R2
MS_MIN, MS_MAX = 100_000_000_000, 4_102_444_800_000


def as_utc(dt: datetime) -> datetime:
    """Naive datetimes are treated as UTC (that is what Mongo stores)."""
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def trunc_seconds(dt: datetime) -> datetime:
    """R1: instants are truncated to whole seconds before any calculation."""
    return as_utc(dt).replace(microsecond=0)


def ms_to_dt(ms: int) -> datetime:
    return datetime.fromtimestamp(ms // 1000, tz=UTC)  # floor to whole seconds


def dt_to_ms(dt: datetime) -> int:
    d = as_utc(dt)
    return int(d.timestamp()) * 1000 + d.microsecond // 1000


def hhmm_minutes(value: str) -> int:
    h, m = value.split(":")
    return int(h) * 60 + int(m)


def is_overnight(shift_start: str, shift_end: str) -> bool:
    return hhmm_minutes(shift_end) <= hhmm_minutes(shift_start)


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    """R1: IST calendar date of the punch-in; an overnight shift's early-morning punch-in
    belongs to the shift that started the previous day."""
    local = trunc_seconds(punch_in).astimezone(IST)
    day = local.date()
    seconds_of_day = local.hour * 3600 + local.minute * 60 + local.second
    if is_overnight(shift_start, shift_end) and seconds_of_day < hhmm_minutes(shift_end) * 60:
        day -= timedelta(days=1)
    return day.isoformat()


def shift_bounds(date_str: str, shift_start: str, shift_end: str):
    """Aware IST datetimes for the shift that belongs to attendance date `date_str`."""
    d = date_cls.fromisoformat(date_str)
    midnight = datetime(d.year, d.month, d.day, tzinfo=IST)
    start_m, end_m = hhmm_minutes(shift_start), hhmm_minutes(shift_end)
    start = midnight + timedelta(minutes=start_m)
    end = midnight + timedelta(minutes=end_m) + (timedelta(days=1) if end_m <= start_m else timedelta())
    return start, end


def compute_late_minutes(punch_in: datetime, date_str: str, shift_start: str, shift_end: str) -> int:
    """R2: late only when strictly more than 10:00 after shift start; then floor minutes since shift start."""
    start, _ = shift_bounds(date_str, shift_start, shift_end)
    secs = (trunc_seconds(punch_in) - start).total_seconds()
    return int(secs // 60) if secs > GRACE_SECONDS else 0


def compute_overtime(punch_out: datetime, date_str: str, shift_start: str, shift_end: str) -> int:
    """R3: floor minutes after shift end, counted only when at least 30."""
    _, end = shift_bounds(date_str, shift_start, shift_end)
    secs = (trunc_seconds(punch_out) - end).total_seconds()
    minutes = int(secs // 60) if secs > 0 else 0
    return minutes if minutes >= 30 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: seconds / 3600, 2 decimals, half-up (Decimal, never float rounding)."""
    secs = int((trunc_seconds(punch_out) - trunc_seconds(punch_in)).total_seconds())
    return float((Decimal(secs) / Decimal(3600)).quantize(Decimal("0.01"), ROUND_HALF_UP))


def derive(shift_start: str, shift_end: str, date_str: str, punch_in, punch_out) -> dict:
    """All derived fields for a record (R2-R5). ABSENT / LEAVE records pass punch_in=None."""
    out = {"late_minutes": 0, "work_hours": None, "overtime_minutes": 0, "half_day": False}
    if punch_in is None:
        return out
    out["late_minutes"] = compute_late_minutes(punch_in, date_str, shift_start, shift_end)
    if punch_out is not None:
        wh = compute_work_hours(punch_in, punch_out)
        out["work_hours"] = wh
        out["overtime_minutes"] = compute_overtime(punch_out, date_str, shift_start, shift_end)
        out["half_day"] = wh < 4.5  # R5: on the already-rounded value
    return out


def month_bounds(month: str):
    year, mon = int(month[:4]), int(month[5:7])
    return f"{month}-01", f"{month}-{calendar.monthrange(year, mon)[1]:02d}"


def working_days(month: str, joined_on: str) -> int:
    """R7: Mon-Fri, counted from joined_on when that falls inside the month."""
    first, last = month_bounds(month)
    day, end = date_cls.fromisoformat(max(first, joined_on)), date_cls.fromisoformat(last)
    count = 0
    while day <= end:
        count += day.weekday() < 5
        day += timedelta(days=1)
    return count


def half_up(value: Decimal, places: int) -> float:
    """R8: half-up rounding of an exact Decimal."""
    return float(value.quantize(Decimal(1).scaleb(-places), ROUND_HALF_UP))
# </rules>


# --------------------------------------------------------------------------- #
# 3. Models and API helpers
# --------------------------------------------------------------------------- #
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]
# StrictInt: floats ("1.5", 1.5), strings and booleans are rejected -> 422. Range rejects seconds.
Millis = Annotated[StrictInt, Field(ge=MS_MIN, le=MS_MAX)]
HHMM = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"
DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
MONTH_PATTERN = r"^[0-9]{4}-(0[1-9]|1[0-2])$"


def bad(message: str, field: str = "", where: str = "query"):
    """Raise a 422 in FastAPI's own validation-error shape."""
    loc = (where, field) if field else (where,)
    raise RequestValidationError([{"type": "value_error", "loc": loc, "msg": message, "input": None}])


def check_date(value: Optional[str], field: str, where: str = "query") -> Optional[str]:
    if value is None:
        return None
    try:
        if not DATE_RE.match(value):
            raise ValueError
        date_cls.fromisoformat(value)
    except ValueError:
        bad(f"{field} must be a real date in YYYY-MM-DD format", field, where)
    return value


def check_month(value: Optional[str], field: str = "month") -> str:
    if value is None:
        bad("month is required (YYYY-MM)", field)
    if not re.match(MONTH_PATTERN, value) or int(value[:4]) < 1:
        bad("month must be YYYY-MM", field)
    return value


class EmployeeIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    emp_code: str = Field(pattern=r"^EMP[0-9]{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=HHMM)
    shift_end: str = Field("18:30", pattern=HHMM)
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def _real_date(cls, v):
        if not DATE_RE.match(v):
            raise ValueError("joined_on must be YYYY-MM-DD")
        date_cls.fromisoformat(v)
        return v

    @model_validator(mode="after")
    def _shift_differs(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str = Field(min_length=1, max_length=20)
    punched_at: Optional[Millis] = None
    status: PresenceStatus = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str = Field(min_length=1, max_length=20)
    punched_at: Optional[Millis] = None


class RegularizeIn(BaseModel):
    # Unknown keys (e.g. a client sending work_hours) are ignored, as the contract asks.
    model_config = ConfigDict(str_strip_whitespace=True)
    status: Optional[Status] = None
    punch_in: Optional[Millis] = None
    punch_out: Optional[Millis] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


def to_api(value: Any) -> Any:
    """Datetimes (also nested inside history) -> epoch milliseconds."""
    if isinstance(value, datetime):
        return dt_to_ms(value)
    if isinstance(value, dict):
        return {k: to_api(v) for k, v in value.items()}
    if isinstance(value, list):
        return [to_api(v) for v in value]
    return value


def emp_out(d: dict) -> dict:
    return {
        "emp_code": d["emp_code"], "name": d.get("name"), "email": d.get("email"),
        "department": d.get("department"), "shift_start": d.get("shift_start"),
        "shift_end": d.get("shift_end"), "joined_on": d.get("joined_on"),
        "created_at": to_api(d.get("created_at")),
    }


def rec_out(d: dict) -> dict:
    """Stored document -> API record. Never exposes _id; missing history/half_day mean empty/false."""
    return {
        "emp_code": d["emp_code"], "date": d["date"], "status": d["status"],
        "punch_in": to_api(d.get("punch_in")), "punch_out": to_api(d.get("punch_out")),
        "work_hours": d.get("work_hours"),
        "late_minutes": int(d.get("late_minutes") or 0),
        "overtime_minutes": int(d.get("overtime_minutes") or 0),
        "half_day": bool(d.get("half_day")),
        "history": [
            {"at": to_api(h.get("at")), "by": h.get("by"), "reason": h.get("reason"),
             "changes": to_api(h.get("changes") or {})}
            for h in (d.get("history") or [])
        ],
    }


def get_employee(emp_code: str) -> dict:
    emp = EMP.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, f"employee {emp_code} not found")
    return emp


PageQ = Annotated[int, Query(ge=1)]
SizeQ = Annotated[int, Query(ge=1, le=100)]
MonthQ = Annotated[str, Query(pattern=MONTH_PATTERN)]


# --------------------------------------------------------------------------- #
# 4. Indexes + app lifecycle
# --------------------------------------------------------------------------- #
_indexes_ready = False


def ensure_indexes() -> None:
    """Idempotent: re-creating an identical index is a no-op."""
    global _indexes_ready
    EMP.create_index([("emp_code", ASCENDING)], unique=True, name="uniq_emp_code")
    EMP.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name="dept_emp")
    EMP.create_index([("joined_on", ASCENDING), ("department", ASCENDING)], name="joined_dept")
    EMP.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="dept_joined")
    LOGS.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uniq_emp_date")
    LOGS.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="date_emp")
    LOGS.create_index([("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)], name="status_date_emp")
    LOGS.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)], name="emp_punch_in")
    _indexes_ready = True


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        ensure_indexes()
    except PyMongoError as exc:  # Mongo not up yet: /health reports 503 and retries index creation
        log.warning("index creation deferred: %s", exc)
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.1.0", lifespan=lifespan)


@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        if not _indexes_ready:
            ensure_indexes()
    except PyMongoError:
        raise HTTPException(503, "MongoDB is not reachable")
    return {"status": "ok"}


# --------------------------------------------------------------------------- #
# 5. Employees and attendance
# --------------------------------------------------------------------------- #
@app.post("/employees", status_code=201, tags=["employees"])
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = datetime.now(UTC)
    try:
        EMP.insert_one(doc)  # the unique index is the guarantee; no check-then-insert
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return emp_out(doc)


@app.get("/employees", tags=["employees"])
def list_employees(department: Optional[str] = None, page: PageQ = 1, page_size: SizeQ = 20):
    q = {"department": department} if department else {}
    total = EMP.count_documents(q) if q else EMP.estimated_document_count()
    cur = EMP.find(q, {"_id": 0}).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size)
    return {"items": [emp_out(d) for d in cur], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201, tags=["attendance"])
def punch_in(body: PunchInIn):
    emp = get_employee(body.emp_code)
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else trunc_seconds(datetime.now(UTC))
    day = attendance_date(ts, emp["shift_start"], emp["shift_end"])
    doc = {"emp_code": body.emp_code, "date": day, "status": body.status, "punch_in": ts,
           "punch_out": None, "history": []}
    doc.update(derive(emp["shift_start"], emp["shift_end"], day, ts, None))
    try:
        LOGS.insert_one(doc)  # unique (emp_code, date): exactly one of N parallel requests wins
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return rec_out(doc)


@app.post("/attendance/punch-out", tags=["attendance"])
def punch_out(body: PunchOutIn):
    emp = get_employee(body.emp_code)
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else trunc_seconds(datetime.now(UTC))
    rec = LOGS.find_one({"emp_code": body.emp_code, "punch_in": {"$lte": ts}}, sort=[("punch_in", DESCENDING)])
    if rec is None:
        raise HTTPException(404, "no punch-in found for this employee at or before that time")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    pin = trunc_seconds(rec["punch_in"])
    if ts <= pin:
        bad("punched_at must be after the punch-in", "punched_at", "body")
    if ts - pin > timedelta(hours=24):
        bad("punched_at is more than 24 hours after the punch-in", "punched_at", "body")
    d = derive(emp["shift_start"], emp["shift_end"], rec["date"], pin, ts)
    updated = LOGS.find_one_and_update(
        # the filter is the lock: only one concurrent punch-out can still see punch_out == null
        {"_id": rec["_id"], "punch_out": None, "punch_in": rec["punch_in"]},
        {"$set": {"punch_out": ts, "work_hours": d["work_hours"],
                  "overtime_minutes": d["overtime_minutes"], "half_day": d["half_day"]}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "already punched out")
    return rec_out(updated)


def build_attendance_query(emp_code, date_from, date_to, status, page, page_size):
    """Shared by GET /attendance and the explain endpoint so both run the same query."""
    check_date(date_from, "date_from")
    check_date(date_to, "date_to")
    if date_from and date_to and date_from > date_to:
        bad("date_from must not be after date_to", "date_from")
    q: dict = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status
    return q, {"date": -1, "emp_code": 1}, (page - 1) * page_size, page_size


@app.get("/attendance", tags=["attendance"])
def list_attendance(emp_code: Optional[str] = None, date_from: Optional[str] = None,
                    date_to: Optional[str] = None, status: Optional[Status] = None,
                    page: PageQ = 1, page_size: SizeQ = 20):
    q, sort, skip, limit = build_attendance_query(emp_code, date_from, date_to, status, page, page_size)
    total = LOGS.count_documents(q) if q else LOGS.estimated_document_count()
    cur = LOGS.find(q, {"_id": 0}).sort(list(sort.items())).skip(skip).limit(limit)
    return {"items": [rec_out(d) for d in cur], "total": total, "page": page, "page_size": page_size}


TRACKED = ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day")


@app.patch("/attendance/{emp_code}/{date}", tags=["attendance"])
def regularize(emp_code: str, body: RegularizeIn, date: str = Path(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")):
    check_date(date, "date", "path")
    emp = get_employee(emp_code)
    rec = LOGS.find_one({"emp_code": emp_code, "date": date})
    if rec is None:
        raise HTTPException(404, f"no attendance record for {emp_code} on {date}")

    new_status = body.status or rec["status"]
    old_in = trunc_seconds(rec["punch_in"]) if rec.get("punch_in") else None
    old_out = trunc_seconds(rec["punch_out"]) if rec.get("punch_out") else None
    if new_status in PRESENCE:
        new_in = ms_to_dt(body.punch_in) if body.punch_in is not None else old_in
        new_out = ms_to_dt(body.punch_out) if body.punch_out is not None else old_out
        if new_in is None:
            bad("a presence status requires a punch_in", "punch_in", "body")
        if attendance_date(new_in, emp["shift_start"], emp["shift_end"]) != date:
            bad("punch_in must stay on the record's attendance date", "punch_in", "body")
        if new_out is not None and not (new_in < new_out <= new_in + timedelta(hours=24)):
            bad("punch_out must be after punch_in and within 24 hours", "punch_out", "body")
    else:  # ABSENT / LEAVE clear both punch times
        if body.punch_in is not None or body.punch_out is not None:
            bad(f"{new_status} clears punch times; do not supply punch_in/punch_out", "status", "body")
        new_in = new_out = None

    new = {"status": new_status, "punch_in": new_in, "punch_out": new_out}
    new.update(derive(emp["shift_start"], emp["shift_end"], date, new_in, new_out))
    old = {"status": rec["status"], "punch_in": old_in, "punch_out": old_out,
           "work_hours": rec.get("work_hours"), "late_minutes": int(rec.get("late_minutes") or 0),
           "overtime_minutes": int(rec.get("overtime_minutes") or 0), "half_day": bool(rec.get("half_day"))}
    changes = {k: {"from": old[k], "to": new[k]} for k in TRACKED if old[k] != new[k]}
    if not changes:
        bad("the request changes nothing", "", "body")

    history = rec.get("history") or []
    n = len(history)
    entry = {"at": trunc_seconds(datetime.now(UTC)), "by": body.regularized_by, "reason": body.reason, "changes": changes}
    guard = {"emp_code": emp_code, "date": date, "status": rec["status"],
             "punch_in": rec.get("punch_in"), "punch_out": rec.get("punch_out"),
             f"history.{n}": {"$exists": False}}  # array length must still be n: nobody appended meanwhile
    if n:
        guard[f"history.{n - 1}"] = {"$exists": True}
    updated = LOGS.find_one_and_update(guard, {"$set": new, "$push": {"history": entry}},
                                       return_document=ReturnDocument.AFTER)
    if updated is None:
        raise HTTPException(409, "record was modified concurrently; reload and retry")
    return rec_out(updated)


# --------------------------------------------------------------------------- #
# 6. Analytics (MongoDB aggregation pipelines)
# --------------------------------------------------------------------------- #
IS_PRESENT = {"$in": ["$status", list(PRESENCE)]}
# Presence counted in half-units so everything stays an exact integer: full day = 2, half day = 1.
PRESENT_X2 = {"$cond": [IS_PRESENT, {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 1, 2]}, 0]}
LATE = {"$ifNull": ["$late_minutes", 0]}
IS_LATE = {"$gt": [LATE, 0]}
IS_WEEKDAY = {"$let": {
    "vars": {"w": {"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d", "onError": None}}}},
    "in": {"$and": [{"$ne": ["$$w", None]}, {"$lte": ["$$w", 5]}]}}}
HAS_HOURS = {"$and": [IS_PRESENT, {"$isNumber": {"$ifNull": ["$work_hours", None]}}]}
HOURS_CENTS = {"$toLong": {"$round": [{"$multiply": [{"$ifNull": ["$work_hours", 0]}, 100]}, 0]}}


def month_match(month: str) -> dict:
    first, last = month_bounds(month)
    return {"$match": {"date": {"$gte": first, "$lte": last}}}


def employee_monthly_pipeline(emp_code: str, month: str) -> list:
    first, last = month_bounds(month)
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first, "$lte": last}}},
        {"$group": {
            "_id": None,
            "present_x2": {"$sum": {"$cond": [IS_WEEKDAY, PRESENT_X2, 0]}},  # R7: weekday presence only
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [IS_LATE, 1, 0]}},
            "total_late_minutes": {"$sum": LATE},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]


def dept_summary_pipeline(month: str, department: Optional[str]) -> list:
    _, last = month_bounds(month)
    union_match: dict = {"joined_on": {"$lte": last}}
    if department:
        union_match["department"] = department
    sums = {k: {"$sum": f"${k}"} for k in
            ("present_x2", "cents", "hours_n", "late_count", "late_total", "leave_count", "duty_count")}
    pipe: list = [
        month_match(month),
        {"$group": {  # one row per employee first, so the join below runs once per person
            "_id": "$emp_code",
            "present_x2": {"$sum": {"$cond": [IS_WEEKDAY, PRESENT_X2, 0]}},
            "cents": {"$sum": {"$cond": [HAS_HOURS, HOURS_CENTS, 0]}},  # exact integer cents of work_hours
            "hours_n": {"$sum": {"$cond": [HAS_HOURS, 1, 0]}},
            "late_count": {"$sum": {"$cond": [IS_LATE, 1, 0]}},
            "late_total": {"$sum": LATE},
            "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
        }},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "e",
                     "pipeline": [{"$project": {"_id": 0, "department": 1}}]}},
        {"$unwind": "$e"},  # logs of unknown employees disappear here
    ]
    if department:
        pipe.append({"$match": {"e.department": department}})
    pipe += [
        {"$group": {"_id": "$e.department", **sums}},
        # headcount (R9) comes from the employees collection, so people without logs are counted
        {"$unionWith": {"coll": "employees", "pipeline": [
            {"$match": union_match},
            {"$group": {"_id": "$department", "headcount": {"$sum": 1}}}]}},
        {"$group": {"_id": "$_id", "headcount": {"$sum": "$headcount"}, **sums}},
        {"$match": {"headcount": {"$gt": 0}}},
        {"$sort": {"_id": 1}},
    ]
    return pipe


def run_dept_summary(month: str, department: Optional[str]):
    items, cents_all, n_all = [], 0, 0
    for r in LOGS.aggregate(dept_summary_pipeline(month, department)):
        cents, n = int(r.get("cents", 0)), int(r.get("hours_n", 0))
        cents_all, n_all = cents_all + cents, n_all + n
        items.append({
            "department": r["_id"], "headcount": int(r["headcount"]),
            "present_days": int(r.get("present_x2", 0)) / 2,
            "avg_work_hours": half_up(Decimal(cents) / Decimal(n) / 100, 2) if n else None,
            "late_count": int(r.get("late_count", 0)), "total_late_minutes": int(r.get("late_total", 0)),
            "leave_count": int(r.get("leave_count", 0)), "on_duty_count": int(r.get("duty_count", 0)),
        })
    overall = half_up(Decimal(cents_all) / Decimal(n_all) / 100, 2) if n_all else None
    return items, overall


def leaderboard_pipeline(month: str, limit: int, department: Optional[str]) -> list:
    pipe: list = [
        month_match(month),
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": LATE},
                    "late_count": {"$sum": {"$cond": [IS_LATE, 1, 0]}}}},
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "e",
                     "pipeline": [{"$project": {"_id": 0, "name": 1, "department": 1}}]}},
        {"$unwind": "$e"},
    ]
    if department:
        pipe.append({"$match": {"e.department": department}})  # ranking happens inside the department
    pipe += [
        # $rank = standard competition ranking (1, 2, 2, 4); $limit would cut ties apart, so filter on rank
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$e.name", "department": "$e.department",
                      "total_late_minutes": 1, "late_count": 1}},
    ]
    return pipe


def trend_inputs(department: str, frm: str, to: str):
    check_date(frm, "from")
    check_date(to, "to")
    if to < frm:
        bad("to must not be before from", "to")
    span = (date_cls.fromisoformat(to) - date_cls.fromisoformat(frm)).days + 1
    if span > 92:
        bad("the range must not exceed 92 days", "to")
    codes = EMP.distinct("emp_code", {"department": department})
    if not codes:
        raise HTTPException(404, f"department {department} not found")
    joins = [{"d": r["_id"], "n": r["n"]} for r in EMP.aggregate(
        [{"$match": {"department": department}}, {"$group": {"_id": "$joined_on", "n": {"$sum": 1}}}])]
    return codes, joins


def trend_pipeline(codes: list, joins: list, frm: str, to: str) -> list:
    lo = datetime.fromisoformat(frm).replace(tzinfo=UTC)
    hi = datetime.fromisoformat(to).replace(tzinfo=UTC) + timedelta(days=1)  # densify upper bound is exclusive
    window = {"documents": [-6, 0]}
    return [
        {"$match": {"emp_code": {"$in": codes}, "date": {"$gte": frm, "$lte": to}}},
        {"$group": {"_id": "$date", "px2": {"$sum": PRESENT_X2}, "late": {"$sum": {"$cond": [IS_LATE, 1, 0]}}}},
        {"$addFields": {"d": {"$dateFromString": {"dateString": "$_id", "format": "%Y-%m-%d"}}}},
        {"$densify": {"field": "d", "range": {"step": 1, "unit": "day", "bounds": [lo, hi]}}},  # one row per day
        {"$addFields": {"date": {"$dateToString": {"date": "$d", "format": "%Y-%m-%d"}},
                        "px2": {"$ifNull": ["$px2", 0]}, "late": {"$ifNull": ["$late", 0]},
                        "working": {"$lte": [{"$isoDayOfWeek": "$d"}, 5]}}},
        {"$addFields": {"hc": {"$reduce": {  # R9: employees who had joined by that day
            "input": {"$literal": joins}, "initialValue": 0,
            "in": {"$cond": [{"$lte": ["$$this.d", "$date"]}, {"$add": ["$$value", "$$this.n"]}, "$$value"]}}}}},
        # rate in integer ten-thousandths, half-up: floor((2*a + h) / (2*h)) with a = px2 * 5000 -> exact, no float ties
        {"$addFields": {"rate_i": {"$cond": [
            {"$and": ["$working", {"$gt": ["$hc", 0]}]},
            {"$floor": {"$divide": [{"$add": [{"$multiply": ["$px2", 10000]}, "$hc"]}, {"$multiply": [2, "$hc"]}]}},
            None]}}},
        {"$setWindowFields": {"sortBy": {"d": 1}, "output": {
            "s": {"$sum": "$rate_i", "window": window},
            "n": {"$sum": {"$cond": [{"$ne": ["$rate_i", None]}, 1, 0]}, "window": window}}}},
        {"$sort": {"d": 1}},
        {"$project": {
            "_id": 0, "date": 1, "is_working_day": "$working", "headcount": "$hc",
            "present_count": {"$divide": ["$px2", 2]}, "late_count": "$late",
            "attendance_rate": {"$cond": [{"$eq": ["$rate_i", None]}, None, {"$divide": ["$rate_i", 10000]}]},
            "moving_avg_7d": {"$cond": [{"$gt": ["$n", 0]}, {"$divide": [
                {"$floor": {"$divide": [{"$add": [{"$multiply": ["$s", 2]}, "$n"]}, {"$multiply": [2, "$n"]}]}},
                10000]}, None]}}},
    ]


@app.get("/analytics/employees/{emp_code}/monthly", tags=["analytics"])
def employee_monthly(emp_code: str, month: MonthQ):
    check_month(month)
    emp = get_employee(emp_code)
    wd = working_days(month, emp["joined_on"])
    rows = list(LOGS.aggregate(employee_monthly_pipeline(emp_code, month)))
    r = rows[0] if rows else {}
    x2 = int(r.get("present_x2", 0))
    return {
        "emp_code": emp_code, "month": month, "working_days": wd, "present_days": x2 / 2,
        "leave_days": int(r.get("leave_days", 0)), "late_count": int(r.get("late_count", 0)),
        "total_late_minutes": int(r.get("total_late_minutes", 0)),
        "total_overtime_minutes": int(r.get("total_overtime_minutes", 0)),
        "attendance_pct": half_up(Decimal(x2) * 50 / Decimal(wd), 2) if wd else None,
    }


@app.get("/analytics/departments/summary", tags=["analytics"])
def department_summary(month: MonthQ, department: Optional[str] = None):
    check_month(month)
    items, _ = run_dept_summary(month, department)
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late", tags=["analytics"])
def late_leaderboard(month: MonthQ, limit: Annotated[int, Query(ge=1, le=50)] = 10, department: Optional[str] = None):
    check_month(month)
    items = list(LOGS.aggregate(leaderboard_pipeline(month, limit, department)))
    for r in items:
        r["rank"], r["total_late_minutes"], r["late_count"] = int(r["rank"]), int(r["total_late_minutes"]), int(r["late_count"])
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend", tags=["analytics"])
def department_trend(department: str, from_: Annotated[str, Query(alias="from")], to: str):
    codes, joins = trend_inputs(department, from_, to)
    return {"department": department, "items": list(LOGS.aggregate(trend_pipeline(codes, joins, from_, to)))}


@app.get("/admin/explain/{endpoint}", tags=["admin"])
def explain_endpoint(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],
    emp_code: Optional[str] = None, month: Annotated[Optional[str], Query(pattern=MONTH_PATTERN)] = None, department: Optional[str] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 10, date_from: Optional[str] = None, date_to: Optional[str] = None,
    status: Optional[Status] = None, from_: Annotated[Optional[str], Query(alias="from")] = None,
    to: Optional[str] = None, page: PageQ = 1, page_size: SizeQ = 20,
):
    """executionStats explain of the endpoint's main query, built by the very same functions the endpoint uses."""
    if endpoint == "attendance_list":
        q, sort, skip, lim = build_attendance_query(emp_code, date_from, date_to, status, page, page_size)
        coll, cmd = "attendance_logs", {"find": "attendance_logs", "filter": q, "sort": sort, "skip": skip, "limit": lim}
    else:
        if endpoint == "employee_monthly":
            if not emp_code:
                bad("emp_code is required", "emp_code")
            pipe = employee_monthly_pipeline(emp_code, check_month(month))
        elif endpoint == "department_summary":
            pipe = dept_summary_pipeline(check_month(month), department)
        elif endpoint == "late_leaderboard":
            pipe = leaderboard_pipeline(check_month(month), limit, department)
        else:
            if not (department and from_ and to):
                bad("department, from and to are required", "department")
            codes, joins = trend_inputs(department, from_, to)
            pipe = trend_pipeline(codes, joins, from_, to)
        coll, cmd = "attendance_logs", {"aggregate": "attendance_logs", "pipeline": pipe, "cursor": {}}
    raw = db.command({"explain": cmd, "verbosity": "executionStats"})
    return {"endpoint": endpoint, "collection": coll,
            "explain": json.loads(json_util.dumps(raw, json_options=json_util.RELAXED_JSON_OPTIONS))}


# --------------------------------------------------------------------------- #
# 7. Dashboard (extra, outside the contract)
# --------------------------------------------------------------------------- #
@app.get("/dashboard/api/overview", tags=["dashboard"])
def dashboard_overview(month: Annotated[Optional[str], Query(pattern=MONTH_PATTERN)] = None):
    """One call that feeds the dashboard header: defaults to the month of the newest record."""
    newest = LOGS.find_one({}, {"date": 1, "_id": 0}, sort=[("date", DESCENDING)])
    month = check_month(month) if month else (newest["date"][:7] if newest else datetime.now(IST).strftime("%Y-%m"))
    items, overall = run_dept_summary(month, None)
    return {
        "month": month, "newest_record": newest["date"] if newest else None,
        "employees": EMP.estimated_document_count(), "records": LOGS.estimated_document_count(),
        "departments": sorted(EMP.distinct("department")), "summary": items, "avg_work_hours": overall,
    }


@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse("/dashboard")


@app.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
def dashboard():
    return HTMLResponse(DASHBOARD_HTML)


DASHBOARD_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Punchcard · Attendance control room</title>
<style>
:root{--bg:#E6ECF0;--panel:#FFFFFF;--wash:#F2F5F7;--ink:#101C28;--mute:#566776;--line:#C9D3DC;--pres:#0E7C73;--late:#B96F00;--abs:#B42336;--acc:#2747D8;--hatch:#D5DDE4;--shadow:0 1px 0 rgba(16,28,40,.04)}
:root[data-theme=dark]{--bg:#0B131A;--panel:#121E28;--wash:#0E1920;--ink:#E7EEF4;--mute:#8EA1B2;--line:#25364457;--pres:#38C4B5;--late:#E8A53D;--abs:#F06A80;--acc:#8AA2FF;--hatch:#1B2A36;--shadow:none}
:root[data-theme=dark]{--line:#273846}
*{box-sizing:border-box}
html,body{margin:0;height:100%}
body{background:var(--bg);color:var(--ink);font:14px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;display:grid;grid-template-columns:208px 1fr}
button,input,select{font:inherit;color:inherit}
.num{font-variant-numeric:tabular-nums}
.serif{font-family:ui-serif,"Iowan Old Style","Palatino Linotype",Georgia,serif}
:focus-visible{outline:2px solid var(--acc);outline-offset:2px}
/* rail */
nav{background:var(--ink);color:#E7EEF4;padding:20px 14px;display:flex;flex-direction:column;gap:4px;position:sticky;top:0;height:100vh}
:root[data-theme=dark] nav{background:#070D12;border-right:1px solid var(--line)}
.brand{font-size:21px;margin:0 6px 2px}
.tag{color:#8EA1B2;font-size:12px;margin:0 6px 18px}
.tick{height:10px;margin:0 6px 18px;background:repeating-linear-gradient(90deg,#8EA1B2 0 1px,transparent 1px 6px);opacity:.55}
nav button{background:none;border:0;color:#B7C5D1;text-align:left;padding:9px 10px;border-radius:6px;cursor:pointer;display:flex;justify-content:space-between}
nav button:hover{background:#ffffff14}
nav button[aria-current=page]{background:#ffffff1f;color:#fff}
nav .sp{flex:1}
nav .small{font-size:12px;color:#8EA1B2;margin:6px}
nav label{display:flex;gap:8px;align-items:center;color:#B7C5D1;font-size:13px;margin:4px 6px}
/* main */
main{padding:22px 28px 60px;min-width:0}
header.top{display:flex;gap:16px;align-items:end;justify-content:space-between;flex-wrap:wrap;margin-bottom:14px}
h1{margin:0;font-size:38px;line-height:1.05;font-weight:600;letter-spacing:-.01em}
h2{margin:0 0 10px;font-size:17px;font-weight:650}
.sub{color:var(--mute);margin-top:4px}
.ctl{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
input,select{background:var(--panel);border:1px solid var(--line);border-radius:6px;padding:7px 10px;min-height:34px}
.btn{background:var(--ink);color:var(--bg);border:0;border-radius:6px;padding:7px 14px;cursor:pointer;min-height:34px}
:root[data-theme=dark] .btn{background:var(--acc);color:#0B131A}
.btn.ghost{background:transparent;color:var(--ink);border:1px solid var(--line)}
.btn:disabled{opacity:.5;cursor:wait}
section.panel{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:16px 18px;margin-bottom:16px;box-shadow:var(--shadow)}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));margin:0 0 16px;border-top:1px solid var(--line);border-bottom:1px solid var(--line)}
.kpi{padding:12px 16px 12px 0;margin-right:16px;border-right:1px solid var(--line)}
.kpi:last-child{border-right:0}
.kpi b{display:block;font-size:28px;font-weight:600;line-height:1.1}
.kpi span{color:var(--mute);font-size:12.5px}
.cols{display:grid;grid-template-columns:minmax(0,1.5fr) minmax(0,1fr);gap:16px}
@media(max-width:1100px){.cols{grid-template-columns:1fr}}
@media(max-width:820px){body{grid-template-columns:1fr}nav{position:static;height:auto;flex-direction:row;flex-wrap:wrap;align-items:center}nav .tick,nav .tag,nav .sp,nav .small{display:none}main{padding:16px}}
/* heatmap */
.heat{display:grid;gap:3px;align-items:center;overflow-x:auto;padding-bottom:4px}
.heat .lab{font-size:13px;padding-right:10px;white-space:nowrap}
.heat .cell{height:26px;min-width:14px;border-radius:3px;background:var(--wash)}
.heat .cell.off{background:repeating-linear-gradient(135deg,var(--hatch) 0 3px,transparent 3px 6px)}
.heat .dn{font-size:10.5px;color:var(--mute);text-align:center}
.legend{display:flex;gap:6px;align-items:center;color:var(--mute);font-size:12px;margin-top:10px}
.legend i{width:22px;height:10px;border-radius:2px;display:inline-block}
/* tables */
table{width:100%;border-collapse:collapse}
th{font-weight:600;color:var(--mute);text-align:left;font-size:12.5px;padding:6px 8px;border-bottom:1px solid var(--line);white-space:nowrap}
td{padding:8px;border-bottom:1px solid var(--line)}
tr.click{cursor:pointer}
tr.click:hover td{background:var(--wash)}
td.r,th.r{text-align:right}
.code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12.5px}
.bar{height:6px;background:var(--wash);border-radius:3px;overflow:hidden;min-width:60px}
.bar i{display:block;height:100%;background:var(--pres)}
.bar.late i{background:var(--late)}
.pill{display:inline-block;padding:1px 8px;border-radius:99px;font-size:12px;border:1px solid var(--line)}
.pill.PRESENT,.pill.WFH,.pill.ON_DUTY{color:var(--pres);border-color:var(--pres)}
.pill.ABSENT{color:var(--abs);border-color:var(--abs)}
.pill.LEAVE{color:var(--late);border-color:var(--late)}
.pill.ok{color:var(--pres);border-color:var(--pres)}.pill.no{color:var(--abs);border-color:var(--abs)}
.scroll{overflow-x:auto}
.pager{display:flex;gap:10px;align-items:center;margin-top:10px;color:var(--mute)}
.board{list-style:none;margin:0;padding:0}
.board li{display:grid;grid-template-columns:34px 1fr auto;gap:10px;align-items:center;padding:7px 0;border-bottom:1px solid var(--line)}
.board .rk{font-size:22px;color:var(--mute)}
.empty{color:var(--mute);padding:14px 0}
svg text{fill:var(--mute);font-size:11px}
/* forms & drawer */
.forms{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:16px}
form.f{display:grid;gap:8px}
form.f label{display:grid;gap:3px;color:var(--mute);font-size:12.5px}
form.f .row{display:grid;grid-template-columns:1fr 1fr;gap:8px}
pre{background:var(--wash);border:1px solid var(--line);border-radius:6px;padding:10px;overflow:auto;max-height:260px;font-size:12px;margin:8px 0 0}
aside#drawer{position:fixed;top:0;right:0;width:min(560px,100%);height:100%;background:var(--panel);border-left:1px solid var(--line);padding:20px;overflow:auto;transform:translateX(105%);transition:transform .22s ease;z-index:5}
aside#drawer.open{transform:none}
.cal{display:grid;grid-template-columns:repeat(7,1fr);gap:4px;margin:8px 0 14px}
.cal div{min-height:44px;border-radius:5px;background:var(--wash);padding:3px 5px;font-size:11px;color:var(--mute);position:relative}
.cal div b{display:block;color:var(--ink);font-weight:500}
.cal .PRESENT,.cal .WFH,.cal .ON_DUTY{background:color-mix(in srgb,var(--pres) 22%,var(--panel))}
.cal .late{box-shadow:inset 0 -3px 0 var(--late)}
.cal .ABSENT{background:color-mix(in srgb,var(--abs) 24%,var(--panel))}
.cal .LEAVE{background:color-mix(in srgb,var(--late) 22%,var(--panel))}
.cal .half{background-image:linear-gradient(135deg,transparent 55%,var(--panel) 55% 62%,transparent 62%)}
#toast{position:fixed;left:50%;bottom:22px;transform:translateX(-50%);background:var(--ink);color:var(--bg);padding:9px 16px;border-radius:8px;opacity:0;pointer-events:none;transition:opacity .2s;z-index:9;max-width:90vw}
#toast.show{opacity:1}#toast.err{background:var(--abs);color:#fff}
.hide{display:none!important}
@media(prefers-reduced-motion:reduce){aside#drawer,#toast{transition:none}}
</style>
</head>
<body>
<nav aria-label="Sections">
  <div class="brand serif">Punchcard</div>
  <div class="tag">attendance control room</div>
  <div class="tick" aria-hidden="true"></div>
  <button data-tab="overview" aria-current="page">Overview</button>
  <button data-tab="employees">Employees</button>
  <button data-tab="records">Records</button>
  <button data-tab="desk">Desk</button>
  <button data-tab="plans">Query plans</button>
  <div class="sp"></div>
  <label><input type="checkbox" id="auto"> Auto-refresh 30s</label>
  <label><input type="checkbox" id="dark"> Dark mode</label>
  <div class="small" id="foot">&nbsp;</div>
  <div class="small"><a href="/docs" style="color:#B7C5D1">API docs</a></div>
</nav>
<main>
<header class="top">
  <div><h1 class="serif" id="title">Month at a glance</h1><div class="sub" id="subtitle">Loading…</div></div>
  <div class="ctl"><label class="sub" for="month">Month</label><input type="month" id="month"><button class="btn ghost" id="reload">Reload</button></div>
</header>

<div id="t-overview">
  <div class="kpis" id="kpis"></div>
  <section class="panel">
    <h2>Attendance rate by department and day</h2>
    <div id="heat" class="heat"></div>
    <div class="legend"><span>low</span><i style="background:color-mix(in srgb,var(--pres) 12%,var(--wash))"></i><i style="background:color-mix(in srgb,var(--pres) 40%,var(--wash))"></i><i style="background:color-mix(in srgb,var(--pres) 70%,var(--wash))"></i><i style="background:var(--pres)"></i><span>full</span><i class="cell off" style="background:repeating-linear-gradient(135deg,var(--hatch) 0 3px,transparent 3px 6px)"></i><span>weekend</span></div>
  </section>
  <div class="cols">
    <section class="panel"><h2>Departments</h2><div class="scroll" id="depts"></div></section>
    <section class="panel"><h2>Late-comers <select id="boardDept" aria-label="Leaderboard department"></select></h2><ol class="board" id="board"></ol></section>
  </div>
  <section class="panel"><h2>Trend <select id="trendDept" aria-label="Trend department"></select> <span class="sub">bars: daily rate · line: 7-day moving average</span></h2><div class="scroll" id="trend"></div></section>
</div>

<div id="t-employees" class="hide">
  <section class="panel">
    <div class="ctl" style="margin-bottom:10px"><select id="empDept" aria-label="Department"><option value="">All departments</option></select>
    <input id="empFilter" placeholder="Filter this page…  ( / )" aria-label="Filter"><input id="openCode" placeholder="Open EMP0001" class="code" style="width:130px"><button class="btn" id="openBtn">Open</button></div>
    <div class="scroll" id="emps"></div><div class="pager" id="empPager"></div>
  </section>
</div>

<div id="t-records" class="hide">
  <section class="panel">
    <div class="ctl" style="margin-bottom:10px">
      <input id="rEmp" placeholder="emp_code" class="code" style="width:110px"><input type="date" id="rFrom" aria-label="From"><input type="date" id="rTo" aria-label="To">
      <select id="rStatus"><option value="">Any status</option><option>PRESENT</option><option>WFH</option><option>ON_DUTY</option><option>ABSENT</option><option>LEAVE</option></select>
      <button class="btn" id="rGo">Apply</button><button class="btn ghost" id="rCsv">Export page CSV</button></div>
    <div class="scroll" id="recs"></div><div class="pager" id="recPager"></div>
  </section>
</div>

<div id="t-desk" class="hide"><div class="forms">
  <section class="panel"><h2>New employee</h2>
    <form class="f" id="fEmp">
      <div class="row"><label>emp_code<input name="emp_code" placeholder="EMP0100" required pattern="EMP[0-9]{4,6}"></label><label>Department<input name="department" required></label></div>
      <div class="row"><label>Name<input name="name" required></label><label>Email<input name="email" type="email" required></label></div>
      <div class="row"><label>Shift start<input name="shift_start" value="09:30" pattern="[0-2][0-9]:[0-5][0-9]"></label><label>Shift end<input name="shift_end" value="18:30" pattern="[0-2][0-9]:[0-5][0-9]"></label></div>
      <label>Joined on<input name="joined_on" type="date" required></label><button class="btn">Create</button></form></section>
  <section class="panel"><h2>Punch</h2>
    <form class="f" id="fPunch">
      <div class="row"><label>emp_code<input name="emp_code" required class="code"></label><label>Status (punch-in)<select name="status"><option>PRESENT</option><option>WFH</option><option>ON_DUTY</option></select></label></div>
      <label>Time, IST (empty = now)<input name="at" type="datetime-local"></label>
      <div class="row"><button class="btn" value="in">Punch in</button><button class="btn ghost" value="out">Punch out</button></div></form></section>
  <section class="panel"><h2>Regularize a day</h2>
    <form class="f" id="fReg">
      <div class="row"><label>emp_code<input name="emp_code" required class="code"></label><label>Attendance date<input name="date" type="date" required></label></div>
      <div class="row"><label>Status<select name="status"><option value="">(unchanged)</option><option>PRESENT</option><option>WFH</option><option>ON_DUTY</option><option>ABSENT</option><option>LEAVE</option></select></label><label>Regularized by<input name="by" required value="hr.admin"></label></div>
      <div class="row"><label>Punch in, IST<input name="pin" type="datetime-local"></label><label>Punch out, IST<input name="pout" type="datetime-local"></label></div>
      <label>Reason<input name="reason" required minlength="5"></label><button class="btn">Apply correction</button></form></section>
</div><section class="panel"><h2>Last response</h2><pre id="out">Nothing yet.</pre></section></div>

<div id="t-plans" class="hide">
  <section class="panel"><div class="ctl" style="margin-bottom:10px"><h2 style="margin:0">Do the endpoints use indexes?</h2><button class="btn" id="runPlans">Run explain on all five</button></div>
  <div class="scroll" id="plans"><div class="empty">Runs <span class="code">/admin/explain/…</span> with the month and department currently selected.</div></div></section>
</div>
</main>

<aside id="drawer" aria-label="Employee detail"></aside>
<div id="toast" role="status"></div>

<script>
const $=(s,r=document)=>r.querySelector(s),$$=(s,r=document)=>[...r.querySelectorAll(s)];
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const st={month:null,depts:[],tab:'overview',empPage:1,recPage:1,empRows:[],recRows:[],timer:null};
const tf=new Intl.DateTimeFormat('en-GB',{timeZone:'Asia/Kolkata',hour:'2-digit',minute:'2-digit',hour12:false});
const fT=ms=>ms==null?'—':tf.format(ms);
const n2=v=>v==null?'—':(Math.round(v*100)/100).toString();
const pct=v=>v==null?'—':(v*100).toFixed(1)+'%';
const daysIn=m=>new Date(Date.UTC(+m.slice(0,4),+m.slice(5,7),0)).getUTCDate();
const monthLabel=m=>new Date(Date.UTC(+m.slice(0,4),+m.slice(5,7)-1,1)).toLocaleString('en',{month:'long',year:'numeric',timeZone:'UTC'});
const istMs=v=>v?Date.parse(v.length===16?v+':00+05:30':v+'+05:30'):null;

function toast(msg,err){const t=$('#toast');t.textContent=msg;t.className='show'+(err?' err':'');clearTimeout(toast.h);toast.h=setTimeout(()=>t.className='',3200)}
async function api(path,opt){const r=await fetch(path,opt);let j=null;try{j=await r.json()}catch(e){}
  if(!r.ok){let m=j&&j.detail;if(Array.isArray(m))m=m.map(x=>((x.loc||[]).slice(1).join('.')||'request')+': '+x.msg).join('; ');throw new Error(m||r.statusText)}return j}
const send=(p,b,m='POST')=>api(p,{method:m,headers:{'Content-Type':'application/json'},body:JSON.stringify(b)});
const enc=encodeURIComponent;

/* ---------- navigation ---------- */
function tab(name){st.tab=name;$$('nav [data-tab]').forEach(b=>b.toggleAttribute('aria-current',b.dataset.tab===name));
  $$('main > div[id^=t-]').forEach(d=>d.classList.toggle('hide',d.id!=='t-'+name));
  $('#title').textContent={overview:'Month at a glance',employees:'People',records:'Attendance records',desk:'Front desk',plans:'Query plans'}[name];loadTab()}
$$('nav [data-tab]').forEach(b=>b.onclick=()=>tab(b.dataset.tab));
function loadTab(){return({overview:loadOverview,employees:loadEmps,records:loadRecs,desk:()=>{},plans:()=>{}})[st.tab]().catch(e=>toast(e.message,true))}

/* ---------- overview ---------- */
async function loadOverview(){
  const o=await api('/dashboard/api/overview'+(st.month?`?month=${st.month}`:''));
  st.month=o.month;st.depts=o.departments;$('#month').value=o.month;
  $('#subtitle').textContent=`${monthLabel(o.month)} · ${o.employees} employees · ${o.records.toLocaleString()} records · newest ${o.newest_record||'n/a'}`;
  const T=o.summary.reduce((a,d)=>({h:a.h+d.headcount,p:a.p+d.present_days,l:a.l+d.late_count,m:a.m+d.total_late_minutes,v:a.v+d.leave_count,d:a.d+d.on_duty_count}),{h:0,p:0,l:0,m:0,v:0,d:0});
  $('#kpis').innerHTML=[[T.h,'headcount'],[n2(T.p),'present days'],[n2(o.avg_work_hours),'avg work hours'],[T.l,'late arrivals'],[T.m,'late minutes'],[T.v,'leave days'],[T.d,'on-duty records']].map(([v,l])=>`<div class="kpi"><b class="serif num">${esc(v)}</b><span>${l}</span></div>`).join('');
  const mx=Math.max(1,...o.summary.map(d=>d.present_days));
  $('#depts').innerHTML=o.summary.length?`<table><thead><tr><th>Department</th><th class="r">Head</th><th>Present days</th><th class="r">Avg h</th><th class="r">Late</th><th class="r">Leave</th></tr></thead><tbody>${o.summary.map(d=>`<tr><td>${esc(d.department)}</td><td class="r num">${d.headcount}</td><td><div class="bar"><i style="width:${d.present_days/mx*100}%"></i></div><span class="num">${n2(d.present_days)}</span></td><td class="r num">${n2(d.avg_work_hours)}</td><td class="r num">${d.late_count} <span class="sub">(${d.total_late_minutes}m)</span></td><td class="r num">${d.leave_count}</td></tr>`).join('')}</tbody></table>`:'<div class="empty">No department has headcount in this month.</div>';
  fillSelects(o.departments);
  await Promise.all([renderHeat(),renderBoard(),renderTrend()]);
}
function fillSelects(ds){const opt=ds.map(d=>`<option>${esc(d)}</option>`).join('');
  const keep=id=>$('#'+id).value;const b=keep('boardDept'),t=keep('trendDept'),e=keep('empDept');
  $('#boardDept').innerHTML='<option value="">All departments</option>'+opt;$('#trendDept').innerHTML=opt;$('#empDept').innerHTML='<option value="">All departments</option>'+opt;
  if(ds.includes(b))$('#boardDept').value=b;$('#trendDept').value=ds.includes(t)?t:(ds[0]||'');if(ds.includes(e))$('#empDept').value=e}
async function trendOf(d){const m=st.month;return(await api(`/analytics/departments/${enc(d)}/trend?from=${m}-01&to=${m}-${String(daysIn(m)).padStart(2,'0')}`)).items}
async function renderHeat(){const m=st.month,n=daysIn(m),rows=await Promise.all(st.depts.map(async d=>[d,await trendOf(d)]));
  const h=$('#heat');h.style.gridTemplateColumns=`minmax(90px,max-content) repeat(${n},minmax(14px,1fr))`;
  let html='<div></div>'+Array.from({length:n},(_,i)=>`<div class="dn">${i+1}</div>`).join('');
  for(const [d,items] of rows){html+=`<div class="lab">${esc(d)}</div>`+items.map(x=>x.attendance_rate==null?`<div class="cell off" title="${x.date} · ${x.is_working_day?'no headcount':'weekend'}${x.present_count?' · '+x.present_count+' present':''}"></div>`:`<div class="cell" style="background:color-mix(in srgb,var(--pres) ${Math.round(8+Math.min(1,x.attendance_rate)*92)}%,var(--wash))" title="${x.date} · ${pct(x.attendance_rate)} (${x.present_count}/${x.headcount}) · ${x.late_count} late"></div>`).join('')}
  h.innerHTML=st.depts.length?html:'<div class="empty">No departments yet.</div>'}
async function renderBoard(){const d=$('#boardDept').value;const r=await api(`/analytics/leaderboard/late?month=${st.month}&limit=10${d?'&department='+enc(d):''}`);const mx=Math.max(1,...r.items.map(x=>x.total_late_minutes));
  $('#board').innerHTML=r.items.length?r.items.map(x=>`<li><span class="rk serif num">${x.rank}</span><div><div>${esc(x.name)} <span class="code sub">${esc(x.emp_code)}</span></div><div class="bar late"><i style="width:${x.total_late_minutes/mx*100}%"></i></div></div><div class="num r">${x.total_late_minutes}m<div class="sub">${x.late_count}×</div></div></li>`).join(''):'<li class="empty">Nobody was late. Suspicious.</li>'}
async function renderTrend(){const d=$('#trendDept').value;if(!d){$('#trend').innerHTML='<div class="empty">No department.</div>';return}
  const it=await trendOf(d),W=Math.max(640,it.length*22+60),H=210,L=36,B=24,T=10,pw=W-L-8,ph=H-B-T,bw=pw/it.length;
  const y=v=>T+ph-(Math.min(v,1)*ph);let s=`<svg viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" role="img" aria-label="Attendance trend for ${esc(d)}">`;
  for(const g of [0,.5,1])s+=`<line x1="${L}" x2="${W-8}" y1="${y(g)}" y2="${y(g)}" stroke="var(--line)"/><text x="${L-6}" y="${y(g)+4}" text-anchor="end">${g*100}%</text>`;
  it.forEach((x,i)=>{const cx=L+i*bw;if(x.attendance_rate!=null)s+=`<rect x="${cx+2}" y="${y(x.attendance_rate)}" width="${Math.max(2,bw-4)}" height="${ph-(y(x.attendance_rate)-T)}" fill="var(--pres)" opacity=".38"><title>${x.date}: ${pct(x.attendance_rate)}</title></rect>`;else s+=`<rect x="${cx+2}" y="${T+ph-3}" width="${Math.max(2,bw-4)}" height="3" fill="var(--hatch)"/>`;
    if(i%Math.ceil(it.length/16)===0)s+=`<text x="${cx+bw/2}" y="${H-6}" text-anchor="middle">${x.date.slice(8)}</text>`});
  const pts=it.map((x,i)=>x.moving_avg_7d==null?null:[L+i*bw+bw/2,y(x.moving_avg_7d)]).filter(Boolean);
  if(pts.length)s+=`<polyline fill="none" stroke="var(--acc)" stroke-width="2.2" points="${pts.map(p=>p.join(',')).join(' ')}"/>`+pts.map((p,i)=>`<circle cx="${p[0]}" cy="${p[1]}" r="2.4" fill="var(--acc)"><title>7-day average ${pct(it.filter(q=>q.moving_avg_7d!=null)[i].moving_avg_7d)}</title></circle>`).join('');
  $('#trend').innerHTML=s+'</svg>'}
$('#boardDept').onchange=()=>renderBoard().catch(e=>toast(e.message,true));$('#trendDept').onchange=()=>renderTrend().catch(e=>toast(e.message,true));

/* ---------- employees ---------- */
async function loadEmps(){const d=$('#empDept').value;const r=await api(`/employees?page=${st.empPage}&page_size=12${d?'&department='+enc(d):''}`);st.empRows=r.items;drawEmps();
  const pages=Math.max(1,Math.ceil(r.total/r.page_size));$('#empPager').innerHTML=`<button class="btn ghost" ${r.page<=1?'disabled':''} id="ep">Prev</button><span class="num">Page ${r.page} / ${pages} · ${r.total} people</span><button class="btn ghost" ${r.page>=pages?'disabled':''} id="en">Next</button>`;
  $('#ep').onclick=()=>{st.empPage--;loadTab()};$('#en').onclick=()=>{st.empPage++;loadTab()}}
function drawEmps(){const f=$('#empFilter').value.toLowerCase();const rows=st.empRows.filter(e=>!f||(e.emp_code+e.name+e.department).toLowerCase().includes(f));
  $('#emps').innerHTML=rows.length?`<table><thead><tr><th>Code</th><th>Name</th><th>Department</th><th>Shift</th><th>Joined</th></tr></thead><tbody>${rows.map(e=>`<tr class="click" data-c="${esc(e.emp_code)}"><td class="code">${esc(e.emp_code)}</td><td>${esc(e.name)}</td><td>${esc(e.department)}</td><td class="num">${esc(e.shift_start)}–${esc(e.shift_end)}${e.shift_end<=e.shift_start?' <span class="pill">overnight</span>':''}</td><td class="num">${esc(e.joined_on)}</td></tr>`).join('')}</tbody></table>`:'<div class="empty">No employees match.</div>';
  $$('#emps tr.click').forEach(r=>r.onclick=()=>openEmp(r.dataset.c))}
$('#empFilter').oninput=drawEmps;$('#empDept').onchange=()=>{st.empPage=1;loadTab()};
$('#openBtn').onclick=()=>{const c=$('#openCode').value.trim();if(c)openEmp(c)};

/* ---------- drawer ---------- */
async function openEmp(code){const dr=$('#drawer');dr.innerHTML='<div class="empty">Loading…</div>';dr.classList.add('open');
  try{const m=st.month||new Date().toISOString().slice(0,7),n=daysIn(m);
    const [s,a]=await Promise.all([api(`/analytics/employees/${enc(code)}/monthly?month=${m}`),api(`/attendance?emp_code=${enc(code)}&date_from=${m}-01&date_to=${m}-${String(n).padStart(2,'0')}&page_size=100`)]);
    const by={};a.items.forEach(r=>by[r.date]=r);const lead=new Date(Date.UTC(+m.slice(0,4),+m.slice(5,7)-1,1)).getUTCDay();
    let cal=['M','T','W','T','F','S','S'].map(x=>`<div style="min-height:auto;background:none">${x}</div>`).join('')+'<div style="background:none"></div>'.repeat((lead+6)%7);
    for(let d=1;d<=n;d++){const k=`${m}-${String(d).padStart(2,'0')}`,r=by[k];cal+=`<div class="${r?r.status+(r.late_minutes?' late':'')+(r.half_day?' half':''):''}" title="${r?esc(r.status)+' · in '+fT(r.punch_in)+' out '+fT(r.punch_out)+(r.late_minutes?' · late '+r.late_minutes+'m':''):'no record'}"><b>${d}</b>${r?esc(r.status.slice(0,3)):''}</div>`}
    dr.innerHTML=`<div class="ctl" style="justify-content:space-between"><h2 class="serif" style="font-size:26px;margin:0">${esc(code)}</h2><button class="btn ghost" id="cl">Close</button></div><div class="sub">${monthLabel(m)}</div>
    <div class="kpis" style="margin-top:12px">${[[n2(s.present_days),'present days'],[s.working_days,'working days'],[s.attendance_pct==null?'—':s.attendance_pct+'%','attendance'],[s.late_count,'late'],[s.total_overtime_minutes,'overtime min']].map(([v,l])=>`<div class="kpi"><b class="serif num" style="font-size:22px">${v}</b><span>${l}</span></div>`).join('')}</div>
    <div class="cal">${cal}</div>
    <h2>Records</h2><div class="scroll"><table><thead><tr><th>Date</th><th>Status</th><th>In</th><th>Out</th><th class="r">Hours</th><th class="r">Late</th><th></th></tr></thead><tbody>${a.items.map(r=>`<tr><td class="num">${r.date}</td><td><span class="pill ${r.status}">${r.status}</span>${r.half_day?' <span class="pill">half</span>':''}</td><td class="num">${fT(r.punch_in)}</td><td class="num">${fT(r.punch_out)}</td><td class="r num">${n2(r.work_hours)}</td><td class="r num">${r.late_minutes||''}</td><td>${r.history.length?`<span class="pill" title="${esc(r.history.map(h=>h.by+': '+h.reason).join(' | '))}">${r.history.length} edit</span>`:''}</td></tr>`).join('')||'<tr><td colspan="7" class="empty">No records this month.</td></tr>'}</tbody></table></div>`;
    $('#cl').onclick=()=>dr.classList.remove('open')}catch(e){dr.innerHTML=`<div class="ctl" style="justify-content:space-between"><h2>${esc(code)}</h2><button class="btn ghost" id="cl">Close</button></div><div class="empty">${esc(e.message)}</div>`;$('#cl').onclick=()=>dr.classList.remove('open')}}
document.addEventListener('keydown',e=>{if(e.key==='Escape')$('#drawer').classList.remove('open');if(e.key==='/'&&!/INPUT|SELECT/.test(document.activeElement.tagName)){e.preventDefault();tab('employees');$('#empFilter').focus()}});

/* ---------- records ---------- */
async function loadRecs(){const q=new URLSearchParams({page:st.recPage,page_size:15});for(const [k,id] of [['emp_code','rEmp'],['date_from','rFrom'],['date_to','rTo'],['status','rStatus']]){const v=$('#'+id).value.trim();if(v)q.set(k,v)}
  const r=await api('/attendance?'+q);st.recRows=r.items;
  $('#recs').innerHTML=r.items.length?`<table><thead><tr><th>Date</th><th>Employee</th><th>Status</th><th>In</th><th>Out</th><th class="r">Hours</th><th class="r">Late</th><th class="r">OT</th><th>Flags</th></tr></thead><tbody>${r.items.map((x,i)=>`<tr class="click" data-i="${i}"><td class="num">${x.date}</td><td class="code">${esc(x.emp_code)}</td><td><span class="pill ${x.status}">${x.status}</span></td><td class="num">${fT(x.punch_in)}</td><td class="num">${fT(x.punch_out)}</td><td class="r num">${n2(x.work_hours)}</td><td class="r num">${x.late_minutes||''}</td><td class="r num">${x.overtime_minutes||''}</td><td>${x.half_day?'<span class="pill">half day</span> ':''}${x.history.length?'<span class="pill">edited</span>':''}${x.punch_in&&!x.punch_out?'<span class="pill">open</span>':''}</td></tr>`).join('')}</tbody></table>`:'<div class="empty">No records match these filters.</div>';
  $$('#recs tr.click').forEach(tr=>tr.onclick=()=>{const x=st.recRows[tr.dataset.i],f=$('#fReg');f.emp_code.value=x.emp_code;f.date.value=x.date;tab('desk');toast('Loaded into the correction form')});
  const pages=Math.max(1,Math.ceil(r.total/r.page_size));$('#recPager').innerHTML=`<button class="btn ghost" id="rp" ${r.page<=1?'disabled':''}>Prev</button><span class="num">Page ${r.page} / ${pages} · ${r.total.toLocaleString()} records</span><button class="btn ghost" id="rn" ${r.page>=pages?'disabled':''}>Next</button>`;
  $('#rp').onclick=()=>{st.recPage--;loadTab()};$('#rn').onclick=()=>{st.recPage++;loadTab()}}
$('#rGo').onclick=()=>{st.recPage=1;loadTab()};
$('#rCsv').onclick=()=>{const h=['emp_code','date','status','punch_in_ist','punch_out_ist','work_hours','late_minutes','overtime_minutes','half_day'];
  const csv=[h.join(',')].concat(st.recRows.map(r=>[r.emp_code,r.date,r.status,fT(r.punch_in),fT(r.punch_out),r.work_hours??'',r.late_minutes,r.overtime_minutes,r.half_day].join(','))).join('\n');
  const a=document.createElement('a');a.href=URL.createObjectURL(new Blob([csv],{type:'text/csv'}));a.download='attendance_page.csv';a.click()};

/* ---------- desk ---------- */
const show=(o)=>{$('#out').textContent=JSON.stringify(o,null,2)};
async function run(fn,ok){try{const r=await fn();show(r);toast(ok);return r}catch(e){show({error:e.message});toast(e.message,true)}}
$('#fEmp').onsubmit=e=>{e.preventDefault();const f=Object.fromEntries(new FormData(e.target));run(()=>send('/employees',f),'Employee created')};
$('#fPunch').onsubmit=e=>{e.preventDefault();const f=e.target,out=e.submitter&&e.submitter.value==='out',at=istMs(f.at.value);const b={emp_code:f.emp_code.value.trim()};if(at)b.punched_at=at;if(!out)b.status=f.status.value;
  run(()=>send(out?'/attendance/punch-out':'/attendance/punch-in',b),out?'Punched out':'Punched in')};
$('#fReg').onsubmit=e=>{e.preventDefault();const f=e.target,b={reason:f.reason.value,regularized_by:f.by.value};if(f.status.value)b.status=f.status.value;const a=istMs(f.pin.value),o=istMs(f.pout.value);if(a)b.punch_in=a;if(o)b.punch_out=o;
  run(()=>send(`/attendance/${enc(f.emp_code.value.trim())}/${f.date.value}`,b,'PATCH'),'Record corrected')};

/* ---------- query plans ---------- */
function walk(o,fn){if(o&&typeof o==='object'){fn(o);for(const k in o)walk(o[k],fn)}}
function inspect(ex){const st={stages:new Set(),idx:new Set(),keys:0,docs:0,ms:null};
  walk(ex,o=>{if(typeof o.stage==='string')st.stages.add(o.stage);if(typeof o.indexName==='string')st.idx.add(o.indexName);if(o.totalKeysExamined!=null&&!st.keys&&!st.docs){st.keys=o.totalKeysExamined;st.docs=o.totalDocsExamined;st.ms=o.executionTimeMillis}});return st}
$('#runPlans').onclick=async()=>{const b=$('#runPlans');b.disabled=true;const m=st.month||new Date().toISOString().slice(0,7),d=$('#trendDept').value||st.depts[0]||'',n=String(daysIn(m)).padStart(2,'0');
  const emp=(await api('/employees?page_size=1').catch(()=>({items:[]}))).items[0];const c=emp?emp.emp_code:'EMP0001';
  const jobs=[['attendance_list',`date_from=${m}-01&date_to=${m}-${n}`],['employee_monthly',`emp_code=${c}&month=${m}`],['department_summary',`month=${m}`],['late_leaderboard',`month=${m}`],['department_trend',`department=${enc(d)}&from=${m}-01&to=${m}-${n}`]];
  const rows=[];for(const [k,q] of jobs){try{const r=await api(`/admin/explain/${k}?${q}`);const i=inspect(r.explain);rows.push(`<tr><td class="code">${k}</td><td><span class="pill ${i.stages.has('IXSCAN')?'ok':'no'}">${i.stages.has('IXSCAN')?'IXSCAN':'no IXSCAN'}</span> ${i.stages.has('COLLSCAN')?'<span class="pill no">COLLSCAN</span>':''}</td><td class="code">${esc([...i.idx].join(', ')||'—')}</td><td class="r num">${i.keys}</td><td class="r num">${i.docs}</td><td class="r num">${i.ms??'—'}</td></tr>`)}catch(e){rows.push(`<tr><td class="code">${k}</td><td colspan="5">${esc(e.message)}</td></tr>`)}}
  $('#plans').innerHTML=`<table><thead><tr><th>Endpoint</th><th>Plan</th><th>Index</th><th class="r">Keys</th><th class="r">Docs</th><th class="r">ms</th></tr></thead><tbody>${rows.join('')}</tbody></table>`;b.disabled=false};

/* ---------- chrome ---------- */
$('#month').onchange=e=>{if(e.target.value){st.month=e.target.value;loadTab()}};$('#reload').onclick=()=>loadTab();
$('#dark').onchange=e=>{document.documentElement.dataset.theme=e.target.checked?'dark':'light';try{localStorage.setItem('pc-theme',e.target.checked?'dark':'light')}catch(_){}};
$('#auto').onchange=e=>{clearInterval(st.timer);if(e.target.checked)st.timer=setInterval(()=>{if(!document.hidden)loadTab()},30000)};
try{const t=localStorage.getItem('pc-theme')||(matchMedia('(prefers-color-scheme:dark)').matches?'dark':'light');document.documentElement.dataset.theme=t;$('#dark').checked=t==='dark'}catch(_){}
api('/health').then(()=>$('#foot').textContent='MongoDB: connected').catch(e=>$('#foot').textContent='MongoDB: '+e.message);
loadTab();
</script>
</body>
</html>
"""
