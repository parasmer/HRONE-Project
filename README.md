
# Employee Attendance & Analytics API
A FastAPI and MongoDB service for tracking employee attendance, punch logs, and real-time operational analytics.

🛠️ Quick Start
Prerequisites
Python 3.10+

MongoDB 6.0+ (Required for aggregation pipelines using $setWindowFields, $rank, and $isNumber)

Running the API
Install dependencies:

Bash
pip install -r requirements.txt
Set up environment variables:
Create a .env file or export your database configuration:

Bash
export MONGO_URI="mongodb://localhost:27017"
export MONGO_DB="attendance_db"
Start the server:

Bash
uvicorn app.main:app --port 8000
(Note: Database indexes are automatically built on application startup.)

📌 Implementation Notes & Business Logic
Here are a few important design choices and edge-case behaviors built into the service:

📈 Analytics & Trends
7-Day Moving Averages: The moving_avg_7d calculation averages unrounded daily rates first and rounds the final result to 4 decimal places (using standard half-up rounding).

Trend Query Limits: Trend analytics are capped at a maximum date range of 92 inclusive days (counting both start and end dates). Queries exceeding this limit return a 422 Unprocessable Entity.

Gap Filling: To ensure seamless daily timelines without missing dates, the trend pipeline fills missing days in MongoDB using a $range + $unwind approach rather than $densify.

⏱️ Punch-In / Punch-Out Behavior
Validation Order: When punching out, the system checks whether the session is already closed (409 Conflict) before executing timestamp validation checks (422 Unprocessable Entity).

✏️ Corrections & Updates (PATCH Requests)
No-Op Protection: A PATCH request that doesn't change the actual status or punch timestamps is rejected as a no-op (422 Unprocessable Entity), even if calculated/derived fields in the record are currently stale.

Concurrency Control: Concurrent record updates use optimistic locking. The system automatically retries updates up to 5 times before failing with a 409 Conflict.