import os
import json
import time
import logging
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List

import httpx
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    CommandHandler,
)

# Optional: Using yt-dlp / tiktok-api-dl logic for profile extraction
try:
    import yt_dlp
    HAS_YTDLP = True
except ImportError:
    HAS_YTDLP = False

# ================== CONFIGURATION ==================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ALERT_CHAT_ID = os.getenv("ALERT_CHAT_ID")
CONFIG_FILE = "config.json"
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

# ================== TIKTOK PROFILE SCRAPER ==================
async def fetch_latest_posts(username: str) -> List[Dict[str, Any]]:
    """
    1. Checks the user's profile feed for the newest video URL/ID.
    2. Passes the specific video URL to extract the direct MP4 download link.
    """
    clean_user = username.lstrip("@").strip().lower()
    profile_url = f"https://www.tiktok.com/@{clean_user}"
    posts = []

    # Step 1: Use yt-dlp / flat extraction to pull the latest video link from the profile
    latest_video_url = None
    latest_video_id = None
    title = "New TikTok Video"

    if HAS_YTDLP:
        ydl_opts = {
            "extract_flat": True,
            "playlistend": 1,
            "quiet": True,
            "no_warnings": True,
        }
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(profile_url, download=False)
                if info and "entries" in info and len(info["entries"]) > 0:
                    entry = info["entries"][0]
                    latest_video_url = entry.get("url")
                    latest_video_id = str(entry.get("id") or "")
                    title = entry.get("title") or title
        except Exception as e:
            logger.error(f"Error fetching profile feed for @{clean_user} with yt-dlp: {e}")

    # Step 2: Extract direct MP4 download URL for the specific video
    direct_download_url = None
    if latest_video_url:
        # Resolve non-watermarked download link using tiktok-api-dl worker / tdown endpoint
        downloader_api = f"https://tdownv4.sl-bjs.workers.dev/?down={latest_video_url}"
        try:
            async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
                res = await client.get(downloader_api)
                if res.status_code == 200:
                    data = res.json()
                    direct_download_url = data.get("download_url")
                    if not latest_video_id:
                        latest_video_id = str(data.get("video_id") or "")
                    if data.get("title"):
                        title = data.get("title")
        except Exception as e:
            logger.warning(f"Could not extract direct download URL for {latest_video_url}: {e}")

    if latest_video_id or latest_video_url:
        posts.append({
            "id": latest_video_id or latest_video_url.split("/")[-1],
            "url": latest_video_url or f"https://www.tiktok.com/@{clean_user}/video/{latest_video_id}",
            "direct_video_url": direct_download_url,
            "title": title
        })

    return posts

# ================== COMMAND HANDLERS ==================
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    welcome = (
        "👋 <b>TikTok Monitor Bot</b>\n\n"
        "<b>Available Commands:</b>\n"
        "• /add &lt;username&gt; – Add user to monitoring list\n"
        "• /remove &lt;username&gt; – Remove user from monitoring list\n"
        "• /reset &lt;username&gt; – Clear stored video ID for user\n"
        "• /list – List monitored accounts\n"
        "• /checknow – Run manual account scan\n"
        "• /online – Show bot uptime"
    )
    await update.message.reply_text(welcome, parse_mode="HTML")

async def add_user_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("⚠️ Usage: <code>/add username</code>", parse_mode="HTML")
        return

    username = context.args[0].lstrip("@").lower().strip()
    if username in config["monitored_users"]:
        await update.message.reply_text(f"ℹ️ @{username} is already monitored.")
        return

    config["monitored_users"].append(username)
    config["last_seen"][username] = None
    save_config()

    await update.message.reply_text(f"✅ Added @{username}. Run `/checknow` to fetch their latest post.")

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

    await update.message.reply_text(f"✅ Removed @{username} from monitoring.")

async def reset_user_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("⚠️ Usage: <code>/reset username</code>", parse_mode="HTML")
        return

    username = context.args[0].lstrip("@").lower().strip()
    config["last_seen"][username] = None
    save_config()

    await update.message.reply_text(f"🔄 Memory cleared for @{username}. Next `/checknow` will detect their top post as new.")

async def list_users_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    users = config.get("monitored_users", [])
    if not users:
        await update.message.reply_text("📋 No accounts monitored.")
        return

    formatted_list = []
    for u in users:
        last = config["last_seen"].get(u) or "None (Cleared)"
        formatted_list.append(f"• @{u} (Stored ID: <code>{last}</code>)")

    await update.message.reply_text(
        f"📋 <b>Monitored Accounts ({len(users)}):</b>\n\n" + "\n".join(formatted_list),
        parse_mode="HTML"
    )

