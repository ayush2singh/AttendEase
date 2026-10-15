"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB
"""
import os
import calendar
from datetime import datetime, timedelta, date, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

import pymongo
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError

load_dotenv()

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")

# ---------------------------------------------------------------------------
# Ensure indexes on startup
# ---------------------------------------------------------------------------
@app.on_event("startup")
def create_indexes():
    # Unique compound index: one record per employee per attendance date
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)],
        unique=True,
        name="uq_emp_date",
    )
    # Compound index for sorted list queries (date desc, emp_code asc)
    db.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)],
        name="idx_date_emp",
    )
    # Employee lookup for punch-in
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="uq_emp_code")
    # Employees by department + joined_on for analytics
    db.employees.create_index(
        [("department", ASCENDING), ("joined_on", ASCENDING)],
        name="idx_dept_joined",
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _dt_to_ms(dt: Optional[datetime]) -> Optional[int]:
    """Convert a datetime (UTC or aware) to epoch milliseconds, or None."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _ms_to_dt(ms: Optional[int]) -> Optional[datetime]:
    """Convert epoch milliseconds to a UTC-aware datetime, or None."""
    if ms is None:
        return None
    # Truncate to whole seconds (R1)
    ms = (ms // 1000) * 1000
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def _round_half_up(value: float, decimals: int = 2) -> float:
    """Round using ROUND_HALF_UP (not banker's rounding). Fixes R4/R8."""
    exp = Decimal(10) ** -decimals
    return float(Decimal(str(value)).quantize(exp, rounding=ROUND_HALF_UP))


def _is_overnight(shift_start: str, shift_end: str) -> bool:
    """Return True when shift_end <= shift_start (overnight shift)."""
    sh, sm = map(int, shift_start.split(":"))
    eh, em = map(int, shift_end.split(":"))
    return (eh * 60 + em) <= (sh * 60 + sm)


def _shift_start_dt(attendance_date: str, shift_start: str) -> datetime:
    """Return shift-start as an IST-aware datetime anchored to the attendance date."""
    h, m = map(int, shift_start.split(":"))
    d = date.fromisoformat(attendance_date)
    return datetime(d.year, d.month, d.day, h, m, 0, tzinfo=IST)


def _shift_end_dt(attendance_date: str, shift_start: str, shift_end: str) -> datetime:
    """Return shift-end as an IST-aware datetime; adds 1 day for overnight shifts."""
    h, m = map(int, shift_end.split(":"))
    d = date.fromisoformat(attendance_date)
    end = datetime(d.year, d.month, d.day, h, m, 0, tzinfo=IST)
    if _is_overnight(shift_start, shift_end):
        end += timedelta(days=1)
    return end


def _attendance_date_for_punchin(ts_ist: datetime, emp: dict) -> str:
    """
    Determine the attendance date for a punch-in timestamp (R1).
    For overnight shifts, a punch-in whose IST time is earlier than shift_end
    belongs to the shift that started the previous calendar day.
    """
    shift_start = emp["shift_start"]
    shift_end = emp["shift_end"]
    if _is_overnight(shift_start, shift_end):
        eh, em = map(int, shift_end.split(":"))
        shift_end_time = ts_ist.replace(hour=eh, minute=em, second=0, microsecond=0)
        if ts_ist.time() < shift_end_time.time():
            return (ts_ist - timedelta(days=1)).date().isoformat()
    return ts_ist.date().isoformat()


def compute_late_minutes(punch_in_ist: datetime, attendance_date: str, shift_start: str) -> int:
    """
    R2: late only when strictly more than 10:00 after shift_start.
    Returns whole minutes (floor) elapsed since shift_start (not since end of grace).
    Fixes defects #1 and #2 from REVIEW.md.
    """
    start = _shift_start_dt(attendance_date, shift_start)
    elapsed_sec = (punch_in_ist - start).total_seconds()
    if elapsed_sec > 600:  # strictly more than 10 minutes (600 seconds)
        return int(elapsed_sec // 60)
    return 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """
    R4: (punch_out - punch_in) / 3600, rounded to 2 decimals half-up.
    Fixes defects #3 and #4 from REVIEW.md.
    """
    seconds = (punch_out - punch_in).total_seconds()
    return _round_half_up(seconds / 3600, 2)


def compute_overtime(punch_out: datetime, attendance_date: str, shift_start: str, shift_end: str) -> int:
    """
    R3: whole minutes (floor) between shift_end and punch_out, only when >= 30; otherwise 0.
    Handles overnight shifts (shift_end on next day). Fixes defects #5 and #6 from REVIEW.md.
    """
    end = _shift_end_dt(attendance_date, shift_start, shift_end)
    diff_sec = (punch_out - end).total_seconds()
    if diff_sec <= 0:
        return 0
    diff_minutes = int(diff_sec // 60)
    return diff_minutes if diff_minutes >= 30 else 0


def compute_half_day(work_hours: float) -> bool:
    """R5: half_day = True when rounded work_hours < 4.50."""
    return work_hours < 4.50


def _format_record(doc: dict) -> dict:
    """
    Serialize an attendance document to the API response shape.
    Converts BSON dates to epoch milliseconds (R1, defect #14 / #18).
    """
    out = {k: v for k, v in doc.items()}
    out.pop("_id", None)
    out["punch_in"] = _dt_to_ms(doc.get("punch_in"))
    out["punch_out"] = _dt_to_ms(doc.get("punch_out"))
    # history entries: convert at/punch_in/punch_out
    history = []
    for h in doc.get("history", []):
        entry = dict(h)
        entry["at"] = _dt_to_ms(h.get("at"))
        changes = {}
        for field, change in h.get("changes", {}).items():
            if field in ("punch_in", "punch_out"):
                changes[field] = {
                    "from": _dt_to_ms(change.get("from")),
                    "to": _dt_to_ms(change.get("to")),
                }
            else:
                changes[field] = change
        entry["changes"] = changes
        history.append(entry)
    out["history"] = history
    return out


def _format_employee(doc: dict) -> dict:
    out = {k: v for k, v in doc.items()}
    out.pop("_id", None)
    # created_at must be epoch milliseconds (defect #7)
    if isinstance(doc.get("created_at"), datetime):
        out["created_at"] = _dt_to_ms(doc["created_at"])
    return out


def _month_range(month: str):
    """Return (date_from_str, date_to_str) for a 'YYYY-MM' month string."""
    year, mon = map(int, month.split("-"))
    _, last_day = calendar.monthrange(year, mon)
    return f"{month}-01", f"{month}-{last_day:02d}"


def _working_days_in_range(from_date: str, to_date: str, joined_on: Optional[str] = None) -> int:
    """Count Mon-Fri days in [from_date, to_date], optionally starting from joined_on (R7)."""
    start = date.fromisoformat(max(from_date, joined_on) if joined_on else from_date)
    end = date.fromisoformat(to_date)
    count = 0
    current = start
    while current <= end:
        if current.weekday() < 5:  # Mon=0 .. Fri=4
            count += 1
        current += timedelta(days=1)
    return count


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------
PRESENCE_STATUSES = {"PRESENT", "WFH", "ON_DUTY"}
ALL_STATUSES = {"PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"}


class EmployeeIn(BaseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: str = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None


class RegularizeIn(BaseModel):
    status: Optional[str] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    reason: str
    regularized_by: str


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    return {"status": "ok"}


# ---- Employees ----

@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    if body.status if hasattr(body, "status") else None:
        pass  # no status field
    try:
        doc = body.model_dump()
        doc["created_at"] = datetime.now(timezone.utc)
        db.employees.insert_one(doc)
        doc.pop("_id", None)
        return _format_employee(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {}
    if department:
        q["department"] = department
    # Fix defect #8: skip = (page-1)*page_size; fix defect #9: count with filter
    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)
    items = list(db.employees.find(q, {"_id": 0}).skip(skip).limit(page_size))
    items = [_format_employee(e) for e in items]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


# ---- Attendance ----

@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    if body.status not in PRESENCE_STATUSES:
        raise HTTPException(422, f"status must be one of {sorted(PRESENCE_STATUSES)}")

    # Fix defect #10: check employee exists; raise 404 not 500
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    # Fix defect #12: truncate to whole seconds (R1)
    ts_ms = body.punched_at
    if ts_ms is not None:
        ts_ms = (ts_ms // 1000) * 1000
        ts = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
    else:
        # Truncate now() to whole seconds
        now = datetime.now(timezone.utc)
        ts = now.replace(microsecond=0)

    ts_ist = ts.astimezone(IST)

    # Fix defect #11: attendance date per R1 (overnight shift awareness)
    d = _attendance_date_for_punchin(ts_ist, emp)

    late = compute_late_minutes(ts_ist, d, emp["shift_start"])

    doc = {
        "emp_code": body.emp_code,
        "date": d,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    # Fix defect #13: rely on unique index; catch DuplicateKeyError -> 409
    try:
        res = db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "Already punched in for this date")

    doc["id"] = str(res.inserted_id)
    return _format_record(doc)


@app.post("/attendance/punch-out", status_code=200)
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    ts_ms = body.punched_at
    if ts_ms is not None:
        ts_ms = (ts_ms // 1000) * 1000
        ts = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
    else:
        now = datetime.now(timezone.utc)
        ts = now.replace(microsecond=0)

    # Find the most recent open record whose punch_in <= ts
    record = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$lte": ts},
            "punch_out": None,
        },
        sort=[("punch_in", DESCENDING)],
    )
    if not record:
        # Maybe already punched out
        already = db.attendance_logs.find_one(
            {"emp_code": body.emp_code, "punch_out": {"$ne": None}},
            sort=[("punch_in", DESCENDING)],
        )
        if already:
            raise HTTPException(409, "Record is already punched out")
        raise HTTPException(404, "No punch-in record found for this employee")

    punch_in_ts = record["punch_in"]
    if punch_in_ts.tzinfo is None:
        punch_in_ts = punch_in_ts.replace(tzinfo=timezone.utc)

    # Validate: punch_out must be strictly after punch_in and within 24 hours
    if ts <= punch_in_ts:
        raise HTTPException(422, "punched_at must be after punch_in")
    if (ts - punch_in_ts).total_seconds() > 86400:
        raise HTTPException(422, "punched_at is more than 24 hours after punch_in")

    attendance_date = record["date"]
    work_hrs = compute_work_hours(punch_in_ts, ts)
    ot_mins = compute_overtime(ts, attendance_date, emp["shift_start"], emp["shift_end"])
    hd = compute_half_day(work_hrs)

    result = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},  # atomic: only if still open
        {"$set": {
            "punch_out": ts,
            "work_hours": work_hrs,
            "overtime_minutes": ot_mins,
            "half_day": hd,
        }},
        return_document=pymongo.ReturnDocument.AFTER,
    )
    if not result:
        raise HTTPException(409, "Record was already punched out by a concurrent request")

    return _format_record(result)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    # Fix defect #17: validate date range order
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from cannot be greater than date_to")

    q = {}
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

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size

    # Fix defect #15: server-side sort+skip+limit (no in-memory load)
    # Fix defect #16: compound sort date desc, emp_code asc
    cursor = (
        db.attendance_logs.find(q)
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip(skip)
        .limit(page_size)
    )
    items = [_format_record(doc) for doc in cursor]
    # Attach id field
    for i, doc in enumerate(cursor if False else []):
        pass
    # id is added inside _format_record via _id removal; add string id
    raw_cursor = (
        db.attendance_logs.find(q)
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip(skip)
        .limit(page_size)
    )
    items = []
    for doc in raw_cursor:
        rec = _format_record(doc)
        rec["id"] = str(doc["_id"])
        items.append(rec)

    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date_str}")
def regularize_attendance(emp_code: str, date_str: str, body: RegularizeIn):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date_str})
    if not record:
        raise HTTPException(404, "Attendance record not found")

    new_status = body.status if body.status is not None else record["status"]
    new_punch_in_ms = body.punch_in
    new_punch_out_ms = body.punch_out

    # Rule: ABSENT/LEAVE clears both punch times; cannot supply punch times with them
    if new_status in ("ABSENT", "LEAVE"):
        if new_punch_in_ms is not None or new_punch_out_ms is not None:
            raise HTTPException(422, "Cannot supply punch_in or punch_out with ABSENT/LEAVE status")
        new_pi_dt = None
        new_po_dt = None
    else:
        # Presence status: resolve final punch_in and punch_out datetimes
        existing_pi = record.get("punch_in")
        existing_po = record.get("punch_out")
        if existing_pi is not None and existing_pi.tzinfo is None:
            existing_pi = existing_pi.replace(tzinfo=timezone.utc)
        if existing_po is not None and existing_po.tzinfo is None:
            existing_po = existing_po.replace(tzinfo=timezone.utc)

        new_pi_dt = _ms_to_dt(new_punch_in_ms) if new_punch_in_ms is not None else existing_pi
        new_po_dt = _ms_to_dt(new_punch_out_ms) if new_punch_out_ms is not None else existing_po

        if new_pi_dt is None:
            raise HTTPException(422, "A presence status requires punch_in")

        # punch_in must stay on the record's attendance date (R1)
        pi_ist = new_pi_dt.astimezone(IST)
        required_date = _attendance_date_for_punchin(pi_ist, emp)
        if required_date != date_str:
            raise HTTPException(422, "punch_in must remain on the record's attendance date (R1)")

        # punch_out validations
        if new_po_dt is not None:
            if new_po_dt <= new_pi_dt:
                raise HTTPException(422, "punch_out must be after punch_in")
            if (new_po_dt - new_pi_dt).total_seconds() > 86400:
                raise HTTPException(422, "punch_out must be within 24 hours of punch_in")

    # Compute new derived values
    if new_status in ("ABSENT", "LEAVE"):
        new_late = 0
        new_work_hrs = None
        new_ot = 0
        new_hd = False
    else:
        pi_ist = new_pi_dt.astimezone(IST)
        new_late = compute_late_minutes(pi_ist, date_str, emp["shift_start"])
        if new_po_dt is not None:
            new_work_hrs = compute_work_hours(new_pi_dt, new_po_dt)
            new_ot = compute_overtime(new_po_dt, date_str, emp["shift_start"], emp["shift_end"])
            new_hd = compute_half_day(new_work_hrs)
        else:
            new_work_hrs = None
            new_ot = 0
            new_hd = False

    # Build changes dict (only fields that actually changed)
    old_pi = record.get("punch_in")
    old_po = record.get("punch_out")
    if old_pi is not None and old_pi.tzinfo is None:
        old_pi = old_pi.replace(tzinfo=timezone.utc)
    if old_po is not None and old_po.tzinfo is None:
        old_po = old_po.replace(tzinfo=timezone.utc)

    changes = {}
    if new_status != record["status"]:
        changes["status"] = {"from": record["status"], "to": new_status}
    if new_pi_dt != old_pi:
        changes["punch_in"] = {"from": old_pi, "to": new_pi_dt}
    if new_po_dt != old_po:
        changes["punch_out"] = {"from": old_po, "to": new_po_dt}
    if new_late != record.get("late_minutes", 0):
        changes["late_minutes"] = {"from": record.get("late_minutes", 0), "to": new_late}
    if new_work_hrs != record.get("work_hours"):
        changes["work_hours"] = {"from": record.get("work_hours"), "to": new_work_hrs}
    if new_ot != record.get("overtime_minutes", 0):
        changes["overtime_minutes"] = {"from": record.get("overtime_minutes", 0), "to": new_ot}
    if new_hd != record.get("half_day", False):
        changes["half_day"] = {"from": record.get("half_day", False), "to": new_hd}

    if not changes:
        raise HTTPException(422, "Request changes nothing")

    history_entry = {
        "at": datetime.now(timezone.utc),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    update = {
        "$set": {
            "status": new_status,
            "punch_in": new_pi_dt,
            "punch_out": new_po_dt,
            "work_hours": new_work_hrs,
            "late_minutes": new_late,
            "overtime_minutes": new_ot,
            "half_day": new_hd,
        },
        "$push": {"history": history_entry},
    }

    # Optimistic concurrency: match on current history length to prevent silent loss
    history_len = len(record.get("history", []))
    result = db.attendance_logs.find_one_and_update(
        {
            "_id": record["_id"],
            "$expr": {"$eq": [{"$size": {"$ifNull": ["$history", []]}}, history_len]},
        },
        update,
        return_document=pymongo.ReturnDocument.AFTER,
    )
    if not result:
        raise HTTPException(409, "Concurrent edit detected; please retry")

    return _format_record(result)


# ---------------------------------------------------------------------------
# Analytics
# ---------------------------------------------------------------------------

@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    date_from, date_to = _month_range(month)
    joined_on = emp.get("joined_on", date_from)
    working_days = _working_days_in_range(date_from, date_to, joined_on)

    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$gte": date_from, "$lte": date_to}}},
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {
                                "$cond": [
                                    # weekday only (R7)
                                    {
                                        "$in": [
                                            {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
                                            [2, 3, 4, 5, 6],  # Mon=2..Fri=6 in MongoDB
                                        ]
                                    },
                                    {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]},
                                    0,
                                ]
                            },
                            0,
                        ]
                    }
                },
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
            }
        },
    ]

    rows = list(db.attendance_logs.aggregate(pipeline))
    if rows:
        r = rows[0]
        present_days = r["present_days"]
        leave_days = r["leave_days"]
        late_count = r["late_count"]
        total_late = r["total_late_minutes"]
        total_ot = r["total_overtime_minutes"]
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late = 0
        total_ot = 0

    if working_days > 0:
        att_pct = _round_half_up(present_days / working_days * 100, 2)
    else:
        att_pct = None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late,
        "total_overtime_minutes": total_ot,
        "attendance_pct": att_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    date_from, date_to = _month_range(month)

    # Match employees active by end of the month (R9)
    emp_match = {"joined_on": {"$lte": date_to}}
    if department:
        emp_match["department"] = department

    pipeline = [
        {"$match": emp_match},
        # Group employees by department for headcount
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "emp_codes": {"$push": "$emp_code"},
                "joined_ons": {"$push": "$joined_on"},
            }
        },
        # Lookup attendance logs for each department's employees
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"emps": "$emp_codes"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {"$in": ["$emp_code", "$$emps"]},
                            "date": {"$gte": date_from, "$lte": date_to},
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {
            "$group": {
                "_id": "$_id",
                "headcount": {"$first": "$headcount"},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {"$in": [{"$ifNull": ["$logs.status", ""]}, ["PRESENT", "WFH", "ON_DUTY"]]},
                            {
                                "$cond": [
                                    {
                                        "$in": [
                                            {"$dayOfWeek": {"$dateFromString": {"dateString": {"$ifNull": ["$logs.date", "2000-01-01"]}}}},
                                            [2, 3, 4, 5, 6],
                                        ]
                                    },
                                    {"$cond": [{"$eq": ["$logs.half_day", True]}, 0.5, 1]},
                                    0,
                                ]
                            },
                            0,
                        ]
                    }
                },
                "work_hours_vals": {
                    "$push": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": [{"$ifNull": ["$logs.status", ""]}, ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": ["$logs.work_hours", None]},
                                    {"$gt": ["$logs.work_hours", None]},
                                ]
                            },
                            "$logs.work_hours",
                            "$$REMOVE",
                        ]
                    }
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]}
                },
                "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
                "leave_count": {
                    "$sum": {"$cond": [{"$eq": [{"$ifNull": ["$logs.status", ""]}, "LEAVE"]}, 1, 0]}
                },
                "on_duty_count": {
                    "$sum": {"$cond": [{"$eq": [{"$ifNull": ["$logs.status", ""]}, "ON_DUTY"]}, 1, 0]}
                },
            }
        },
        # Compute avg_work_hours
        {
            "$addFields": {
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": [{"$size": "$work_hours_vals"}, 0]},
                        {"$avg": "$work_hours_vals"},
                        None,
                    ]
                }
            }
        },
        {"$sort": {"_id": 1}},
    ]

    rows = list(db.employees.aggregate(pipeline))
    items = []
    for r in rows:
        avg_wh = _round_half_up(r["avg_work_hours"], 2) if r["avg_work_hours"] is not None else None
        items.append({
            "department": r["_id"],
            "headcount": r["headcount"],
            "present_days": r["present_days"],
            "avg_work_hours": avg_wh,
            "late_count": r["late_count"],
            "total_late_minutes": r["total_late_minutes"],
            "leave_count": r["leave_count"],
            "on_duty_count": r["on_duty_count"],
        })

    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    date_from, date_to = _month_range(month)

    # Aggregate totals for employees with late_minutes > 0
    match_logs: dict = {
        "date": {"$gte": date_from, "$lte": date_to},
        "late_minutes": {"$gt": 0},
    }

    pipeline = [
        {"$match": match_logs},
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        # Join with employees to get name/department
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
    ]

    if department:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline += [
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
    ]

    rows = list(db.attendance_logs.aggregate(pipeline))

    # Standard competition ranking (1, 2, 2, 4 ...) and apply limit after ranking
    items = []
    rank = 0
    prev_total = None
    skipped = 0
    for i, r in enumerate(rows):
        total = r["total_late_minutes"]
        if total != prev_total:
            rank = rank + skipped + 1
            skipped = 0
        else:
            skipped += 1
        prev_total = total

        if rank <= limit:
            items.append({
                "rank": rank,
                "emp_code": r["_id"],
                "name": r["emp"]["name"],
                "department": r["emp"]["department"],
                "total_late_minutes": total,
                "late_count": r["late_count"],
            })

    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: str = Query(..., alias="from"),
    to_date: str = Query(..., alias="to"),
):
    # Validate range (R422: to < from or > 92 days)
    if to_date < from_date:
        raise HTTPException(422, "to must be >= from")
    fd = date.fromisoformat(from_date)
    td = date.fromisoformat(to_date)
    if (td - fd).days > 92:
        raise HTTPException(422, "Range cannot exceed 92 days")

    # Check department exists
    dept_count = db.employees.count_documents({"department": department})
    if dept_count == 0:
        raise HTTPException(404, "Department not found")

    # Aggregate attendance logs for this department over the range
    pipeline = [
        {
            "$match": {
                "date": {"$gte": from_date, "$lte": to_date},
            }
        },
        # Join with employees to filter by department
        {
            "$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
        {"$match": {"emp.department": department}},
        {
            "$group": {
                "_id": "$date",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]},
                            0,
                        ]
                    }
                },
                "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
            }
        },
    ]

    log_rows = {r["_id"]: r for r in db.attendance_logs.aggregate(pipeline)}

    # Build employee headcounts per day (R9)
    all_emps = list(db.employees.find({"department": department}, {"emp_code": 1, "joined_on": 1, "_id": 0}))

    # Generate one row per calendar day (gap-filling in Python is acceptable here since $densify may not be available)
    items = []
    rates = []  # for moving_avg_7d
    current = fd
    while current <= td:
        day_str = current.isoformat()
        is_working = current.weekday() < 5

        headcount = sum(1 for e in all_emps if e.get("joined_on", "9999-12-31") <= day_str)

        log = log_rows.get(day_str, {})
        present_count = log.get("present_count", 0)
        late_count = log.get("late_count", 0)

        if is_working and headcount > 0:
            rate = _round_half_up(present_count / headcount, 4)
        else:
            rate = None

        rates.append(rate)

        # moving_avg_7d: mean of non-null rates in the last 7 rows (current + 6 preceding)
        window = [r for r in rates[-7:] if r is not None]
        if window:
            moving_avg = _round_half_up(sum(window) / len(window), 4)
        else:
            moving_avg = None

        items.append({
            "date": day_str,
            "is_working_day": is_working,
            "headcount": headcount,
            "present_count": present_count,
            "late_count": late_count,
            "attendance_rate": rate,
            "moving_avg_7d": moving_avg,
        })

        current += timedelta(days=1)

    return {"department": department, "items": items}


