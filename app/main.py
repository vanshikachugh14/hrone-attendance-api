"""Employee Attendance & Analytics API for the HROne engineering assignment."""
from __future__ import annotations

import calendar
import os
import re
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Optional, Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Path
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")
client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=3000, tz_aware=True)
db = client[MONGO_DB]
employees = db["employees"]
logs = db["attendance_logs"]
IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
PRESENCE = {"PRESENT", "WFH", "ON_DUTY"}
STATUSES = PRESENCE | {"ABSENT", "LEAVE"}
EPOCH_MIN, EPOCH_MAX = 100_000_000_000, 4_102_444_800_000
app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


def now_utc() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def truncate_second(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).replace(microsecond=0)


def epoch_to_dt(value: int | None, default_now: bool = False) -> datetime:
    if value is None:
        if default_now:
            return now_utc()
        raise HTTPException(422, "timestamp is required")
    if type(value) is not int or not EPOCH_MIN <= value <= EPOCH_MAX:
        raise HTTPException(422, "timestamp must be an integer epoch millisecond in the supported range")
    try:
        return datetime.fromtimestamp(value / 1000, UTC).replace(microsecond=0)
    except (ValueError, OverflowError, OSError):
        raise HTTPException(422, "timestamp is out of range")


def dt_to_epoch(value: datetime | None) -> int | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return int(value.timestamp() * 1000)


def round_half_up(value: float | Decimal, places: int = 2) -> float:
    quantum = Decimal("1").scaleb(-places)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def valid_date(value: str, label: str = "date") -> date:
    try:
        parsed = date.fromisoformat(value)
        if parsed.isoformat() != value:
            raise ValueError
        return parsed
    except (TypeError, ValueError):
        raise HTTPException(422, f"{label} must be YYYY-MM-DD")


def month_bounds(month: str) -> tuple[str, str]:
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
        raise HTTPException(422, "month must be YYYY-MM")
    year, mon = map(int, month.split("-"))
    return f"{month}-01", f"{month}-{calendar.monthrange(year, mon)[1]:02d}"


def month_working_days(joined_on: str, month: str) -> int:
    start_s, end_s = month_bounds(month)
    start, end, joined = date.fromisoformat(start_s), date.fromisoformat(end_s), date.fromisoformat(joined_on)
    start = max(start, joined)
    if start > end:
        return 0
    return sum(1 for i in range((end - start).days + 1) if (start + timedelta(days=i)).weekday() < 5)


def shift_datetime(attendance_date: str, shift_time: str, overnight_end: bool = False) -> datetime:
    d = date.fromisoformat(attendance_date) + (timedelta(days=1) if overnight_end else timedelta())
    h, m = map(int, shift_time.split(":"))
    return datetime.combine(d, time(h, m), IST).astimezone(UTC)


def attendance_date_for(punch: datetime, emp: dict) -> str:
    local = punch.astimezone(IST)
    d = local.date()
    start_h, start_m = map(int, emp["shift_start"].split(":"))
    end_h, end_m = map(int, emp["shift_end"].split(":"))
    start_min, end_min = start_h * 60 + start_m, end_h * 60 + end_m
    local_min = local.hour * 60 + local.minute
    if end_min <= start_min and local_min < end_min:
        d -= timedelta(days=1)
    return d.isoformat()


def late_minutes(punch: datetime, attendance_date: str, emp: dict) -> int:
    start = shift_datetime(attendance_date, emp["shift_start"])
    seconds = int((punch - start).total_seconds())
    return seconds // 60 if seconds > 600 else 0


def shift_end_datetime(attendance_date: str, emp: dict) -> datetime:
    sh, sm = map(int, emp["shift_start"].split(":"))
    eh, em = map(int, emp["shift_end"].split(":"))
    overnight = eh * 60 + em <= sh * 60 + sm
    return shift_datetime(attendance_date, emp["shift_end"], overnight_end=overnight)


