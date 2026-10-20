"""
main.py — Universal OLX.ua AI Scanner & Background Daemon.

Orchestrates:
- Dynamic SQLite-backed search tasks (fully manageable via Telegram).
- Chrome TLS impersonated HTTP polling via curl_cffi.
- Google Gemini multimodal analysis with structured JSON.
- Telegram interactive bot control panel and instant alerts.
- Graceful shutdown handling.
"""

import asyncio
import logging
import signal
import sys
from typing import Any

from curl_cffi.requests import AsyncSession
from dotenv import load_dotenv

from ai_analyzer import analyze_offer
from database import (
    get_active_tasks,
    init_db,
    is_offer_seen,
    mark_offer_seen,
    record_scanned,
)
from olx_client import download_images, extract_image_urls, fetch_offers
from telegram_bot import scan_requested_event, send_alert, start_telegram_polling

# ── Logging Setup ─────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("olx_hunter")


# ── HTTP Client Factory ───────────────────────────────────────────────────────

def _build_http_client() -> AsyncSession:
    """Return a curl_cffi async session with authentic Google Chrome TLS fingerprint."""
    headers = {
        "Accept-Language": "uk-UA,uk;q=0.9,en-US;q=0.8,en;q=0.7",
        "Referer": "https://www.olx.ua/",
    }
    return AsyncSession(impersonate="chrome124", headers=headers, timeout=30.0)


# ── Per-Offer Pipeline ────────────────────────────────────────────────────────

async def process_offer(
    client: AsyncSession,
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
    """
    offer_id = str(offer.get("id", ""))
    task_name: str = task.get("name", "unknown")

    if not offer_id:
        return

    # Step 1 — Deduplication check
    if await is_offer_seen(offer_id):
        logger.debug("Offer %s already seen — skipping", offer_id)
        return

    await record_scanned(1)
    logger.info("New offer [%s] '%s'", offer_id, offer.get("title", "")[:70])

    try:
        # Step 2 — Image download
        image_urls = extract_image_urls(offer)
        image_bytes_list = await download_images(client, image_urls, max_images=4)

        # Step 3 — Gemini AI analysis
        ai_prompt: str = task.get("ai_prompt", "Evaluate this listing.")
        analysis = await analyze_offer(offer, image_bytes_list, ai_prompt)

        if analysis is None:
            logger.warning("AI analysis returned None for offer %s", offer_id)
        elif analysis.get("is_good_deal"):
            # Step 4 — Telegram alert
            logger.info("🔥 GOOD DEAL DETECTED for offer %s (confidence: %d%%)", offer_id, analysis.get("confidence", 0))
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
        # Step 5 — Always mark as seen
        await mark_offer_seen(offer_id, task_name)


# ── Per-Task Scan Routine ─────────────────────────────────────────────────────

async def scan_task(client: AsyncSession, task: dict[str, Any]) -> None:
    """Execute a single scan cycle for one active task."""
    task_name = task.get("name", "unnamed")
    params: dict[str, Any] = {
        "query": task.get("query", ""),
        "currency": "UAH",
    }
    cat_id = str(task.get("category_id", "0"))
    if cat_id and cat_id != "0":
        params["category_id"] = cat_id

    if task.get("price_from"):
        params["filter_float_price:from"] = str(task["price_from"])
    if task.get("price_to"):
        params["filter_float_price:to"] = str(task["price_to"])

    logger.info("→ Scanning task '%s' (query: '%s')", task_name, params["query"])
    offers = await fetch_offers(client, params, max_pages=3)
    logger.info("← Task '%s': fetched %d total offers", task_name, len(offers))

    for offer in offers:
        await process_offer(client, offer, task)


# ── Continuous Dynamic Scheduler ──────────────────────────────────────────────

async def run_scanner_loop() -> None:
    """
    Main background scheduler loop.

    Polls active tasks dynamically from SQLite database and listens
    for manual 'Scan Now' events from Telegram.
    """
    logger.info("OLX Hunter background scanner started.")
    last_scan_times: dict[int, float] = {}

    while True:
        try:
            tasks = await get_active_tasks()
            loop_now = asyncio.get_event_loop().time()
            force_scan = scan_requested_event.is_set()

            if force_scan:
                scan_requested_event.clear()
                logger.info("⚡ Immediate scan requested from Telegram!")

            async with _build_http_client() as client:
                for idx, task in enumerate(tasks):
                    t_id = task["id"]
                    poll_interval = int(task.get("poll_interval", 300))
                    last_time = last_scan_times.get(t_id, 0.0)

                    # Scan if due or if manual scan was triggered
                    if force_scan or (loop_now - last_time >= poll_interval):
                        try:
                            # Stagger between tasks by 3s to be polite
                            if idx > 0:
                                await asyncio.sleep(3)
                            await scan_task(client, task)
                            last_scan_times[t_id] = asyncio.get_event_loop().time()
                        except Exception as exc:  # noqa: BLE001
                            logger.error("Scan failed for task '%s': %s", task.get("name"), exc)

            # Sleep short duration to stay responsive to Telegram triggers and timers
            await asyncio.sleep(10)

        except asyncio.CancelledError:
            logger.info("Scanner loop cancelled.")
            break
        except Exception as exc:  # noqa: BLE001
            logger.error("Scheduler error: %s", exc)
            await asyncio.sleep(15)


# ── Main Entry Point ──────────────────────────────────────────────────────────

async def main() -> None:
    """Bootstrap the daemon: load config, init DB, launch task loops and bot."""
    load_dotenv()
    await init_db()

    logger.info("OLX Hunter AI starting up...")

    # Run bot polling and scanner concurrently
    bot_task = asyncio.create_task(start_telegram_polling())
    scanner_task = asyncio.create_task(run_scanner_loop())

    try:
        await asyncio.gather(bot_task, scanner_task)
    except asyncio.CancelledError:
        logger.info("Main daemon received cancellation — shutting down cleanly.")


def _install_signal_handlers(loop: asyncio.AbstractEventLoop) -> None:
    """Register SIGINT / SIGTERM handlers."""
    def _handle_signal(sig: signal.Signals) -> None:  # type: ignore[name-defined]
        logger.info("Received signal %s — initiating graceful shutdown …", sig.name)
        for task in asyncio.all_tasks(loop):
            task.cancel()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _handle_signal, sig)


if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    _install_signal_handlers(loop)
    try:
        loop.run_until_complete(main())
    except asyncio.CancelledError:
        logger.info("Daemon shut down cleanly.")
    finally:
        loop.close()
