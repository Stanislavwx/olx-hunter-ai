# OLX Hunter AI 🤖

A production-ready asynchronous daemon and interactive Telegram bot that continuously scans OLX.ua listings with Chrome TLS impersonation, evaluates them with Google Gemini multimodal AI via structured JSON, and fires instant deal alerts.

---

## Architecture Overview

```
main.py               ← Async dynamic scheduler with instant Telegram wakeup
  ├── olx_client.py   ← Chrome TLS impersonation via curl_cffi (WAF bypass)
  ├── ai_analyzer.py  ← Gemini 3.1 multimodal analysis (official google.genai SDK)
  ├── telegram_bot.py ← Interactive in-place control panel & HTML deal notifier
  └── database.py     ← SQLite dynamic task CRUD, per-task deduplication & settings
```

- **Zero-Latency Triggers:** Polling scheduler wakes up instantly when manual scans or task rescans are requested from Telegram.
- **WAF Immunity:** Mimics Google Chrome's authentic TLS/JA3/JA4 fingerprint and HTTP/2 behavior via `curl_cffi`, completely bypassing CloudFront/Akamai 403 blocks.
- **Full Page Inspection:** Inspects all listings on each page, ensuring sponsored/top ads never mask fresh organic listings.
- **Per-Task Deduplication:** Composite key `(offer_id, task_id)` guarantees independent evaluation across distinct search tasks.
- **Interactive In-Place Telegram UI:** Single-message navigation via `editMessageText` (zero chat spam), task CRUD, live prompt & price editing, instant rescan, and cache clearing.

---

## Prerequisites

| Tool | Min Version | Recommended |
|------|-------------|-------------|
| Python | 3.11+ | 3.12 |
| Docker | 24+ | Latest |
| Docker Compose | 2.20+ | Latest |

---

## Quick Start (Docker — Recommended)

### 1. Clone and configure

```bash
git clone <repo-url> olx-hunter-ai
cd olx-hunter-ai
cp .env.example .env
```

### 2. Fill in `.env`

```dotenv
GEMINI_API_KEY=AIza...                  # from https://aistudio.google.com/app/apikey
GEMINI_MODEL_NAME=gemini-3.1-flash-lite-preview
TELEGRAM_BOT_TOKEN=123456:ABC-DEF...   # from @BotFather
TELEGRAM_CHAT_ID=123456789             # your chat ID
ALERT_LANGUAGE=ua                      # 'ua' or 'en'
```

> **Get your Chat ID:**  
> Send any message to your bot, then visit:  
> `https://api.telegram.org/bot<TOKEN>/getUpdates`  
> Look for `"chat": {"id": ...}` in the response.

### 3. Build and launch

```bash
sudo docker compose up -d --build
```

### 4. Check logs

```bash
sudo docker compose logs -f olx-hunter
```

---

