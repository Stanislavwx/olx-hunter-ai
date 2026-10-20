"""
telegram_bot.py — Interactive Telegram Bot management dashboard & deal notifier.

Features:
- Single-message clean in-place navigation (uses editMessageText to prevent chat clutter).
- Complete task management: view, pause/resume, edit AI prompts, edit prices, edit queries, delete.
- Wizard for adding new search tasks directly from Telegram chat.
- Live system settings: confidence threshold (60%/75%/90%) and language (UA/EN).
- Immediate manual scan trigger.
- HTML deal alerts with inline "View on OLX" buttons.
"""

import asyncio
import logging
import os
import re
from typing import Any

import httpx

from database import (
    add_task,
    clear_all_seen_offers,
    clear_seen_offers_for_task,
    delete_task,
    get_all_tasks,
    get_seen_offers_count,
    get_setting,
    get_stats,
    get_task_by_id,
    record_deal,
    set_setting,
    toggle_task,
    update_task_prices,
    update_task_prompt,
    update_task_query,
)

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"

# FSM state: chat_id -> {"state": str, "task_id": int | None, "prompt_msg_id": int | None}
_user_fsm: dict[int, dict[str, Any]] = {}

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
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(url, json=payload)
            data = resp.json()
            if not data.get("ok"):
                desc = str(data.get("description", ""))
                # Telegram returns 400 'message is not modified' when user clicks button resulting in identical state
                if "message is not modified" not in desc:
                    logger.warning("Telegram API call '%s' returned error: %s", method, desc)
            return data
    except Exception as exc:  # noqa: BLE001
        logger.error("Telegram API network error (%s): %s", method, exc)
        return {}


async def _edit_or_send(
    chat_id: int,
    text: str,
    reply_markup: dict[str, Any] | None = None,
    message_id: int | None = None,
) -> None:
    """Edit current message in-place if message_id is provided, else send new."""
    if message_id:
        res = await _api_call("editMessageText", {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": "HTML",
            "reply_markup": reply_markup or {},
            "disable_web_page_preview": True,
        })
        if res.get("ok"):
            return
        # If edit failed (e.g. text identical or deleted), fallback to send
        if "message is not modified" in str(res.get("description", "")):
            return

    await _api_call("sendMessage", {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "reply_markup": reply_markup or {},
        "disable_web_page_preview": True,
    })


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


# ── Interactive Screens ───────────────────────────────────────────────────────

async def show_main_menu(chat_id: int, message_id: int | None = None) -> None:
    """Main dashboard screen."""
    stats = await get_stats()
    tasks = await get_all_tasks()
    active_cnt = sum(1 for t in tasks if t.get("is_active"))
    min_conf = await get_setting("min_confidence", "70")
    lang = await get_setting("language", "ua")
    seen_cnt = await get_seen_offers_count()

    text = (
        "🤖 <b>OLX Hunter AI — Панель керування</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"🟢 <b>Активних завдань:</b> {active_cnt} з {len(tasks)}\n"
        f"🔥 <b>Знайдено вигідних угод:</b> {stats.get('deals_found', 0)}\n"
        f"🔍 <b>Проскановано оголошень:</b> {stats.get('scanned_total', 0)}\n"
        f"🗄 <b>В базі переглянутих:</b> {seen_cnt} шт\n"
        f"⚙️ <b>Поріг ШІ:</b> {min_conf}% | <b>Мова:</b> {'Українська 🇺🇦' if lang == 'ua' else 'English 🇬🇧'}\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "Оберіть потрібний розділ:"
    )

    keyboard = {
        "inline_keyboard": [
            [
                {"text": f"📋 Мої завдання ({active_cnt}/{len(tasks)})", "callback_data": "menu_tasks"},
            ],
            [
                {"text": "➕ Додати завдання", "callback_data": "menu_add"},
                {"text": "⚡ Сканувати зараз", "callback_data": "menu_scan"},
            ],
            [
                {"text": "📊 Статистика", "callback_data": "menu_stats"},
                {"text": "⚙️ Налаштування", "callback_data": "menu_settings"},
            ],
        ]
    }
    await _edit_or_send(chat_id, text, keyboard, message_id)