# ---------------------------------------------------------------------------
# Admin: explain
# ---------------------------------------------------------------------------

@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    VALID_ENDPOINTS = {
        "attendance_list", "employee_monthly", "department_summary",
        "late_leaderboard", "department_trend"
    }
    if endpoint not in VALID_ENDPOINTS:
        raise HTTPException(422, f"endpoint must be one of {sorted(VALID_ENDPOINTS)}")

    if endpoint == "attendance_list":
        if date_from and date_to and date_from > date_to:
            raise HTTPException(422, "date_from cannot be greater than date_to")
        q = {}
        if emp_code:
            q["emp_code"] = emp_code
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to
        skip = (page - 1) * page_size
        plan = db.command(
            "explain",
            {
                "find": "attendance_logs",
                "filter": q,
                "sort": {"date": -1, "emp_code": 1},
                "skip": skip,
                "limit": page_size,
            },
            verbosity="executionStats",
        )

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, "emp_code and month are required for employee_monthly")
        date_from_m, date_to_m = _month_range(month)
        pipeline = [
            {"$match": {"emp_code": emp_code, "date": {"$gte": date_from_m, "$lte": date_to_m}}},
            {"$group": {"_id": None, "total": {"$sum": 1}}},
        ]
        plan = db.command("explain", {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(422, "month is required for department_summary")
        date_from_m, date_to_m = _month_range(month)
        emp_match = {"joined_on": {"$lte": date_to_m}}
        if department:
            emp_match["department"] = department
        pipeline = [{"$match": emp_match}, {"$group": {"_id": "$department", "headcount": {"$sum": 1}}}]
        plan = db.command("explain", {"aggregate": "employees", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, "month is required for late_leaderboard")
        date_from_m, date_to_m = _month_range(month)
        pipeline = [
            {"$match": {"date": {"$gte": date_from_m, "$lte": date_to_m}, "late_minutes": {"$gt": 0}}},
            {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}}},
        ]
        plan = db.command("explain", {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")

    elif endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(422, "department, from, and to are required for department_trend")
        if to_date < from_date:
            raise HTTPException(422, "to must be >= from")
        pipeline = [
            {"$match": {"date": {"$gte": from_date, "$lte": to_date}}},
            {"$group": {"_id": "$date", "present_count": {"$sum": 1}}},
        ]
        plan = db.command("explain", {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")

    # MongoDB explain output contains ObjectIds and other non-JSON-serializable types; convert to str
    import json
    def _default(obj):
        return str(obj)
    return JSONResponse(content=json.loads(json.dumps(plan, default=_default)))
