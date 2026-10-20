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
from telegram_bot import (
    deep_scan_requested_event,
    scan_requested_event,
    send_alert,
    send_system_message,
    start_telegram_polling,
    task_rescan_queue,
)

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
) -> bool:
    """
    Run the full pipeline for a single offer:
      1. Deduplicate per-task via SQLite.
      2. Download images.
      3. Send to Gemini for analysis.
      4. Alert via Telegram if it's a good deal.
      5. Persist the offer_id ONLY after successful AI evaluation.

    Returns:
        True if deal alert was sent, False otherwise.
    """
    offer_id = str(offer.get("id", ""))
    task_id = int(task.get("id", 0))
    task_name: str = task.get("name", "unknown")

    if not offer_id:
        return False

    # Step 1 — Per-task deduplication check
    if await is_offer_seen(offer_id, task_id):
        logger.debug("Offer %s already seen for task '%s' — skipping", offer_id, task_name)
        return False

    await record_scanned(1)
    logger.info("New offer [%s] '%s'", offer_id, offer.get("title", "")[:70])

    try:
        # Step 2 — Image download
        image_urls = extract_image_urls(offer)
        max_imgs = int(os.environ.get("MAX_OFFER_IMAGES", 8))
        image_bytes_list = await download_images(client, image_urls, max_images=max_imgs)

        # Step 3 — Gemini AI analysis
        ai_prompt: str = task.get("ai_prompt", "Evaluate this listing.")
        analysis = await analyze_offer(offer, image_bytes_list, ai_prompt)

        if analysis is None:
            logger.warning("AI analysis returned None for offer %s — leaving unseen to retry later", offer_id)
            return False

        # Step 4 — Mark as seen ONLY after successful AI evaluation!
        await mark_offer_seen(offer_id, task_id, task_name)

        if analysis.get("is_good_deal"):
            # Step 5 — Telegram alert
            logger.info("🔥 GOOD DEAL DETECTED for offer %s: '%s' (confidence: %d%%)",
                        offer_id, offer.get("title", "")[:50], analysis.get("confidence", 0))
            await send_alert(offer, analysis, task_name)
            return True
        else:
            logger.info(
                "Offer %s rejected by AI (confidence=%d%% reason=%s)",
                offer_id,
                analysis.get("confidence", 0),
                analysis.get("reason", "—")[:80],
            )
            return False

    except Exception as exc:  # noqa: BLE001
        logger.error("Unhandled error processing offer %s: %s", offer_id, exc)
        return False


# ── Per-Task Scan Routine ─────────────────────────────────────────────────────

async def scan_task(
    client: AsyncSession,
    task: dict[str, Any],
    deep_scan: bool = False,
) -> tuple[int, int]:
    """
    Execute a scan cycle for one active task.

    If deep_scan=True, scans all pages up to task's max_pages, ensuring
    every existing listing on the market is analyzed.
    If deep_scan=False (regular background monitoring), scans from newest to oldest
    and stops once reaching already indexed listings.

    Returns:
        tuple (new_offers_count, deals_count)
    """
    task_id = int(task.get("id", 0))
    task_name = task.get("name", "unnamed")
    task_max_pages = int(task.get("max_pages", 3))
    max_pages = max(task_max_pages, 5) if deep_scan else task_max_pages

    params: dict[str, Any] = {
        "query": task.get("query", ""),
        "currency": "UAH",
        "sort_by": "created_at:desc",
        "limit": 40,
    }
    cat_id = str(task.get("category_id", "0")).strip()
    if cat_id and cat_id != "0":
        params["category_id"] = cat_id

    if task.get("price_from"):
        params["filter_float_price:from"] = str(task["price_from"])
    if task.get("price_to"):
        params["filter_float_price:to"] = str(task["price_to"])

    mode_label = "ПОВНИЙ ПІДБІР" if deep_scan else "фоновий моніторинг"
    logger.info("→ Scanning task '%s' [%s] (query: '%s', cat: %s, price: %s-%s грн, max_pages: %d)",
                task_name, mode_label, params["query"], cat_id or "all", task.get("price_from", 0), task.get("price_to", 0), max_pages)

    new_offers_count = 0
    deals_count = 0

    for page in range(max_pages):
        page_params = dict(params)
        page_params["offset"] = page * 40

        offers = await fetch_offers(client, page_params, max_pages=1)
        if not offers:
            break

        logger.info("Task '%s' page %d: inspecting %d offers", task_name, page + 1, len(offers))

        new_on_this_page = 0
        for offer in offers:
            offer_id = str(offer.get("id", ""))
            if not offer_id:
                continue

            if await is_offer_seen(offer_id, task_id):
                continue

            new_on_this_page += 1
            new_offers_count += 1
            deal_found = await process_offer(client, offer, task)
            if deal_found:
                deals_count += 1

            # Courtesy pause between AI evaluations (prevents Gemini 429 rate limit spikes)
            await asyncio.sleep(1.5)

        # In regular monitoring, if an entire page has 0 new offers, older listings are already known
        if new_on_this_page == 0 and not deep_scan:
            logger.info("Task '%s': all %d offers on page %d already seen — scan up to date.", task_name, len(offers), page + 1)
            break

        # If OLX returned fewer offers than page limit, we reached the end of results
        if len(offers) < int(page_params.get("limit", 40)):
            break

        if page < max_pages - 1:
            await asyncio.sleep(2)

    logger.info("← Task '%s' scan finished. New offers processed: %d, deals found: %d", task_name, new_offers_count, deals_count)

    if deep_scan:
        await send_system_message(
            f"✅ <b>Підбір варіантів для «{task_name}» завершено!</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"🔍 Проаналізовано: <b>{new_offers_count}</b> оголошень\n"
            f"🔥 Знайдено вигідних угод: <b>{deals_count}</b>\n\n"
            f"<i>Бот продовжує моніторинг у фоні та сповістить про нові оголошення!</i>"
        )

    return new_offers_count, deals_count


