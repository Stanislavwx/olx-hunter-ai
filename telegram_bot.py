"""
telegram_bot.py — Pure async Telegram alerts via the Bot HTTP API.

Sends richly formatted HTML messages with an inline "View on OLX" button.
Supports configurable language (UA / EN) via the ALERT_LANGUAGE environment variable.
"""

import logging
import os
from typing import Any

import httpx

logger = logging.getLogger(__name__)

TELEGRAM_API_BASE = "https://api.telegram.org/bot{token}/sendMessage"

# Localization dictionaries
MESSAGES = {
    "ua": {
        "header": "🔥 <b>ВИГІДНА ПРОПОЗИЦІЯ!</b> — <i>{task_name}</i>",
        "price": "💰 <b>Ціна:</b> {price}",
        "city": "📍 <b>Місто:</b> {city}",
        "confidence": "🤖 <b>Впевненість ШІ:</b> {confidence}% {stars}",
        "specs": "🔩 <b>Характеристики:</b> <code>{specs}</code>",
        "reason": "💡 <b>Причина:</b> {reason}",
        "footer": "<i>Працює на OLX Hunter AI 🤖</i>",
        "btn_view": "👁 Переглянути на OLX",
    },
    "en": {
        "header": "🔥 <b>GOOD DEAL FOUND!</b> — <i>{task_name}</i>",
        "price": "💰 <b>Price:</b> {price}",
        "city": "📍 <b>City:</b> {city}",
        "confidence": "🤖 <b>AI Confidence:</b> {confidence}% {stars}",
        "specs": "🔩 <b>Specs:</b> <code>{specs}</code>",
        "reason": "💡 <b>Reason:</b> {reason}",
        "footer": "<i>Powered by OLX Hunter AI 🤖</i>",
        "btn_view": "👁 View on OLX",
    },
}


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
    Build the HTML message body and inline keyboard for a Telegram alert
    in the configured language (ALERT_LANGUAGE=ua or en).
    """
    lang = os.environ.get("ALERT_LANGUAGE", "ua").strip().lower()
    texts = MESSAGES.get(lang, MESSAGES["ua"])

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
        f"{texts['header'].format(task_name=task_name)}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📦 <b>{title}</b>\n"
        f"{texts['price'].format(price=price_str)}\n"
        f"{texts['city'].format(city=city)}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{texts['confidence'].format(confidence=confidence, stars=stars)}\n"
        f"{texts['specs'].format(specs=specs)}\n"
        f"{texts['reason'].format(reason=reason)}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{texts['footer']}"
    )

    reply_markup = {
        "inline_keyboard": [
            [{"text": texts["btn_view"], "url": url}]
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
