"""Pure unit tests for the business rules (no MongoDB needed):  pytest tests/test_rules.py"""
from datetime import datetime, timezone

from app.main import (IST, attendance_date, compute_late_minutes, compute_overtime, compute_work_hours,
                      derive, dt_to_ms, ms_to_dt, working_days)


def ist(y, mo, d, h, mi, s=0, us=0):
    return datetime(y, mo, d, h, mi, s, us, tzinfo=IST)


def test_late_boundaries_r2():
    day = "2026-07-06"
    late = lambda h, m, s: compute_late_minutes(ist(2026, 7, 6, h, m, s), day, "09:30", "18:30")
    assert late(9, 40, 0) == 0          # exactly 10:00 after start is still inside the grace period
    assert late(9, 40, 1) == 10         # one second more: late, counted from shift start
    assert late(10, 15, 59) == 45
    assert late(9, 0, 0) == 0           # early
    assert compute_late_minutes(ist(2026, 7, 6, 9, 40, 0, 900_000), day, "09:30", "18:30") == 0  # truncated to 09:40:00


def test_overtime_r3():
    ot = lambda h, m: compute_overtime(ist(2026, 7, 6, h, m), "2026-07-06", "09:30", "18:30")
    assert ot(18, 59) == 0 and ot(19, 0) == 30 and ot(19, 10) == 40 and ot(17, 0) == 0


def test_work_hours_half_up_r4():
    t0 = ist(2026, 7, 6, 9, 0)
    assert compute_work_hours(t0, ist(2026, 7, 6, 18, 0, 18)) == 9.01     # 9.005 -> 9.01 (banker's would give 9.0)
    assert compute_work_hours(t0, ist(2026, 7, 6, 12, 30)) == 3.5
    assert compute_work_hours(ist(2026, 7, 6, 9, 28), ist(2026, 7, 6, 18, 35)) == 9.12


def test_half_day_uses_rounded_hours_r5():
    kw = dict(shift_start="09:30", shift_end="18:30", date_str="2026-07-06")
    t0 = ist(2026, 7, 6, 9, 0)
    assert derive(punch_in=t0, punch_out=ist(2026, 7, 6, 13, 29), **kw)["half_day"] is True      # 4.48
    assert derive(punch_in=t0, punch_out=ist(2026, 7, 6, 13, 29, 42), **kw)["half_day"] is False  # 4.495 -> 4.50
    assert derive(punch_in=t0, punch_out=ist(2026, 7, 6, 13, 30), **kw)["half_day"] is False
    assert derive(punch_in=None, punch_out=None, **kw) == {"late_minutes": 0, "work_hours": None, "overtime_minutes": 0, "half_day": False}


def test_overnight_attendance_date_r1():
    d = lambda *a: attendance_date(ist(*a), "22:00", "06:00")
    assert d(2026, 7, 6, 21, 55) == "2026-07-06"
    assert d(2026, 7, 7, 5, 59, 59) == "2026-07-06"   # before shift_end -> previous day's shift
    assert d(2026, 7, 7, 6, 0) == "2026-07-07"
    assert attendance_date(ist(2026, 7, 7, 5, 0), "09:30", "18:30") == "2026-07-07"  # day shifts are unaffected


def test_overnight_derived_values_match_sample_7():
    pin, pout = ist(2026, 7, 6, 21, 55), ist(2026, 7, 7, 6, 40)
    r = derive("22:00", "06:00", "2026-07-06", pin, pout)
    assert r == {"late_minutes": 0, "work_hours": 8.75, "overtime_minutes": 40, "half_day": False}


def test_sample_documents():
    r = derive("09:30", "18:30", "2026-07-06", ist(2026, 7, 6, 10, 5), ist(2026, 7, 6, 19, 10))
    assert (r["late_minutes"], r["overtime_minutes"], r["work_hours"]) == (35, 40, 9.08)


def test_epoch_ms_roundtrip_and_utc():
    ms = 1783312500123
    dt = ms_to_dt(ms)
    assert dt.tzinfo is not None and dt.microsecond == 0 and dt_to_ms(dt) == 1783312500000
    assert dt_to_ms(datetime(2026, 7, 6, 4, 35, tzinfo=timezone.utc)) == 1783312500000


def test_working_days_r7():
    assert working_days("2026-07", "2026-01-05") == 23
    assert working_days("2026-07", "2026-07-13") == 15      # mid-month joiner counted from joined_on
    assert working_days("2026-07", "2026-08-01") == 0       # joins after the month