async def show_tasks_menu(chat_id: int, message_id: int | None = None) -> None:
    """List of all tasks as interactive selectable buttons."""
    tasks = await get_all_tasks()
    if not tasks:
        text = (
            "📭 <b>У вас немає жодного збереженого завдання.</b>\n\n"
            "Натисніть кнопку нижче, щоб додати перше завдання для сканування OLX:"
        )
        keyboard = {
            "inline_keyboard": [
                [{"text": "➕ Додати перше завдання", "callback_data": "menu_add"}],
                [{"text": "🔙 Головне меню", "callback_data": "menu_main"}],
            ]
        }
        await _edit_or_send(chat_id, text, keyboard, message_id)
        return

    text = (
        "📋 <b>Ваші пошукові завдання:</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "Натисніть на будь-яке завдання, щоб <b>переглянути деталі</b>, <b>змінити промпт</b>, <b>ціну</b> чи <b>призупинити</b>:\n"
    )

    buttons = []
    for t in tasks:
        icon = "🟢" if t["is_active"] else "⏸"
        price = f"{t['price_from']}–{t['price_to']} грн" if (t['price_from'] or t['price_to']) else "будь-яка ціна"
        btn_title = f"{icon} {t['name']} ({price})"
        buttons.append([{"text": btn_title, "callback_data": f"task_view_{t['id']}"}])

    buttons.append([
        {"text": "➕ Додати нове", "callback_data": "menu_add"},
        {"text": "🔙 Головне меню", "callback_data": "menu_main"},
    ])

    await _edit_or_send(chat_id, text, {"inline_keyboard": buttons}, message_id)


async def show_task_detail(chat_id: int, task_id: int, message_id: int | None = None) -> None:
    """Detailed view and live editor for a single task."""
    task = await get_task_by_id(task_id)
    if not task:
        await show_tasks_menu(chat_id, message_id)
        return

    task_seen = await get_seen_offers_count(task_id)

    status_str = "🟢 Працює (активне)" if task["is_active"] else "⏸ На паузі (призупинено)"
    toggle_label = "⏸ Призупинити" if task["is_active"] else "▶️ Запустити"

    price_str = "Без обмежень"
    if task["price_from"] or task["price_to"]:
        price_str = f"від <b>{task['price_from']}</b> до <b>{task['price_to']}</b> грн"

    text = (
        f"🎯 <b>Завдання:</b> {task['name']}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🔎 <b>Пошуковий запит:</b> <code>{task['query']}</code>\n"
        f"📂 <b>Категорія OLX:</b> {task['category_id']}\n"
        f"💰 <b>Ціна:</b> {price_str}\n"
        f"⏱ <b>Інтервал:</b> {task['poll_interval']} сек\n"
        f"📊 <b>Статус:</b> <b>{status_str}</b>\n"
        f"🗄 <b>В базі для цього завдання:</b> {task_seen} оголошень\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🧠 <b>Промпт для ШІ Gemini:</b>\n"
        f"<i>{task['ai_prompt']}</i>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Оберіть дію:"
    )

    keyboard = {
        "inline_keyboard": [
            [
                {"text": toggle_label, "callback_data": f"task_toggle_{task_id}"},
                {"text": "🔄 Пересканувати", "callback_data": f"task_rescan_{task_id}"},
            ],
            [
                {"text": "✏️ Змінити промпт", "callback_data": f"task_edit_prompt_{task_id}"},
                {"text": "💰 Змінити ціну", "callback_data": f"task_edit_price_{task_id}"},
            ],
            [
                {"text": "🔍 Змінити пошуковий запит", "callback_data": f"task_edit_query_{task_id}"},
                {"text": "🗑 Видалити", "callback_data": f"task_delete_{task_id}"},
            ],
            [
                {"text": "🔙 До списку завдань", "callback_data": "menu_tasks"},
                {"text": "🏠 Головне меню", "callback_data": "menu_main"},
            ],
        ]
    }
    await _edit_or_send(chat_id, text, keyboard, message_id)


