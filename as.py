import asyncio
from datetime import datetime, timedelta
import os
from threading import Thread
import urllib.parse
import time
import re
import json
import secrets
import string
from flask import Flask
import aiohttp

import asyncpg
from aiogram import Bot, Dispatcher, F
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart, CommandObject, ChatMemberUpdatedFilter, IS_MEMBER, IS_NOT_MEMBER, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CopyTextButton,
    ChatMemberUpdated
)
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder
from aiogram.exceptions import TelegramForbiddenError, TelegramBadRequest

# ============================================
# CONFIGURATION & INITIALIZATION
# ============================================

BOT_TOKEN = os.environ.get('BOT_TOKEN', '8970788656:AAGmGCBKEAhNSpaW0YTv7zztcLPTTQwYRGo')
ADMIN_ID = int(os.environ.get('ADMIN_ID', 8856827908))
DATABASE_URL = os.environ.get('DATABASE_URL')
WORKER_BOT_TOKEN = os.environ.get('WORKER_BOT_TOKEN', '').strip()

# Currency Conversion Rate (1 USD/USDT = 96.30 INR)
USD_TO_INR = 96.30

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

db_pool = None
BANNED_USERS_CACHE = set()
SUPPORT_REQUESTS_CACHE = {}  # In-memory store: {user_id: {"username": str, "message": str}}
MUST_JOIN_CHANNEL = None
BOT_USERNAME = "GmailEarnexBot"
BOT_STATUS = True           # True = ON, False = OFF
REF_STATUS = True           # True = ON, False = OFF (Silent Referral Disabling)
ULTRA_STATUS = True         # True = ON, False = OFF
SINGLE_TASK_STATUS = True   # True = 1/1 task (must wait for review), False = Unlimited tasks concurrently
SELL_GMAIL_STATUS = True    # True = Enabled, False = Disabled
DEFAULT_TASK_PASS_STATUS = True  # True = Fixed Default Password, False = Random Generated Password

# GLOBAL DYNAMIC RATES & DEFAULTS
DEFAULT_TASK_RATE = 50.0
GMAIL_SELL_RATE = 30.0
MIN_WITHDRAWAL_AMT = 150.0
DEFAULT_TASK_PASS = "TaskVerse@#"
REFERRAL_SELL_BONUS = 5.0
REFERRAL_TASK_BONUS = 7.0

# DYNAMIC FEES CONFIGURATION (INR)
UPI_FEES = 3.0
USDT_FEES = 3.0
ULTRA_FEES = 0.0

# ULTRA GATEWAY CONFIGURATION
ULTRA_TOKEN = "niJeDFHRIN9ONCwxGparqUp0degHIpjHu0w3pprXok"
ULTRA_KEY = "DL6mlu7DBRSR8odXWGG5"

# VALIDATOR CONFIGURATION
EMAILABLE_API_KEY = "05FXQPo7bT7K2ZtZ"
VALIDATOR_ENABLED = True     # True = Active, False = Deactivated
VALIDATOR_PROVIDER = "myemailverifier"  # "myemailverifier" or "emailable"

# IN-MEMORY SPEED CACHES
JOINED_CACHE = {}     # {user_id: timestamp_joined}
USER_CACHE = {}       # {user_id: dict_data}

# List of all menu buttons to prevent state bleeding
MENU_BUTTONS = {
    "✍️ Get Task", "💰 Balance", "📨 Sell Gmail", "📜 History", "👥 Referrals", "📁 My Accounts", "⚙️ Settings", "🛠 Support", "🚫 Cancel", "🏠 Main Menu",
    "➕ Add Task", "📋 Tasks", "🟢 Available Tasks", "📥 Pending Reviews", "💸 Pending Withdrawals", "💬 Chat", "🚫 Cancel Sell", "🚫 Cancel Task", "🗑 Unassign Tasks", "🔍 Find ID", "➕ Add Balance", 
    "➖ Cut Balance", "🔎 Check Balance", "🏆 Top Balances", "🚫 Ban User", "✅ Unban User",
    "📢 Broadcast", "⚙️ Change Values", "🗑 Remove Task", "💳 Transactions", "📊 View Stats",
    "📢 Must Join Channel", "🔴 Bot Status: OFF", "🟢 Bot Status: ON", "🟢 Ref Status: ON", "🔴 Ref Status: OFF", "⚙️ Validator", "👑 Transfer Admin",
    "🟢 Ultra Status: ON", "🔴 Ultra Status: OFF", "👷 Manage Workers"
}

# ============================================
# HELPER FOR RANDOM PASSWORD GENERATION
# ============================================

def generate_random_password(length: int = 12) -> str:
    upper = string.ascii_uppercase
    lower = string.ascii_lowercase
    digits = string.digits
    
    password = [
        secrets.choice(upper),
        secrets.choice(lower),
        secrets.choice(digits)
    ]
    
    all_chars = upper + lower + digits
    for _ in range(length - 3):
        password.append(secrets.choice(all_chars))
        
    secrets.SystemRandom().shuffle(password)
    return ''.join(password)

# ============================================
# DYNAMIC GMAIL VALIDATOR ENGINE
# ============================================

def get_provider_url() -> str:
    if VALIDATOR_PROVIDER == "emailable":
        return "https://api.emailable.com/v1/verify?email={email}&api_key={key}"
    return "https://api.myemailverifier.com/api/validate_single.php?apikey={key}&email={email}"

async def is_gmail_registered(email: str, user_id: int = None) -> bool:
    if not VALIDATOR_ENABLED:
        return True

    email = email.strip().lower()
    
    if not email.endswith("@gmail.com"):
        return False

    username = email[:-10]

    if len(username) < 6 or len(username) > 30:
        return False

    if not re.match(r'^[a-z0-9.]+$', username):
        return False

    if username.startswith('.') or username.endswith('.') or '..' in username:
        return False

    verify_msg = None
    if user_id:
        try:
            verify_msg = await bot.send_message(
                user_id,
                "📤<i>Verifying Your Gmail From Official Google</i>🚀",
                parse_mode=ParseMode.HTML
            )
        except Exception:
            pass

    is_valid_email = False

    try:
        if VALIDATOR_PROVIDER == "emailable":
            url = f"https://api.emailable.com/v1/verify?email={urllib.parse.quote(email)}&api_key={urllib.parse.quote(EMAILABLE_API_KEY)}"
        else:
            url = f"https://api.myemailverifier.com/api/validate_single.php?apikey={urllib.parse.quote(EMAILABLE_API_KEY)}&email={urllib.parse.quote(email)}"

        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=12.0)) as resp:
                if resp.status == 200:
                    raw_text = await resp.text()
                    try:
                        data = json.loads(raw_text) if isinstance(raw_text, str) else await resp.json()
                    except Exception:
                        data = await resp.json()

                    if isinstance(data, dict):
                        lower_data = {str(k).lower(): str(v).strip().lower() for k, v in data.items()}

                        if VALIDATOR_PROVIDER == "emailable":
                            state = lower_data.get("state", "")
                            if state == "deliverable":
                                is_valid_email = True
                        else:
                            status_val = lower_data.get("status") or lower_data.get("addressstatus") or lower_data.get("statuscode") or ""
                            diagnosis_val = lower_data.get("diagnosis", "")
                            if status_val in ["valid", "1", "deliverable", "ok", "true"] or "exists" in diagnosis_val or "active" in diagnosis_val:
                                is_valid_email = True
                else:
                    print(f"Validator HTTP Error ({VALIDATOR_PROVIDER}): {resp.status}")
    except Exception as e:
        print(f"Validator Exception ({VALIDATOR_PROVIDER}): {e}")

    if verify_msg:
        try:
            await verify_msg.delete()
        except Exception:
            pass

    return is_valid_email

# ============================================
# DUMMY FLASK SERVER FOR RENDER KEEP-ALIVE
# ============================================

flask_app = Flask('')

@flask_app.route('/')
def home():
    return "Bot is running!"

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    flask_app.run(host='0.0.0.0', port=port)

# ============================================
# STATES
# ============================================

class UserState(StatesGroup):
    selling = State()
    selling_username = State()
    selling_password = State()
    setting_upi = State()
    setting_usdt = State()
    setting_ultra = State()
    submitting_task = State()
    waiting_for_support = State()

class AdminState(StatesGroup):
    waiting_for_task_reject_reason = State()
    waiting_for_sell_reject_reason = State()
    waiting_for_channel_link = State()
    waiting_for_add_balance = State()
    waiting_for_cut_balance = State()
    waiting_for_check_balance = State()
    waiting_for_ban_user = State()
    waiting_for_unban_user = State()
    waiting_for_add_task = State()
    waiting_for_bulk_add_task = State()
    waiting_for_remove_task = State()
    waiting_for_broadcast = State()
    waiting_for_user_transactions = State()
    waiting_for_chat_user_id = State()
    waiting_for_chat_message = State()
    waiting_for_unassign_user_id = State()
    waiting_for_find_id_query = State()
    waiting_for_validator_key = State()
    waiting_for_transfer_admin_id = State()
    waiting_for_support_reply = State()
    waiting_for_change_tasks_rate = State()
    waiting_for_change_sell_rate = State()
    waiting_for_change_min_withdraw = State()
    waiting_for_change_task_pass = State()
    waiting_for_change_fees = State()
    waiting_for_change_ultra_token = State()
    waiting_for_bulk_cancel_sell_reason = State()
    waiting_for_bulk_cancel_task_reason = State()
    waiting_for_cancel_sell_by_id_target = State()
    waiting_for_cancel_sell_by_id_reason = State()
    waiting_for_cancel_task_by_id_target = State()
    waiting_for_cancel_task_by_id_reason = State()

# ============================================
# DATABASE INITIALIZATION & CACHE
# ============================================

async def init_db():
    global db_pool
    url = DATABASE_URL
    if not url:
        raise ValueError("DATABASE_URL environment variable is missing!")
        
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
        
    db_pool = await asyncpg.create_pool(
        dsn=url, 
        ssl='require', 
        min_size=3, 
        max_size=15,
        timeout=10.0,
        command_timeout=10.0,
        statement_cache_size=0
    )
    
    async with db_pool.acquire() as conn:
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY, 
                balance DOUBLE PRECISION DEFAULT 0,
                upi TEXT DEFAULT 'None',
                usdt_address TEXT DEFAULT 'None',
                ultra_number TEXT DEFAULT 'None',
                notifications_enabled BOOLEAN DEFAULT TRUE,
                currency TEXT DEFAULT 'USD',
                referred_by BIGINT DEFAULT NULL,
                referral_earnings DOUBLE PRECISION DEFAULT 0
            )
        ''')
        await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS upi TEXT DEFAULT 'None'")
        await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS usdt_address TEXT DEFAULT 'None'")
        await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS ultra_number TEXT DEFAULT 'None'")
        await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS notifications_enabled BOOLEAN DEFAULT TRUE")
        await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS currency TEXT DEFAULT 'USD'")
        await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS referred_by BIGINT DEFAULT NULL")
        await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS referral_earnings DOUBLE PRECISION DEFAULT 0")

        await conn.execute('''
            CREATE TABLE IF NOT EXISTS banned_users (
                user_id BIGINT PRIMARY KEY
            )
        ''')
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS transactions (
                id SERIAL PRIMARY KEY, 
                user_id BIGINT, 
                type TEXT, 
                amount DOUBLE PRECISION, 
                note TEXT, 
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS withdrawals (
                id SERIAL PRIMARY KEY, 
                user_id BIGINT, 
                amount DOUBLE PRECISION, 
                method TEXT DEFAULT 'UPI',
                payment_address TEXT, 
                status TEXT DEFAULT 'pending', 
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        await conn.execute("ALTER TABLE withdrawals ADD COLUMN IF NOT EXISTS method TEXT DEFAULT 'UPI'")
        await conn.execute("ALTER TABLE withdrawals ADD COLUMN IF NOT EXISTS payment_address TEXT")
        
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS tasks (
                id SERIAL PRIMARY KEY, 
                title TEXT, 
                details TEXT, 
                reward DOUBLE PRECISION, 
                status TEXT DEFAULT 'available',
                added_by BIGINT DEFAULT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        await conn.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP")
        await conn.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS added_by BIGINT DEFAULT NULL")

        await conn.execute('''
            CREATE TABLE IF NOT EXISTS task_assignments (
                task_id INT UNIQUE, 
                user_id BIGINT, 
                assigned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                message_id BIGINT DEFAULT NULL
            )
        ''')
        await conn.execute("ALTER TABLE task_assignments ADD COLUMN IF NOT EXISTS message_id BIGINT DEFAULT NULL")

        await conn.execute('''
            CREATE TABLE IF NOT EXISTS task_history (
                id SERIAL PRIMARY KEY,
                task_id INT,
                user_id BIGINT,
                password_used TEXT,
                assigned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        await conn.execute('''
            CREATE TABLE IF NOT EXISTS pending_sells (
                id SERIAL PRIMARY KEY,
                user_id BIGINT,
                details TEXT,
                amount DOUBLE PRECISION DEFAULT 30.0,
                status TEXT DEFAULT 'pending_review',
                claimed_by BIGINT DEFAULT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        await conn.execute("ALTER TABLE pending_sells ADD COLUMN IF NOT EXISTS claimed_by BIGINT DEFAULT NULL")
        
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS bot_settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        ''')
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS worker_permissions (
                worker_id BIGINT PRIMARY KEY,
                name TEXT DEFAULT 'Worker',
                is_active BOOLEAN DEFAULT TRUE,
                can_sell_gmail BOOLEAN DEFAULT FALSE,
                is_deleted BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        await conn.execute("ALTER TABLE worker_permissions ADD COLUMN IF NOT EXISTS name TEXT DEFAULT 'Worker'")
        await conn.execute("ALTER TABLE worker_permissions ADD COLUMN IF NOT EXISTS is_deleted BOOLEAN DEFAULT FALSE")
        await conn.execute("ALTER TABLE worker_permissions ADD COLUMN IF NOT EXISTS created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP")

async def load_settings_and_cache():
    global BANNED_USERS_CACHE, MUST_JOIN_CHANNEL, BOT_USERNAME, BOT_STATUS, REF_STATUS, ULTRA_STATUS, SINGLE_TASK_STATUS, SELL_GMAIL_STATUS, EMAILABLE_API_KEY, VALIDATOR_ENABLED, VALIDATOR_PROVIDER, ADMIN_ID
    global DEFAULT_TASK_RATE, GMAIL_SELL_RATE, MIN_WITHDRAWAL_AMT, DEFAULT_TASK_PASS, DEFAULT_TASK_PASS_STATUS, UPI_FEES, USDT_FEES, ULTRA_FEES, ULTRA_TOKEN, ULTRA_KEY
    
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT user_id FROM banned_users")
        BANNED_USERS_CACHE = {r['user_id'] for r in rows}
        
        channel_val = await conn.fetchval("SELECT value FROM bot_settings WHERE key='must_join_channel'")
        MUST_JOIN_CHANNEL = channel_val if channel_val else None

        status_val = await conn.fetchval("SELECT value FROM bot_settings WHERE key='bot_status'")
        BOT_STATUS = (status_val != 'off')

        ref_val = await conn.fetchval("SELECT value FROM bot_settings WHERE key='ref_status'")
        REF_STATUS = (ref_val != 'off')

        ultra_stat = await conn.fetchval("SELECT value FROM bot_settings WHERE key='ultra_status'")
        ULTRA_STATUS = (ultra_stat != 'off')

        single_task_val = await conn.fetchval("SELECT value FROM bot_settings WHERE key='single_task_status'")
        SINGLE_TASK_STATUS = (single_task_val != 'off')

        sell_gmail_val = await conn.fetchval("SELECT value FROM bot_settings WHERE key='sell_gmail_status'")
        SELL_GMAIL_STATUS = (sell_gmail_val != 'off')

        task_pass_stat = await conn.fetchval("SELECT value FROM bot_settings WHERE key='default_task_pass_status'")
        DEFAULT_TASK_PASS_STATUS = (task_pass_stat != 'off')

        key_val = await conn.fetchval("SELECT value FROM bot_settings WHERE key='emailable_api_key'")
        if key_val:
            EMAILABLE_API_KEY = key_val

        val_enabled = await conn.fetchval("SELECT value FROM bot_settings WHERE key='validator_enabled'")
        VALIDATOR_ENABLED = (val_enabled != 'off')

        provider_val = await conn.fetchval("SELECT value FROM bot_settings WHERE key='validator_provider'")
        if provider_val:
            VALIDATOR_PROVIDER = provider_val

        admin_val = await conn.fetchval("SELECT value FROM bot_settings WHERE key='admin_id'")
        if admin_val and admin_val.isdigit():
            ADMIN_ID = int(admin_val)

        task_rate_val = await conn.fetchval("SELECT value FROM bot_settings WHERE key='default_task_rate'")
        if task_rate_val:
            DEFAULT_TASK_RATE = float(task_rate_val)

        sell_rate_val = await conn.fetchval("SELECT value FROM bot_settings WHERE key='gmail_sell_rate'")
        if sell_rate_val:
            GMAIL_SELL_RATE = float(sell_rate_val)

        min_w_val = await conn.fetchval("SELECT value FROM bot_settings WHERE key='min_withdrawal_rate'")
        if min_w_val:
            MIN_WITHDRAWAL_AMT = float(min_w_val)

        task_pass_val = await conn.fetchval("SELECT value FROM bot_settings WHERE key='default_task_pass'")
        if task_pass_val:
            DEFAULT_TASK_PASS = task_pass_val

        upi_f = await conn.fetchval("SELECT value FROM bot_settings WHERE key='upi_fees'")
        if upi_f:
            UPI_FEES = float(upi_f)

        usdt_f = await conn.fetchval("SELECT value FROM bot_settings WHERE key='usdt_fees'")
        if usdt_f:
            USDT_FEES = float(usdt_f)

        ultra_f = await conn.fetchval("SELECT value FROM bot_settings WHERE key='ultra_fees'")
        if ultra_f:
            ULTRA_FEES = float(ultra_f)

        u_tok = await conn.fetchval("SELECT value FROM bot_settings WHERE key='ultra_token'")
        if u_tok:
            ULTRA_TOKEN = u_tok

        u_key = await conn.fetchval("SELECT value FROM bot_settings WHERE key='ultra_key'")
        if u_key:
            ULTRA_KEY = u_key

    try:
        me = await bot.get_me()
        if me.username:
            BOT_USERNAME = me.username
    except Exception:
        pass

# ============================================
# HELPERS & KEYBOARDS
# ============================================

def invalidate_user_cache(user_id: int):
    USER_CACHE.pop(user_id, None)

async def ensure_user(user_id: int, referrer_id: int = None, conn=None) -> bool:
    is_new = False
    
    async def _run_ensure(c):
        nonlocal is_new
        result = await c.execute(
            "INSERT INTO users (user_id, balance, upi, usdt_address, ultra_number, notifications_enabled, currency) VALUES ($1, 0, 'None', 'None', 'None', TRUE, 'USD') ON CONFLICT (user_id) DO NOTHING", 
            user_id
        )
        if result == "INSERT 0 1":
            is_new = True

        if referrer_id and referrer_id != user_id:
            ref_exists = await c.fetchval("SELECT user_id FROM users WHERE user_id=$1", referrer_id)
            if ref_exists:
                await c.execute(
                    "UPDATE users SET referred_by = $1 WHERE user_id = $2 AND referred_by IS NULL",
                    referrer_id, user_id
                )

    if conn:
        await _run_ensure(conn)
    else:
        async with db_pool.acquire() as conn:
            await _run_ensure(conn)

    return is_new

async def get_user_data(user_id: int):
    await ensure_user(user_id)
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT balance, upi, usdt_address, ultra_number, notifications_enabled, currency, referred_by, referral_earnings FROM users WHERE user_id=$1", 
            user_id
        )
        return dict(row) if row else None

async def is_banned(user_id: int) -> bool:
    return user_id in BANNED_USERS_CACHE

async def send_user_notification(user_id: int, text: str, **kwargs):
    user_data = await get_user_data(user_id)
    if user_data and user_data.get('notifications_enabled', True):
        try:
            await asyncio.wait_for(bot.send_message(user_id, text, **kwargs), timeout=5.0)
        except Exception:
            pass

def format_currency(amount_in_inr: float, currency_code: str) -> str:
    if currency_code == "USD":
        val = amount_in_inr / USD_TO_INR
        return f"${val:.2f}"
    return f"₹{amount_in_inr:.2f}"

async def check_user_joined_channel(user_id: int) -> bool:
    if not MUST_JOIN_CHANNEL:
        return True
        
    now = time.time()
    if user_id in JOINED_CACHE and (now - JOINED_CACHE[user_id]) < 600:
        return True

    try:
        member = await asyncio.wait_for(
            bot.get_chat_member(chat_id=MUST_JOIN_CHANNEL, user_id=user_id),
            timeout=5.0
        )
        is_joined = member.status in ['creator', 'administrator', 'member']
        if is_joined:
            JOINED_CACHE[user_id] = now
        else:
            JOINED_CACHE.pop(user_id, None)
        return is_joined
    except Exception as e:
        print(f"Error checking channel membership: {e}")
        return True

def get_must_join_keyboard():
    channel_url = f"https://t.me/{MUST_JOIN_CHANNEL.replace('@', '')}" if MUST_JOIN_CHANNEL.startswith("@") else "https://t.me/"
    kb = InlineKeyboardBuilder()
    kb.button(text="📢 Join Channel", url=channel_url)
    kb.button(
        text="Joined / Verify", 
        callback_data="check_must_join",
        icon_custom_emoji_id="6217663806110175239",
        style="success"
    )
    kb.adjust(1, 1)
    return kb.as_markup()

def get_main_menu_keyboard():
    kb = InlineKeyboardBuilder()
    kb.button(
        text="Get Task",
        callback_data="menu_get_task",
        icon_custom_emoji_id="5197269100878907942",
        style="success"
    )
    kb.button(
        text="Balance",
        callback_data="menu_balance",
        icon_custom_emoji_id="5417924076503062111",
        style="primary"
    )
    kb.button(
        text="Sell Gmail",
        callback_data="menu_sell_gmail",
        icon_custom_emoji_id="5377548235709619284",
        style="success"
    )
    kb.button(
        text="History",
        callback_data="menu_history",
        icon_custom_emoji_id="5440410042773824003",
        style="primary"
    )
    kb.button(
        text="Referrals",
        callback_data="menu_referrals",
        icon_custom_emoji_id="5391292736647209211",
        style="success"
    )
    kb.button(
        text="My Accounts",
        callback_data="menu_my_accounts",
        icon_custom_emoji_id="5445221832074483553",
        style="primary"
    )
    kb.button(
        text="Settings",
        callback_data="menu_settings",
        icon_custom_emoji_id="5893161718179173515"
    )
    kb.button(
        text="Support",
        callback_data="menu_support",
        icon_custom_emoji_id="5274099962655816924",
        style="danger"
    )
    kb.adjust(2, 2, 2, 1, 1)
    return kb.as_markup()

def get_add_task_type_keyboard():
    kb = InlineKeyboardBuilder()
    kb.button(
        text="Single Add", 
        callback_data="admin_add_task_single", 
        icon_custom_emoji_id="5870458774455587120", 
        style="success"
    )
    kb.button(
        text="Bulk Add", 
        callback_data="admin_add_task_bulk", 
        icon_custom_emoji_id="5206607081334906820", 
        style="primary"
    )
    kb.adjust(2)
    return kb.as_markup()

def get_referral_inline_keyboard(user_id: int):
    invite_link = f"https://t.me/{BOT_USERNAME}?start={user_id}"
    custom_share_text = "🚀Join Gmail Earnex and Start Earning Money!✅"
    share_url = f"https://t.me/share/url?url={urllib.parse.quote(invite_link)}&text={urllib.parse.quote(custom_share_text)}"
    
    kb = InlineKeyboardBuilder()
    kb.button(
        text="Copy link",
        copy_text=CopyTextButton(text=invite_link),
        icon_custom_emoji_id="5271604874419647061",
        style="primary"
    )
    kb.button(
        text="Share link",
        url=share_url,
        icon_custom_emoji_id="5305265301917549162",
        style="primary"
    )
    kb.button(
        text="Back",
        callback_data="menu_back",
        icon_custom_emoji_id="5352759161945867747"
    )
    kb.adjust(2, 1)
    return kb.as_markup()

def get_settings_keyboard(notif_enabled: bool, currency: str):
    kb = InlineKeyboardBuilder()
    notif_text = "Notifications: ON" if notif_enabled else "Notifications: OFF"
    notif_emoji = "6039486778597970865" if notif_enabled else "6039569594157371705"
    curr_text = f"Currency: {currency} ({'$' if currency=='USD' else '₹'})"
    
    kb.button(text=notif_text, callback_data="toggle_notif", icon_custom_emoji_id=notif_emoji, style="primary")
    kb.button(text=curr_text, callback_data="toggle_currency", icon_custom_emoji_id="5893365462837760511", style="primary")
    kb.button(
        text="Back",
        callback_data="menu_back",
        icon_custom_emoji_id="5352759161945867747"
    )
    kb.adjust(1, 1, 1)
    return kb.as_markup()

def get_admin_menu_keyboard():
    kb = ReplyKeyboardBuilder()
    
    kb.button(text="➕ Add Task", style="success")
    kb.button(text="📋 Tasks", style="primary")
    
    kb.button(text="🟢 Available Tasks", style="primary")
    kb.button(text="📥 Pending Reviews", style="primary")
    
    kb.button(text="💸 Pending Withdrawals", style="primary")
    kb.button(text="💬 Chat", style="primary")
    
    kb.button(text="🗑 Unassign Tasks", style="danger")
    kb.button(text="🚫 Cancel Sell", style="danger")
    
    kb.button(text="🚫 Cancel Task", style="danger")
    kb.button(text="🔍 Find ID", style="primary")
    
    kb.button(text="➕ Add Balance", style="success")
    kb.button(text="➖ Cut Balance", style="danger")
    kb.button(text="🔎 Check Balance", style="primary")
    kb.button(text="🏆 Top Balances", style="primary")
    kb.button(text="🚫 Ban User", style="danger")
    kb.button(text="✅ Unban User", style="success")
    kb.button(text="📢 Broadcast", style="primary")
    kb.button(text="⚙️ Change Values", style="primary")
    kb.button(text="🗑 Remove Task", style="danger")
    kb.button(text="💳 Transactions", style="primary")
    kb.button(text="📊 View Stats", style="primary")
    kb.button(text="📢 Must Join Channel", style="primary")
    
    status_btn_text = "🟢 Bot Status: ON" if BOT_STATUS else "🔴 Bot Status: OFF"
    kb.button(text=status_btn_text, style="danger" if BOT_STATUS else "success")
    
    ref_btn_text = "🟢 Ref Status: ON" if REF_STATUS else "🔴 Ref Status: OFF"
    kb.button(text=ref_btn_text, style="success" if REF_STATUS else "danger")
    
    kb.button(text="⚙️ Validator", style="primary")
    ultra_btn_text = "🟢 Ultra Status: ON" if ULTRA_STATUS else "🔴 Ultra Status: OFF"
    kb.button(text=ultra_btn_text, style="success" if ULTRA_STATUS else "danger")

    kb.button(text="👷 Manage Workers", style="primary")
    kb.button(text="👑 Transfer Admin", style="danger")

    kb.button(text="🏠 Main Menu", style="primary")
    
    kb.adjust(2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 1)
    return kb.as_markup(resize_keyboard=True)

def get_cancel_sell_options_keyboard():
    kb = InlineKeyboardBuilder()
    kb.button(text="Cancel All", callback_data="admin_cancel_sell_all", style="danger")
    kb.button(text="Cancel By Id", callback_data="admin_cancel_sell_by_id", style="primary")
    kb.adjust(2)
    return kb.as_markup()

def get_cancel_task_options_keyboard():
    kb = InlineKeyboardBuilder()
    kb.button(text="Cancel All", callback_data="admin_cancel_task_all", style="danger")
    kb.button(text="Cancel By Id", callback_data="admin_cancel_task_by_id", style="primary")
    kb.adjust(2)
    return kb.as_markup()

def get_pending_reviews_inline_keyboard():
    kb = InlineKeyboardBuilder()
    kb.button(
        text="📨 Sell Gmail",
        callback_data="admin_view_pending_sells",
        icon_custom_emoji_id="5377548235709619284",
        style="primary"
    )
    kb.button(
        text="✍️ Task Gmail",
        callback_data="admin_view_pending_tasks",
        icon_custom_emoji_id="5197269100878907942",
        style="primary"
    )
    kb.adjust(2)
    return kb.as_markup()

def get_pending_withdrawals_inline_keyboard():
    kb = InlineKeyboardBuilder()
    kb.button(
        text="🏦 UPI",
        callback_data="admin_view_pending_withdraw_upi",
        icon_custom_emoji_id="6291696801636424911",
        style="primary"
    )
    kb.button(
        text="🪙 USDT BEP-20",
        callback_data="admin_view_pending_withdraw_usdt",
        icon_custom_emoji_id="5197434882321567830",
        style="primary"
    )
    if ULTRA_STATUS:
        kb.button(
            text="⚡️ Ultra Gateway",
            callback_data="admin_view_pending_withdraw_ultra",
            icon_custom_emoji_id="5195033767969839232",
            style="primary"
        )
        kb.adjust(3)
    else:
        kb.adjust(2)
    return kb.as_markup()

def get_change_values_inline_keyboard():
    kb = InlineKeyboardBuilder()
    kb.button(
        text="1. Change Tasks Rate",
        callback_data="admin_change_tasks_rate",
        icon_custom_emoji_id="5417924076503062111",
        style="primary"
    )
    kb.button(
        text="2. Change Sell Rate",
        callback_data="admin_change_sell_rate",
        icon_custom_emoji_id="5377548235709619284",
        style="primary"
    )
    kb.button(
        text="3. Change Min. Withdrawal",
        callback_data="admin_change_min_withdraw",
        icon_custom_emoji_id="5444856076954520455",
        style="primary"
    )
    kb.button(
        text="4. Change Task Password",
        callback_data="admin_change_task_pass",
        icon_custom_emoji_id="6005570495603282482",
        style="primary"
    )
    
    pass_mode_text = f"4b. Password Mode: {'🟢 Fixed (Default)' if DEFAULT_TASK_PASS_STATUS else '🔴 Random'}"
    kb.button(
        text=pass_mode_text,
        callback_data="admin_toggle_task_pass_mode",
        icon_custom_emoji_id="6005570495603282482",
        style="success" if DEFAULT_TASK_PASS_STATUS else "danger"
    )

    kb.button(
        text="5. Change Fees",
        callback_data="admin_change_fees",
        icon_custom_emoji_id="5417924076503062111",
        style="primary"
    )
    kb.button(
        text="6. Change Ultra",
        callback_data="admin_change_ultra",
        icon_custom_emoji_id="6005570495603282482",
        style="primary"
    )
    
    single_task_btn_text = f"7. Single Tasks: {'🟢 ON' if SINGLE_TASK_STATUS else '🔴 OFF'}"
    kb.button(
        text=single_task_btn_text,
        callback_data="admin_toggle_single_task",
        icon_custom_emoji_id="5197269100878907942",
        style="success" if SINGLE_TASK_STATUS else "danger"
    )

    sell_gmail_btn_text = f"8. Sell Gmail: {'🟢 ON' if SELL_GMAIL_STATUS else '🔴 OFF'}"
    kb.button(
        text=sell_gmail_btn_text,
        callback_data="admin_toggle_sell_gmail",
        icon_custom_emoji_id="5377548235709619284",
        style="success" if SELL_GMAIL_STATUS else "danger"
    )

    kb.adjust(1, 1, 1, 1, 1, 1, 1, 1, 1)
    return kb.as_markup()

def get_validator_admin_inline_keyboard():
    kb = InlineKeyboardBuilder()
    status_toggle_text = "🔴 Deactivate" if VALIDATOR_ENABLED else "🟢 Activate"
    status_style = "danger" if VALIDATOR_ENABLED else "success"

    kb.button(
        text="🔑 Change Key", 
        callback_data="admin_validator_change_key", 
        icon_custom_emoji_id="6005570495603282482", 
        style="primary"
    )
    kb.button(
        text="🔄 Change Provider", 
        callback_data="admin_validator_change_provider", 
        icon_custom_emoji_id="5893365462837760511", 
        style="primary"
    )
    kb.button(
        text=status_toggle_text, 
        callback_data="admin_validator_toggle_status", 
        icon_custom_emoji_id="6217663806110175239", 
        style=status_style
    )
    kb.adjust(2, 1)
    return kb.as_markup()

def get_unassign_inline_keyboard():
    kb = InlineKeyboardBuilder()
    kb.button(
        text="👤 User ID", 
        callback_data="unassign_by_user_id", 
        icon_custom_emoji_id="5870458774455587120",
        style="primary"
    )
    kb.button(
        text="👥 All Users", 
        callback_data="unassign_all_users", 
        icon_custom_emoji_id="5274099962655816924",
        style="danger"
    )
    kb.adjust(2)
    return kb.as_markup()

def get_balance_inline_keyboard(upi_set: bool, usdt_set: bool, ultra_set: bool = False):
    kb = InlineKeyboardBuilder()
    upi_link_text = "Change UPI" if upi_set else "Link UPI"
    usdt_link_text = "Change USDT" if usdt_set else "Link USDT BEP-20"
    
    upi_emoji = "6291696801636424911" if upi_set else "5902449142575141204"
    usdt_emoji = "5197434882321567830" if usdt_set else "5902449142575141204"

    kb.button(text=upi_link_text, callback_data="link_upi", icon_custom_emoji_id=upi_emoji, style="primary")
    kb.button(text=usdt_link_text, callback_data="link_usdt", icon_custom_emoji_id=usdt_emoji, style="primary")
    
    if ULTRA_STATUS:
        ultra_link_text = "Change Ultra" if ultra_set else "Link Ultra Gateway"
        ultra_emoji = "5195033767969839232" if ultra_set else "5902449142575141204"
        kb.button(text=ultra_link_text, callback_data="link_ultra", icon_custom_emoji_id=ultra_emoji, style="primary")

    kb.button(
        text="Withdraw", 
        callback_data="choose_withdraw_method", 
        icon_custom_emoji_id="5444856076954520455",
        style="success"
    )
    kb.button(
        text="Back",
        callback_data="menu_back",
        icon_custom_emoji_id="5352759161945867747"
    )
    if ULTRA_STATUS:
        kb.adjust(2, 1, 1, 1)
    else:
        kb.adjust(2, 1, 1)
    return kb.as_markup()

def get_withdraw_options_keyboard():
    kb = InlineKeyboardBuilder()
    kb.button(text=f"Withdraw via UPI (Fee: ₹{UPI_FEES:.2f})", callback_data="withdraw_upi", icon_custom_emoji_id="6291696801636424911", style="success")
    kb.button(text=f"Withdraw via USDT BEP-20 (Fee: ₹{USDT_FEES:.2f})", callback_data="withdraw_usdt", icon_custom_emoji_id="5197434882321567830", style="success")
    if ULTRA_STATUS:
        kb.button(text=f"Withdraw via Ultra Gateway (0 Fees)", callback_data="withdraw_ultra", icon_custom_emoji_id="5195033767969839232", style="success")
    kb.button(
        text="Back",
        callback_data="menu_balance",
        icon_custom_emoji_id="5352759161945867747"
    )
    if ULTRA_STATUS:
        kb.adjust(1, 1, 1, 1)
    else:
        kb.adjust(1, 1, 1)
    return kb.as_markup()

def get_back_inline_keyboard():
    kb = InlineKeyboardBuilder()
    kb.button(
        text="Back",
        callback_data="menu_back",
        icon_custom_emoji_id="5352759161945867747"
    )
    kb.adjust(1)
    return kb.as_markup()

def get_task_action_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="Submit", 
            callback_data="user_submit_task", 
            icon_custom_emoji_id="5206607081334906820",
            style="success"
        ),
        InlineKeyboardButton(
            text="Cancel", 
            callback_data="user_cancel_task", 
            icon_custom_emoji_id="5274099962655816924",
            style="danger"
        )
    ]])

