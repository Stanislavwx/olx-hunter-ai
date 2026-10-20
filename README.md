# OLX Hunter AI 🤖

A production-ready asynchronous background daemon that continuously scans OLX.ua listings, evaluates them with Google Gemini's multimodal AI, and fires Telegram alerts for genuine good deals.

---

## Architecture Overview

```
main.py               ← Async scheduler, runs one loop per task
  ├── olx_client.py   ← OLX REST API + image downloader (httpx + backoff)
  ├── ai_analyzer.py  ← Gemini multimodal analysis (structured JSON output)
  ├── telegram_bot.py ← HTML-formatted Telegram alerts (pure httpx)
  └── database.py     ← SQLite deduplication via aiosqlite
```

Each task runs in its own `asyncio` coroutine. A crash in one task never affects others.

---

## Prerequisites

| Tool | Min Version |
|------|-------------|
| Python | 3.11+ |
| Docker | 24+ |
| Docker Compose | 2.20+ |

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
GEMINI_API_KEY=AIza...          # from https://aistudio.google.com/app/apikey
GEMINI_MODEL_NAME=gemini-2.0-flash
TELEGRAM_BOT_TOKEN=123456:ABC-DEF...   # from @BotFather
TELEGRAM_CHAT_ID=-100123456789         # your chat/channel/group ID
```

> **Get your Chat ID:**  
> Send any message to your bot, then visit:  
> `https://api.telegram.org/bot<TOKEN>/getUpdates`  
> Look for `"chat": {"id": ...}` in the response.

### 3. Build and launch

```bash
docker compose up -d --build
```

### 4. Check logs

```bash
docker compose logs -f olx-hunter
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

## Configuration (`config.json`)

All search tasks are defined in `config.json` — **no code changes needed** to add new searches.

```jsonc
{
  "tasks": [
    {
      "name":                    "My Task Name",
      "poll_interval_seconds":   300,      // how often to rescan (seconds)
      "max_pages":               3,        // OLX result pages per cycle
      "olx_params": {
        "category_id":           "89",     // OLX category
        "query":                 "nvme",   // search query
        "filter_float_price:from": "1000", // min price UAH
        "filter_float_price:to":   "4000"  // max price UAH
      },
      "ai_prompt": "Your expert evaluation instructions here…"
    }
  ]
}
```

### Default Tasks

| Task | Category | Price Range | Interval |
|------|----------|-------------|----------|
| SSD NVMe Hunter | Storage (89) | 1 000–4 000 UAH | 5 min |
| PC Flipping Under 5k | Computers (84) | 2 000–5 000 UAH | 6 min |
| ThinkPad X1 Carbon Gen 7 | Laptops (43) | any | 7 min |

---

## How It Works

```
┌─ Every poll_interval_seconds ──────────────────────────────┐
│  1. Fetch paginated OLX listings (with 429 backoff)        │
│  2. For each offer:                                        │
│     a. Check SQLite → skip if already seen                 │
│     b. Download up to 4 images (broken links tolerated)    │
│     c. Send text + images to Gemini → structured JSON      │
│     d. If is_good_deal=true → send Telegram HTML alert     │
│     e. Mark offer_id as seen (even on AI failure)          │
└────────────────────────────────────────────────────────────┘
```

### AI Response Schema

```json
{
  "is_good_deal":    true,
  "confidence":      87,
  "extracted_specs": "Samsung 980 Pro 1TB | Health 99% | THW 45TB",
  "reason":          "Excellent health, well-known brand, price 20% below market"
}
```

---

## Telegram Alert Example

```
🔥 GOOD DEAL FOUND! — SSD NVMe Hunter
━━━━━━━━━━━━━━━━━━━━
📦 Samsung 980 Pro 1TB NVMe
💰 Price: 2 200 грн
📍 City: Київ
━━━━━━━━━━━━━━━━━━━━
🤖 AI Confidence: 87% ⭐⭐⭐⭐☆
🔩 Specs: Samsung 980 Pro 1TB | Health 99% | THW 45TB
💡 Reason: Excellent health, well below market average
━━━━━━━━━━━━━━━━━━━━
              [ 👁 View on OLX ]
```

---

## Resilience Features

| Feature | Implementation |
|---------|---------------|
| **HTTP 429 Rate Limit** | Exponential backoff (30s → 300s cap) |
| **Inter-request delay** | Random 10–25s sleep between OLX pages |
| **Broken images** | `try/except` per image, silently skipped |
| **AI payload size** | Hard cap of 4 images per listing |
| **Task isolation** | Each task in own coroutine; failures don't cross-contaminate |
| **Persistence** | SQLite survives container restarts via Docker volume |
| **Non-root container** | Dockerfile runs as dedicated `appuser` |
| **Resource limits** | 512 MB RAM / 0.5 CPU cap in Compose |

---

## Updating the Model

To switch Gemini models, edit `.env`:

```dotenv
GEMINI_MODEL_NAME=gemini-2.5-pro
```

Then restart the container: `docker compose restart olx-hunter`

---

## Stopping

```bash
docker compose down          # stop and remove container (data volume kept)
docker compose down -v       # stop AND delete the SQLite volume (resets seen state)
```

---

---

# 🇺🇦 OLX Hunter AI — Документація українською

Асинхронний фоновий демон для безперервного сканування оголошень на OLX.ua. Знаходить нові лістинги, аналізує їх за допомогою мультимодального ШІ Google Gemini та надсилає Telegram-сповіщення про вигідні пропозиції.

---

## 🏗️ Огляд архітектури

```
main.py               ← Головний планувальник, окремий цикл на кожне завдання
  ├── olx_client.py   ← HTTP-клієнт до OLX API (httpx + exponential backoff)
  ├── ai_analyzer.py  ← Аналіз Gemini з примусовим JSON-виводом
  ├── telegram_bot.py ← HTML-сповіщення у Telegram (чистий httpx)
  └── database.py     ← SQLite-дедублікація через aiosqlite
