# FAILURES.md — Known Failure Modes & Edge Conditions

This document lists the specific conditions under which this system can still lose a DM, send a duplicate, or report an inaccurate `/stats` count.

---

### 1. Crashes Between 202 Acceptance and DB Persistence Cause Duplicate Retries
* **Condition**: `sender_worker` sends `POST /v1/dm/send`. The mock API accepts the request and returns `202 Accepted` with `{"dm_id": "dm_7c1f0a"}`. Before `worker.py` can commit `status = 'queued'` and `dm_id = 'dm_7c1f0a'` to SQLite, the process is killed (SIGKILL / container restart).
* **Impact**: The mock API has the DM queued, but our database still marks it as `pending`. On reboot, the recovery worker re-sends the DM with `Idempotency-Key: <user_id>:<rule_id>`. If the mock API's idempotency window expired or failed, a duplicate DM is sent.

---

### 2. Uncommitted Counter Updates Lead to Stat Under-Counting
* **Condition**: A duplicate `event_id` or duplicate `(user_id, rule_id)` triggers a DB unique constraint violation. The exception handler catches it and executes `increment_stats_counter('duplicates_blocked')`.
* **Impact**: If the process restarts before this counter commit succeeds, the duplicate DM is correctly blocked (zero duplicate DMs sent), but `/stats` will under-report `duplicates_blocked` by 1 relative to server-side logs.

---

### 3. Reconciliation Polling Interval Creates Metric Lag Under Load
* **Condition**: The mock API accepts a DM (`queued`), but internal delivery fails 100ms later. Our poller worker inspects `GET /v1/dm/{dm_id}` on a 4-second sweep interval.
* **Impact**: For up to 4 seconds, `/stats` reports the DM as `queued` instead of `failed` (or re-enqueuing for retry). While eventual consistency is guaranteed, high-frequency evaluation scripts hitting `/stats` mid-sweep will see temporary queue staleness.

---

### 4. Hard Retry Cap Marks Outage Failures Terminal
* **Condition**: The mock API returns `500 Internal Error` continuously for all 6 backoff retry attempts (spanning ~2 minutes).
* **Impact**: Once `retry_count` hits 6, the system transitions `DMAttempt` to `failed` permanently. If the mock API recovers on attempt #7, the DM will not be re-attempted without manual database intervention.

---

### 5. Render Free-Tier Cold Starts Miss the 5-Second Webhook SLA
* **Condition**: On Render's free tier, the web service spins down after 15 minutes of idle time. The first incoming webhook request triggers a container cold start taking 15–30 seconds.
* **Impact**: The evaluator expects `POST /webhook` to respond within 5 seconds. During cold boot, the initial request times out on the mock API side, forcing the mock API to rely on its ~8% redelivery mechanism.

---

### 6. Post-Delivery `comment.deleted` Cannot Be Recalled
* **Condition**: A user comments "PRICE", the DM is matched and delivered within 1.5 seconds, and the user deletes their comment 5 seconds later.
* **Impact**: Instagram APIs do not allow unsending delivered DMs. The comment row records `deleted_at`, but `/stats` retains `sent = 1`.
