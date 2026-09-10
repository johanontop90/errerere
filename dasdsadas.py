import os
import requests
import json
import time
import base64
import sys
from datetime import datetime, timezone
from telegram import Bot, Update
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler
import threading

# --- CONFIGURATION ---
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
ALERT_CHAT_ID = int(os.getenv('ALERT_CHAT_ID')) 
CONFIG_FILE = "config.json"

# Load config
try:
    with open(CONFIG_FILE, 'r') as f:
        config = json.load(f)
except FileNotFoundError:
    config = {
        "monitored_users": [], # Stores Base64 strings
        "last_seen": {}
    }

def save_config():
    with open(CONFIG_FILE, 'w') as f:
        json.dump(config, f)

def encode_username(username):
    """Convert username to Base64 string"""
    return base64.b64encode(username.encode('utf-8')).decode('utf-8')

def decode_username(b64_username):
    """Convert Base64 string back to username"""
    return base64.b64decode(b64_username.encode('utf-8')).decode('utf-8')

def get_tiktok_profile_data(username):
    user = username.lstrip('@')
    url = f"https://www.tiktok.com/api/post/item_list/?uniqueId={user}&count=5&cursor=0&secUid=&itemId=&id={user}"
    headers = {
        "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 13_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/13.1.1 Mobile/15E148 Safari/604.1",
        "Referer": f"https://www.tiktok.com/@{user}"
    }
    try:
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code == 200:
            data = response.json()
            return data.get('itemList', [])
    except Exception as e:
        print(f"Error fetching data for {user}: {e}")
    return []

def get_media_url_and_type(item):
    video_info = item.get('video', {})
    play_addr = video_info.get('playAddr', '')
    if play_addr:
        return play_addr, 'video'
    if 'imagePost' in item:
        images = item['imagePost'].get('images', [])
        if images:
            return images[0]['url'], 'photo'
    return None, None

# --- COMMAND HANDLERS ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Welcome! I am your TikTok Monitor Bot. 🤖\n\nUse /help for commands.")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = """
<b>TikTok Monitor Bot Commands:</b>

/add <username> - Add a user to monitor (e.g., /add charlidamelio)
/remove <username> - Remove a user from monitoring
/list - View currently monitored users (encoded)
/online - See how long I've been running
"""
    await update.message.reply_text(help_text, parse_mode='HTML')

async def add_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.args:
        username = context.args[0].lstrip('@')
        encoded = encode_username(username)
        if encoded not in config["monitored_users"]:
            config["monitored_users"].append(encoded)
            config["last_seen"][encoded] = None 
            save_config()
            await update.message.reply_text(f"✅ Added @{username} to monitoring list (stored as ID).")
        else:
            await update.message.reply_text(f"❌ @{username} is already being monitored.")
    else:
        await update.message.reply_text("❌ Please provide a username. Usage: /add username")

async def remove_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.args:
        username = context.args[0].lstrip('@')
        encoded = encode_username(username)
        if encoded in config["monitored_users"]:
            config["monitored_users"].remove(encoded)
            if encoded in config["last_seen"]:
                del config["last_seen"][encoded]
            save_config()
            await update.message.reply_text(f"✅ Removed @{username} from monitoring list.")
        else:
            await update.message.reply_text(f"❌ @{username} is not in the monitoring list.")
    else:
        await update.message.reply_text("❌ Please provide a username. Usage: /remove username")

async def list_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if config["monitored_users"]:
        users_text = "📋 <b>Monitored Users (Base64):**\n\n"
        for encoded_user in config["monitored_users"]:
            # Decode just to show the real name in the chat
            real_name = decode_username(encoded_user)
            users_text += f"• @{real_name} (ID: `{encoded_user}`)\n"
        await update.message.reply_text(users_text, parse_mode='HTML')
    else:
        await update.message.reply_text("📋 No users are currently being monitored.")

async def online(update: Update, context: ContextTypes.DEFAULT_TYPE):
    start_time = datetime.fromtimestamp(os.environ.get('START_TIME', time.time()), tz=timezone.utc)
    now = datetime.now(timezone.utc)
    duration = now - start_time
    days = duration.days
    hours, remainder = divmod(duration.seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    uptime_text = f"⏱️ <b>Bot Uptime:</b>\n\n{days} days, {hours} hours, {minutes} minutes, {seconds} seconds."
    await update.message.reply_text(uptime_text, parse_mode='HTML')

# --- MAIN MONITORING LOGIC ---

def monitor_loop():
    print("Bot started. Monitoring TikTok...")
    while True:
        # Iterate through encoded usernames
        for encoded_user in config["monitored_users"]:
            username = decode_username(encoded_user)
            posts = get_tiktok_profile_data(username)
            
            if not posts:
                continue
                
            latest_post_id = posts[0].get('id')
            
            if latest_post_id and config["last_seen"].get(encoded_user) != latest_post_id:
                print(f"New post detected from @{username}: {latest_post_id}")
                
                media_url, media_type = get_media_url_and_type(posts[0])
                caption = f"New post from @{username}\n\n👀 Watch here: https://www.tiktok.com/@{username}"
                
                try:
                    if media_url:
                        r = requests.get(media_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
                        if r.status_code == 200:
                            if media_type == 'video':
                                requests.post(
                                    f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendVideo",
                                    data={
                                        'chat_id': ALERT_CHAT_ID,
                                        'caption': caption
                                    },
                                    files={'video': r.content}
                                )
                            elif media_type == 'photo':
                                requests.post(
                                    f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto",
                                    data={
                                        'chat_id': ALERT_CHAT_ID,
                                        'caption': caption
                                    },
                                    files={'photo': r.content}
                                )
                    # Update state using the encoded key
                    config["last_seen"][encoded_user] = latest_post_id
                    save_config()
                except Exception as e:
                    print(f"Error sending alert: {e}")
        
        print(f"Checked all users. Sleeping for 300 seconds...")
        time.sleep(300)

if __name__ == "__main__":
    os.environ['START_TIME'] = str(int(time.time()))
    
    monitor_thread = threading.Thread(target=monitor_loop)
    monitor_thread.start()
    
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("add", add_user))
    app.add_handler(CommandHandler("remove", remove_user))
    app.add_handler(CommandHandler("list", list_users))
    app.add_handler(CommandHandler("online", online))
    
    print("Telegram Bot is listening...")
    app.run_polling()
