"""
database.py — SQLite state management via aiosqlite.

Manages:
- Seen offers deduplication across restarts.
- Dynamic task management (create, list, edit, pause, resume, delete from Telegram).
- Runtime settings (minimum confidence, language).
- Global analytics & counters.
"""

import json
import logging
from pathlib import Path
from typing import Any

import aiosqlite

logger = logging.getLogger(__name__)

DB_PATH = Path("data/seen_offers.db")
CONFIG_PATH = Path("config.json")


async def init_db() -> None:
    """Initialize SQLite tables and migrate default tasks from config.json."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        # 1. Seen offers table (per-task deduplication)
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS seen_offers (
                offer_id    TEXT NOT NULL,
                task_id     INTEGER NOT NULL DEFAULT 0,
                task_name   TEXT NOT NULL,
                seen_at     DATETIME DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (offer_id, task_id)
            )
            """
        )

        # Migration check: ensure task_id column exists
        async with db.execute("PRAGMA table_info(seen_offers)") as cur:
            columns = [row["name"] for row in await cur.fetchall()]
        if "task_id" not in columns:
            await db.execute("DROP TABLE IF EXISTS seen_offers_backup")
            await db.execute("ALTER TABLE seen_offers RENAME TO seen_offers_backup")
            await db.execute(
                """
                CREATE TABLE seen_offers (
                    offer_id    TEXT NOT NULL,
                    task_id     INTEGER NOT NULL DEFAULT 0,
                    task_name   TEXT NOT NULL,
                    seen_at     DATETIME DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (offer_id, task_id)
                )
                """
            )
            await db.execute(
                """
                INSERT OR IGNORE INTO seen_offers (offer_id, task_id, task_name, seen_at)
                SELECT offer_id, 0, task_name, seen_at FROM seen_offers_backup
                """
            )
            await db.execute("DROP TABLE IF EXISTS seen_offers_backup")

        # 2. Dynamic tasks table
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                name           TEXT NOT NULL UNIQUE,
                category_id    TEXT NOT NULL,
                query          TEXT NOT NULL,
                price_from     INTEGER DEFAULT 0,
                price_to       INTEGER DEFAULT 0,
                ai_prompt      TEXT NOT NULL,
                poll_interval  INTEGER DEFAULT 300,
                max_pages      INTEGER DEFAULT 3,
                is_active      INTEGER DEFAULT 1,
                created_at     DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        # Migration check: ensure max_pages, extra_params, exclude_keywords columns exist
        async with db.execute("PRAGMA table_info(tasks)") as cur:
            t_cols = [row["name"] for row in await cur.fetchall()]
        if "max_pages" not in t_cols:
            await db.execute("ALTER TABLE tasks ADD COLUMN max_pages INTEGER DEFAULT 3")
        if "extra_params" not in t_cols:
            await db.execute("ALTER TABLE tasks ADD COLUMN extra_params TEXT DEFAULT '{}'")
        if "exclude_keywords" not in t_cols:
            await db.execute("ALTER TABLE tasks ADD COLUMN exclude_keywords TEXT DEFAULT '[]'")

        # 3. Settings table
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )

        # 4. System statistics
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS stats (
                key   TEXT PRIMARY KEY,
                count INTEGER DEFAULT 0
            )
            """
        )
        await db.execute("INSERT OR IGNORE INTO stats (key, count) VALUES ('scanned_total', 0)")
        await db.execute("INSERT OR IGNORE INTO stats (key, count) VALUES ('deals_found', 0)")

        # Set default settings if absent
        await db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('min_confidence', '70')")
        await db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('language', 'ua')")

        # Sync tasks from config.json
        if CONFIG_PATH.exists():
            try:
                with CONFIG_PATH.open(encoding="utf-8") as fh:
                    data = json.load(fh)
                for t in data.get("tasks", []):
                    params = t.get("olx_params", {})
                    p_from = int(params.get("filter_float_price:from", 0))
                    p_to = int(params.get("filter_float_price:to", 0))
                    t_name = t.get("name", "Unnamed")
                    t_max_pages = int(t.get("max_pages", 3))

                    extra_keys = {
                        k: v for k, v in params.items()
                        if k not in ("category_id", "query", "filter_float_price:from", "filter_float_price:to", "currency")
                    }
                    extra_json = json.dumps(extra_keys, ensure_ascii=False)
                    exclude_json = json.dumps(t.get("exclude_keywords", []), ensure_ascii=False)

                    async with db.execute("SELECT id FROM tasks WHERE name = ?", (t_name,)) as cur:
                        existing = await cur.fetchone()

                    if existing:
                        await db.execute(
                            """
                            UPDATE tasks
                            SET category_id = ?, query = ?, price_from = ?, price_to = ?, ai_prompt = ?,
                                poll_interval = ?, max_pages = ?, extra_params = ?, exclude_keywords = ?
                            WHERE id = ?
                            """,
                            (
                                params.get("category_id", "0"),
                                params.get("query", ""),
                                p_from,
                                p_to,
                                t.get("ai_prompt", "Evaluate offer."),
                                int(t.get("poll_interval_seconds", 300)),
                                t_max_pages,
                                extra_json,
                                exclude_json,
                                existing["id"],
                            ),
                        )
                    else:
                        await db.execute(
                            """
                            INSERT INTO tasks (
                                name, category_id, query, price_from, price_to, ai_prompt,
                                poll_interval, max_pages, extra_params, exclude_keywords, is_active
                            )
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                            """,
                            (
                                t_name,
                                params.get("category_id", "0"),
                                params.get("query", ""),
                                p_from,
                                p_to,
                                t.get("ai_prompt", "Evaluate offer."),
                                int(t.get("poll_interval_seconds", 300)),
                                t_max_pages,
                                extra_json,
                                exclude_json,
                            ),
                        )
                logger.info("Synchronized tasks from config.json into SQLite database.")
            except Exception as exc:  # noqa: BLE001
                logger.error("Failed to sync config.json: %s", exc)

        await db.commit()
    logger.info("Database initialised at %s", DB_PATH)


# ── Offer Deduplication & History Management ──────────────────────────────────

async def is_offer_seen(offer_id: str, task_id: int = 0) -> bool:
    """Return True if this offer_id has already been processed for this specific task."""
    async with aiosqlite.connect(DB_PATH) as db:
        if task_id > 0:
            query = "SELECT 1 FROM seen_offers WHERE offer_id = ? AND task_id = ?"
            params = (offer_id, task_id)
        else:
            query = "SELECT 1 FROM seen_offers WHERE offer_id = ?"
            params = (offer_id,)

        async with db.execute(query, params) as cursor:
            row = await cursor.fetchone()
            return row is not None


async def mark_offer_seen(offer_id: str, task_id: int, task_name: str) -> None:
    """Persist an offer_id so it is never alerted on again for this task."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO seen_offers (offer_id, task_id, task_name) VALUES (?, ?, ?)",
            (offer_id, task_id, task_name),
        )
        await db.commit()


