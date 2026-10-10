"""Integration tests: need a running MongoDB 6+ (MONGO_URI, default localhost) and `pip install pytest httpx`.
They use their own database (attendance_test) and drop it first.   python -m pytest tests/test_api.py"""
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

os.environ["MONGO_DB"] = os.environ.get("TEST_MONGO_DB", "attendance_test")

import pytest
from fastapi.testclient import TestClient

from app import main
from app.main import IST, LOGS, EMP, UTC


def ms(y, mo, d, h, mi, s=0):
    return int(datetime(y, mo, d, h, mi, s, tzinfo=IST).timestamp()) * 1000


def emp(code, dept, joined="2026-01-01", start="09:30", end="18:30"):
    return {"emp_code": code, "name": f"Name {code}", "email": f"{code}@x.io", "department": dept,
            "shift_start": start, "shift_end": end, "joined_on": joined, "created_at": datetime(2026, 1, 1, tzinfo=UTC)}


def log(code, day, late=0, hours=8.0, status="PRESENT", half=False):
    y, m, d = map(int, day.split("-"))
    return {"emp_code": code, "date": day, "status": status, "punch_in": datetime(y, m, d, 4, 0, tzinfo=UTC),
            "punch_out": datetime(y, m, d, 12, 0, tzinfo=UTC), "work_hours": hours, "late_minutes": late,
            "overtime_minutes": 0, "half_day": half, "history": []}


@pytest.fixture(scope="module")
def c():
    main.client.drop_database(main.db.name)
    with TestClient(main.app) as client:   # runs the lifespan -> creates indexes
        EMP.insert_many([emp(f"EMP900{i}", "QA_T") for i in range(1, 6)] + [
            emp("EMP9006", "OPS_T"), emp("EMP9007", "QA_T", joined="2026-08-01"), emp("EMP9008", "MID_T", joined="2026-07-20")])
        LOGS.insert_many([
            log("EMP9001", "2026-07-06", 40), log("EMP9002", "2026-07-06", 30), log("EMP9003", "2026-07-06", 30),
            log("EMP9004", "2026-07-06", 10), log("EMP9005", "2026-07-11", 15, hours=4.0, half=True),   # a Saturday
            log("EMP9008", "2026-07-20"), log("EMP9008", "2026-07-21", hours=4.0, half=True),
            log("EMP9003", "2026-07-07", 0, status="LEAVE") | {"punch_in": None, "punch_out": None, "work_hours": None}])
        yield client


def test_health_and_indexes(c):
    assert c.get("/health").json() == {"status": "ok"}
    names = {i["name"] for i in LOGS.list_indexes()}
    assert {"uniq_emp_date", "date_emp"} <= names


def test_employee_validation_and_duplicates(c):
    body = {"emp_code": "EMP9100", "name": "Ada", "email": "ada@x.io", "department": "API_T", "joined_on": "2026-01-01"}
    r = c.post("/employees", json=body)
    assert r.status_code == 201 and "_id" not in r.json() and isinstance(r.json()["created_at"], int)
    assert c.post("/employees", json=body).status_code == 409
    assert c.post("/employees", json=body | {"emp_code": "bad"}).status_code == 422
    assert c.post("/employees", json=body | {"emp_code": "EMP9101", "shift_end": "09:30"}).status_code == 422
    assert c.post("/employees", json=body | {"emp_code": "EMP9101", "joined_on": "2026-02-30"}).status_code == 422
    codes = list(range(8))
    with ThreadPoolExecutor(8) as ex:   # concurrent duplicate creates: exactly one 201
        res = sorted(ex.map(lambda _: c.post("/employees", json=body | {"emp_code": "EMP9150"}).status_code, codes))
    assert res == [201] + [409] * 7


def test_employee_list_pagination(c):
    r = c.get("/employees", params={"department": "QA_T", "page": 1, "page_size": 2}).json()
    assert r["total"] == 6 and [e["emp_code"] for e in r["items"]] == ["EMP9001", "EMP9002"]
    assert c.get("/employees", params={"page": 0}).status_code == 422
    assert c.get("/employees", params={"page_size": 101}).status_code == 422