async def process_account_check(username: str) -> Dict[str, Any]:
    """Scrapes account feed, checks if the newest post ID is new, and returns status."""
    posts = await fetch_latest_posts(username)
    if not posts:
        return {"status": "failed", "message": f"❌ <b>@{username}</b>: Couldn't fetch latest video."}

    latest = posts[0]
    fetched_id = latest.get("id")
    post_url = latest.get("url")
    direct_url = latest.get("direct_video_url")
    title = latest.get("title", "")
    last_seen_id = config["last_seen"].get(username)

    # ID CHECK: Skip if video ID matches stored memory
    if fetched_id and fetched_id == last_seen_id:
        return {
            "status": "no_change", 
            "message": f"ℹ️ <b>@{username}</b>: Same video ID (<code>{fetched_id}</code>). Skipped."
        }

    return {
        "status": "new_post",
        "message": f"🆕 <b>@{username}</b>: New post detected! (ID: <code>{fetched_id}</code>)",
        "post_id": fetched_id,
        "post_url": post_url,
        "direct_url": direct_url,
        "title": title
    }

async def checknow_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    users = list(config.get("monitored_users", []))
    if not users:
        await update.message.reply_text("📋 No accounts in list. Add one with <code>/add username</code>", parse_mode="HTML")
        return

    status_msg = await update.message.reply_text("⏳ <b>Starting live scan...</b>", parse_mode="HTML")
    logs = ["🔍 <b>Live Scan Progress:</b>\n"]
    found_total = 0

    for idx, username in enumerate(users, start=1):
        current_status = f"🔄 Checking [{idx}/{len(users)}]: <b>@{username}</b>..."
        await status_msg.edit_text("\n".join(logs + [current_status]), parse_mode="HTML")

        res = await process_account_check(username)
        logs.append(res["message"])

        if res["status"] == "new_post":
            found_total += 1
            post_id = res["post_id"]
            post_url = res["post_url"]
            direct_url = res["direct_url"]
            title = res["title"]
            caption = f"🆕 <b>New post from @{username}</b>\n\n{title}\n\n{post_url}"
            sent = False

            if direct_url:
                try:
                    async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
                        response = await client.get(direct_url)
                        if response.status_code == 200:
                            await context.bot.send_video(
                                chat_id=ALERT_CHAT_ID,
                                video=response.content,
                                caption=caption,
                                parse_mode="HTML"
                            )
                            sent = True
                except Exception as stream_err:
                    logger.warning(f"Video download/upload failed: {stream_err}")

            if not sent:
                await context.bot.send_message(
                    chat_id=ALERT_CHAT_ID,
                    text=caption,
                    parse_mode="HTML"
                )

            # REPLACE OLD ID WITH NEW ID IN STATE AND SAVE
            config["last_seen"][username] = post_id
            save_config()

    logs.append(f"\n🏁 <b>Scan complete.</b> Discovered {found_total} new post(s).")
    await status_msg.edit_text("\n".join(logs), parse_mode="HTML")

async def scheduled_monitor_job(context: ContextTypes.DEFAULT_TYPE):
    """Automatic background polling task."""
    users = list(config.get("monitored_users", []))
    for username in users:
        res = await process_account_check(username)
        if res["status"] == "new_post":
            post_id = res["post_id"]
            post_url = res["post_url"]
            direct_url = res["direct_url"]
            title = res["title"]
            caption = f"🆕 <b>New post from @{username}</b>\n\n{title}\n\n{post_url}"
            sent = False

            if direct_url:
                try:
                    async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
                        response = await client.get(direct_url)
                        if response.status_code == 200:
                            await context.bot.send_video(
                                chat_id=ALERT_CHAT_ID,
                                video=response.content,
                                caption=caption,
                                parse_mode="HTML"
                            )
                            sent = True
                except Exception as e:
                    logger.warning(f"Scheduled video download failed: {e}")

            if not sent:
                await context.bot.send_message(
                    chat_id=ALERT_CHAT_ID,
                    text=caption,
                    parse_mode="HTML"
                )

            # REPLACE OLD ID WITH NEW ID IN STATE AND SAVE
            config["last_seen"][username] = post_id
            save_config()

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
    app.add_handler(CommandHandler("reset", reset_user_command))
    app.add_handler(CommandHandler("list", list_users_command))
    app.add_handler(CommandHandler("checknow", checknow_command))
    app.add_handler(CommandHandler("online", online_command))

    if app.job_queue:
        app.job_queue.run_repeating(
            scheduled_monitor_job,
            interval=CHECK_INTERVAL,
            first=10
        )

    logger.info("Bot started successfully...")
    app.run_polling(drop_pending_updates=True)
