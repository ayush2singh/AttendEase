# Employee Attendance & Analytics API

A FastAPI + MongoDB REST API that records employee punch-in / punch-out events and exposes attendance analytics.

## Tech stack

| Layer | Choice |
|---|---|
| Runtime | Python 3.12 |
| Framework | FastAPI 0.110+ |
| Server | Uvicorn (ASGI) |
| Database | MongoDB 6.0+ (local or Atlas M0) |
| Driver | PyMongo 4.7+ |
| Validation | Pydantic v2 |

## Prerequisites

- Python 3.10 or newer
- MongoDB 6.0 or newer running on `localhost:27017` (or a free MongoDB Atlas M0 cluster)

## Local setup

```bash
# 1. Clone and enter the project
git clone <your-repo-url>
cd <repo-folder>

# 2. Create and activate a virtual environment
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure environment
#    Create a .env file in the project root:
#    MONGO_URI=mongodb://localhost:27017
#    MONGO_DB=attendance_db

# 5. (Optional) Seed sample data
python sample_seed.py

# 6. Start the server
uvicorn app.main:app --port 8000 --reload
```

The API is now available at **http://localhost:8000**. Interactive docs at **http://localhost:8000/docs**.

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `MONGO_URI` | `mongodb://localhost:27017` | MongoDB connection string |
| `MONGO_DB` | `attendance_db` | Database name |

> **Never commit `.env` to version control.**

## API overview

| Method | Path | Description |
|---|---|---|
| GET | `/health` | Liveness check |
| POST | `/employees` | Create an employee |
| GET | `/employees` | List employees (paginated, filterable by department) |
| POST | `/attendance/punch-in` | Record a punch-in |
| POST | `/attendance/punch-out` | Record a punch-out |
| GET | `/attendance` | List attendance records (paginated, filterable) |
| PATCH | `/attendance/{emp_code}/{date}` | Correct an attendance record |
| GET | `/analytics/employees/{emp_code}/monthly` | Monthly summary for one employee |
| GET | `/analytics/departments/summary` | Department-level summary for a month |
| GET | `/analytics/leaderboard/late` | Late-comers leaderboard for a month |
| GET | `/analytics/departments/{department}/trend` | Daily trend for a department |
| GET | `/admin/explain/{endpoint}` | MongoDB explain plan for an endpoint |

Full contract: `openapi.yaml`. Business rules R1–R10 are documented in that file.

## Project files

```
app/
  main.py          # All application code (single-file requirement)
requirements.txt
REVIEW.md          # Defect catalogue for the starter app/main.py
DECISIONS.md       # Design decisions: indexes, race handling, analytics approach
README.md          # This file
```

## Running tests (if present)

```bash
pip install httpx pytest pytest-asyncio
pytest
```

## Submit checklist

- [ ] `app/main.py` — complete implementation
- [ ] `requirements.txt` — pinned or minimum-version dependencies
- [ ] `REVIEW.md` — starter defect catalogue filled in
- [ ] `DECISIONS.md` — design decisions filled in
- [ ] `README.md` — setup instructions
- [ ] No `.env`, no secrets, no Dockerfile committed
