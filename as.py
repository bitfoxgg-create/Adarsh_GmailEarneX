import asyncio
from datetime import datetime, timedelta
import os
from threading import Thread
import urllib.parse
import time
import re
import json
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
ADMIN_ID = int(os.environ.get('ADMIN_ID', 6237763207))
DATABASE_URL = os.environ.get('DATABASE_URL')

# Currency Conversion Rate (1 USD/USDT = 96.30 INR)
USD_TO_INR = 96.30

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

db_pool = None
BANNED_USERS_CACHE = set()
MUST_JOIN_CHANNEL = None
BOT_USERNAME = "GmailEarnexBot"
BOT_STATUS = True           # True = ON, False = OFF

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
    "➕ Add Task", "📥 Pending Reviews", "💸 Pending Withdrawals", "💬 Chat", "🗑 Unassign Tasks", "🔍 Find ID", "➕ Add Balance", 
    "➖ Cut Balance", "🔎 Check Balance", "🏆 Top Balances", "🚫 Ban User", "✅ Unban User",
    "📢 Broadcast", "🏷 Update All Rewards", "🗑 Remove Task", "💳 Transactions", "📊 View Stats",
    "📢 Must Join Channel", "🔴 Bot Status: OFF", "🟢 Bot Status: ON", "⚙️ Validator", "👑 Transfer Admin"
}

# ============================================
# DYNAMIC GMAIL VALIDATOR ENGINE
# ============================================

def get_provider_url() -> str:
    """Returns the API endpoint format string for the current provider."""
    if VALIDATOR_PROVIDER == "emailable":
        return "https://api.emailable.com/v1/verify?email={email}&api_key={key}"
    return "https://api.myemailverifier.com/api/validate_single.php?apikey={key}&email={email}"

async def is_gmail_registered(email: str, user_id: int = None) -> bool:
    """Verifies Gmail account existence with auto-cleanup of verification notice."""
    email = email.strip().lower()
    
    if not email.endswith("@gmail.com"):
        return False

    username = email[:-10]  # Strip '@gmail.com'

    # 1. Strict Google Account Syntax Validation
    if len(username) < 6 or len(username) > 30:
        return False

    if not re.match(r'^[a-z0-9.]+$', username):
        return False

    if username.startswith('.') or username.endswith('.') or '..' in username:
        return False

    # 2. Check if Validator is globally enabled by admin
    if not VALIDATOR_ENABLED:
        return True

    # Temporary verification message sent to user
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

    # 3. Dynamic Multi-Provider Verification
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
                        else: # myemailverifier
                            status_val = lower_data.get("status") or lower_data.get("addressstatus") or lower_data.get("statuscode") or ""
                            diagnosis_val = lower_data.get("diagnosis", "")
                            if status_val in ["valid", "1", "deliverable", "ok", "true"] or "exists" in diagnosis_val or "active" in diagnosis_val:
                                is_valid_email = True
                else:
                    print(f"Validator HTTP Error ({VALIDATOR_PROVIDER}): {resp.status}")
    except Exception as e:
        print(f"Validator Exception ({VALIDATOR_PROVIDER}): {e}")

    # Auto-delete the "Verifying..." message to keep chat clean
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
    waiting_for_update_rewards = State()
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
                notifications_enabled BOOLEAN DEFAULT TRUE,
                currency TEXT DEFAULT 'USD',
                referred_by BIGINT DEFAULT NULL,
                referral_earnings DOUBLE PRECISION DEFAULT 0
            )
        ''')
        await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS upi TEXT DEFAULT 'None'")
        await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS usdt_address TEXT DEFAULT 'None'")
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
                status TEXT DEFAULT 'available'
            )
        ''')
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS task_assignments (
                task_id INT UNIQUE, 
                user_id BIGINT, 
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
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS bot_settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        ''')

async def load_settings_and_cache():
    global BANNED_USERS_CACHE, MUST_JOIN_CHANNEL, BOT_USERNAME, BOT_STATUS, EMAILABLE_API_KEY, VALIDATOR_ENABLED, VALIDATOR_PROVIDER, ADMIN_ID
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT user_id FROM banned_users")
        BANNED_USERS_CACHE = {r['user_id'] for r in rows}
        
        channel_val = await conn.fetchval("SELECT value FROM bot_settings WHERE key='must_join_channel'")
        MUST_JOIN_CHANNEL = channel_val if channel_val else None

        status_val = await conn.fetchval("SELECT value FROM bot_settings WHERE key='bot_status'")
        BOT_STATUS = (status_val != 'off')

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
            "INSERT INTO users (user_id, balance, upi, usdt_address, notifications_enabled, currency) VALUES ($1, 0, 'None', 'None', TRUE, 'USD') ON CONFLICT (user_id) DO NOTHING", 
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
        async with db_pool.acquire() as c:
            await _run_ensure(c)

    return is_new

async def get_user_data(user_id: int):
    if user_id in USER_CACHE:
        return USER_CACHE[user_id]
        
    await ensure_user(user_id)
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT balance, upi, usdt_address, notifications_enabled, currency, referred_by, referral_earnings FROM users WHERE user_id=$1", user_id)
        if row:
            USER_CACHE[user_id] = dict(row)
        return row

async def get_balance(user_id: int) -> float:
    data = await get_user_data(user_id)
    return data['balance'] if data else 0.0

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
    kb.button(text=curr_text, callback_data="toggle_currency", icon_custom_emoji_id="589336546
