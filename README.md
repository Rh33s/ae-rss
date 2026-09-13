# ae-rss: MRSS to Telegram Release Bot & Clean XML Feed Service

An automated RSS/MRSS feed monitor that broadcasts new releases to Telegram channels using **Native Slideshows, Collages**, and modern **Rich Text Formatting**, alongside a clean standards-compliant XML feed server.

---

## 🌟 Features

- **Telegram Rich Messages (Bot API 10.1+)**:
  - Automatically pairs high-definition Front and Back covers into native `<tg-slideshow>` and `<tg-collage>`.
  - Native per-scene collapsible sections (`<details><summary>...</summary>...`).
  - Per-scene 4K screenshot caps and metadata tables.
  - Automatic fallback hierarchy: `sendRichMessage` -> `sendMediaGroup` -> `sendPhoto` -> `sendMessage`.
- **Automated Handshake & Cookie Persistence**:
  - Pre-authenticates via eager AJAX handshake to obtain real session tokens and bypass age confirmation blocks.
- **Clean XML Feed Server (`server.py`)**:
  - Eliminates BOM, malformed tags, and duplicate GUIDs.
  - Serves clean RSS 2.0 / MRSS at `/clean.xml` with smart in-memory caching.
  - Includes `/poll` HTTP endpoint for webhook / external cron triggers.
- **Flexible Hosting**:
  - Run once (Cron Job): `python bot.py`
  - Continuous loop (Background Worker / Docker): `python bot.py --loop 1800`
  - Free Web Service on Render: `python server.py` (with `RUN_BOT=true` or triggered via [cron-job.org](https://cron-job.org)).

---

## 🚀 Quick Setup Guide

### 1. Telegram Bot Credentials
1. Create a bot with [@BotFather](https://t.me/BotFather) and obtain the `TELEGRAM_BOT_TOKEN`.
2. Add the bot as an Administrator to your channel or group with permission to post messages.
3. Note your `TELEGRAM_CHAT_ID` (e.g. `@my_channel` or `-1001234567890`).

### 2. Environment Variables

| Variable | Description | Default |
| :--- | :--- | :--- |
| `TELEGRAM_BOT_TOKEN` | Bot API token from `@BotFather` | *(Required)* |
| `TELEGRAM_CHAT_ID` | Telegram chat or channel ID | *(Required)* |
| `FEED_URL` | Media RSS source URL | Default Adult DVD Empire MRSS |
| `MAX_POSTS_PER_RUN` | Maximum new items dispatched per run | `10` |
| `INITIAL_POST_LIMIT` | Items dispatched on the first run (0 = all items) | `0` |
| `POLL_INTERVAL_SECONDS` | Polling interval in seconds | `1800` |
| `HISTORY_FILE` | Path to persistent history JSON file | `data/history.json` |
| `RUN_BOT` | Run background bot thread inside `server.py` | `false` |
| `CRON_SECRET` | Secret token to authenticate `/poll` triggers | `""` |

---

## 🌐 Deploying on Render.com (Free Tier)

1. Create a new **Web Service** on [Render.com](https://render.com) connected to this repository.
2. **Build Command:** `pip install -r requirements.txt`
3. **Start Command:** `python server.py`
4. **Environment Variables:**
   - `TELEGRAM_BOT_TOKEN`: `<your-token>`
   - `TELEGRAM_CHAT_ID`: `<your-chat-id>`
   - `RUN_BOT`: `true`
   - `POLL_INTERVAL_SECONDS`: `1800`
5. *(Optional for scheduled triggers)*: Setup a free 30-minute cron on [cron-job.org](https://cron-job.org) targeting `https://your-service.onrender.com/poll`.

---

## 💻 Local Testing

```bash
# Dry-run test (prints rich messages without sending to Telegram)
python bot.py --dry-run --limit 2

# Run local clean XML server
python server.py
```
