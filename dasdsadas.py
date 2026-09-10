import os
import json
import time
import threading
import logging
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List

import requests
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler

# ---------------- CONFIG ----------------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ALERT_CHAT_ID = os.getenv("ALERT_CHAT_ID")  # can be string or int
CONFIG_FILE = "config.json"
CHECK_INTERVAL = 300  # seconds (5 minutes)

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN environment variable is required")
if not ALERT_CHAT_ID:
    raise RuntimeError("ALERT_CHAT_ID environment variable is required")

try:
    ALERT_CHAT_ID = int(ALERT_CHAT_ID)
except ValueError:
    pass  # keep as string if it's a channel username etc.

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Thread-safe config
config_lock = threading.Lock()
config: Dict[str, Any] = {
    "monitored_users": [],   # list of usernames (without @)
    "last_seen": {}          # username -> last post id
}

def load_config():
    global config
    try:
        with open(CONFIG_FILE, "r") as f:
            loaded = json.load(f)
            with config_lock:
                config = loaded
        logger.info(f"Loaded config with {len(config.get('monitored_users', []))} users")
    except FileNotFoundError:
        save_config()
    except Exception as e:
        logger.error(f"Failed to load config: {e}")

def save_config():
    with config_lock:
        try:
            with open(CONFIG_FILE, "w") as f:
                json.dump(config, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to save config: {e}")

# ---------------- TIKTOK FETCHING ----------------
HEADERS = {
    "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.tiktok.com/",
}

def get_sec_uid(username: str) -> Optional[str]:
    """Get secUid from user profile page."""
    url = f"https://www.tiktok.com/@{username}"
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        if r.status_code != 200:
            return None

        # Try to extract from the rehydration data
        text = r.text
        # Common pattern
        if '"secUid":"' in text:
            start = text.find('"secUid":"') + len('"secUid":"')
            end = text.find('"', start)
            if end > start:
                return text[start:end]
    except Exception as e:
        logger.warning(f"Failed to get secUid for @{username}: {e}")
    return None

def get_latest_posts(username: str, count: int = 5) -> List[Dict]:
    """Fetch recent posts for a user."""
    sec_uid = get_sec_uid(username)
    if not sec_uid:
        logger.warning(f"Could not resolve secUid for @{username}")
        return []

    url = "https://www.tiktok.com/api/post/item_list/"
    params = {
        "secUid": sec_uid,
        "count": count,
        "cursor": 0,
        "aid": 1988,
        "app_language": "en",
        "device_platform": "web_pc",
    }

    try:
        r = requests.get(url, headers=HEADERS, params=params, timeout=15)
        if r.status_code == 200:
            data = r.json()
            return data.get("itemList", []) or []
    except Exception as e:
        logger.warning(f"Error fetching posts for @{username}: {e}")
    return []

def get_media_info(item: Dict) -> tuple[Optional[str], Optional[str]]:
    """Return (url, type) where type is 'video' or 'photo'."""
    video = item.get("video") or {}
    play_addr = video.get("playAddr") or video.get("downloadAddr")
    if play_addr:
        return play_addr, "video"

    image_post = item.get("imagePost") or {}
    images = image_post.get("images") or []
    if images:
        # Take first image
        url_list = images[0].get("imageURL", {}).get("urlList") or []
        if url_list:
            return url_list[0], "photo"
    return None, None

# ---------------- TELEGRAM COMMANDS ----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 TikTok Monitor Bot\n\n"
        "Commands:\n"
        "/add <username> – start monitoring\n"
        "/remove <username> – stop monitoring\n"
        "/list – show monitored users\n"
        "/online – bot uptime\n"
        "/help"
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)

async def add_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /add username")
        return

    username = context.args[0].lstrip("@").lower().strip()
    if not username:
        await update.message.reply_text("Invalid username")
        return

    with config_lock:
        if username in config["monitored_users"]:
            await update.message.reply_text(f"@{username} is already monitored.")
            return
        config["monitored_users"].append(username)
        config["last_seen"][username] = None
        save_config()

    await update.message.reply_text(f"✅ Now monitoring @{username}")