def get_support_cancel_keyboard():
    kb = InlineKeyboardBuilder()
    kb.button(
        text="Back", 
        callback_data="menu_back", 
        icon_custom_emoji_id="5352759161945867747"
    )
    kb.adjust(1)
    return kb.as_markup()

async def edit_admin_message(call: CallbackQuery, additional_text: str):
    try:
        if call.message.photo:
            new_caption = (call.message.caption or "") + "\n\n" + additional_text
            await call.message.edit_caption(caption=new_caption, reply_markup=None, parse_mode=ParseMode.HTML)
        else:
            new_text = (call.message.text or "") + "\n\n" + additional_text
            await call.message.edit_text(text=new_text, reply_markup=None, parse_mode=ParseMode.HTML)
    except Exception as e:
        print(f"Error editing admin message: {e}")

# ============================================
# PAGINATED ALL TASKS DASHBOARD FOR ADMIN
# ============================================

async def render_admin_all_tasks_page(page: int = 1):
    items_per_page = 10

    async with db_pool.acquire() as conn:
        tasks_rows = await conn.fetch('''
            SELECT t.id, t.title, t.details, t.reward, t.status, ta.user_id 
            FROM tasks t
            LEFT JOIN task_assignments ta ON t.id = ta.task_id
            ORDER BY t.id DESC
        ''')

    total_items = len(tasks_rows)
    total_pages = max(1, (total_items + items_per_page - 1) // items_per_page)

    if page < 1:
        page = 1
    elif page > total_pages:
        page = total_pages

    start_idx = (page - 1) * items_per_page
    end_idx = min(start_idx + items_per_page, total_items)

    page_items = tasks_rows[start_idx:end_idx]

    if total_items == 0:
        text = "📋 <b>All System Tasks</b>\n\n📭 No tasks found in the database."
    else:
        text = (
            f"📋 <b>All System Tasks</b>\n"
            f"Showing <b>{start_idx + 1}-{end_idx}</b> of <b>{total_items}</b> total task(s).\n\n"
        )

        for t in page_items:
            task_id = t['id']
            status_raw = t['status']
            user_id = t['user_id']
            
            try:
                email = t['details'].split(" | ")[0].replace("Email: ", "").strip()
            except Exception:
                email = t['title'].replace("Login to ", "").strip()

            if status_raw == 'available':
                status_str = "🟢 Available"
            elif status_raw == 'assigned':
                status_str = "🔵 Assigned"
            elif status_raw == 'pending_review':
                status_str = "🟡 Under Review"
            elif status_raw == 'completed':
                status_str = "✅ Completed"
            else:
                status_str = f"⚪️ {status_raw.capitalize()}"

            user_info_str = "Not Assigned"
            if user_id:
                try:
                    chat_member = await bot.get_chat(user_id)
                    username = f"@{chat_member.username}" if chat_member.username else f"ID: {user_id}"
                except Exception:
                    username = f"ID: {user_id}"
                user_info_str = f"{username} (<code>{user_id}</code>)"

            text += (
                f"🆔 <b>Task #{task_id}</b> | {status_str}\n"
                f"📧 <b>Gmail:</b> <code>{email}</code>\n"
                f"👤 <b>User Info:</b> {user_info_str}\n"
                f"━━━━━━━━━━━━━━━━━━\n"
            )

    kb = InlineKeyboardBuilder()

    if total_pages > 1:
        nav_buttons = []
        if page > 1:
            nav_buttons.append(InlineKeyboardButton(text="<- Prev", callback_data=f"adm_all_tasks_page:{page - 1}"))
        
        nav_buttons.append(InlineKeyboardButton(text=f"{page}/{total_pages}", callback_data="noop"))
        
        if page < total_pages:
            nav_buttons.append(InlineKeyboardButton(text="Next ->", callback_data=f"adm_all_tasks_page:{page + 1}"))
        
        kb.row(*nav_buttons)

    return text, kb.as_markup()

# ============================================
# PAGINATED AVAILABLE TASKS DASHBOARD
# ============================================

async def render_admin_tasks_page(page: int = 1):
    items_per_page = 10

    async with db_pool.acquire() as conn:
        tasks_rows = await conn.fetch('''
            SELECT t.id, t.title, t.details, t.reward, t.status, ta.user_id 
            FROM tasks t
            LEFT JOIN task_assignments ta ON t.id = ta.task_id
            WHERE t.status IN ('available', 'assigned')
            ORDER BY t.id DESC
        ''')

    total_items = len(tasks_rows)
    total_pages = max(1, (total_items + items_per_page - 1) // items_per_page)

    if page < 1:
        page = 1
    elif page > total_pages:
        page = total_pages

    start_idx = (page - 1) * items_per_page
    end_idx = min(start_idx + items_per_page, total_items)

    page_items = tasks_rows[start_idx:end_idx]

    if total_items == 0:
        text = "🟢 <b>Available & Assigned Tasks</b>\n\n📭 No available or assigned tasks found in the database."
    else:
        text = (
            f"🟢 <b>Available & Assigned Tasks</b>\n"
            f"Showing <b>{start_idx + 1}-{end_idx}</b> of <b>{total_items}</b> active task(s).\n\n"
        )

        for t in page_items:
            task_id = t['id']
            status_raw = t['status']
            user_id = t['user_id']
            
            try:
                email = t['details'].split(" | ")[0].replace("Email: ", "").strip()
            except Exception:
                email = t['title'].replace("Login to ", "").strip()

            if status_raw == 'available':
                status_str = "🟢 Available"
            elif status_raw == 'assigned':
                status_str = "🔵 Assigned"
            else:
                status_str = f"⚪️ {status_raw.capitalize()}"

            user_info_str = "Not Assigned"
            if user_id:
                try:
                    chat_member = await bot.get_chat(user_id)
                    username = f"@{chat_member.username}" if chat_member.username else f"ID: {user_id}"
                except Exception:
                    username = f"ID: {user_id}"
                user_info_str = f"{username} (<code>{user_id}</code>)"

            text += (
                f"🆔 <b>Task #{task_id}</b> | 📌 <b>Type:</b> {status_str}\n"
                f"📧 <b>Gmail:</b> <code>{email}</code>\n"
                f"👤 <b>User Info:</b> {user_info_str}\n"
                f"━━━━━━━━━━━━━━━━━━\n"
            )

    kb = InlineKeyboardBuilder()

    if total_pages > 1:
        nav_buttons = []
        if page > 1:
            nav_buttons.append(InlineKeyboardButton(text="<- Prev", callback_data=f"adm_tasks_page:{page - 1}"))
        
        nav_buttons.append(InlineKeyboardButton(text=f"{page}/{total_pages}", callback_data="noop"))
        
        if page < total_pages:
            nav_buttons.append(InlineKeyboardButton(text="Next ->", callback_data=f"adm_tasks_page:{page + 1}"))
        
        kb.row(*nav_buttons)

    return text, kb.as_markup()

# ============================================
# PAGINATED TRANSACTION HISTORY RENDERER
# ============================================

async def render_transaction_history_page(target_user_id: int, page: int = 1, is_admin: bool = False):
    items_per_page = 10

    user_data = await get_user_data(target_user_id)
    curr = user_data['currency'] if user_data else "USD"

    async with db_pool.acquire() as conn:
        tx_rows = await conn.fetch('''
            SELECT type, amount, note, created_at 
            FROM transactions 
            WHERE user_id=$1 
            ORDER BY id DESC
        ''', target_user_id)

    total_items = len(tx_rows)
    total_pages = max(1, (total_items + items_per_page - 1) // items_per_page)

    if page < 1:
        page = 1
    elif page > total_pages:
        page = total_pages

    start_idx = (page - 1) * items_per_page
    end_idx = min(start_idx + items_per_page, total_items)

    page_items = tx_rows[start_idx:end_idx]

    header_title = f"💳 <b>Transaction History (User <code>{target_user_id}</code>)</b>" if is_admin else '<tg-emoji emoji-id="5440410042773824003">📜</tg-emoji> <b>Transaction History</b>'

    if total_items == 0:
        text = f"{header_title}\n\n📭 No transaction records found."
    else:
        text = (
            f"{header_title}\n"
            f"Showing <b>{start_idx + 1}-{end_idx}</b> of <b>{total_items}</b> transaction(s).\n\n"
        )

        for tx in page_items:
            amt = tx['amount']
            sign = "+" if amt >= 0 else "-"
            formatted_amt = format_currency(abs(amt), curr)
            
            raw_type = (tx['type'] or 'general').lower()
            if raw_type in ['withdrawal', 'withdrawal_paid']:
                tx_type = "WITHDRAWAL"
            elif raw_type == 'withdrawal_pending':
                tx_type = "WITHDRAWAL_PENDING"
            else:
                tx_type = raw_type.upper()

            date_fmt = tx['created_at'].strftime("%b %d, %Y %I:%M %p")
            note_str = f"\n📝 <i>{tx['note']}</i>" if tx['note'] else ""

            type_emoji = "🟢" if amt >= 0 else "🔴"
            text += (
                f"{type_emoji} <b>{sign}{formatted_amt}</b> | <code>{tx_type}</code>\n"
                f"📅 {date_fmt}{note_str}\n"
                f"━━━━━━━━━━━━━━━━━━\n"
            )

    kb = InlineKeyboardBuilder()

    prefix = f"adm_tx_page:{target_user_id}" if is_admin else "user_tx_page"

    if total_pages > 1:
        nav_buttons = []
        if page > 1:
            nav_buttons.append(InlineKeyboardButton(text="<- Prev", callback_data=f"{prefix}:{page - 1}"))
        
        nav_buttons.append(InlineKeyboardButton(text=f"{page}/{total_pages}", callback_data="noop"))
        
        if page < total_pages:
            nav_buttons.append(InlineKeyboardButton(text="Next ->", callback_data=f"{prefix}:{page + 1}"))
        
        kb.row(*nav_buttons)

    if not is_admin:
        kb.row(InlineKeyboardButton(text="⬅️ Back", callback_data="menu_back", icon_custom_emoji_id="5352759161945867747"))

    return text, kb.as_markup()

# ============================================
# GLOBAL BAN, BOT STATUS & MUST-JOIN MIDDLEWARES
# ============================================

@dp.message.outer_middleware()
async def global_message_middleware(handler, event: Message, data):
    if not event.from_user:
        return await handler(event, data)

    user_id = event.from_user.id

    if user_id == ADMIN_ID:
        return await handler(event, data)
        
    if not BOT_STATUS:
        await event.answer("⚠️ Bot is Currently Off, Wait For Admin To On The Bot")
        return

    if await is_banned(user_id):
        await event.answer("🚫 You are banned from using this bot.")
        return

    if MUST_JOIN_CHANNEL and not await check_user_joined_channel(user_id):
        await event.answer(
            f'<tg-emoji emoji-id="5274099962655816924">❗️</tg-emoji> <b>You must join our main channel to use this bot!</b>\n\n'
            f'Please join the channel below and click verify.',
            parse_mode=ParseMode.HTML,
            reply_markup=get_must_join_keyboard()
        )
        return

    return await handler(event, data)

@dp.callback_query.outer_middleware()
async def global_callback_middleware(handler, event: CallbackQuery, data):
    if not event.from_user:
        return await handler(event, data)

    user_id = event.from_user.id

    if user_id == ADMIN_ID:
        return await handler(event, data)
        
    if not BOT_STATUS:
        try:
            await event.answer("⚠️ Bot is Currently Off, Wait For Admin To On The Bot", show_alert=True)
        except Exception:
            pass
        return

    if await is_banned(user_id):
        try:
            await event.answer("🚫 You are banned from using this bot.", show_alert=True)
        except Exception:
            pass
        return

    if event.data == "check_must_join":
        return await handler(event, data)

    if MUST_JOIN_CHANNEL and not await check_user_joined_channel(user_id):
        try:
            await event.answer("⚠️ You must join our channel first to use the bot!", show_alert=True)
        except Exception:
            pass
        return

    return await handler(event, data)

@dp.callback_query(F.data == "check_must_join")
async def verify_must_join_callback(call: CallbackQuery):
    await call.answer()
    user_id = call.from_user.id
    if await check_user_joined_channel(user_id):
        try:
            await call.message.delete()
        except:
            pass
        await call.message.answer(
            f'<tg-emoji emoji-id="6217663806110175239">✅</tg-emoji> <b>Verification successful! You can now use the bot.</b>',
            parse_mode=ParseMode.HTML,
            reply_markup=get_main_menu_keyboard()
        )
    else:
        try:
            await call.answer("❌ You haven't joined the channel yet! Please join and try again.", show_alert=True)
        except Exception:
            pass

@dp.chat_member(ChatMemberUpdatedFilter(IS_MEMBER >> IS_NOT_MEMBER))
async def user_left_channel(event: ChatMemberUpdated):
    user_id = event.from_user.id
    JOINED_CACHE.pop(user_id, None)
    try:
        await bot.send_message(
            user_id,
            '<tg-emoji emoji-id="5274099962655816924">❗️</tg-emoji> <b>You left our official channel!</b>\n\nAccess to the bot has been paused. Rejoin the channel to use the bot again.',
            parse_mode=ParseMode.HTML,
            reply_markup=get_must_join_keyboard()
        )
    except Exception:
        pass

# ============================================
# START & GLOBAL CANCEL
# ============================================

@dp.message(CommandStart())
async def start(message: Message, state: FSMContext, command: CommandObject = None):
    try:
        data = await state.get_data()
        last_msg_id = data.get("last_menu_msg_id")
        if last_msg_id:
            try:
                await bot.delete_message(chat_id=message.chat.id, message_id=last_msg_id)
            except Exception:
                pass

        await state.clear()
        
        referrer_id = None
        if command and command.args and command.args.isdigit():
            referrer_id = int(command.args)

        is_new_user = await ensure_user(message.from_user.id, referrer_id)
        
        if is_new_user:
            text = (
                '<tg-emoji emoji-id="5458904472598095631">👋</tg-emoji> <b>Welcome to Gmail Earnex!</b>\n\n'
                '💵 <b>Default Currency Selected:</b> <code>USD ($)</code>\n'
                '⚙️ <i>You can change your currency anytime in <b>Settings</b>.</i>\n\n'
                'Choose an option from the menu below:'
            )
        else:
            text = (
                '<tg-emoji emoji-id="5458904472598095631">👋</tg-emoji> <b>Welcome back.</b>\n\n'
                'Choose an option from the menu below:'
            )
        
        sent_msg = await message.answer(text, parse_mode=ParseMode.HTML, reply_markup=get_main_menu_keyboard())
        await state.update_data(last_menu_msg_id=sent_msg.message_id)
    except Exception as e:
        print(f"Error in start command: {e}")

@dp.message(Command("cancel"), StateFilter("*"))
@dp.message(F.text == "🚫 Cancel", StateFilter("*"))
async def cancel(message: Message, state: FSMContext):
    await state.clear()
    sent_msg = await message.answer('<tg-emoji emoji-id="5274099962655816924">❗️</tg-emoji> Current operation cancelled.', reply_markup=get_main_menu_keyboard(), parse_mode=ParseMode.HTML)
    await state.update_data(last_menu_msg_id=sent_msg.message_id)

@dp.message(F.text == "🏠 Main Menu", StateFilter("*"))
async def return_to_main_menu(message: Message, state: FSMContext):
    await state.clear()
    sent_msg = await message.answer("🏠 Returned to Main Menu.", reply_markup=get_main_menu_keyboard())
    await state.update_data(last_menu_msg_id=sent_msg.message_id)

# ============================================
# INLINE MAIN MENU CALLBACK HANDLERS
# ============================================

@dp.callback_query(F.data == "menu_back")
async def cb_menu_back(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.clear()
    text = (
        '<tg-emoji emoji-id="5458904472598095631">👋</tg-emoji> <b>Welcome back.</b>\n\n'
        'Choose an option from the menu below:'
    )
    try:
        await call.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=get_main_menu_keyboard())
    except:
        sent_msg = await call.message.answer(text, parse_mode=ParseMode.HTML, reply_markup=get_main_menu_keyboard())
        await state.update_data(last_menu_msg_id=sent_msg.message_id)
    else:
        await state.update_data(last_menu_msg_id=call.message.message_id)

@dp.callback_query(F.data == "menu_referrals")
async def cb_referrals(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.clear()
    user_id = call.from_user.id
    user_data = await get_user_data(user_id)
    curr = user_data['currency'] if user_data else "USD"

    async with db_pool.acquire() as conn:
        invited_users_count = await conn.fetchval(
            "SELECT COUNT(*) FROM users WHERE referred_by=$1", user_id
        ) or 0
        
        approved_ref_accounts = await conn.fetchval('''
            SELECT COUNT(*) FROM transactions 
            WHERE user_id = $1 AND type = 'referral'
        ''', user_id) or 0

        total_earnings = user_data['referral_earnings'] if user_data else 0.0

    formatted_earnings = format_currency(total_earnings, curr)
    invite_link = f"https://t.me/{BOT_USERNAME}?start={user_id}"

    rate_sell = format_currency(REFERRAL_SELL_BONUS, curr)
    rate_task = format_currency(REFERRAL_TASK_BONUS, curr)

    text = (
        f'<tg-emoji emoji-id="6183862417785626642">👥</tg-emoji> <b>My Referrals</b>\n'
        f'━━━━━━━━━━━━━━━━━━\n'
        f'<b>Total earnings:</b> {formatted_earnings}\n'
        f'<b>Invited users:</b> {invited_users_count}\n'
        f'<b>Approved referral accounts:</b> {approved_ref_accounts}\n'
        f'━━━━━━━━━━━━━━━━━━\n'
        f'<tg-emoji emoji-id="5417831807720642261">ℹ️</tg-emoji> <b>How it works</b>\n'
        f'Share your invite link. Every time someone you invited gets a Gmail account accepted, you earn a cash referral reward — for a lifetime. No limit, it never expires.\n\n'
        f'<tg-emoji emoji-id="5278467510604160626">💵</tg-emoji> <b>Referral Rewards</b>\n'
        f'Sell Gmail accepted account: {rate_sell}\n'
        f'Task Gmail accepted account: {rate_task}\n'
        f'Paid on every accepted account from your referrals — for life.\n\n'
        f'<tg-emoji emoji-id="5337080053119336309">🔗</tg-emoji> <b>Your invite link:</b>\n'
        f'<code>{invite_link}</code>'
    )

    try:
        await call.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=get_referral_inline_keyboard(user_id))
    except:
        await call.message.answer(text, parse_mode=ParseMode.HTML, reply_markup=get_referral_inline_keyboard(user_id))
    await state.update_data(last_menu_msg_id=call.message.message_id)

# ============================================
# MY ACCOUNTS PAGINATED SYSTEM
# ============================================

async def render_my_accounts_page(user_id: int, page: int = 1):
    items_per_page = 5

    async with db_pool.acquire() as conn:
        sells = await conn.fetch('''
            SELECT id, details, status, created_at 
            FROM pending_sells 
            WHERE user_id=$1 
        ''', user_id)

        tasks = await conn.fetch('''
            SELECT t.id, t.details, t.status, ta.assigned_at as created_at 
            FROM task_assignments ta
            JOIN tasks t ON ta.task_id = t.id
            WHERE ta.user_id=$1
        ''', user_id)

        completed_tasks = await conn.fetch('''
            SELECT tr.id, tr.note as details, 'completed' as status, tr.created_at 
            FROM transactions tr 
            WHERE tr.user_id=$1 AND tr.type='task'
        ''', user_id)

    all_accounts = []

    for s in sells:
        sell_id = s['id']
        try:
            email = s['details'].split("\n")[0].replace("Username: ", "").strip()
        except Exception:
            email = s['details'].strip()
        
        if "@gmail.com" not in email.lower() and "@" not in email:
            email += "@gmail.com"

        display_title = f"{email} #{sell_id}"
        
        status_raw = s['status']
        if status_raw == 'pending_review':
            status_str = "🟡 Waiting For Review"
        elif status_raw == 'approved':
            status_str = "🟢 Approved & Paid"
        else:
            status_str = "🔴 Declined / Rejected"

        all_accounts.append({
            'title': display_title,
            'type': 'Sell',
            'status': status_str,
            'date': s['created_at']
        })

    for t in tasks:
        task_id = t['id']
        try:
            parts = t['details'].split(" | ")
            email = parts[0].replace("Email: ", "").strip()
        except Exception:
            email = f"Task"

        if "@gmail.com" not in email.lower() and "@" not in email:
            email += "@gmail.com"

        display_title = f"{email} #{task_id}"

        status_raw = t['status']
        if status_raw == 'pending_review':
            status_str = "🟡 Waiting For Review"
        elif status_raw == 'assigned':
            status_str = "🔵 In Progress"
        elif status_raw == 'completed':
            status_str = "🟢 Approved & Paid"
        else:
            status_str = "🔴 Declined / Rejected"

        all_accounts.append({
            'title': display_title,
            'type': 'Register',
            'status': status_str,
            'date': t['created_at']
        })

    for ct in completed_tasks:
        note_text = ct['details'] or ""
        task_id_str = ""
        if "#" in note_text:
            task_id_str = f" #{note_text.split('#')[-1]}"
            
        email_str = note_text.replace("Task #", "").split()[0] if "Task #" in note_text else "Task Account"
        if "@gmail.com" not in email_str.lower() and "@" not in email_str and email_str.isalnum():
            email_str += "@gmail.com"

        all_accounts.append({
            'title': f"{email_str}{task_id_str}",
            'type': 'Register',
            'status': "🟢 Approved & Paid",
            'date': ct['created_at']
        })

    all_accounts.sort(key=lambda x: x['date'], reverse=True)

    total_items = len(all_accounts)
    total_pages = max(1, (total_items + items_per_page - 1) // items_per_page)

    if page < 1:
        page = 1
    elif page > total_pages:
        page = total_pages

    start_idx = (page - 1) * items_per_page
    end_idx = min(start_idx + items_per_page, total_items)

    page_items = all_accounts[start_idx:end_idx]

    if total_items == 0:
        text = (
            '<tg-emoji emoji-id="5445221832074483553">🏷️</tg-emoji> <b>My Accounts</b>\n\n'
            "📭 You haven't submitted any Gmail accounts yet."
        )
    else:
        text = (
            f'<tg-emoji emoji-id="5445221832074483553">🏷️</tg-emoji> <b>My Accounts</b>\n'
            f'You have <b>{total_items}</b> submitted Gmail accounts.\n'
            f'Showing <b>{start_idx + 1}-{end_idx}</b> of <b>{total_items}</b>.\n\n'
        )

        for item in page_items:
            date_fmt = item['date'].strftime("%b %d %I:%M %p")
            text += (
                f"<code>{item['title']}</code>\n"
                f"📌 <b>Type:</b> {item['type']}\n"
                f"{item['status']}\n"
                f"Created: {date_fmt}\n\n"
            )

    kb = InlineKeyboardBuilder()

    if total_pages > 1:
        nav_buttons = []
        if page > 1:
            nav_buttons.append(InlineKeyboardButton(text="<- Prev", callback_data=f"myacc_page:{page - 1}"))
        
        nav_buttons.append(InlineKeyboardButton(text=f"{page}/{total_pages}", callback_data="noop"))
        
        if page < total_pages:
            nav_buttons.append(InlineKeyboardButton(text="Next ->", callback_data=f"myacc_page:{page + 1}"))
        
        kb.row(*nav_buttons)

    kb.row(InlineKeyboardButton(text="⬅️ Back", callback_data="menu_back", icon_custom_emoji_id="5352759161945867747"))

    return text, kb.as_markup()

@dp.callback_query(F.data == "menu_my_accounts")
async def cb_my_accounts(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.clear()
    text, reply_markup = await render_my_accounts_page(call.from_user.id, page=1)
    try:
        await call.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
    except Exception:
        await call.message.answer(text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
    await state.update_data(last_menu_msg_id=call.message.message_id)

@dp.callback_query(F.data.startswith("myacc_page:"))
async def cb_my_accounts_page(call: CallbackQuery):
    await call.answer()
    page = int(call.data.split(":")[1])
    text, reply_markup = await render_my_accounts_page(call.from_user.id, page=page)
    try:
        await call.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
    except Exception:
        pass

@dp.callback_query(F.data == "noop")
async def cb_noop(call: CallbackQuery):
    await call.answer()

@dp.callback_query(F.data == "menu_settings")
async def cb_settings(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.clear()
    user_data = await get_user_data(call.from_user.id)
    notif = user_data['notifications_enabled']
    curr = user_data['currency']
    
    text = (
        '<tg-emoji emoji-id="5893161718179173515">⚙️</tg-emoji> <b>Settings</b>\n\n'
        '<tg-emoji emoji-id="5902002809573740949">⚙️</tg-emoji> Customize your preferences using the options below:'
    )
    try:
        await call.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=get_settings_keyboard(notif, curr))
    except:
        await call.message.answer(text, parse_mode=ParseMode.HTML, reply_markup=get_settings_keyboard(notif, curr))
    await state.update_data(last_menu_msg_id=call.message.message_id)

@dp.callback_query(F.data == "toggle_notif")
async def cb_toggle_notif(call: CallbackQuery):
    user_data = await get_user_data(call.from_user.id)
    current_notif = user_data['notifications_enabled']
    new_notif = not current_notif
    
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE users SET notifications_enabled=$1 WHERE user_id=$2", new_notif, call.from_user.id)
        
    invalidate_user_cache(call.from_user.id)
    status_str = "ENABLED" if new_notif else "DISABLED"
    await call.answer(f"Notifications are now {status_str}", show_alert=True)
    
    try:
        await call.message.edit_reply_markup(reply_markup=get_settings_keyboard(new_notif, user_data['currency']))
    except:
        pass

@dp.callback_query(F.data == "toggle_currency")
async def cb_toggle_currency(call: CallbackQuery):
    user_data = await get_user_data(call.from_user.id)
    current_curr = (user_data['currency'] or 'USD').upper()
    new_curr = "INR" if current_curr == "USD" else "USD"
    
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE users SET currency=$1 WHERE user_id=$2", new_curr, call.from_user.id)
        
    invalidate_user_cache(call.from_user.id)
    symbol = "₹" if new_curr == "INR" else "$"
    await call.answer(f"Currency updated to {new_curr} ({symbol})", show_alert=True)
    
    try:
        await call.message.edit_reply_markup(reply_markup=get_settings_keyboard(user_data['notifications_enabled'], new_curr))
    except:
        pass

@dp.callback_query(F.data == "menu_get_task")
async def cb_get_task(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.clear()
    user_id = call.from_user.id
    user_data = await get_user_data(user_id)
    user_curr = user_data['currency']

    async with db_pool.acquire() as conn:
        existing = await conn.fetchrow('''
            SELECT t.id, t.title, t.details, t.reward, t.status, a.assigned_at 
            FROM task_assignments a
            JOIN tasks t ON a.task_id = t.id
            WHERE a.user_id=$1
            ORDER BY a.assigned_at DESC
            LIMIT 1
        ''', user_id)
        
        if existing:
            task_id = existing['id']
            assigned_time = existing['assigned_at']
            task_status = existing['status']
            
            if task_status == 'pending_review':
                if SINGLE_TASK_STATUS:
                    txt = '<tg-emoji emoji-id="5195033767969839232">🚀</tg-emoji> Your task submission is currently under admin review. Please wait for approval before taking another task.'
                    try:
                        await call.message.edit_text(txt, reply_markup=get_main_menu_keyboard(), parse_mode=ParseMode.HTML)
                    except:
                        await call.message.answer(txt, reply_markup=get_main_menu_keyboard(), parse_mode=ParseMode.HTML)
                    await state.update_data(last_menu_msg_id=call.message.message_id)
                    return
            elif task_status == 'assigned':
                expire_time = assigned_time + timedelta(minutes=30)
                remaining = expire_time - datetime.utcnow()
                total_seconds = int(remaining.total_seconds())
                
                if total_seconds > 0:
                    mins = total_seconds // 60
                    secs = total_seconds % 60
                    
                    try:
                        parts = existing['details'].split(" | ")
                        username = parts[0].replace("Email: ", "").strip()
                        password = parts[1].replace("Pass: ", "").strip()
                    except:
                        username = existing['title'].replace("Login to ", "")
                        password = "See Admin"

                    reward_str = format_currency(existing["reward"], user_curr)
                    txt = (
                        f'<tg-emoji emoji-id="5447644880824181073">⚠️</tg-emoji> <b>You already have an active task.</b>\n\n'
                        f'<tg-emoji emoji-id="5310278924616356636">🎯</tg-emoji> <b>Your Current Task</b>\n\n'
                        f'<tg-emoji emoji-id="5197269100878907942">✍️</tg-emoji> #{task_id}\n'
                        f'<tg-emoji emoji-id="5870458774455587120">👤</tg-emoji> <b>Email:</b> {username} | <tg-emoji emoji-id="6005570495603282482">🔑</tg-emoji> <b>Password:</b> <code>{password}</code>\n'
                        f'<tg-emoji emoji-id="5417924076503062111">💰</tg-emoji> <b>Reward:</b> {reward_str}\n\n'
                        f'<tg-emoji emoji-id="5195033767969839232">🚀</tg-emoji> Time Remaining: {mins}m {secs}s'
                    )
                    try:
                        await call.message.edit_text(txt, parse_mode=ParseMode.HTML, reply_markup=get_task_action_keyboard())
                        await conn.execute("UPDATE task_assignments SET message_id=$1 WHERE task_id=$2", call.message.message_id, task_id)
                    except:
                        new_m = await call.message.answer(txt, parse_mode=ParseMode.HTML, reply_markup=get_task_action_keyboard())
                        await conn.execute("UPDATE task_assignments SET message_id=$1 WHERE task_id=$2", new_m.message_id, task_id)
                    await state.update_data(last_menu_msg_id=call.message.message_id)
                    return
                else:
                    async with conn.transaction():
                        await conn.execute('DELETE FROM task_assignments WHERE user_id=$1 AND task_id=$2', user_id, task_id)
                        await conn.execute('UPDATE tasks SET status=$1 WHERE id=$2', 'available', task_id)

        task = await conn.fetchrow("SELECT id, title, details, reward FROM tasks WHERE status='available' ORDER BY RANDOM() LIMIT 1")
        if not task:
            txt = '📭 No tasks available right now.'
            try:
                await call.message.edit_text(txt, reply_markup=get_main_menu_keyboard())
            except:
                await call.message.answer(txt, reply_markup=get_main_menu_keyboard())
            await state.update_data(last_menu_msg_id=call.message.message_id)
            return
        
        task_id = task['id']
        title = task['title']
        details = task['details']
        reward = task['reward']

        try:
            parts = details.split(" | ")
            username = parts[0].replace("Email: ", "").strip()
        except:
            username = title.replace("Login to ", "").strip()

        if DEFAULT_TASK_PASS_STATUS:
            password = DEFAULT_TASK_PASS
        else:
            password = generate_random_password(12)

        new_details = f"Email: {username} | Pass: {password}"
        
        async with conn.transaction():
            await conn.execute("UPDATE tasks SET status='assigned', details=$1 WHERE id=$2", new_details, task_id)
            await conn.execute('INSERT INTO task_assignments(task_id, user_id, message_id) VALUES ($1, $2, $3)', task_id, user_id, call.message.message_id)
            await conn.execute('INSERT INTO task_history(task_id, user_id, password_used) VALUES ($1, $2, $3)', task_id, user_id, password)

    reward_str = format_currency(reward, user_curr)
    txt = (
        f'<tg-emoji emoji-id="5310278924616356636">🎯</tg-emoji> <b>Task #{task_id}</b>\n\n'
        f'<tg-emoji emoji-id="5870458774455587120">👤</tg-emoji> <b>Email:</b> {username} | <tg-emoji emoji-id="6005570495603282482">🔑</tg-emoji> <b>Password:</b> <code>{password}</code>\n'
        f'<tg-emoji emoji-id="5417924076503062111">💰</tg-emoji> <b>Reward:</b> {reward_str}\n\n'
        f'<tg-emoji emoji-id="5195033767969839232">🚀</tg-emoji> You have ONLY 30 MINUTES to complete this task.'
    )
    try:
        await call.message.edit_text(txt, parse_mode=ParseMode.HTML, reply_markup=get_task_action_keyboard())
        async with db_pool.acquire() as conn:
            await conn.execute("UPDATE task_assignments SET message_id=$1 WHERE task_id=$2", call.message.message_id, task_id)
    except:
        new_m = await call.message.answer(txt, parse_mode=ParseMode.HTML, reply_markup=get_task_action_keyboard())
        async with db_pool.acquire() as conn:
            await conn.execute("UPDATE task_assignments SET message_id=$1 WHERE task_id=$2", new_m.message_id, task_id)
    await state.update_data(last_menu_msg_id=call.message.message_id)

@dp.callback_query(F.data == "menu_balance")
async def cb_balance(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.clear()
    user_data = await get_user_data(call.from_user.id)
    bal = user_data['balance'] if user_data else 0.0
    upi = user_data['upi'] if user_data and user_data['upi'] else "None"
    usdt = user_data['usdt_address'] if user_data and user_data['usdt_address'] else "None"
    ultra = user_data['ultra_number'] if user_data and user_data['ultra_number'] else "None"
    curr = user_data['currency'] if user_data else "USD"
    
    upi_set = upi != "None" and upi != ""
    usdt_set = usdt != "None" and usdt != ""
    ultra_set = ultra != "None" and ultra != ""
    formatted_bal = format_currency(bal, curr)
    
    ultra_line = f'\n<tg-emoji emoji-id="5195033767969839232">⚡️</tg-emoji> <b>Ultra Gateway:</b> <code>{ultra}</code>' if ULTRA_STATUS else ""
    
    text = (
        f'<tg-emoji emoji-id="5445353829304387411">💳</tg-emoji> <b>Balance</b>\n\n'
        f'<tg-emoji emoji-id="5278467510604160626">💵</tg-emoji> <b>Available:</b> {formatted_bal}\n'
        f'<tg-emoji emoji-id="6291696801636424911">🏦</tg-emoji> <b>UPI:</b> <code>{upi}</code>\n'
        f'<tg-emoji emoji-id="5197434882321567830">🪙</tg-emoji> <b>USDT BEP-20:</b> <code>{usdt}</code>'
        f'{ultra_line}'
    )
    
    try:
        await call.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=get_balance_inline_keyboard(upi_set, usdt_set, ultra_set))
    except Exception:
        await call.message.answer(text, parse_mode=ParseMode.HTML, reply_markup=get_balance_inline_keyboard(upi_set, usdt_set, ultra_set))
    await state.update_data(last_menu_msg_id=call.message.message_id)

@dp.callback_query(F.data == "menu_sell_gmail")
async def cb_sell_gmail(call: CallbackQuery, state: FSMContext):
    if not SELL_GMAIL_STATUS:
        await call.answer("⚠️ Selling Gmail is currently disabled by Admin!", show_alert=True)
        return

    await call.answer()
    await state.clear()
    await state.set_state(UserState.selling_username)
    user_data = await get_user_data(call.from_user.id)
    rate_str = format_currency(GMAIL_SELL_RATE, user_data['currency'])
    txt = (
        f'<tg-emoji emoji-id="5445221832074483553">🏷️</tg-emoji> <b>Sell Price {rate_str}/Gmail</b>\n\n'
        '<tg-emoji emoji-id="5377548235709619284">🤑</tg-emoji> <b>Step 1/2:</b> Please send the Gmail <b>Username</b> (e.g., <code>example@gmail.com</code>):'
    )
    try:
        await call.message.edit_text(txt, parse_mode=ParseMode.HTML, reply_markup=get_back_inline_keyboard())
    except:
        await call.message.answer(txt, parse_mode=ParseMode.HTML, reply_markup=get_back_inline_keyboard())
    await state.update_data(last_menu_msg_id=call.message.message_id)

@dp.message(UserState.selling_username, F.text, ~F.text.startswith("/"), ~F.text.in_(MENU_BUTTONS))
async def process_sell_username(message: Message, state: FSMContext):
    if not SELL_GMAIL_STATUS:
        await message.answer("⚠️ Selling Gmail is currently disabled by Admin!", reply_markup=get_main_menu_keyboard())
        await state.clear()
        return

    username_input = message.text.strip()
    if "@gmail.com" not in username_input.lower() and "@" not in username_input:
        username = f"{username_input}@gmail.com"
    else:
        username = username_input

    search_pattern = f"%{username.lower()}%"

    async with db_pool.acquire() as conn:
        existing_sell = await conn.fetchval(
            "SELECT id FROM pending_sells WHERE LOWER(details) LIKE $1",
            search_pattern
        )
        existing_task = await conn.fetchval(
            "SELECT id FROM tasks WHERE LOWER(title) LIKE $1 OR LOWER(details) LIKE $1",
            search_pattern
        )

    if existing_sell or existing_task:
        await message.answer(
            "❌ <b>This email is already in the database. You cannot sell the same email twice.</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=get_main_menu_keyboard()
        )
        await state.clear()
        return

    is_valid = await is_gmail_registered(username, user_id=message.from_user.id)
    if not is_valid:
        await message.answer(
            f"❌ This Gmail account ({username}) does not exist on Google!\n\n"
            f"Please Provide Valid Gmail Username, then try again.",
            parse_mode=ParseMode.HTML,
            reply_markup=get_back_inline_keyboard()
        )
        return

    await state.update_data(sell_username=username)
    await state.set_state(UserState.selling_password)
    sent_msg = await message.answer(
        '<tg-emoji emoji-id="6005570495603282482">🔑</tg-emoji> <b>Step 2/2:</b> Now send the <b>Password</b> for this Gmail account:',
        parse_mode=ParseMode.HTML,
        reply_markup=get_back_inline_keyboard()
    )
    await state.update_data(last_menu_msg_id=sent_msg.message_id)

@dp.message(UserState.selling_password, F.text, ~F.text.startswith("/"), ~F.text.in_(MENU_BUTTONS))
async def process_sell_password(message: Message, state: FSMContext):
    if not SELL_GMAIL_STATUS:
        await message.answer("⚠️ Selling Gmail is currently disabled by Admin!", reply_markup=get_main_menu_keyboard())
        await state.clear()
        return

    password = message.text.strip()
    data = await state.get_data()
    username = data.get('sell_username')
    user_id = message.from_user.id
    rate = GMAIL_SELL_RATE

    details = f"Username: {username}\nPassword: {password}"

    async with db_pool.acquire() as conn:
        sell_id = await conn.fetchval(
            "INSERT INTO pending_sells (user_id, details, amount) VALUES ($1, $2, $3) RETURNING id",
            user_id, details, rate
        )

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Approve", callback_data=f"sa:{sell_id}", icon_custom_emoji_id="6217663806110175239", style="success"),
        InlineKeyboardButton(text="Decline", callback_data=f"sd:{sell_id}", icon_custom_emoji_id="5274099962655816924", style="danger")
    ]])

    admin_message_text = (
        f'<tg-emoji emoji-id="5377548235709619284">📨</tg-emoji> <b>New Gmail Sell Request #{sell_id}</b>\n\n'
        f'<tg-emoji emoji-id="5870458774455587120">👤</tg-emoji> <b>Seller:</b> @{message.from_user.username} (<code>{user_id}</code>)\n'
        f'📧 <b>Username:</b> <code>{username}</code>\n'
        f'<tg-emoji emoji-id="6005570495603282482">🔑</tg-emoji> <b>Password:</b> <code>{password}</code>\n'
        f'<tg-emoji emoji-id="5417924076503062111">💰</tg-emoji> <b>Payout Rate:</b> ₹{rate:.2f}'
    )

    await bot.send_message(
        ADMIN_ID, 
        admin_message_text, 
        reply_markup=kb, 
        parse_mode=ParseMode.HTML
    )

    # Broadcast real-time stock alert to all authorized workers
    async def alert_authorized_workers():
        if not WORKER_BOT_TOKEN:
            return
        try:
            w_bot = Bot(token=WORKER_BOT_TOKEN)
            async with db_pool.acquire() as conn:
                active_workers = await conn.fetch(
                    "SELECT worker_id FROM worker_permissions WHERE is_active = TRUE AND can_sell_gmail = TRUE AND is_deleted = FALSE"
                )
                current_stock = await conn.fetchval("SELECT COUNT(*) FROM pending_sells WHERE status = 'pending_review' AND claimed_by IS NULL") or 0
            
            w_msg = (
                f'<tg-emoji emoji-id="5377548235709619284">📦</tg-emoji> <b>New Gmail Sell Request Stock!</b>\n\n'
                f'📊 <b>Available Stock:</b> <code>{current_stock}</code>\n\n'
                f'Go to <b>Pending Reviews</b> to claim review tasks.'
            )
            for w in active_workers:
                try:
                    await w_bot.send_message(w['worker_id'], w_msg, parse_mode=ParseMode.HTML)
                except Exception:
                    pass
            await w_bot.session.close()
        except Exception as e:
            print(f"Error alerting workers of sell request: {e}")

    asyncio.create_task(alert_authorized_workers())

    sent_msg = await message.answer(
        f'<tg-emoji emoji-id="6217663806110175239">✅</tg-emoji> Your Gmail sell account details (Request #{sell_id}) have been sent for admin review.\n\n'
        f'<tg-emoji emoji-id="5447644880824181073">⚠️</tg-emoji> <b>Important:</b> Please make sure to <b>logout</b> of this account from your device!', 
        reply_markup=get_main_menu_keyboard(), 
        parse_mode=ParseMode.HTML
    )
    await state.clear()
    await state.update_data(last_menu_msg_id=sent_msg.message_id)

@dp.callback_query(F.data == "menu_history")
@dp.message(Command("history"), StateFilter("*"))
async def cb_history(event: CallbackQuery | Message, state: FSMContext):
    if isinstance(event, CallbackQuery):
        await event.answer()

    await state.clear()
    user_id = event.from_user.id
    text, reply_markup = await render_transaction_history_page(user_id, page=1, is_admin=False)

    if isinstance(event, CallbackQuery):
        try:
            await event.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
        except Exception:
            await event.message.answer(text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
    else:
        sent_msg = await event.answer(text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
        await state.update_data(last_menu_msg_id=sent_msg.message_id)

@dp.callback_query(F.data.startswith("user_tx_page:"))
async def cb_user_tx_page(call: CallbackQuery):
    await call.answer()
    page = int(call.data.split(":")[1])
    text, reply_markup = await render_transaction_history_page(call.from_user.id, page=page, is_admin=False)
    try:
        await call.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
    except Exception:
        pass

@dp.callback_query(F.data == "menu_support")
async def cb_support_start(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.clear()
    await state.set_state(UserState.waiting_for_support)
    txt = (
        '🛠 <b>Support Center</b>\n\n'
        'Please type and send your question, issue, or message below.\n\n'
        'An admin will be notified and respond to you as soon as possible.'
    )
    try:
        await call.message.edit_text(txt, parse_mode=ParseMode.HTML, reply_markup=get_support_cancel_keyboard())
    except Exception:
        await call.message.answer(txt, parse_mode=ParseMode.HTML, reply_markup=get_support_cancel_keyboard())

@dp.message(UserState.waiting_for_support, ~F.text.startswith("/") if F.text else True, ~F.text.in_(MENU_BUTTONS) if F.text else True)
async def process_user_support_message(message: Message, state: FSMContext):
    user_id = message.from_user.id
    username = f"@{message.from_user.username}" if message.from_user.username else f"ID: {user_id}"

    user_msg_content = message.caption if message.photo else message.text
    if not user_msg_content:
        user_msg_content = "Photo attachment"

    SUPPORT_REQUESTS_CACHE[user_id] = {
        "username": username,
        "message": user_msg_content
    }

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="👁 View Request", 
            callback_data=f"view_supp:{user_id}",
            style="primary"
        )
    ]])

    admin_init_text = "🛠 <b>A new support request</b>"

    try:
        if message.photo:
            await bot.send_photo(
                ADMIN_ID, 
                photo=message.photo[-1].file_id, 
                caption=admin_init_text,
                reply_markup=kb, 
                parse_mode=ParseMode.HTML
            )
        else:
            await bot.send_message(
                ADMIN_ID, 
                admin_init_text, 
                reply_markup=kb, 
                parse_mode=ParseMode.HTML
            )
        
        await message.answer(
            "✅ <b>Your support message has been delivered to our team!</b>\n\nWe will get back to you shortly.",
            parse_mode=ParseMode.HTML,
            reply_markup=get_main_menu_keyboard()
        )
    except Exception as e:
        print(f"Error forwarding support message: {e}")
        await message.answer("❌ Failed to send your support message. Please try again later.", reply_markup=get_main_menu_keyboard())

    await state.clear()

@dp.callback_query(F.data.startswith("view_supp:"))
async def cb_admin_view_support(call: CallbackQuery):
    await call.answer()
    if call.from_user.id != ADMIN_ID:
        return

    target_user_id = int(call.data.split(":")[1])
    supp_info = SUPPORT_REQUESTS_CACHE.get(target_user_id, {})
    username = supp_info.get("username", f"ID: {target_user_id}")
    msg_text = supp_info.get("message", "N/A")

    revealed_text = (
        f"🛠 <b>Support Request</b>\n\n"
        f"👤 <b>Username:</b> {username}\n"
        f"🆔 <b>User ID:</b> <code>{target_user_id}</code>\n"
        f"💬 <b>Support Message:</b>\n{msg_text}"
    )

    action_kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="💬 Reply User",
                callback_data=f"sr:{target_user_id}",
                icon_custom_emoji_id="5870458774455587120",
                style="primary"
            )
        ],
        [
            InlineKeyboardButton(
                text="🚫 Ban User",
                callback_data=f"ban_supp:{target_user_id}",
                icon_custom_emoji_id="5274099962655816924",
                style="danger"
            )
        ]
    ])

    try:
        if call.message.photo:
            await call.message.edit_caption(
                caption=revealed_text,
                reply_markup=action_kb,
                parse_mode=ParseMode.HTML
            )
        else:
            await call.message.edit_text(
                text=revealed_text,
                reply_markup=action_kb,
                parse_mode=ParseMode.HTML
            )
    except Exception as e:
        print(f"Error viewing support details: {e}")