def test_punch_in_out_flow_and_rules(c):
    r = c.post("/attendance/punch-in", json={"emp_code": "EMP9100", "punched_at": ms(2026, 7, 8, 9, 40, 1)})
    assert r.status_code == 201
    j = r.json()
    assert j["date"] == "2026-07-08" and j["late_minutes"] == 10 and j["history"] == [] and "id" not in j
    assert c.post("/attendance/punch-in", json={"emp_code": "EMP9100", "punched_at": ms(2026, 7, 8, 11, 0)}).status_code == 409
    assert c.post("/attendance/punch-in", json={"emp_code": "NOPE"}).status_code == 404
    for bad in ({"punched_at": 1783312500}, {"punched_at": 1.5e12}, {"punched_at": "1783312500000"}, {"status": "ABSENT"}):
        assert c.post("/attendance/punch-in", json={"emp_code": "EMP9100"} | bad).status_code == 422
    out = c.post("/attendance/punch-out", json={"emp_code": "EMP9100", "punched_at": ms(2026, 7, 8, 18, 35)})
    assert out.status_code == 200 and out.json()["work_hours"] == 8.92 and out.json()["overtime_minutes"] == 0
    assert c.post("/attendance/punch-out", json={"emp_code": "EMP9100", "punched_at": ms(2026, 7, 8, 19, 0)}).status_code == 409
    assert c.post("/attendance/punch-out", json={"emp_code": "EMP9150", "punched_at": ms(2026, 7, 8, 19, 0)}).status_code == 404


def test_punch_races(c):
    body = {"emp_code": "EMP9150", "punched_at": ms(2026, 7, 9, 9, 30)}
    with ThreadPoolExecutor(8) as ex:
        res = sorted(ex.map(lambda _: c.post("/attendance/punch-in", json=body).status_code, range(8)))
    assert res == [201] + [409] * 7
    out = {"emp_code": "EMP9150", "punched_at": ms(2026, 7, 9, 18, 30)}
    with ThreadPoolExecutor(8) as ex:
        res = sorted(ex.map(lambda _: c.post("/attendance/punch-out", json=out).status_code, range(8)))
    assert res == [200] + [409] * 7


def test_overnight_punch_across_midnight(c):
    c.post("/employees", json={"emp_code": "EMP9200", "name": "Night", "email": "n@x.io", "department": "NIGHT_T",
                               "shift_start": "22:00", "shift_end": "06:00", "joined_on": "2026-01-01"})
    a = c.post("/attendance/punch-in", json={"emp_code": "EMP9200", "punched_at": ms(2026, 7, 6, 21, 55)}).json()
    b = c.post("/attendance/punch-out", json={"emp_code": "EMP9200", "punched_at": ms(2026, 7, 7, 6, 40)}).json()
    assert a["date"] == b["date"] == "2026-07-06" and b["work_hours"] == 8.75 and b["overtime_minutes"] == 40
    early = c.post("/attendance/punch-in", json={"emp_code": "EMP9200", "punched_at": ms(2026, 7, 8, 5, 30)}).json()
    assert early["date"] == "2026-07-07"       # before shift_end -> belongs to the previous day's shift


def test_regularize(c):
    url = "/attendance/EMP9100/2026-07-08"
    ok = c.patch(url, json={"punch_in": ms(2026, 7, 8, 9, 20), "reason": "biometric glitch", "regularized_by": "hr", "work_hours": 99})
    j = ok.json()
    assert ok.status_code == 200 and j["late_minutes"] == 0 and len(j["history"]) == 1
    assert set(j["history"][0]["changes"]) == {"punch_in", "late_minutes", "work_hours"}
    assert j["history"][0]["changes"]["punch_in"]["to"] == ms(2026, 7, 8, 9, 20)
    same = {"punch_in": ms(2026, 7, 8, 9, 20), "reason": "again again", "regularized_by": "hr"}
    assert c.patch(url, json=same).status_code == 422                       # changes nothing
    assert c.patch(url, json={"status": "ABSENT", "punch_in": ms(2026, 7, 8, 9, 0), "reason": "x" * 6, "regularized_by": "hr"}).status_code == 422
    assert c.patch(url, json={"punch_in": ms(2026, 7, 9, 9, 0), "reason": "wrong day", "regularized_by": "hr"}).status_code == 422
    assert c.patch("/attendance/EMP9100/2026-01-01", json=same).status_code == 404
    gone = c.patch(url, json={"status": "LEAVE", "reason": "approved leave", "regularized_by": "hr"}).json()
    assert gone["punch_in"] is None and gone["work_hours"] is None and len(gone["history"]) == 2


def test_list_attendance(c):
    r = c.get("/attendance", params={"date_from": "2026-07-06", "date_to": "2026-07-06", "page_size": 2}).json()
    # 4 seeded records + EMP9200's overnight record created by the earlier test = 5
    assert r["total"] == 5 and [i["emp_code"] for i in r["items"]] == ["EMP9001", "EMP9002"]
    assert c.get("/attendance", params={"date_from": "2026-07-07", "date_to": "2026-07-06"}).status_code == 422
    assert c.get("/attendance", params={"status": "NOPE"}).status_code == 422
    assert c.get("/attendance", params={"date_from": "2026-13-01"}).status_code == 422


