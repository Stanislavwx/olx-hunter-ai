"""
olx_client.py — Async OLX.ua REST API client with Chrome TLS impersonation.

Uses `curl_cffi` to mimic Google Chrome's authentic TLS/JA3/JA4 fingerprint and HTTP/2
behavior, completely bypassing CloudFront and Akamai WAF 403 blocks.
"""

import asyncio
import logging
import random
from typing import Any

from curl_cffi.requests import AsyncSession

logger = logging.getLogger(__name__)

OLX_API_BASE = "https://www.olx.ua/api/v1/offers/"

# Delay range (seconds) between successive API calls — mimics human browsing
REQUEST_DELAY_MIN = 8
REQUEST_DELAY_MAX = 20

# Exponential-backoff parameters for HTTP 429
BACKOFF_BASE = 20
BACKOFF_MAX = 180
BACKOFF_FACTOR = 2


async def _sleep_between_requests() -> None:
    """Sleep a random interval between OLX API calls."""
    delay = random.uniform(REQUEST_DELAY_MIN, REQUEST_DELAY_MAX)
    logger.debug("Courtesy sleep between pages: %.1f s", delay)
    await asyncio.sleep(delay)


async def fetch_offers(
    client: AsyncSession,
    params: dict[str, Any],
    max_pages: int = 3,
) -> list[dict[str, Any]]:
    """
    Fetch up to *max_pages* of offers from the OLX API.

    Args:
        client:     Shared async curl_cffi session with Chrome impersonation.
        params:     OLX query parameters (category_id, query, price_from, …).
        max_pages:  Maximum number of result pages to retrieve per scan cycle.

    Returns:
        Flat list of raw offer dicts from the API.
    """
    all_offers: list[dict[str, Any]] = []
    base_params = dict(params)  # shallow copy
    base_params.setdefault("offset", 0)
    base_params.setdefault("limit", 40)
    base_params.setdefault("sort_by", "created_at:desc")

    start_offset = int(base_params.get("offset", 0))
    limit = int(base_params.get("limit", 40))

    for page in range(max_pages):
        base_params["offset"] = start_offset + (page * limit)
        backoff = BACKOFF_BASE

        while True:
            try:
                response = await client.get(OLX_API_BASE, params=base_params)

                if response.status_code in (403, 429):
                    logger.warning(
                        "OLX returned HTTP %d (rate-limit / bot protection). Detail: %s. Backing off %d s …",
                        response.status_code,
                        response.text[:180],
                        backoff,
                    )
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * BACKOFF_FACTOR, BACKOFF_MAX)
                    continue  # retry same page

                response.raise_for_status()
                data = response.json()
                offers = data.get("data", [])

                if not offers:
                    logger.debug("No more offers on page %d — stopping.", page + 1)
                    return all_offers

                all_offers.extend(offers)
                logger.info("Page %d: fetched %d offers", page + 1, len(offers))
                break  # success — move to next page

            except Exception as exc:  # noqa: BLE001
                logger.error("Error fetching OLX page %d: %s", page + 1, exc)
                return all_offers

        if page < max_pages - 1:
            await _sleep_between_requests()

    return all_offers


async def download_images(
    client: AsyncSession,
    image_urls: list[str],
    max_images: int = 8,
) -> list[bytes]:
    """
    Download up to *max_images* images, skipping broken links gracefully.

    Args:
        client:     Shared async session.
        image_urls: List of image URLs extracted from an offer.
        max_images: Hard cap to keep AI payloads small.

    Returns:
        List of raw image bytes (may be shorter than *image_urls* if some fail).
    """
    results: list[bytes] = []
    for url in image_urls[:max_images]:
        try:
            resp = await client.get(url, timeout=15.0)
            resp.raise_for_status()
            results.append(resp.content)
            logger.debug("Downloaded image (%d bytes) from %s", len(resp.content), url)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to download image %s — skipping. Reason: %s", url, exc)
    return results


def extract_image_urls(offer: dict[str, Any]) -> list[str]:
    """
    Pull all photo URLs out of an offer dict.

    OLX wraps images inside ``offer["photos"]`` as a list of objects,
    each containing a ``"link"`` template with ``{width}`` / ``{height}``
    placeholders that we resolve to a 720×540 size.
    """
    photos: list[dict] = offer.get("photos", [])
    urls: list[str] = []
    for photo in photos:
        link: str = photo.get("link", "")
        if link:
            urls.append(link.replace("{width}", "720").replace("{height}", "540"))
    return urls
