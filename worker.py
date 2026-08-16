import os
import asyncio
import datetime
import logging
from typing import Optional, List
import httpx
from sqlalchemy import select, update, and_, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from database import (
    AsyncSessionLocal, Comment, Rule, DMAttempt, Event, increment_stats_counter, utcnow
)

logger = logging.getLogger("linkplease.worker")

MOCK_API_BASE = os.getenv("MOCK_API_BASE", "https://pseudogram-api.onrender.com")
API_KEY = os.getenv("API_KEY", "").strip()

# In-memory queue for waking matcher worker
matcher_queue: asyncio.Queue[str] = asyncio.Queue()

# Sliding window rate limiter for Sender Worker (max 10 requests per rolling 60s)
RATE_LIMIT_MAX_REQUESTS = 10
RATE_LIMIT_WINDOW_SECONDS = 60.0
send_timestamps: List[float] = []
rate_limit_lock = asyncio.Lock()

MAX_SEND_RETRIES = 6


def set_api_key(key: str):
    global API_KEY
    API_KEY = key.strip()


async def enforce_rate_limit():
    """Sliding window rate limiter guaranteeing max 10 requests per rolling 60 seconds."""
    async with rate_limit_lock:
        now = asyncio.get_event_loop().time()
        global send_timestamps
        send_timestamps = [t for t in send_timestamps if now - t < RATE_LIMIT_WINDOW_SECONDS]

        if len(send_timestamps) >= RATE_LIMIT_MAX_REQUESTS:
            wait_time = RATE_LIMIT_WINDOW_SECONDS - (now - send_timestamps[0]) + 0.1
            if wait_time > 0:
                logger.info(f"Rate limiter threshold reached ({len(send_timestamps)}/10). Sleeping {wait_time:.2f}s...")
                await asyncio.sleep(wait_time)
                now = asyncio.get_event_loop().time()
                send_timestamps = [t for t in send_timestamps if now - t < RATE_LIMIT_WINDOW_SECONDS]

        send_timestamps.append(now)


async def recover_unmatched_comments():
    """On boot/startup, scans comments table for all existing comments and enqueues them for matcher worker."""
    try:
        async with AsyncSessionLocal() as session:
            result = await session.execute(select(Comment.comment_id))
            comment_ids = result.scalars().all()
            logger.info(f"Startup recovery: Enqueuing {len(comment_ids)} comments for processing.")
            for cid in comment_ids:
                await matcher_queue.put(cid)
    except Exception as e:
        logger.error(f"Error in recover_unmatched_comments: {e}")


async def matcher_worker():
    """Consumes comment_ids from matcher_queue and creates DM attempts."""
    logger.info("Matcher worker started.")
    while True:
        try:
            comment_id = await matcher_queue.get()
            async with AsyncSessionLocal() as session:
                # 1. Load comment
                res = await session.execute(select(Comment).where(Comment.comment_id == comment_id))
                comment = res.scalar_one_or_none()

                if not comment:
                    matcher_queue.task_done()
                    continue

                comment_user_id = comment.user_id
                comment_text = comment.text
                comment_id_value = comment.comment_id

                # 2. Check if comment was marked deleted
                if comment.deleted_at is not None:
                    # Cancel any pending DM attempt for this comment
                    res_pending = await session.execute(
                        select(DMAttempt).where(and_(DMAttempt.comment_id == comment_id, DMAttempt.status == "pending"))
                    )
                    pending_attempts = res_pending.scalars().all()

                    if pending_attempts:
                        for pa in pending_attempts:
                            pa.status = "failed"
                            pa.updated_at = utcnow()
                            await increment_stats_counter(session, "duplicates_blocked", 1)
                        await session.commit()
                        logger.info(f"Canceled {len(pending_attempts)} pending DM attempt(s) for deleted comment {comment_id}")

                    matcher_queue.task_done()
                    continue

                # 3. Match against active rules
                res_rules = await session.execute(select(Rule.rule_id, Rule.keyword, Rule.dm_message))
                rules = res_rules.all()

                text_lower = comment_text.lower()
                for rule in rules:
                    rule_id = rule.rule_id
                    keyword = rule.keyword
                    message = rule.dm_message
                    if keyword.lower() in text_lower:
                        user_id = comment_user_id
                        
                        idempotency_key = f"{user_id}:{rule_id}"
                        attempt = DMAttempt(
                            user_id=user_id,
                            rule_id=rule_id,
                            
                            comment_id=comment_id_value,
                            status="pending",
                            idempotency_key=idempotency_key,
                            created_at=utcnow(),
                            updated_at=utcnow()
                        )
                        session.add(attempt)
                        try:
                            await session.commit()
                            logger.info(f"Created DM attempt for user {user_id}, rule {keyword}")
                        except IntegrityError:
                            await session.rollback()
                            await increment_stats_counter(session, "duplicates_blocked", 1)
                            logger.info(f"Duplicate user DM blocked for user {user_id}, rule {rule_id}")
                            
            matcher_queue.task_done()
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Error in matcher_worker: {e}", exc_info=True)
            await asyncio.sleep(1)


