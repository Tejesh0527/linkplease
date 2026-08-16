import hmac
import hashlib
import json
import asyncio
import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from main import app, API_KEY
from database import init_db


@pytest_asyncio.fixture(autouse=True)
async def setup_database():
    await init_db()
    yield


def generate_signature(body: bytes, secret: str) -> str:
    h = hmac.new(secret.encode('utf-8'), body, hashlib.sha256).hexdigest()
    return f"sha256={h}"


@pytest.mark.asyncio
async def test_create_rule():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        response = await ac.post("/rules", json={"keyword": "PRICE", "dm_message": "Price list: $100"})
        assert response.status_code == 201
        data = response.json()
        assert data["keyword"] == "PRICE"
        assert data["dm_message"] == "Price list: $100"
        assert "rule_id" in data


@pytest.mark.asyncio
async def test_webhook_hmac_verification():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        payload = {
            "event_id": "evt_test_hmac_1",
            "event_type": "comment.created",
            "sent_at": "2026-08-10T09:14:22Z",
            "data": {
                "comment_id": "cmt_hmac_1",
                "post_id": "post_1",
                "text": "PRICE please",
                "from": {"user_id": "usr_hmac_1", "username": "user1"}
            }
        }
        raw_bytes = json.dumps(payload).encode('utf-8')

        # 1. Invalid signature
        bad_headers = {"X-PseudoGram-Signature": "sha256=invalid_hash", "Content-Type": "application/json"}
        resp_bad = await ac.post("/webhook", content=raw_bytes, headers=bad_headers)
        assert resp_bad.status_code == 401

        # 2. Valid signature
        valid_sig = generate_signature(raw_bytes, API_KEY)
        good_headers = {"X-PseudoGram-Signature": valid_sig, "Content-Type": "application/json"}
        resp_good = await ac.post("/webhook", content=raw_bytes, headers=good_headers)
        assert resp_good.status_code == 200


@pytest.mark.asyncio
async def test_duplicate_event_deduplication():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        payload = {
            "event_id": "evt_dedup_repeat_1",
            "event_type": "comment.created",
            "sent_at": "2026-08-10T09:14:22Z",
            "data": {
                "comment_id": "cmt_dedup_1",
                "post_id": "post_1",
                "text": "PRICE list",
                "from": {"user_id": "usr_dedup_1", "username": "user_dedup"}
            }
        }
        raw_bytes = json.dumps(payload).encode('utf-8')
        headers = {"X-PseudoGram-Signature": generate_signature(raw_bytes, API_KEY), "Content-Type": "application/json"}

        # First delivery
        r1 = await ac.post("/webhook", content=raw_bytes, headers=headers)
        assert r1.status_code == 200

        # Second delivery (Duplicate event_id)
        r2 = await ac.post("/webhook", content=raw_bytes, headers=headers)
        assert r2.status_code == 200
        assert r2.json().get("detail") == "duplicate_event_blocked"


@pytest.mark.asyncio
async def test_user_rule_deduplication():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        # Create Rule
        await ac.post("/rules", json={"keyword": "INFO", "dm_message": "Here is info"})

        # Webhook 1 for user_a
        p1 = {
            "event_id": "evt_user_a_1",
            "event_type": "comment.created",
            "sent_at": "2026-08-10T09:14:22Z",
            "data": {
                "comment_id": "cmt_info_1",
                "post_id": "post_1",
                "text": "INFO please",
                "from": {"user_id": "usr_same_user_1", "username": "user_a"}
            }
        }
        raw1 = json.dumps(p1).encode('utf-8')
        await ac.post("/webhook", content=raw1, headers={"X-PseudoGram-Signature": generate_signature(raw1, API_KEY), "Content-Type": "application/json"})

        # Webhook 2 for same user_a, different comment
        p2 = {
            "event_id": "evt_user_a_2",
            "event_type": "comment.created",
            "sent_at": "2026-08-10T09:14:25Z",
            "data": {
                "comment_id": "cmt_info_2",
                "post_id": "post_1",
                "text": "give me INFO again!",
                "from": {"user_id": "usr_same_user_1", "username": "user_a"}
            }
        }
        raw2 = json.dumps(p2).encode('utf-8')
        await ac.post("/webhook", content=raw2, headers={"X-PseudoGram-Signature": generate_signature(raw2, API_KEY), "Content-Type": "application/json"})

        # Give matcher worker time to process
        await asyncio.sleep(0.5)

        # Check stats endpoint
        res = await ac.get("/stats")
        stats = res.json()
        assert stats["duplicates_blocked"] >= 1


@pytest.mark.asyncio
async def test_stats_endpoint_contract():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        res = await ac.get("/stats")
        assert res.status_code == 200
        data = res.json()
        assert "sent" in data
        assert "failed" in data
        assert "queued" in data
        assert "duplicates_blocked" in data
