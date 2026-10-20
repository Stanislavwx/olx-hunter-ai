"""
main.py — OLX Hunter AI daemon entry point.

Loads configuration from config.json, initialises the database, then runs an
infinite async loop that scans each configured task on its own interval.
Failures in one task never affect others.
"""

import asyncio
import json
import logging
import os
import signal
import sys
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv

from ai_analyzer import analyze_offer
from database import init_db, is_offer_seen, mark_offer_seen
from olx_client import download_images, extract_image_urls, fetch_offers
from telegram_bot import send_alert

# ── Logging Setup ─────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("olx_hunter")

# ── Configuration ─────────────────────────────────────────────────────────────

CONFIG_PATH = Path("config.json")
DEFAULT_POLL_INTERVAL = 300  # seconds between scan cycles per task


def load_config() -> list[dict[str, Any]]:
    """Load and validate task configurations from config.json."""
    if not CONFIG_PATH.exists():
        logger.critical("config.json not found. Exiting.")
        sys.exit(1)

    with CONFIG_PATH.open(encoding="utf-8") as fh:
        data = json.load(fh)

    tasks: list[dict] = data.get("tasks", [])
    if not tasks:
        logger.critical("No tasks found in config.json. Exiting.")
        sys.exit(1)

    logger.info("Loaded %d task(s) from config.json", len(tasks))
    return tasks


# ── HTTP Client Factory ───────────────────────────────────────────────────────

def _build_http_client() -> httpx.AsyncClient:
    """Return a shared async HTTP client with browser-like headers."""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "uk-UA,uk;q=0.9,en-US;q=0.8,en;q=0.7",
        "Accept": "application/json",
        "Referer": "https://www.olx.ua/",
        "Origin": "https://www.olx.ua",
        "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Linux"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
    }
    return httpx.AsyncClient(headers=headers, timeout=30.0, follow_redirects=True)


# ── Per-Offer Pipeline ────────────────────────────────────────────────────────

async def process_offer(
    client: httpx.AsyncClient,
    offer: dict[str, Any],
    task: dict[str, Any],
) -> None:
    """
    Run the full pipeline for a single offer:
      1. Deduplicate via SQLite.
      2. Download images.
      3. Send to Gemini for analysis.
      4. Alert via Telegram if it's a good deal.
      5. Persist the offer_id regardless of outcome.

    Errors are caught and logged; they never propagate upward.
    """
    offer_id = str(offer.get("id", ""))
    task_name: str = task.get("name", "unknown")

    if not offer_id:
        logger.warning("Skipping offer with missing id in task '%s'", task_name)
        return

    # Step 1 — deduplication
    if await is_offer_seen(offer_id):
        logger.debug("Offer %s already seen — skipping", offer_id)
        return

    logger.info("New offer [%s] '%s'", offer_id, offer.get("title", "")[:70])

    try:
        # Step 2 — image download
        image_urls = extract_image_urls(offer)
        image_bytes_list = await download_images(client, image_urls, max_images=4)

        # Step 3 — AI analysis
        ai_prompt: str = task.get("ai_prompt", "Evaluate this listing.")
        analysis = await analyze_offer(offer, image_bytes_list, ai_prompt)

        if analysis is None:
            logger.warning("AI analysis returned None for offer %s — skipping alert", offer_id)
        elif analysis.get("is_good_deal"):
            # Step 4 — Telegram alert
            await send_alert(offer, analysis, task_name)
        else:
            logger.info(
                "Offer %s rejected by AI (confidence=%d%% reason=%s)",
                offer_id,
                analysis.get("confidence", 0),
                analysis.get("reason", "—")[:80],
            )

    except Exception as exc:  # noqa: BLE001
        logger.error("Unhandled error processing offer %s: %s", offer_id, exc)

    finally:
        # Step 5 — always mark as seen to prevent future reprocessing
        await mark_offer_seen(offer_id, task_name)


# ── Per-Task Scan Loop ────────────────────────────────────────────────────────

async def run_task_loop(task: dict[str, Any], initial_delay: float = 0.0) -> None:
    """
    Infinite loop for a single configured task.

    Polls OLX on the configured interval and processes every new offer
    through the full AI pipeline. Exceptions at the task level are caught
    so other task loops continue running.
    """
    task_name: str = task.get("name", "unnamed_task")
    poll_interval: int = int(task.get("poll_interval_seconds", DEFAULT_POLL_INTERVAL))
    olx_params: dict[str, Any] = task.get("olx_params", {})
    max_pages: int = int(task.get("max_pages", 2))

    logger.info(
        "Task '%s' started — polling every %ds with params %s",
        task_name, poll_interval, olx_params,
    )

    if initial_delay > 0:
        logger.debug("Task '%s' delaying start by %.1fs to stagger initial requests", task_name, initial_delay)
        await asyncio.sleep(initial_delay)

    async with _build_http_client() as client:
        while True:
            try:
                logger.info("→ Task '%s': starting scan cycle", task_name)
                offers = await fetch_offers(client, olx_params, max_pages=max_pages)
                logger.info(
                    "← Task '%s': fetched %d total offers", task_name, len(offers)
                )

                for offer in offers:
                    await process_offer(client, offer, task)

            except asyncio.CancelledError:
                logger.info("Task '%s' received cancellation — shutting down.", task_name)
                raise
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "Task '%s' scan cycle failed: %s — will retry in %ds",
                    task_name, exc, poll_interval,
                )

            logger.info(
                "Task '%s': sleeping %ds until next cycle …", task_name, poll_interval
            )
            await asyncio.sleep(poll_interval)


# ── Graceful Shutdown ─────────────────────────────────────────────────────────

def _install_signal_handlers(loop: asyncio.AbstractEventLoop) -> None:
    """Register SIGINT / SIGTERM handlers for clean shutdown."""
    def _handle_signal(sig: signal.Signals) -> None:  # type: ignore[name-defined]
        logger.info("Received signal %s — initiating graceful shutdown …", sig.name)
        for task in asyncio.all_tasks(loop):
            task.cancel()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _handle_signal, sig)


# ── Main Entry Point ──────────────────────────────────────────────────────────

async def main() -> None:
    """Bootstrap the daemon: load config, init DB, launch task loops."""
    load_dotenv()
    tasks = load_config()
    await init_db()

    logger.info("OLX Hunter AI daemon starting — %d task(s) active", len(tasks))

    task_coroutines = [
        run_task_loop(task, initial_delay=idx * 6.0)
        for idx, task in enumerate(tasks)
    ]
    results = await asyncio.gather(*task_coroutines, return_exceptions=True)

    for task_cfg, result in zip(tasks, results):
        if isinstance(result, Exception) and not isinstance(result, asyncio.CancelledError):
            logger.error(
                "Task '%s' exited with error: %s", task_cfg.get("name"), result
            )

    logger.info("All tasks finished. Daemon exiting.")


if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    _install_signal_handlers(loop)
    try:
        loop.run_until_complete(main())
    except asyncio.CancelledError:
        logger.info("Daemon shut down cleanly.")
    finally:
        loop.close()
