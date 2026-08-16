import os
from datetime import datetime, timezone

def utcnow():
    return datetime.now(timezone.utc)

from typing import AsyncGenerator
from sqlalchemy import (
    Column, String, Text, DateTime, Integer, UniqueConstraint, select, update, func
)
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import declarative_base

# SQLite database file URL (using aiosqlite driver)
DB_PATH = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./data/linkplease.db")

# Ensure data directory exists if using relative sqlite file
if DB_PATH.startswith("sqlite+aiosqlite:///./"):
    os.makedirs("./data", exist_ok=True)

engine = create_async_engine(
    DB_PATH,
    echo=False,
    future=True,
    # SQLite WAL mode support
    connect_args={"check_same_thread": False} if "sqlite" in DB_PATH else {}
)

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False
)

Base = declarative_base()


class Event(Base):
    __tablename__ = "events"

    event_id = Column(String(128), primary_key=True)
    event_type = Column(String(64), nullable=False)
    comment_id = Column(String(128), nullable=True)
    received_at = Column(DateTime, default=utcnow)


class Comment(Base):
    __tablename__ = "comments"

    comment_id = Column(String(128), primary_key=True)
    post_id = Column(String(128), nullable=True)
    text = Column(Text, nullable=False, default="")
    user_id = Column(String(128), nullable=False, default="")
    username = Column(String(128), nullable=True)
    created_at = Column(DateTime, default=utcnow)
    deleted_at = Column(DateTime, nullable=True)


class Rule(Base):
    __tablename__ = "rules"

    rule_id = Column(String(128), primary_key=True)
    keyword = Column(String(256), nullable=False)
    dm_message = Column(Text, nullable=False)
    created_at = Column(DateTime, default=utcnow)


class DMAttempt(Base):
    __tablename__ = "dm_attempts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String(128), nullable=False)
    rule_id = Column(String(128), nullable=False)
    comment_id = Column(String(128), nullable=False)
    dm_id = Column(String(128), nullable=True)
    status = Column(String(32), nullable=False, default="pending")  # pending | queued | delivered | failed
    retry_count = Column(Integer, nullable=False, default=0)
    next_retry_at = Column(DateTime, nullable=True)
    idempotency_key = Column(String(256), nullable=False)
    created_at = Column(DateTime, default=utcnow)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)

    __table_args__ = (
        UniqueConstraint("user_id", "rule_id", name="uq_user_rule"),
    )


class StatsCounter(Base):
    __tablename__ = "stats_counters"

    key = Column(String(64), primary_key=True)
    value = Column(Integer, nullable=False, default=0)


async def init_db():
    """Initializes the database schema and enables SQLite WAL mode."""
    async with engine.begin() as conn:
        if "sqlite" in str(engine.url):
            await conn.exec_driver_sql("PRAGMA journal_mode=WAL;")
            await conn.exec_driver_sql("PRAGMA synchronous=NORMAL;")
        await conn.run_sync(Base.metadata.create_all)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Dependency helper for FastAPI endpoints."""
    async with AsyncSessionLocal() as session:
        yield session


async def increment_stats_counter(session: AsyncSession, key: str, amount: int = 1):
    """Atomically increments a named counter in the stats_counters table."""
    stmt = update(StatsCounter).where(StatsCounter.key == key).values(value=StatsCounter.value + amount)
    result = await session.execute(stmt)
    if result.rowcount == 0:
        # Create row if it doesn't exist
        counter = StatsCounter(key=key, value=amount)
        session.add(counter)
    await session.commit()


async def get_stats_counter(session: AsyncSession, key: str) -> int:
    """Reads a named counter from stats_counters."""
    result = await session.execute(select(StatsCounter.value).where(StatsCounter.key == key))
    val = result.scalar_one_or_none()
    return val if val is not None else 0
