# Starter code review

| # | Where | What is wrong | How to notice | Fix |
|---|---|---|---|---|
| 1 | `health` | Returns healthy without contacting MongoDB. | Stop MongoDB and call `/health`; it still returns 200. | Ping MongoDB and return 503 on connection failure. |
| 2 | `create_employee` | Duplicate check then insert is racy; no database uniqueness guarantee. | Send concurrent creates with the same `emp_code`. | Unique `emp_code` index and translate `DuplicateKeyError` to 409. |
| 3 | `create_employee` | `created_at` is a naive local datetime, not a UTC instant. | Inspect BSON timezone and epoch response around non-UTC hosts. | Store UTC datetimes and serialize as epoch milliseconds. |
| 4 | `list_employees` | `skip` is `page * page_size`, skipping page 1. | Page 1 omits the first page of employees. | Use `(page - 1) * page_size`. |
| 5 | `list_employees` | `total` ignores the department filter. | Filter by department and compare `total` with matching records. | Count with the same filter used for retrieval. |
| 6 | employee request model | No contract validation for employee code, email, dates, shift times, or equal shift boundaries. | Submit malformed codes/dates/times. | Add Pydantic constraints and calendar validation. |
| 7 | `punch_in` | Missing employee is dereferenced; returns 500 rather than 404. | Punch in with an unknown code. | Check employee existence first. |
| 8 | `punch_in` | `if body.punched_at` treats zero as omitted; seconds, floats and strings are not correctly rejected. | Send `0`, a seconds timestamp, or a float. | Strict integer and epoch-millisecond range validation; default only for `None`. |
| 9 | `punch_in` | Uses host-local time rather than UTC/IST-aware conversion; mishandles overnight attendance dates. | Punch after midnight on an overnight shift or run under a different host timezone. | Convert instants to IST for attendance date and store UTC instants. |
| 10 | `punch_in` | Check-then-insert permits duplicate records under concurrent requests. | Send simultaneous punches for one employee/date. | Unique compound `(emp_code, date)` index and handle duplicate-key conflicts. |
| 11 | `punch_in` | Status isn't restricted to presence statuses and returned record includes an ID field. | Submit `ABSENT`, or inspect response fields. | Validate status and never expose MongoDB IDs. |
| 12 | `compute_late_minutes` | Uses timestamp's calendar date rather than the attendance date and does not truncate the instant before calculation. | Overnight shift or sub-second timestamps near the grace boundary. | Use attendance-day shift start and truncate to whole seconds. |
| 13 | `compute_work_hours` | Python `round` uses bankers' rounding, not decimal half-up. | Test values exactly halfway at two decimals. | Use `Decimal` with `ROUND_HALF_UP`. |
| 14 | `compute_overtime` | Always places shift end on the attendance date and counts any positive overtime. | Overnight shift or punch-out less than 30 minutes after shift end. | Move overnight shift end to next day and enforce the 30-minute minimum. |
| 15 | `list_attendance` | Loads every matching record into memory, sorts in Python, and paginates afterward. | Run against 100k records; memory and latency grow with all matches. | Database sort, count, skip and limit with indexes. |
| 16 | `list_attendance` | Sort order omits `emp_code` tie-breaker and response leaks `_id` as `id`. | Multiple employees share a date; inspect returned schema. | Sort date descending/employee ascending and exclude internal IDs. |
| 17 | `list_attendance` | `total` requires fetching all documents and date range order is not validated. | Reverse `date_from`/`date_to`. | Count query with applied filters and return 422 for reversed ranges. |
| 18 | Application setup | No indexes are created at startup. | Inspect indexes on a clean seeded database. | Create idempotent unique and query-supporting indexes at startup. |
| 19 | Missing endpoints | Punch-out, regularization, analytics, and explain endpoints are absent. | Request any contract endpoint marked for implementation. | Implement missing routes with validation, atomic updates, and aggregation pipelines. |

**Checked and retained:** `load_dotenv()` is useful for local development, provided real environment variables take precedence. The default database name is a reasonable local fallback. The API contract requires all returned instants to be epoch milliseconds and all IDs to remain internal.