async def remove_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /remove username")
        return

    username = context.args[0].lstrip("@").lower().strip()

    with config_lock:
        if username not in config["monitored_users"]:
            await update.message.reply_text(f"@{username} is not monitored.")
            return
        config["monitored_users"].remove(username)
        config["last_seen"].pop(username, None)
        save_config()

    await update.message.reply_text(f"✅ Removed @{username}")

async def list_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with config_lock:
        users = config["monitored_users"]

    if not users:
        await update.message.reply_text("No users are being monitored.")
        return

    text = "📋 Monitored users:\n\n" + "\n".join(f"• @{u}" for u in users)
    await update.message.reply_text(text)

async def online(update: Update, context: ContextTypes.DEFAULT_TYPE):
    start_ts = float(os.environ.get("START_TIME", time.time()))
    start_time = datetime.fromtimestamp(start_ts, tz=timezone.utc)
    now = datetime.now(timezone.utc)
    delta = now - start_time

    days = delta.days
    hours, rem = divmod(delta.seconds, 3600)
    minutes, seconds = divmod(rem, 60)

    await update.message.reply_text(
        f"⏱ Uptime: {days}d {hours}h {minutes}m {seconds}s"
    )

# ---------------- MONITORING LOOP ----------------
def monitor_loop():
    logger.info("Monitoring thread started")
    while True:
        with config_lock:
            users = list(config["monitored_users"])

        for username in users:
            try:
                posts = get_latest_posts(username, count=3)
                if not posts:
                    continue

                latest = posts[0]
                post_id = str(latest.get("id") or "")

                with config_lock:
                    last_seen = config["last_seen"].get(username)

                if not post_id or post_id == last_seen:
                    continue

                # New post detected
                logger.info(f"New post from @{username}: {post_id}")

                caption = (
                    f"🆕 New post from @{username}\n\n"
                    f"https://www.tiktok.com/@{username}/video/{post_id}"
                )

                # Try to send media, fall back to link
                media_url, media_type = get_media_info(latest)
                sent = False

                if media_url:
                    try:
                        # Download with same headers
                        r = requests.get(media_url, headers=HEADERS, timeout=20, stream=True)
                        if r.status_code == 200:
                            content = r.content
                            if media_type == "video" and len(content) > 1000:
                                requests.post(
                                    f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendVideo",
                                    data={"chat_id": ALERT_CHAT_ID, "caption": caption},
                                    files={"video": ("video.mp4", content)},
                                    timeout=60,
                                )
                                sent = True
                            elif media_type == "photo":
                                requests.post(
                                    f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto",
                                    data={"chat_id": ALERT_CHAT_ID, "caption": caption},
                                    files={"photo": ("photo.jpg", content)},
                                    timeout=30,
                                )
                                sent = True
                    except Exception as e:
                        logger.warning(f"Failed to send media for @{username}: {e}")

                if not sent:
                    # Fallback: just send the link
                    requests.post(
                        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                        data={"chat_id": ALERT_CHAT_ID, "text": caption, "disable_web_page_preview": False},
                        timeout=15,
                    )

                # Update last_seen
                with config_lock:
                    config["last_seen"][username] = post_id
                    save_config()

            except Exception as e:
                logger.error(f"Error processing @{username}: {e}")

        logger.info(f"Checked {len(users)} users. Sleeping {CHECK_INTERVAL}s...")
        time.sleep(CHECK_INTERVAL)

# ---------------- MAIN ----------------
if __name__ == "__main__":
    os.environ["START_TIME"] = str(time.time())
    load_config()

    # Start monitoring thread
    t = threading.Thread(target=monitor_loop, daemon=True)
    t.start()

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("add", add_user))
    app.add_handler(CommandHandler("remove", remove_user))
    app.add_handler(CommandHandler("list", list_users))
    app.add_handler(CommandHandler("online", online))

    logger.info("Telegram bot starting (polling)...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)
