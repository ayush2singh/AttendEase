# DECISIONS.md

1. **Indexes.** I created three indexes. A unique compound index on `{emp_code, date}` in `attendance_logs` enforces one record per employee per attendance day at the database level and powers the 409 duplicate punch-in rejection. A non-unique compound index on `{date, emp_code}` supports the `GET /attendance` list query with its required `date` descending / `emp_code` ascending sort without a collection scan. A single-field index on `emp_code` in `employees` speeds up the frequent employee lookup on every punch-in. I considered a partial index on `{emp_code, date}` filtered to presence statuses, but rejected it because `ABSENT` and `LEAVE` records must also be unique per employee per day.

2. **Punch-in race.** Both requests pass the application-level `find_one` check at nearly the same instant. Both attempt `insert_one`. MongoDB's unique index allows exactly one insert; the second raises `DuplicateKeyError`. The handler catches that exception and returns HTTP 409, so exactly one record is created.

3. **Ties.** The leaderboard query sorts by `total_late_minutes` descending, then `emp_code` ascending as a stable tiebreaker. When two employees share the same total at the `limit` boundary, the one whose `emp_code` is lexicographically smaller appears within the page and the other is on the next page — deterministic and reproducible across calls.

4. **Headcount.** The department summary uses a `$lookup` from `employees` into `attendance_logs` (left outer join), so employees with zero logs still appear. The `$match` on `joined_on <= last_day_of_month` is applied to the employees side before the join, satisfying R9 without requiring any attendance record to exist.

5. **One thing I'd change at 100x scale.** I would pre-aggregate daily analytics into a summary collection via a change stream or a nightly batch job, so the analytics endpoints read from small pre-computed documents instead of aggregating millions of raw logs on every request.