@dp.callback_query(F.data.startswith("ban_supp:"))
async def cb_admin_ban_support_user(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return

    target_user_id = int(call.data.split(":")[1])
    if target_user_id == ADMIN_ID:
        await call.answer("❌ You cannot ban yourself!", show_alert=True)
        return

    async with db_pool.acquire() as conn:
        await conn.execute("INSERT INTO banned_users (user_id) VALUES ($1) ON CONFLICT DO NOTHING", target_user_id)

    BANNED_USERS_CACHE.add(target_user_id)
    await call.answer("🚫 User has been directly banned!", show_alert=True)

    action_kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="💬 Reply User",
                callback_data=f"sr:{target_user_id}",
                icon_custom_emoji_id="5870458774455587120",
                style="primary"
            )
        ]
    ])

    try:
        if call.message.photo:
            current_caption = call.message.caption or ""
            await call.message.edit_caption(
                caption=current_caption + "\n\n🚫 <b>User Banned</b>",
                reply_markup=action_kb,
                parse_mode=ParseMode.HTML
            )
        else:
            current_text = call.message.text or ""
            await call.message.edit_text(
                text=current_text + "\n\n🚫 <b>User Banned</b>",
                reply_markup=action_kb,
                parse_mode=ParseMode.HTML
            )
    except Exception as e:
        print(f"Error editing banned support message: {e}")

@dp.callback_query(F.data.startswith("sr:"))
async def cb_admin_reply_support(call: CallbackQuery, state: FSMContext):
    await call.answer()
    if call.from_user.id != ADMIN_ID:
        return

    target_user_id = int(call.data.split(":")[1])
    await state.set_state(AdminState.waiting_for_support_reply)
    await state.update_data(reply_target_user_id=target_user_id)

    await call.message.answer(
        f"✉️ <b>Send your reply to User <code>{target_user_id}</code> below:</b>",
        parse_mode=ParseMode.HTML
    )

@dp.message(AdminState.waiting_for_support_reply, ~F.text.startswith("/") if F.text else True, ~F.text.in_(MENU_BUTTONS) if F.text else True)
async def process_admin_support_reply(message: Message, state: FSMContext):
    data = await state.get_data()
    target_user_id = data.get('reply_target_user_id')

    if not target_user_id:
        await message.answer("❌ Error: Reply target lost.", reply_markup=get_admin_menu_keyboard())
        await state.clear()
        return

    reply_header = "🛠 <b>Support Reply from Admin:</b>\n\n"

    try:
        if message.photo:
            await bot.send_photo(
                target_user_id,
                photo=message.photo[-1].file_id,
                caption=reply_header + (message.caption or ""),
                parse_mode=ParseMode.HTML
            )
        else:
            await bot.send_message(
                target_user_id,
                reply_header + message.text,
                parse_mode=ParseMode.HTML
            )

        await message.answer(
            f"✅ <b>Reply successfully sent to User <code>{target_user_id}</code>!</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=get_admin_menu_keyboard()
        )
    except Exception as e:
        await message.answer(
            f"❌ Failed to send reply to User <code>{target_user_id}</code>.\n\nError: <code>{e}</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=get_admin_menu_keyboard()
        )

    await state.clear()

# ============================================
# USER PAYMENT ADDRESS SETTERS
# ============================================

@dp.message(UserState.setting_upi, ~F.text.startswith("/"), ~F.text.in_(MENU_BUTTONS))
async def process_setting_upi(message: Message, state: FSMContext):
    upi_input = message.text.strip()
    user_id = message.from_user.id

    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE users SET upi=$1 WHERE user_id=$2", upi_input, user_id)

    invalidate_user_cache(user_id)
    await message.answer(
        f"✅ <b>UPI ID Updated Successfully!</b>\n\n<code>{upi_input}</code>",
        parse_mode=ParseMode.HTML,
        reply_markup=get_main_menu_keyboard()
    )
    await state.clear()

@dp.message(UserState.setting_usdt, ~F.text.startswith("/"), ~F.text.in_(MENU_BUTTONS))
async def process_setting_usdt(message: Message, state: FSMContext):
    usdt_input = message.text.strip()
    user_id = message.from_user.id

    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE users SET usdt_address=$1 WHERE user_id=$2", usdt_input, user_id)

    invalidate_user_cache(user_id)
    await message.answer(
        f"✅ <b>USDT BEP-20 Address Updated Successfully!</b>\n\n<code>{usdt_input}</code>",
        parse_mode=ParseMode.HTML,
        reply_markup=get_main_menu_keyboard()
    )
    await state.clear()

