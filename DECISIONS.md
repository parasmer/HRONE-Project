# DECISIONS.md

1. **Indexes.** Unique `employees(emp_code)` and unique `attendance_logs(emp_code, date)` give the 409s. `(date desc, emp_code)` serves the list sort, date ranges and the leaderboard. `(status, date, emp_code)` serves status filters. `(emp_code, punch_in desc)` finds the record to punch out. `employees(departArchitecture & Design Decisions
1. Indexing Strategy
To prevent duplicate records, I added unique indexes on employees(emp_code) and attendance_logs(emp_code, date). These ensure database-level uniqueness and trigger a clear 409 Conflict when duplicates occur.

For queries, I prioritized indexing based on actual access patterns:

(date desc, emp_code): Optimizes the default sorted list view, date range filters, and leaderboard queries.

(status, date, emp_code): Speeds up status-based filtering.

(emp_code, punch_in desc): Rapidly locates the most recent entry needed during a punch-out request.

employees(department, emp_code, joined_on): Covers employee directory listing, headcount aggregation, and hiring trend reports.

What I skipped: I intentionally decided against indexing late_minutes. Since leaderboard queries are already scoped down by a date range, adding another index here would slow down write operations for minimal lookup gain.

2. Handling Punch-In Race Conditions
When concurrent punch-in requests arrive at the exact same time, both compute the target attendance date and attempt an insert_one.

Instead of doing a read-before-write check (which is inherently prone to race conditions), I rely entirely on the unique (emp_code, date) index. The database allows exactly one insert to succeed. The losing request throws a DuplicateKeyError, which the application catches and converts into a clean 409 Conflict response.

3. Leaderboard Tie-Breaking
Leaderboard rankings use standard competition ranking via $rank (e.g., 1st, 2nd, 2nd, 4th), followed by a post-filter for rank <= limit.

If a tie occurs right at the threshold cutoff, all tied employees are returned—meaning the final result count may occasionally exceed the requested limit. Tied results are sorted deterministically by total minutes, then alphabetically by emp_code.

4. Headcount & Analytics Calculation
To calculate headcount, the aggregation pipeline anchors on the employees collection first (filtered by joined_on <= month end) and then $lookups matching attendance logs.

This ensures that employees who haven't logged any attendance during the period still appear in the overall headcount with empty/zero stats, rather than being inadvertently filtered out.

5. Scaling Plan (100x Data Volume)
If raw log volume grows significantly, calculating analytics on the fly will become a performance bottleneck.

To handle scale, I would move toward pre-aggregated daily rollups per employee. These summary tables would update asynchronously whenever a punch-in, punch-out, or manual correction occurs, keeping analytics queries fast and avoiding full table scans over millions of raw log entries.
