Code Review & Bug Fix LogBelow is a breakdown of the defects identified in the starter code (app/main.py), how they impacted the application, and the solutions implemented.
🐛 Bug Fixes & Refactoring
Timezones & Shift Calculations
# 1. Timezone-Aware Shift Starts (compute_late_minutes)
Issue: Shift starts were calculated using the server’s local time/date rather than IST, causing calculation errors on non-IST servers.
Fix: Standardized calculations to convert times correctly to IST and anchor shift starts to the correct attendance date.
# 2. Precise Late-Minute Truncation
Issue: Truncating seconds before comparing against the 10-minute threshold caused edge cases (e.g., 10 minutes and 59 seconds being counted as 0 late minutes).
Fix: Compared raw whole seconds first (> 600), then floored down to minutes.
# 3. Overnight Shift Handling
Issue: Overnight shifts (e.g., 22:00–06:00) compared punch-ins against the same calendar day's start time instead of the previous day, resulting in missed late minutes.
Fix: Used the correct attendance date anchor for shift validation and properly truncated microsecond values.
# 4. Accurate Work-Hour Rounding
Issue: Python's built-in round() uses float representation/banker's rounding, leading to discrepancies (e.g., rounding 4.125 to 4.12 instead of 4.13) and flipping half-day thresholds incorrectly.
Fix: Switched to strict Decimal arithmetic with ROUND_HALF_UP.
# 5. Overtime Minimum ThresholdIssue: Overtime was credited immediately without enforcing the 30-minute minimum rule.
Fix: Enforced a check ensuring overtime is only counted when floor-minutes greater than equal to  30.
# 6. Overnight Punch-Out & Type Safety
Issue: Punch-out times for overnight shifts referenced the wrong calendar day, and subtracting an IST-aware value from a naive datetime triggered a TypeError (HTTP 500).
Fix: Assigned overnight shift ends to the next day and ensured all datetime objects remain timezone-aware.

 Data Integrity & Race Conditions
# 7 & 15. Race Conditions on Creation & Punch-In
Issue: Check-then-insert patterns allowed parallel requests to bypass validation and create duplicate records.
Fix: Added unique database indexes (emp_code and composite (emp_code, date)) and handled DuplicateKeyError to cleanly return a 409 Conflict.
# 8. Input Validation (EmployeeIn)
Issue: Missing validation allowed malformed data (e.g., invalid employee codes, fake dates like joined_on="foo", invalid shift times like 25:99, or identical start/end shifts) to crash the server later with 500 errors.
Fix: Added strict schema patterns, real-date verification, and shift-time logic checks to reject bad inputs with a 422 Unprocessable Entity.
# 12. Missing Employee Handling (punch_in)
Issue: Punching in with an unknown employee code caused a TypeError when trying to access shift attributes on a None object.
Fix: Added a proper 404 Not Found response.
# 13 & 14. Punch-In Validation & Epoch Processing
Issue: Unchecked status values (like ABSENT) were accepted, and punched_at timestamp parsing relied on server-local, naive time conversions.
Fix: Restricted statuses to a strict Literal set (PRESENT, WFH, ON_DUTY) and enforced strict epoch-millisecond validation converted to UTC with truncated seconds.

API Responses & Performance
# 9. Employee Creation Timestamps
Issue: created_at used naive local time and returned as an ISO string instead of epoch milliseconds.
Fix: Standardized to UTC, stored as a BSON date, and returned as epoch milliseconds.
# 10 & 11. Pagination & Filtering (list_employees)
Issue: Pagination offsets skipped pages incorrectly (page * page_size), headcount totals ignored department filters, and unconstrained parameters (page=0 or massive page sizes) went unchecked.
Fix: Fixed page math to (page - 1) * page_size, included filters in total counts, sorted by emp_code, and bounded page sizes between 1 and 100.
# 16. Clean API Payload Formatting
Issue: Responses leaked internal MongoDB _id ObjectIds and returned raw ISO date strings instead of expected formats.
Fix: Created a serialization mapper (record_to_api) to drop _id, format timestamps as epoch ms, and provide fallback defaults for missing fields.
# 17 & 18. Scalability & Query Validation (list_attendance)
Issue: The endpoint loaded entire collections into memory and sorted/sliced in Python, failing under high data volumes. Date range validation and sorting order also deviated from specs.
Fix: Shifted sorting, skipping, and limiting to server-side database queries, added supporting indexes, and added 422 validations for invalid date ranges and page parameters.
# 19. Health Check Verification
Issue: The /health endpoint always returned 200 OK without checking database connectivity.
Fix: Added a database ping; the endpoint now returns 503 Service Unavailable if MongoDB drops.
# 20. Database Client Configuration
Issue: The default MongoClient configuration created naive datetimes, lacked timeout safety, and ran without startup indexes.
Fix: Configured tz_aware=True, added a 5-second connection timeout, and set up idempotent ensure_indexes() at application startup.

Code Reviewed & Validated (No Defects Found)

load_dotenv() behavior (allowing real environment variables to take precedence).

Stripping _id during employee creation responses.

Default shift timings (09:30 / 18:30) matching specifications.

Comparing date strings lexicographically with $gte / $lte (which is efficient and index-friendly).

Standardized {"detail": ...} shapes for 409 Conflict responses.