async def clear_seen_offers_for_task(task_id: int) -> int:
    """Clear seen offers for a specific task. Returns number of deleted records."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("DELETE FROM seen_offers WHERE task_id = ?", (task_id,))
        await db.commit()
        return cursor.rowcount


async def clear_all_seen_offers() -> int:
    """Clear all seen offers across all tasks. Returns number of deleted records."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("DELETE FROM seen_offers")
        await db.commit()
        return cursor.rowcount


async def get_seen_offers_count(task_id: int | None = None) -> int:
    """Return total count of recorded offers."""
    async with aiosqlite.connect(DB_PATH) as db:
        if task_id is not None:
            async with db.execute("SELECT COUNT(*) FROM seen_offers WHERE task_id = ?", (task_id,)) as cur:
                row = await cur.fetchone()
                return int(row[0]) if row else 0
        async with db.execute("SELECT COUNT(*) FROM seen_offers") as cur:
            row = await cur.fetchone()
            return int(row[0]) if row else 0


# ── Task CRUD & Live Editing ──────────────────────────────────────────────────

def _format_task(row: aiosqlite.Row | None) -> dict[str, Any] | None:
    """Format an SQLite row into a dict with parsed extra_params and exclude_keywords."""
    if not row:
        return None
    d = dict(row)
    if "extra_params" in d:
        try:
            d["extra_params"] = json.loads(d["extra_params"]) if isinstance(d["extra_params"], str) else (d["extra_params"] or {})
        except Exception:
            d["extra_params"] = {}
    else:
        d["extra_params"] = {}

    if "exclude_keywords" in d:
        try:
            d["exclude_keywords"] = json.loads(d["exclude_keywords"]) if isinstance(d["exclude_keywords"], str) else (d["exclude_keywords"] or [])
        except Exception:
            d["exclude_keywords"] = []
    else:
        d["exclude_keywords"] = []

    return d


