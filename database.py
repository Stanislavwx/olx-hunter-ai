"""
database.py — SQLite state management via aiosqlite.

Stores processed offer_id values to prevent duplicate alerts across restarts.
"""

import logging
from pathlib import Path

import aiosqlite

logger = logging.getLogger(__name__)

DB_PATH = Path("data/seen_offers.db")


async def init_db() -> None:
    """Initialize the database and create the seen_offers table if missing."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS seen_offers (
                offer_id    TEXT PRIMARY KEY,
                task_name   TEXT NOT NULL,
                seen_at     DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        await db.commit()
    logger.info("Database initialised at %s", DB_PATH)


async def is_offer_seen(offer_id: str) -> bool:
    """Return True if this offer_id has already been processed."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT 1 FROM seen_offers WHERE offer_id = ?", (offer_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return row is not None


async def mark_offer_seen(offer_id: str, task_name: str) -> None:
    """Persist an offer_id so it is never alerted on again."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO seen_offers (offer_id, task_name) VALUES (?, ?)",
            (offer_id, task_name),
        )
        await db.commit()
    logger.debug("Marked offer %s (task: %s) as seen", offer_id, task_name)
