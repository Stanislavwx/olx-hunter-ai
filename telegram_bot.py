"""
telegram_bot.py — Pure async Telegram alerts via the Bot HTTP API.

Sends richly formatted HTML messages with an inline "View on OLX" button.
Uses httpx directly (no python-telegram-bot dependency) for minimal footprint.
"""

import logging
import os
from typing import Any

import httpx

logger = logging.getLogger(__name__)

TELEGRAM_API_BASE = "https://api.telegram.org/bot{token}/sendMessage"

# Confidence threshold above which we display a ⭐ star rating
HIGH_CONFIDENCE_THRESHOLD = 75


def _confidence_stars(confidence: int) -> str:
    """Convert 0-100 confidence to a visual star rating (max 5 ⭐)."""
    stars = round(confidence / 20)
    return "⭐" * stars + "☆" * (5 - stars)


def _build_message(
    offer: dict[str, Any],
    analysis: dict[str, Any],
    task_name: str,
) -> tuple[str, dict]:
    """
    Build the HTML message body and inline keyboard for a Telegram alert.

    Returns:
        Tuple of (html_text, reply_markup_dict).
    """
    title: str = offer.get("title", "N/A")
    url: str = offer.get("url", "#")
    price_str = "N/A"
    for p in offer.get("params", []):
        if p.get("key") == "price":
            price_str = p.get("value", {}).get("label", "N/A")
            break

    city: str = offer.get("location", {}).get("city", {}).get("name", "N/A")

    confidence: int = analysis.get("confidence", 0)
    specs: str = analysis.get("extracted_specs", "—")
    reason: str = analysis.get("reason", "—")
    stars = _confidence_stars(confidence)

    text = (
        f"🔥 <b>GOOD DEAL FOUND!</b> — <i>{task_name}</i>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📦 <b>{title}</b>\n"
        f"💰 <b>Price:</b> {price_str}\n"
        f"📍 <b>City:</b> {city}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🤖 <b>AI Confidence:</b> {confidence}% {stars}\n"
        f"🔩 <b>Specs:</b> <code>{specs}</code>\n"
        f"💡 <b>Reason:</b> {reason}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<i>Powered by OLX Hunter AI 🤖</i>"
    )

    reply_markup = {
        "inline_keyboard": [
            [{"text": "👁 View on OLX", "url": url}]
        ]
    }

    return text, reply_markup


async def send_alert(
    offer: dict[str, Any],
    analysis: dict[str, Any],
    task_name: str,
) -> bool:
    """
    Send a formatted Telegram alert for a confirmed good deal.

    Args:
        offer:     Raw OLX offer dict.
        analysis:  Parsed Gemini JSON response.
        task_name: Human-readable name from config.json for context.

    Returns:
        True on success, False on any error.
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        logger.error(
            "TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set — cannot send alert."
        )
        return False

    message_text, reply_markup = _build_message(offer, analysis, task_name)
    api_url = TELEGRAM_API_BASE.format(token=token)

    payload = {
        "chat_id": chat_id,
        "text": message_text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
        "reply_markup": reply_markup,
    }

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(api_url, json=payload)
            resp.raise_for_status()
            logger.info(
                "Telegram alert sent for offer '%s'",
                offer.get("title", "unknown")[:60],
            )
            return True

    except httpx.HTTPStatusError as exc:
        logger.error(
            "Telegram API returned HTTP %d: %s",
            exc.response.status_code,
            exc.response.text,
        )
        return False
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to send Telegram alert: %s", exc)
        return False