async def get_active_tasks() -> list[dict[str, Any]]:
    """Return all currently active search tasks."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM tasks WHERE is_active = 1 ORDER BY id ASC") as cur:
            rows = await cur.fetchall()
            return [res for r in rows if (res := _format_task(r)) is not None]


async def get_all_tasks() -> list[dict[str, Any]]:
    """Return all search tasks (active and paused)."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM tasks ORDER BY id ASC") as cur:
            rows = await cur.fetchall()
            return [res for r in rows if (res := _format_task(r)) is not None]


async def get_task_by_id(task_id: int) -> dict[str, Any] | None:
    """Get a single task by its database ID."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)) as cur:
            row = await cur.fetchone()
            return _format_task(row)


async def add_task(
    name: str,
    query: str,
    category_id: str,
    price_from: int,
    price_to: int,
    ai_prompt: str,
    poll_interval: int = 300,
    max_pages: int = 3,
    extra_params: dict[str, Any] | None = None,
    exclude_keywords: list[str] | None = None,
) -> int:
    """Create a new search task in SQLite. Returns the new task ID."""
    extra_json = json.dumps(extra_params or {}, ensure_ascii=False)
    exclude_json = json.dumps(exclude_keywords or [], ensure_ascii=False)
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            INSERT INTO tasks (
                name, query, category_id, price_from, price_to, ai_prompt,
                poll_interval, max_pages, extra_params, exclude_keywords, is_active
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
            """,
            (name, query, category_id, price_from, price_to, ai_prompt, poll_interval, max_pages, extra_json, exclude_json),
        )
        await db.commit()
        return cursor.lastrowid or 0


async def update_task_prompt(task_id: int, new_prompt: str) -> bool:
    """Update AI prompt for a specific task."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE tasks SET ai_prompt = ? WHERE id = ?", (new_prompt, task_id))
        await db.commit()
        return True


async def update_task_prices(task_id: int, price_from: int, price_to: int) -> bool:
    """Update price bounds for a specific task."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE tasks SET price_from = ?, price_to = ? WHERE id = ?", (price_from, price_to, task_id))
        await db.commit()
        return True


async def update_task_query(task_id: int, new_query: str) -> bool:
    """Update query keyword for a specific task."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE tasks SET query = ? WHERE id = ?", (new_query, task_id))
        await db.commit()
        return True


async def toggle_task(task_id: int) -> bool:
    """Toggle a task active/paused state. Returns new active status."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT is_active FROM tasks WHERE id = ?", (task_id,)) as cur:
            row = await cur.fetchone()
            if not row:
                return False
            new_status = 0 if row["is_active"] == 1 else 1
        await db.execute("UPDATE tasks SET is_active = ? WHERE id = ?", (new_status, task_id))
        await db.commit()
        return bool(new_status)


async def delete_task(task_id: int) -> bool:
    """Delete a task from database by ID."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        await db.commit()
        return True


# ── Settings ──────────────────────────────────────────────────────────────────

async def get_setting(key: str, default: str = "") -> str:
    """Retrieve runtime setting value."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT value FROM settings WHERE key = ?", (key,)) as cur:
            row = await cur.fetchone()
            return row["value"] if row else default


async def set_setting(key: str, value: str) -> None:
    """Update or insert runtime setting."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            (key, value),
        )
        await db.commit()


# ── Statistics ────────────────────────────────────────────────────────────────

async def record_scanned(count: int = 1) -> None:
    """Increment scanned offers counter."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE stats SET count = count + ? WHERE key = 'scanned_total'", (count,)
        )
        await db.commit()


async def record_deal() -> None:
    """Increment good deals found counter."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE stats SET count = count + 1 WHERE key = 'deals_found'"
        )
        await db.commit()


async def get_stats() -> dict[str, int]:
    """Return dictionary of system statistics."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT key, count FROM stats") as cur:
            rows = await cur.fetchall()
            return {r["key"]: r["count"] for r in rows}