async def show_settings_menu(chat_id: int, message_id: int | None = None) -> None:
    """Settings menu in single editable message."""
    min_conf = await get_setting("min_confidence", "70")
    lang = await get_setting("language", "ua")
    seen_cnt = await get_seen_offers_count()

    text = (
        "⚙️ <b>Налаштування системи OLX Hunter AI</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"🎯 <b>Мінімальна впевненість ШІ:</b> {min_conf}%\n"
        f"<i>(Сповіщення надсилатимуться тільки коли оцінка ШІ ≥ {min_conf}%)</i>\n\n"
        f"🌐 <b>Мова сповіщень та аналізу:</b> {'Українська 🇺🇦' if lang == 'ua' else 'English 🇬🇧'}\n"
        f"🗄 <b>Всього оголошень у базі:</b> {seen_cnt} шт\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "Натисніть кнопку, щоб змінити налаштування:"
    )
    keyboard = {
        "inline_keyboard": [
            [
                {"text": f"{'✅ ' if min_conf == '60' else ''}Поріг 60%", "callback_data": "set_conf_60"},
                {"text": f"{'✅ ' if min_conf == '75' else ''}Поріг 75%", "callback_data": "set_conf_75"},
                {"text": f"{'✅ ' if min_conf == '90' else ''}Поріг 90%", "callback_data": "set_conf_90"},
            ],
            [
                {"text": f"{'✅ ' if lang == 'ua' else ''}🇺🇦 Українська", "callback_data": "set_lang_ua"},
                {"text": f"{'✅ ' if lang == 'en' else ''}🇬🇧 English", "callback_data": "set_lang_en"},
            ],
            [
                {"text": "🗑 Очистити історію переглянутих", "callback_data": "menu_clear_cache"},
            ],
            [
                {"text": "🔙 Головне меню", "callback_data": "menu_main"},
            ],
        ]
    }
    await _edit_or_send(chat_id, text, keyboard, message_id)


async def show_stats_screen(chat_id: int, message_id: int | None = None) -> None:
    """Statistics view in single message."""
    stats = await get_stats()
    tasks = await get_all_tasks()
    active_cnt = sum(1 for t in tasks if t.get("is_active"))

    text = (
        "📊 <b>Статистика системи</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"🔎 <b>Всього проскановано оголошень:</b> {stats.get('scanned_total', 0)}\n"
        f"🔥 <b>Знайдено вигідних пропозицій:</b> {stats.get('deals_found', 0)}\n"
        f"📋 <b>Всього завдань:</b> {len(tasks)} (🟢 {active_cnt} активних, ⏸ {len(tasks) - active_cnt} на паузі)\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "Дані зберігаються в локальній базі SQLite."
    )
    keyboard = {
        "inline_keyboard": [
            [
                {"text": "⚡ Запустити сканування", "callback_data": "menu_scan"},
                {"text": "🔙 Головне меню", "callback_data": "menu_main"},
            ]
        ]
    }
    await _edit_or_send(chat_id, text, keyboard, message_id)


# ── Inbound Message Handling & Live Editing ───────────────────────────────────

