# -*- coding: utf-8 -*-
import telebot
import subprocess
import os
import zipfile
import tempfile
import shutil
from telebot import types
import time
from datetime import datetime, timedelta
import psutil
import sqlite3
import logging
import threading
import re
import sys
import atexit
import requests
from dotenv import load_dotenv
from flask import Flask
from threading import Thread

# --- Load environment variables ---
load_dotenv()

# --- Flask Keep Alive ---
app = Flask('')

@app.route('/')
def home():
    return "🤖 Demon Hosting Bot is Running"

# --- Configuration (Environment Variables) ---
TOKEN = os.getenv('BOT_TOKEN')
OWNER_ID = int(os.getenv('OWNER_ID', '0'))
ADMIN_ID = int(os.getenv('ADMIN_ID', str(OWNER_ID)))
YOUR_USERNAME = os.getenv('YOUR_USERNAME', 'PR4MOD_H4X')
UPDATE_CHANNEL = os.getenv('UPDATE_CHANNEL', 'pramodd_750x')

if not TOKEN:
    raise ValueError("BOT_TOKEN environment variable is not set!")

if not OWNER_ID:
    raise ValueError("OWNER_ID environment variable is not set!")

FREE_USER_LIMIT = 2
SUBSCRIBED_USER_LIMIT = 20
ADMIN_LIMIT = 999
OWNER_LIMIT = float('inf')

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
UPLOAD_BOTS_DIR = os.path.join(BASE_DIR, 'upload_bots')
IROTECH_DIR = os.path.join(BASE_DIR, 'inf')
DATABASE_PATH = os.path.join(IROTECH_DIR, 'bot_data.db')

os.makedirs(UPLOAD_BOTS_DIR, exist_ok=True)
os.makedirs(IROTECH_DIR, exist_ok=True)

bot = telebot.TeleBot(TOKEN)

# --- Data structures ---
bot_scripts = {}
user_subscriptions = {}
user_files = {}
active_users = set()
admin_ids = {ADMIN_ID, OWNER_ID}
banned_users = set()
user_limits = {}
bot_locked = False
mandatory_channels = {}
DB_LOCK = threading.Lock()
user_states = {}

