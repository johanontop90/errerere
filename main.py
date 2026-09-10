import os
import json
import time
import logging
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List

import httpx
import yt_dlp
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    CommandHandler,
    MessageHandler,
    filters,
)

# ================== CONFIGURATION ==================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ALERT_CHAT_ID = os.getenv("ALERT_CHAT_ID")
CONFIG_FILE = "config.json"
CHECK_INTERVAL = 300  # 5 minutes in seconds

if not TELEGRAM_BOT_TOKEN or not ALERT_CHAT_ID:
    raise RuntimeError("Missing TELEGRAM_BOT_TOKEN or ALERT_CHAT_ID environment variables.")

try:
    ALERT_CHAT_ID = int(ALERT_CHAT_ID)
except ValueError:
    pass

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger("TikTokMonitorBot")

# ================== STATE MANAGEMENT ==================
config: Dict[str, Any] = {
    "monitored_users": [],
    "last_seen": {}
}

def load_config():
    global config
    try:
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                config = json.load(f)
            logger.info(f"Loaded {len(config.get('monitored_users', []))} monitored users.")
        else:
            save_config()
    except Exception as e:
        logger.error(f"Error loading config: {e}")

def save_config():
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)
        logger.info("Config saved successfully.")
    except Exception as e:
        logger.error(f"Error saving config: {e}")

# ================== TIKTOK SCRAPER (YT-DLP) ==================
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

def fetch_latest_posts(username: str, max_results: int = 3) -> List[Dict[str, Any]]:
    """Fetch recent posts for a TikTok user using yt-dlp."""
    profile_url = f"https://www.tiktok.com/@{username}"
    ydl_opts = {
        "extract_flat": True,
        "playlistend": max_results,
        "quiet": True,
        "no_warnings": True,
        "ignoreerrors": True,
        "http_headers": HEADERS,
    }

    posts = []
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            result = ydl.extract_info(profile_url, download=False)
            if result and "entries" in result:
                for entry in result["entries"]:
                    if entry:
                        posts.append({
                            "id": str(entry.get("id")),
                            "url": entry.get("url") or f"https://www.tiktok.com/@{username}/video/{entry.get('id')}",
                            "title": entry.get("title") or "New TikTok Video"
                        })
    except Exception as e:
        logger.error(f"yt-dlp failed to fetch profile for @{username}: {e}")

    return posts

