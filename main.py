import os
import json
import time
import re
import logging
import xml.etree.ElementTree as ET
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

# ================== DUAL SCRAPER ENGINE ==================
async def fetch_tikwm(username: str) -> List[Dict[str, Any]]:
    """Engine 1: TikWM Public API."""
    url = f"https://www.tikwm.com/api/user/posts?unique_id={username}&count=5"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json"
    }
    posts = []
    try:
        async with httpx.AsyncClient(headers=headers, timeout=12.0) as client:
            res = await client.get(url)
            if res.status_code == 200:
                data = res.json()
                if data.get("code") == 0 and "data" in data and "videos" in data["data"]:
                    for vid in data["data"]["videos"]:
                        video_id = str(vid.get("video_id"))
                        posts.append({
                            "id": video_id,
                            "url": f"https://www.tiktok.com/@{username}/video/{video_id}",
                            "direct_video_url": vid.get("play"),
                            "title": vid.get("title") or "New TikTok Video"
                        })
    except Exception as e:
        logger.warning(f"TikWM engine failed for @{username}: {e}")
    return posts

async def fetch_rsshub(username: str) -> List[Dict[str, Any]]:
    """Engine 2: RSSHub Feed Fallback."""
    url = f"https://rsshub.app/tiktok/user/@{username}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/rss+xml, application/xml;q=0.9"
    }
    posts = []
    try:
        async with httpx.AsyncClient(headers=headers, timeout=12.0) as client:
            res = await client.get(url)
            if res.status_code == 200:
                root = ET.fromstring(res.text)
                channel = root.find("channel")
                if channel is not None:
                    for item in channel.findall("item"):
                        link = item.findtext("link") or ""
                        match = re.search(r'/video/(\d+)', link)
                        if match:
                            video_id = match.group(1)
                            posts.append({
                                "id": video_id,
                                "url": f"https://www.tiktok.com/@{username}/video/{video_id}",
                                "direct_video_url": None,
                                "title": item.findtext("title") or "New TikTok Video"
                            })
    except Exception as e:
        logger.warning(f"RSSHub fallback failed for @{username}: {e}")
    return posts

async def fetch_latest_posts(username: str) -> List[Dict[str, Any]]:
    """Fetches user posts trying TikWM first, then RSSHub."""
    posts = await fetch_tikwm(username)
    if not posts:
        logger.info(f"TikWM yielded 0 posts for @{username}, switching to RSSHub fallback...")
        posts = await fetch_rsshub(username)
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
    last_seen = config["last_seen"].get(username)

    if post_id == last_seen:
        return {"status": "no_change", "message": f"ℹ️ <b>@{username}</b>: Up to date (Latest ID: <code>{post_id}</code>)"}

    return {
        "status": "new_post",
        "message": f"🆕 <b>@{username}</b>: <b>NEW POST DETECTED!</b> (ID: <code>{post_id}</code>)",
        "post_id": post_id,
        "post_url": post_url,
        "direct_url": direct_url
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
            caption = f"🆕 <b>New post from @{username}</b>\n\n{post_url}"
            sent = False

            if direct_url:
                try:
                    async with httpx.AsyncClient(timeout=60.0) as client:
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
            caption = f"🆕 <b>New post from @{username}</b>\n\n{post_url}"
            sent = False

            if direct_url:
                try:
                    async with httpx.AsyncClient(timeout=60.0) as client:
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
