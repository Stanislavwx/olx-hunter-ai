"""
telegram_bot.py — Telegram bot alerting & interactive management control panel.

Features:
- Real-time HTML deal alerts with inline buttons.
- Interactive dashboard: manage tasks (list, toggle pause/resume, delete, add).
- Live settings: adjust confidence threshold and language on the fly.
- Real-time statistics counter.
- Pure async long polling via httpx — no bloated frameworks needed.
"""

import asyncio
import logging
import os
from typing import Any

import httpx

from database import (
    add_task,
    delete_task,
    get_all_tasks,
    get_setting,
    get_stats,
    record_deal,
    set_setting,
    toggle_task,
)

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"

# User input states for FSM (Finite State Machine)
_user_states: dict[int, str] = {}

# Event triggered when user clicks "Scan Now"
scan_requested_event = asyncio.Event()

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
    stars = round(confidence / 20)
    return "⭐" * stars + "☆" * (5 - stars)


async def _api_call(method: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Execute raw Telegram Bot API HTTP request."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        return {}
    url = TELEGRAM_API.format(token=token, method=method)
    try:
        async with httpx.AsyncClient(timeout=35.0) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            return resp.json()
    except Exception as exc:  # noqa: BLE001
        logger.error("Telegram API error (%s): %s", method, exc)
        return {}


# ── Alerts ────────────────────────────────────────────────────────────────────

async def send_alert(
    offer: dict[str, Any],
    analysis: dict[str, Any],
    task_name: str,
) -> bool:
    """Send deal alert if confidence satisfies the minimum threshold setting."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return False

    confidence: int = analysis.get("confidence", 0)
    min_conf_str = await get_setting("min_confidence", "70")
    try:
        min_conf = int(min_conf_str)
    except ValueError:
        min_conf = 70

    if confidence < min_conf:
        logger.info(
            "Confidence %d%% is below threshold (%d%%) — skipping alert for '%s'",
            confidence, min_conf, offer.get("title", "")[:50]
        )
        return False

    lang = await get_setting("language", os.environ.get("ALERT_LANGUAGE", "ua"))
    texts = MESSAGES.get(lang, MESSAGES["ua"])

    title: str = offer.get("title", "N/A")
    url: str = offer.get("url", "#")
    price_str = "N/A"
    for p in offer.get("params", []):
        if p.get("key") == "price":
            price_str = p.get("value", {}).get("label", "N/A")
            break

    city: str = offer.get("location", {}).get("city", {}).get("name", "N/A")
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

    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
        "reply_markup": {
            "inline_keyboard": [[{"text": texts["btn_view"], "url": url}]]
        },
    }

    res = await _api_call("sendMessage", payload)
    if res.get("ok"):
        await record_deal()
        return True
    return False


# ── Interactive UI Builders ───────────────────────────────────────────────────

def _get_main_menu_keyboard() -> dict[str, Any]:
    """Inline keyboard for main dashboard."""
    return {
        "inline_keyboard": [
            [
                {"text": "📋 Мої завдання", "callback_data": "menu_tasks"},
                {"text": "➕ Додати завдання", "callback_data": "menu_add"},
            ],
            [
                {"text": "⚡ Сканувати зараз", "callback_data": "menu_scan"},
                {"text": "📊 Статистика", "callback_data": "menu_stats"},
            ],
            [
                {"text": "⚙️ Налаштування", "callback_data": "menu_settings"},
            ],
        ]
    }


async def send_main_menu(chat_id: int) -> None:
    """Send or refresh main control panel."""
    stats = await get_stats()
    tasks = await get_all_tasks()
    active_cnt = sum(1 for t in tasks if t.get("is_active"))

    text = (
        "🤖 <b>OLX Hunter AI — Панель керування</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"🟢 <b>Активних завдань:</b> {active_cnt} з {len(tasks)}\n"
        f"🔥 <b>Знайдено угод:</b> {stats.get('deals_found', 0)}\n"
        f"🔍 <b>Всього перевірено:</b> {stats.get('scanned_total', 0)}\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "Оберіть дію нижче:"
    )
    await _api_call("sendMessage", {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "reply_markup": _get_main_menu_keyboard(),
    })


async def send_tasks_list(chat_id: int) -> None:
    """Render list of all configured search tasks with inline controls."""
    tasks = await get_all_tasks()
    if not tasks:
        await _api_call("sendMessage", {
            "chat_id": chat_id,
            "text": "📭 У вас ще немає збережених завдань.",
            "reply_markup": {"inline_keyboard": [[{"text": "➕ Додати перше завдання", "callback_data": "menu_add"}]]},
        })
        return

    for t in tasks:
        status_icon = "🟢 Працює" if t["is_active"] else "⏸ На паузі"
        toggle_btn_text = "⏸ Призупинити" if t["is_active"] else "▶️ Запустити"

        price_text = "Будь-яка"
        if t["price_from"] or t["price_to"]:
            price_text = f"{t['price_from']} – {t['price_to']} грн"

        msg = (
            f"🎯 <b>{t['name']}</b>\n"
            f"🔎 Запит: <code>{t['query']}</code>\n"
            f"💰 Ціна: <b>{price_text}</b>\n"
            f"⏱ Інтервал: <b>{t['poll_interval']} сек</b>\n"
            f"Статус: <b>{status_icon}</b>"
        )
        keyboard = {
            "inline_keyboard": [
                [
                    {"text": toggle_btn_text, "callback_data": f"task_toggle_{t['id']}"},
                    {"text": "🗑 Видалити", "callback_data": f"task_del_{t['id']}"},
                ]
            ]
        }
        await _api_call("sendMessage", {
            "chat_id": chat_id,
            "text": msg,
            "parse_mode": "HTML",
            "reply_markup": keyboard,
        })

    await _api_call("sendMessage", {
        "chat_id": chat_id,
        "text": "👆 Керуйте завданнями кнопками вище або поверніться назад:",
        "reply_markup": {
            "inline_keyboard": [
                [
                    {"text": "➕ Додати нове", "callback_data": "menu_add"},
                    {"text": "🔙 Головне меню", "callback_data": "menu_main"},
                ]
            ]
        },
    })


async def send_settings_menu(chat_id: int) -> None:
    """Show interactive settings dialog."""
    min_conf = await get_setting("min_confidence", "70")
    lang = await get_setting("language", "ua")

    text = (
        "⚙️ <b>Налаштування системи</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"🎯 <b>Мінімальна впевненість ШІ:</b> {min_conf}%\n"
        f"🌐 <b>Мова сповіщень:</b> {'Українська 🇺🇦' if lang == 'ua' else 'English 🇬🇧'}\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "Натисніть кнопку, щоб змінити:"
    )
    keyboard = {
        "inline_keyboard": [
            [
                {"text": "🎯 Поріг: 60%", "callback_data": "set_conf_60"},
                {"text": "🎯 Поріг: 75%", "callback_data": "set_conf_75"},
                {"text": "🎯 Поріг: 90%", "callback_data": "set_conf_90"},
            ],
            [
                {"text": "🇺🇦 Українська", "callback_data": "set_lang_ua"},
                {"text": "🇬🇧 English", "callback_data": "set_lang_en"},
            ],
            [
                {"text": "🔙 Головне меню", "callback_data": "menu_main"},
            ],
        ]
    }
    await _api_call("sendMessage", {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "reply_markup": keyboard,
    })


# ── Inbound Message Handling ──────────────────────────────────────────────────

async def handle_message(msg: dict[str, Any]) -> None:
    """Handle incoming text messages and commands."""
    chat = msg.get("chat", {})
    chat_id = chat.get("id")
    text = (msg.get("text") or "").strip()
    if not chat_id:
        return

    # Security check: only allow configured Telegram chat ID
    allowed_chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if str(chat_id) != allowed_chat_id:
        logger.warning("Unauthorized access attempt from chat_id %s", chat_id)
        return

    state = _user_states.get(chat_id)

    # 1. Handle FSM state for adding task
    if state == "waiting_task_input":
        _user_states.pop(chat_id, None)
        parts = [p.strip() for p in text.split("|")]
        if len(parts) < 5:
            await _api_call("sendMessage", {
                "chat_id": chat_id,
                "text": (
                    "❌ <b>Неправильний формат!</b>\n"
                    "Потрібно передати 5 полів через <code>|</code>:\n"
                    "<code>Назва | Запит | МінЦіна | МаксЦіна | Промпт для ШІ</code>\n\n"
                    "Спробуйте знову через кнопку 'Додати завдання'."
                ),
                "parse_mode": "HTML",
                "reply_markup": _get_main_menu_keyboard(),
            })
            return

        name, query, p_from_str, p_to_str, prompt = parts[0], parts[1], parts[2], parts[3], parts[4]
        try:
            p_from = int(p_from_str)
            p_to = int(p_to_str)
        except ValueError:
            p_from, p_to = 0, 0

        # Auto-category fallback (0 means all categories)
        new_id = await add_task(
            name=name,
            query=query,
            category_id="0",
            price_from=p_from,
            price_to=p_to,
            ai_prompt=prompt,
            poll_interval=300,
        )
        await _api_call("sendMessage", {
            "chat_id": chat_id,
            "text": (
                f"✅ <b>Завдання #{new_id} створено та активовано!</b>\n\n"
                f"📦 <b>{name}</b>\n"
                f"🔎 Запит: <code>{query}</code>\n"
                f"💰 Ціна: {p_from} – {p_to} грн\n"
                f"🤖 Промпт: <i>{prompt[:100]}…</i>"
            ),
            "parse_mode": "HTML",
            "reply_markup": _get_main_menu_keyboard(),
        })
        scan_requested_event.set()
        return

    # 2. Command handling
    if text in ("/start", "/menu"):
        await send_main_menu(chat_id)
    elif text == "/tasks":
        await send_tasks_list(chat_id)
    elif text == "/add":
        _user_states[chat_id] = "waiting_task_input"
        await _api_call("sendMessage", {
            "chat_id": chat_id,
            "text": (
                "➕ <b>Створення нового завдання пошуку</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "Надішліть параметри одним повідомленням через роздільник <code>|</code>:\n\n"
                "<code>Назва | Запит | Мін ціна | Макс ціна | Інструкція для ШІ</code>\n\n"
                "💡 <b>Приклад:</b>\n"
                "<code>MacBook Air M1 | macbook air m1 | 14000 | 22000 | Шукати цілий ноутбук. Мінімум 16GB RAM або батарея >85%. Відхиляти MDM, iCloud або тріснуті матриці.</code>"
            ),
            "parse_mode": "HTML",
        })
    elif text == "/scan":
        scan_requested_event.set()
        await _api_call("sendMessage", {
            "chat_id": chat_id,
            "text": "⚡ <b>Сканування запущено!</b> Результати надсилатимуться сюди.",
            "parse_mode": "HTML",
        })
    elif text == "/stats":
        stats = await get_stats()
        tasks = await get_all_tasks()
        await _api_call("sendMessage", {
            "chat_id": chat_id,
            "text": (
                "📊 <b>Статистика OLX Hunter AI</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                f"🔎 Перевірено лістингів: <b>{stats.get('scanned_total', 0)}</b>\n"
                f"🔥 Знайдено вигідних угод: <b>{stats.get('deals_found', 0)}</b>\n"
                f"📋 Всього завдань: <b>{len(tasks)}</b>"
            ),
            "parse_mode": "HTML",
            "reply_markup": _get_main_menu_keyboard(),
        })
    else:
        await send_main_menu(chat_id)


async def handle_callback_query(cb: dict[str, Any]) -> None:
    """Handle inline button clicks."""
    cb_id = cb.get("id")
    data = cb.get("data", "")
    message = cb.get("message", {})
    chat_id = message.get("chat", {}).get("id")

    # Acknowledge button click
    await _api_call("answerCallbackQuery", {"callback_query_id": cb_id})

    if not chat_id:
        return

    if data == "menu_main":
        await send_main_menu(chat_id)
    elif data == "menu_tasks":
        await send_tasks_list(chat_id)
    elif data == "menu_add":
        _user_states[chat_id] = "waiting_task_input"
        await _api_call("sendMessage", {
            "chat_id": chat_id,
            "text": (
                "➕ <b>Створення нового завдання пошуку</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "Надішліть параметри одним повідомленням через роздільник <code>|</code>:\n\n"
                "<code>Назва | Запит | Мін ціна | Макс ціна | Інструкція для ШІ</code>\n\n"
                "💡 <b>Приклад:</b>\n"
                "<code>RTX 3060 12GB | rtx 3060 | 6000 | 9500 | Шукати версію на 12GB. Перевіряти скріншоти FurMark. Відхиляти версії 8GB або після ремонту.</code>"
            ),
            "parse_mode": "HTML",
        })
    elif data == "menu_scan":
        scan_requested_event.set()
        await _api_call("sendMessage", {
            "chat_id": chat_id,
            "text": "⚡ <b>Позачергове сканування ініційовано!</b>",
            "parse_mode": "HTML",
        })
    elif data == "menu_stats":
        stats = await get_stats()
        tasks = await get_all_tasks()
        await _api_call("sendMessage", {
            "chat_id": chat_id,
            "text": (
                "📊 <b>Статистика OLX Hunter AI</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                f"🔎 Перевірено лістингів: <b>{stats.get('scanned_total', 0)}</b>\n"
                f"🔥 Знайдено вигідних угод: <b>{stats.get('deals_found', 0)}</b>\n"
                f"📋 Всього завдань: <b>{len(tasks)}</b>"
            ),
            "parse_mode": "HTML",
            "reply_markup": _get_main_menu_keyboard(),
        })
    elif data == "menu_settings":
        await send_settings_menu(chat_id)
    elif data.startswith("set_conf_"):
        val = data.replace("set_conf_", "")
        await set_setting("min_confidence", val)
        await _api_call("sendMessage", {
            "chat_id": chat_id,
            "text": f"✅ Поріг впевненості встановлено на <b>{val}%</b>",
            "parse_mode": "HTML",
        })
        await send_settings_menu(chat_id)
    elif data.startswith("set_lang_"):
        lang = data.replace("set_lang_", "")
        await set_setting("language", lang)
        await _api_call("sendMessage", {
            "chat_id": chat_id,
            "text": f"✅ Мову змінено на <b>{'Українська 🇺🇦' if lang == 'ua' else 'English 🇬🇧'}</b>",
            "parse_mode": "HTML",
        })
        await send_settings_menu(chat_id)
    elif data.startswith("task_toggle_"):
        t_id = int(data.replace("task_toggle_", ""))
        new_status = await toggle_task(t_id)
        status_text = "🟢 активовано" if new_status else "⏸ призупинено"
        await _api_call("sendMessage", {
            "chat_id": chat_id,
            "text": f"Завдання #{t_id} {status_text}!",
        })
    elif data.startswith("task_del_"):
        t_id = int(data.replace("task_del_", ""))
        await delete_task(t_id)
        await _api_call("sendMessage", {
            "chat_id": chat_id,
            "text": f"🗑 Завдання #{t_id} видалено!",
        })


# ── Long Polling Loop ─────────────────────────────────────────────────────────

async def start_telegram_polling() -> None:
    """Infinite async long polling loop for Telegram bot commands & buttons."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        logger.error("TELEGRAM_BOT_TOKEN not configured — bot polling disabled.")
        return

    logger.info("Telegram Bot Polling loop started.")
    offset = 0

    while True:
        try:
            url = TELEGRAM_API.format(token=token, method="getUpdates")
            payload = {"offset": offset, "timeout": 25}

            async with httpx.AsyncClient(timeout=35.0) as client:
                resp = await client.post(url, json=payload)
                if resp.status_code != 200:
                    await asyncio.sleep(3)
                    continue
                data = resp.json()

            updates = data.get("result", [])
            for upd in updates:
                offset = max(offset, upd["update_id"] + 1)
                if "message" in upd:
                    await handle_message(upd["message"])
                elif "callback_query" in upd:
                    await handle_callback_query(upd["callback_query"])

        except asyncio.CancelledError:
            logger.info("Telegram polling cancelled.")
            break
        except Exception as exc:  # noqa: BLE001
            logger.error("Error in Telegram polling: %s", exc)
            await asyncio.sleep(4)
