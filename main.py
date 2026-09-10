import os
import json
import time
import threading
import logging
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List, Tuple

import requests
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, MessageHandler, filters

# ================== CONFIG ==================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ALERT_CHAT_ID = os.getenv("ALERT_CHAT_ID")
CONFIG_FILE = "config.json"
CHECK_INTERVAL = 300  # 5 minutes

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("Missing TELEGRAM_BOT_TOKEN")
if not ALERT_CHAT_ID:
    raise RuntimeError("Missing ALERT_CHAT_ID")

try:
    ALERT_CHAT_ID = int(ALERT_CHAT_ID)
except ValueError:
    pass

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

config_lock = threading.Lock()
config: Dict[str, Any] = {
    "monitored_users": [],
    "last_seen": {}
}

def load_config():
    global config
    try:
        with open(CONFIG_FILE, "r") as f:
            loaded = json.load(f)
            with config_lock:
                config = loaded
        logger.info(f"Loaded {len(config.get('monitored_users', []))} monitored users")
    except FileNotFoundError:
        save_config()
    except Exception as e:
        logger.error(f"Error loading config: {e}")

def save_config():
    with config_lock:
        try:
            with open(CONFIG_FILE, "w") as f:
                json.dump(config, f, indent=2)
            logger.info("Config saved successfully")
        except Exception as e:
            logger.error(f"Error saving config: {e}")

# ================== TIKTOK ==================
HEADERS = {
    "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1",
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.tiktok.com/",
}

def get_sec_uid(username: str) -> Optional[str]:
    url = f"https://www.tiktok.com/@{username}"
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        if r.status_code != 200:
            return None
        text = r.text
        if '"secUid":"' in text:
            start = text.find('"secUid":"') + len('"secUid":"')
            end = text.find('"', start)
            if end > start:
                return text[start:end]
    except Exception as e:
        logger.warning(f"secUid error @{username}: {e}")
    return None

def get_latest_posts(username: str, count: int = 5) -> List[Dict]:
    sec_uid = get_sec_uid(username)
    if not sec_uid:
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
            return r.json().get("itemList", []) or []
    except Exception as e:
        logger.warning(f"Posts error @{username}: {e}")
    return []

def extract_all_media(item: Dict) -> Tuple[List[str], Optional[str]]:
    photos = []
    video_url = None

    video = item.get("video") or {}
    play_addr = video.get("playAddr") or video.get("downloadAddr")
    if play_addr:
        video_url = play_addr

    image_post = item.get("imagePost") or {}
    images = image_post.get("images") or []
    for img in images:
        url_list = img.get("imageURL", {}).get("urlList") or []
        if url_list:
            photos.append(url_list[0])

    return photos, video_url

# ================== SENDING ==================
def send_telegram_message(text: str):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            data={
                "chat_id": ALERT_CHAT_ID,
                "text": text,
                "disable_web_page_preview": False
            },
            timeout=15,
        )
    except Exception as e:
        logger.error(f"Failed to send message: {e}")

def send_video(video_url: str, caption: str) -> bool:
    try:
        r = requests.get(video_url, headers=HEADERS, timeout=30)
        if r.status_code == 200 and len(r.content) > 1000:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendVideo",
                data={"chat_id": ALERT_CHAT_ID, "caption": caption},
                files={"video": ("video.mp4", r.content)},
                timeout=90,
            )
            return True
    except Exception as e:
        logger.warning(f"Failed to send video: {e}")
    return False

def send_photos(photo_urls: List[str], caption: str) -> bool:
    if not photo_urls:
        return False
    try:
        media = []
        files = {}
        for i, url in enumerate(photo_urls[:10]):
            r = requests.get(url, headers=HEADERS, timeout=20)
            if r.status_code == 200:
                file_name = f"photo{i}.jpg"
                files[file_name] = r.content
                media.append({
                    "type": "photo",
                    "media": f"attach://{file_name}",
                    "caption": caption if i == 0 else ""
                })
        if not media:
            return False
        data = {"chat_id": ALERT_CHAT_ID, "media": json.dumps(media)}
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMediaGroup",
            data=data,
            files=files,
            timeout=60,
        )
        return True
    except Exception as e:
        logger.warning(f"Failed to send photos: {e}")
        return False

# ================== CHECK LOGIC ==================
def check_all_users(force: bool = False) -> int:
    """Check all monitored users for new posts. Returns number of new posts found."""
    found = 0
    with config_lock:
        users = list(config.get("monitored_users", []))

    for username in users:
        try:
            posts = get_latest_posts(username, count=3)
            if not posts:
                continue

            latest = posts[0]
            post_id = str(latest.get("id") or "")

            with config_lock:
                last_seen = config["last_seen"].get(username)

            if not post_id:
                continue

            if not force and post_id == last_seen:
                continue

            # New post (or force check)
            logger.info(f"{'Force check' if force else 'New post'} from @{username}: {post_id}")

            caption = (
                f"🆕 New post from @{username}\n\n"
                f"https://www.tiktok.com/@{username}/video/{post_id}"
            )

            photos, video_url = extract_all_media(latest)
            sent = False

            if video_url:
                sent = send_video(video_url, caption)
            elif photos:
                sent = send_photos(photos, caption)

            if not sent:
                send_telegram_message(caption)

            with config_lock:
                config["last_seen"][username] = post_id
                save_config()

            found += 1

        except Exception as e:
            logger.error(f"Error processing @{username}: {e}")

    return found