## Quick Start (Local Python)

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in your keys
python main.py
```

---

## Interactive Telegram Bot Dashboard

The bot provides a full interactive control panel right inside Telegram:

* 🏠 **Main Dashboard:** Live overview of active tasks, deals found, total scanned, and database records.
* 🚀 **Full Market Selection («Повний підбір»):** Evaluates all existing listings across all pages on OLX against your AI instructions and sends every good deal directly to Telegram.
* ⚡ **Live Monitoring («Перевірити нові»):** Continuously monitors Page 1 in background and instantly alerts when a brand new matching listing is published on OLX.
* 📋 **Task Management:** View all tasks, toggle status (🟢 Active / ⏸ Paused), or delete tasks.
* 🖼️ **Photo Cards:** Sends listing photos directly in Telegram cards with formatted specifications and OLX button.
* ✏️ **Live Editing:**
  * **Edit AI Prompt:** Adjust expert instructions for any task directly in chat.
  * **Edit Price Range:** Change min/max budget bounds on the fly (e.g. `6000-15000`).
  * **Edit Search Query:** Update search keywords without touching code.
* 🔎 **Task Deep Search («Повний пошук»):** Resets seen history for a specific task and deep-scans all active market listings.
* ➕ **Add Task Wizard:** Add new search tasks via `/add` or the menu by sending a single line:
  `ThinkPad T14 | thinkpad t14 | 8000 | 16000 | Only complete laptop. Min 16GB RAM. Reject parts.`
* 🗑️ **Clear History Cache:** Reset seen listings database from the Settings menu.
* ⚙️ **Runtime Settings:** Toggle minimum AI confidence threshold (60% / 75% / 90%) and language (UA 🇺🇦 / EN 🇬🇧).

### Telegram Commands

| Command | Action |
|---------|--------|
| `/start` | Open the interactive main dashboard |
| `/tasks` | View and edit your search tasks |
| `/add` | Launch the new task wizard |
| `/scan` | Trigger an immediate manual scan |
| `/stats` | View scanning statistics |

---

## Configuration (`config.json`)

Initial default tasks are loaded from `config.json` into SQLite on startup and can be managed directly via Telegram afterwards:

```jsonc
{
  "tasks": [
    {
      "name": "SSD NVMe Hunter",
      "poll_interval_seconds": 300,
      "max_pages": 3,
      "olx_params": {
        "category_id": "78",               // Components -> Storage / SSD
        "query": "nvme",
        "filter_float_price:from": "1000",
        "filter_float_price:to": "4000",
        "currency": "UAH"
      },
      "ai_prompt": "Act as a sysadmin evaluating a used NVMe SSD... [firm anti-parts rules]"
    }
  ]
}
```

### Pre-configured Tasks

| Task | OLX Category | Price Range | Target Criteria |
|------|-------------|-------------|-----------------|
| **SSD NVMe Hunter** | Components / Storage (78) | 1 000–4 000 UAH | Samsung 980 Pro, PM9A1, KC3000, WD SN850. Auto-rejects adapters, heatsinks, nettops. |
| **PC Flipping Under 5k** | Desktop PCs (84) | 2 000–5 000 UAH | Full desktop PCs on AM4 / LGA1151v2 / LGA1200. Auto-rejects Windows keys, PSUs, cases. |
| **ThinkPad X1 Carbon Gen 7** | Laptops (80) | 5 000–18 000 UAH | Core i5/i7 8th gen, 16GB RAM, NVMe SSD. Auto-rejects batteries, screens, adapters. |

---

## How It Works

```
┌─ Every poll_interval_seconds (or instant Telegram trigger) ─┐
│  1. Fetch OLX listings with Chrome TLS impersonation       │
│  2. Inspect all offers on page (top ads don't block scan)  │
│  3. For each unseen offer:                                  │
│     a. Download up to 3 high-res images                     │
│     b. Send metadata + photos to Gemini 3.1 Flash Lite      │
│     c. Enforce strict JSON output schema                    │
│     d. If is_good_deal=true & conf >= threshold:            │
│        → Send rich HTML Telegram alert with OLX button      │
│     e. Record (offer_id, task_id) in SQLite                 │
└─────────────────────────────────────────────────────────────┘
```

### Structured AI Output Schema

```json
{
  "is_good_deal": true,
  "confidence": 90,
  "extracted_specs": "Lenovo ThinkPad X1 Carbon Gen 7 | Intel Core i7-8665U | 16GB RAM | 512GB NVMe | FHD IPS | Стан 9/10",
  "reason": "Ціна 13 000 грн нижча за ринкову для конфігурації з i7 та 16GB RAM у відмінному стані."
}
```

---

## Telegram Alert Example

```
🔥 ВИГІДНА ПРОПОЗИЦІЯ! — ThinkPad X1 Carbon Gen 7
━━━━━━━━━━━━━━━━━━━━
📦 Lenovo ThinkPad X1 Carbon Gen 7 i7 16GB 512GB
💰 Ціна: 13 000 грн
📍 Місто: Київ
━━━━━━━━━━━━━━━━━━━━
🤖 Впевненість ШІ: 90% ⭐⭐⭐⭐☆
🔩 Характеристики: Intel Core i7-8665U, 16GB RAM, 512GB SSD, FHD IPS, знос АКБ 15%
💡 Причина: Ціна на 20% нижча середньоринкової для топової конфігурації у збереженому стані.
━━━━━━━━━━━━━━━━━━━━
<i>Працює на OLX Hunter AI 🤖</i>

[ 👁 Переглянути на OLX ]
```

---

## Resilience & Production Features

| Feature | Implementation Details |
|---------|------------------------|
| **WAF & CloudFront Bypass** | `curl_cffi` Chrome 124 TLS/JA3/JA4 fingerprinting |
| **DNS Stability** | Explicit Google (`8.8.8.8`) & Cloudflare (`1.1.1.1`) resolvers in Docker |
| **Exponential Backoff** | Automatic retry on HTTP 429/503 spikes for both OLX and Gemini API |
| **In-Place Navigation** | Edits messages in-place via Telegram API without polluting the chat |
| **Non-blocking Paging** | Checks all offers on page 1 without premature break on sponsored ads |
| **Container Security** | Non-root `appuser`, read-only templates, isolated SQLite volume |
| **Resource Limits** | Capped at 512 MB RAM / 0.5 CPU in `docker-compose.yml` |

---

## Stopping & Resetting

```bash
# Stop daemon (SQLite database is preserved in Docker volume)
sudo docker compose down

# Stop and wipe database volume (resets seen history)
sudo docker compose down -v
```

---

---

# 🇺🇦 OLX Hunter AI — Документація українською

Повнофункціональний асинхронний демон та інтерактивний Telegram-бот для безперервного моніторингу OLX.ua. Знаходить нові лістинги в обхід WAF-захисту, глибоко аналізує їх за допомогою мультимодального ШІ Google Gemini та надсилає детальні сповіщення про вигідні пропозиції з кнопкою перегляду.

---

## 🏗️ Архітектура системи

```
main.py               ← Асинхронний планувальник з миттєвим пробудженням за подією
  ├── olx_client.py   ← HTTP-клієнт curl_cffi з емуляцією TLS-відбитка Google Chrome
  ├── ai_analyzer.py  ← Мультимодальний аналіз Gemini 3.1 (офіційний google.genai SDK)
  ├── telegram_bot.py ← Інтерактивна панель керування в один екран та HTML-сповіщення
  └── database.py     ← SQLite: динамічні завдання (CRUD), налаштування та історія
```

* **Миттєва реакція:** Сканування запускається негайно (0 мс затримки) при натисканні кнопки в Telegram.
* **Обхід CloudFront WAF:** Використання `curl_cffi` з автентичним відбитком Chrome 124 гарантує статус 200 OK без блокувань 403.
* **Повна перевірка сторінки:** Сканер переглядає всі 40–50 пропозицій сторінки. Платні рекламні оголошення (`top_ad`) більше ніколи не зупиняють пошук свіжих пропозицій.
* **Роздільна історія:** Складений ключ `(offer_id, task_id)` відстежує переглянуті лістинги окремо для кожного завдання.
* **Керування прямо з бота:** Редагування промптів, цін, запитів, пауза, додавання нових завдань та очищення бази без перезапуску контейнера.

---

## ⚡ Швидкий запуск (Docker — рекомендовано)

### 1. Клонування та налаштування оточення

```bash
git clone <repo-url> olx-hunter-ai
cd olx-hunter-ai
cp .env.example .env
```

### 2. Заповнення `.env`

```dotenv
GEMINI_API_KEY=AIza...                  # отримати на https://aistudio.google.com/app/apikey
GEMINI_MODEL_NAME=gemini-3.1-flash-lite-preview
TELEGRAM_BOT_TOKEN=123456:ABC-DEF...   # отримати від @BotFather
TELEGRAM_CHAT_ID=123456789             # ваш Telegram ID
ALERT_LANGUAGE=ua                      # 'ua' (українська) або 'en'
```

> **Як дізнатися свій Chat ID:**  
> Напишіть будь-яке повідомлення вашому боту, потім перейдіть за посиланням:  
> `https://api.telegram.org/bot<TOKEN>/getUpdates`  
> Знайдіть `"chat": {"id": ...}` у відповіді.

### 3. Збірка та запуск

```bash
sudo docker compose up -d --build
```

### 4. Перегляд логів роботи

```bash
sudo docker compose logs -f olx-hunter
```

---

## 🐍 Локальний запуск (Python)

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # заповніть API-ключі
python main.py
```

---

## 📱 Інтерактивне керування через Telegram-бот

Бот працює як повноцінна панель управління прямо в чаті, замінюючи поточне повідомлення замість спаму:

* 🏠 **Головне меню:** Статус завдань, кількість знайдених угод, перевірених оголошень та записів у базі.
* 🚀 **Повний підбір (знайти всі наявні):** Сканує абсолютно всі сторінки OLX за вашими завданнями, аналізує всі пропозиції через ШІ та надсилає всі вигідні варіанти в чат.
* ⚡ **Фоновий моніторинг («Перевірити нові»):** Тихо стежить за ринком у фоновому режимі та надсилає сповіщення негайно, щойно на OLX з'являється нове оголошення.
* 📋 **Список завдань:** Перегляд деталей кожного пошуку, призупинення (⏸) та активація (🟢), видалення.
* 🖼️ **Картки з реальними фото:** Бот надсилає картки пропозицій із фотографіями товару, характеристиками, висновком ШІ та прямим посиланням на OLX.
* ✏️ **Редагування на льоту:**
  * **Змінити промпт:** Надішліть новий текст інструкцій для ШІ безпосередньо у відповідь боту.
  * **Змінити діапазон цін:** Введіть нові межі (наприклад: `7000-14000`).
  * **Змінити пошуковий запит:** Змініть ключові слова пошуку.
* 🔎 **Повний пошук (знайти всі):** Скидає пам'ять саме для вибраного завдання і проводить повний підбір усіх наявних варіантів з OLX.
* ➕ **Майстер створення завдань:** Команда `/add` або кнопка «Додати завдання» — формат в один рядок:
  `ThinkPad T14 | thinkpad t14 | 8000 | 16000 | Тільки цілий ноутбук. Від 16GB RAM. Без запчастин.`
* 🗑️ **Очистити пам'ять оголошень:** Повне скидання бази переглянутих у меню налаштувань.
* ⚙️ **Налаштування:** Зміна порогу впевненості ШІ (60% / 75% / 90%) та мови (🇺🇦 / 🇬🇧).

### Доступні команди бота

| Команда | Опис |
|---------|------|
| `/start` | Відкрити головне меню панелі керування |
| `/tasks` | Список усіх завдань та їх редагування |
| `/add` | Створити нове пошукове завдання |
| `/scan` | Запустити позачергове сканування |
| `/stats` | Переглянути статистику роботи системи |

---

## 🎯 Налаштовані завдання (`config.json`)

Завдання за замовчуванням завантажуються в SQLite під час першого старту:

| Завдання | Категорія OLX | Ціновий діапазон | Критерії та правила відбору |
|----------|---------------|------------------|-----------------------------|
| **SSD NVMe Hunter** | Комплектуючі / Накопичувачі (78) | 1 000–4 000 грн | Samsung 980 Pro, PM9A1, KC3000, WD SN850. Перевірка CrystalDiskInfo. Відхилення адаптерів, радіаторів, неттопів. |
| **PC Flipping Under 5k** | Системні блоки (84) | 2 000–5 000 грн | ПК на AM4 / LGA1151v2 / LGA1200 з DDR4. Відхилення ключів Windows, старих сокетів, порожніх корпусів. |
| **ThinkPad X1 Carbon Gen 7** | Ноутбуки (80) | 5 000–18 000 грн | Core i5/i7 8-го покоління, 16GB RAM, NVMe. **Суворе відхилення батарей, екранів, зарядок та запчастин.** |

---

## 🛡️ Надійність та стабільність

* **Захист від DNS-збоїв:** У `docker-compose.yml` вказано статичні DNS-сервери `8.8.8.8` та `1.1.1.1`, що усуває проблеми резолвінгу через `systemd-resolved` чи `Tailscale`.
* **Автоматичний Backoff:** Плавне експоненційне очікування при тимчасових перевантаженнях Gemini API (503 / 429) або лімітах OLX.
* **Безпека:** Додаток у Docker запускається під ізольованим користувачем без root-прав.
* **Збереження даних:** База SQLite монтується через Docker volume `olx_data`, тому перезапуски не втрачають налаштувань.

---

## 🛑 Зупинка роботи

```bash
# Зупинити контейнер (база даних та налаштування зберігаються)
sudo docker compose down

# Зупинити та повністю видалити базу даних
sudo docker compose down -v
```
