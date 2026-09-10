import os
import json
import time
import logging
import asyncio
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List

import httpx
import yt_dlp
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    CommandHandler,
)

# ================== CONFIGURATION ==================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ALERT_CHAT_ID = os.getenv("ALERT_CHAT_ID")
CONFIG_FILE = "config.json"
COOKIES_FILE = "cookies.txt"
CHECK_INTERVAL = 300  # 5 minutes

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

# ================== TIKTOK SCRAPER ==================
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) "
        "Version/17.0 Mobile/15E148 Safari/604.1"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.tiktok.com/",
}

def fetch_latest_posts(username: str, max_results: int = 3) -> List[Dict[str, Any]]:
    """Fetch recent posts forcing live requests without cache."""
    profile_url = f"https://www.tiktok.com/@{username}"
    
    ydl_opts: Dict[str, Any] = {
        "extract_flat": True,
        "playlistend": max_results,
        "quiet": True,
        "no_warnings": True,
        "ignoreerrors": True,
        "cachedir": False,  # Force yt-dlp to bypass disk cache
        "http_headers": HEADERS,
        "extractor_args": {
            "tiktok": {
                "webpage_download": True,
            }
        }
    }

    if os.path.exists(COOKIES_FILE):
        ydl_opts["cookiefile"] = COOKIES_FILE

    posts = []
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            result = ydl.extract_info(profile_url, download=False)
            if result and "entries" in result:
                for entry in result["entries"]:
                    if entry and entry.get("id"):
                        posts.append({
                            "id": str(entry.get("id")),
                            "url": entry.get("url") or f"https://www.tiktok.com/@{username}/video/{entry.get('id')}",
                            "title": entry.get("title") or "New TikTok Video"
                        })
    except Exception as e:
        logger.error(f"yt-dlp error for @{username}: {e}")

    return posts

def get_direct_video_stream(post_url: str) -> Optional[str]:
    """Extract direct playable MP4 stream URL."""
    ydl_opts: Dict[str, Any] = {
        "format": "b[ext=mp4]/best[ext=mp4]/best",
        "quiet": True,
        "no_warnings": True,
        "cachedir": False,
        "http_headers": HEADERS,
    }

    if os.path.exists(COOKIES_FILE):
        ydl_opts["cookiefile"] = COOKIES_FILE

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(post_url, download=False)
            return info.get("url")
    except Exception as e:
        logger.warning(f"Failed stream extraction for {post_url}: {e}")
        return None

# ================== COMMAND HANDLERS ==================
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    welcome = (
        "👋 <b>TikTok Monitor Bot</b>\n\n"
        "<b>Available Commands:</b>\n"
        "• /add &lt;username&gt; – Add user to monitoring list\n"
        "• /remove &lt;username&gt; – Remove user from monitoring list\n"
        "• /list – List all monitored accounts\n"
        "• /checknow – Force step-by-step account check\n"
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
    """Executes a real-time account-by-account check with live status output."""
    users = list(config.get("monitored_users", []))
    if not users:
        await update.message.reply_text("📋 No accounts in monitoring list. Add one using <code>/add username</code>", parse_mode="HTML")
        return

    status_msg = await update.message.reply_text("⏳ <b>Starting live scan...</b>", parse_mode="HTML")
    
    logs = ["🔍 <b>Live Account Check Progress:</b>\n"]
    found_total = 0

    for idx, username in enumerate(users, start=1):
        # Update progress header
        current_status = f"🔄 Checking [{idx}/{len(users)}]: <b>@{username}</b>..."
        await status_msg.edit_text("\n".join(logs + [current_status]), parse_mode="HTML")

        # Run extraction non-blockingly
        loop = asyncio.get_running_loop()
        posts = await loop.run_in_executor(None, fetch_latest_posts, username, 3)

        if not posts:
            logs.append(f"❌ <b>@{username}</b>: TikTok blocked request or 0 posts found.")
            continue

        latest = posts[0]
        post_id = latest.get("id")
        post_url = latest.get("url")
        last_seen = config["last_seen"].get(username)

        if post_id == last_seen:
            logs.append(f"ℹ️ <b>@{username}</b>: Up to date (Latest ID: <code>{post_id}</code>)")
        else:
            logs.append(f"🆕 <b>@{username}</b>: <b>NEW POST DETECTED!</b> (ID: <code>{post_id}</code>)")
            found_total += 1

            # Dispatch video/link alert to alert channel/user
            caption = f"🆕 <b>New post from @{username}</b>\n\n{post_url}"
            direct_url = await loop.run_in_executor(None, get_direct_video_stream, post_url)
            sent = False

            if direct_url:
                try:
                    async with httpx.AsyncClient(headers=HEADERS, timeout=60.0) as client:
                        res = await client.get(direct_url)
                        if res.status_code == 200:
                            await context.bot.send_video(
                                chat_id=ALERT_CHAT_ID,
                                video=res.content,
                                caption=caption,
                                parse_mode="HTML"
                            )
                            sent = True
                except Exception as e:
                    logger.warning(f"Could not send stream video: {e}")

            if not sent:
                await context.bot.send_message(
                    chat_id=ALERT_CHAT_ID,
                    text=caption,
                    parse_mode="HTML"
                )

            # Update state
            config["last_seen"][username] = post_id
            save_config()

    logs.append(f"\n🏁 <b>Scan complete.</b> Found {found_total} new post(s).")
    await status_msg.edit_text("\n".join(logs), parse_mode="HTML")

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

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", start_command))
    app.add_handler(CommandHandler("add", add_user_command))
    app.add_handler(CommandHandler("remove", remove_user_command))
    app.add_handler(CommandHandler("list", list_users_command))
    app.add_handler(CommandHandler("checknow", checknow_command))
    app.add_handler(CommandHandler("online", online_command))

    logger.info("Bot running...")
    app.run_polling(drop_pending_updates=True)