def derived_values(punch_in: datetime | None, punch_out: datetime | None, attendance_date: str, emp: dict) -> dict:
    if punch_in is None:
        return {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    late = late_minutes(punch_in, attendance_date, emp)
    if punch_out is None:
        return {"work_hours": None, "late_minutes": late, "overtime_minutes": 0, "half_day": False}
    seconds = int((punch_out - punch_in).total_seconds())
    hours = round_half_up(Decimal(seconds) / Decimal(3600), 2)
    over_seconds = int((punch_out - shift_end_datetime(attendance_date, emp)).total_seconds())
    over = over_seconds // 60 if over_seconds >= 1800 else 0
    return {"work_hours": hours, "late_minutes": late, "overtime_minutes": max(0, over), "half_day": hours < 4.50}


def serialize_employee(doc: dict) -> dict:
    return {k: (dt_to_epoch(v) if k == "created_at" else v) for k, v in doc.items() if k != "_id"}


def serialize_record(doc: dict) -> dict:
    result = {k: v for k, v in doc.items() if k != "_id"}
    result.setdefault("history", [])
    result.setdefault("half_day", False)
    result.setdefault("late_minutes", 0)
    result.setdefault("overtime_minutes", 0)
    for key in ("punch_in", "punch_out"):
        result[key] = dt_to_epoch(result.get(key))
    history = []
    for entry in result.get("history", []):
        e = dict(entry)
        e["at"] = dt_to_epoch(e.get("at"))
        changes = {}
        for field, change in e.get("changes", {}).items():
            c = dict(change)
            if field in {"punch_in", "punch_out"}:
                c["from"] = dt_to_epoch(c.get("from"))
                c["to"] = dt_to_epoch(c.get("to"))
            changes[field] = c
        e["changes"] = changes
        history.append(e)
    result["history"] = history
    return result


class EmployeeCreate(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str
    @field_validator("joined_on")
    @classmethod
    def check_joined(cls, v):
        valid_date(v, "joined_on"); return v
    @model_validator(mode="after")
    def check_shift(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: StrictInt | None = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"
    @field_validator("punched_at")
    @classmethod
    def check_ts(cls, v):
        if v is not None and not EPOCH_MIN <= v <= EPOCH_MAX: raise ValueError("invalid epoch milliseconds")
        return v


class PunchOutRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: StrictInt | None = None
    @field_validator("punched_at")
    @classmethod
    def check_ts(cls, v):
        if v is not None and not EPOCH_MIN <= v <= EPOCH_MAX: raise ValueError("invalid epoch milliseconds")
        return v


class RegularizeRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    status: Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"] | None = None
    punch_in: StrictInt | None = None
    punch_out: StrictInt | None = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)
    @field_validator("punch_in", "punch_out")
    @classmethod
    def check_ts(cls, v):
        if v is not None and not EPOCH_MIN <= v <= EPOCH_MAX: raise ValueError("invalid epoch milliseconds")
        return v


@app.on_event("startup")
def ensure_indexes():
    employees.create_index([("emp_code", ASCENDING)], unique=True, name="uq_employee_code")
    employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="department_joined")
    employees.create_index([("joined_on", ASCENDING)], name="joined_on")
    logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uq_attendance_employee_date")
    logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="attendance_date_employee")
    logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING), ("status", ASCENDING)], name="attendance_employee_date_status")
    logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING), ("punch_out", ASCENDING)], name="attendance_open_punch")