```

Кожне завдання запускається у власній `asyncio`-корутині. Збій в одному завданні **не впливає** на інші.

---

## ⚡ Швидкий старт (Docker — рекомендовано)

### 1. Клонування та налаштування

```bash
git clone <repo-url> olx-hunter-ai
cd olx-hunter-ai
cp .env.example .env
```

### 2. Заповніть `.env`

```dotenv
GEMINI_API_KEY=AIza...               # з https://aistudio.google.com/app/apikey
GEMINI_MODEL_NAME=gemini-2.0-flash
TELEGRAM_BOT_TOKEN=123456:ABC-DEF... # від @BotFather
TELEGRAM_CHAT_ID=-100123456789       # ваш chat/канал/група ID
```

> **Як дізнатись Chat ID:**  
> Напишіть будь-яке повідомлення своєму боту, потім відкрийте:  
> `https://api.telegram.org/bot<TOKEN>/getUpdates`  
> Знайдіть `"chat": {"id": ...}` у відповіді.

### 3. Збірка та запуск

```bash
docker compose up -d --build
```

### 4. Перегляд логів

```bash
docker compose logs -f olx-hunter
```

---

## 🐍 Запуск без Docker (локально)

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # заповніть ключі
python main.py
```

---

## ⚙️ Конфігурація (`config.json`)

Всі пошукові завдання описані у `config.json` — **без змін коду** можна додавати нові пошуки.

```jsonc
{
  "tasks": [
    {
      "name":                  "Назва завдання",
      "poll_interval_seconds": 300,     // інтервал повторного сканування (сек)
      "max_pages":             3,       // кількість сторінок OLX за цикл
      "olx_params": {
        "category_id":             "89",    // ID категорії OLX
        "query":                   "nvme",  // пошуковий запит
        "filter_float_price:from": "1000",  // мінімальна ціна (грн)
        "filter_float_price:to":   "4000"   // максимальна ціна (грн)
      },
      "ai_prompt": "Ваша інструкція для ШІ-аналізу…"
    }
  ]
}
```

### Завдання за замовчуванням

| Завдання | Категорія | Ціновий діапазон | Інтервал |
|----------|-----------|-----------------|----------|
| SSD NVMe Hunter | Накопичувачі (89) | 1 000–4 000 грн | 5 хв |
| PC Фліппінг до 5k | Комп'ютери (84) | 2 000–5 000 грн | 6 хв |
| ThinkPad X1 Carbon Gen 7 | Ноутбуки (43) | будь-яка | 7 хв |

---

## 🔄 Як це працює

```
┌─ Кожні poll_interval_seconds ──────────────────────────────────┐
│  1. Завантаження сторінок OLX (з backoff при 429)              │
│  2. Для кожного оголошення:                                     │
│     а. Перевірка SQLite → пропуск, якщо вже оброблено          │
│     б. Завантаження до 4 фото (зламані посилання ігноруються)  │
│     в. Надсилання тексту + фото до Gemini → структурований JSON │
│     г. Якщо is_good_deal=true → Telegram HTML-сповіщення       │
│     д. Запис offer_id як переглянутого (навіть при помилці ШІ) │
└────────────────────────────────────────────────────────────────┘
```

### Схема відповіді ШІ

```json
{
  "is_good_deal":    true,
  "confidence":      87,
  "extracted_specs": "Samsung 980 Pro 1TB | Health 99% | THW 45TB",
  "reason":          "Відмінний стан, ціна на 20% нижча за ринкову"
}
```

---

## 📬 Приклад Telegram-сповіщення

```
🔥 ВИГІДНА ПРОПОЗИЦІЯ! — SSD NVMe Hunter
━━━━━━━━━━━━━━━━━━━━
📦 Samsung 980 Pro 1TB NVMe
💰 Ціна: 2 200 грн
📍 Місто: Київ
━━━━━━━━━━━━━━━━━━━━
🤖 Впевненість ШІ: 87% ⭐⭐⭐⭐☆
🔩 Характеристики: Samsung 980 Pro 1TB | Health 99% | THW 45TB
💡 Причина: Відмінний стан, значно нижча за ринкову ціну
━━━━━━━━━━━━━━━━━━━━
              [ 👁 Переглянути на OLX ]
```

---

## 🛡️ Захисні механізми

| Механізм | Реалізація |
|----------|-----------|
| **HTTP 429 (ліміт запитів)** | Exponential backoff (30с → макс. 300с) |
| **Затримка між запитами** | Випадкова пауза 10–25с між сторінками OLX |
| **Зламані фото** | `try/except` на кожне фото, тихо пропускаються |
| **Розмір ШІ-запиту** | Жорсткий ліміт — 4 фото на оголошення |
| **Ізоляція завдань** | Кожне завдання у власній корутині; збої не каскадують |
| **Персистентність** | SQLite виживає після перезапуску контейнера (Docker volume) |
| **Безпека контейнера** | Dockerfile запускається від непривілейованого `appuser` |
| **Ліміти ресурсів** | 512 МБ RAM / 0.5 CPU у Compose |

---

## 🔁 Зміна моделі Gemini

Відредагуйте `.env`:

```dotenv
GEMINI_MODEL_NAME=gemini-2.5-pro
```

Перезапустіть контейнер:

```bash
docker compose restart olx-hunter
```

---

## 🛑 Зупинка

```bash
docker compose down      # зупинити контейнер (дані зберігаються)
docker compose down -v   # зупинити + видалити SQLite volume (скидає стан)
```