async def handle_message(msg: dict[str, Any]) -> None:
    """Handle incoming text messages and FSM wizard steps."""
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

    fsm = _user_fsm.get(chat_id)

    # 1. Handle live editing of existing tasks
    if fsm:
        state = fsm.get("state")
        task_id = fsm.get("task_id")
        orig_msg_id = fsm.get("message_id")

        if state == "edit_prompt" and task_id:
            _user_fsm.pop(chat_id, None)
            await update_task_prompt(task_id, text)
            await _api_call("sendMessage", {
                "chat_id": chat_id,
                "text": f"✅ <b>Промпт для завдання #{task_id} успішно оновлено!</b>",
                "parse_mode": "HTML",
            })
            await show_task_detail(chat_id, task_id)
            return

        elif state == "edit_price" and task_id:
            _user_fsm.pop(chat_id, None)
            # Match formats: "5000-18000", "5000 18000", "5000 - 18000"
            nums = re.findall(r"\d+", text)
            if len(nums) >= 2:
                p_from, p_to = int(nums[0]), int(nums[1])
            elif len(nums) == 1:
                p_from, p_to = int(nums[0]), 0
            else:
                p_from, p_to = 0, 0

            await update_task_prices(task_id, p_from, p_to)
            await _api_call("sendMessage", {
                "chat_id": chat_id,
                "text": f"✅ <b>Ціну оновлено: {p_from} – {p_to} грн!</b>",
                "parse_mode": "HTML",
            })
            await show_task_detail(chat_id, task_id)
            return

        elif state == "edit_query" and task_id:
            _user_fsm.pop(chat_id, None)
            await update_task_query(task_id, text)
            await _api_call("sendMessage", {
                "chat_id": chat_id,
                "text": f"✅ <b>Пошуковий запит оновлено на:</b> <code>{text}</code>",
                "parse_mode": "HTML",
            })
            await show_task_detail(chat_id, task_id)
            return

        elif state == "add_task":
            _user_fsm.pop(chat_id, None)
            parts = [p.strip() for p in text.split("|")]
            if len(parts) < 5:
                await _api_call("sendMessage", {
                    "chat_id": chat_id,
                    "text": (
                        "❌ <b>Неправильний формат!</b> Потрібно 5 параметрів через <code>|</code>:\n"
                        "<code>Назва | Запит | Мін ціна | Макс ціна | Інструкція для ШІ</code>\n\n"
                        "Спробуйте знову через кнопку 'Додати завдання'."
                    ),
                    "parse_mode": "HTML",
                })
                await show_main_menu(chat_id)
                return

            name, query, p_from_str, p_to_str, prompt = parts[0], parts[1], parts[2], parts[3], parts[4]
            try:
                p_from = int(p_from_str)
                p_to = int(p_to_str)
            except ValueError:
                p_from, p_to = 0, 0

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
                "text": f"✅ <b>Завдання «{name}» успішно створено!</b>",
                "parse_mode": "HTML",
            })
            scan_requested_event.set()
            await show_task_detail(chat_id, new_id)
            return

    # 2. Command handling
    if text in ("/start", "/menu"):
        await show_main_menu(chat_id)
    elif text == "/tasks":
        await show_tasks_menu(chat_id)
    elif text == "/add":
        _user_fsm[chat_id] = {"state": "add_task"}
        await _api_call("sendMessage", {
            "chat_id": chat_id,
            "text": (
                "➕ <b>Створення нового завдання пошуку</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "Надішліть параметри одним повідомленням через роздільник <code>|</code>:\n\n"
                "<code>Назва | Запит | Мін ціна | Макс ціна | Інструкція для ШІ</code>\n\n"
                "💡 <b>Приклад:</b>\n"
                "<code>ThinkPad T14 | thinkpad t14 | 8000 | 16000 | Тільки цілий ноутбук. Мінімум 16GB RAM. Відхиляти батареї та запчастини.</code>"
            ),
            "parse_mode": "HTML",
        })
    elif text == "/scan":
        scan_requested_event.set()
        await _api_call("sendMessage", {
            "chat_id": chat_id,
            "text": "⚡ <b>Позачергове сканування ініційовано!</b>",
            "parse_mode": "HTML",
        })
    elif text == "/stats":
        await show_stats_screen(chat_id)
    else:
        await show_main_menu(chat_id)