@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except PyMongoError:
        raise HTTPException(503, "MongoDB unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    doc = body.model_dump()
    doc["created_at"] = now_utc()
    try:
        employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(department: str | None = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    q = {"department": department} if department is not None else {}
    total = employees.count_documents(q)
    cursor = employees.find(q, {"_id": 0}).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size)
    return {"items": [serialize_employee(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    emp = employees.find_one({"emp_code": body.emp_code})
    if not emp: raise HTTPException(404, "employee not found")
    ts = epoch_to_dt(body.punched_at, default_now=True)
    day = attendance_date_for(ts, emp)
    derived = derived_values(ts, None, day, emp)
    doc = {"emp_code": body.emp_code, "date": day, "status": body.status, "punch_in": ts, "punch_out": None,
           **derived, "overtime_minutes": 0, "half_day": False, "history": []}
    try:
        logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return serialize_record(doc)


def attendance_filter(emp_code=None, date_from=None, date_to=None, status=None):
    q: dict[str, Any] = {}
    if emp_code is not None: q["emp_code"] = emp_code
    if date_from is not None:
        valid_date(date_from, "date_from")
    if date_to is not None:
        valid_date(date_to, "date_to")
    if date_from and date_to and date_from > date_to: raise HTTPException(422, "date_from must be <= date_to")
    if date_from or date_to:
        q["date"] = {}
        if date_from: q["date"]["$gte"] = date_from
        if date_to: q["date"]["$lte"] = date_to
    if status is not None: q["status"] = status
    return q


def list_attendance_query(q, page, page_size):
    total = logs.count_documents(q)
    cur = logs.find(q, {"_id": 0}).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip((page - 1) * page_size).limit(page_size)
    return {"items": [serialize_record(d) for d in cur], "total": total, "page": page, "page_size": page_size}


@app.get("/attendance")
def list_attendance(emp_code: str | None = None, date_from: str | None = None, date_to: str | None = None,
                    status: Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"] | None = None,
                    page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    return list_attendance_query(attendance_filter(emp_code, date_from, date_to, status), page, page_size)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutRequest):
    emp = employees.find_one({"emp_code": body.emp_code})
    if not emp: raise HTTPException(404, "employee not found")
    ts = epoch_to_dt(body.punched_at, default_now=True)
    # Find latest open punch not later than the supplied punch-out instant, including overnight shifts.
    rec = logs.find_one({"emp_code": body.emp_code, "punch_in": {"$lte": ts}, "punch_out": None,
                         "status": {"$in": list(PRESENCE)}}, sort=[("punch_in", DESCENDING)])
    if not rec:
        # Distinguish already-closed matching record from no punch-in.
        closed = logs.find_one({"emp_code": body.emp_code, "punch_in": {"$lte": ts}, "punch_out": {"$ne": None},
                                "status": {"$in": list(PRESENCE)}}, sort=[("punch_in", DESCENDING)])
        if closed: raise HTTPException(409, "record is already punched out")
        raise HTTPException(404, "no punch-in found")
    if ts <= rec["punch_in"] or ts - rec["punch_in"] > timedelta(hours=24): raise HTTPException(422, "punch-out must be after punch-in and within 24 hours")
    derived = derived_values(rec["punch_in"], ts, rec["date"], emp)
    updated = logs.find_one_and_update({"_id": rec["_id"], "punch_out": None}, {"$set": {"punch_out": ts, **derived}}, return_document=ReturnDocument.AFTER)
    if updated is None: raise HTTPException(409, "record is already punched out")
    return serialize_record(updated)


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeRequest):
    valid_date(date)
    emp = employees.find_one({"emp_code": emp_code})
    if not emp: raise HTTPException(404, "employee not found")
    rec = logs.find_one({"emp_code": emp_code, "date": date})
    if not rec: raise HTTPException(404, "attendance record not found")
    old = dict(rec)
    status = body.status if body.status is not None else rec["status"]
    if body.status in {"ABSENT", "LEAVE"} and (body.punch_in is not None or body.punch_out is not None):
        raise HTTPException(422, "ABSENT/LEAVE cannot include punch times")
    if status in {"ABSENT", "LEAVE"}:
        pin = pout = None
    else:
        pin = epoch_to_dt(body.punch_in) if body.punch_in is not None else rec.get("punch_in")
        pout = epoch_to_dt(body.punch_out) if body.punch_out is not None else rec.get("punch_out")
        if pin is None: raise HTTPException(422, "presence status requires punch_in")
        if attendance_date_for(pin, emp) != date: raise HTTPException(422, "punch_in must belong to the record attendance date")
        if pout is not None and (pout <= pin or pout - pin > timedelta(hours=24)): raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")
    derived = derived_values(pin, pout, date, emp)
    final = {"status": status, "punch_in": pin, "punch_out": pout, **derived}
    # Preserve legacy absent derived fields as their contract defaults.
    changes = {}
    for field in ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day"):
        before = old.get(field, False if field == "half_day" else 0 if field in ("late_minutes", "overtime_minutes") else None)
        after = final.get(field, before)
        if before != after: changes[field] = {"from": before, "to": after}
    if not changes: raise HTTPException(422, "regularization does not change anything")
    entry = {"at": now_utc(), "by": body.regularized_by, "reason": body.reason, "changes": changes}
    # Compare-and-swap protects history from lost updates; a losing edit gets 409.
    expected = {"_id": rec["_id"]}
    for field in ("status", "punch_in", "punch_out"):
        expected[field] = rec.get(field)
    updated = logs.find_one_and_update(expected, {"$set": final, "$push": {"history": entry}}, return_document=ReturnDocument.AFTER)
    if updated is None: raise HTTPException(409, "record changed concurrently; retry")
    return serialize_record(updated)


def month_match(month: str):
    start, end = month_bounds(month)
    return {"date": {"$gte": start, "$lte": end}}


def working_expr(date_expr="$_date"):
    # MongoDB dayOfWeek: Sunday=1, Saturday=7.
    return {"$not": [{"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": date_expr}}}, [1, 7]]}]}


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):
    emp = employees.find_one({"emp_code": emp_code}, {"_id": 0})
    if not emp: raise HTTPException(404, "employee not found")
    start, end = month_bounds(month)
    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$gte": start, "$lte": end}}},
        {"$set": {"_is_weekday": {"$not": [{"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, [1, 7]]}]},
                   "_is_present": {"$in": ["$status", list(PRESENCE)]}}},
        {"$group": {"_id": None,
            "present_days": {"$sum": {"$cond": [{"$and": ["$_is_weekday", "$_is_present"]}, {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]}, 0]}},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}}}},
    ]
    agg = list(logs.aggregate(pipeline, hint="attendance_employee_date_status"))
    row = agg[0] if agg else {}
    working = month_working_days(emp["joined_on"], month)
    pct = round_half_up(Decimal(str(row.get("present_days", 0))) / Decimal(working) * 100, 2) if working else None
    return {"emp_code": emp_code, "month": month, "working_days": working,
            "present_days": round_half_up(row.get("present_days", 0), 2), "leave_days": row.get("leave_days", 0),
            "late_count": row.get("late_count", 0), "total_late_minutes": row.get("total_late_minutes", 0),
            "total_overtime_minutes": row.get("total_overtime_minutes", 0), "attendance_pct": pct}


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"), department: str | None = None):
    start, end = month_bounds(month)
    match = {"joined_on": {"$lte": end}}
    if department is not None: match["department"] = department
    pipeline = [
        {"$match": match},
        {"$lookup": {"from": "attendance_logs", "let": {"code": "$emp_code"}, "pipeline": [
            {"$match": {"$expr": {"$and": [{"$eq": ["$emp_code", "$$code"]}, {"$gte": ["$date", start]}, {"$lte": ["$date", end]}]}}},
            {"$set": {"_weekday": {"$not": [{"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, [1, 7]]}]}}}
        ], "as": "month_logs"}},
        {"$unwind": {"path": "$month_logs", "preserveNullAndEmptyArrays": True}},
        {"$group": {"_id": "$department", "headcount": {"$addToSet": "$emp_code"},
            "present_days": {"$sum": {"$cond": [{"$and": [{"$ne": [{"$ifNull": ["$month_logs.emp_code", None]}, None]}, "$month_logs._weekday", {"$in": ["$month_logs.status", list(PRESENCE)]}]}, {"$cond": [{"$eq": ["$month_logs.half_day", True]}, 0.5, 1]}, 0]}},
            "work_hours_sum": {"$sum": {"$cond": [{"$and": [{"$in": ["$month_logs.status", list(PRESENCE)]}, {"$ne": ["$month_logs.work_hours", None]}]}, "$month_logs.work_hours", 0]}},
            "work_hours_count": {"$sum": {"$cond": [{"$and": [{"$in": ["$month_logs.status", list(PRESENCE)]}, {"$ne": ["$month_logs.work_hours", None]}]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$month_logs.late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$month_logs.late_minutes", 0]}},
            "leave_count": {"$sum": {"$cond": [{"$eq": ["$month_logs.status", "LEAVE"]}, 1, 0]}},
            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$month_logs.status", "ON_DUTY"]}, 1, 0]}}}},
        {"$project": {"_id": 0, "department": "$_id", "headcount": {"$size": "$headcount"}, "present_days": 1,
            "avg_work_hours": {"$cond": [{"$gt": ["$work_hours_count", 0]}, {"$divide": [{"$floor": {"$add": [{"$multiply": [{"$divide": ["$work_hours_sum", "$work_hours_count"]}, 100]}, 0.5]}}, 100]}, None]},
            "late_count": 1, "total_late_minutes": 1, "leave_count": 1, "on_duty_count": 1}},
        {"$sort": {"department": 1}}
    ]
    items = list(employees.aggregate(pipeline, hint="department_joined"))
    for x in items: x["present_days"] = round_half_up(x.get("present_days", 0), 2)
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"), limit: int = Query(10, ge=1, le=50), department: str | None = None):
    start, end = month_bounds(month)
    pipeline = [
        {"$match": {**month_match(month), "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"},
    ]
    if department is not None: pipeline.append({"$match": {"employee.department": department}})
    pipeline += [
        {"$set": {"emp_code": "$_id", "name": "$employee.name", "department": "$employee.department"}},
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": 1, "name": 1, "department": 1, "total_late_minutes": 1, "late_count": 1}},
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}}
    ]
    items = list(logs.aggregate(pipeline, hint="attendance_date_employee"))
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_date: str = Query(..., alias="from"), to: str = Query(...)):
    start = valid_date(from_date, "from"); end = valid_date(to, "to")
    if end < start: raise HTTPException(422, "to must be on or after from")
    if (end - start).days + 1 > 92: raise HTTPException(422, "date range cannot exceed 92 days")
    if not employees.find_one({"department": department}): raise HTTPException(404, "department not found")
    # Calendar is generated in MongoDB; lookup pipelines use the attendance compound index.
    pipeline = [
        {"$match": {"department": department}},
        {"$group": {"_id": None, "employees": {"$push": {"emp_code": "$emp_code", "joined_on": "$joined_on"}}}},
        {"$project": {"_id": 0, "days": {"$map": {"input": {"$range": [0, (end - start).days + 1]}, "as": "n", "in": {"$dateToString": {"date": {"$dateAdd": {"startDate": {"$dateFromString": {"dateString": from_date}}, "unit": "day", "amount": "$$n"}}, "format": "%Y-%m-%d"}}}}, "employees": 1}},
        {"$unwind": "$days"},
        {"$set": {"date": "$days", "is_working_day": {"$not": [{"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$days"}}}, [1, 7]]}]},
                   "headcount": {"$size": {"$filter": {"input": "$employees", "as": "e", "cond": {"$lte": ["$$e.joined_on", "$days"]}}}}}},
        {"$lookup": {"from": "attendance_logs", "let": {"d": "$date"}, "pipeline": [
            {"$match": {"$expr": {"$eq": ["$date", "$$d"]}}},
            {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "e"}},
            {"$unwind": "$e"}, {"$match": {"e.department": department}},
            {"$group": {"_id": None, "present_count": {"$sum": {"$cond": [{"$in": ["$status", list(PRESENCE)]}, {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]}, 0]}}, "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}}}}
        ], "as": "daily"}},
        {"$set": {"present_count": {"$ifNull": [{"$arrayElemAt": ["$daily.present_count", 0]}, 0]}, "late_count": {"$ifNull": [{"$arrayElemAt": ["$daily.late_count", 0]}, 0]}}},
        {"$set": {"attendance_rate": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            {"$divide": [{"$floor": {"$add": [{"$multiply": [{"$divide": ["$present_count", "$headcount"]}, 10000]}, 0.5]}}, 10000]}, None
        ]}}},
        {"$setWindowFields": {"sortBy": {"date": 1}, "output": {"moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}}}},
        {"$project": {"_id": 0, "date": 1, "is_working_day": 1, "headcount": 1, "present_count": 1, "late_count": 1, "attendance_rate": 1, "moving_avg_7d": {"$cond": [{"$eq": ["$moving_avg_7d", None]}, None, {"$divide": [{"$floor": {"$add": [{"$multiply": ["$moving_avg_7d", 10000]}, 0.5]}}, 10000]}]}}},
        {"$sort": {"date": 1}}
    ]
    items = list(employees.aggregate(pipeline, hint="department_joined"))
    return {"department": department, "items": items}


def json_safe(value):
    if isinstance(value, datetime): return dt_to_epoch(value)
    if isinstance(value, dict): return {str(k): json_safe(v) for k, v in value.items() if k != "$clusterTime"}
    if isinstance(value, list): return [json_safe(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None: return value
    return str(value)


def explain_find(collection, query, sort, skip=0, limit=20, hint=None):
    cmd = {"find": collection.name, "filter": query, "sort": dict(sort), "skip": skip, "limit": limit}
    if hint: cmd["hint"] = hint
    return db.command("explain", cmd, verbosity="executionStats")


def explain_aggregate(collection, pipeline, hint=None):
    cmd = {"aggregate": collection.name, "pipeline": pipeline, "cursor": {}}
    if hint: cmd["hint"] = hint
    return db.command("explain", cmd, verbosity="executionStats")


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],
                     emp_code: str | None = None, month: str | None = None, department: str | None = None,
                     limit: int = Query(10, ge=1, le=50), date_from: str | None = None, date_to: str | None = None,
                     status: Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"] | None = None,
                     from_date: str | None = Query(None, alias="from"), to: str | None = None,
                     page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    if endpoint == "attendance_list":
        q = attendance_filter(emp_code, date_from, date_to, status)
        exp = explain_find(logs, q, [("date", DESCENDING), ("emp_code", ASCENDING)], (page - 1) * page_size, page_size, "attendance_date_employee")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": json_safe(exp)}
    if endpoint == "employee_monthly":
        if not emp_code or not month: raise HTTPException(422, "emp_code and month are required")
        start, end = month_bounds(month)
        pipeline = [{"$match": {"emp_code": emp_code, "date": {"$gte": start, "$lte": end}}}, {"$group": {"_id": None, "count": {"$sum": 1}}}]
        exp = explain_aggregate(logs, pipeline, "attendance_employee_date_status")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": json_safe(exp)}
    if endpoint == "department_summary":
        if not month: raise HTTPException(422, "month is required")
        start, end = month_bounds(month); q = {"joined_on": {"$lte": end}}
        if department is not None: q["department"] = department
        exp = explain_aggregate(employees, [{"$match": q}, {"$limit": 1}], "department_joined")
        return {"endpoint": endpoint, "collection": "employees", "explain": json_safe(exp)}
    if endpoint == "late_leaderboard":
        if not month: raise HTTPException(422, "month is required")
        exp = explain_aggregate(logs, [{"$match": {**month_match(month), "late_minutes": {"$gt": 0}}}, {"$group": {"_id": "$emp_code", "total": {"$sum": "$late_minutes"}}}], "attendance_date_employee")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": json_safe(exp)}
    if not department or not from_date or not to: raise HTTPException(422, "department, from and to are required")
    valid_date(from_date, "from"); valid_date(to, "to")
    exp = explain_aggregate(employees, [{"$match": {"department": department}}, {"$limit": 1}], "department_joined")
    return {"endpoint": endpoint, "collection": "employees", "explain": json_safe(exp)}