# ── Continuous Dynamic Scheduler ──────────────────────────────────────────────

def _has_pending_triggers() -> bool:
    return bool(scan_requested_event.is_set() or deep_scan_requested_event.is_set() or len(task_rescan_queue) > 0)


async def run_scanner_loop() -> None:
    """
    Main background scheduler loop.

    Polls active tasks dynamically from SQLite database and listens
    for manual 'Scan Now' and 'Deep Scan' events from Telegram with zero-latency wakeup.
    """
    logger.info("OLX Hunter background scanner started.")
    last_scan_times: dict[int, float] = {}

    while True:
        try:
            tasks = await get_active_tasks()
            loop_now = asyncio.get_event_loop().time()

            force_deep_all = deep_scan_requested_event.is_set()
            if force_deep_all:
                deep_scan_requested_event.clear()
                logger.info("🚀 Full Deep Scan requested for ALL active tasks!")

            force_scan = scan_requested_event.is_set()
            if force_scan:
                scan_requested_event.clear()
                logger.info("⚡ Immediate scan requested from Telegram! Active tasks: %d", len(tasks))

            rescan_tasks = set(task_rescan_queue)
            task_rescan_queue.clear()

            total_new_manual = 0
            total_deals_manual = 0

            if tasks:
                async with _build_http_client() as client:
                    for idx, task in enumerate(tasks):
                        t_id = task["id"]
                        poll_interval = int(task.get("poll_interval", 300))
                        last_time = last_scan_times.get(t_id, 0.0)

                        is_deep = force_deep_all or (t_id in rescan_tasks)
                        is_due = (loop_now - last_time >= poll_interval)

                        if is_deep or force_scan or is_due:
                            try:
                                if idx > 0:
                                    await asyncio.sleep(2)
                                n_new, n_deals = await scan_task(client, task, deep_scan=is_deep)
                                last_scan_times[t_id] = asyncio.get_event_loop().time()
                                if force_scan and not is_deep:
                                    total_new_manual += n_new
                                    total_deals_manual += n_deals
                            except Exception as exc:  # noqa: BLE001
                                logger.error("Scan failed for task '%s': %s", task.get("name"), exc)

                if force_scan and not force_deep_all and not rescan_tasks:
                    if total_new_manual == 0:
                        await send_system_message(
                            "⚡ <b>Перевірка завершена:</b> свіжих оголошень наразі немає.\n"
                            "<i>Усі наявні пропозиції вже перевірені. Бот моніторить OLX у фоні та сповістить щойно з'явиться нове оголошення!</i>"
                        )
                    else:
                        await send_system_message(
                            f"⚡ <b>Перевірка завершена:</b> знайдено {total_new_manual} нових оголошень, підійшло під критерії: {total_deals_manual}."
                        )

            # Responsive wait: sleep up to 10s or wake instantly on scan trigger
            for _ in range(20):
                if _has_pending_triggers():
                    break
                await asyncio.sleep(0.5)

        except asyncio.CancelledError:
            logger.info("Scanner loop cancelled.")
            break
        except Exception as exc:  # noqa: BLE001
            logger.error("Scheduler error: %s", exc)
            await asyncio.sleep(10)


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
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    _install_signal_handlers(loop)
    try:
        loop.run_until_complete(main())
    except (KeyboardInterrupt, asyncio.CancelledError):
        logger.info("Daemon shut down cleanly.")
    finally:
        loop.close()
