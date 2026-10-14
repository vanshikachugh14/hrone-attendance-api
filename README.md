# Employee Attendance & Analytics API

FastAPI + PyMongo implementation of the HROne attendance assignment. All application code is in `app/main.py`.

## Run

```bash
python -m venv .venv
# Windows: .venv\\Scripts\\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
# Set MONGO_URI and MONGO_DB in your shell, or use a local .env (do not commit it).
uvicorn app.main:app --port 8000
```

MongoDB 6.0+ is required. The application creates indexes at startup. The API expects the `employees` and `attendance_logs` collections to exist in the configured database; the evaluator seeds them directly.

## Implementation notes

- API timestamps are epoch milliseconds; MongoDB stores instants as BSON UTC datetimes.
- Analytics are implemented with MongoDB aggregation pipelines; attendance listing uses database-side filtering, sorting and pagination.
- The sample data supplied with the assignment is intentionally not included in this submission. Test against a local copy and the specified edge cases before relying on results.
- Do not commit credentials, `.env`, a virtual environment, Dockerfile, or data dumps.