async def handle_callback_query(cb: dict[str, Any]) -> None:
    """Handle button clicks with in-place message modification."""
    cb_id = cb.get("id")
    data = cb.get("data", "")
    message = cb.get("message", {})
    chat_id = message.get("chat", {}).get("id")
    msg_id = message.get("message_id")

    # Acknowledge button click with contextual feedback
    if data == "menu_scan":
        scan_requested_event.set()
        await _api_call("answerCallbackQuery", {
            "callback_query_id": cb_id,
            "text": "⚡ Сканування всіх завдань запущено!",
            "show_alert": False,
        })
    elif data.startswith("task_rescan_"):
        t_id = int(data.replace("task_rescan_", ""))
        cleared = await clear_seen_offers_for_task(t_id)
        scan_requested_event.set()
        await _api_call("answerCallbackQuery", {
            "callback_query_id": cb_id,
            "text": f"🔄 Скинуто {cleared} оголошень. Запущено пересканування!",
            "show_alert": False,
        })
    elif data == "menu_clear_cache":
        cleared = await clear_all_seen_offers()
        await _api_call("answerCallbackQuery", {
            "callback_query_id": cb_id,
            "text": f"🗑️ Очищено {cleared} оголошень з бази!\nНаступне сканування перевірить всі оголошення заново.",
            "show_alert": True,
        })
    else:
        await _api_call("answerCallbackQuery", {"callback_query_id": cb_id})

    if not chat_id:
        return

    # Clean existing FSM if user navigates away
    if data in ("menu_main", "menu_tasks", "menu_stats", "menu_settings"):
        _user_fsm.pop(chat_id, None)

    # 1. Navigation
    if data == "menu_main":
        await show_main_menu(chat_id, message_id=msg_id)

    elif data == "menu_tasks":
        await show_tasks_menu(chat_id, message_id=msg_id)

    elif data == "menu_stats":
        await show_stats_screen(chat_id, message_id=msg_id)

    elif data == "menu_settings" or data == "menu_clear_cache":
        await show_settings_menu(chat_id, message_id=msg_id)

    elif data == "menu_scan":
        await show_main_menu(chat_id, message_id=msg_id)

    elif data.startswith("task_rescan_"):
        t_id = int(data.replace("task_rescan_", ""))
        await show_task_detail(chat_id, t_id, message_id=msg_id)

    elif data == "menu_add":
        _user_fsm[chat_id] = {"state": "add_task", "message_id": msg_id}
        text = (
            "➕ <b>Створення нового завдання пошуку</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "Надішліть параметри одним повідомленням через роздільник <code>|</code>:\n\n"
            "<code>Назва | Запит | Мін ціна | Макс ціна | Інструкція для ШІ</code>\n\n"
            "💡 <b>Приклад:</b>\n"
            "<code>ThinkPad T14 | thinkpad t14 | 8000 | 16000 | Тільки цілий ноутбук. Мінімум 16GB RAM. Відхиляти батареї та запчастини.</code>"
        )
        keyboard = {"inline_keyboard": [[{"text": "❌ Скасувати", "callback_data": "menu_tasks"}]]}
        await _edit_or_send(chat_id, text, keyboard, message_id=msg_id)

    # 2. Task View & Toggle & Delete
    elif data.startswith("task_view_"):
        t_id = int(data.replace("task_view_", ""))
        await show_task_detail(chat_id, t_id, message_id=msg_id)

    elif data.startswith("task_toggle_"):
        t_id = int(data.replace("task_toggle_", ""))
        await toggle_task(t_id)
        await show_task_detail(chat_id, t_id, message_id=msg_id)

    elif data.startswith("task_delete_"):
        t_id = int(data.replace("task_delete_", ""))
        await delete_task(t_id)
        await show_tasks_menu(chat_id, message_id=msg_id)

    # 3. Live Task Editing
    elif data.startswith("task_edit_prompt_"):
        t_id = int(data.replace("task_edit_prompt_", ""))
        task = await get_task_by_id(t_id)
        if not task:
            await show_tasks_menu(chat_id, message_id=msg_id)
            return

        _user_fsm[chat_id] = {"state": "edit_prompt", "task_id": t_id, "message_id": msg_id}
        text = (
            f"✏️ <b>Редагування промпту для «{task['name']}»</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"<b>Поточний промпт:</b>\n"
            f"<i>{task['ai_prompt']}</i>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"👇 <b>Надішліть новий текст інструкції для ШІ у відповідь:</b>"
        )
        keyboard = {"inline_keyboard": [[{"text": "❌ Скасувати", "callback_data": f"task_view_{t_id}"}]]}
        await _edit_or_send(chat_id, text, keyboard, message_id=msg_id)

    elif data.startswith("task_edit_price_"):
        t_id = int(data.replace("task_edit_price_", ""))
        task = await get_task_by_id(t_id)
        if not task:
            await show_tasks_menu(chat_id, message_id=msg_id)
            return

        _user_fsm[chat_id] = {"state": "edit_price", "task_id": t_id, "message_id": msg_id}
        text = (
            f"💰 <b>Зміна діапазону цін для «{task['name']}»</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Поточна ціна: <b>{task['price_from']} – {task['price_to']} грн</b>\n\n"
            f"👇 <b>Введіть новий діапазон через дефіс:</b>\n"
            f"<i>(наприклад: <code>5000-18000</code> або <code>0-10000</code>)</i>"
        )
        keyboard = {"inline_keyboard": [[{"text": "❌ Скасувати", "callback_data": f"task_view_{t_id}"}]]}
        await _edit_or_send(chat_id, text, keyboard, message_id=msg_id)

    elif data.startswith("task_edit_query_"):
        t_id = int(data.replace("task_edit_query_", ""))
        task = await get_task_by_id(t_id)
        if not task:
            await show_tasks_menu(chat_id, message_id=msg_id)
            return

        _user_fsm[chat_id] = {"state": "edit_query", "task_id": t_id, "message_id": msg_id}
        text = (
            f"🔍 <b>Зміна пошукового запиту для «{task['name']}»</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Поточний запит: <code>{task['query']}</code>\n\n"
            f"👇 <b>Надішліть новий пошуковий рядок:</b>"
        )
        keyboard = {"inline_keyboard": [[{"text": "❌ Скасувати", "callback_data": f"task_view_{t_id}"}]]}
        await _edit_or_send(chat_id, text, keyboard, message_id=msg_id)

    # 4. Settings Toggles
    elif data.startswith("set_conf_"):
        val = data.replace("set_conf_", "")
        await set_setting("min_confidence", val)
        await show_settings_menu(chat_id, message_id=msg_id)

    elif data.startswith("set_lang_"):
        lang = data.replace("set_lang_", "")
        await set_setting("language", lang)
        await show_settings_menu(chat_id, message_id=msg_id)


# ── Long Polling Loop ─────────────────────────────────────────────────────────

async def start_telegram_polling() -> None:
    """Infinite async long polling loop for Telegram bot commands & buttons."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        logger.error("TELEGRAM_BOT_TOKEN not configured — bot polling disabled.")
        return

    logger.info("Telegram Bot Polling loop active.")
    offset = 0

    while True:
        try:
            url = TELEGRAM_API.format(token=token, method="getUpdates")
            payload = {"offset": offset, "timeout": 20}

            async with httpx.AsyncClient(timeout=30.0) as client:
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
            logger.info("Telegram polling loop cancelled.")
            break
        except Exception as exc:  # noqa: BLE001
            err_str = str(exc)
            if "name resolution" in err_str or "gaierror" in err_str or "ConnectError" in type(exc).__name__:
                logger.warning("DNS/network resolution temporary issue in Telegram polling: %s. Retrying in 10s...", exc)
                await asyncio.sleep(10)
            else:
                logger.error("Error in Telegram polling: %s", exc)
                await asyncio.sleep(3)
