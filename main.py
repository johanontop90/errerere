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

# ================== CONFIGURATION ==================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ALERT_CHAT_ID = os.getenv("ALERT_CHAT_ID")
RAPIDAPI_KEY = os.getenv("RAPIDAPI_KEY", "5e0accb80dmshac265d29cc278d6p1913fdjsn02f9fb70c725")
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

# ================== RAPIDAPI SCRAPER ENGINE ==================
async def fetch_latest_posts(username: str) -> List[Dict[str, Any]]:
    """Fetches user posts using Realtime Tiktok Data Scraper via RapidAPI."""
    url = "https://realtime-tiktok-data-scraper.p.rapidapi.com/get_collection_by_user.php"
    
    headers = {
        "x-rapidapi-host": "realtime-tiktok-data-scraper.p.rapidapi.com",
        "x-rapidapi-key": RAPIDAPI_KEY,
        "Content-Type": "application/json"
    }
    
    params = {
        "unique_id": username.lstrip("@"),
        "count": "5",
        "cursor": "0"
    }
    
    posts = []
    try:
        async with httpx.AsyncClient(headers=headers, timeout=20.0) as client:
            res = await client.get(url, params=params)
            logger.info(f"RapidAPI Response Status for @{username}: {res.status_code}")
            
            if res.status_code == 200:
                data = res.json()
                
                # Unpack response items dynamically across different potential schemas
                items = []
                if isinstance(data, list):
                    items = data
                elif isinstance(data, dict):
                    if "data" in data and isinstance(data["data"], list):
                        items = data["data"]
                    elif "data" in data and isinstance(data["data"], dict):
                        items = data["data"].get("videos") or data["data"].get("itemList") or data["data"].get("aweme_list") or []
                    else:
                        items = data.get("aweme_list") or data.get("itemList") or data.get("videos") or []

                for item in items:
                    if not isinstance(item, dict):
                        continue

                    # Extract video ID
                    video_id = str(
                        item.get("aweme_id") or 
                        item.get("id") or 
                        item.get("video_id") or 
                        item.get("item_id") or ""
                    )
                    
                    # Extract direct play URL
                    play_url = None
                    video_info = item.get("video")
                    if isinstance(video_info, dict):
                        play_addr = video_info.get("play_addr") or video_info.get("download_addr")
                        if isinstance(play_addr, dict):
                            urls = play_addr.get("url_list", [])
                            if urls:
                                play_url = urls[0]
                        elif isinstance(video_info.get("play_addr"), str):
                            play_url = video_info.get("play_addr")
                            
                    if not play_url:
                        play_url = item.get("play") or item.get("video_url") or item.get("direct_url")

                    # Extract title / description
                    title = item.get("desc") or item.get("title") or "New TikTok Video"

                    if video_id:
                        posts.append({
                            "id": video_id,
                            "url": f"https://www.tiktok.com/@{username}/video/{video_id}",
                            "direct_video_url": play_url,
                            "title": title
                        })
            else:
                logger.error(f"RapidAPI request failed ({res.status_code}): {res.text[:200]}")
                
    except Exception as e:
        logger.error(f"Error fetching posts via RapidAPI for @{username}: {e}")
        
    return posts

# ================== COMMAND HANDLERS ==================
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    welcome = (
        "👋 <b>TikTok Monitor Bot</b>\n\n"
        "<b>Available Commands:</b>\n"
        "• /add &lt;username&gt; – Add user to monitoring list\n"
        "• /remove &lt;username&gt; – Remove user from monitoring list\n"
        "• /reset &lt;username&gt; – Clear video memory for user\n"
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

async def reset_user_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("⚠️ Usage: <code>/reset username</code>", parse_mode="HTML")
        return

    username = context.args[0].lstrip("@").lower().strip()
    config["last_seen"][username] = None
    save_config()

    await update.message.reply_text(f"🔄 Memory cleared for @{username}. Next `/checknow` will resend their top video.")

async def list_users_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    users = config.get("monitored_users", [])
    if not users:
        await update.message.reply_text("📋 No accounts are currently monitored.")
        return

    formatted_list = []
    for u in users:
        last = config["last_seen"].get(u) or "None"
        formatted_list.append(f"• @{u} (Last ID: <code>{last}</code>)")

    await update.message.reply_text(
        f"📋 <b>Monitored Accounts ({len(users)}):</b>\n\n" + "\n".join(formatted_list),
        parse_mode="HTML"
    )

async def process_account_check(username: str) -> Dict[str, Any]:
    """Helper function to run scan on single user and update state."""
    posts = await fetch_latest_posts(username)
    if not posts:
        return {"status": "failed", "message": f"❌ <b>@{username}</b>: Couldn't fetch feed (Private/Not indexing)."}

    latest = posts[0]
    post_id = latest.get("id")
    post_url = latest.get("url")
    direct_url = latest.get("direct_video_url")
    title = latest.get("title", "")
    last_seen = config["last_seen"].get(username)

    if post_id == last_seen:
        return {"status": "no_change", "message": f"ℹ️ <b>@{username}</b>: Up to date (Latest ID: <code>{post_id}</code>)"}

    return {
        "status": "new_post",
        "message": f"🆕 <b>@{username}</b>: <b>NEW POST DETECTED!</b> (ID: <code>{post_id}</code>)",
        "post_id": post_id,
        "post_url": post_url,
        "direct_url": direct_url,
        "title": title
    }

async def checknow_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    users = list(config.get("monitored_users", []))
    if not users:
        await update.message.reply_text("📋 No accounts in list. Add one using <code>/add username</code>", parse_mode="HTML")
        return

    status_msg = await update.message.reply_text("⏳ <b>Starting live scan...</b>", parse_mode="HTML")
    logs = ["🔍 <b>Live Account Check Progress:</b>\n"]
    found_total = 0

    for idx, username in enumerate(users, start=1):
        current_status = f"🔄 Checking [{idx}/{len(users)}]: <b>@{username}</b>..."
        await status_msg.edit_text("\n".join(logs + [current_status]), parse_mode="HTML")

        res = await process_account_check(username)
        logs.append(res["message"])

        if res["status"] == "new_post":
            found_total += 1
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
                    logger.warning(f"Failed sending video stream to Telegram: {stream_err}")

            if not sent:
                await context.bot.send_message(
                    chat_id=ALERT_CHAT_ID,
                    text=caption,
                    parse_mode="HTML"
                )

            config["last_seen"][username] = res["post_id"]
            save_config()

    logs.append(f"\n🏁 <b>Scan complete.</b> Found {found_total} new post(s).")
    await status_msg.edit_text("\n".join(logs), parse_mode="HTML")

async def scheduled_monitor_job(context: ContextTypes.DEFAULT_TYPE):
    """Background polling function."""
    users = list(config.get("monitored_users", []))
    for username in users:
        res = await process_account_check(username)
        if res["status"] == "new_post":
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
                    logger.warning(f"Scheduled job video error: {e}")

            if not sent:
                await context.bot.send_message(
                    chat_id=ALERT_CHAT_ID,
                    text=caption,
                    parse_mode="HTML"
                )

            config["last_seen"][username] = res["post_id"]
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