def get_direct_video_stream(post_url: str) -> Optional[str]:
    """Extract direct playable MP4 stream URL."""
    ydl_opts = {
        "format": "b[ext=mp4]/best[ext=mp4]/best",
        "quiet": True,
        "no_warnings": True,
        "http_headers": HEADERS,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(post_url, download=False)
            return info.get("url")
    except Exception as e:
        logger.warning(f"Failed to extract video stream for {post_url}: {e}")
        return None

# ================== ASYNC MONITOR TASK ==================
async def check_all_users(context: ContextTypes.DEFAULT_TYPE, force: bool = False) -> int:
    """Checks all users in the monitoring list for new posts."""
    users = list(config.get("monitored_users", []))
    found_count = 0

    for username in users:
        try:
            # Offload synchronous yt-dlp extraction to async loop executor
            loop = context.application.loop
            posts = await loop.run_in_executor(None, fetch_latest_posts, username, 3)

            if not posts:
                logger.info(f"No posts retrieved for @{username} (Profile private or blocked).")
                continue

            latest = posts[0]
            post_id = latest.get("id")
            post_url = latest.get("url")
            last_seen = config["last_seen"].get(username)

            if not post_id or (not force and post_id == last_seen):
                continue

            logger.info(f"New post detected for @{username}: {post_id}")
            caption = f"🆕 <b>New post from @{username}</b>\n\n{post_url}"

            # Attempt to download video and send directly via Telegram Bot API
            direct_url = await loop.run_in_executor(None, get_direct_video_stream, post_url)
            sent_successfully = False

            if direct_url:
                try:
                    async with httpx.AsyncClient(headers=HEADERS, timeout=60.0) as client:
                        response = await client.get(direct_url)
                        if response.status_code == 200:
                            await context.bot.send_video(
                                chat_id=ALERT_CHAT_ID,
                                video=response.content,
                                caption=caption,
                                parse_mode="HTML"
                            )
                            sent_successfully = True
                except Exception as stream_err:
                    logger.warning(f"Failed sending video stream to Telegram: {stream_err}")

            # Fallback to plain text alert with video link if media delivery fails
            if not sent_successfully:
                await context.bot.send_message(
                    chat_id=ALERT_CHAT_ID,
                    text=caption,
                    parse_mode="HTML",
                    disable_web_page_preview=False
                )

            # Update state
            config["last_seen"][username] = post_id
            save_config()
            found_count += 1

        except Exception as err:
            logger.error(f"Error checking user @{username}: {err}")

    return found_count

async def scheduled_monitor_job(context: ContextTypes.DEFAULT_TYPE):
    """Job queue task run periodically."""
    await check_all_users(context, force=False)

# ================== COMMAND HANDLERS ==================
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    welcome = (
        "👋 <b>TikTok Monitor Bot</b>\n\n"
        "<b>Available Commands:</b>\n"
        "• /add &lt;username&gt; – Add user to monitoring list\n"
        "• /remove &lt;username&gt; – Remove user from monitoring list\n"
        "• /list – List all monitored accounts\n"
        "• /checknow – Force immediate scan\n"
        "• /online – Show bot uptime"
    )
    await update.message.reply_text(welcome, parse_mode="HTML")

async def add_user_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("⚠️ Usage: <code>/add username</code>", parse_mode="HTML")
        return

    username = context.args[0].lstrip("@").lower().strip()
    if username in config["monitored_users"]:
        await update.message.reply_text(f"ℹ️ @{username} is already being monitored.")
        return

    config["monitored_users"].append(username)
    config["last_seen"][username] = None
    save_config()

    await update.message.reply_text(f"✅ Added @{username} to the monitoring list.")

async def remove_user_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("⚠️ Usage: <code>/remove username</code>", parse_mode="HTML")
        return

    username = context.args[0].lstrip("@").lower().strip()
    if username not in config["monitored_users"]:
        await update.message.reply_text(f"ℹ️ @{username} is not in the list.")
        return

    config["monitored_users"].remove(username)
    config["last_seen"].pop(username, None)
    save_config()

    await update.message.reply_text(f"✅ Removed @{username} from the monitoring list.")

async def list_users_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    users = config.get("monitored_users", [])
    if not users:
        await update.message.reply_text("📋 No accounts are currently monitored.")
        return

    formatted_list = "\n".join([f"• @{u}" for u in users])
    await update.message.reply_text(
        f"📋 <b>Monitored Accounts ({len(users)}):</b>\n\n{formatted_list}",
        parse_mode="HTML"
    )

async def checknow_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔍 Checking all monitored accounts now...")
    found = await check_all_users(context, force=False)
    await update.message.reply_text(f"✅ Scan completed! Found {found} new post(s).")

async def online_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    start_ts = float(os.environ.get("START_TIME", time.time()))
    start_time = datetime.fromtimestamp(start_ts, tz=timezone.utc)
    uptime = datetime.now(timezone.utc) - start_time

    days = uptime.days
    hours, remainder = divmod(uptime.seconds, 3600)
    minutes, seconds = divmod(remainder, 60)

    await update.message.reply_text(
        f"⏱ <b>Bot Uptime:</b> {days}d {hours}h {minutes}m {seconds}s",
        parse_mode="HTML"
    )

# ================== MAIN ENTRY POINT ==================
if __name__ == "__main__":
    os.environ["START_TIME"] = str(time.time())
    load_config()

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", start_command))
    app.add_handler(CommandHandler("add", add_user_command))
    app.add_handler(CommandHandler("remove", remove_user_command))
    app.add_handler(CommandHandler("list", list_users_command))
    app.add_handler(CommandHandler("checknow", checknow_command))
    app.add_handler(CommandHandler("online", online_command))

    # Add periodic background task using JobQueue
    if app.job_queue:
        app.job_queue.run_repeating(
            scheduled_monitor_job,
            interval=CHECK_INTERVAL,
            first=10
        )

    logger.info("Bot starting up...")
    app.run_polling(drop_pending_updates=True)