# --- Logging ---
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- Database Setup ---
def init_db():
    try:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS subscriptions
                     (user_id INTEGER PRIMARY KEY, expiry TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS user_files
                     (user_id INTEGER, file_name TEXT, file_type TEXT,
                      PRIMARY KEY (user_id, file_name))''')
        c.execute('''CREATE TABLE IF NOT EXISTS active_users
                     (user_id INTEGER PRIMARY KEY, join_date TEXT, last_seen TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS admins
                     (user_id INTEGER PRIMARY KEY, added_by INTEGER, added_date TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS banned_users
                     (user_id INTEGER PRIMARY KEY, reason TEXT, banned_by INTEGER, ban_date TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS user_limits
                     (user_id INTEGER PRIMARY KEY, file_limit INTEGER, set_by INTEGER, set_date TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS mandatory_channels
                     (channel_id TEXT PRIMARY KEY, channel_username TEXT, channel_name TEXT,
                      added_by INTEGER, added_date TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS install_logs
                     (id INTEGER PRIMARY KEY AUTOINCREMENT,
                      user_id INTEGER, module_name TEXT, package_name TEXT,
                      status TEXT, log TEXT, install_date TEXT)''')
        
        c.execute('INSERT OR IGNORE INTO admins (user_id, added_by, added_date) VALUES (?, ?, ?)', 
                  (OWNER_ID, OWNER_ID, datetime.now().isoformat()))
        if ADMIN_ID != OWNER_ID:
            c.execute('INSERT OR IGNORE INTO admins (user_id, added_by, added_date) VALUES (?, ?, ?)', 
                      (ADMIN_ID, OWNER_ID, datetime.now().isoformat()))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"DB init error: {e}")

def load_data():
    try:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        c = conn.cursor()
        c.execute('SELECT user_id, expiry FROM subscriptions')
        for user_id, expiry in c.fetchall():
            try:
                user_subscriptions[user_id] = {'expiry': datetime.fromisoformat(expiry)}
            except:
                pass
        c.execute('SELECT user_id, file_name, file_type FROM user_files')
        for user_id, file_name, file_type in c.fetchall():
            if user_id not in user_files:
                user_files[user_id] = []
            user_files[user_id].append((file_name, file_type))
        c.execute('SELECT user_id FROM active_users')
        active_users.update(user_id for (user_id,) in c.fetchall())
        c.execute('SELECT user_id FROM admins')
        admin_ids.update(user_id for (user_id,) in c.fetchall())
        c.execute('SELECT user_id FROM banned_users')
        banned_users.update(user_id for (user_id,) in c.fetchall())
        c.execute('SELECT user_id, file_limit FROM user_limits')
        for user_id, file_limit in c.fetchall():
            user_limits[user_id] = file_limit
        c.execute('SELECT channel_id, channel_username, channel_name FROM mandatory_channels')
        for channel_id, channel_username, channel_name in c.fetchall():
            mandatory_channels[channel_id] = {'username': channel_username, 'name': channel_name}
        conn.close()
    except Exception as e:
        logger.error(f"Load data error: {e}")

init_db()
load_data()

# --- Helper Functions ---
def is_user_banned(user_id):
    return user_id in banned_users

def get_user_folder(user_id):
    user_folder = os.path.join(UPLOAD_BOTS_DIR, str(user_id))
    os.makedirs(user_folder, exist_ok=True)
    return user_folder

def get_user_file_limit(user_id):
    if user_id == OWNER_ID: return OWNER_LIMIT
    if user_id in admin_ids: return ADMIN_LIMIT
    if user_id in user_limits: return user_limits[user_id]
    if user_id in user_subscriptions and user_subscriptions[user_id]['expiry'] > datetime.now():
        return SUBSCRIBED_USER_LIMIT
    return FREE_USER_LIMIT

def get_user_file_count(user_id):
    return len(user_files.get(user_id, []))

def is_bot_running(script_owner_id, file_name):
    script_key = f"{script_owner_id}_{file_name}"
    script_info = bot_scripts.get(script_key)
    if script_info and script_info.get('process'):
        try:
            proc = psutil.Process(script_info['process'].pid)
            is_running = proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE
            if not is_running:
                if script_key in bot_scripts:
                    del bot_scripts[script_key]
            return is_running
        except:
            if script_key in bot_scripts:
                del bot_scripts[script_key]
            return False
    return False

def kill_process_tree(process_info):
    try:
        if 'log_file' in process_info and hasattr(process_info['log_file'], 'close'):
            try:
                process_info['log_file'].close()
            except:
                pass
        process = process_info.get('process')
        if process and hasattr(process, 'pid'):
            try:
                parent = psutil.Process(process.pid)
                children = parent.children(recursive=True)
                for child in children:
                    try:
                        child.terminate()
                    except:
                        try:
                            child.kill()
                        except:
                            pass
                try:
                    parent.terminate()
                except:
                    try:
                        parent.kill()
                    except:
                        pass
            except:
                pass
    except:
        pass

def save_user_file(user_id, file_name, file_type='py'):
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        c = conn.cursor()
        try:
            c.execute('INSERT OR REPLACE INTO user_files (user_id, file_name, file_type) VALUES (?, ?, ?)',
                      (user_id, file_name, file_type))
            conn.commit()
            if user_id not in user_files: user_files[user_id] = []
            user_files[user_id] = [(fn, ft) for fn, ft in user_files[user_id] if fn != file_name]
            user_files[user_id].append((file_name, file_type))
        except Exception as e:
            logger.error(f"Error saving file: {e}")
        finally:
            conn.close()

def remove_user_file_db(user_id, file_name):
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        c = conn.cursor()
        try:
            c.execute('DELETE FROM user_files WHERE user_id = ? AND file_name = ?', (user_id, file_name))
            conn.commit()
            if user_id in user_files:
                user_files[user_id] = [f for f in user_files[user_id] if f[0] != file_name]
                if not user_files[user_id]: del user_files[user_id]
        except Exception as e:
            logger.error(f"Error removing file: {e}")
        finally:
            conn.close()

def add_active_user(user_id):
    active_users.add(user_id)
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        c = conn.cursor()
        try:
            join_date = datetime.now().isoformat()
            c.execute('INSERT OR REPLACE INTO active_users (user_id, join_date, last_seen) VALUES (?, ?, ?)', 
                      (user_id, join_date, join_date))
            conn.commit()
        except Exception as e:
            logger.error(f"Error adding active user: {e}")
        finally:
            conn.close()

def check_mandatory_subscription(user_id):
    if not mandatory_channels:
        return True, []
    not_joined = []
    for channel_id, channel_info in mandatory_channels.items():
        try:
            chat_member = bot.get_chat_member(channel_id, user_id)
            if chat_member.status not in ['member', 'administrator', 'creator']:
                not_joined.append((channel_id, channel_info))
        except:
            not_joined.append((channel_id, channel_info))
    if not_joined:
        return False, not_joined
    return True, []

# --- STYLISH MENU CREATION ---
def create_main_menu(user_id):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    
    if user_id in admin_ids:
        layout = [
            ["📤 Upload File", "📂 My Files"],
            ["⚡ Bot Speed", "📊 Statistics"],
            ["📢 Updates", "📞 Contact"],
            ["📦 Manual Install", "🆘 Help"],
            ["👑 Admin Panel", "💳 Subscriptions"],
            ["📢 Broadcast", "🔒 Lock Bot"],
            ["🟢 Run All", "📢 Channel"],
            ["👥 User Mgmt", "⚙️ Settings"]
        ]
    else:
        layout = [
            ["📤 Upload File", "📂 My Files"],
            ["⚡ Bot Speed", "📊 Statistics"],
            ["📢 Updates", "📞 Contact"],
            ["📦 Manual Install", "🆘 Help"]
        ]
    
    for row in layout:
        markup.add(*[types.KeyboardButton(text) for text in row])
    return markup

def create_admin_panel_menu():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    layout = [
        ["➕ Add Admin", "➖ Remove Admin"],
        ["📋 List Admins", "👤 User Info"],
        ["👥 All Users", "📊 User Stats"],
        ["📝 Set Limit", "🗑️ Remove Limit"],
        ["🚫 Ban User", "✅ Unban User"],
        ["📢 Add Channel", "❌ Remove Channel"],
        ["📋 List Channels", "📦 Install Module"],
        ["🔙 Back To Main"]
    ]
    for row in layout:
        markup.add(*[types.KeyboardButton(text) for text in row])
    return markup

def create_subscription_menu():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    layout = [
        ["➕ Add Subscription", "➖ Remove Subscription"],
        ["📋 Check Subscription", "📊 Sub Stats"],
        ["🔙 Back To Main"]
    ]
    for row in layout:
        markup.add(*[types.KeyboardButton(text) for text in row])
    return markup

def create_settings_menu():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    layout = [
        ["💻 System Info", "📈 Performance"],
        ["🧹 Cleanup", "📋 Install Logs"],
        ["🔒 Lock Bot", "🔙 Back To Main"]
    ]
    for row in layout:
        markup.add(*[types.KeyboardButton(text) for text in row])
    return markup

def create_user_management_menu():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    layout = [
        ["🚫 Ban User", "✅ Unban User"],
        ["👤 User Info", "👥 All Users"],
        ["📝 Set Limit", "🗑️ Remove Limit"],
        ["🔙 Back To Main"]
    ]
    for row in layout:
        markup.add(*[types.KeyboardButton(text) for text in row])
    return markup

# --- FILE MANAGEMENT INLINE MENU ---
def create_file_menu(user_id, file_name, is_running):
    markup = types.InlineKeyboardMarkup(row_width=2)
    buttons = []
    
    if is_running:
        buttons.append(types.InlineKeyboardButton("⏹️ Stop", callback_data=f"stop_{user_id}_{file_name}"))
        buttons.append(types.InlineKeyboardButton("🔄 Restart", callback_data=f"restart_{user_id}_{file_name}"))
    else:
        buttons.append(types.InlineKeyboardButton("▶️ Start", callback_data=f"start_{user_id}_{file_name}"))
    
    buttons.append(types.InlineKeyboardButton("🗑️ Delete", callback_data=f"delete_{user_id}_{file_name}"))
    buttons.append(types.InlineKeyboardButton("📜 Logs", callback_data=f"logs_{user_id}_{file_name}"))
    buttons.append(types.InlineKeyboardButton("🔙 Back", callback_data="back_to_files"))
    
    for i in range(0, len(buttons), 2):
        if i+1 < len(buttons):
            markup.add(buttons[i], buttons[i+1])
        else:
            markup.add(buttons[i])
    
    return markup

# --- SCRIPT RUNNING FUNCTIONS ---
TELEGRAM_MODULES = {
    'telebot': 'pyTelegramBotAPI',
    'telegram': 'python-telegram-bot',
    'aiogram': 'aiogram',
    'pyrogram': 'pyrogram',
    'telethon': 'telethon',
    'requests': 'requests',
    'pillow': 'Pillow',
    'cv2': 'opencv-python',
    'yaml': 'PyYAML',
    'dotenv': 'python-dotenv',
    'psutil': 'psutil',
    'flask': 'Flask'
}

def run_script(script_path, script_owner_id, user_folder, file_name, message):
    script_key = f"{script_owner_id}_{file_name}"
    try:
        log_file_path = os.path.join(user_folder, f"{os.path.splitext(file_name)[0]}.log")
        log_file = open(log_file_path, 'w', encoding='utf-8', errors='ignore')
        process = subprocess.Popen([sys.executable, script_path], cwd=user_folder, 
                                   stdout=log_file, stderr=log_file, stdin=subprocess.PIPE)
        bot_scripts[script_key] = {'process': process, 'log_file': log_file, 'file_name': file_name,
                                   'script_owner_id': script_owner_id, 'script_key': script_key}
        bot.reply_to(message, f"✅ Started `{file_name}` (PID: {process.pid})", parse_mode='Markdown')
    except Exception as e:
        bot.reply_to(message, f"❌ Error: {str(e)}")

def run_js_script(script_path, script_owner_id, user_folder, file_name, message):
    script_key = f"{script_owner_id}_{file_name}"
    try:
        log_file_path = os.path.join(user_folder, f"{os.path.splitext(file_name)[0]}.log")
        log_file = open(log_file_path, 'w', encoding='utf-8', errors='ignore')
        process = subprocess.Popen(['node', script_path], cwd=user_folder,
                                   stdout=log_file, stderr=log_file, stdin=subprocess.PIPE)
        bot_scripts[script_key] = {'process': process, 'log_file': log_file, 'file_name': file_name,
                                   'script_owner_id': script_owner_id, 'script_key': script_key}
        bot.reply_to(message, f"✅ Started `{file_name}` (PID: {process.pid})", parse_mode='Markdown')
    except Exception as e:
        bot.reply_to(message, f"❌ Error: {str(e)}")

# --- MAIN MENU HANDLERS ---

def handle_upload_file(message):
    user_id = message.from_user.id
    if is_user_banned(user_id):
        bot.reply_to(message, "❌ You Are Banned.")
        return
    if bot_locked and user_id not in admin_ids:
        bot.reply_to(message, "⚠️ Bot Locked.")
        return
    file_limit = get_user_file_limit(user_id)
    if get_user_file_count(user_id) >= file_limit:
        bot.reply_to(message, f"⚠️ File Limit Reached ({file_limit}).")
        return
    bot.reply_to(message, "📤 Send `.py`, `.js`, or `.zip` File.")

def handle_my_files(message):
    user_id = message.from_user.id
    if is_user_banned(user_id):
        bot.reply_to(message, "❌ You Are Banned.")
        return
    
    user_files_list = user_files.get(user_id, [])
    if not user_files_list:
        bot.reply_to(message, "📂 No Files Uploaded Yet.")
        return
    
    response = "📂 **Your Files:**\n━━━━━━━━━━━━━━━━\n"
    
    for file_name, file_type in sorted(user_files_list):
        is_running = is_bot_running(user_id, file_name)
        status = "🟢 Running" if is_running else "🔴 Stopped"
        response += f"\n📄 **{file_name}** ({file_type})\n"
        response += f"└ Status: {status}\n"
        response += "└ Use buttons below\n"
        response += "━━━━━━━━━━━━━━━━\n"
    
    response += "\n💡 Click buttons below to manage files."
    bot.reply_to(message, response, parse_mode='Markdown')
    
    for file_name, file_type in sorted(user_files_list):
        is_running = is_bot_running(user_id, file_name)
        markup = create_file_menu(user_id, file_name, is_running)
        status = "🟢 Running" if is_running else "🔴 Stopped"
        bot.send_message(message.chat.id, f"📄 **{file_name}** - {status}", 
                        reply_markup=markup, parse_mode='Markdown')
        time.sleep(0.3)

def handle_bot_speed(message):
    user_id = message.from_user.id
    if is_user_banned(user_id):
        bot.reply_to(message, "❌ You Are Banned.")
        return
    start = time.time()
    wait = bot.reply_to(message, "🏃 Testing Speed...")
    response = round((time.time() - start) * 1000, 2)
    bot.edit_message_text(f"⚡ Response: {response}ms\n🔒 Status: {'Locked' if bot_locked else 'Unlocked'}", 
                         message.chat.id, wait.message_id)

def handle_statistics(message):
    user_id = message.from_user.id
    if is_user_banned(user_id):
        bot.reply_to(message, "❌ You Are Banned.")
        return
    stats = (f"📊 **Bot Stats**\n\n"
             f"👥 Users: {len(active_users)}\n"
             f"🚫 Banned: {len(banned_users)}\n"
             f"📂 Files: {sum(len(f) for f in user_files.values())}\n"
             f"🟢 Running: {len(bot_scripts)}\n"
             f"🔒 Status: {'Locked' if bot_locked else 'Unlocked'}")
    bot.reply_to(message, stats, parse_mode='Markdown')

def handle_updates(message):
    bot.reply_to(message, f"📢 Join Our Updates Channel: [Click Here](https://t.me/{UPDATE_CHANNEL})", 
                parse_mode='Markdown', disable_web_page_preview=True)

def handle_contact(message):
    bot.reply_to(message, f"📞 Contact Owner: [Click Here](https://t.me/{YOUR_USERNAME})", 
                parse_mode='Markdown', disable_web_page_preview=True)

def handle_manual_install(message):
    user_id = message.from_user.id
    if is_user_banned(user_id):
        bot.reply_to(message, "❌ You Are Banned.")
        return
    if bot_locked and user_id not in admin_ids:
        bot.reply_to(message, "⚠️ Bot Locked.")
        return
    
    user_states[user_id] = {'state': 'waiting_module', 'action': 'install'}
    msg = bot.reply_to(message, "📦 **Send Module Name**\n\n"
                               "For Python: `requests`\n"
                               "For Node: `npm:express`\n"
                               "Send `/cancel` to cancel")
    bot.register_next_step_handler(msg, process_manual_install)

def process_manual_install(message):
    user_id = message.from_user.id
    if message.text.lower() == '/cancel':
        bot.reply_to(message, "❌ Cancelled.")
        if user_id in user_states:
            del user_states[user_id]
        return
    
    module_name = message.text.strip()
    if module_name.lower().startswith('npm:'):
        module_name = module_name[4:].strip()
        user_folder = get_user_folder(user_id)
        attempt_install_npm(module_name, user_folder, message)
    else:
        attempt_install_pip(module_name, message)
    
    if user_id in user_states:
        del user_states[user_id]

def attempt_install_pip(module_name, message):
    package_name = TELEGRAM_MODULES.get(module_name.lower(), module_name)
    try:
        bot.reply_to(message, f"🔄 Installing `{package_name}`...", parse_mode='Markdown')
        result = subprocess.run([sys.executable, '-m', 'pip', 'install', package_name], 
                               capture_output=True, text=True, encoding='utf-8', errors='ignore')
        if result.returncode == 0:
            bot.reply_to(message, f"✅ `{package_name}` Installed.", parse_mode='Markdown')
            return True
        else:
            bot.reply_to(message, f"❌ Failed to Install `{package_name}`", parse_mode='Markdown')
            return False
    except Exception as e:
        bot.reply_to(message, f"❌ Error: {str(e)}")
        return False

def attempt_install_npm(module_name, user_folder, message):
    try:
        bot.reply_to(message, f"🔄 Installing Node `{module_name}`...", parse_mode='Markdown')
        result = subprocess.run(['npm', 'install', module_name], cwd=user_folder,
                               capture_output=True, text=True, encoding='utf-8', errors='ignore')
        if result.returncode == 0:
            bot.reply_to(message, f"✅ Node `{module_name}` Installed.", parse_mode='Markdown')
            return True
        else:
            bot.reply_to(message, f"❌ Failed to Install `{module_name}`", parse_mode='Markdown')
            return False
    except:
        bot.reply_to(message, "❌ Npm Not Found.")
        return False

def handle_help(message):
    help_text = """
🤖 **Demon Hosting Bot**

📌 **Main Menu Buttons:**
• 📤 Upload File - Upload .py/.js/.zip
• 📂 My Files - Manage your files
• ⚡ Bot Speed - Check response time
• 📊 Statistics - View bot stats
• 📢 Updates - Join updates channel
• 📞 Contact - Contact support
• 📦 Manual Install - Install modules
• 🆘 Help - This help menu

📁 **File Management:**
• Upload `.py`, `.js`, `.zip` files
• Auto-install missing dependencies
• Start/Stop/Restart/Delete files
• View logs of running files

👑 **Admin Features:**
• User Management (Ban/Unban)
• Subscription Management
• File Limit Control
• Channel Management
• Broadcast Messages
• System Monitoring

💡 All features accessible via buttons!
"""
    bot.reply_to(message, help_text, parse_mode='Markdown')

# --- ADMIN PANEL HANDLERS ---

def handle_admin_panel(message):
    if message.from_user.id not in admin_ids:
        bot.reply_to(message, "⚠️ Admin Only.")
        return
    bot.reply_to(message, "👑 **Admin Panel**\n\nSelect an option below:", 
                parse_mode='Markdown', reply_markup=create_admin_panel_menu())

def handle_subscriptions(message):
    if message.from_user.id not in admin_ids:
        bot.reply_to(message, "⚠️ Admin Only.")
        return
    bot.reply_to(message, "💳 **Subscription Management**\n\nSelect an option below:", 
                parse_mode='Markdown', reply_markup=create_subscription_menu())

def handle_broadcast(message):
    if message.from_user.id not in admin_ids:
        bot.reply_to(message, "⚠️ Admin Only.")
        return
    user_states[message.from_user.id] = {'state': 'waiting_broadcast'}
    msg = bot.reply_to(message, "📢 **Send Broadcast Message**\n\nSend `/cancel` to cancel")
    bot.register_next_step_handler(msg, process_broadcast)

def process_broadcast(message):
    user_id = message.from_user.id
    if user_id not in admin_ids:
        return
    if message.text.lower() == '/cancel':
        bot.reply_to(message, "❌ Cancelled.")
        if user_id in user_states:
            del user_states[user_id]
        return
    
    text = message.text
    sent = 0
    for uid in list(active_users):
        try:
            bot.send_message(uid, text)
            sent += 1
            time.sleep(0.05)
        except:
            pass
    bot.reply_to(message, f"✅ Broadcast Sent to {sent} Users.")
    if user_id in user_states:
        del user_states[user_id]

def handle_lock_bot(message):
    global bot_locked
    if message.from_user.id not in admin_ids:
        bot.reply_to(message, "⚠️ Admin Only.")
        return
    bot_locked = not bot_locked
    bot.reply_to(message, f"🔒 Bot {'Locked' if bot_locked else 'Unlocked'}.")

def handle_run_all(message):
    if message.from_user.id not in admin_ids:
        bot.reply_to(message, "⚠️ Admin Only.")
        return
    bot.reply_to(message, "⏳ Starting All Scripts...")
    count = 0
    for uid, files in user_files.items():
        for fname, ftype in files:
            if not is_bot_running(uid, fname):
                fpath = os.path.join(get_user_folder(uid), fname)
                if os.path.exists(fpath):
                    if ftype == 'py':
                        threading.Thread(target=run_script, args=(fpath, uid, get_user_folder(uid), fname, message)).start()
                    else:
                        threading.Thread(target=run_js_script, args=(fpath, uid, get_user_folder(uid), fname, message)).start()
                    count += 1
                    time.sleep(0.3)
    bot.reply_to(message, f"✅ Started {count} Scripts.")

def handle_channel(message):
    if message.from_user.id not in admin_ids:
        bot.reply_to(message, "⚠️ Admin Only.")
        return
    user_states[message.from_user.id] = {'state': 'waiting_channel'}
    msg = bot.reply_to(message, "📢 **Send Channel Id to Add**\n\n"
                                "Format: `-1001234567890`\n"
                                "Send `/cancel` to cancel")
    bot.register_next_step_handler(msg, process_channel)

def process_channel(message):
    user_id = message.from_user.id
    if user_id not in admin_ids:
        return
    if message.text.lower() == '/cancel':
        bot.reply_to(message, "❌ Cancelled.")
        if user_id in user_states:
            del user_states[user_id]
        return
    
    channel = message.text.strip()
    try:
        chat = bot.get_chat(channel)
        cid = str(chat.id)
        cname = chat.title
        cusername = f"@{chat.username}" if chat.username else ""
        mandatory_channels[cid] = {'username': cusername, 'name': cname}
        bot.reply_to(message, f"✅ Added Channel: {cname}")
    except Exception as e:
        bot.reply_to(message, f"❌ Error: {str(e)}")
    if user_id in user_states:
        del user_states[user_id]

def handle_settings(message):
    if message.from_user.id not in admin_ids:
        bot.reply_to(message, "⚠️ Admin Only.")
        return
    bot.reply_to(message, "⚙️ **Settings**\n\nSelect an option below:", 
                parse_mode='Markdown', reply_markup=create_settings_menu())

def handle_user_mgmt(message):
    if message.from_user.id not in admin_ids:
        bot.reply_to(message, "⚠️ Admin Only.")
        return
    bot.reply_to(message, "👥 **User Management**\n\nSelect an option below:", 
                parse_mode='Markdown', reply_markup=create_user_management_menu())

# --- ADMIN SPECIFIC COMMAND HANDLERS ---

def handle_add_admin(message):
    if message.from_user.id != OWNER_ID:
        bot.reply_to(message, "⚠️ Owner Only.")
        return
    user_states[message.from_user.id] = {'state': 'waiting_add_admin'}
    msg = bot.reply_to(message, "➕ **Send User Id to Add Admin**\n\nSend `/cancel` to cancel")
    bot.register_next_step_handler(msg, process_add_admin)

def process_add_admin(message):
    user_id = message.from_user.id
    if user_id != OWNER_ID:
        return
    if message.text.lower() == '/cancel':
        bot.reply_to(message, "❌ Cancelled.")
        if user_id in user_states:
            del user_states[user_id]
        return
    try:
        new_admin = int(message.text.strip())
        if new_admin in admin_ids:
            bot.reply_to(message, "⚠️ Already Admin.")
        else:
            admin_ids.add(new_admin)
            bot.reply_to(message, f"✅ Added Admin `{new_admin}`")
            try:
                bot.send_message(new_admin, "🎉 You Are Now An Admin!")
            except:
                pass
    except:
        bot.reply_to(message, "⚠️ Invalid User Id.")
    if user_id in user_states:
        del user_states[user_id]

def handle_remove_admin(message):
    if message.from_user.id != OWNER_ID:
        bot.reply_to(message, "⚠️ Owner Only.")
        return
    user_states[message.from_user.id] = {'state': 'waiting_remove_admin'}
    msg = bot.reply_to(message, "➖ **Send User Id to Remove Admin**\n\nSend `/cancel` to cancel")
    bot.register_next_step_handler(msg, process_remove_admin)

def process_remove_admin(message):
    user_id = message.from_user.id
    if user_id != OWNER_ID:
        return
    if message.text.lower() == '/cancel':
        bot.reply_to(message, "❌ Cancelled.")
        if user_id in user_states:
            del user_states[user_id]
        return
    try:
        rem_admin = int(message.text.strip())
        if rem_admin == OWNER_ID:
            bot.reply_to(message, "⚠️ Cannot Remove Owner.")
        elif rem_admin not in admin_ids:
            bot.reply_to(message, "⚠️ Not An Admin.")
        else:
            admin_ids.remove(rem_admin)
            bot.reply_to(message, f"✅ Removed Admin `{rem_admin}`")
    except:
        bot.reply_to(message, "⚠️ Invalid User Id.")
    if user_id in user_states:
        del user_states[user_id]

def handle_list_admins(message):
    if message.from_user.id not in admin_ids:
        bot.reply_to(message, "⚠️ Admin Only.")
        return
    admin_list = "\n".join(f"• `{a}` {'(Owner)' if a == OWNER_ID else ''}" for a in sorted(admin_ids))
    bot.reply_to(message, f"👑 **Admins**\n\n{admin_list}", parse_mode='Markdown')

# --- SUBSCRIPTION HANDLERS ---

def handle_add_subscription(message):
    if message.from_user.id not in admin_ids:
        bot.reply_to(message, "⚠️ Admin Only.")
        return
    user_states[message.from_user.id] = {'state': 'waiting_add_sub'}
    msg = bot.reply_to(message, "➕ **Send User Id and Days**\n\nFormat: `user_id days`\nSend `/cancel` to cancel")
    bot.register_next_step_handler(msg, process_add_subscription)

def process_add_subscription(message):
    user_id = message.from_user.id
    if user_id not in admin_ids:
        return
    if message.text.lower() == '/cancel':
        bot.reply_to(message, "❌ Cancelled.")
        if user_id in user_states:
            del user_states[user_id]
        return
    try:
        parts = message.text.strip().split()
        if len(parts) < 2:
            bot.reply_to(message, "⚠️ Format: `user_id days`")
            return
        uid = int(parts[0])
        days = int(parts[1])
        expiry = datetime.now() + timedelta(days=days)
        user_subscriptions[uid] = {'expiry': expiry}
        bot.reply_to(message, f"✅ Added {days} Days For `{uid}`")
    except:
        bot.reply_to(message, "⚠️ Invalid Format.")
    if user_id in user_states:
        del user_states[user_id]

def handle_remove_subscription(message):
    if message.from_user.id not in admin_ids:
        bot.reply_to(message, "⚠️ Admin Only.")
        return
    user_states[message.from_user.id] = {'state': 'waiting_remove_sub'}
    msg = bot.reply_to(message, "➖ **Send User Id to Remove Subscription**\n\nSend `/cancel` to cancel")
    bot.register_next_step_handler(msg, process_remove_subscription)

def process_remove_subscription(message):
    user_id = message.from_user.id
    if user_id not in admin_ids:
        return
    if message.text.lower() == '/cancel':
        bot.reply_to(message, "❌ Cancelled.")
        if user_id in user_states:
            del user_states[user_id]
        return
    try:
        uid = int(message.text.strip())
        if uid in user_subscriptions:
            del user_subscriptions[uid]
            bot.reply_to(message, f"✅ Removed Subscription For `{uid}`")
        else:
            bot.reply_to(message, f"⚠️ No Subscription For `{uid}`")
    except:
        bot.reply_to(message, "⚠️ Invalid User Id.")
    if user_id in user_states:
        del user_states[user_id]

def handle_check_subscription(message):
    if message.from_user.id not in admin_ids:
        bot.reply_to(message, "⚠️ Admin Only.")
        return
    user_states[message.from_user.id] = {'state': 'waiting_check_sub'}
    msg = bot.reply_to(message, "📋 **Send User Id to Check Subscription**\n\nSend `/cancel` to cancel")
    bot.register_next_step_handler(msg, process_check_subscription)

def process_check_subscription(message):
    user_id = message.from_user.id
    if user_id not in admin_ids:
        return
    if message.text.lower() == '/cancel':
        bot.reply_to(message, "❌ Cancelled.")
        if user_id in user_states:
            del user_states[user_id]
        return
    try:
        uid = int(message.text.strip())
        if uid in user_subscriptions:
            expiry = user_subscriptions[uid]['expiry']
            if expiry > datetime.now():
                bot.reply_to(message, f"✅ `{uid}` Active Until {expiry.strftime('%Y-%m-%d')}")
            else:
                bot.reply_to(message, f"⚠️ `{uid}` Expired")
        else:
            bot.reply_to(message, f"ℹ️ `{uid}` No Subscription")
    except:
        bot.reply_to(message, "⚠️ Invalid User Id.")
    if user_id in user_states:
        del user_states[user_id]

# --- USER MANAGEMENT HANDLERS ---

def handle_ban_user(message):
    if message.from_user.id not in admin_ids:
        bot.reply_to(message, "⚠️ Admin Only.")
        return
    user_states[message.from_user.id] = {'state': 'waiting_ban'}
    msg = bot.reply_to(message, "🚫 **Send User Id and Reason**\n\nFormat: `user_id reason`\nSend `/cancel` to cancel")
    bot.register_next_step_handler(msg, process_ban_user)

def process_ban_user(message):
    user_id = message.from_user.id
    if user_id not in admin_ids:
        return
    if message.text.lower() == '/cancel':
        bot.reply_to(message, "❌ Cancelled.")
        if user_id in user_states:
            del user_states[user_id]
        return
    try:
        parts = message.text.strip().split()
        if len(parts) < 2:
            bot.reply_to(message, "⚠️ Format: `user_id reason`")
            return
        uid = int(parts[0])
        reason = ' '.join(parts[1:])
        if uid == OWNER_ID or uid in admin_ids:
            bot.reply_to(message, "⚠️ Cannot Ban Owner/Admin.")
        else:
            banned_users.add(uid)
            for fname, _ in user_files.get(uid, []):
                script_key = f"{uid}_{fname}"
                if script_key in bot_scripts:
                    kill_process_tree(bot_scripts[script_key])
                    del bot_scripts[script_key]
            bot.reply_to(message, f"✅ Banned `{uid}`\nReason: {reason}")
            try:
                bot.send_message(uid, f"🚫 You Have Been Banned!\nReason: {reason}")
            except:
                pass
    except:
        bot.reply_to(message, "⚠️ Invalid Format.")
    if user_id in user_states:
        del user_states[user_id]

def handle_unban_user(message):
    if message.from_user.id not in admin_ids:
        bot.reply_to(message, "⚠️ Admin Only.")
        return
    user_states[message.from_user.id] = {'state': 'waiting_unban'}
    msg = bot.reply_to(message, "✅ **Send User Id to Unban**\n\nSend `/cancel` to cancel")
    bot.register_next_step_handler(msg, process_unban_user)

def process_unban_user(message):
    user_id = message.from_user.id
    if user_id not in admin_ids:
        return
    if message.text.lower() == '/cancel':
        bot.reply_to(message, "❌ Cancelled.")
        if user_id in user_states:
            del user_states[user_id]
        return
    try:
        uid = int(message.text.strip())
        if uid not in banned_users:
            bot.reply_to(message, f"ℹ️ `{uid}` Not Banned.")
        else:
            banned_users.remove(uid)
            bot.reply_to(message, f"✅ Unbanned `{uid}`")
            try:
                bot.send_message(uid, "✅ You Have Been Unbanned!")
            except:
                pass
    except:
        bot.reply_to(message, "⚠️ Invalid User Id.")
    if user_id in user_states:
        del user_states[user_id]

def handle_user_info(message):
    if message.from_user.id not in admin_ids:
        bot.reply_to(message, "⚠️ Admin Only.")
        return
    user_states[message.from_user.id] = {'state': 'waiting_user_info'}
    msg = bot.reply_to(message, "👤 **Send User Id to Get Info**\n\nSend `/cancel` to cancel")
    bot.register_next_step_handler(msg, process_user_info)

def process_user_info(message):
    user_id = message.from_user.id
    if user_id not in admin_ids:
        return
    if message.text.lower() == '/cancel':
        bot.reply_to(message, "❌ Cancelled.")
        if user_id in user_states:
            del user_states[user_id]
        return
    try:
        uid = int(message.text.strip())
        status = "👑 Owner" if uid == OWNER_ID else "🛡️ Admin" if uid in admin_ids else "🚫 Banned" if uid in banned_users else "⭐ Premium" if uid in user_subscriptions else "🆓 Free"
        files = get_user_file_count(uid)
        limit = get_user_file_limit(uid)
        running = sum(1 for f, _ in user_files.get(uid, []) if is_bot_running(uid, f))
        bot.reply_to(message, f"👤 **User Info**\n\nId: `{uid}`\nStatus: {status}\nFiles: {files}/{limit}\nRunning: {running}", parse_mode='Markdown')
    except:
        bot.reply_to(message, "⚠️ Invalid User Id.")
    if user_id in user_states:
        del user_states[user_id]

def handle_all_users(message):
    if message.from_user.id not in admin_ids:
        bot.reply_to(message, "⚠️ Admin Only.")
        return
    if not active_users:
        bot.reply_to(message, "👥 No Active Users.")
        return
    users_list = list(active_users)[:50]
    text = f"👥 **Active Users:** ({len(active_users)} Total)\n\n"
    for i, uid in enumerate(users_list, 1):
        status = "👑" if uid == OWNER_ID else "🛡️" if uid in admin_ids else "🚫" if uid in banned_users else "⭐" if uid in user_subscriptions else "🆓"
        text += f"{i}. `{uid}` {status}\n"
    if len(active_users) > 50:
        text += f"\n... And {len(active_users) - 50} More Users"
    bot.reply_to(message, text, parse_mode='Markdown')

def handle_set_limit(message):
    if message.from_user.id not in admin_ids:
        bot.reply_to(message, "⚠️ Admin Only.")
        return
    user_states[message.from_user.id] = {'state': 'waiting_set_limit'}
    msg = bot.reply_to(message, "📝 **Send User Id and Limit**\n\nFormat: `user_id limit`\nSend `/cancel` to cancel")
    bot.register_next_step_handler(msg, process_set_limit)

def process_set_limit(message):
    user_id = message.from_user.id
    if user_id not in admin_ids:
        return
    if message.text.lower() == '/cancel':
        bot.reply_to(message, "❌ Cancelled.")
        if user_id in user_states:
            del user_states[user_id]
        return
    try:
        parts = message.text.strip().split()
        if len(parts) < 2:
            bot.reply_to(message, "⚠️ Format: `user_id limit`")
            return
        uid = int(parts[0])
        limit = int(parts[1])
        user_limits[uid] = limit
        bot.reply_to(message, f"✅ Set Limit {limit} For `{uid}`")
    except:
        bot.reply_to(message, "⚠️ Invalid Format.")
    if user_id in user_states:
        del user_states[user_id]

def handle_remove_limit(message):
    if message.from_user.id not in admin_ids:
        bot.reply_to(message, "⚠️ Admin Only.")
        return
    user_states[message.from_user.id] = {'state': 'waiting_remove_limit'}
    msg = bot.reply_to(message, "🗑️ **Send User Id to Remove Limit**\n\nSend `/cancel` to cancel")
    bot.register_next_step_handler(msg, process_remove_limit)

def process_remove_limit(message):
    user_id = message.from_user.id
    if user_id not in admin_ids:
        return
    if message.text.lower() == '/cancel':
        bot.reply_to(message, "❌ Cancelled.")
        if user_id in user_states:
            del user_states[user_id]
        return
    try:
        uid = int(message.text.strip())
        if uid in user_limits:
            del user_limits[uid]
            bot.reply_to(message, f"✅ Removed Limit For `{uid}`")
        else:
            bot.reply_to(message, f"ℹ️ No Limit For `{uid}`")
    except:
        bot.reply_to(message, "⚠️ Invalid User Id.")
    if user_id in user_states:
        del user_states[user_id]

# --- CHANNEL MANAGEMENT HANDLERS ---

def handle_add_channel(message):
    if message.from_user.id not in admin_ids:
        bot.reply_to(message, "⚠️ Admin Only.")
        return
    user_states[message.from_user.id] = {'state': 'waiting_add_channel'}
    msg = bot.reply_to(message, "📢 **Send Channel Id to Add**\n\nFormat: `-1001234567890`\nSend `/cancel` to cancel")
    bot.register_next_step_handler(msg, process_add_channel)

def process_add_channel(message):
    user_id = message.from_user.id
    if user_id not in admin_ids:
        return
    if message.text.lower() == '/cancel':
        bot.reply_to(message, "❌ Cancelled.")
        if user_id in user_states:
            del user_states[user_id]
        return
    channel = message.text.strip()
    try:
        chat = bot.get_chat(channel)
        cid = str(chat.id)
        cname = chat.title
        cusername = f"@{chat.username}" if chat.username else ""
        mandatory_channels[cid] = {'username': cusername, 'name': cname}
        bot.reply_to(message, f"✅ Added Channel: {cname}")
    except Exception as e:
        bot.reply_to(message, f"❌ Error: {str(e)}")
    if user_id in user_states:
        del user_states[user_id]

def handle_remove_channel(message):
    if message.from_user.id not in admin_ids:
        bot.reply_to(message, "⚠️ Admin Only.")
        return
    user_states[message.from_user.id] = {'state': 'waiting_remove_channel'}
    msg = bot.reply_to(message, "❌ **Send Channel Id to Remove**\n\nSend `/cancel` to cancel")
    bot.register_next_step_handler(msg, process_remove_channel)

def process_remove_channel(message):
    user_id = message.from_user.id
    if user_id not in admin_ids:
        return
    if message.text.lower() == '/cancel':
        bot.reply_to(message, "❌ Cancelled.")
        if user_id in user_states:
            del user_states[user_id]
        return
    cid = message.text.strip()
    if cid in mandatory_channels:
        name = mandatory_channels[cid].get('name', 'Unknown')
        del mandatory_channels[cid]
        bot.reply_to(message, f"✅ Removed Channel: {name}")
    else:
        bot.reply_to(message, "⚠️ Channel Not Found.")
    if user_id in user_states:
        del user_states[user_id]

def handle_list_channels(message):
    if message.from_user.id not in admin_ids:
        bot.reply_to(message, "⚠️ Admin Only.")
        return
    if not mandatory_channels:
        bot.reply_to(message, "📢 No Mandatory Channels.")
        return
    text = "📢 **Mandatory Channels**\n\n"
    for cid, info in mandatory_channels.items():
        text += f"• {info.get('name', 'Unknown')}\n  {info.get('username', cid)}\n\n"
    bot.reply_to(message, text, parse_mode='Markdown')

def handle_install_module(message):
    if message.from_user.id not in admin_ids:
        bot.reply_to(message, "⚠️ Admin Only.")
        return
    user_states[message.from_user.id] = {'state': 'waiting_install_module'}
    msg = bot.reply_to(message, "📦 **Send User Id and Module**\n\nFormat: `user_id module`\nSend `/cancel` to cancel")
    bot.register_next_step_handler(msg, process_install_module)

def process_install_module(message):
    user_id = message.from_user.id
    if user_id not in admin_ids:
        return
    if message.text.lower() == '/cancel':
        bot.reply_to(message, "❌ Cancelled.")
        if user_id in user_states:
            del user_states[user_id]
        return
    try:
        parts = message.text.strip().split()
        if len(parts) < 2:
            bot.reply_to(message, "⚠️ Format: `user_id module`")
            return
        uid = int(parts[0])
        module_name = ' '.join(parts[1:])
        if module_name.lower().startswith('npm:'):
            module_name = module_name[4:].strip()
            user_folder = get_user_folder(uid)
            attempt_install_npm(module_name, user_folder, message)
        else:
            attempt_install_pip(module_name, message)
        try:
            bot.send_message(uid, f"📦 Admin Installed `{module_name}` For You.")
        except:
            pass
    except:
        bot.reply_to(message, "⚠️ Invalid Format.")
    if user_id in user_states:
        del user_states[user_id]

# --- SETTINGS HANDLERS ---

def handle_system_info(message):
    if message.from_user.id not in admin_ids:
        bot.reply_to(message, "⚠️ Admin Only.")
        return
    import platform
    text = f"💻 **System Info**\n\nPython: {platform.python_version()}\nPlatform: {platform.platform()}\n"
    try:
        text += f"CPU: {psutil.cpu_percent()}%\nMemory: {psutil.virtual_memory().percent}%\n"
        text += f"Disk: {psutil.disk_usage('/').percent}%"
    except:
        pass
    bot.reply_to(message, text, parse_mode='Markdown')

def handle_performance(message):
    if message.from_user.id not in admin_ids:
        bot.reply_to(message, "⚠️ Admin Only.")
        return
    text = f"📈 **Performance**\n\nRunning: {len(bot_scripts)}\nTotal Files: {sum(len(f) for f in user_files.values())}\nUsers: {len(active_users)}\nSubs: {len(user_subscriptions)}"
    bot.reply_to(message, text, parse_mode='Markdown')

def handle_cleanup(message):
    if message.from_user.id not in admin_ids:
        bot.reply_to(message, "⚠️ Admin Only.")
        return
    cleaned = 0
    for user_dir in os.listdir(UPLOAD_BOTS_DIR):
        path = os.path.join(UPLOAD_BOTS_DIR, user_dir)
        if os.path.isdir(path) and not os.listdir(path):
            try:
                os.rmdir(path)
                cleaned += 1
            except:
                pass
    bot.reply_to(message, f"🧹 Cleaned {cleaned} Empty Dirs.")

def handle_install_logs(message):
    if message.from_user.id not in admin_ids:
        bot.reply_to(message, "⚠️ Admin Only.")
        return
    bot.reply_to(message, "📋 Install Logs - Check Database.")

# --- BACK TO MAIN MENU ---
def handle_back_to_main(message):
    user_id = message.from_user.id
    if is_user_banned(user_id):
        bot.reply_to(message, "❌ You Are Banned.")
        return
    bot.reply_to(message, "🏠 **Main Menu**", 
                parse_mode='Markdown', reply_markup=create_main_menu(user_id))

# --- START HANDLER ---
@bot.message_handler(commands=['start'])
def send_welcome(message):
    user_id = message.from_user.id
    chat_id = message.chat.id
    user_name = message.from_user.first_name

    if is_user_banned(user_id):
        bot.send_message(chat_id, "❌ You Are Banned.")
        return

    is_subscribed, not_joined = check_mandatory_subscription(user_id)
    if not is_subscribed and user_id not in admin_ids:
        msg = "📢 **Join Our Channels First:**\n\n"
        for channel_id, channel_info in not_joined:
            channel_name = channel_info.get('name', 'Channel')
            channel_username = channel_info.get('username', '')
            if channel_username:
                link = f"https://t.me/{channel_username.replace('@', '')}"
            else:
                link = f"https://t.me/c/{channel_id.replace('-100', '')}"
            msg += f"• [{channel_name}]({link})\n"
        bot.send_message(chat_id, msg, parse_mode='Markdown', disable_web_page_preview=True)
        return

    if user_id not in active_users:
        add_active_user(user_id)

    file_limit = get_user_file_limit(user_id)
    current_files = get_user_file_count(user_id)
    limit_str = str(file_limit) if file_limit != float('inf') else "Unlimited"
    
    if user_id == OWNER_ID:
        user_status = "👑 Owner"
    elif user_id in admin_ids:
        user_status = "🛡️ Admin"
    elif user_id in user_subscriptions:
        user_status = "⭐ Premium"
    else:
        user_status = "🆓 Free"

    welcome_msg = (f"〽️ Welcome, {user_name}!\n\n"
                   f"🆔 Id: `{user_id}`\n"
                   f"🔰 Status: {user_status}\n"
                   f"📁 Files: {current_files}/{limit_str}\n\n"
                   f"🤖 Host Python/Js Scripts.\n"
                   f"📦 Manual Install Available.\n\n"
                   f"👇 Use Buttons Below:")
    
    bot.send_message(chat_id, welcome_msg, reply_markup=create_main_menu(user_id), parse_mode='Markdown')

# --- DOCUMENT HANDLER ---
@bot.message_handler(content_types=['document'])
def handle_file_upload(message):
    user_id = message.from_user.id
    chat_id = message.chat.id
    
    if is_user_banned(user_id):
        bot.reply_to(message, "❌ You Are Banned.")
        return

    if bot_locked and user_id not in admin_ids:
        bot.reply_to(message, "⚠️ Bot Locked.")
        return

    doc = message.document
    file_name = doc.file_name
    if not file_name:
        bot.reply_to(message, "⚠️ No File Name.")
        return
    
    file_ext = os.path.splitext(file_name)[1].lower()
    if file_ext not in ['.py', '.js', '.zip']:
        bot.reply_to(message, "⚠️ Only `.py`, `.js`, `.zip` Allowed.")
        return
    
    if doc.file_size > 20 * 1024 * 1024:
        bot.reply_to(message, "⚠️ Max 20MB.")
        return

    file_limit = get_user_file_limit(user_id)
    if get_user_file_count(user_id) >= file_limit:
        bot.reply_to(message, f"⚠️ File Limit Reached ({file_limit}).")
        return

    try:
        try:
            bot.forward_message(OWNER_ID, chat_id, message.message_id)
        except:
            pass
        
        wait = bot.reply_to(message, f"⏳ Downloading `{file_name}`...")
        file_info = bot.get_file(doc.file_id)
        content = bot.download_file(file_info.file_path)
        bot.edit_message_text(f"✅ Downloaded. Processing...", chat_id, wait.message_id)
        
        user_folder = get_user_folder(user_id)
        
        if file_ext == '.zip':
            zip_path = os.path.join(user_folder, file_name)
            with open(zip_path, 'wb') as f:
                f.write(content)
            
            try:
                temp_dir = tempfile.mkdtemp(prefix=f"user_{user_id}_zip_")
                with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                    zip_ref.extractall(temp_dir)
                
                extracted = os.listdir(temp_dir)
                py_files = [f for f in extracted if f.endswith('.py')]
                js_files = [f for f in extracted if f.endswith('.js')]
                
                main_script = None
                file_type = None
                for p in ['main.py', 'bot.py', 'app.py']:
                    if p in py_files:
                        main_script = p
                        file_type = 'py'
                        break
                if not main_script:
                    for p in ['index.js', 'main.js', 'bot.js']:
                        if p in js_files:
                            main_script = p
                            file_type = 'js'
                            break
                if not main_script and py_files:
                    main_script = py_files[0]
                    file_type = 'py'
                elif not main_script and js_files:
                    main_script = js_files[0]
                    file_type = 'js'
                
                if not main_script:
                    bot.reply_to(message, "❌ No `.py` or `.js` Found In Zip!")
                    shutil.rmtree(temp_dir)
                    os.remove(zip_path)
                    return
                
                for item in os.listdir(temp_dir):
                    src = os.path.join(temp_dir, item)
                    dst = os.path.join(user_folder, item)
                    if os.path.exists(dst):
                        if os.path.isdir(dst):
                            shutil.rmtree(dst)
                        else:
                            os.remove(dst)
                    shutil.move(src, dst)
                shutil.rmtree(temp_dir)
                os.remove(zip_path)
                
                save_user_file(user_id, main_script, file_type)
                bot.reply_to(message, f"✅ Extracted. Starting `{main_script}`...", parse_mode='Markdown')
                
                main_path = os.path.join(user_folder, main_script)
                if file_type == 'py':
                    threading.Thread(target=run_script, args=(main_path, user_id, user_folder, main_script, message)).start()
                else:
                    threading.Thread(target=run_js_script, args=(main_path, user_id, user_folder, main_script, message)).start()
                    
            except Exception as e:
                bot.reply_to(message, f"❌ Zip Error: {str(e)}")
                try:
                    shutil.rmtree(temp_dir)
                except:
                    pass
                try:
                    os.remove(zip_path)
                except:
                    pass
        else:
            file_path = os.path.join(user_folder, file_name)
            with open(file_path, 'wb') as f:
                f.write(content)
            
            save_user_file(user_id, file_name, file_ext[1:])
            bot.reply_to(message, f"✅ File Saved: `{file_name}`", parse_mode='Markdown')
            
    except Exception as e:
        bot.reply_to(message, f"❌ Error: {str(e)}")

# --- INLINE BUTTON CALLBACK HANDLERS ---
@bot.callback_query_handler(func=lambda call: True)
def handle_callback(call):
    data = call.data
    user_id = call.from_user.id
    chat_id = call.message.chat.id
    message_id = call.message.message_id
    
    if data == "back_to_files":
        bot.delete_message(chat_id, message_id)
        handle_my_files(call.message)
        return
    
    try:
        parts = data.split('_', 2)
        if len(parts) < 3:
            bot.answer_callback_query(call.id, "❌ Invalid command.")
            return
        
        action = parts[0]
        owner_id = int(parts[1])
        file_name = parts[2] if len(parts) > 2 else ""
        
        if user_id != owner_id and user_id not in admin_ids:
            bot.answer_callback_query(call.id, "⚠️ You can only manage your own files.")
            return
        
        user_files_list = user_files.get(owner_id, [])
        file_info = next((f for f in user_files_list if f[0] == file_name), None)
        if not file_info:
            bot.answer_callback_query(call.id, "❌ File not found.")
            bot.delete_message(chat_id, message_id)
            return
        
        ftype = file_info[1]
        fpath = os.path.join(get_user_folder(owner_id), file_name)
        
        if action == 'start':
            if not os.path.exists(fpath):
                bot.answer_callback_query(call.id, "❌ File missing.")
                remove_user_file_db(owner_id, file_name)
                bot.delete_message(chat_id, message_id)
                return
            if is_bot_running(owner_id, file_name):
                bot.answer_callback_query(call.id, "⚠️ Already running.")
                return
            bot.answer_callback_query(call.id, f"⏳ Starting {file_name}...")
            bot.edit_message_text(f"⏳ Starting `{file_name}`...", chat_id, message_id, parse_mode='Markdown')
            if ftype == 'py':
                threading.Thread(target=run_script, args=(fpath, owner_id, get_user_folder(owner_id), file_name, call.message)).start()
            else:
                threading.Thread(target=run_js_script, args=(fpath, owner_id, get_user_folder(owner_id), file_name, call.message)).start()
                
        elif action == 'stop':
            skey = f"{owner_id}_{file_name}"
            if not is_bot_running(owner_id, file_name):
                bot.answer_callback_query(call.id, "⚠️ Not running.")
                return
            if skey in bot_scripts:
                kill_process_tree(bot_scripts[skey])
                del bot_scripts[skey]
            bot.answer_callback_query(call.id, f"✅ Stopped {file_name}")
            bot.edit_message_text(f"✅ Stopped `{file_name}`", chat_id, message_id, parse_mode='Markdown')
            time.sleep(1)
            bot.delete_message(chat_id, message_id)
            handle_my_files(call.message)
            
        elif action == 'restart':
            if not os.path.exists(fpath):
                bot.answer_callback_query(call.id, "❌ File missing.")
                remove_user_file_db(owner_id, file_name)
                bot.delete_message(chat_id, message_id)
                return
            skey = f"{owner_id}_{file_name}"
            if is_bot_running(owner_id, file_name):
                if skey in bot_scripts:
                    kill_process_tree(bot_scripts[skey])
                    del bot_scripts[skey]
                time.sleep(0.5)
            bot.answer_callback_query(call.id, f"⏳ Restarting {file_name}...")
            bot.edit_message_text(f"⏳ Restarting `{file_name}`...", chat_id, message_id, parse_mode='Markdown')
            if ftype == 'py':
                threading.Thread(target=run_script, args=(fpath, owner_id, get_user_folder(owner_id), file_name, call.message)).start()
            else:
                threading.Thread(target=run_js_script, args=(fpath, owner_id, get_user_folder(owner_id), file_name, call.message)).start()
                
        elif action == 'delete':
            skey = f"{owner_id}_{file_name}"
            if is_bot_running(owner_id, file_name):
                if skey in bot_scripts:
                    kill_process_tree(bot_scripts[skey])
                    del bot_scripts[skey]
                time.sleep(0.3)
            logpath = os.path.join(get_user_folder(owner_id), f"{os.path.splitext(file_name)[0]}.log")
            if os.path.exists(fpath):
                os.remove(fpath)
            if os.path.exists(logpath):
                os.remove(logpath)
            remove_user_file_db(owner_id, file_name)
            bot.answer_callback_query(call.id, f"🗑️ Deleted {file_name}")
            bot.delete_message(chat_id, message_id)
            handle_my_files(call.message)
            
        elif action == 'logs':
            logpath = os.path.join(get_user_folder(owner_id), f"{os.path.splitext(file_name)[0]}.log")
            if not os.path.exists(logpath):
                bot.answer_callback_query(call.id, "📭 No logs found.")
                return
            with open(logpath, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read()
            if len(content) > 4000:
                content = "...\n" + content[-4000:]
            if not content.strip():
                content = "(Empty)"
            bot.answer_callback_query(call.id, "📜 Logs fetched.")
            bot.send_message(chat_id, f"📜 **Logs for `{file_name}`:**\n```\n{content}\n```", parse_mode='Markdown')
            
    except Exception as e:
        bot.answer_callback_query(call.id, f"❌ Error: {str(e)}")

# --- BUTTON HANDLER MAPPING ---
button_handlers = {
    "📤 Upload File": handle_upload_file,
    "📂 My Files": handle_my_files,
    "⚡ Bot Speed": handle_bot_speed,
    "📊 Statistics": handle_statistics,
    "📢 Updates": handle_updates,
    "📞 Contact": handle_contact,
    "📦 Manual Install": handle_manual_install,
    "🆘 Help": handle_help,
    "👑 Admin Panel": handle_admin_panel,
    "💳 Subscriptions": handle_subscriptions,
    "📢 Broadcast": handle_broadcast,
    "🔒 Lock Bot": handle_lock_bot,
    "🟢 Run All": handle_run_all,
    "📢 Channel": handle_channel,
    "👥 User Mgmt": handle_user_mgmt,
    "⚙️ Settings": handle_settings,
    "➕ Add Admin": handle_add_admin,
    "➖ Remove Admin": handle_remove_admin,
    "📋 List Admins": handle_list_admins,
    "👤 User Info": handle_user_info,
    "👥 All Users": handle_all_users,
    "📊 User Stats": handle_statistics,
    "📝 Set Limit": handle_set_limit,
    "🗑️ Remove Limit": handle_remove_limit,
    "🚫 Ban User": handle_ban_user,
    "✅ Unban User": handle_unban_user,
    "📢 Add Channel": handle_add_channel,
    "❌ Remove Channel": handle_remove_channel,
    "📋 List Channels": handle_list_channels,
    "📦 Install Module": handle_install_module,
    "➕ Add Subscription": handle_add_subscription,
    "➖ Remove Subscription": handle_remove_subscription,
    "📋 Check Subscription": handle_check_subscription,
    "📊 Sub Stats": handle_statistics,
    "💻 System Info": handle_system_info,
    "📈 Performance": handle_performance,
    "🧹 Cleanup": handle_cleanup,
    "📋 Install Logs": handle_install_logs,
    "🔙 Back To Main": handle_back_to_main,
}

@bot.message_handler(func=lambda message: message.text in button_handlers)
def handle_buttons(message):
    handler = button_handlers.get(message.text)
    if handler:
        handler(message)

# --- CLEANUP ---
def cleanup():
    for key, info in bot_scripts.items():
        try:
            kill_process_tree(info)
        except:
            pass
    logger.info("Cleanup Done.")

atexit.register(cleanup)

# --- MAIN EXECUTION (RENDER COMPATIBLE) ---
if __name__ == '__main__':
    logger.info("="*50 + "\n🤖 Demon Hosting Bot Starting...\n" + "="*50)
    
    # Start bot in a separate thread
    def run_bot():
        logger.info("🚀 Starting Polling...")
        while True:
            try:
                bot.infinity_polling(logger_level=logging.INFO, timeout=60)
            except Exception as e:
                logger.error(f"Polling Error: {e}")
                time.sleep(10)
    
    bot_thread = threading.Thread(target=run_bot)
    bot_thread.daemon = True
    bot_thread.start()
    
    # Start Flask on Render PORT
    port = int(os.environ.get("PORT", 8080))
    logger.info(f"🌐 Flask starting on port {port}")
    app.run(host='0.0.0.0', port=port)