async def sender_worker():
    """Picks up pending DM attempts and calls POST /v1/dm/send respecting rate limits."""
    logger.info("Sender worker started.")
    async with httpx.AsyncClient(timeout=10.0) as client:
        while True:
            try:
                now = utcnow()
                async with AsyncSessionLocal() as session:
                    # Find next pending attempt ready to be sent
                    stmt = select(DMAttempt, Rule.dm_message).join(
                        Rule, DMAttempt.rule_id == Rule.rule_id
                    ).where(
                        and_(
                            DMAttempt.status == "pending",
                            or_(DMAttempt.next_retry_at.is_(None), DMAttempt.next_retry_at <= now)
                        )
                    ).order_by(DMAttempt.id.asc()).limit(1)

                    result = await session.execute(stmt)
                    row = result.first()

                    if not row:
                        # No pending attempts right now
                        await asyncio.sleep(1.0)
                        continue

                    attempt, dm_message = row

                    # Verify comment hasn't been deleted prior to sending
                    res_cmt = await session.execute(select(Comment.deleted_at).where(Comment.comment_id == attempt.comment_id))
                    deleted_at = res_cmt.scalar_one_or_none()
                    if deleted_at is not None:
                        attempt.status = "failed"
                        attempt.updated_at = utcnow()
                        await session.commit()
                        await increment_stats_counter(session, "duplicates_blocked", 1)
                        logger.info(f"Aborted sending DM attempt #{attempt.id} because comment was deleted.")
                        continue

                    # Rate limit enforcement
                    await enforce_rate_limit()

                    # Send request to mock API
                    url = f"{MOCK_API_BASE}/v1/dm/send"
                    headers = {
                        "Content-Type": "application/json",
                        "X-API-Key": API_KEY,
                        "Idempotency-Key": attempt.idempotency_key
                    }
                    payload = {
                        "recipient_user_id": attempt.user_id,
                        "message": dm_message,
                        "comment_id": attempt.comment_id
                    }

                    try:
                        resp = await client.post(url, json=payload, headers=headers)
                        
                        if resp.status_code in (200, 202):
                            data = resp.json()
                            attempt.status = "queued"
                            attempt.dm_id = data.get("dm_id")
                            attempt.updated_at = utcnow()
                            await session.commit()
                            logger.info(
                                f"SENDER: attempt_id={attempt.id}, "
                                f"status={attempt.status}, "
                                f"dm_id={attempt.dm_id}, "
                                f"api_response={data}"
                            )
                        elif resp.status_code == 429:
                            retry_after = int(resp.headers.get("Retry-After", "60"))
                            attempt.next_retry_at = utcnow() + datetime.timedelta(seconds=retry_after + 1)
                            attempt.updated_at = utcnow()
                            await session.commit()
                            logger.warning(f"Rate limited by mock API (429). Retrying after {retry_after}s")
                            await asyncio.sleep(retry_after)

                        elif resp.status_code == 500:
                            attempt.retry_count += 1
                            if attempt.retry_count >= MAX_SEND_RETRIES:
                                attempt.status = "failed"
                                logger.error(f"DM attempt #{attempt.id} failed permanently after {attempt.retry_count} retries.")
                            else:
                                backoff = min(60, 2 ** attempt.retry_count)
                                attempt.next_retry_at = utcnow() + datetime.timedelta(seconds=backoff)
                                logger.warning(f"Mock API returned 500. Retry {attempt.retry_count}/{MAX_SEND_RETRIES} in {backoff}s")
                            attempt.updated_at = utcnow()
                            await session.commit()

                        elif resp.status_code == 400:
                            attempt.status = "failed"
                            attempt.updated_at = utcnow()
                            await session.commit()
                            logger.error(f"Mock API returned 400 Invalid Request for attempt #{attempt.id}: {resp.text}")

                        else:
                            attempt.retry_count += 1
                            attempt.updated_at = utcnow()
                            await session.commit()

                    except Exception as http_err:
                        logger.error(f"Network error sending DM attempt #{attempt.id}: {http_err}")
                        attempt.retry_count += 1
                        attempt.next_retry_at = utcnow() + datetime.timedelta(seconds=5)
                        attempt.updated_at = utcnow()
                        await session.commit()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in sender_worker: {e}", exc_info=True)
                await asyncio.sleep(1)