# ================== COMMANDS ==================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"Received /start from {update.effective_user.id}")
    await update.message.reply_text(
        "👋 <b>TikTok Monitor Bot</b>\n\n"
        "Commands:\n"
        "/add &lt;username&gt; – start monitoring\n"
        "/remove &lt;username&gt; – stop monitoring\n"
        "/list – show monitored users\n"
        "/checknow – force check for new posts right now\n"
        "/online – bot uptime\n"
        "/help",
        parse_mode="HTML"
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)

async def add_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"Received /add from {update.effective_user.id}")
    if not context.args:
        await update.message.reply_text("Usage: /add username")
        return

    username = context.args[0].lstrip("@").lower().strip()
    if not username:
        await update.message.reply_text("Invalid username")
        return

    try:
        with config_lock:
            if username in config["monitored_users"]:
                await update.message.reply_text(f"@{username} is already being monitored.")
                return
            config["monitored_users"].append(username)
            config["last_seen"][username] = None

        save_config()
        await update.message.reply_text(f"✅ Now monitoring @{username}")
    except Exception as e:
        logger.error(f"Error in /add: {e}")
        await update.message.reply_text(f"❌ Failed to add @{username}. Error: {e}")

async def remove_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"Received /remove from {update.effective_user.id}")
    if not context.args:
        await update.message.reply_text("Usage: /remove username")
        return

    username = context.args[0].lstrip("@").lower().strip()

    try:
        with config_lock:
            if username not in config["monitored_users"]:
                await update.message.reply_text(f"@{username} is not being monitored.")
                return
            config["monitored_users"].remove(username)
            config["last_seen"].pop(username, None)

        save_config()
        await update.message.reply_text(f"✅ Removed @{username}")
    except Exception as e:
        logger.error(f"Error in /remove: {e}")
        await update.message.reply_text(f"❌ Failed to remove @{username}. Error: {e}")

async def list_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"Received /list from {update.effective_user.id}")
    with config_lock:
        users = config.get("monitored_users", [])

    if not users:
        await update.message.reply_text("📋 No users are currently being monitored.")
        return

    text = "📋 <b>Monitored Users</b>\n\n" + "\n".join(f"• @{u}" for u in users)
    await update.message.reply_text(text, parse_mode="HTML")

async def checknow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"Received /checknow from {update.effective_user.id}")
    await update.message.reply_text("🔍 Checking for new posts right now...")

    # Run the check in a background thread so it doesn't block the bot
    def run_check():
        found = check_all_users(force=False)
        if found == 0:
            send_telegram_message("✅ No new posts found.")
        else:
            send_telegram_message(f"✅ Found and sent {found} new post(s).")

    threading.Thread(target=run_check, daemon=True).start()

async def online(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"Received /online from {update.effective_user.id}")
    start_ts = float(os.environ.get("START_TIME", time.time()))
    start_time = datetime.fromtimestamp(start_ts, tz=timezone.utc)
    now = datetime.now(timezone.utc)
    delta = now - start_time

    days = delta.days
    hours, rem = divmod(delta.seconds, 3600)
    minutes, seconds = divmod(rem, 60)

    await update.message.reply_text(
        f"⏱ Uptime: <b>{days}d {hours}h {minutes}m {seconds}s</b>",
        parse_mode="HTML"
    )

async def debug_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message and update.message.text:
        logger.info(f"DEBUG - Received: {update.message.text} from {update.effective_user.id}")

# ================== MONITORING LOOP ==================
def monitor_loop():
    logger.info("Monitoring thread started")
    while True:
        try:
            check_all_users(force=False)
        except Exception as e:
            logger.error(f"Monitor loop error: {e}")
        logger.info(f"Checked users → sleeping {CHECK_INTERVAL}s")
        time.sleep(CHECK_INTERVAL)

# ================== MAIN ==================
if __name__ == "__main__":
    os.environ["START_TIME"] = str(time.time())
    load_config()

    monitor_thread = threading.Thread(target=monitor_loop, daemon=True)
    monitor_thread.start()

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("add", add_user))
    app.add_handler(CommandHandler("remove", remove_user))
    app.add_handler(CommandHandler("list", list_users))
    app.add_handler(CommandHandler("checknow", checknow))
    app.add_handler(CommandHandler("online", online))
    app.add_handler(MessageHandler(filters.ALL, debug_all))

    logger.info("Bot is running...")
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True
    )
