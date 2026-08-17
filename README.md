# LinkPlease Instagram DM Automation Engine

LinkPlease automates Instagram DMs for creators when followers comment specific keywords on posts. This engine handles hostility from the mock Instagram API (`pseudogram-api.onrender.com`), including rate limits (10 req / 60s), event redeliveries (~8%), out-of-order webhooks, 500 errors (~20%), false 202 acceptances (~15%), and `comment.deleted` events.

## Features & Compliance

- **Part A (Required)**:
  - `POST /rules`: Create keyword rules (case-insensitive, substring matching anywhere in comment text).
  - `POST /webhook`: Fast event intake (< 50ms), HMAC signature verification, redelivery deduplication.
  - **Single DM Guarantee**: Unique constraint `UNIQUE(user_id, rule_id)` guarantees the same user is never DMed twice for the same rule.
  - **Zero Lost DMs**: DB-backed job queue with exponential backoff retries.

- **Part B**:
  - **Signature Verification**: Validates `X-PseudoGram-Signature` HMAC-SHA256 headers using the configured API Key.
  - **Live `/stats`**: Real-time stats calculation from DB state (`sent`, `failed`, `queued`, `duplicates_blocked`).

- **Part C**:
  - **Status Reconciliation**: Background worker polls `GET /v1/dm/{dm_id}` (unlimited reads) to reconcile status of queued DMs and retry failed deliveries.
  - **`comment.deleted` Handling**: Out-of-order tombstoning and pending DM cancellation prior to sending.
  - **Rate Limiter (500 events in 10s)**: Sliding window rate limiter guarantees `POST /v1/dm/send` never exceeds 10 requests per rolling 60 seconds (0 rate limit breaches).

---

## API Contract Endpoints

| Method | Path | Description |
|---|---|---|
| `POST` | `/webhook` | Receives comment events. Returns 200 within <50ms. Validates HMAC signature header. |
| `POST` | `/rules` | Body: `{ "keyword": "PRICE", "dm_message": "..." }`. Returns 201 with `{ "rule_id": "...", "keyword": "...", "dm_message": "..." }`. |
| `GET` | `/stats` | Returns `{ "sent": N, "failed": N, "queued": N, "duplicates_blocked": N }`. |
| `GET` | `/dashboard` | Interactive web dashboard for live monitoring and rule management. |

---

## Local Setup & Quickstart

### 1. Install Dependencies
```bash
pip install -r requirements.txt
```

### 2. Run the Application
```bash
python -m uvicorn main:app --reload --port 8000
```
Open `http://localhost:8000` in your browser to view the Dashboard UI!

### 3. Run Automated Tests
```bash
python -m pytest -v test_app.py
```

### 4. Run Mock API Simulation Load Test
```bash
python run_simulation.py
```

---

## Architecture Diagram

```
+-------------------+      POST /webhook      +-----------------------+
|  Mock Instagram   | ----------------------> | FastAPI Intake Engine |
|       API         | <---------------------- | (HMAC, Dedup, SQLite) |
+-------------------+     200 OK (<50ms)      +-----------------------+
          ^                                               |
          | POST /v1/dm/send (Max 10 req / 60s)           v
          +----------------------------------- [ Rate-Limited Worker ]
          |                                               |
          | GET /v1/dm/{dm_id}                            v
          +----------------------------------- [ Status Reconciler ]
```

---

## Deliverables & Documentation

- **FAILURES.md**: Detailed analysis of failure modes, edge cases, race windows, and cold start considerations.
- **Dockerfile & Procfile**: Ready for deployment on Render, Railway, or Fly.io.


LOOM VIDEO URL:https://www.loom.com/share/7dba0f06534744ea937e708c528104bb