async def poller_worker():
    """Polls status of queued DMs via GET /v1/dm/{dm_id} to reconcile terminal state."""
    logger.info("Poller worker started.")
    async with httpx.AsyncClient(timeout=10.0) as client:
        while True:
            try:
                await asyncio.sleep(4.0)  # Polling interval
                async with AsyncSessionLocal() as session:
                    stmt = select(DMAttempt).where(
                        and_(DMAttempt.status == "queued", DMAttempt.dm_id.isnot(None))
                    ).limit(20)
                    res = await session.execute(stmt)
                    queued_attempts = res.scalars().all()
                    logger.info(f"Poller: found {len(queued_attempts)} queued attempts.")


                    if not queued_attempts:
                        logger.info("Poller: no queued attempts found.")
                        continue

                    for attempt in queued_attempts:
                        url = f"{MOCK_API_BASE}/v1/dm/{attempt.dm_id}"
                        headers = {"X-API-Key": API_KEY}
                        try:
                            resp = await client.get(url, headers=headers)
                            logger.info(
                                f"Polling dm_id={attempt.dm_id}: "
                                f"status_code={resp.status_code}, response={resp.text}"
                                )
                            if resp.status_code == 200:
                                data = resp.json()
                                mock_status = data.get("status")

                                if mock_status == "delivered":
                                    attempt.status = "delivered"
                                    attempt.updated_at = utcnow()
                                    await session.commit()
                                    logger.info(f"DM attempt #{attempt.id} (dm_id={attempt.dm_id}) delivered successfully!")

                                elif mock_status == "failed":
                                    attempt.retry_count += 1
                                    if attempt.retry_count >= MAX_SEND_RETRIES:
                                        attempt.status = "failed"
                                        logger.error(f"DM attempt #{attempt.id} marked failed after delivery poll.")
                                    else:
                                        # Re-enqueue for re-sending
                                        attempt.status = "pending"
                                        attempt.dm_id = None
                                        attempt.next_retry_at = utcnow()
                                        logger.warning(f"DM attempt #{attempt.id} failed delivery. Re-enqueuing for retry {attempt.retry_count}.")
                                    attempt.updated_at = utcnow()
                                    await session.commit()

                        except Exception as poll_err:
                            logger.error(f"Error polling dm_id {attempt.dm_id}: {poll_err}")

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in poller_worker: {e}", exc_info=True)
                await asyncio.sleep(2)
