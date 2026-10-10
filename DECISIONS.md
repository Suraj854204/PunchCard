# DECISIONS.md

1. **Indexes.** Unique `(emp_code, date)` makes duplicate punch-ins impossible and serves per-employee queries and the monthly summary. `(date desc, emp_code)` gives the list its sort and the month-range scans for summary and leaderboard. A `status` version serves status filters; `(emp_code, punch_in desc)` finds the record to punch out. On employees: unique `emp_code`, and department/joined_on indexes for headcount. I rejected a text index on names: nothing searches by name.

2. **Punch-in race.** Both requests compute the same date and try `insert_one`. The unique index lets exactly one insert win; the other raises `DuplicateKeyError`, which I turn into 409. No read happens before the insert, so there is no gap to race through.

3. **Ties.** `$rank` gives 1, 2, 2, 4. I filter `rank <= limit` instead of `$limit`, so both tied people at the cutoff are returned and the list can be longer than `limit`. Ties are ordered by `emp_code`.

4. **Headcount.** It never comes from the logs. A `$unionWith` counts employees with `joined_on <=` month end straight from the employees collection, then merges that with the log totals per department.

5. **At 100x data.** I would pre-aggregate daily department totals into a summary collection, updated on each punch, so analytics stop scanning raw logs.
