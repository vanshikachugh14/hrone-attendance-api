# Design decisions

1. **Indexes.** A unique `employees.emp_code` index enforces identity; `(department, joined_on)` and `joined_on` support department and period filters. A unique `(emp_code, date)` index is the attendance idempotency guarantee. Date/employee and employee/date/status indexes support list and monthly queries. I would avoid indexing every derived field without measured query need because each index slows writes and consumes storage.

2. **Punch-in race.** Both requests may pass the employee lookup, but only one insert can satisfy the unique `(emp_code, date)` index. MongoDB accepts one; the other gets `DuplicateKeyError`, translated to 409. No check-then-insert is relied on for uniqueness.

3. **Ties.** `$rank` ranks equal totals equally and skips the next rank (1, 2, 2, 4). Filtering `rank <= limit` happens after ranking, so ties at the cutoff are all returned. Employee code breaks display-order ties, not ranking ties.

4. **Headcount.** The department summary starts from employees joined by month-end, then looks up that month's logs with `preserveNullAndEmptyArrays`. Therefore employees with no logs still contribute to headcount; later joiners are excluded.

5. **100x scale.** I would validate real query plans and cardinalities, partition/archive historical attendance by time if operationally justified, and consider pre-aggregated daily summaries for read-heavy analytics. I would first benchmark the actual workload rather than add speculative indexes or caching.