@dp.message(UserState.setting_ultra, ~F.text.startswith("/"), ~F.text.in_(MENU_BUTTONS))
async def process_setting_ultra(message: Message, state: FSMContext):
    ultra_input = message.text.strip()
    user_id = message.from_user.id

    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE users SET ultra_number=$1 WHERE user_id=$2", ultra_input, user_id)

    invalidate_user_cache(user_id)
    await message.answer(
        f"✅ <b>Ultra Gateway Number Updated Successfully!</b>\n\n<code>{ultra_input}</code>\n"
        f"🌐 <b>Ultra Gateway:</b> https://ultra-pay.store",
        parse_mode=ParseMode.HTML,
        reply_markup=get_main_menu_keyboard()
    )
    await state.clear()

# ============================================
# MULTI-WORKER MANAGEMENT SYSTEM
# ============================================

async def render_workers_list_text_and_kb():
    async with db_pool.acquire() as conn:
        workers = await conn.fetch("SELECT worker_id, name, is_active, can_sell_gmail FROM worker_permissions WHERE is_deleted = FALSE ORDER BY created_at ASC, worker_id ASC")

    if not workers:
        text = "👷 <b>Manage Workers</b>\n\n📭 No active workers registered in database yet."
        kb = InlineKeyboardBuilder()
        return text, kb.as_markup()

    text = f"👷 <b>Manage Workers ({len(workers)} Registered)</b>\n\nSelect a worker below to manage full permissions:\n"
    kb = InlineKeyboardBuilder()

    for idx, w in enumerate(workers, start=1):
        status_icon = "🟢" if w['is_active'] else "🔴"
        sell_icon = "📨" if w['can_sell_gmail'] else "🚫"
        worker_display_name = w['name'] if w['name'] else f"Worker ({w['worker_id']})"
        btn_label = f"#{idx} {worker_display_name} {status_icon}{sell_icon}"
        kb.button(text=btn_label, callback_data=f"adm_work_view:{w['worker_id']}")

    kb.adjust(1)
    return text, kb.as_markup()

