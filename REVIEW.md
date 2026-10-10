# REVIEW.md

Defects found in the starter `app/main.py`. All are fixed in this version.

| # | Where | What is wrong | How you'd notice it | How I fixed it |
|---|---|---|---|---|
| 1 | `MongoClient(...)` | No `tz_aware=True`, so PyMongo returns naive datetimes; mixing them with aware ones breaks arithmetic or silently shifts times. | `TypeError: can't subtract offset-naive and offset-aware datetimes`, or times off by 5h30. | `tz_aware=True`; helper `as_utc()` for anything naive. |
| 2 | startup | No indexes at all. | `/attendance` and analytics slow at 100k docs; explain shows COLLSCAN. | `ensure_indexes()` in the lifespan hook; unique `(emp_code, date)`, unique `emp_code`, plus sort/filter indexes. |
| 3 | `GET /health` | Always 200, never pings Mongo. | Stop MongoDB: still "ok". | Ping; 503 on failure. |
| 4 | `compute_late_minutes` | Uses the punch-in's own timezone/date, not IST; ignores overnight shifts; no whole-second truncation. | A punch at 09:30 IST stored as 04:00 UTC is compared against 09:30 UTC. | `shift_bounds()` builds the IST shift for the attendance date; `trunc_seconds()` first. |
| 5 | `compute_late_minutes` | Floors minutes first, then tests `> 10`. 09:40:01 floors to 10, so it returns 0 (should be 10). | Punch 09:40:01 on a 09:30 shift. | Compare in seconds (`> 600`), then floor. |
| 6 | `compute_work_hours` | Python `round()` is not half-up; no truncation. | 9h + 18s should be 9.01, gives 9.0. | `Decimal` quantize `ROUND_HALF_UP`. |
| 7 | `compute_overtime` | No 30-minute minimum; wrong for overnight shifts (end is next day); never called; `half_day` never computed. | Punch-out 5 min after shift end gives overtime 5. | Rewritten (R3); one `derive()` computes all four fields (R2-R5). |
| 8 | `EmployeeIn` | No validation: emp_code format, email, `HH:MM`, real `joined_on`, lengths, `shift_start != shift_end`. | POST `{"shift_start": "99:99"}` is accepted. | Pydantic constraints + validators, all returning 422. |
| 9 | `create_employee` | Check-then-insert race; two parallel creates both succeed. `created_at` is naive local time and returned as a datetime string, not epoch ms. | 8 parallel POSTs: more than one 201. | Unique index + catch `DuplicateKeyError` -> 409; UTC time, epoch ms. |
| 10 | `list_employees` | `skip = page * page_size` (page 1 skips the first page); `total` ignores the department filter; no sort; no bounds on `page`/`page_size` (negative skip -> 500). | `?page=1` returns page 2; `total` wrong with `department`. | `(page-1)*page_size`, filtered count, sort by `emp_code`, `Query(ge=1, le=100)`. |
| 11 | `PunchInIn` | Any `status` accepted (ABSENT, junk); `punched_at` is a lax int so floats/strings coerce; `if body.punched_at` treats 0 as missing. | `status: "ABSENT"` returns 201. | `Literal` for presence statuses, `StrictInt` with ms range, `is not None`. |
| 12 | `punch_in` | Unknown employee -> `TypeError` -> 500 (should be 404). Uses `fromtimestamp` in server-local time: date is not IST, no overnight rule, no truncation. | Punch for `NOPE`; run the server in UTC. | 404; `attendance_date()` (R1). |
| 13 | `punch_in` | Check-then-insert race, so duplicates under concurrency; leaks an `id` (`_id`); returns raw datetimes. | Parallel identical requests; response has `id`. | Unique index + `DuplicateKeyError` -> 409; `rec_out()` hides `_id`, converts to epoch ms. |
| 14 | `list_attendance` | Loads the whole collection and sorts/paginates in Python; sort lacks the `emp_code` tiebreak; no date/status validation; no `date_from > date_to` 422; leaks `id`; ignores legacy docs missing `history`/`half_day`. | Slow at 100k; `KeyError`-style gaps on legacy records. | Index-backed `find().sort().skip().limit()`, `count_documents`, validation, `rec_out()` defaults. |
| 15 | missing | No punch-out, regularize, analytics or explain. | Contract tests 404. | Implemented (Part B/C). |

## Looked at and judged fine
- `load_dotenv()` defaults to `override=False`, so real environment variables already win over `.env`.
- Plain `def` endpoints with the synchronous PyMongo client run in FastAPI's thread pool; that is correct and safe, mixing sync PyMongo into `async def` would be the bug.
- `doc.pop("_id")` after `insert_one` works, but it mutates the dict; I now build the response through `rec_out()`/`emp_out()` instead.

"""
Employee Attendance & Analytics API  (FastAPI + MongoDB)

Run:   uvicorn app.main:app --port 8000
Env:   MONGO_URI, MONGO_DB   (a local .env is loaded for convenience; real env variables win)
Extra: /dashboard  - a self-contained operations dashboard served by this same file (no external assets).

Everything lives in this one file, in this order:
  1. configuration and Mongo connection
  2. business rules (R1-R10) as small pure functions        <- unit-testable without a database
  3. request models and API helpers
  4. indexes (created at startup, idempotent)
  5. employees, attendance, regularization
  6. analytics (aggregation pipelines) and the explain endpoint
  7. the dashboard
"""
