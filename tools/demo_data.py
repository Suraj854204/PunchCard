"""Generate demo / load-test data into the database named by MONGO_DB (uses the same rules as the API).

  python tools/demo_data.py --drop                       # ~60 people, July 2026: good for the dashboard
  python tools/demo_data.py --drop --employees 1200 --days 90 --start 2026-05-01   # ~100k records for index/speed checks

Deliberately includes the awkward cases: tied late totals, weekend records, overnight shifts, mid-month joiners,
employees with no logs at all, ABSENT / LEAVE rows, half days, open (not punched-out) records and one corrected record.
This script is a dev tool, it is not part of the graded submission.
"""
import argparse
import random
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.main import EMP, IST, LOGS, UTC, attendance_date, derive, ensure_indexes  # noqa: E402

DEPTS = ["Engineering", "Sales", "Support", "Finance", "People Ops", "Design"]
FIRST = "Asha Vikram Neha Rohit Meera Karan Isha Dev Tara Zoya Arjun Kabir Sana Ravi Nisha Omar Leela Hari".split()
LAST = "Rao Shah Iyer Das Nair Bedi Khan Mehta Joshi Menon Gill Sethi Pillai Bose Kulkarni".split()


def ist_dt(day: date, h: int, m: int, s: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, h, m, s, tzinfo=IST).astimezone(UTC)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--employees", type=int, default=60)
    ap.add_argument("--days", type=int, default=31)
    ap.add_argument("--start", default="2026-07-01")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--drop", action="store_true", help="empty both collections first")
    a = ap.parse_args()
    rnd = random.Random(a.seed)
    start = date.fromisoformat(a.start)
    if a.drop:
        EMP.delete_many({})
        LOGS.delete_many({})
    ensure_indexes()

    emps = []
    for i in range(1, a.employees + 1):
        overnight = rnd.random() < 0.04
        joined = start - timedelta(days=rnd.randint(30, 700))
        if i % 17 == 0:
            joined = start + timedelta(days=rnd.randint(3, max(4, a.days - 3)))   # mid-period joiner
        emps.append({
            "emp_code": f"EMP{i:04d}", "name": f"{rnd.choice(FIRST)} {rnd.choice(LAST)}",
            "email": f"user{i}@example.com", "department": DEPTS[i % len(DEPTS)],
            "shift_start": "22:00" if overnight else rnd.choice(["09:00", "09:30", "10:00"]),
            "shift_end": "06:00" if overnight else rnd.choice(["18:00", "18:30", "19:00"]),
            "joined_on": joined.isoformat(), "created_at": datetime.now(UTC)})
    EMP.insert_many(emps)
    no_logs = {e["emp_code"] for e in emps[-5:]}          # the last five people never get a record

    batch, total = [], 0
    for e in emps:
        if e["emp_code"] in no_logs:
            continue
        night = e["shift_end"] <= e["shift_start"]
        sh, sm = map(int, e["shift_start"].split(":"))
        eh, em = map(int, e["shift_end"].split(":"))
        habit = rnd.choice([0.0, 0.0, 0.1, 0.25, 0.5])      # per-person chance of being late
        for k in range(a.days):
            day = start + timedelta(days=k)
            if day.isoformat() < e["joined_on"]:
                continue
            weekend = day.weekday() >= 5
            roll = rnd.random()
            if weekend and roll > 0.03:
                continue
            if not weekend and roll > 0.96:
                continue                                      # a weekday with no record at all
            doc = {"emp_code": e["emp_code"], "date": day.isoformat(), "history": []}
            kind = rnd.random()
            if not weekend and kind < 0.04:
                doc.update(status="ABSENT", punch_in=None, punch_out=None)
                doc.update(derive(e["shift_start"], e["shift_end"], doc["date"], None, None))
            elif not weekend and kind < 0.08:
                doc.update(status="LEAVE", punch_in=None, punch_out=None)
                doc.update(derive(e["shift_start"], e["shift_end"], doc["date"], None, None))
            else:
                doc["status"] = rnd.choices(["PRESENT", "WFH", "ON_DUTY"], [78, 16, 6])[0]
                offset = rnd.randint(11, 95) * 60 + rnd.randint(0, 59) if rnd.random() < habit else rnd.randint(-25, 10) * 60
                start_dt = datetime(day.year, day.month, day.day, sh, sm, tzinfo=IST)
                pin = (start_dt + timedelta(seconds=offset)).astimezone(UTC)
                doc["date"] = attendance_date(pin, e["shift_start"], e["shift_end"])
                span = rnd.uniform(3.0, 4.4) if rnd.random() < 0.07 else rnd.uniform(8.0, 10.6)
                pout = pin + timedelta(seconds=int(span * 3600))
                open_rec = day == start + timedelta(days=a.days - 1) and rnd.random() < 0.3
                doc.update(punch_in=pin, punch_out=None if open_rec else pout)
                doc.update(derive(e["shift_start"], e["shift_end"], doc["date"], pin, None if open_rec else pout))
            batch.append(doc)
            if len(batch) >= 5000:
                LOGS.insert_many(batch, ordered=False)
                total += len(batch)
                batch = []
    if batch:
        LOGS.insert_many(batch, ordered=False)
        total += len(batch)
    print(f"database '{LOGS.database.name}': {len(emps)} employees ({len(no_logs)} with no logs), {total} attendance records")


if __name__ == "__main__":
    main()