@dp.message(F.text == "👷 Manage Workers", StateFilter("*"))
async def admin_btn_manage_workers(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.clear()
    text, reply_markup = await render_workers_list_text_and_kb()
    await message.answer(text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)

@dp.callback_query(F.data == "adm_work_back")
async def cb_admin_workers_back(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    await call.answer()
    text, reply_markup = await render_workers_list_text_and_kb()
    try:
        await call.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
    except Exception:
        pass

@dp.callback_query(F.data.startswith("adm_work_view:"))
async def cb_admin_view_worker(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    await call.answer()
    worker_id = int(call.data.split(":")[1])

    async with db_pool.acquire() as conn:
        w = await conn.fetchrow("SELECT worker_id, name, is_active, can_sell_gmail FROM worker_permissions WHERE worker_id=$1", worker_id)

    if not w:
        await call.message.edit_text("❌ Worker not found in database.", reply_markup=None)
        return

    status_str = "🟢 ACTIVE (ON)" if w['is_active'] else "🔴 DISABLED (OFF)"
    sell_str = "🟢 ENABLED (Can review sell & get alerts)" if w['can_sell_gmail'] else "🔴 DISABLED (Sell hidden)"

    text = (
        f"👷 <b>Worker Control Panel</b>\n\n"
        f"🆔 <b>Worker ID:</b> <code>{w['worker_id']}</code>\n"
        f"🏷 <b>Name:</b> {w['name']}\n"
        f"⚡️ <b>Bot Access:</b> {status_str}\n"
        f"📨 <b>Sell Gmail Feature:</b> {sell_str}\n\n"
        f"Use the buttons below to toggle permissions:"
    )

    kb = InlineKeyboardBuilder()
    toggle_access_label = "🔴 Turn Worker OFF" if w['is_active'] else "🟢 Turn Worker ON"
    toggle_sell_label = "🚫 Disable Sell Gmail" if w['can_sell_gmail'] else "📨 Enable Sell Gmail"

    kb.button(text=toggle_access_label, callback_data=f"adm_work_tog_access:{worker_id}")
    kb.button(text=toggle_sell_label, callback_data=f"adm_work_tog_sell:{worker_id}")
    kb.button(text="⬅️ Back to Workers", callback_data="adm_work_back")
    kb.adjust(1, 1, 1)

    try:
        await call.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=kb.as_markup())
    except Exception:
        pass

@dp.callback_query(F.data.startswith("adm_work_tog_access:"))
async def cb_admin_toggle_worker_access(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    worker_id = int(call.data.split(":")[1])

    async with db_pool.acquire() as conn:
        current_state = await conn.fetchval("SELECT is_active FROM worker_permissions WHERE worker_id=$1", worker_id)
        new_state = not current_state
        await conn.execute("UPDATE worker_permissions SET is_active=$1 WHERE worker_id=$2", new_state, worker_id)

    status_msg = "ENABLED" if new_state else "DISABLED"
    await call.answer(f"Worker {worker_id} access is now {status_msg}!", show_alert=True)
    await cb_admin_view_worker(call)

@dp.callback_query(F.data.startswith("adm_work_tog_sell:"))
async def cb_admin_toggle_worker_sell(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    worker_id = int(call.data.split(":")[1])

    async with db_pool.acquire() as conn:
        current_state = await conn.fetchval("SELECT can_sell_gmail FROM worker_permissions WHERE worker_id=$1", worker_id)
        new_state = not current_state
        await conn.execute("UPDATE worker_permissions SET can_sell_gmail=$1 WHERE worker_id=$2", new_state, worker_id)

    status_msg = "ENABLED" if new_state else "DISABLED"
    await call.answer(f"Worker {worker_id} Sell Gmail is now {status_msg}!", show_alert=True)
    await cb_admin_view_worker(call)

# ============================================
# HIDDEN ADMIN WORKER COMMANDS (/name, /delete, /recover)
# ============================================

@dp.message(Command("name"))
async def admin_cmd_set_worker_name(message: Message, command: CommandObject):
    if message.from_user.id != ADMIN_ID:
        return
    
    args = (command.args or "").strip()
    if not args:
        await message.answer("⚠️ <b>Usage:</b> <code>/name #1 Karim</code>", parse_mode=ParseMode.HTML)
        return

    parts = args.split(maxsplit=1)
    target_tag = parts[0]
    new_name = parts[1] if len(parts) > 1 else ""

    if not new_name:
        await message.answer("⚠️ Please provide a name. <i>Example:</i> <code>/name #1 Karim</code>", parse_mode=ParseMode.HTML)
        return

    index_match = re.search(r'\d+', target_tag)
    if not index_match:
        await message.answer("❌ Invalid format. Use <code>/name #1 NewName</code>", parse_mode=ParseMode.HTML)
        return

    target_index = int(index_match.group(0))

    async with db_pool.acquire() as conn:
        active_workers = await conn.fetch("SELECT worker_id, name FROM worker_permissions WHERE is_deleted = FALSE ORDER BY created_at ASC, worker_id ASC")
        
        if target_index < 1 or target_index > len(active_workers):
            await message.answer(f"❌ Worker <b>#{target_index}</b> not found. Active count: {len(active_workers)}", parse_mode=ParseMode.HTML)
            return

        target_worker = active_workers[target_index - 1]
        target_worker_id = target_worker['worker_id']

        await conn.execute("UPDATE worker_permissions SET name=$1 WHERE worker_id=$2", new_name, target_worker_id)

    await message.answer(f"✅ <b>Worker #{target_index} (ID: <code>{target_worker_id}</code>) name updated to:</b> <code>{new_name}</code>", parse_mode=ParseMode.HTML)

@dp.message(Command("delete"))
async def admin_cmd_delete_worker(message: Message, command: CommandObject):
    if message.from_user.id != ADMIN_ID:
        return

    args = (command.args or "").strip()
    if not args:
        await message.answer("⚠️ <b>Usage:</b> <code>/delete #1</code>", parse_mode=ParseMode.HTML)
        return

    index_match = re.search(r'\d+', args)
    if not index_match:
        await message.answer("❌ Invalid format. Use <code>/delete #1</code>", parse_mode=ParseMode.HTML)
        return

    target_index = int(index_match.group(0))

    async with db_pool.acquire() as conn:
        active_workers = await conn.fetch("SELECT worker_id, name FROM worker_permissions WHERE is_deleted = FALSE ORDER BY created_at ASC, worker_id ASC")

        if target_index < 1 or target_index > len(active_workers):
            await message.answer(f"❌ Worker <b>#{target_index}</b> not found. Active count: {len(active_workers)}", parse_mode=ParseMode.HTML)
            return

        target_worker = active_workers[target_index - 1]
        target_worker_id = target_worker['worker_id']

        # Complete off (is_active=FALSE) and mark as deleted
        await conn.execute("UPDATE worker_permissions SET is_active = FALSE, is_deleted = TRUE WHERE worker_id = $1", target_worker_id)

    await message.answer(
        f"🗑 <b>Worker #{target_index} (ID: <code>{target_worker_id}</code>) is now OFF and deleted from Manage Workers!</b>\n"
        f"All subsequent worker IDs have been shifted down automatically.\n\n"
        f"<i>To recover this worker later, use:</i> <code>/recover #-{target_index}</code>",
        parse_mode=ParseMode.HTML
    )

@dp.message(Command("recover"))
async def admin_cmd_recover_worker(message: Message, command: CommandObject):
    if message.from_user.id != ADMIN_ID:
        return

    args = (command.args or "").strip()
    if not args:
        await message.answer("⚠️ <b>Usage:</b> <code>/recover #-1</code>", parse_mode=ParseMode.HTML)
        return

    index_match = re.search(r'\d+', args)
    if not index_match:
        await message.answer("❌ Invalid format. Use <code>/recover #-1</code>", parse_mode=ParseMode.HTML)
        return

    target_index = int(index_match.group(0))

    async with db_pool.acquire() as conn:
        deleted_workers = await conn.fetch("SELECT worker_id, name FROM worker_permissions WHERE is_deleted = TRUE ORDER BY created_at ASC, worker_id ASC")

        if not deleted_workers:
            await message.answer("📭 No deleted workers available to recover.", parse_mode=ParseMode.HTML)
            return

        if target_index < 1 or target_index > len(deleted_workers):
            await message.answer(f"❌ Deleted worker record with index #{target_index} not found. (Total deleted: {len(deleted_workers)})", parse_mode=ParseMode.HTML)
            return

        target_worker = deleted_workers[target_index - 1]
        target_worker_id = target_worker['worker_id']

        # Restore worker, set is_active=TRUE, is_deleted=FALSE, and refresh created_at so it takes the latest free index
        await conn.execute(
            "UPDATE worker_permissions SET is_active = TRUE, is_deleted = FALSE, created_at = CURRENT_TIMESTAMP WHERE worker_id = $1",
            target_worker_id
        )
        
        active_count = await conn.fetchval("SELECT COUNT(*) FROM worker_permissions WHERE is_deleted = FALSE")

    await message.answer(
        f"✅ <b>Worker recovered successfully!</b>\n\n"
        f"🆔 <b>Worker ID:</b> <code>{target_worker_id}</code>\n"
        f"🏷 <b>Assigned Index:</b> <b>#{active_count}</b> (Latest Free ID)\n"
        f"⚡️ <b>Status:</b> 🟢 Active & Restored to Manage Workers",
        parse_mode=ParseMode.HTML
    )

# ============================================
# ADMIN PANEL COMMAND & BUTTON HANDLERS
# ============================================

@dp.message(Command("adminpanel"), StateFilter("*"))
@dp.message(Command("admin"), StateFilter("*"))
async def open_admin_panel(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.clear()
    await message.answer(
        "🛠 <b>Admin Control Panel</b>\n\nChoose an action from the admin menu below:",
        parse_mode=ParseMode.HTML,
        reply_markup=get_admin_menu_keyboard()
    )

@dp.message(F.text.in_({"🔴 Ref Status: OFF", "🟢 Ref Status: ON"}), StateFilter("*"))
async def admin_btn_toggle_ref_status(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    global REF_STATUS
    REF_STATUS = not REF_STATUS
    new_val = 'on' if REF_STATUS else 'off'

    async with db_pool.acquire() as conn:
        await conn.execute("INSERT INTO bot_settings (key, value) VALUES ('ref_status', $1) ON CONFLICT (key) DO UPDATE SET value = $1", new_val)

    status_str = "🟢 <b>Referral rewards are now ENABLED!</b>" if REF_STATUS else "🔴 <b>Referral rewards are now SILENTLY DISABLED!</b> (Users won't receive bonuses upon approvals)"
    await message.answer(status_str, parse_mode=ParseMode.HTML, reply_markup=get_admin_menu_keyboard())

@dp.message(F.text.in_({"🔴 Ultra Status: OFF", "🟢 Ultra Status: ON"}), StateFilter("*"))
async def admin_btn_toggle_ultra_status(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    global ULTRA_STATUS
    ULTRA_STATUS = not ULTRA_STATUS
    new_val = 'on' if ULTRA_STATUS else 'off'

    async with db_pool.acquire() as conn:
        await conn.execute("INSERT INTO bot_settings (key, value) VALUES ('ultra_status', $1) ON CONFLICT (key) DO UPDATE SET value = $1", new_val)

    status_str = "🟢 <b>Ultra Gateway is now ON and available in withdrawal options!</b>" if ULTRA_STATUS else "🔴 <b>Ultra Gateway is now OFF and hidden from withdrawal options!</b>"
    await message.answer(status_str, parse_mode=ParseMode.HTML, reply_markup=get_admin_menu_keyboard())

@dp.message(F.text == "📋 Tasks", StateFilter("*"))
async def admin_btn_view_all_tasks_dashboard(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.clear()
    text, reply_markup = await render_admin_all_tasks_page(page=1)
    await message.answer(text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)

@dp.callback_query(F.data.startswith("adm_all_tasks_page:"))
async def cb_admin_all_tasks_page(call: CallbackQuery):
    await call.answer()
    page = int(call.data.split(":")[1])
    text, reply_markup = await render_admin_all_tasks_page(page=page)
    try:
        await call.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
    except Exception:
        pass

@dp.message(F.text == "🟢 Available Tasks", StateFilter("*"))
async def admin_btn_view_tasks_dashboard(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.clear()
    text, reply_markup = await render_admin_tasks_page(page=1)
    await message.answer(text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)

@dp.callback_query(F.data.startswith("adm_tasks_page:"))
async def cb_admin_tasks_page(call: CallbackQuery):
    await call.answer()
    page = int(call.data.split(":")[1])
    text, reply_markup = await render_admin_tasks_page(page=page)
    try:
        await call.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
    except Exception:
        pass

@dp.message(F.text == "⚙️ Validator", StateFilter("*"))
async def admin_btn_validator_menu(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.clear()
    
    val_status_str = "🟢 <b>Active</b>" if VALIDATOR_ENABLED else "🔴 <b>Deactivated</b>"
    provider_name = "Emailable" if VALIDATOR_PROVIDER == "emailable" else "MyEmailVerifier"
    provider_url = get_provider_url()

    text = (
        f"⚙️ <b>Gmail Validator Management</b>\n\n"
        f"🌐 <b>Current Provider:</b> <code>{provider_name}</code>\n"
        f"🔗 <b>Provider Endpoint:</b> <code>{provider_url}</code>\n"
        f"🔑 <b>Current API Key:</b> <code>{EMAILABLE_API_KEY}</code>\n"
        f"📌 <b>Validator Status:</b> {val_status_str}\n\n"
        f"Use the buttons below to configure the email validator:"
    )
    
    await message.answer(text, parse_mode=ParseMode.HTML, reply_markup=get_validator_admin_inline_keyboard())

@dp.callback_query(F.data == "admin_validator_toggle_status")
async def cb_admin_validator_toggle_status(call: CallbackQuery):
    await call.answer("Validator status updated!", show_alert=True)
    global VALIDATOR_ENABLED
    VALIDATOR_ENABLED = not VALIDATOR_ENABLED
    new_val = 'on' if VALIDATOR_ENABLED else 'off'

    async with db_pool.acquire() as conn:
        await conn.execute("INSERT INTO bot_settings (key, value) VALUES ('validator_enabled', $1) ON CONFLICT (key) DO UPDATE SET value = $1", new_val)

    status_text = "🟢 <b>Active</b>" if VALIDATOR_ENABLED else "🔴 <b>Deactivated</b>"
    provider_name = "Emailable" if VALIDATOR_PROVIDER == "emailable" else "MyEmailVerifier"
    provider_url = get_provider_url()

    text = (
        f"⚙️ <b>Gmail Validator Management</b>\n\n"
        f"🌐 <b>Current Provider:</b> <code>{provider_name}</code>\n"
        f"🔗 <b>Provider Endpoint:</b> <code>{provider_url}</code>\n"
        f"🔑 <b>Current API Key:</b> <code>{EMAILABLE_API_KEY}</code>\n"
        f"📌 <b>Validator Status:</b> {status_text}\n\n"
        f"Use the buttons below to configure the email validator:"
    )

    try:
        await call.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=get_validator_admin_inline_keyboard())
    except Exception:
        pass

@dp.callback_query(F.data == "admin_validator_change_provider")
async def cb_admin_validator_change_provider(call: CallbackQuery):
    global VALIDATOR_PROVIDER
    VALIDATOR_PROVIDER = "emailable" if VALIDATOR_PROVIDER == "myemailverifier" else "myemailverifier"

    async with db_pool.acquire() as conn:
        await conn.execute("INSERT INTO bot_settings (key, value) VALUES ('validator_provider', $1) ON CONFLICT (key) DO UPDATE SET value = $1", VALIDATOR_PROVIDER)

    status_text = "🟢 <b>Active</b>" if VALIDATOR_ENABLED else "🔴 <b>Deactivated</b>"
    provider_name = "Emailable" if VALIDATOR_PROVIDER == "emailable" else "MyEmailVerifier"
    provider_url = get_provider_url()

    await call.answer(f"Switched provider to {provider_name}!", show_alert=True)

    text = (
        f"⚙️ <b>Gmail Validator Management</b>\n\n"
        f"🌐 <b>Current Provider:</b> <code>{provider_name}</code>\n"
        f"🔗 <b>Provider Endpoint:</b> <code>{provider_url}</code>\n"
        f"🔑 <b>Current API Key:</b> <code>{EMAILABLE_API_KEY}</code>\n"
        f"📌 <b>Validator Status:</b> {status_text}\n\n"
        f"Use the buttons below to configure the email validator:"
    )

    try:
        await call.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=get_validator_admin_inline_keyboard())
    except Exception:
        pass

@dp.callback_query(F.data == "admin_validator_change_key")
async def cb_admin_validator_change_key(call: CallbackQuery, state: FSMContext):
    await call.answer()
    provider_name = "Emailable" if VALIDATOR_PROVIDER == "emailable" else "MyEmailVerifier"

    await state.set_state(AdminState.waiting_for_validator_key)
    await call.message.answer(
        f"🔑 <b>Send the new API key for {provider_name}:</b>",
        parse_mode=ParseMode.HTML
    )

@dp.message(AdminState.waiting_for_validator_key, ~F.text.startswith("/"), ~F.text.in_(MENU_BUTTONS))
async def process_change_validator_key(message: Message, state: FSMContext):
    global EMAILABLE_API_KEY
    new_key = message.text.strip()

    EMAILABLE_API_KEY = new_key

    async with db_pool.acquire() as conn:
        await conn.execute("INSERT INTO bot_settings (key, value) VALUES ('emailable_api_key', $1) ON CONFLICT (key) DO UPDATE SET value = $1", new_key)

    provider_name = "Emailable" if VALIDATOR_PROVIDER == "emailable" else "MyEmailVerifier"

    await message.answer(
        f"✅ <b>{provider_name} API Key Updated Successfully!</b>\n\n"
        f"🔑 <b>New Key:</b> <code>{new_key}</code>",
        parse_mode=ParseMode.HTML,
        reply_markup=get_admin_menu_keyboard()
    )
    await state.clear()

@dp.message(F.text == "👑 Transfer Admin", StateFilter("*"))
async def admin_btn_transfer_admin(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.set_state(AdminState.waiting_for_transfer_admin_id)
    await message.answer(
        "👑 <b>Transfer Admin Privileges</b>\n\n"
        "Send the numeric <b>User ID</b> of the user you want to transfer full adminship to:\n\n"
        "<i>⚠️ Warning: Once transferred, your current user ID will no longer have access to the admin panel!</i>",
        parse_mode=ParseMode.HTML
    )

@dp.message(AdminState.waiting_for_transfer_admin_id, ~F.text.startswith("/"), ~F.text.in_(MENU_BUTTONS))
async def process_transfer_admin_id_step(message: Message, state: FSMContext):
    global ADMIN_ID
    try:
        new_admin_id = int(message.text.strip())
        if new_admin_id == message.from_user.id:
            await message.answer("❌ You are already the admin!", reply_markup=get_admin_menu_keyboard())
            await state.clear()
            return

        await ensure_user(new_admin_id)

        async with db_pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO bot_settings (key, value) VALUES ('admin_id', $1) ON CONFLICT (key) DO UPDATE SET value = $1",
                str(new_admin_id)
            )

        old_admin_id = ADMIN_ID
        ADMIN_ID = new_admin_id

        await message.answer(
            f"👑 <b>Adminship Successfully Transferred!</b>\n\n"
            f"<b>New Admin ID:</b> <code>{new_admin_id}</code>\n"
            f"You have been demoted to a regular user.",
            parse_mode=ParseMode.HTML,
            reply_markup=get_main_menu_keyboard()
        )

        asyncio.create_task(send_user_notification(
            new_admin_id,
            f"👑 <b>Congratulations!</b>\n\nYou have been promoted to the <b>Full Admin</b> of Gmail Earnex by User ID <code>{old_admin_id}</code>.\n\nUse /adminpanel to open the control panel.",
            parse_mode=ParseMode.HTML
        ))

    except ValueError:
        await message.answer("❌ Invalid User ID. Please enter a valid numeric Telegram ID.", reply_markup=get_admin_menu_keyboard())

    await state.clear()

@dp.message(F.text == "➕ Add Task", StateFilter("*"))
async def admin_btn_add_task(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.clear()
    await message.answer(
        "➕ <b>Add Tasks Options</b>\n\n"
        "Choose an option below:\n"
        "• <b>Single Add:</b> Add a single email username.\n"
        "• <b>Bulk Add:</b> Add multiple email usernames at once.",
        parse_mode=ParseMode.HTML,
        reply_markup=get_add_task_type_keyboard()
    )

@dp.callback_query(F.data == "admin_add_task_single")
async def cb_admin_add_task_single(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.set_state(AdminState.waiting_for_add_task)
    await call.message.answer("📧 Send the email/username to add as a task (e.g. <code>example@gmail.com</code>):", parse_mode=ParseMode.HTML)

@dp.callback_query(F.data == "admin_add_task_bulk")
async def cb_admin_add_task_bulk(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.set_state(AdminState.waiting_for_bulk_add_task)
    await call.message.answer(
        "📦 <b>Bulk Task Addition</b>\n\n"
        "Send the list of email usernames separated by line breaks, spaces, or commas:\n\n"
        "<i>Example:</i>\n"
        "<code>john</code>\n"
        "<code>adarsh</code>\n"
        "<code>mayank</code>",
        parse_mode=ParseMode.HTML
    )

@dp.message(AdminState.waiting_for_bulk_add_task, ~F.text.startswith("/"), ~F.text.in_(MENU_BUTTONS))
async def process_bulk_add_task_step(message: Message, state: FSMContext):
    raw_text = message.text.strip()
    raw_lines = re.split(r'[\n,\s]+', raw_text)
    
    usernames = []
    for line in raw_lines:
        item = line.strip()
        if item:
            formatted_email = item if "@" in item else f"{item}@gmail.com"
            if formatted_email.lower() not in usernames:
                usernames.append(formatted_email.lower())

    if not usernames:
        await message.answer("❌ No valid usernames found. Please try again.", reply_markup=get_admin_menu_keyboard())
        await state.clear()
        return

    total_items = len(usernames)
    added_count = 0
    skipped_count = 0

    status_msg = await message.answer(f"⏳ <b>Adding tasks in bulk... 0/{total_items}</b>", parse_mode=ParseMode.HTML)

    async with db_pool.acquire() as conn:
        for idx, email in enumerate(usernames, start=1):
            search_pattern = f"%{email}%"
            existing = await conn.fetchval(
                "SELECT id FROM tasks WHERE LOWER(title) LIKE $1 OR LOWER(details) LIKE $1 LIMIT 1",
                search_pattern
            )
            
            if not existing:
                password = DEFAULT_TASK_PASS
                default_reward = DEFAULT_TASK_RATE
                title = f"Login to {email}"
                details = f"Email: {email} | Pass: {password}"
                
                await conn.execute(
                    "INSERT INTO tasks (title, details, reward) VALUES ($1, $2, $3)",
                    title, details, default_reward
                )
                added_count += 1
            else:
                skipped_count += 1

            if idx % 5 == 0 or idx == total_items:
                try:
                    await status_msg.edit_text(f"⏳ <b>Bulk Adding Progress: {idx}/{total_items}</b>", parse_mode=ParseMode.HTML)
                except Exception:
                    pass

    await status_msg.edit_text(
        f"✅ <b>Bulk Task Addition Completed!</b>\n\n"
        f"📊 <b>Total Processed:</b> {total_items}\n"
        f"🟢 <b>Successfully Added:</b> {added_count}\n"
        f"⚠️ <b>Skipped (Duplicates):</b> {skipped_count}",
        parse_mode=ParseMode.HTML,
        reply_markup=get_admin_menu_keyboard()
    )
    await state.clear()

@dp.message(AdminState.waiting_for_add_task, ~F.text.startswith("/"), ~F.text.in_(MENU_BUTTONS))
async def process_add_task_step(message: Message, state: FSMContext):
    username_input = message.text.strip()
    username = f"{username_input}@gmail.com" if "@" not in username_input else username_input
    
    search_pattern = f"%{username.lower()}%"

    async with db_pool.acquire() as conn:
        existing_task = await conn.fetchrow(
            "SELECT id, status FROM tasks WHERE LOWER(title) LIKE $1 OR LOWER(details) LIKE $1 LIMIT 1",
            search_pattern
        )

    if existing_task:
        await state.update_data(pending_add_username=username)
        kb = InlineKeyboardBuilder()
        kb.button(
            text="Confirm", 
            callback_data="confirm_add_duplicate_task", 
            icon_custom_emoji_id="6217663806110175239", 
            style="success"
        )
        kb.button(
            text="Back", 
            callback_data="cancel_add_duplicate_task", 
            icon_custom_emoji_id="5352759161945867747", 
            style="danger"
        )
        kb.adjust(2)

        await message.answer(
            f'<tg-emoji emoji-id="5447644880824181073">⚠️</tg-emoji> <b>This task (<code>{username}</code>) already exists in database!</b>\n\n'
            f'Would you like to add it anyway?',
            parse_mode=ParseMode.HTML,
            reply_markup=kb.as_markup()
        )
        return

    await insert_new_task(message, username)
    await state.clear()

async def insert_new_task(message: Message, username: str):
    password = DEFAULT_TASK_PASS
    default_reward = DEFAULT_TASK_RATE
    title = f"Login to {username}"
    details = f"Email: {username} | Pass: {password}"
    
    async with db_pool.acquire() as conn:
        task_id = await conn.fetchval(
            "INSERT INTO tasks (title, details, reward) VALUES ($1, $2, $3) RETURNING id",
            title, details, default_reward
        )
        
    await message.answer(
        f'<tg-emoji emoji-id="6217663806110175239">✅</tg-emoji> <b>Task Added Successfully!</b>\n\n'
        f'<tg-emoji emoji-id="5197269100878907942">✍️</tg-emoji> <b>Task ID:</b> <code>#{task_id}</code>\n'
        f'<tg-emoji emoji-id="5870458774455587120">👤</tg-emoji> <b>Email:</b> <code>{username}</code>\n'
        f'<tg-emoji emoji-id="6005570495603282482">🔑</tg-emoji> <b>Password:</b> <code>{password}</code>\n'
        f'<tg-emoji emoji-id="5417924076503062111">💰</tg-emoji> <b>Reward:</b> ₹{default_reward}', 
        parse_mode=ParseMode.HTML,
        reply_markup=get_admin_menu_keyboard()
    )

@dp.callback_query(F.data == "confirm_add_duplicate_task")
async def cb_confirm_add_duplicate_task(call: CallbackQuery, state: FSMContext):
    await call.answer()
    data = await state.get_data()
    username = data.get("pending_add_username")
    if username:
        try:
            await call.message.delete()
        except Exception:
            pass
        await insert_new_task(call.message, username)
    await state.clear()

@dp.callback_query(F.data == "cancel_add_duplicate_task")
async def cb_cancel_add_duplicate_task(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.clear()
    try:
        await call.message.edit_text("❌ Task addition cancelled.", reply_markup=None)
    except Exception:
        pass
    await call.message.answer("🏠 Returned to Admin Menu.", reply_markup=get_admin_menu_keyboard())

@dp.message(F.text == "📥 Pending Reviews", StateFilter("*"))
async def admin_btn_pending_reviews(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.clear()
        
    async with db_pool.acquire() as conn:
        task_count = await conn.fetchval("SELECT COUNT(*) FROM tasks WHERE status = 'pending_review'") or 0
        sell_count = await conn.fetchval("SELECT COUNT(*) FROM pending_sells WHERE status = 'pending_review'") or 0

    total_pending = task_count + sell_count
        
    if total_pending == 0:
        await message.answer("📭 <b>No pending reviews (tasks or sell requests) found!</b>", parse_mode=ParseMode.HTML, reply_markup=get_admin_menu_keyboard())
        return

    text = (
        f"📥 <b>Pending Reviews Dashboard</b>\n\n"
        f"📨 <b>Pending Sell Gmail:</b> <code>{sell_count}</code>\n"
        f"✍️ <b>Pending Task Gmail:</b> <code>{task_count}</code>\n\n"
        f"Click an option below to view requests:"
    )

    await message.answer(
        text, 
        parse_mode=ParseMode.HTML, 
        reply_markup=get_pending_reviews_inline_keyboard()
    )

@dp.callback_query(F.data == "admin_view_pending_sells")
async def cb_admin_view_pending_sells(call: CallbackQuery):
    await call.answer()
    if call.from_user.id != ADMIN_ID:
        return

    async with db_pool.acquire() as conn:
        sell_rows = await conn.fetch('''
            SELECT id, user_id, details, amount, claimed_by 
            FROM pending_sells 
            WHERE status = 'pending_review'
            ORDER BY created_at ASC
        ''')

    if not sell_rows:
        try:
            await call.message.edit_text("📭 <b>No pending sell Gmail requests found!</b>", parse_mode=ParseMode.HTML)
        except Exception:
            await call.message.answer("📭 <b>No pending sell Gmail requests found!</b>", parse_mode=ParseMode.HTML)
        return

    await call.message.answer(f"📨 <b>Displaying {len(sell_rows)} pending Gmail sell request(s):</b>", parse_mode=ParseMode.HTML)

    for r in sell_rows:
        sell_id = r['id']
        user_id = r['user_id']
        details = r['details']
        amount = r['amount']
        claimed_by = r['claimed_by']

        try:
            lines = details.split("\n")
            username = lines[0].replace("Username: ", "").strip()
            password = lines[1].replace("Password: ", "").strip()
            
            if "@gmail.com" not in username.lower() and "@" not in username:
                username += "@gmail.com"
                
            formatted_details = f"📧 <b>Username:</b> <code>{username}</code>\n<tg-emoji emoji-id=\"6005570495603282482\">🔑</tg-emoji> <b>Password:</b> <code>{password}</code>"
        except Exception:
            formatted_details = f"<code>{details}</code>"

        claimed_str = f"\n👷 <b>Claimed By Worker:</b> <code>{claimed_by}</code>" if claimed_by else "\n📦 <b>Status:</b> 🟢 Unclaimed Stock"

        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="Approve", callback_data=f"sa:{sell_id}", icon_custom_emoji_id="6217663806110175239", style="success"),
            InlineKeyboardButton(text="Decline", callback_data=f"sd:{sell_id}", icon_custom_emoji_id="5274099962655816924", style="danger")
        ]])

        await call.message.answer(
            f'<tg-emoji emoji-id="5377548235709619284">📦</tg-emoji> <b>Pending Gmail Sell Request #{sell_id}</b>\n\n'
            f'<tg-emoji emoji-id="5870458774455587120">👤</tg-emoji> <b>User ID:</b> <code>{user_id}</code>\n'
            f'<tg-emoji emoji-id="5417924076503062111">💰</tg-emoji> <b>Rate:</b> ₹{amount:.2f}'
            f'{claimed_str}\n\n'
            f'📝 <b>Details:</b>\n{formatted_details}',
            reply_markup=kb,
            parse_mode=ParseMode.HTML
        )

@dp.callback_query(F.data == "admin_view_pending_tasks")
async def cb_admin_view_pending_tasks(call: CallbackQuery):
    await call.answer()
    if call.from_user.id != ADMIN_ID:
        return

    async with db_pool.acquire() as conn:
        task_rows = await conn.fetch('''
            SELECT t.id, t.title, t.details, t.reward, ta.user_id 
            FROM tasks t 
            JOIN task_assignments ta ON t.id = ta.task_id 
            WHERE t.status = 'pending_review'
            ORDER BY ta.assigned_at ASC
        ''')

    if not task_rows:
        try:
            await call.message.edit_text("📭 <b>No pending task submissions found!</b>", parse_mode=ParseMode.HTML)
        except Exception:
            await call.message.answer("📭 <b>No pending task submissions found!</b>", parse_mode=ParseMode.HTML)
        return

    await call.message.answer(f"✍️ <b>Displaying {len(task_rows)} pending task submission(s):</b>", parse_mode=ParseMode.HTML)

    for r in task_rows:
        task_id = r['id']
        title = r['title']
        reward = r['reward']
        user_id = r['user_id']
        details = r['details']

        try:
            parts = details.split(" | ")
            email = parts[0].replace("Email: ", "").strip()
            password = parts[1].replace("Pass: ", "").strip()
        except Exception:
            email = title.replace("Login to ", "").strip()
            password = DEFAULT_TASK_PASS
        
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text='Approve', callback_data=f'ta:{task_id}', icon_custom_emoji_id="6217663806110175239", style="success"),
            InlineKeyboardButton(text='Decline', callback_data=f'td:{task_id}', icon_custom_emoji_id="5274099962655816924", style="danger")
        ]])
        
        await call.message.answer(
            f'<tg-emoji emoji-id="5206607081334906820">📤</tg-emoji> <b>Pending Task Submission</b>\n\n'
            f'<tg-emoji emoji-id="5870458774455587120">👤</tg-emoji> <b>User ID:</b> <code>{user_id}</code>\n'
            f'<tg-emoji emoji-id="5197269100878907942">✍️</tg-emoji> <b>Task #{task_id}</b>\n'
            f'📧 <b>Email:</b> <code>{email}</code>\n'
            f'<tg-emoji emoji-id="6005570495603282482">🔑</tg-emoji> <b>Password:</b> <code>{password}</code>\n'
            f'<tg-emoji emoji-id="5417924076503062111">💰</tg-emoji> <b>Reward:</b> ₹{reward}',
            reply_markup=kb,
            parse_mode=ParseMode.HTML
        )

@dp.message(F.text == "💸 Pending Withdrawals", StateFilter("*"))
async def admin_btn_pending_withdrawals(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.clear()

    async with db_pool.acquire() as conn:
        upi_count = await conn.fetchval("SELECT COUNT(*) FROM withdrawals WHERE status = 'pending' AND method ILIKE '%UPI%'") or 0
        usdt_count = await conn.fetchval("SELECT COUNT(*) FROM withdrawals WHERE status = 'pending' AND method ILIKE '%USDT%'") or 0
        ultra_count = await conn.fetchval("SELECT COUNT(*) FROM withdrawals WHERE status = 'pending' AND method ILIKE '%Ultra%'") or 0

    total_pending = upi_count + usdt_count + ultra_count

    if total_pending == 0:
        await message.answer("📭 <b>No pending withdrawal requests found!</b>", parse_mode=ParseMode.HTML, reply_markup=get_admin_menu_keyboard())
        return

    ultra_line = f"\n⚡️ <b>Ultra Gateway Pending:</b> <code>{ultra_count}</code>" if ULTRA_STATUS else ""

    text = (
        f"💸 <b>Pending Withdrawals Dashboard</b>\n\n"
        f"🏦 <b>UPI Pending:</b> <code>{upi_count}</code>\n"
        f"🪙 <b>USDT BEP-20 Pending:</b> <code>{usdt_count}</code>"
        f"{ultra_line}\n\n"
        f"Select a method below to review requests:"
    )

    await message.answer(
        text, 
        parse_mode=ParseMode.HTML, 
        reply_markup=get_pending_withdrawals_inline_keyboard()
    )

@dp.callback_query(F.data.startswith("admin_view_pending_withdraw_"))
async def cb_admin_view_pending_withdrawals(call: CallbackQuery):
    await call.answer()
    if call.from_user.id != ADMIN_ID:
        return

    method_key = call.data.replace("admin_view_pending_withdraw_", "").strip().lower()

    if method_key == "upi":
        target_pattern = "%UPI%"
        display_label = "UPI"
    elif method_key == "usdt":
        target_pattern = "%USDT%"
        display_label = "USDT BEP-20"
    else:
        target_pattern = "%Ultra%"
        display_label = "Ultra Gateway"

    async with db_pool.acquire() as conn:
        withdraw_rows = await conn.fetch('''
            SELECT id, user_id, amount, method, payment_address, created_at
            FROM withdrawals
            WHERE status = 'pending' AND method ILIKE $1
            ORDER BY created_at ASC
        ''', target_pattern)

    if not withdraw_rows:
        try:
            await call.message.edit_text(f"📭 <b>No pending withdrawal requests for {display_label}!</b>", parse_mode=ParseMode.HTML)
        except Exception:
            await call.message.answer(f"📭 <b>No pending withdrawal requests for {display_label}!</b>", parse_mode=ParseMode.HTML)
        return

    await call.message.answer(f"💸 <b>Displaying {len(withdraw_rows)} pending withdrawal request(s) for {display_label}:</b>", parse_mode=ParseMode.HTML)

    for r in withdraw_rows:
        withdraw_id = r['id']
        user_id = r['user_id']
        amount = r['amount']
        method = r['method'] or display_label
        payment_address = r['payment_address'] or 'None'
        
        extra_usdt_info = f" (~${(amount / USD_TO_INR):.2f} USDT)" if "usdt" in method.lower() else ""

        kb = InlineKeyboardBuilder()
        kb.button(
            text="Pay", 
            callback_data=f"wp:{withdraw_id}", 
            icon_custom_emoji_id="5444856076954520455", 
            style="success"
        )
        kb.button(
            text="Reject", 
            callback_data=f"wr:{withdraw_id}", 
            icon_custom_emoji_id="5274099962655816924", 
            style="danger"
        )
        kb.adjust(2)

        address_emoji = '<tg-emoji emoji-id="6152069549442208798">🏦</tg-emoji>' if "upi" in method.lower() else ('<tg-emoji emoji-id="5197434882321567830">🪙</tg-emoji>' if "usdt" in method.lower() else '<tg-emoji emoji-id="5195033767969839232">⚡️</tg-emoji>')

        await call.message.answer(
            f'<tg-emoji emoji-id="5417924076503062111">💰</tg-emoji> <b>WITHDRAWAL REQUEST #{withdraw_id}</b>\n\n'
            f'<tg-emoji emoji-id="5197269100878907942">✍️</tg-emoji> <b>User ID:</b> <code>{user_id}</code>\n'
            f'💳 <b>Method:</b> <code>{method}</code>\n'
            f'<tg-emoji emoji-id="5417924076503062111">💰</tg-emoji> <b>Amount:</b> ₹{amount:.2f}{extra_usdt_info}\n'
            f'{address_emoji} <b>Address:</b> <code>{payment_address}</code>\n'
            f'📅 <b>Date:</b> {r["created_at"].strftime("%Y-%m-%d %H:%M:%S")}',
            reply_markup=kb.as_markup(),
            parse_mode=ParseMode.HTML
        )

@dp.message(F.text == "💬 Chat", StateFilter("*"))
async def admin_btn_chat(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.set_state(AdminState.waiting_for_chat_user_id)
    await message.answer("💬 Send the numeric **User ID** you want to message:", parse_mode=ParseMode.MARKDOWN)

@dp.message(AdminState.waiting_for_chat_user_id, ~F.text.startswith("/"), ~F.text.in_(MENU_BUTTONS))
async def process_chat_user_id_step(message: Message, state: FSMContext):
    try:
        target_user_id = int(message.text.strip())
        await state.update_data(chat_target_user_id=target_user_id)
        await state.set_state(AdminState.waiting_for_chat_message)
        await message.answer(f"✉️ **Now send the message you want to deliver to User `{target_user_id}`:**", parse_mode=ParseMode.MARKDOWN)
    except ValueError:
        await message.answer("❌ Invalid User ID. Please enter a valid numeric Telegram User ID.", reply_markup=get_admin_menu_keyboard())
        await state.clear()

@dp.message(AdminState.waiting_for_chat_message, ~F.text.startswith("/") if F.text else True, ~F.text.in_(MENU_BUTTONS))
async def process_chat_message_step(message: Message, state: FSMContext):
    data = await state.get_data()
    target_user_id = data.get('chat_target_user_id')

    if not target_user_id:
        await message.answer("❌ Error: Target user lost.", reply_markup=get_admin_menu_keyboard())
        await state.clear()
        return

    try:
        await bot.copy_message(
            chat_id=target_user_id,
            from_chat_id=message.chat.id,
            message_id=message.message_id
        )
        await message.answer(f"✅ **Message successfully sent to User `{target_user_id}`!**", parse_mode=ParseMode.MARKDOWN, reply_markup=get_admin_menu_keyboard())
    except Exception as e:
        await message.answer(f"❌ Failed to send message to User `{target_user_id}`.\n\nError: `{e}`", parse_mode=ParseMode.MARKDOWN, reply_markup=get_admin_menu_keyboard())

    await state.clear()

# ============================================
# CANCEL SELL & CANCEL TASK SUB-MENU SYSTEM
# ============================================

@dp.message(F.text == "🚫 Cancel Sell", StateFilter("*"))
async def admin_btn_cancel_sell(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.clear()
    
    async with db_pool.acquire() as conn:
        count = await conn.fetchval("SELECT COUNT(*) FROM pending_sells WHERE status = 'pending_review'")
        
    if not count:
        await message.answer("📭 <b>No pending Gmail sell requests found to cancel.</b>", parse_mode=ParseMode.HTML, reply_markup=get_admin_menu_keyboard())
        return

    await message.answer(
        f"🚫 <b>Cancel Sell Gmail Dashboard</b>\n\n"
        f"Currently <b>{count}</b> pending Gmail sell request(s).\n"
        f"Choose an option below:",
        parse_mode=ParseMode.HTML,
        reply_markup=get_cancel_sell_options_keyboard()
    )

@dp.callback_query(F.data == "admin_cancel_sell_all")
async def cb_admin_cancel_sell_all(call: CallbackQuery, state: FSMContext):
    await call.answer()
    if call.from_user.id != ADMIN_ID:
        return
    await state.set_state(AdminState.waiting_for_bulk_cancel_sell_reason)
    await call.message.answer(
        "🚫 <b>Cancel All Pending Sell Gmail</b>\n\n"
        "Send the single rejection reason message to send to all affected users below:",
        parse_mode=ParseMode.HTML
    )

@dp.callback_query(F.data == "admin_cancel_sell_by_id")
async def cb_admin_cancel_sell_by_id(call: CallbackQuery, state: FSMContext):
    await call.answer()
    if call.from_user.id != ADMIN_ID:
        return
    await state.set_state(AdminState.waiting_for_cancel_sell_by_id_target)
    await call.message.answer(
        "🚫 <b>Cancel Sell Gmail By ID</b>\n\n"
        "Send the target <b>Sell Request ID</b> (e.g., <code>100</code>).\n"
        "<i>Note: Request #100 and all pending sell requests prior to #100 will be cancelled!</i>",
        parse_mode=ParseMode.HTML
    )

@dp.message(AdminState.waiting_for_cancel_sell_by_id_target, ~F.text.startswith("/"), ~F.text.in_(MENU_BUTTONS))
async def process_cancel_sell_by_id_target_step(message: Message, state: FSMContext):
    try:
        target_id = int(message.text.strip())
        await state.update_data(target_sell_id=target_id)
        await state.set_state(AdminState.waiting_for_cancel_sell_by_id_reason)
        await message.answer(
            f"🚫 Target Sell ID set to <code>#{target_id}</code>.\n\n"
            f"Now send the single rejection reason message to send to all affected users:",
            parse_mode=ParseMode.HTML
        )
    except ValueError:
        await message.answer("❌ Invalid Sell ID. Please enter a valid number.", reply_markup=get_admin_menu_keyboard())
        await state.clear()

@dp.message(AdminState.waiting_for_cancel_sell_by_id_reason, ~F.text.startswith("/"), ~F.text.in_(MENU_BUTTONS))
async def process_cancel_sell_by_id_reason_step(message: Message, state: FSMContext):
    data = await state.get_data()
    target_id = data.get('target_sell_id')
    reason = message.text.strip()

    if not target_id:
        await message.answer("❌ Error: Target Sell ID lost.", reply_markup=get_admin_menu_keyboard())
        await state.clear()
        return

    async with db_pool.acquire() as conn:
        affected_sells = await conn.fetch(
            "SELECT id, user_id FROM pending_sells WHERE status = 'pending_review' AND id <= $1",
            target_id
        )

        if not affected_sells:
            await message.answer(f"📭 No pending sell requests found with ID <= <code>#{target_id}</code>.", parse_mode=ParseMode.HTML, reply_markup=get_admin_menu_keyboard())
            await state.clear()
            return

        affected_ids = [r['id'] for r in affected_sells]
        await conn.execute("UPDATE pending_sells SET status = 'declined' WHERE id = ANY($1::int[])", affected_ids)

    count = len(affected_sells)
    await message.answer(f"✅ <b>Successfully cancelled {count} pending Gmail sell request(s) up to #{target_id} and notified users!</b>", parse_mode=ParseMode.HTML, reply_markup=get_admin_menu_keyboard())

    for r in affected_sells:
        sell_id = r['id']
        uid = r['user_id']
        asyncio.create_task(send_user_notification(
            uid,
            f'<tg-emoji emoji-id="5447644880824181073">⚠️</tg-emoji> <b>Your sell request #{sell_id} was declined.</b>\n\n<tg-emoji emoji-id="4956475826762679249">💬</tg-emoji> <b>Reason:</b> {reason}',
            parse_mode=ParseMode.HTML
        ))

    await state.clear()

@dp.message(AdminState.waiting_for_bulk_cancel_sell_reason, ~F.text.startswith("/"), ~F.text.in_(MENU_BUTTONS))
async def process_bulk_cancel_sell_reason_step(message: Message, state: FSMContext):
    reason = message.text.strip()
    
    async with db_pool.acquire() as conn:
        pending_sells = await conn.fetch("SELECT id, user_id FROM pending_sells WHERE status = 'pending_review'")
        
        if not pending_sells:
            await message.answer("📭 No pending sell requests found.", reply_markup=get_admin_menu_keyboard())
            await state.clear()
            return

        await conn.execute("UPDATE pending_sells SET status = 'declined' WHERE status = 'pending_review'")

    count = len(pending_sells)
    await message.answer(f"✅ <b>Successfully cancelled {count} pending Gmail sell requests and notified users!</b>", parse_mode=ParseMode.HTML, reply_markup=get_admin_menu_keyboard())

    for r in pending_sells:
        sell_id = r['id']
        uid = r['user_id']
        asyncio.create_task(send_user_notification(
            uid,
            f'<tg-emoji emoji-id="5447644880824181073">⚠️</tg-emoji> <b>Your sell request #{sell_id} was declined.</b>\n\n<tg-emoji emoji-id="4956475826762679249">💬</tg-emoji> <b>Reason:</b> {reason}',
            parse_mode=ParseMode.HTML
        ))

    await state.clear()

@dp.message(F.text == "🚫 Cancel Task", StateFilter("*"))
async def admin_btn_cancel_task(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.clear()

    async with db_pool.acquire() as conn:
        count = await conn.fetchval("SELECT COUNT(*) FROM tasks WHERE status = 'pending_review'")

    if not count:
        await message.answer("📭 <b>No pending task submissions found to cancel.</b>", parse_mode=ParseMode.HTML, reply_markup=get_admin_menu_keyboard())
        return

    await message.answer(
        f"🚫 <b>Cancel Pending Tasks Dashboard</b>\n\n"
        f"Currently <b>{count}</b> pending task submission(s).\n"
        f"Choose an option below:",
        parse_mode=ParseMode.HTML,
        reply_markup=get_cancel_task_options_keyboard()
    )

@dp.callback_query(F.data == "admin_cancel_task_all")
async def cb_admin_cancel_task_all(call: CallbackQuery, state: FSMContext):
    await call.answer()
    if call.from_user.id != ADMIN_ID:
        return
    await state.set_state(AdminState.waiting_for_bulk_cancel_task_reason)
    await call.message.answer(
        "🚫 <b>Cancel All Pending Tasks</b>\n\n"
        "Send the single rejection reason message to send to all affected users below:",
        parse_mode=ParseMode.HTML
    )

@dp.callback_query(F.data == "admin_cancel_task_by_id")
async def cb_admin_cancel_task_by_id(call: CallbackQuery, state: FSMContext):
    await call.answer()
    if call.from_user.id != ADMIN_ID:
        return
    await state.set_state(AdminState.waiting_for_cancel_task_by_id_target)
    await call.message.answer(
        "🚫 <b>Cancel Task Submissions By ID</b>\n\n"
        "Send the target <b>Task ID</b> (e.g., <code>100</code>).\n"
        "<i>Note: Task #100 and all pending task submissions prior to #100 will be cancelled!</i>",
        parse_mode=ParseMode.HTML
    )

@dp.message(AdminState.waiting_for_cancel_task_by_id_target, ~F.text.startswith("/"), ~F.text.in_(MENU_BUTTONS))
async def process_cancel_task_by_id_target_step(message: Message, state: FSMContext):
    try:
        target_id = int(message.text.strip())
        await state.update_data(target_task_id=target_id)
        await state.set_state(AdminState.waiting_for_cancel_task_by_id_reason)
        await message.answer(
            f"🚫 Target Task ID set to <code>#{target_id}</code>.\n\n"
            f"Now send the single rejection reason message to send to all affected users:",
            parse_mode=ParseMode.HTML
        )
    except ValueError:
        await message.answer("❌ Invalid Task ID. Please enter a valid number.", reply_markup=get_admin_menu_keyboard())
        await state.clear()

@dp.message(AdminState.waiting_for_cancel_task_by_id_reason, ~F.text.startswith("/"), ~F.text.in_(MENU_BUTTONS))
async def process_cancel_task_by_id_reason_step(message: Message, state: FSMContext):
    data = await state.get_data()
    target_id = data.get('target_task_id')
    reason = message.text.strip()

    if not target_id:
        await message.answer("❌ Error: Target Task ID lost.", reply_markup=get_admin_menu_keyboard())
        await state.clear()
        return

    async with db_pool.acquire() as conn:
        affected_tasks = await conn.fetch('''
            SELECT t.id as task_id, ta.user_id 
            FROM tasks t
            JOIN task_assignments ta ON t.id = ta.task_id
            WHERE t.status = 'pending_review' AND t.id <= $1
        ''', target_id)

        if not affected_tasks:
            await message.answer(f"📭 No pending task submissions found with ID <= <code>#{target_id}</code>.", parse_mode=ParseMode.HTML, reply_markup=get_admin_menu_keyboard())
            await state.clear()
            return

        affected_task_ids = [r['task_id'] for r in affected_tasks]

        async with conn.transaction():
            await conn.execute("DELETE FROM task_assignments WHERE task_id = ANY($1::int[])", affected_task_ids)
            await conn.execute("UPDATE tasks SET status = 'available' WHERE id = ANY($1::int[])", affected_task_ids)

    count = len(affected_tasks)
    await message.answer(f"✅ <b>Successfully cancelled {count} pending tasks up to #{target_id}, returned them to pool, and notified users!</b>", parse_mode=ParseMode.HTML, reply_markup=get_admin_menu_keyboard())

    for r in affected_tasks:
        tid = r['task_id']
        uid = r['user_id']
        asyncio.create_task(send_user_notification(
            uid,
            f'<tg-emoji emoji-id="5447644880824181073">⚠️</tg-emoji> <b>Your submission for Task #{tid} was declined.</b>\n\n<tg-emoji emoji-id="4956475826762679249">💬</tg-emoji> <b>Reason:</b> {reason}\n\n<tg-emoji emoji-id="5251203410396458957">🛡</tg-emoji> The task has been returned to the pool.',
            parse_mode=ParseMode.HTML
        ))

    await state.clear()

@dp.message(AdminState.waiting_for_bulk_cancel_task_reason, ~F.text.startswith("/"), ~F.text.in_(MENU_BUTTONS))
async def process_bulk_cancel_task_reason_step(message: Message, state: FSMContext):
    reason = message.text.strip()

    async with db_pool.acquire() as conn:
        pending_tasks = await conn.fetch('''
            SELECT t.id as task_id, ta.user_id 
            FROM tasks t
            JOIN task_assignments ta ON t.id = ta.task_id
            WHERE t.status = 'pending_review'
        ''')

        if not pending_tasks:
            await message.answer("📭 No pending task submissions found.", reply_markup=get_admin_menu_keyboard())
            await state.clear()
            return

        task_ids = [r['task_id'] for r in pending_tasks]

        async with conn.transaction():
            await conn.execute("DELETE FROM task_assignments WHERE task_id = ANY($1::int[])", task_ids)
            await conn.execute("UPDATE tasks SET status = 'available' WHERE id = ANY($1::int[])", task_ids)

    count = len(pending_tasks)
    await message.answer(f"✅ <b>Successfully cancelled {count} pending tasks, returned them to pool, and notified users!</b>", parse_mode=ParseMode.HTML, reply_markup=get_admin_menu_keyboard())

    for r in pending_tasks:
        tid = r['task_id']
        uid = r['user_id']
        asyncio.create_task(send_user_notification(
            uid,
            f'<tg-emoji emoji-id="5447644880824181073">⚠️</tg-emoji> <b>Your submission for Task #{tid} was declined.</b>\n\n<tg-emoji emoji-id="4956475826762679249">💬</tg-emoji> <b>Reason:</b> {reason}\n\n<tg-emoji emoji-id="5251203410396458957">🛡</tg-emoji> The task has been returned to the pool.',
            parse_mode=ParseMode.HTML
        ))

    await state.clear()

@dp.message(F.text == "🗑 Unassign Tasks", StateFilter("*"))
async def admin_btn_unassign_tasks(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.clear()
    await message.answer(
        "🗑 <b>Unassign Active Tasks</b>\n\n"
        "Choose an option below:\n"
        "• <b>User ID:</b> Unassign current active task of a specific user.\n"
        "• <b>All Users:</b> Unassign all active tasks across all users and return them to the pool.",
        parse_mode=ParseMode.HTML,
        reply_markup=get_unassign_inline_keyboard()
    )

@dp.callback_query(F.data == "unassign_by_user_id")
async def start_unassign_user_id(call: CallbackQuery, state: FSMContext):
    await call.answer()
    if call.from_user.id != ADMIN_ID:
        return
    await state.set_state(AdminState.waiting_for_unassign_user_id)
    await call.message.answer("👤 Send the numeric **User ID** whose task you want to unassign:", parse_mode=ParseMode.MARKDOWN)

@dp.callback_query(F.data == "unassign_all_users")
async def start_unassign_all_users(call: CallbackQuery, state: FSMContext):
    await call.answer()
    if call.from_user.id != ADMIN_ID:
        return
    await state.clear()

    async with db_pool.acquire() as conn:
        active_assignments = await conn.fetch('''
            SELECT ta.task_id, ta.user_id, ta.message_id, t.title, t.details
            FROM task_assignments ta
            JOIN tasks t ON ta.task_id = t.id
            WHERE t.status != 'pending_review'
        ''')

        if not active_assignments:
            try:
                await call.message.edit_text("📭 <b>No active assigned tasks found to unassign.</b>", parse_mode=ParseMode.HTML)
            except Exception:
                await call.message.answer("📭 <b>No active assigned tasks found to unassign.</b>", parse_mode=ParseMode.HTML)
            return

        task_ids = [r['task_id'] for r in active_assignments]

        async with conn.transaction():
            await conn.execute("DELETE FROM task_assignments WHERE task_id = ANY($1::int[])", task_ids)
            for r in active_assignments:
                tid = r['task_id']
                if DEFAULT_TASK_PASS_STATUS:
                    try:
                        email_clean = r['details'].split(" | ")[0].replace("Email: ", "").strip()
                    except Exception:
                        email_clean = r['title'].replace("Login to ", "").strip()
                    reset_details = f"Email: {email_clean} | Pass: {DEFAULT_TASK_PASS}"
                    await conn.execute("UPDATE tasks SET status='available', details=$1 WHERE id=$2", reset_details, tid)
                else:
                    await conn.execute("UPDATE tasks SET status='available' WHERE id=$1", tid)

    count = len(task_ids)
    try:
        await call.message.edit_text(f"✅ <b>Successfully unassigned {count} active task(s) from all users, removed active task messages, and returned them to the pool.</b>", parse_mode=ParseMode.HTML)
    except Exception:
        await call.message.answer(f"✅ <b>Successfully unassigned {count} active task(s) from all users, removed active task messages, and returned them to the pool.</b>", parse_mode=ParseMode.HTML)

    for r in active_assignments:
        uid = r['user_id']
        mid = r['message_id']
        if mid:
            try:
                await bot.delete_message(chat_id=uid, message_id=mid)
            except Exception:
                pass

        asyncio.create_task(send_user_notification(
            uid,
            '⚠️ <b>Your active task has been unassigned by the admin and returned to the pool.</b>\n\nChoose an option from the menu below:',
            reply_markup=get_main_menu_keyboard(),
            parse_mode=ParseMode.HTML
        ))

@dp.message(AdminState.waiting_for_unassign_user_id, ~F.text.startswith("/"), ~F.text.in_(MENU_BUTTONS))
async def process_unassign_user_id_step(message: Message, state: FSMContext):
    try:
        target_id = int(message.text.strip())
        async with db_pool.acquire() as conn:
            assigned = await conn.fetchrow('''
                SELECT ta.task_id, ta.message_id, t.title, t.details 
                FROM task_assignments ta
                JOIN tasks t ON ta.task_id = t.id
                WHERE ta.user_id=$1
            ''', target_id)
            if not assigned:
                await message.answer(f"📭 User `{target_id}` does not have any active task assigned.", parse_mode=ParseMode.MARKDOWN, reply_markup=get_admin_menu_keyboard())
                await state.clear()
                return

            task_id = assigned['task_id']
            task_msg_id = assigned['message_id']

            async with conn.transaction():
                await conn.execute("DELETE FROM task_assignments WHERE user_id=$1", target_id)
                if DEFAULT_TASK_PASS_STATUS:
                    try:
                        email_clean = assigned['details'].split(" | ")[0].replace("Email: ", "").strip()
                    except Exception:
                        email_clean = assigned['title'].replace("Login to ", "").strip()
                    reset_details = f"Email: {email_clean} | Pass: {DEFAULT_TASK_PASS}"
                    await conn.execute("UPDATE tasks SET status='available', details=$1 WHERE id=$2", reset_details, task_id)
                else:
                    await conn.execute("UPDATE tasks SET status='available' WHERE id=$1", task_id)

        if task_msg_id:
            try:
                await bot.delete_message(chat_id=target_id, message_id=task_msg_id)
            except Exception:
                pass

        await message.answer(f"✅ **Successfully unassigned Task #{task_id} from User `{target_id}`, removed task message, and returned it to pool.**", parse_mode=ParseMode.MARKDOWN, reply_markup=get_admin_menu_keyboard())
        
        asyncio.create_task(send_user_notification(
            target_id,
            '⚠️ <b>Your active task has been unassigned by the admin and returned to the pool.</b>\n\nChoose an option from the menu below:',
            reply_markup=get_main_menu_keyboard(),
            parse_mode=ParseMode.HTML
        ))
    except ValueError:
        await message.answer("❌ Invalid User ID.", reply_markup=get_admin_menu_keyboard())
    await state.clear()

@dp.message(F.text == "🔍 Find ID", StateFilter("*"))
async def admin_btn_find_id(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.set_state(AdminState.waiting_for_find_id_query)
    await message.answer(
        "🔍 <b>Find Task & User ID</b>\n\n"
        "Please send the Gmail username or address (e.g., <code>john</code> or <code>john@gmail.com</code>):",
        parse_mode=ParseMode.HTML
    )

@dp.message(AdminState.waiting_for_find_id_query, ~F.text.startswith("/"), ~F.text.in_(MENU_BUTTONS))
async def process_find_id_query_step(message: Message, state: FSMContext):
    query = message.text.strip().lower()
    search_pattern = f"%{query}%"

    async with db_pool.acquire() as conn:
        task_match = await conn.fetchrow(
            "SELECT t.id, t.title, t.details, t.status, ta.user_id FROM tasks t LEFT JOIN task_assignments ta ON t.id = ta.task_id WHERE LOWER(t.title) LIKE $1 OR LOWER(t.details) LIKE $1 LIMIT 1",
            search_pattern
        )
        sell_match = await conn.fetchrow(
            "SELECT id, user_id, details, status, amount FROM pending_sells WHERE LOWER(details) LIKE $1 LIMIT 1",
            search_pattern
        )

    if not task_match and not sell_match:
        await message.answer(f"📭 <b>No records found matching:</b> <code>{query}</code>", parse_mode=ParseMode.HTML, reply_markup=get_admin_menu_keyboard())
        await state.clear()
        return

    text = f"🔍 <b>Search Results for:</b> <code>{query}</code>\n━━━━━━━━━━━━━━━━━━━━\n\n"
    
    target_task_id = None
    if task_match:
        target_task_id = task_match['id']
        assigned_u = task_match['user_id']
        if assigned_u:
            try:
                chat_info = await bot.get_chat(assigned_u)
                u_name = f"@{chat_info.username}" if chat_info.username else f"User {assigned_u}"
            except Exception:
                u_name = f"User {assigned_u}"
            assigned_str = f"{u_name} (<code>{assigned_u}</code>)"
        else:
            assigned_str = "<i>None (Unassigned)</i>"

        status_emoji = {
            'available': '🟢',
            'assigned': '🔵',
            'pending_review': '🟡',
            'completed': '✅'
        }.get(task_match['status'], '⚪️')

        text += (
            f"📋 <b>Task Record</b>\n"
            f"• 🆔 <b>Task ID:</b> <code>#{task_match['id']}</code>\n"
            f"• 📌 <b>Status:</b> {status_emoji} <b>{task_match['status'].upper()}</b>\n"
            f"• 👤 <b>Assigned User:</b> {assigned_str}\n"
            f"• 📝 <b>Details:</b> <code>{task_match['details']}</code>\n\n"
        )
        
    if sell_match:
        seller_id = sell_match['user_id']
        try:
            chat_info = await bot.get_chat(seller_id)
            s_name = f"@{chat_info.username}" if chat_info.username else f"User {seller_id}"
        except Exception:
            s_name = f"User {seller_id}"

        status_emoji = {
            'pending_review': '🟡',
            'approved': '🟢',
            'declined': '🔴'
        }.get(sell_match['status'], '⚪️')

        text += (
            f"📦 <b>Sell Request Record</b>\n"
            f"• 🆔 <b>Sell ID:</b> <code>#{sell_match['id']}</code>\n"
            f"• 📌 <b>Status:</b> {status_emoji} <b>{sell_match['status'].upper()}</b>\n"
            f"• 👤 <b>Seller:</b> {s_name} (<code>{seller_id}</code>)\n"
            f"• 💰 <b>Amount:</b> ₹{sell_match['amount']:.2f}\n"
            f"• 📝 <b>Details:</b>\n<code>{sell_match['details']}</code>\n"
        )

    kb = InlineKeyboardBuilder()
    if target_task_id:
        kb.button(
            text="ViewPast",
            callback_data=f"view_past_task:{target_task_id}",
            icon_custom_emoji_id="5870458774455587120",
            style="success"
        )
    
    await message.answer(text, parse_mode=ParseMode.HTML, reply_markup=kb.as_markup() if target_task_id else None)
    await state.clear()

@dp.callback_query(F.data.startswith("view_past_task:"))
async def cb_view_past_task(call: CallbackQuery):
    await call.answer()
    if call.from_user.id != ADMIN_ID:
        return

    task_id = int(call.data.split(":")[1])

    async with db_pool.acquire() as conn:
        history_records = await conn.fetch('''
            SELECT user_id, password_used, assigned_at 
            FROM task_history 
            WHERE task_id = $1 
            ORDER BY id DESC
        ''', task_id)

    if not history_records:
        await call.message.answer(
            f"📭 <b>No past assignment history recorded for Task #{task_id}.</b>",
            parse_mode=ParseMode.HTML
        )
        return

    history_text = (
        f"📜 <b>Past Assignment History for Task #{task_id}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
    )

    for idx, row in enumerate(history_records, start=1):
        u_id = row['user_id']
        pwd = row['password_used']
        dt_str = row['assigned_at'].strftime("%b %d, %Y %I:%M %p")

        try:
            user_obj = await bot.get_chat(u_id)
            user_display = f"@{user_obj.username}" if user_obj.username else f"User {u_id}"
        except Exception:
            user_display = f"User {u_id}"

        history_text += (
            f"<b>{idx}. Assigned Entry:</b>\n"
            f"• 👤 <b>User:</b> {user_display} (<code>{u_id}</code>)\n"
            f"• 🔑 <b>Password:</b> <code>{pwd}</code>\n"
            f"• 📅 <b>Assigned At:</b> {dt_str}\n"
            f"────────────────────\n"
        )

    await call.message.answer(history_text, parse_mode=ParseMode.HTML)

@dp.message(F.text == "➕ Add Balance", StateFilter("*"))
async def admin_btn_add_balance(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.set_state(AdminState.waiting_for_add_balance)
    await message.answer("💰 Send the User ID and Amount separated by space:\n\n<i>Example: 123456789 50</i>", parse_mode=ParseMode.HTML)

@dp.message(AdminState.waiting_for_add_balance, ~F.text.startswith("/"), ~F.text.in_(MENU_BUTTONS))
async def process_add_balance_step(message: Message, state: FSMContext):
    try:
        parts = message.text.strip().split()
        target_id = int(parts[0])
        amount = float(parts[1])

        await ensure_user(target_id)
        async with db_pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("UPDATE users SET balance = balance + $1 WHERE user_id=$2", amount, target_id)
                await conn.execute("INSERT INTO transactions (user_id, type, amount, note) VALUES ($1, $2, $3, $4)", target_id, "admin_add", amount, "Added by admin")

        invalidate_user_cache(target_id)
        await message.answer(f"✅ **Added ₹{amount:.2f} to User `{target_id}`'s balance.**", parse_mode=ParseMode.MARKDOWN, reply_markup=get_admin_menu_keyboard())
        
        asyncio.create_task(send_user_notification(
            target_id,
            f"💰 <b>Admin credited your balance!</b>\n+₹{amount:.2f} added to your account.",
            parse_mode=ParseMode.HTML
        ))
    except Exception as e:
        await message.answer(f"❌ Invalid format or error: `{e}`", parse_mode=ParseMode.MARKDOWN, reply_markup=get_admin_menu_keyboard())
    await state.clear()

@dp.message(F.text == "➖ Cut Balance", StateFilter("*"))
async def admin_btn_cut_balance(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.set_state(AdminState.waiting_for_cut_balance)
    await message.answer("⚠️ Send the User ID and Amount to deduct separated by space:\n\n<i>Example: 123456789 20</i>", parse_mode=ParseMode.HTML)

@dp.message(AdminState.waiting_for_cut_balance, ~F.text.startswith("/"), ~F.text.in_(MENU_BUTTONS))
async def process_cut_balance_step(message: Message, state: FSMContext):
    try:
        parts = message.text.strip().split()
        target_id = int(parts[0])
        amount = float(parts[1])

        await ensure_user(target_id)
        async with db_pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("UPDATE users SET balance = GREATEST(0, balance - $1) WHERE user_id=$2", amount, target_id)
                await conn.execute("INSERT INTO transactions (user_id, type, amount, note) VALUES ($1, $2, $3, $4)", target_id, "admin_deduct", -amount, "Deducted by admin")

        invalidate_user_cache(target_id)
        await message.answer(f"✅ **Deducted ₹{amount:.2f} from User `{target_id}`'s balance.**", parse_mode=ParseMode.MARKDOWN, reply_markup=get_admin_menu_keyboard())
        
        async with db_pool.acquire() as conn:
            target_user_data = await conn.fetchrow("SELECT currency FROM users WHERE user_id=$1", target_id)
            target_curr = target_user_data['currency'] if target_user_data else "USD"
        
        formatted_deduct_amt = format_currency(amount, target_curr)
        asyncio.create_task(send_user_notification(
            target_id,
            f"⚠️ <b>Admin deducted from your balance!</b>\n-{formatted_deduct_amt} deducted from your account.",
            parse_mode=ParseMode.HTML
        ))
    except Exception as e:
        await message.answer(f"❌ Invalid format or error: `{e}`", parse_mode=ParseMode.MARKDOWN, reply_markup=get_admin_menu_keyboard())
    await state.clear()

@dp.message(F.text == "🔎 Check Balance", StateFilter("*"))
async def admin_btn_check_balance(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.set_state(AdminState.waiting_for_check_balance)
    await message.answer("🔎 Send the numeric User ID to check:", parse_mode=ParseMode.MARKDOWN)

@dp.message(AdminState.waiting_for_check_balance, ~F.text.startswith("/"), ~F.text.in_(MENU_BUTTONS))
async def process_check_balance_step(message: Message, state: FSMContext):
    try:
        target_id = int(message.text.strip())
        user_data = await get_user_data(target_id)
        if not user_data:
            await message.answer(f"📭 User `{target_id}` not found in database.", parse_mode=ParseMode.MARKDOWN, reply_markup=get_admin_menu_keyboard())
            await state.clear()
            return

        bal = user_data['balance']
        upi = user_data['upi']
        usdt = user_data['usdt_address']

        await message.answer(
            f"👤 **User Information for `{target_id}`:**\n\n"
            f"• **Balance:** ₹{bal:.2f}\n"
            f"• **UPI:** `{upi}`\n"
            f"• **USDT BEP-20:** `{usdt}`",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=get_admin_menu_keyboard()
        )
    except ValueError:
        await message.answer("❌ Invalid User ID.", reply_markup=get_admin_menu_keyboard())
    await state.clear()

@dp.message(F.text == "🏆 Top Balances", StateFilter("*"))
async def admin_btn_top_balances(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.clear()
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT user_id, balance FROM users ORDER BY balance DESC LIMIT 10")

    if not rows:
        await message.answer("📭 No users found in database.", reply_markup=get_admin_menu_keyboard())
        return

    text = "🏆 **Top 10 Balance Holders**\n\n"
    for idx, r in enumerate(rows, start=1):
        text += f"**{idx}.** User ID: `{r['user_id']}` — **₹{r['balance']:.2f}**\n"

    await message.answer(text, parse_mode=ParseMode.MARKDOWN, reply_markup=get_admin_menu_keyboard())

@dp.message(F.text == "💳 Transactions", StateFilter("*"))
async def admin_btn_transactions(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.set_state(AdminState.waiting_for_user_transactions)
    await message.answer("💳 Send the User ID to check their transaction history:", parse_mode=ParseMode.MARKDOWN)

@dp.message(AdminState.waiting_for_user_transactions, ~F.text.startswith("/"), ~F.text.in_(MENU_BUTTONS))
async def process_user_transactions_step(message: Message, state: FSMContext):
    try:
        target_id = int(message.text.strip())
        text, reply_markup = await render_transaction_history_page(target_id, page=1, is_admin=True)
        await message.answer(text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
    except ValueError:
        await message.answer("❌ Invalid User ID.", reply_markup=get_admin_menu_keyboard())
    await state.clear()

@dp.callback_query(F.data.startswith("adm_tx_page:"))
async def cb_admin_tx_page(call: CallbackQuery):
    await call.answer()
    parts = call.data.split(":")
    target_user_id = int(parts[1])
    page = int(parts[2])
    text, reply_markup = await render_transaction_history_page(target_user_id, page=page, is_admin=True)
    try:
        await call.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
    except Exception:
        pass

@dp.message(F.text == "📊 View Stats", StateFilter("*"))
async def admin_btn_view_stats(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.clear()
    async with db_pool.acquire() as conn:
        total_users = await conn.fetchval("SELECT COUNT(*) FROM users")
        total_tasks = await conn.fetchval("SELECT COUNT(*) FROM tasks")
        avail_tasks = await conn.fetchval("SELECT COUNT(*) FROM tasks WHERE status='available'")
        assigned_tasks = await conn.fetchval("SELECT COUNT(*) FROM tasks WHERE status='assigned'")
        pending_review_tasks = await conn.fetchval("SELECT COUNT(*) FROM tasks WHERE status='pending_review'")
        completed_tasks = await conn.fetchval("SELECT COUNT(*) FROM tasks WHERE status='completed'")
        
        pending_sells = await conn.fetchval("SELECT COUNT(*) FROM pending_sells WHERE status='pending_review'")
        completed_sells = await conn.fetchval("SELECT COUNT(*) FROM pending_sells WHERE status='approved'")
        pending_withdrawals = await conn.fetchval("SELECT COUNT(*) FROM withdrawals WHERE status='pending'")

    total_pending = (pending_review_tasks or 0) + (pending_sells or 0)

    text = (
        f"📊 <b>Bot Task & User Statistics</b>\n\n"
        f"👥 <b>Total Users (started bot):</b> <code>{total_users}</code>\n\n"
        f"📋 <b>Total Tasks Added:</b> <code>{total_tasks}</code>\n"
        f"🟢 <b>Available Tasks Pool:</b> <code>{avail_tasks}</code>\n"
        f"💼 <b>Assigned Tasks:</b> <code>{assigned_tasks}</code>\n"
        f"⏳ <b>Pending Review (Tasks + Sells):</b> <code>{total_pending}</code>\n"
        f"✅ <b>Completed Tasks:</b> <code>{completed_tasks or 0}</code>\n"
        f"📦 <b>Completed Sell Gmail:</b> <code>{completed_sells or 0}</code>\n"
        f"💸 <b>Pending Withdrawals:</b> <code>{pending_withdrawals or 0}</code>"
    )
    await message.answer(text, parse_mode=ParseMode.HTML, reply_markup=get_admin_menu_keyboard())

@dp.message(F.text == "🚫 Ban User", StateFilter("*"))
async def admin_btn_ban_user(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.set_state(AdminState.waiting_for_ban_user)
    await message.answer("🚫 Send the numeric User ID to ban:", parse_mode=ParseMode.MARKDOWN)

@dp.message(AdminState.waiting_for_ban_user, ~F.text.startswith("/"), ~F.text.in_(MENU_BUTTONS))
async def process_ban_user_step(message: Message, state: FSMContext):
    try:
        target_id = int(message.text.strip())
        if target_id == ADMIN_ID:
            await message.answer("❌ You cannot ban yourself!", reply_markup=get_admin_menu_keyboard())
            await state.clear()
            return

        if target_id in BANNED_USERS_CACHE:
            await message.answer(f"⚠️ User `{target_id}` is already banned.", parse_mode=ParseMode.MARKDOWN, reply_markup=get_admin_menu_keyboard())
            await state.clear()
            return

        async with db_pool.acquire() as conn:
            await conn.execute("INSERT INTO banned_users (user_id) VALUES ($1) ON CONFLICT DO NOTHING", target_id)

        BANNED_USERS_CACHE.add(target_id)
        await message.answer(f"🚫 **User `{target_id}` has been banned!**", parse_mode=ParseMode.MARKDOWN, reply_markup=get_admin_menu_keyboard())
    except ValueError:
        await message.answer("❌ Invalid User ID.", reply_markup=get_admin_menu_keyboard())
    await state.clear()

@dp.message(F.text == "✅ Unban User", StateFilter("*"))
async def admin_btn_unban_user(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.set_state(AdminState.waiting_for_unban_user)
    await message.answer("✅ Send the numeric User ID to unban:", parse_mode=ParseMode.MARKDOWN)

@dp.message(AdminState.waiting_for_unban_user, ~F.text.startswith("/"), ~F.text.in_(MENU_BUTTONS))
async def process_unban_user_step(message: Message, state: FSMContext):
    try:
        target_id = int(message.text.strip())

        async with db_pool.acquire() as conn:
            res = await conn.execute("DELETE FROM banned_users WHERE user_id=$1", target_id)

        if res == "DELETE 0" and target_id not in BANNED_USERS_CACHE:
            await message.answer(f"⚠️ User `{target_id}` is not currently banned, so they cannot be unbanned.", parse_mode=ParseMode.MARKDOWN, reply_markup=get_admin_menu_keyboard())
            await state.clear()
            return

        BANNED_USERS_CACHE.discard(target_id)
        await message.answer(f"✅ **User `{target_id}` has been unbanned!**", parse_mode=ParseMode.MARKDOWN, reply_markup=get_admin_menu_keyboard())
    except ValueError:
        await message.answer("❌ Invalid User ID.", reply_markup=get_admin_menu_keyboard())
    await state.clear()

@dp.message(F.text == "📢 Broadcast", StateFilter("*"))
async def admin_btn_broadcast(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.set_state(AdminState.waiting_for_broadcast)
    await message.answer("📢 Send or forward the broadcast message below:", parse_mode=ParseMode.MARKDOWN)

@dp.message(AdminState.waiting_for_broadcast, ~F.text.in_(MENU_BUTTONS))
async def process_broadcast_message(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return

    async with db_pool.acquire() as conn:
        users = await conn.fetch("SELECT user_id FROM users")

    if not users:
        await message.answer("📭 No users found in database to send broadcast.", reply_markup=get_admin_menu_keyboard())
        await state.clear()
        return

    total_users = len(users)
    status_msg = await message.answer(
        f"⏳ <b>Broadcast in progress...</b>\nTotal targets: <b>{total_users}</b>",
        parse_mode=ParseMode.HTML
    )

    success_count = 0
    fail_count = 0

    for idx, u in enumerate(users, start=1):
        target_id = u['user_id']
        try:
            await bot.copy_message(
                chat_id=target_id,
                from_chat_id=message.chat.id,
                message_id=message.message_id
            )
            success_count += 1
        except TelegramForbiddenError:
            fail_count += 1
        except Exception:
            fail_count += 1

        if idx % 20 == 0 or idx == total_users:
            try:
                await status_msg.edit_text(
                    f"⏳ <b>Broadcasting...</b> ({idx}/{total_users})\n\n"
                    f"🟢 Success: <b>{success_count}</b>\n"
                    f"🔴 Failed: <b>{fail_count}</b>",
                    parse_mode=ParseMode.HTML
                )
            except Exception:
                pass

        await asyncio.sleep(0.04)

    await status_msg.edit_text(
        f"✅ <b>Broadcast Completed!</b>\n\n"
        f"📊 <b>Total Users Processed:</b> {total_users}\n"
        f"🟢 <b>Successfully Sent:</b> {success_count}\n"
        f"🔴 <b>Failed / Blocked:</b> {fail_count}",
        parse_mode=ParseMode.HTML,
        reply_markup=get_admin_menu_keyboard()
    )
    await state.clear()

@dp.message(F.text == "⚙️ Change Values", StateFilter("*"))
async def admin_btn_change_values(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.clear()
    
    task_usd = DEFAULT_TASK_RATE / USD_TO_INR
    sell_usd = GMAIL_SELL_RATE / USD_TO_INR
    min_w_usd = MIN_WITHDRAWAL_AMT / USD_TO_INR

    text = (
        f"⚙️ <b>Change System Rates & Limits</b>\n\n"
        f"📝 <b>Current Tasks Rate:</b> ₹{DEFAULT_TASK_RATE:.2f} (${task_usd:.2f})\n"
        f"📨 <b>Current Sell Rate:</b> ₹{GMAIL_SELL_RATE:.2f} (${sell_usd:.2f})\n"
        f"💸 <b>Current Minimum Withdrawal:</b> ₹{MIN_WITHDRAWAL_AMT:.2f} (${min_w_usd:.2f})\n"
        f"🔑 <b>Current Task Password:</b> <code>{DEFAULT_TASK_PASS}</code>\n"
        f"🔒 <b>Password Mode:</b> {'🟢 Fixed Default Password' if DEFAULT_TASK_PASS_STATUS else '🔴 Random Generated Password'}\n"
        f"🏷 <b>Current Fees:</b> UPI: ₹{UPI_FEES:.2f} | USDT: ₹{USDT_FEES:.2f} | Ultra: ₹{ULTRA_FEES:.2f}\n"
        f"⚡️ <b>Ultra API Token:</b> <code>{ULTRA_TOKEN}</code>\n"
        f"✍️ <b>Single Tasks System:</b> {'🟢 ON (1 Task at a time)' if SINGLE_TASK_STATUS else '🔴 OFF (Unlimited tasks without waiting for review)'}\n"
        f"📨 <b>Sell Gmail Function:</b> {'🟢 ON (Enabled)' if SELL_GMAIL_STATUS else '🔴 OFF (Disabled)'}\n\n"
        f"Select an option below to update:"
    )
    await message.answer(text, parse_mode=ParseMode.HTML, reply_markup=get_change_values_inline_keyboard())

@dp.callback_query(F.data == "admin_toggle_task_pass_mode")
async def cb_admin_toggle_task_pass_mode(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    global DEFAULT_TASK_PASS_STATUS
    DEFAULT_TASK_PASS_STATUS = not DEFAULT_TASK_PASS_STATUS
    new_val = 'on' if DEFAULT_TASK_PASS_STATUS else 'off'

    async with db_pool.acquire() as conn:
        await conn.execute("INSERT INTO bot_settings (key, value) VALUES ('default_task_pass_status', $1) ON CONFLICT (key) DO UPDATE SET value = $1", new_val)

    await call.answer(f"Task Password Mode is now {'Fixed (Default)' if DEFAULT_TASK_PASS_STATUS else 'Random Password'}!", show_alert=True)
    
    task_usd = DEFAULT_TASK_RATE / USD_TO_INR
    sell_usd = GMAIL_SELL_RATE / USD_TO_INR
    min_w_usd = MIN_WITHDRAWAL_AMT / USD_TO_INR

    text = (
        f"⚙️ <b>Change System Rates & Limits</b>\n\n"
        f"📝 <b>Current Tasks Rate:</b> ₹{DEFAULT_TASK_RATE:.2f} (${task_usd:.2f})\n"
        f"📨 <b>Current Sell Rate:</b> ₹{GMAIL_SELL_RATE:.2f} (${sell_usd:.2f})\n"
        f"💸 <b>Current Minimum Withdrawal:</b> ₹{MIN_WITHDRAWAL_AMT:.2f} (${min_w_usd:.2f})\n"
        f"🔑 <b>Current Task Password:</b> <code>{DEFAULT_TASK_PASS}</code>\n"
        f"🔒 <b>Password Mode:</b> {'🟢 Fixed Default Password' if DEFAULT_TASK_PASS_STATUS else '🔴 Random Generated Password'}\n"
        f"🏷 <b>Current Fees:</b> UPI: ₹{UPI_FEES:.2f} | USDT: ₹{USDT_FEES:.2f} | Ultra: ₹{ULTRA_FEES:.2f}\n"
        f"⚡️ <b>Ultra API Token:</b> <code>{ULTRA_TOKEN}</code>\n"
        f"✍️ <b>Single Tasks System:</b> {'🟢 ON (1 Task at a time)' if SINGLE_TASK_STATUS else '🔴 OFF (Unlimited tasks without waiting for review)'}\n"
        f"📨 <b>Sell Gmail Function:</b> {'🟢 ON (Enabled)' if SELL_GMAIL_STATUS else '🔴 OFF (Disabled)'}\n\n"
        f"Select an option below to update:"
    )

    try:
        await call.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=get_change_values_inline_keyboard())
    except Exception:
        pass

@dp.callback_query(F.data == "admin_toggle_single_task")
async def cb_admin_toggle_single_task(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    global SINGLE_TASK_STATUS
    SINGLE_TASK_STATUS = not SINGLE_TASK_STATUS
    new_val = 'on' if SINGLE_TASK_STATUS else 'off'

    async with db_pool.acquire() as conn:
        await conn.execute("INSERT INTO bot_settings (key, value) VALUES ('single_task_status', $1) ON CONFLICT (key) DO UPDATE SET value = $1", new_val)

    await call.answer(f"Single Tasks is now {'ON' if SINGLE_TASK_STATUS else 'OFF'}!", show_alert=True)
    
    task_usd = DEFAULT_TASK_RATE / USD_TO_INR
    sell_usd = GMAIL_SELL_RATE / USD_TO_INR
    min_w_usd = MIN_WITHDRAWAL_AMT / USD_TO_INR

    text = (
        f"⚙️ <b>Change System Rates & Limits</b>\n\n"
        f"📝 <b>Current Tasks Rate:</b> ₹{DEFAULT_TASK_RATE:.2f} (${task_usd:.2f})\n"
        f"📨 <b>Current Sell Rate:</b> ₹{GMAIL_SELL_RATE:.2f} (${sell_usd:.2f})\n"
        f"💸 <b>Current Minimum Withdrawal:</b> ₹{MIN_WITHDRAWAL_AMT:.2f} (${min_w_usd:.2f})\n"
        f"🔑 <b>Current Task Password:</b> <code>{DEFAULT_TASK_PASS}</code>\n"
        f"🔒 <b>Password Mode:</b> {'🟢 Fixed Default Password' if DEFAULT_TASK_PASS_STATUS else '🔴 Random Generated Password'}\n"
        f"🏷 <b>Current Fees:</b> UPI: ₹{UPI_FEES:.2f} | USDT: ₹{USDT_FEES:.2f} | Ultra: ₹{ULTRA_FEES:.2f}\n"
        f"⚡️ <b>Ultra API Token:</b> <code>{ULTRA_TOKEN}</code>\n"
        f"✍️ <b>Single Tasks System:</b> {'🟢 ON (1 Task at a time)' if SINGLE_TASK_STATUS else '🔴 OFF (Unlimited tasks without waiting for review)'}\n"
        f"📨 <b>Sell Gmail Function:</b> {'🟢 ON (Enabled)' if SELL_GMAIL_STATUS else '🔴 OFF (Disabled)'}\n\n"
        f"Select an option below to update:"
    )

    try:
        await call.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=get_change_values_inline_keyboard())
    except Exception:
        pass

@dp.callback_query(F.data == "admin_toggle_sell_gmail")
async def cb_admin_toggle_sell_gmail(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    global SELL_GMAIL_STATUS
    SELL_GMAIL_STATUS = not SELL_GMAIL_STATUS
    new_val = 'on' if SELL_GMAIL_STATUS else 'off'

    async with db_pool.acquire() as conn:
        await conn.execute("INSERT INTO bot_settings (key, value) VALUES ('sell_gmail_status', $1) ON CONFLICT (key) DO UPDATE SET value = $1", new_val)

    await call.answer(f"Sell Gmail is now {'ON' if SELL_GMAIL_STATUS else 'OFF'}!", show_alert=True)
    
    task_usd = DEFAULT_TASK_RATE / USD_TO_INR
    sell_usd = GMAIL_SELL_RATE / USD_TO_INR
    min_w_usd = MIN_WITHDRAWAL_AMT / USD_TO_INR

    text = (
        f"⚙️ <b>Change System Rates & Limits</b>\n\n"
        f"📝 <b>Current Tasks Rate:</b> ₹{DEFAULT_TASK_RATE:.2f} (${task_usd:.2f})\n"
        f"📨 <b>Current Sell Rate:</b> ₹{GMAIL_SELL_RATE:.2f} (${sell_usd:.2f})\n"
        f"💸 <b>Current Minimum Withdrawal:</b> ₹{MIN_WITHDRAWAL_AMT:.2f} (${min_w_usd:.2f})\n"
        f"🔑 <b>Current Task Password:</b> <code>{DEFAULT_TASK_PASS}</code>\n"
        f"🔒 <b>Password Mode:</b> {'🟢 Fixed Default Password' if DEFAULT_TASK_PASS_STATUS else '🔴 Random Generated Password'}\n"
        f"🏷 <b>Current Fees:</b> UPI: ₹{UPI_FEES:.2f} | USDT: ₹{USDT_FEES:.2f} | Ultra: ₹{ULTRA_FEES:.2f}\n"
        f"⚡️ <b>Ultra API Token:</b> <code>{ULTRA_TOKEN}</code>\n"
        f"✍️ <b>Single Tasks System:</b> {'🟢 ON (1 Task at a time)' if SINGLE_TASK_STATUS else '🔴 OFF (Unlimited tasks without waiting for review)'}\n"
        f"📨 <b>Sell Gmail Function:</b> {'🟢 ON (Enabled)' if SELL_GMAIL_STATUS else '🔴 OFF (Disabled)'}\n\n"
        f"Select an option below to update:"
    )

    try:
        await call.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=get_change_values_inline_keyboard())
    except Exception:
        pass

@dp.callback_query(F.data == "admin_change_tasks_rate")
async def cb_admin_change_tasks_rate(call: CallbackQuery, state: FSMContext):
    await call.answer()
    if call.from_user.id != ADMIN_ID:
        return
    await state.set_state(AdminState.waiting_for_change_tasks_rate)
    await call.message.answer(
        f"💰 <b>Current Tasks Rate:</b> ₹{DEFAULT_TASK_RATE:.2f}\n\n"
        f"Send the new rate for Tasks in INR (e.g. <code>40.0</code>):",
        parse_mode=ParseMode.HTML
    )

@dp.message(AdminState.waiting_for_change_tasks_rate, ~F.text.startswith("/"), ~F.text.in_(MENU_BUTTONS))
async def process_change_tasks_rate_step(message: Message, state: FSMContext):
    global DEFAULT_TASK_RATE
    try:
        new_rate = float(message.text.strip())
        DEFAULT_TASK_RATE = new_rate

        async with db_pool.acquire() as conn:
            await conn.execute("INSERT INTO bot_settings (key, value) VALUES ('default_task_rate', $1) ON CONFLICT (key) DO UPDATE SET value = $1", str(new_rate))
            await conn.execute("UPDATE tasks SET reward=$1 WHERE status='available'", new_rate)

        await message.answer(
            f"✅ <b>Tasks Rate Updated Successfully!</b>\n\nNew Rate: ₹{new_rate:.2f}\n(All available tasks updated as well)",
            parse_mode=ParseMode.HTML,
            reply_markup=get_admin_menu_keyboard()
        )
    except ValueError:
        await message.answer("❌ Invalid amount. Please send a valid number.", reply_markup=get_admin_menu_keyboard())
    await state.clear()

@dp.callback_query(F.data == "admin_change_sell_rate")
async def cb_admin_change_sell_rate(call: CallbackQuery, state: FSMContext):
    await call.answer()
    if call.from_user.id != ADMIN_ID:
        return
    await state.set_state(AdminState.waiting_for_change_sell_rate)
    await call.message.answer(
        f"📨 <b>Current Gmail Sell Rate:</b> ₹{GMAIL_SELL_RATE:.2f}\n\n"
        f"Send the new Sell Gmail rate in INR (e.g. <code>35.0</code>):",
        parse_mode=ParseMode.HTML
    )

@dp.message(AdminState.waiting_for_change_sell_rate, ~F.text.startswith("/"), ~F.text.in_(MENU_BUTTONS))
async def process_change_sell_rate_step(message: Message, state: FSMContext):
    global GMAIL_SELL_RATE
    try:
        new_rate = float(message.text.strip())
        GMAIL_SELL_RATE = new_rate

        async with db_pool.acquire() as conn:
            await conn.execute("INSERT INTO bot_settings (key, value) VALUES ('gmail_sell_rate', $1) ON CONFLICT (key) DO UPDATE SET value = $1", str(new_rate))

        await message.answer(
            f"✅ <b>Gmail Sell Rate Updated Successfully!</b>\n\nNew Rate: ₹{new_rate:.2f}",
            parse_mode=ParseMode.HTML,
            reply_markup=get_admin_menu_keyboard()
        )
    except ValueError:
        await message.answer("❌ Invalid amount. Please send a valid number.", reply_markup=get_admin_menu_keyboard())
    await state.clear()

@dp.callback_query(F.data == "admin_change_min_withdraw")
async def cb_admin_change_min_withdraw(call: CallbackQuery, state: FSMContext):
    await call.answer()
    if call.from_user.id != ADMIN_ID:
        return
    await state.set_state(AdminState.waiting_for_change_min_withdraw)
    await call.message.answer(
        f"💸 <b>Current Minimum Withdrawal:</b> ₹{MIN_WITHDRAWAL_AMT:.2f}\n\n"
        f"Send the new Minimum Withdrawal limit in INR (e.g. <code>100.0</code>):",
        parse_mode=ParseMode.HTML
    )

@dp.message(AdminState.waiting_for_change_min_withdraw, ~F.text.startswith("/"), ~F.text.in_(MENU_BUTTONS))
async def process_change_min_withdraw_step(message: Message, state: FSMContext):
    global MIN_WITHDRAWAL_AMT
    try:
        new_min = float(message.text.strip())
        MIN_WITHDRAWAL_AMT = new_min

        async with db_pool.acquire() as conn:
            await conn.execute("INSERT INTO bot_settings (key, value) VALUES ('min_withdrawal_rate', $1) ON CONFLICT (key) DO UPDATE SET value = $1", str(new_min))

        await message.answer(
            f"✅ <b>Minimum Withdrawal Updated Successfully!</b>\n\nNew Minimum Limit: ₹{new_min:.2f}",
            parse_mode=ParseMode.HTML,
            reply_markup=get_admin_menu_keyboard()
        )
    except ValueError:
        await message.answer("❌ Invalid amount. Please send a valid number.", reply_markup=get_admin_menu_keyboard())
    await state.clear()

@dp.callback_query(F.data == "admin_change_task_pass")
async def cb_admin_change_task_pass(call: CallbackQuery, state: FSMContext):
    await call.answer()
    if call.from_user.id != ADMIN_ID:
        return
    await state.set_state(AdminState.waiting_for_change_task_pass)
    await call.message.answer(
        f"🔑 <b>Current Task Password:</b> <code>{DEFAULT_TASK_PASS}</code>\n\n"
        f"Send the new default password for newly added tasks (e.g. <code>TaskVerseX</code>):",
        parse_mode=ParseMode.HTML
    )

@dp.message(AdminState.waiting_for_change_task_pass, ~F.text.startswith("/"), ~F.text.in_(MENU_BUTTONS))
async def process_change_task_pass_step(message: Message, state: FSMContext):
    global DEFAULT_TASK_PASS
    new_pass = message.text.strip()
    if not new_pass:
        await message.answer("❌ Password cannot be empty.", reply_markup=get_admin_menu_keyboard())
        await state.clear()
        return

    DEFAULT_TASK_PASS = new_pass

    async with db_pool.acquire() as conn:
        await conn.execute("INSERT INTO bot_settings (key, value) VALUES ('default_task_pass', $1) ON CONFLICT (key) DO UPDATE SET value = $1", new_pass)

    await message.answer(
        f"✅ <b>Default Task Password Updated Successfully!</b>\n\nNew Password: <code>{new_pass}</code>",
        parse_mode=ParseMode.HTML,
        reply_markup=get_admin_menu_keyboard()
    )
    await state.clear()

@dp.callback_query(F.data == "admin_change_fees")
async def cb_admin_change_fees(call: CallbackQuery, state: FSMContext):
    await call.answer()
    if call.from_user.id != ADMIN_ID:
        return
    await state.set_state(AdminState.waiting_for_change_fees)
    await call.message.answer(
        f"🏷 <b>Current Fees Settings:</b>\n"
        f"• UPI: ₹{UPI_FEES:.2f}\n"
        f"• USDT: ₹{USDT_FEES:.2f}\n"
        f"• Ultra: ₹{ULTRA_FEES:.2f}\n\n"
        f"Please send the new fees line by line below:\n"
        f"<i>Example:</i>\n"
        f"<code>upi 5</code>\n"
        f"<code>usdt 5</code>\n"
        f"<code>ultra 0</code>",
        parse_mode=ParseMode.HTML
    )

@dp.message(AdminState.waiting_for_change_fees, ~F.text.startswith("/"), ~F.text.in_(MENU_BUTTONS))
async def process_change_fees_step(message: Message, state: FSMContext):
    global UPI_FEES, USDT_FEES, ULTRA_FEES
    lines = message.text.strip().split('\n')
    
    updated = []
    async with db_pool.acquire() as conn:
        for line in lines:
            parts = line.strip().split()
            if len(parts) == 2:
                method = parts[0].lower()
                try:
                    fee_val = float(parts[1])
                    if method == 'upi':
                        UPI_FEES = fee_val
                        await conn.execute("INSERT INTO bot_settings (key, value) VALUES ('upi_fees', $1) ON CONFLICT (key) DO UPDATE SET value = $1", str(fee_val))
                        updated.append(f"UPI Fee: ₹{fee_val:.2f}")
                    elif method == 'usdt':
                        USDT_FEES = fee_val
                        await conn.execute("INSERT INTO bot_settings (key, value) VALUES ('usdt_fees', $1) ON CONFLICT (key) DO UPDATE SET value = $1", str(fee_val))
                        updated.append(f"USDT Fee: ₹{fee_val:.2f}")
                    elif method in ['ultra', 'ultragateway']:
                        ULTRA_FEES = fee_val
                        await conn.execute("INSERT INTO bot_settings (key, value) VALUES ('ultra_fees', $1) ON CONFLICT (key) DO UPDATE SET value = $1", str(fee_val))
                        updated.append(f"Ultra Fee: ₹{fee_val:.2f}")
                except ValueError:
                    pass

    if not updated:
        await message.answer("❌ Invalid format. Please provide values line by line like: `upi 5`", reply_markup=get_admin_menu_keyboard())
    else:
        await message.answer(
            f"✅ <b>Fees Updated Successfully!</b>\n\n" + "\n".join(updated),
            parse_mode=ParseMode.HTML,
            reply_markup=get_admin_menu_keyboard()
        )
    await state.clear()

@dp.callback_query(F.data == "admin_change_ultra")
async def cb_admin_change_ultra(call: CallbackQuery, state: FSMContext):
    await call.answer()
    if call.from_user.id != ADMIN_ID:
        return
    await state.set_state(AdminState.waiting_for_change_ultra_token)
    await call.message.answer(
        f"⚡️ <b>Current Ultra Gateway Settings:</b>\n\n"
        f"🔑 <b>API Token:</b> <code>{ULTRA_TOKEN}</code>\n"
        f"🔐 <b>API Key:</b> <code>{ULTRA_KEY}</code>\n\n"
        f"Send the new API Token (or token and key separated by space):",
        parse_mode=ParseMode.HTML
    )

@dp.message(AdminState.waiting_for_change_ultra_token, ~F.text.startswith("/"), ~F.text.in_(MENU_BUTTONS))
async def process_change_ultra_token_step(message: Message, state: FSMContext):
    global ULTRA_TOKEN, ULTRA_KEY
    parts = message.text.strip().split()
    
    new_token = parts[0]
    ULTRA_TOKEN = new_token
    
    async with db_pool.acquire() as conn:
        await conn.execute("INSERT INTO bot_settings (key, value) VALUES ('ultra_token', $1) ON CONFLICT (key) DO UPDATE SET value = $1", new_token)
        
        if len(parts) > 1:
            new_key = parts[1]
            ULTRA_KEY = new_key
            await conn.execute("INSERT INTO bot_settings (key, value) VALUES ('ultra_key', $1) ON CONFLICT (key) DO UPDATE SET value = $1", new_key)

    await message.answer(
        f"✅ <b>Ultra Gateway API Configuration Updated!</b>\n\n"
        f"🔑 <b>Token:</b> <code>{ULTRA_TOKEN}</code>\n"
        f"🔐 <b>Key:</b> <code>{ULTRA_KEY}</code>",
        parse_mode=ParseMode.HTML,
        reply_markup=get_admin_menu_keyboard()
    )
    await state.clear()

@dp.message(F.text == "🗑 Remove Task", StateFilter("*"))
async def admin_btn_remove_task(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.set_state(AdminState.waiting_for_remove_task)
    await message.answer("🗑 Send the Task ID to remove (e.g. `3`):", parse_mode=ParseMode.MARKDOWN)

@dp.message(AdminState.waiting_for_remove_task, ~F.text.startswith("/"), ~F.text.in_(MENU_BUTTONS))
async def process_remove_task_step(message: Message, state: FSMContext):
    try:
        task_id = int(message.text.strip())
        async with db_pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("DELETE FROM task_assignments WHERE task_id=$1", task_id)
                res = await conn.execute("DELETE FROM tasks WHERE id=$1", task_id)

        if res == "DELETE 0":
            await message.answer(f"📭 Task `#{task_id}` not found.", parse_mode=ParseMode.MARKDOWN, reply_markup=get_admin_menu_keyboard())
        else:
            await message.answer(f"✅ **Task `#{task_id}` removed successfully!**", parse_mode=ParseMode.MARKDOWN, reply_markup=get_admin_menu_keyboard())
    except ValueError:
        await message.answer("❌ Invalid Task ID.", reply_markup=get_admin_menu_keyboard())
    await state.clear()

@dp.message(Command("mustjoin"), StateFilter("*"))
@dp.message(F.text == "📢 Must Join Channel", StateFilter("*"))
async def set_must_join_command(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.set_state(AdminState.waiting_for_channel_link)
    current = MUST_JOIN_CHANNEL if MUST_JOIN_CHANNEL else "Disabled"
    await message.answer(
        f"📢 <b>Must Join Channel Settings</b>\n\n"
        f"Currently set to: <code>{current}</code>\n\n"
        f"Send the channel username (e.g. <code>@MyChannel</code>) or link (e.g. <code>https://t.me/MyChannel</code>).\n\n"
        f"<i>Type <code>none</code> to disable forced channel joining.</i>",
        parse_mode=ParseMode.HTML
    )

@dp.message(AdminState.waiting_for_channel_link, ~F.text.startswith("/"), ~F.text.in_(MENU_BUTTONS))
async def process_must_join_channel_step(message: Message, state: FSMContext):
    global MUST_JOIN_CHANNEL
    val = message.text.strip()

    if val.lower() == "none":
        MUST_JOIN_CHANNEL = None
        db_val = "off"
        msg = "✅ <b>Forced channel join disabled.</b>"
    else:
        if "/" in val:
            val = "@" + val.split("/")[-1].replace("@", "")
        elif not val.startswith("@"):
            val = "@" + val

        MUST_JOIN_CHANNEL = val
        db_val = val
        msg = f"✅ <b>Must join channel updated to:</b> <code>{val}</code>"

    async with db_pool.acquire() as conn:
        await conn.execute("INSERT INTO bot_settings (key, value) VALUES ('must_join_channel', $1) ON CONFLICT (key) DO UPDATE SET value = $1", db_val)

    await message.answer(msg, parse_mode=ParseMode.HTML, reply_markup=get_admin_menu_keyboard())
    await state.clear()

# ============================================
# USER INLINE SUBMIT & CANCEL SYSTEM
# ============================================

@dp.callback_query(F.data == "link_upi")
async def start_link_upi(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.set_state(UserState.setting_upi)
    await call.message.answer('<tg-emoji emoji-id="5902449142575141204">🔡</tg-emoji> Send your UPI ID below:\n\n<i>Example: username@upi or 9876543210@paytm</i>', parse_mode=ParseMode.HTML)

@dp.callback_query(F.data == "link_usdt")
async def start_link_usdt(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.set_state(UserState.setting_usdt)
    await call.message.answer('<tg-emoji emoji-id="5902449142575141204">🪙</tg-emoji> Send your <b>USDT BEP-20</b> address below:\n\n<i>Example: 0x1234567890abcdef1234567890abcdef12345678</i>', parse_mode=ParseMode.HTML)

@dp.callback_query(F.data == "link_ultra")
async def start_link_ultra(call: CallbackQuery, state: FSMContext):
    if not ULTRA_STATUS:
        await call.answer("❌ Ultra Gateway is currently disabled by Admin!", show_alert=True)
        return
    await call.answer()
    await state.set_state(UserState.setting_ultra)
    await call.message.answer(
        '⚡️ Send your <b>Ultra Gateway Number</b> below:\n\n'
        '🌐 <i>Register/Get your Ultra Gateway account here:</i> https://ultra-pay.store', 
        parse_mode=ParseMode.HTML
    )

@dp.callback_query(F.data == "choose_withdraw_method")
async def choose_withdraw_method_handler(call: CallbackQuery):
    user_id = call.from_user.id
    async with db_pool.acquire() as conn:
        existing_pending = await conn.fetchrow(
            "SELECT id FROM withdrawals WHERE user_id = $1 AND status = 'pending'",
            user_id
        )
        if existing_pending:
            await call.answer('Your Previous Withdrawal is Already Pending, Please Wait it to be Processed', show_alert=True)
            return

    await call.answer()
    text = "<tg-emoji emoji-id=\"5445353829304387411\">💳</tg-emoji> <b>Select Withdrawal Method:</b>"
    try:
        await call.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=get_withdraw_options_keyboard())
    except Exception as e:
        print(f"Error choosing withdraw method: {e}")

@dp.callback_query(F.data == "withdraw_upi")
async def inline_withdraw_upi_handler(call: CallbackQuery):
    user_id = call.from_user.id
    user_data = await get_user_data(user_id)
    bal = user_data['balance'] if user_data else 0.0
    upi = user_data['upi'] if user_data else "None"
    curr = user_data['currency'] if user_data else "USD"

    if upi == "None" or not upi:
        await call.answer("❌ Please link your UPI ID first before withdrawing via UPI!", show_alert=True)
        return

    if bal < MIN_WITHDRAWAL_AMT:
        min_withdraw_str = format_currency(MIN_WITHDRAWAL_AMT, curr)
        bal_str = format_currency(bal, curr)
        await call.answer(f"❌ Minimum withdrawal is {min_withdraw_str}. Current Balance: {bal_str}", show_alert=True)
        return

    await call.answer()

    total_deducted = bal
    payout_amount = bal - UPI_FEES

    async with db_pool.acquire() as conn:
        existing_pending = await conn.fetchrow(
            "SELECT id FROM withdrawals WHERE user_id = $1 AND status = 'pending'",
            user_id
        )
        if existing_pending:
            await call.answer("Your Previous Withdrawal is Already Pending, Please Wait it to be Processed", show_alert=True)
            return

        async with conn.transaction():
            await conn.execute("UPDATE users SET balance = 0 WHERE user_id=$1", user_id)
            withdraw_id = await conn.fetchval(
                "INSERT INTO withdrawals(user_id, amount, method, payment_address) VALUES ($1, $2, 'UPI', $3) RETURNING id",
                user_id, payout_amount, upi
            )
            await conn.execute(
                "INSERT INTO transactions (user_id, type, amount, note) VALUES ($1, $2, $3, $4)",
                user_id, "withdrawal_pending", -total_deducted, f"UPI Withdrawal #{withdraw_id} pending (Payout: ₹{payout_amount:.2f}, Fee: ₹{UPI_FEES:.2f})"
            )

    invalidate_user_cache(user_id)

    kb = InlineKeyboardBuilder()
    kb.button(
        text='Pay', 
        callback_data=f'wp:{withdraw_id}',
        icon_custom_emoji_id="5444856076954520455",
        style="success"
    )
    kb.button(
        text='Reject', 
        callback_data=f'wr:{withdraw_id}',
        icon_custom_emoji_id="5274099962655816924",
        style="danger"
    )
    kb.adjust(2)
    
    await bot.send_message(
        ADMIN_ID,
        f'<tg-emoji emoji-id="5417924076503062111">💰</tg-emoji> <b>WITHDRAWAL REQUEST #{withdraw_id} (UPI)</b>\n\n'
        f'<tg-emoji emoji-id="5870458774455587120">👤</tg-emoji> @{call.from_user.username}\n'
        f'<tg-emoji emoji-id="5197269100878907942">✍️</tg-emoji> <code>{user_id}</code>\n'
        f'<tg-emoji emoji-id="5417924076503062111">💰</tg-emoji> Net Payout: ₹{payout_amount:.2f} (Fee Charged: ₹{UPI_FEES:.2f})\n'
        f'<tg-emoji emoji-id="6152069549442208798">🏦</tg-emoji> UPI: <code>{upi}</code>',
        reply_markup=kb.as_markup(),
        parse_mode=ParseMode.HTML
    )

    payout_display = format_currency(payout_amount, curr)
    fee_display = format_currency(UPI_FEES, curr)
    try:
        await call.message.edit_text(
            f'<tg-emoji emoji-id="5195033767969839232">🚀</tg-emoji> Withdrawal request submitted!\n\n'
            f'💰 <b>Net Payout:</b> {payout_display}\n'
            f'🏷 <b>Deducted Fee:</b> {fee_display}\n'
            f'🏦 <b>UPI ID:</b> <code>{upi}</code>',
            parse_mode=ParseMode.HTML
        )
    except Exception as e:
        print(f"Error editing withdraw msg: {e}")

@dp.callback_query(F.data == "withdraw_usdt")
async def inline_withdraw_usdt_handler(call: CallbackQuery):
    user_id = call.from_user.id
    user_data = await get_user_data(user_id)
    bal = user_data['balance'] if user_data else 0.0
    usdt = user_data['usdt_address'] if user_data else "None"
    curr = user_data['currency'] if user_data else "USD"

    if usdt == "None" or not usdt:
        await call.answer("❌ Please link your USDT BEP-20 address first before withdrawing!", show_alert=True)
        return

    if bal < MIN_WITHDRAWAL_AMT:
        min_withdraw_str = format_currency(MIN_WITHDRAWAL_AMT, curr)
        bal_str = format_currency(bal, curr)
        await call.answer(f"❌ Minimum withdrawal is {min_withdraw_str}. Current Balance: {bal_str}", show_alert=True)
        return

    await call.answer()

    total_deducted = bal
    payout_amount = bal - USDT_FEES

    async with db_pool.acquire() as conn:
        existing_pending = await conn.fetchrow(
            "SELECT id FROM withdrawals WHERE user_id = $1 AND status = 'pending'",
            user_id
        )
        if existing_pending:
            await call.answer("Your Previous Withdrawal is Already Pending, Please Wait it to be Processed", show_alert=True)
            return

        async with conn.transaction():
            await conn.execute("UPDATE users SET balance = 0 WHERE user_id=$1", user_id)
            withdraw_id = await conn.fetchval(
                "INSERT INTO withdrawals(user_id, amount, method, payment_address) VALUES ($1, $2, 'USDT BEP-20', $3) RETURNING id",
                user_id, payout_amount, usdt
            )
            await conn.execute(
                "INSERT INTO transactions (user_id, type, amount, note) VALUES ($1, $2, $3, $4)",
                user_id, "withdrawal_pending", -total_deducted, f"USDT Withdrawal #{withdraw_id} pending (Payout: ₹{payout_amount:.2f}, Fee: ₹{USDT_FEES:.2f})"
            )

    invalidate_user_cache(user_id)

    kb = InlineKeyboardBuilder()
    kb.button(
        text='Pay', 
        callback_data=f'wp:{withdraw_id}',
        icon_custom_emoji_id="5444856076954520455",
        style="success"
    )
    kb.button(
        text='Reject', 
        callback_data=f'wr:{withdraw_id}',
        icon_custom_emoji_id="5274099962655816924",
        style="danger"
    )
    kb.adjust(2)
    
    usdt_amount = payout_amount / USD_TO_INR
    await bot.send_message(
        ADMIN_ID,
        f'<tg-emoji emoji-id="5417924076503062111">💰</tg-emoji> <b>WITHDRAWAL REQUEST #{withdraw_id} (USDT BEP-20)</b>\n\n'
        f'<tg-emoji emoji-id="5870458774455587120">👤</tg-emoji> @{call.from_user.username}\n'
        f'<tg-emoji emoji-id="5197269100878907942">✍️</tg-emoji> <code>{user_id}</code>\n'
        f'<tg-emoji emoji-id="5417924076503062111">💰</tg-emoji> Net Payout: ₹{payout_amount:.2f} (~${usdt_amount:.2f} USDT) (Fee Charged: ₹{USDT_FEES:.2f})\n'
        f'<tg-emoji emoji-id="5197434882321567830">🪙</tg-emoji> USDT BEP-20: <code>{usdt}</code>',
        reply_markup=kb.as_markup(),
        parse_mode=ParseMode.HTML
    )

    payout_display = format_currency(payout_amount, curr)
    fee_display = format_currency(USDT_FEES, curr)
    try:
        await call.message.edit_text(
            f'<tg-emoji emoji-id="5195033767969839232">🚀</tg-emoji> Withdrawal request submitted!\n\n'
            f'💰 <b>Net Payout:</b> {payout_display} (~${usdt_amount:.2f} USDT)\n'
            f'🏷 <b>Deducted Fee:</b> {fee_display}\n'
            f'🪙 <b>USDT Address:</b> <code>{usdt}</code>',
            parse_mode=ParseMode.HTML
        )
    except Exception as e:
        print(f"Error editing withdraw msg: {e}")

@dp.callback_query(F.data == "withdraw_ultra")
async def inline_withdraw_ultra_handler(call: CallbackQuery):
    if not ULTRA_STATUS:
        await call.answer("❌ Ultra Gateway is currently disabled by Admin!", show_alert=True)
        return

    user_id = call.from_user.id
    user_data = await get_user_data(user_id)
    bal = user_data['balance'] if user_data else 0.0
    ultra_num = user_data['ultra_number'] if user_data else "None"
    curr = user_data['currency'] if user_data else "USD"

    if ultra_num == "None" or not ultra_num:
        await call.answer("❌ Please link your Ultra Gateway number first before withdrawing!", show_alert=True)
        return

    if bal < MIN_WITHDRAWAL_AMT:
        min_withdraw_str = format_currency(MIN_WITHDRAWAL_AMT, curr)
        bal_str = format_currency(bal, curr)
        await call.answer(f"❌ Minimum withdrawal is {min_withdraw_str}. Current Balance: {bal_str}", show_alert=True)
        return

    payout_amount = bal - ULTRA_FEES

    url = f"https://ultra-pay.store/APIs/api?token={urllib.parse.quote(ULTRA_TOKEN)}&key={urllib.parse.quote(ULTRA_KEY)}&paytoNumber={urllib.parse.quote(ultra_num)}&amount={payout_amount:.2f}&comment=iGmail Pay"

    await call.answer("⚡ Processing instant payment via Ultra Gateway...", show_alert=False)

    api_success = False
    api_reason = "Unknown Error"

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=15.0)) as resp:
                raw_text = await resp.text()
                try:
                    res_data = json.loads(raw_text)
                except Exception:
                    res_data = {}

                if resp.status == 200:
                    status_val = str(res_data.get("status", "")).lower()
                    if status_val in ["success", "true", "1", "ok"]:
                        api_success = True
                    else:
                        api_reason = res_data.get("message") or res_data.get("msg") or raw_text
                else:
                    api_reason = f"HTTP Error {resp.status}: {raw_text}"
    except Exception as e:
        api_reason = f"Connection error: {e}"

    if api_success:
        async with db_pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("UPDATE users SET balance = 0 WHERE user_id=$1", user_id)
                await conn.execute("INSERT INTO withdrawals(user_id, amount, method, payment_address, status) VALUES ($1, $2, 'Ultra Gateway', $3, 'paid')", user_id, payout_amount, ultra_num)
                await conn.execute("INSERT INTO transactions (user_id, type, amount, note) VALUES ($1, $2, $3, $4)", user_id, "withdrawal", -bal, "Ultra Gateway instant payout paid")

        invalidate_user_cache(user_id)

        bal_display = format_currency(payout_amount, curr)
        msg_text = (
            f"🎉 <b>Instant Payment Successful!</b>\n\n"
            f"⚡️ <b>Method:</b> Ultra Gateway\n"
            f"📱 <b>Number:</b> <code>{ultra_num}</code>\n"
            f"💰 <b>Amount Transferred:</b> {bal_display}\n\n"
            f"🌐 <i>Gateway:</i> https://ultra-pay.store"
        )
        try:
            await call.message.edit_text(msg_text, parse_mode=ParseMode.HTML)
        except Exception:
            await call.message.answer(msg_text, parse_mode=ParseMode.HTML)
    else:
        fail_msg = (
            f"❌ <b>Ultra Gateway Instant Payment Failed!</b>\n\n"
            f"💬 <b>Reason:</b> <code>{api_reason}</code>\n\n"
            f"<i>Your balance was not deducted. Please check your Ultra Gateway number or try again later.</i>\n"
            f"🌐 <i>Gateway link:</i> https://ultra-pay.store"
        )
        try:
            await call.message.edit_text(fail_msg, parse_mode=ParseMode.HTML)
        except Exception:
            await call.message.answer(fail_msg, parse_mode=ParseMode.HTML)

@dp.callback_query(F.data == "user_submit_task")
async def inline_submit_task(call: CallbackQuery, state: FSMContext):
    await call.answer()
    user_id = call.from_user.id
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow('SELECT ta.task_id, t.status FROM task_assignments ta JOIN tasks t ON ta.task_id = t.id WHERE ta.user_id=$1 AND t.status != \'completed\' ORDER BY ta.assigned_at DESC LIMIT 1', user_id)
    
    if not row:
        await call.answer('❌ You do not have any active task.', show_alert=True)
        return
    if row['status'] == 'pending_review':
        await call.answer('⏳ You have already submitted this task.', show_alert=True)
        return
        
    await state.set_state(UserState.submitting_task)
    await call.message.answer('<tg-emoji emoji-id="5206607081334906820">✔️</tg-emoji> Send screenshot or proof of completed task.', parse_mode=ParseMode.HTML)

@dp.callback_query(F.data == "user_cancel_task")
async def inline_cancel_task(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.clear()
    user_id = call.from_user.id
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow('SELECT ta.task_id, t.status FROM task_assignments ta JOIN tasks t ON ta.task_id = t.id WHERE ta.user_id=$1 AND t.status != \'completed\' ORDER BY ta.assigned_at DESC LIMIT 1', user_id)
        if not row:
            await call.answer("❌ You don't have any active task to cancel.", show_alert=True)
            return
        
        if row['status'] == 'pending_review':
            await call.answer("❌ Cannot cancel a task already submitted for admin review.", show_alert=True)
            return

        task_id = row['task_id']
        async with conn.transaction():
            await conn.execute('DELETE FROM task_assignments WHERE user_id=$1 AND task_id=$2', user_id, task_id)
            await conn.execute("UPDATE tasks SET status='available' WHERE id=$1", task_id)
            
    try:
        await call.message.edit_text(f'<tg-emoji emoji-id="6217663806110175239">✅</tg-emoji> Task #{task_id} has been cancelled and returned to the pool.', parse_mode=ParseMode.HTML)
    except Exception:
        pass

@dp.message(UserState.submitting_task, F.photo | F.text, ~F.text.startswith("/") if F.text else True, ~F.text.in_(MENU_BUTTONS) if F.text else True)
async def handle_task_submission(message: Message, state: FSMContext):
    user_id = message.from_user.id
    async with db_pool.acquire() as conn:
        task = await conn.fetchrow('''
            SELECT t.id, t.title, t.details, t.reward, t.added_by 
            FROM task_assignments ta 
            JOIN tasks t ON ta.task_id = t.id 
            WHERE ta.user_id=$1 AND t.status = 'assigned' 
            ORDER BY ta.assigned_at DESC 
            LIMIT 1
        ''', user_id)

    if not task:
        await state.clear()
        sent_msg = await message.answer('❌ No active assigned task found to submit.', reply_markup=get_main_menu_keyboard())
        await state.update_data(last_menu_msg_id=sent_msg.message_id)
        return
    
    task_id = task['id']
    title = task['title']
    details = task['details']
    reward = task['reward']
    added_by_worker = task.get('added_by')

    try:
        parts = details.split(" | ")
        email = parts[0].replace("Email: ", "").strip()
        password = parts[1].replace("Pass: ", "").strip()
    except Exception:
        email = title.replace("Login to ", "").strip()
        password = DEFAULT_TASK_PASS

    is_valid = await is_gmail_registered(email, user_id=user_id)
    if not is_valid:
        await message.answer(
            f"❌ <b>This Gmail account (<code>{email}</code>) does not exist on Google!</b>\n\n"
            f"Please create <code>{email}</code> first on Google, then submit your proof again.",
            parse_mode=ParseMode.HTML
        )
        return

    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE tasks SET status='pending_review' WHERE id=$1", task_id)

    admin_kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text='Approve', callback_data=f'ta:{task_id}', icon_custom_emoji_id="6217663806110175239", style="success"),
        InlineKeyboardButton(text='Decline', callback_data=f'td:{task_id}', icon_custom_emoji_id="5274099962655816924", style="danger")
    ]])

    admin_msg_text = (
        f'<tg-emoji emoji-id="5206607081334906820">📤</tg-emoji> <b>Task Submission #{task_id}</b>\n\n'
        f'<tg-emoji emoji-id="5870458774455587120">👤</tg-emoji> <b>User:</b> @{message.from_user.username} (<code>{user_id}</code>)\n\n'
        f'📧 <b>Email:</b>\n<code>{email}</code>\n\n'
        f'<tg-emoji emoji-id="6005570495603282482">🔑</tg-emoji> <b>Password:</b>\n<code>{password}</code>\n\n'
        f'<tg-emoji emoji-id="5417924076503062111">💰</tg-emoji> <b>Reward:</b> ₹{reward}'
    )

    if message.photo:
        await bot.send_photo(ADMIN_ID, photo=message.photo[-1].file_id, caption=admin_msg_text, reply_markup=admin_kb, parse_mode=ParseMode.HTML)
    else:
        proof_text = f"\n\nProof: {message.text}"
        await bot.send_message(ADMIN_ID, admin_msg_text + proof_text, reply_markup=admin_kb, parse_mode=ParseMode.HTML)

    if added_by_worker and str(added_by_worker) != str(ADMIN_ID):
        if WORKER_BOT_TOKEN:
            async def send_worker_alert():
                try:
                    w_bot = Bot(token=WORKER_BOT_TOKEN)
                    worker_kb = InlineKeyboardMarkup(inline_keyboard=[[
                        InlineKeyboardButton(text="Approve", callback_data=f"w_ta:{task_id}", icon_custom_emoji_id="6217663806110175239", style="success"),
                        InlineKeyboardButton(text="Decline", callback_data=f"w_td:{task_id}", icon_custom_emoji_id="5274099962655816924", style="danger")
                    ]])
                    worker_msg_text = (
                        f'<tg-emoji emoji-id="5206607081334906820">📤</tg-emoji> <b>New Task Submission #{task_id}</b>\n\n'
                        f'📧 <b>Email:</b>\n<code>{email}</code>\n\n'
                        f'<tg-emoji emoji-id="6005570495603282482">🔑</tg-emoji> <b>Password:</b>\n<code>{password}</code>'
                    )
                    
                    if message.photo:
                        photo_id = message.photo[-1].file_id
                        if message.caption:
                            worker_msg_text += f"\n\n📝 <b>Proof:</b> {message.caption}"
                        await w_bot.send_photo(added_by_worker, photo=photo_id, caption=worker_msg_text, reply_markup=worker_kb, parse_mode=ParseMode.HTML)
                    else:
                        worker_msg_text += f"\n\n📝 <b>Proof:</b> {message.text}"
                        await w_bot.send_message(added_by_worker, worker_msg_text, reply_markup=worker_kb, parse_mode=ParseMode.HTML)
                    
                    await w_bot.session.close()
                except Exception as err:
                    print(f"Error sending worker real-time submission alert: {err}")

            asyncio.create_task(send_worker_alert())

    sent_msg = await message.answer(
        f'<tg-emoji emoji-id="5206607081334906820">✔️</tg-emoji> Task #{task_id} submission sent for review.\n\n'
        f'<tg-emoji emoji-id="5447644880824181073">⚠️</tg-emoji> <b>Important:</b> Please make sure to <b>logout</b> of this account from your device!', 
        reply_markup=get_main_menu_keyboard(), 
        parse_mode=ParseMode.HTML
    )
    await state.clear()
    await state.update_data(last_menu_msg_id=sent_msg.message_id)

# ============================================
# UNIFIED SELL APPROVE & DECLINE HANDLERS
# ============================================

@dp.callback_query(F.data.startswith("sa:"))
async def approve_sell_unified(call: CallbackQuery):
    sell_id = int(call.data.split(":")[1])

    async with db_pool.acquire() as conn:
        sell_data = await conn.fetchrow("SELECT user_id, amount, status FROM pending_sells WHERE id=$1", sell_id)
        if not sell_data or sell_data['status'] != 'pending_review':
            await call.answer("⚠️ This request is already processed!", show_alert=True)
            return

        await call.answer()
        user_id = sell_data['user_id']
        amount = sell_data['amount']

        await ensure_user(user_id, conn=conn)

        async with conn.transaction():
            await conn.execute("UPDATE users SET balance = balance + $1 WHERE user_id=$2", amount, user_id)
            await conn.execute("INSERT INTO transactions (user_id, type, amount, note) VALUES ($1, $2, $3, $4)", user_id, "sell", amount, f"Gmail sell #{sell_id} approved")
            await conn.execute("UPDATE pending_sells SET status='approved' WHERE id=$1", sell_id)

            referred_by = await conn.fetchval("SELECT referred_by FROM users WHERE user_id=$1", user_id)

        if REF_STATUS and referred_by and referred_by != user_id:
            await ensure_user(referred_by, conn=conn)
            ref_reward = REFERRAL_SELL_BONUS
            async with conn.transaction():
                await conn.execute("UPDATE users SET balance = balance + $1, referral_earnings = referral_earnings + $1 WHERE user_id=$2", ref_reward, referred_by)
                await conn.execute("INSERT INTO transactions (user_id, type, amount, note) VALUES ($1, $2, $3, $4)", referred_by, "referral", ref_reward, f"Referral reward from User #{user_id}")
            
            invalidate_user_cache(referred_by)
            
            async def notify_ref():
                ref_user_data = await get_user_data(referred_by)
                ref_amt_str = format_currency(ref_reward, ref_user_data['currency'])
                notif_text = (
                    f'<tg-emoji emoji-id="6217663806110175239">🎉</tg-emoji> Your referral <code>{user_id}</code> sell gmail got approved and <b>{ref_amt_str}</b> credited to your balance!'
                )
                await send_user_notification(referred_by, notif_text, parse_mode=ParseMode.HTML)
            
            asyncio.create_task(notify_ref())

    invalidate_user_cache(user_id)
    await edit_admin_message(call, '✅ Sell Request Approved')
    
    async def notify_user():
        user_data = await get_user_data(user_id)
        formatted_amt = format_currency(amount, user_data['currency'])
        await send_user_notification(user_id, f"🎉 Your Gmail sell request #{sell_id} was approved!\n+{formatted_amt} added to your balance.")

    asyncio.create_task(notify_user())

@dp.callback_query(F.data.startswith("sd:"))
async def decline_sell_unified(call: CallbackQuery, state: FSMContext):
    sell_id = int(call.data.split(":")[1])

    async with db_pool.acquire() as conn:
        sell_data = await conn.fetchrow("SELECT user_id, status FROM pending_sells WHERE id=$1", sell_id)
        if not sell_data or sell_data['status'] != 'pending_review':
            await call.answer("⚠️ This request is already processed!", show_alert=True)
            return
        user_id = sell_data['user_id']

    await call.answer()
    await state.set_state(AdminState.waiting_for_sell_reject_reason)
    await state.update_data(
        sell_id=sell_id,
        user_id=user_id, 
        admin_msg_id=call.message.message_id,
        is_photo=bool(call.message.photo)
    )
    await call.message.answer('<tg-emoji emoji-id="5447644880824181073">⚠️</tg-emoji> <b>Please reply with the reason for declining this sell request:</b>', parse_mode=ParseMode.HTML)

@dp.message(AdminState.waiting_for_sell_reject_reason, ~F.text.startswith("/"), ~F.text.in_(MENU_BUTTONS))
async def process_sell_reject_reason(message: Message, state: FSMContext):
    data = await state.get_data()
    sell_id = data.get('sell_id')
    user_id = data['user_id']
    admin_msg_id = data['admin_msg_id']
    is_photo = data['is_photo']
    reason = message.text.strip()

    if sell_id:
        async with db_pool.acquire() as conn:
            await conn.execute("UPDATE pending_sells SET status='declined' WHERE id=$1", sell_id)

    new_text = f'<tg-emoji emoji-id="5447644880824181073">⚠️</tg-emoji> <b>Sell request declined.</b>\n<b>Reason:</b> {reason}'
    try:
        if is_photo:
            await bot.edit_message_caption(chat_id=message.chat.id, message_id=admin_msg_id, caption=new_text, reply_markup=None, parse_mode=ParseMode.HTML)
        else:
            await bot.edit_message_text(chat_id=message.chat.id, message_id=admin_msg_id, text=new_text, reply_markup=None, parse_mode=ParseMode.HTML)
    except Exception as e:
        print(f"Error editing admin msg: {e}")

    asyncio.create_task(send_user_notification(
        user_id, 
        f'<tg-emoji emoji-id="5447644880824181073">⚠️</tg-emoji> <b>Your sell request #{sell_id} was declined.</b>\n\n<tg-emoji emoji-id="4956475826762679249">💬</tg-emoji> <b>Reason:</b> {reason}', 
        parse_mode=ParseMode.HTML
    ))

    await message.answer('<tg-emoji emoji-id="6217663806110175239">✅</tg-emoji> Rejection reason sent to user.', parse_mode=ParseMode.HTML)
    await state.clear()

# ============================================
# TASK APPROVE & DECLINE HANDLERS
# ============================================

@dp.callback_query(F.data.startswith("ta:"))
async def approve_task(call: CallbackQuery):
    task_id = int(call.data.split(":")[1])
    
    async with db_pool.acquire() as conn:
        task_data = await conn.fetchrow("SELECT reward, status, details FROM tasks WHERE id=$1", task_id)
        if not task_data or task_data['status'] != 'pending_review':
            await call.answer("⚠️ This request is already processed!", show_alert=True)
            return

        await call.answer()
        reward = task_data['reward']
        assigned_user_id = await conn.fetchval("SELECT user_id FROM task_assignments WHERE task_id=$1", task_id)
        if not assigned_user_id:
            return

        user_id = assigned_user_id
        try:
            task_email = task_data['details'].split(" | ")[0].replace("Email: ", "").strip()
        except Exception:
            task_email = "Task Account"

        await ensure_user(user_id, conn=conn)

        async with conn.transaction():
            await conn.execute("UPDATE users SET balance = balance + $1 WHERE user_id=$2", reward, user_id)
            await conn.execute("INSERT INTO transactions (user_id, type, amount, note) VALUES ($1, $2, $3, $4)", user_id, "task", reward, f"{task_email} #{task_id}")
            await conn.execute("DELETE FROM task_assignments WHERE task_id=$1", task_id)
            await conn.execute("UPDATE tasks SET status='completed' WHERE id=$1", task_id)

            referred_by = await conn.fetchval("SELECT referred_by FROM users WHERE user_id=$1", user_id)

        if REF_STATUS and referred_by and referred_by != user_id:
            await ensure_user(referred_by, conn=conn)
            ref_reward = REFERRAL_TASK_BONUS
            async with conn.transaction():
                await conn.execute("UPDATE users SET balance = balance + $1, referral_earnings = referral_earnings + $1 WHERE user_id=$2", ref_reward, referred_by)
                await conn.execute("INSERT INTO transactions (user_id, type, amount, note) VALUES ($1, $2, $3, $4)", referred_by, "referral", ref_reward, f"Referral reward from User #{user_id}")
            
            invalidate_user_cache(referred_by)

            async def notify_ref():
                ref_user_data = await get_user_data(referred_by)
                ref_amt_str = format_currency(ref_reward, ref_user_data['currency'])
                notif_text = (
                    f'<tg-emoji emoji-id="6217663806110175239">🎉</tg-emoji> Your referral <code>{user_id}</code> task gmail got approved and <b>{ref_amt_str}</b> credited to your balance!'
                )
                await send_user_notification(referred_by, notif_text, parse_mode=ParseMode.HTML)

            asyncio.create_task(notify_ref())
            
    invalidate_user_cache(user_id)
    await edit_admin_message(call, '✅ Task Approved')

    async def notify_user():
        user_data = await get_user_data(user_id)
        formatted_reward = format_currency(reward, user_data['currency'])
        await send_user_notification(user_id, f"🎉 Task #{task_id} approved!\n+{formatted_reward} added to your balance.")

    asyncio.create_task(notify_user())

@dp.callback_query(F.data.startswith("td:"))
async def decline_task(call: CallbackQuery, state: FSMContext):
    task_id = int(call.data.split(":")[1])

    async with db_pool.acquire() as conn:
        task_data = await conn.fetchrow("SELECT status FROM tasks WHERE id=$1", task_id)
        if not task_data or task_data['status'] != 'pending_review':
            await call.answer("⚠️ This request is already processed!", show_alert=True)
            return

        user_id = await conn.fetchval("SELECT user_id FROM task_assignments WHERE task_id=$1", task_id)

    if not user_id:
        return

    await call.answer()
    await state.set_state(AdminState.waiting_for_task_reject_reason)
    await state.update_data(
        task_id=task_id, 
        user_id=user_id, 
        admin_msg_id=call.message.message_id,
        is_photo=bool(call.message.photo)
    )
    await call.message.answer(f'<tg-emoji emoji-id="5447644880824181073">⚠️</tg-emoji> <b>Please reply with the reason for declining Task #{task_id}:</b>', parse_mode=ParseMode.HTML)

@dp.message(AdminState.waiting_for_task_reject_reason, ~F.text.startswith("/"), ~F.text.in_(MENU_BUTTONS))
async def process_task_reject_reason(message: Message, state: FSMContext):
    data = await state.get_data()
    task_id = data['task_id']
    user_id = data['user_id']
    admin_msg_id = data['admin_msg_id']
    is_photo = data['is_photo']
    reason = message.text.strip()

    async with db_pool.acquire() as conn:
        current_status = await conn.fetchval("SELECT status FROM tasks WHERE id=$1", task_id)
        if current_status == 'pending_review':
            async with conn.transaction():
                await conn.execute("DELETE FROM task_assignments WHERE task_id=$1", task_id)
                await conn.execute("UPDATE tasks SET status='available' WHERE id=$1", task_id)

    new_text = f'<tg-emoji emoji-id="5447644880824181073">⚠️</tg-emoji> <b>Task #{task_id} declined.</b>\n<b>Reason:</b> {reason}'
    try:
        if is_photo:
            await bot.edit_message_caption(chat_id=message.chat.id, message_id=admin_msg_id, caption=new_text, reply_markup=None, parse_mode=ParseMode.HTML)
        else:
            await bot.edit_message_text(chat_id=message.chat.id, message_id=admin_msg_id, text=new_text, reply_markup=None, parse_mode=ParseMode.HTML)
    except Exception as e:
        print(f"Error editing admin msg: {e}")

    asyncio.create_task(send_user_notification(
        user_id, 
        f'<tg-emoji emoji-id="5447644880824181073">⚠️</tg-emoji> <b>Your submission for Task #{task_id} was declined.</b>\n\n<tg-emoji emoji-id="4956475826762679249">💬</tg-emoji> <b>Reason:</b> {reason}\n\n<tg-emoji emoji-id="5251203410396458957">🛡</tg-emoji> The task has been returned to the pool.', 
        parse_mode=ParseMode.HTML
    ))

    await message.answer('<tg-emoji emoji-id="6217663806110175239">✅</tg-emoji> Rejection reason recorded and user notified.', parse_mode=ParseMode.HTML)
    await state.clear()

# ============================================
# WITHDRAWAL CALLBACKS (ADMIN SIDE)
# ============================================

@dp.callback_query(F.data.startswith("wp:"))
async def pay_withdraw(call: CallbackQuery):
    withdrawal_id = int(call.data.split(":")[1])

    async with db_pool.acquire() as conn:
        w_data = await conn.fetchrow("SELECT user_id, amount, status FROM withdrawals WHERE id=$1", withdrawal_id)
        if not w_data or w_data['status'] != 'pending':
            await call.answer("⚠️ This request is already processed!", show_alert=True)
            return

        await call.answer()
        user_id = w_data['user_id']
        payout_amount = w_data['amount']

        async with conn.transaction():
            await conn.execute("UPDATE withdrawals SET status='paid' WHERE id=$1", withdrawal_id)
            await conn.execute(
                "UPDATE transactions SET type='withdrawal', note=$1 WHERE user_id=$2 AND note LIKE $3",
                "Withdrawal paid", user_id, f"%Withdrawal #{withdrawal_id}%"
            )

    invalidate_user_cache(user_id)
    await edit_admin_message(call, '✅ Withdrawal Paid')

    async def notify_user():
        user_data = await get_user_data(user_id)
        formatted_amt = format_currency(payout_amount, user_data['currency'])
        await send_user_notification(user_id, f"🎉 Your withdrawal request of {formatted_amt} has been approved and paid!")

    asyncio.create_task(notify_user())

@dp.callback_query(F.data.startswith("wr:"))
async def reject_withdraw(call: CallbackQuery):
    withdrawal_id = int(call.data.split(":")[1])
    
    async with db_pool.acquire() as conn:
        w_data = await conn.fetchrow("SELECT user_id, amount, method, status FROM withdrawals WHERE id=$1", withdrawal_id)
        if not w_data or w_data['status'] != 'pending':
            await call.answer("⚠️ This request is already processed!", show_alert=True)
            return

        await call.answer()
        user_id = w_data['user_id']
        payout_amount = w_data['amount']
        method = (w_data['method'] or 'UPI').lower()

        fee = UPI_FEES if 'upi' in method else (USDT_FEES if 'usdt' in method else ULTRA_FEES)
        refund_total = payout_amount + fee

        async with conn.transaction():
            await conn.execute("UPDATE withdrawals SET status='rejected' WHERE id=$1", withdrawal_id)
            await conn.execute("UPDATE users SET balance = balance + $1 WHERE user_id=$2", refund_total, user_id)
            await conn.execute("DELETE FROM transactions WHERE user_id=$1 AND note LIKE $2", user_id, f"%Withdrawal #{withdrawal_id}%")
            await conn.execute(
                "INSERT INTO transactions (user_id, type, amount, note) VALUES ($1, $2, $3, $4)",
                user_id, "refund", refund_total, f"Refund for rejected withdrawal #{withdrawal_id}"
            )
            
    invalidate_user_cache(user_id)
    await edit_admin_message(call, '⚠️ Withdrawal Rejected (Balance Refunded)')
    
    async def notify_user_refund():
        user_data = await get_user_data(user_id)
        formatted_amt = format_currency(refund_total, user_data['currency'])
        await send_user_notification(
            user_id, 
            f'<tg-emoji emoji-id="5447644880824181073">⚠️</tg-emoji> Your withdrawal request #{withdrawal_id} was rejected.\n'
            f'💰 <b>{formatted_amt}</b> has been refunded back to your balance.', 
            parse_mode=ParseMode.HTML
        )

    asyncio.create_task(notify_user_refund())

# ============================================
# OPTIMIZED AUTO EXPIRE TASKS ENGINE
# ============================================

async def auto_expire_tasks():
    while True:
        try:
            expired_30m = []
            async with db_pool.acquire() as conn:
                rows_30m = await conn.fetch('''
                    SELECT ta.task_id, ta.user_id, ta.assigned_at 
                    FROM task_assignments ta
                    JOIN tasks t ON ta.task_id = t.id
                    WHERE t.status = 'assigned'
                ''')
                
                now = datetime.utcnow()
                for r in rows_30m:
                    if now - r['assigned_at'] > timedelta(minutes=30):
                        expired_30m.append((r['task_id'], r['user_id']))

                if expired_30m:
                    task_ids_30m = [t[0] for t in expired_30m]
                    async with conn.transaction():
                        await conn.execute('DELETE FROM task_assignments WHERE task_id = ANY($1::int[])', task_ids_30m)
                        await conn.execute("UPDATE tasks SET status='available' WHERE id = ANY($1::int[])", task_ids_30m)

            for task_id, user_id in expired_30m:
                asyncio.create_task(send_user_notification(
                    user_id, 
                    f'<tg-emoji emoji-id="5195033767969839232">🚀</tg-emoji> Task #{task_id} time limit expired (30 mins).\nThe task was returned to the pool.', 
                    reply_markup=get_main_menu_keyboard(), 
                    parse_mode=ParseMode.HTML
                ))

            expired_lifetime_tasks = []
            async with db_pool.acquire() as conn:
                rows_lifetime = await conn.fetch('''
                    SELECT t.id, t.title, t.details, t.created_at, ta.user_id 
                    FROM tasks t
                    LEFT JOIN task_assignments ta ON t.id = ta.task_id
                    WHERE t.status != 'completed'
                ''')

                now = datetime.utcnow()
                for r in rows_lifetime:
                    created_at = r['created_at'] or now
                    if now - created_at > timedelta(hours=23, minutes=30):
                        expired_lifetime_tasks.append({
                            'id': r['id'],
                            'details': r['details'],
                            'user_id': r['user_id']
                        })

                if expired_lifetime_tasks:
                    expired_ids = [t['id'] for t in expired_lifetime_tasks]
                    async with conn.transaction():
                        await conn.execute('DELETE FROM task_assignments WHERE task_id = ANY($1::int[])', expired_ids)
                        await conn.execute('DELETE FROM tasks WHERE id = ANY($1::int[])', expired_ids)

            for item in expired_lifetime_tasks:
                task_id = item['id']
                assigned_u = item['user_id']
                try:
                    email_str = item['details'].split(" | ")[0].replace("Email: ", "").strip()
                except Exception:
                    email_str = f"Task #{task_id}"

                admin_notice = f"⏰ <b>Task Expiry Alert:</b>\nTask #{task_id} (<code>{email_str}</code>) expired after 23h 30m and was automatically removed."
                try:
                    await bot.send_message(ADMIN_ID, admin_notice, parse_mode=ParseMode.HTML)
                except Exception:
                    pass

                if assigned_u:
                    user_notice = f"⏰ <b>Task Expired:</b>\nYour assigned task #{task_id} (<code>{email_str}</code>) has expired after 23 hours 30 minutes due to lifetime limit reached."
                    asyncio.create_task(send_user_notification(
                        assigned_u, 
                        user_notice, 
                        reply_markup=get_main_menu_keyboard(), 
                        parse_mode=ParseMode.HTML
                    ))

        except Exception as e:
            print(f"Error in background task: {e}")
            
        await asyncio.sleep(60)

# ============================================
# LONG POLLING INITIALIZER WITH FLASK THREAD
# ============================================

async def main():
    await init_db()
    await load_settings_and_cache()
    asyncio.create_task(auto_expire_tasks())
    
    server_thread = Thread(target=run_flask)
    server_thread.daemon = True
    server_thread.start()
    
    print('🤖 Bot connected to Supabase PostgreSQL and polling 24/7 on Render...')
    await dp.start_polling(bot)

if __name__ == '__main__':
    asyncio.run(main())
