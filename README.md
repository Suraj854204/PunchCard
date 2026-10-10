# Employee Attendance & Analytics API

FastAPI + MongoDB. All application code is in `app/main.py`, including a small operations dashboard.

## Run
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export MONGO_URI="mongodb://localhost:27017" MONGO_DB="attendance_db"   # or put them in .env
uvicorn app.main:app --port 8000
```
MongoDB 6.0+ is required (`$densify`, `$setWindowFields`, `$rank`). Indexes are created at startup.

- API docs: http://localhost:8000/docs
- Dashboard:https://punch-card-chi.vercel.app/dashboard (overview heatmap, department table, late leaderboard, trend chart, employee drawer with a month calendar, records with CSV export, create/punch/regularize forms, live query-plan check, dark mode)

## Try it with data
```bash
python tools/demo_data.py --drop                      
python tools/demo_data.py --drop --employees 1200 --days 90 --start 2026-05-01   
```

## Tests
```bash
pip install -r requirements-dev.txt
pytest tests/test_rules.py        
pytest tests/test_api.py        
```

## Notes
- Instants are epoch milliseconds at the API and BSON dates in MongoDB. Dates, months and shift times are IST strings.
- Analytics read stored derived values and use exact integer arithmetic (half-day = 1 of 2 units, hours in cents) so R8 half-up rounding is exact.
- Beyond the contract: `/dashboard`, `/dashboard/api/overview`. `tools/` and `tests/` are development aids.
- Not run against a live MongoDB in my sandbox (none available): the rule functions are unit-tested; please run `tests/test_api.py` once on your machine.