def test_employee_monthly(c):
    r = c.get("/analytics/employees/EMP9008/monthly", params={"month": "2026-07"}).json()
    assert (r["working_days"], r["present_days"], r["attendance_pct"]) == (10, 1.5, 15.0)
    r = c.get("/analytics/employees/EMP9003/monthly", params={"month": "2026-07"}).json()
    assert (r["leave_days"], r["late_count"], r["total_late_minutes"]) == (1, 1, 30)
    assert c.get("/analytics/employees/EMP9007/monthly", params={"month": "2026-07"}).json()["attendance_pct"] is None
    assert c.get("/analytics/employees/NOPE/monthly", params={"month": "2026-07"}).status_code == 404
    assert c.get("/analytics/employees/EMP9001/monthly", params={"month": "2026-7"}).status_code == 422


def test_department_summary_headcount_and_weekend(c):
    items = {i["department"]: i for i in c.get("/analytics/departments/summary", params={"month": "2026-07"}).json()["items"]}
    qa, ops = items["QA_T"], items["OPS_T"]
    assert qa["headcount"] == 5                      # EMP9007 joins in August
    assert qa["present_days"] == 4.0                 # Saturday record is not a working-day presence
    assert (qa["late_count"], qa["total_late_minutes"], qa["leave_count"]) == (5, 125, 1)
    assert qa["avg_work_hours"] == 7.2               # (4*8 + 4) / 5 records, not an average of averages
    assert ops == {"department": "OPS_T", "headcount": 1, "present_days": 0.0, "avg_work_hours": None,
                   "late_count": 0, "total_late_minutes": 0, "leave_count": 0, "on_duty_count": 0}
    assert "FUTURE_T" not in items


def test_leaderboard_ties(c):
    rows = c.get("/analytics/leaderboard/late", params={"month": "2026-07", "limit": 2, "department": "QA_T"}).json()["items"]
    assert [(r["rank"], r["emp_code"]) for r in rows] == [(1, "EMP9001"), (2, "EMP9002"), (2, "EMP9003")]
    rows = c.get("/analytics/leaderboard/late", params={"month": "2026-07", "limit": 50, "department": "QA_T"}).json()["items"]
    assert [r["rank"] for r in rows] == [1, 2, 2, 4, 5]


def test_trend_gap_filling_and_moving_average(c):
    r = c.get("/analytics/departments/QA_T/trend", params={"from": "2026-07-06", "to": "2026-07-12"}).json()["items"]
    assert [x["date"] for x in r] == [f"2026-07-{d:02d}" for d in range(6, 13)]
    assert [x["attendance_rate"] for x in r] == [0.8, 0.0, 0.0, 0.0, 0.0, None, None]
    assert [x["moving_avg_7d"] for x in r] == [0.8, 0.4, 0.2667, 0.2, 0.16, 0.16, 0.16]
    assert r[5]["present_count"] == 0.5 and r[5]["is_working_day"] is False and r[0]["headcount"] == 5
    mid = c.get("/analytics/departments/MID_T/trend", params={"from": "2026-07-17", "to": "2026-07-20"}).json()["items"]
    assert [x["headcount"] for x in mid] == [0, 0, 0, 1] and [x["attendance_rate"] for x in mid] == [None, None, None, 1.0]
    assert c.get("/analytics/departments/NOPE/trend", params={"from": "2026-07-01", "to": "2026-07-02"}).status_code == 404
    assert c.get("/analytics/departments/QA_T/trend", params={"from": "2026-07-02", "to": "2026-07-01"}).status_code == 422
    assert c.get("/analytics/departments/QA_T/trend", params={"from": "2026-01-01", "to": "2026-06-01"}).status_code == 422


@pytest.mark.parametrize("endpoint,params", [
    ("attendance_list", {"date_from": "2026-07-01", "date_to": "2026-07-31"}),
    ("attendance_list", {"emp_code": "EMP9001"}),
    ("employee_monthly", {"emp_code": "EMP9001", "month": "2026-07"}),
    ("department_summary", {"month": "2026-07"}),
    ("late_leaderboard", {"month": "2026-07"}),
    ("department_trend", {"department": "QA_T", "from": "2026-07-01", "to": "2026-07-31"}),
])
def test_explain_uses_indexes(c, endpoint, params):
    r = c.get(f"/admin/explain/{endpoint}", params=params)
    assert r.status_code == 200
    text = str(r.json()["explain"])
    assert "IXSCAN" in text and "COLLSCAN" not in text


def test_explain_validation(c):
    assert c.get("/admin/explain/employee_monthly").status_code == 422
    assert c.get("/admin/explain/nonsense").status_code == 422


def test_dashboard_served(c):
    assert "Punchcard" in c.get("/dashboard").text
    assert c.get("/dashboard/api/overview", params={"month": "2026-07"}).json()["month"] == "2026-07"