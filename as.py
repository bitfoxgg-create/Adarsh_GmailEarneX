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
BOT_USERNAME = "Gmailpaybot"
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

def get_referral_inline_keyboard(user_id: int):
    invite_link = f"https://t.me/{BOT_USERNAME}?start={user_id}"
    custom_share_text = "🚀Join Earnex Bot and Start Earning Money!✅"
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
        icon_custom_emoji_id="6039539366177541657"
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
        icon_custom_emoji_id="6039539366177541657"
    )
    kb.adjust(1, 1, 1)
    return kb.as_markup()

def get_admin_menu_keyboard():
    kb = ReplyKeyboardBuilder()
    kb.button(text="➕ Add Task", style="success")
    kb.button(text="📥 Pending Reviews", style="primary")
    kb.button(text="💸 Pending Withdrawals", style="primary")
    kb.button(text="💬 Chat", style="primary")
    kb.button(text="🗑 Unassign Tasks", style="danger")
    kb.button(text="🔍 Find ID", style="primary")
    kb.button(text="➕ Add Balance", style="success")
    kb.button(text="➖ Cut Balance", style="danger")
    kb.button(text="🔎 Check Balance", style="primary")
    kb.button(text="🏆 Top Balances", style="primary")
    kb.button(text="🚫 Ban User", style="danger")
    kb.button(text="✅ Unban User", style="success")
    kb.button(text="📢 Broadcast", style="primary")
    kb.button(text="🏷 Update All Rewards", style="primary")
    kb.button(text="🗑 Remove Task", style="danger")
    kb.button(text="💳 Transactions", style="primary")
    kb.button(text="📊 View Stats", style="primary")
    kb.button(text="📢 Must Join Channel", style="primary")
    
    status_btn_text = "🟢 Bot Status: ON" if BOT_STATUS else "🔴 Bot Status: OFF"
    kb.button(text=status_btn_text, style="danger" if BOT_STATUS else "success")
    kb.button(text="⚙️ Validator", style="primary")
    
    # Transfer Admin button in new row above Main Menu
    kb.button(text="👑 Transfer Admin", style="danger")
    kb.button(text="🏠 Main Menu", style="primary")
    kb.adjust(2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 1, 1)
    return kb.as_markup(resize_keyboard=True)

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

def get_balance_inline_keyboard(upi_set: bool, usdt_set: bool):
    kb = InlineKeyboardBuilder()
    upi_link_text = "Change UPI" if upi_set else "Link UPI"
    usdt_link_text = "Change USDT BEP-20" if usdt_set else "Link USDT BEP-20"
    
    upi_emoji = "6278557702109013266" if upi_set else "5902449142575141204"
    usdt_emoji = "5197434882321567830" if usdt_set else "5902449142575141204"

    kb.button(text=upi_link_text, callback_data="link_upi", icon_custom_emoji_id=upi_emoji, style="primary")
    kb.button(text=usdt_link_text, callback_data="link_usdt", icon_custom_emoji_id=usdt_emoji, style="primary")
    kb.button(
        text="Withdraw", 
        callback_data="choose_withdraw_method", 
        icon_custom_emoji_id="5444856076954520455",
        style="success"
    )
    kb.button(
        text="Back",
        callback_data="menu_back",
        icon_custom_emoji_id="6039539366177541657"
    )
    kb.adjust(2, 1, 1)
    return kb.as_markup()

def get_withdraw_options_keyboard():
    kb = InlineKeyboardBuilder()
    kb.button(text="Withdraw via UPI", callback_data="withdraw_upi", icon_custom_emoji_id="6278557702109013266", style="success")
    kb.button(text="Withdraw via USDT BEP-20", callback_data="withdraw_usdt", icon_custom_emoji_id="5197434882321567830", style="success")
    kb.button(
        text="Back",
        callback_data="menu_balance",
        icon_custom_emoji_id="6039539366177541657"
    )
    kb.adjust(1, 1, 1)
    return kb.as_markup()

def get_back_inline_keyboard():
    kb = InlineKeyboardBuilder()
    kb.button(
        text="Back",
        callback_data="menu_back",
        icon_custom_emoji_id="6039539366177541657"
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
        icon_custom_emoji_id="6039539366177541657"
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
                '<tg-emoji emoji-id="5458904472598095631">👋</tg-emoji> <b>Welcome to Gmail Pay Bot!</b>\n\n'
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
    try:
        await call.answer()
    except Exception:
        pass

@dp.callback_query(F.data == "menu_referrals")
async def cb_referrals(call: CallbackQuery, state: FSMContext):
    await state.clear()
    user_id = call.from_user.id
    user_data = await get_user_data(user_id)
    curr = user_data['currency'] if user_data else "USD"

    async with db_pool.acquire() as conn:
        invited_users_count = await conn.fetchval(
            "SELECT COUNT(*) FROM users WHERE referred_by=$1", user_id
        ) or 0
        
        approved_ref_accounts = await conn.fetchval('''
            SELECT COUNT(*) FROM transactions t
            JOIN users u ON t.user_id = u.user_id
            WHERE u.referred_by = $1 AND t.type IN ('sell', 'task')
        ''', user_id) or 0

        total_earnings = user_data['referral_earnings'] if user_data else 0.0

    formatted_earnings = format_currency(total_earnings, curr)
    invite_link = f"https://t.me/{BOT_USERNAME}?start={user_id}"

    rate_sell = format_currency(5.0, curr)
    rate_task = format_currency(7.0, curr)

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
    try:
        await call.answer()
    except Exception:
        pass

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

    kb.row(InlineKeyboardButton(text="⬅️ Back", callback_data="menu_back", icon_custom_emoji_id="6039539366177541657"))

    return text, kb.as_markup()

@dp.callback_query(F.data == "menu_my_accounts")
async def cb_my_accounts(call: CallbackQuery, state: FSMContext):
    await state.clear()
    text, reply_markup = await render_my_accounts_page(call.from_user.id, page=1)
    try:
        await call.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
    except Exception:
        await call.message.answer(text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
    await state.update_data(last_menu_msg_id=call.message.message_id)
    try:
        await call.answer()
    except Exception:
        pass

@dp.callback_query(F.data.startswith("myacc_page:"))
async def cb_my_accounts_page(call: CallbackQuery):
    page = int(call.data.split(":")[1])
    text, reply_markup = await render_my_accounts_page(call.from_user.id, page=page)
    try:
        await call.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
    except Exception:
        pass
    try:
        await call.answer()
    except Exception:
        pass

@dp.callback_query(F.data == "noop")
async def cb_noop(call: CallbackQuery):
    try:
        await call.answer()
    except Exception:
        pass

@dp.callback_query(F.data == "menu_settings")
async def cb_settings(call: CallbackQuery, state: FSMContext):
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
    try:
        await call.answer()
    except Exception:
        pass

@dp.callback_query(F.data == "toggle_notif")
async def cb_toggle_notif(call: CallbackQuery):
    user_data = await get_user_data(call.from_user.id)
    current_notif = user_data['notifications_enabled']
    new_notif = not current_notif
    
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE users SET notifications_enabled=$1 WHERE user_id=$2", new_notif, call.from_user.id)
        
    invalidate_user_cache(call.from_user.id)
    status_str = "ENABLED" if new_notif else "DISABLED"
    try:
        await call.answer(f"Notifications are now {status_str}", show_alert=True)
    except Exception:
        pass
    
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
    try:
        await call.answer(f"Currency updated to {new_curr} ({symbol})", show_alert=True)
    except Exception:
        pass
    
    try:
        await call.message.edit_reply_markup(reply_markup=get_settings_keyboard(user_data['notifications_enabled'], new_curr))
    except:
        pass

@dp.callback_query(F.data == "menu_get_task")
async def cb_get_task(call: CallbackQuery, state: FSMContext):
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
        ''', user_id)
        
        if existing:
            task_id = existing['id']
            assigned_time = existing['assigned_at']
            task_status = existing['status']
            
            if task_status == 'pending_review':
                txt = '<tg-emoji emoji-id="5195033767969839232">🚀</tg-emoji> Your task submission is currently under admin review. Please wait for approval.'
                try:
                    await call.message.edit_text(txt, reply_markup=get_main_menu_keyboard(), parse_mode=ParseMode.HTML)
                except:
                    await call.message.answer(txt, reply_markup=get_main_menu_keyboard(), parse_mode=ParseMode.HTML)
                await state.update_data(last_menu_msg_id=call.message.message_id)
                try:
                    await call.answer()
                except Exception:
                    pass
                return

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
                except:
                    await call.message.answer(txt, parse_mode=ParseMode.HTML, reply_markup=get_task_action_keyboard())
                await state.update_data(last_menu_msg_id=call.message.message_id)
                try:
                    await call.answer()
                except Exception:
                    pass
                return
            else:
                async with conn.transaction():
                    await conn.execute('DELETE FROM task_assignments WHERE user_id=$1', user_id)
                    await conn.execute('UPDATE tasks SET status=$1 WHERE id=$2', 'available', task_id)

        task = await conn.fetchrow("SELECT id, title, details, reward FROM tasks WHERE status='available' ORDER BY RANDOM() LIMIT 1")
        if not task:
            txt = '📭 No tasks available right now.'
            try:
                await call.message.edit_text(txt, reply_markup=get_main_menu_keyboard())
            except:
                await call.message.answer(txt, reply_markup=get_main_menu_keyboard())
            await state.update_data(last_menu_msg_id=call.message.message_id)
            try:
                await call.answer()
            except Exception:
                pass
            return
        
        task_id = task['id']
        title = task['title']
        details = task['details']
        reward = task['reward']
        
        async with conn.transaction():
            await conn.execute("UPDATE tasks SET status='assigned' WHERE id=$1", task_id)
            await conn.execute('INSERT INTO task_assignments(task_id, user_id) VALUES ($1, $2)', task_id, user_id)

    try:
        parts = details.split(" | ")
        username = parts[0].replace("Email: ", "").strip()
        password = parts[1].replace("Pass: ", "").strip()
    except:
        username = title.replace("Login to ", "")
        password = "See Admin"

    reward_str = format_currency(reward, user_curr)
    txt = (
        f'<tg-emoji emoji-id="5310278924616356636">🎯</tg-emoji> <b>Task #{task_id}</b>\n\n'
        f'<tg-emoji emoji-id="5870458774455587120">👤</tg-emoji> <b>Email:</b> {username} | <tg-emoji emoji-id="6005570495603282482">🔑</tg-emoji> <b>Password:</b> <code>{password}</code>\n'
        f'<tg-emoji emoji-id="5417924076503062111">💰</tg-emoji> <b>Reward:</b> {reward_str}\n\n'
        f'<tg-emoji emoji-id="5195033767969839232">🚀</tg-emoji> You have ONLY 30 MINUTES to complete this task.'
    )
    try:
        await call.message.edit_text(txt, parse_mode=ParseMode.HTML, reply_markup=get_task_action_keyboard())
    except:
        await call.message.answer(txt, parse_mode=ParseMode.HTML, reply_markup=get_task_action_keyboard())
    await state.update_data(last_menu_msg_id=call.message.message_id)
    try:
        await call.answer()
    except Exception:
        pass

@dp.callback_query(F.data == "menu_balance")
async def cb_balance(call: CallbackQuery, state: FSMContext):
    await state.clear()
    user_data = await get_user_data(call.from_user.id)
    bal = user_data['balance'] if user_data else 0.0
    upi = user_data['upi'] if user_data and user_data['upi'] else "None"
    usdt = user_data['usdt_address'] if user_data and user_data['usdt_address'] else "None"
    curr = user_data['currency'] if user_data else "USD"
    
    upi_set = upi != "None" and upi != ""
    usdt_set = usdt != "None" and usdt != ""
    formatted_bal = format_currency(bal, curr)
    
    text = (
        f'<tg-emoji emoji-id="5445353829304387411">💳</tg-emoji> <b>Balance</b>\n\n'
        f'<tg-emoji emoji-id="5278467510604160626">💵</tg-emoji> <b>Available:</b> {formatted_bal}\n'
        f'<tg-emoji emoji-id="6278557702109013266">🏦</tg-emoji> <b>UPI:</b> <code>{upi}</code>\n'
        f'<tg-emoji emoji-id="5197434882321567830">🪙</tg-emoji> <b>USDT BEP-20:</b> <code>{usdt}</code>'
    )
    
    try:
        await call.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=get_balance_inline_keyboard(upi_set, usdt_set))
    except Exception:
        await call.message.answer(text, parse_mode=ParseMode.HTML, reply_markup=get_balance_inline_keyboard(upi_set, usdt_set))
    await state.update_data(last_menu_msg_id=call.message.message_id)
    try:
        await call.answer()
    except Exception:
        pass

@dp.callback_query(F.data == "menu_sell_gmail")
async def cb_sell_gmail(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(UserState.selling_username)
    user_data = await get_user_data(call.from_user.id)
    rate_str = format_currency(30.0, user_data['currency'])
    txt = (
        f'<tg-emoji emoji-id="5445221832074483553">🏷️</tg-emoji> <b>Sell Price {rate_str}/Gmail</b>\n\n'
        '<tg-emoji emoji-id="5377548235709619284">🤑</tg-emoji> <b>Step 1/2:</b> Please send the Gmail <b>Username</b> (e.g., <code>example@gmail.com</code>):'
    )
    try:
        await call.message.edit_text(txt, parse_mode=ParseMode.HTML, reply_markup=get_back_inline_keyboard())
    except:
        await call.message.answer(txt, parse_mode=ParseMode.HTML, reply_markup=get_back_inline_keyboard())
    await state.update_data(last_menu_msg_id=call.message.message_id)
    try:
        await call.answer()
    except Exception:
        pass

@dp.message(UserState.selling_username, F.text, ~F.text.startswith("/"), ~F.text.in_(MENU_BUTTONS))
async def process_sell_username(message: Message, state: FSMContext):
    username_input = message.text.strip()
    if "@gmail.com" not in username_input.lower() and "@" not in username_input:
        username = f"{username_input}@gmail.com"
    else:
        username = username_input

    search_pattern = f"%{username.lower()}%"

    # FIRST: Check database to save API credits
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

    # SECOND: Perform Real-Time Verification via API only if not in database
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
    password = message.text.strip()
    data = await state.get_data()
    username = data.get('sell_username')
    user_id = message.from_user.id
    rate = 30.0

    details = f"Username: {username}\nPassword: {password}"

    async with db_pool.acquire() as conn:
        sell_id = await conn.fetchval(
            "INSERT INTO pending_sells (user_id, details, amount) VALUES ($1, $2, $3) RETURNING id",
            user_id, details, rate
        )

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Approve", callback_data=f"sellapprove_db:{sell_id}:{user_id}:{rate}", icon_custom_emoji_id="6217663806110175239", style="success"),
        InlineKeyboardButton(text="Decline", callback_data=f"selldecline_db:{sell_id}:{user_id}", icon_custom_emoji_id="5274099962655816924", style="danger")
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

    sent_msg = await message.answer(
        f'<tg-emoji emoji-id="6217663806110175239">✅</tg-emoji> Your Gmail sell account details (Request #{sell_id}) have been sent for admin review.\n\n'
        f'<tg-emoji emoji-id="5447644880824181073">⚠️</tg-emoji> <b>Important:</b> Please make sure to <b>logout</b> of this account from your device!', 
        reply_markup=get_main_menu_keyboard(), 
        parse_mode=ParseMode.HTML
    )
    await state.clear()
    await state.update_data(last_menu_msg_id=sent_msg.message_id)

@dp.message(Command("history"), StateFilter("*"))
async def history(message: Message, state: FSMContext):
    await state.clear()
    user_data = await get_user_data(message.from_user.id)
    curr = user_data['currency']
    
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT type, amount, note, created_at FROM transactions WHERE user_id=$1 ORDER BY id DESC LIMIT 10", message.from_user.id)
    if not rows:
        sent_msg = await message.answer("📭 No transactions found.", reply_markup=get_back_inline_keyboard())
        await state.update_data(last_menu_msg_id=sent_msg.message_id)
        return
    text = '<tg-emoji emoji-id="5440410042773824003">📜</tg-emoji> <b>Last Transactions</b>\n\n'
    for r in rows:
        sign = "+" if r['amount'] >= 0 else ""
        formatted_amt = format_currency(abs(r['amount']), curr)
        text += f"• {sign}{formatted_amt} | {r['type']}\n{r['note']}\n{r['created_at'].strftime('%Y-%m-%d %H:%M:%S')}\n\n"
    sent_msg = await message.answer(text, parse_mode=ParseMode.HTML, reply_markup=get_back_inline_keyboard())
    await state.update_data(last_menu_msg_id=sent_msg.message_id)

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

@dp.message(F.text.in_({"🔴 Bot Status: OFF", "🟢 Bot Status: ON"}), StateFilter("*"))
async def admin_btn_toggle_status(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    global BOT_STATUS
    BOT_STATUS = not BOT_STATUS
    new_val = 'on' if BOT_STATUS else 'off'

    async with db_pool.acquire() as conn:
        await conn.execute("INSERT INTO bot_settings (key, value) VALUES ('bot_status', $1) ON CONFLICT (key) DO UPDATE SET value = $1", new_val)

    status_str = "🟢 <b>Bot is now ONLINE and ENABLED for all users!</b>" if BOT_STATUS else "🔴 <b>Bot is now OFF and DISABLED for normal users!</b>"
    await message.answer(status_str, parse_mode=ParseMode.HTML, reply_markup=get_admin_menu_keyboard())

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

    try:
        await call.answer(f"Validator status updated!", show_alert=True)
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

    try:
        await call.answer(f"Switched provider to {provider_name}!", show_alert=True)
    except Exception:
        pass

@dp.callback_query(F.data == "admin_validator_change_key")
async def cb_admin_validator_change_key(call: CallbackQuery, state: FSMContext):
    try:
        await call.answer()
    except Exception:
        pass

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
        "Send the numeric **User ID** of the user you want to transfer full adminship to:\n\n"
        "<i>⚠️ Warning: Once transferred, your current user ID will no longer have access to the admin panel!</i>",
        parse_mode=ParseMode.MARKDOWN
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

        # Update database settings and in-memory variable
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
            f"👑 <b>Congratulations!</b>\n\nYou have been promoted to the **Full Admin** of Gmail Pay Bot by User ID `{old_admin_id}`.\n\nUse /adminpanel to open the control panel.",
            parse_mode=ParseMode.MARKDOWN
        ))

    except ValueError:
        await message.answer("❌ Invalid User ID. Please enter a valid numeric Telegram ID.", reply_markup=get_admin_menu_keyboard())

    await state.clear()

@dp.message(F.text == "➕ Add Task", StateFilter("*"))
async def admin_btn_add_task(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.set_state(AdminState.waiting_for_add_task)
    await message.answer("📧 Send the email/username to add as a task (e.g. `example@gmail.com`):", parse_mode=ParseMode.MARKDOWN)

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
            icon_custom_emoji_id="6039539366177541657", 
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
    password = "TaskVerse@#"
    default_reward = 50.0 
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
    data = await state.get_data()
    username = data.get("pending_add_username")
    if username:
        try:
            await call.message.delete()
        except Exception:
            pass
        await insert_new_task(call.message, username)
    await state.clear()
    try:
        await call.answer()
    except Exception:
        pass

@dp.callback_query(F.data == "cancel_add_duplicate_task")
async def cb_cancel_add_duplicate_task(call: CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        await call.message.edit_text("❌ Task addition cancelled.", reply_markup=None)
    except Exception:
        pass
    await call.message.answer("🏠 Returned to Admin Menu.", reply_markup=get_admin_menu_keyboard())
    try:
        await call.answer()
    except Exception:
        pass

@dp.message(F.text == "📥 Pending Reviews", StateFilter("*"))
async def admin_btn_pending_reviews(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.clear()
        
    async with db_pool.acquire() as conn:
        task_rows = await conn.fetch('''
            SELECT t.id, t.title, t.details, t.reward, ta.user_id 
            FROM tasks t 
            JOIN task_assignments ta ON t.id = ta.task_id 
            WHERE t.status = 'pending_review'
            ORDER BY ta.assigned_at ASC
        ''')
        
        sell_rows = await conn.fetch('''
            SELECT id, user_id, details, amount 
            FROM pending_sells 
            WHERE status = 'pending_review'
            ORDER BY created_at ASC
        ''')

    total_pending = len(task_rows) + len(sell_rows)
        
    if total_pending == 0:
        await message.answer("📭 <b>No pending reviews (tasks or sell requests) found!</b>", parse_mode=ParseMode.HTML, reply_markup=get_admin_menu_keyboard())
        return

    await message.answer(f"📥 <b>Found {total_pending} pending item(s). Displaying below:</b>", parse_mode=ParseMode.HTML)

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
            password = "TaskVerse@#"
        
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text='Approve', callback_data=f'taskapprove:{task_id}:{user_id}:{reward}', icon_custom_emoji_id="6217663806110175239", style="success"),
            InlineKeyboardButton(text='Decline', callback_data=f'taskdecline:{task_id}:{user_id}', icon_custom_emoji_id="5274099962655816924", style="danger")
        ]])
        
        await message.answer(
            f'<tg-emoji emoji-id="5206607081334906820">📤</tg-emoji> <b>Pending Task Submission</b>\n\n'
            f'<tg-emoji emoji-id="5870458774455587120">👤</tg-emoji> <b>User ID:</b> <code>{user_id}</code>\n'
            f'<tg-emoji emoji-id="5197269100878907942">✍️</tg-emoji> <b>Task #{task_id}</b>\n'
            f'📧 <b>Email:</b> <code>{email}</code>\n'
            f'<tg-emoji emoji-id="6005570495603282482">🔑</tg-emoji> <b>Password:</b> <code>{password}</code>\n'
            f'<tg-emoji emoji-id="5417924076503062111">💰</tg-emoji> <b>Reward:</b> ₹{reward}',
            reply_markup=kb,
            parse_mode=ParseMode.HTML
        )

    for r in sell_rows:
        sell_id = r['id']
        user_id = r['user_id']
        details = r['details']
        amount = r['amount']

        try:
            lines = details.split("\n")
            username = lines[0].replace("Username: ", "").strip()
            password = lines[1].replace("Password: ", "").strip()
            
            if "@gmail.com" not in username.lower() and "@" not in username:
                username += "@gmail.com"
                
            formatted_details = f"📧 <b>Username:</b> <code>{username}</code>\n<tg-emoji emoji-id=\"6005570495603282482\">🔑</tg-emoji> <b>Password:</b> <code>{password}</code>"
        except Exception:
            formatted_details = f"<code>{details}</code>"

        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="Approve", callback_data=f"sellapprove_db:{sell_id}:{user_id}:{amount}", icon_custom_emoji_id="6217663806110175239", style="success"),
            InlineKeyboardButton(text="Decline", callback_data=f"selldecline_db:{sell_id}:{user_id}", icon_custom_emoji_id="5274099962655816924", style="danger")
        ]])

        await message.answer(
            f'<tg-emoji emoji-id="5377548235709619284">📦</tg-emoji> <b>Pending Gmail Sell Request #{sell_id}</b>\n\n'
            f'<tg-emoji emoji-id="5870458774455587120">👤</tg-emoji> <b>User ID:</b> <code>{user_id}</code>\n'
            f'<tg-emoji emoji-id="5417924076503062111">💰</tg-emoji> <b>Rate:</b> ₹{amount:.2f}\n\n'
            f'📝 <b>Details:</b>\n{formatted_details}',
            reply_markup=kb,
            parse_mode=ParseMode.HTML
        )

@dp.message(F.text == "💸 Pending Withdrawals", StateFilter("*"))
async def admin_btn_pending_withdrawals(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.clear()

    async with db_pool.acquire() as conn:
        withdraw_rows = await conn.fetch('''
            SELECT id, user_id, amount, method, payment_address, created_at
            FROM withdrawals
            WHERE status = 'pending'
            ORDER BY created_at ASC
        ''')

    if not withdraw_rows:
        await message.answer("📭 <b>No pending withdrawal requests found!</b>", parse_mode=ParseMode.HTML, reply_markup=get_admin_menu_keyboard())
        return

    await message.answer(f"💸 <b>Found {len(withdraw_rows)} pending withdrawal request(s). Displaying below:</b>", parse_mode=ParseMode.HTML)

    for r in withdraw_rows:
        withdraw_id = r['id']
        user_id = r['user_id']
        amount = r['amount']
        method = r['method'] or 'UPI'
        payment_address = r['payment_address'] or 'None'
        
        extra_usdt_info = f" (~${(amount / USD_TO_INR):.2f} USDT)" if method == "USDT BEP-20" else ""

        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(
                text="Pay", 
                callback_data=f"pay:{withdraw_id}:{user_id}:{amount}", 
                icon_custom_emoji_id="5444856076954520455", 
                style="success"
            ),
            InlineKeyboardButton(
                text="Reject", 
                callback_data=f"reject:{withdraw_id}:{user_id}", 
                icon_custom_emoji_id="5274099962655816924", 
                style="danger"
            )
        ]])

        address_emoji = '<tg-emoji emoji-id="6152069549442208798">🏦</tg-emoji>' if method == 'UPI' else '<tg-emoji emoji-id="5197434882321567830">🪙</tg-emoji>'

        await message.answer(
            f'<tg-emoji emoji-id="5417924076503062111">💰</tg-emoji> <b>WITHDRAWAL REQUEST #{withdraw_id}</b>\n\n'
            f'<tg-emoji emoji-id="5197269100878907942">✍️</tg-emoji> <b>User ID:</b> <code>{user_id}</code>\n'
            f'💳 <b>Method:</b> <code>{method}</code>\n'
            f'<tg-emoji emoji-id="5417924076503062111">💰</tg-emoji> <b>Amount:</b> ₹{amount:.2f}{extra_usdt_info}\n'
            f'{address_emoji} <b>Address:</b> <code>{payment_address}</code>\n'
            f'📅 <b>Date:</b> {r["created_at"].strftime("%Y-%m-%d %H:%M:%S")}',
            reply_markup=kb,
            parse_mode=ParseMode.HTML
        )

@dp.message(F.text == "💬 Chat", StateFilter("*"))
async def admin_btn_chat(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.set_state(AdminState.waiting_for_chat_user_id)
    await message.answer("💬 Send the numeric **User ID** you want to message:", parse_mode=ParseMode.MARKDOWN)

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

@dp.message(F.text == "🔍 Find ID", StateFilter("*"))
async def admin_btn_find_id(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.set_state(AdminState.waiting_for_find_id_query)
    await message.answer(
        "🔍 <b>Find Task & User ID</b>\n\n"
        "Please send the Gmail username (e.g., <code>jhon</code> without @gmail.com):",
        parse_mode=ParseMode.HTML
    )

@dp.callback_query(F.data == "unassign_by_user_id")
async def start_unassign_user_id(call: CallbackQuery, state: FSMContext):
    try:
        await call.answer()
    except Exception:
        pass
    await state.set_state(AdminState.waiting_for_unassign_user_id)
    await call.message.answer("👤 Send the numeric **User ID** whose task you want to unassign:", parse_mode=ParseMode.MARKDOWN)

@dp.callback_query(F.data == "unassign_all_users")
async def process_unassign_all_users(call: CallbackQuery):
    try:
        await call.answer()
    except Exception:
        pass

    async with db_pool.acquire() as conn:
        assigned_tasks = await conn.fetch('''
            SELECT ta.task_id 
            FROM task_assignments ta 
            JOIN tasks t ON ta.task_id = t.id 
            WHERE t.status = 'assigned'
        ''')
        
        if not assigned_tasks:
            try:
                await call.answer("📭 No active assigned tasks found to unassign!", show_alert=True)
            except Exception:
                pass
            return

        task_ids = [r['task_id'] for r in assigned_tasks]
        
        async with conn.transaction():
            await conn.execute("DELETE FROM task_assignments WHERE task_id = ANY($1::int[])", task_ids)
            await conn.execute("UPDATE tasks SET status='available' WHERE id = ANY($1::int[])", task_ids)

    await edit_admin_message(call, "✅ <b>Successfully unassigned task(s) and returned them to the pool.</b>")

@dp.message(F.text == "➕ Add Balance", StateFilter("*"))
async def admin_btn_add_balance(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.set_state(AdminState.waiting_for_add_balance)
    await message.answer("💰 Send the User ID and Amount separated by space:\n\n<i>Example: 123456789 50</i>", parse_mode=ParseMode.HTML)

@dp.message(F.text == "➖ Cut Balance", StateFilter("*"))
async def admin_btn_cut_balance(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.set_state(AdminState.waiting_for_cut_balance)
    await message.answer("⚠️ Send the User ID and Amount to deduct separated by space:\n\n<i>Example: 123456789 20</i>", parse_mode=ParseMode.HTML)

@dp.message(F.text == "🔎 Check Balance", StateFilter("*"))
async def admin_btn_check_balance(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.set_state(AdminState.waiting_for_check_balance)
    await message.answer("🔎 Send the numeric User ID to check:", parse_mode=ParseMode.MARKDOWN)

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

@dp.message(F.text == "✅ Unban User", StateFilter("*"))
async def admin_btn_unban_user(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.set_state(AdminState.waiting_for_unban_user)
    await message.answer("✅ Send the numeric User ID to unban:", parse_mode=ParseMode.MARKDOWN)

@dp.message(F.text == "📢 Broadcast", StateFilter("*"))
async def admin_btn_broadcast(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.set_state(AdminState.waiting_for_broadcast)
    await message.answer("📢 Send or forward the broadcast message below:", parse_mode=ParseMode.MARKDOWN)

@dp.message(F.text == "🏷 Update All Rewards", StateFilter("*"))
async def admin_btn_update_rewards(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.set_state(AdminState.waiting_for_update_rewards)
    await message.answer("💰 Send the new reward amount for ALL tasks (e.g. `40.0`):", parse_mode=ParseMode.MARKDOWN)

@dp.message(F.text == "🗑 Remove Task", StateFilter("*"))
async def admin_btn_remove_task(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.set_state(AdminState.waiting_for_remove_task)
    await message.answer("🗑 Send the Task ID to remove (e.g. `3`):", parse_mode=ParseMode.MARKDOWN)

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

# ============================================
# INPUT PROCESSORS FOR STATES
# ============================================

@dp.message(AdminState.waiting_for_find_id_query, ~F.text.startswith("/"), ~F.text.in_(MENU_BUTTONS))
async def process_find_id_step(message: Message, state: FSMContext):
    query = message.text.strip().lower()
    search_term = f"%{query}%"

    async with db_pool.acquire() as conn:
        task = await conn.fetchrow('''
            SELECT t.id, t.title, t.details, t.status, ta.user_id 
            FROM tasks t
            LEFT JOIN task_assignments ta ON t.id = ta.task_id
            WHERE LOWER(t.title) LIKE $1 OR LOWER(t.details) LIKE $1
            LIMIT 1
        ''', search_term)

    if not task:
        await message.answer(f"❌ No task found matching username query: <code>{query}</code>", parse_mode=ParseMode.HTML, reply_markup=get_admin_menu_keyboard())
        await state.clear()
        return

    task_id = task['id']
    status = task['status']
    assigned_user_id = task['user_id']
    user_str = f"<code>{assigned_user_id}</code>" if assigned_user_id else "<i>None (Unassigned)</i>"

    text = (
        f"🔍 <b>Task Lookup Result</b>\n\n"
        f"📌 <b>Task ID:</b> <code>#{task_id}</code>\n"
        f"📊 <b>Status:</b> <code>{status}</code>\n"
        f"👤 <b>Assigned User ID:</b> {user_str}\n"
        f"📄 <b>Details:</b> <code>{task['details']}</code>"
    )

    await message.answer(text, parse_mode=ParseMode.HTML, reply_markup=get_admin_menu_keyboard())
    await state.clear()

@dp.message(AdminState.waiting_for_unassign_user_id, ~F.text.startswith("/"), ~F.text.in_(MENU_BUTTONS))
async def process_unassign_user_id_step(message: Message, state: FSMContext):
    try:
        target_id = int(message.text.strip())
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow('''
                SELECT ta.task_id, t.status 
                FROM task_assignments ta 
                JOIN tasks t ON ta.task_id = t.id 
                WHERE ta.user_id=$1
            ''', target_id)

            if not row:
                await message.answer(f"❌ User `{target_id}` does not have any active assigned task.", parse_mode=ParseMode.MARKDOWN, reply_markup=get_admin_menu_keyboard())
                await state.clear()
                return

            if row['status'] == 'pending_review':
                await message.answer(f"⚠️ Cannot unassign task for User `{target_id}` because it has already been submitted for review.", parse_mode=ParseMode.MARKDOWN, reply_markup=get_admin_menu_keyboard())
                await state.clear()
                return

            task_id = row['task_id']
            async with conn.transaction():
                await conn.execute("DELETE FROM task_assignments WHERE user_id=$1", target_id)
                await conn.execute("UPDATE tasks SET status='available' WHERE id=$1", task_id)

        await message.answer(f"✅ **Task #{task_id}** held by User `{target_id}` has been unassigned and returned to the pool.", parse_mode=ParseMode.MARKDOWN, reply_markup=get_admin_menu_keyboard())
        asyncio.create_task(send_user_notification(target_id, f"⚠️ Your current Task #{task_id} has been unassigned by the admin and returned to the pool."))
    except ValueError:
        await message.answer("❌ Invalid User ID. Please enter a valid numeric ID.", parse_mode=ParseMode.MARKDOWN)

    await state.clear()

@dp.message(UserState.setting_upi, F.text, ~F.text.startswith("/"), ~F.text.in_(MENU_BUTTONS))
async def process_link_upi(message: Message, state: FSMContext):
    upi_input = message.text.strip()
    if "@" not in upi_input or len(upi_input) < 5:
        await message.answer('<tg-emoji emoji-id="5274099962655816924">❗️</tg-emoji> Invalid UPI ID format. Please send a valid UPI ID (e.g. <code>yourname@upi</code>).', parse_mode=ParseMode.HTML)
        return

    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE users SET upi=$1 WHERE user_id=$2", upi_input, message.from_user.id)

    invalidate_user_cache(message.from_user.id)
    sent_msg = await message.answer(f'<tg-emoji emoji-id="6217663806110175239">✅</tg-emoji> Your UPI ID has been linked to: <code>{upi_input}</code>', parse_mode=ParseMode.HTML, reply_markup=get_main_menu_keyboard())
    await state.clear()
    await state.update_data(last_menu_msg_id=sent_msg.message_id)

@dp.message(UserState.setting_usdt, F.text, ~F.text.startswith("/"), ~F.text.in_(MENU_BUTTONS))
async def process_link_usdt(message: Message, state: FSMContext):
    usdt_input = message.text.strip()
    if not usdt_input.startswith("0x") or len(usdt_input) != 42:
        await message.answer('<tg-emoji emoji-id="5274099962655816924">❗️</tg-emoji> Invalid USDT BEP-20 address format. Please send a valid 42-character Binance Smart Chain address starting with <code>0x</code>.', parse_mode=ParseMode.HTML)
        return

    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE users SET usdt_address=$1 WHERE user_id=$2", usdt_input, message.from_user.id)

    invalidate_user_cache(message.from_user.id)
    sent_msg = await message.answer(f'<tg-emoji emoji-id="6217663806110175239">✅</tg-emoji> Your USDT BEP-20 address has been linked to: <code>{usdt_input}</code>', parse_mode=ParseMode.HTML, reply_markup=get_main_menu_keyboard())
    await state.clear()
    await state.update_data(last_menu_msg_id=sent_msg.message_id)

@dp.message(AdminState.waiting_for_chat_user_id, ~F.text.startswith("/"), ~F.text.in_(MENU_BUTTONS))
async def process_chat_user_id_step(message: Message, state: FSMContext):
    try:
        target_id = int(message.text.strip())
        await state.update_data(target_user_id=target_id)
        await state.set_state(AdminState.waiting_for_chat_message)
        await message.answer(f"✉️ Now send the text, photo, or media message you want to deliver to User `{target_id}`:", parse_mode=ParseMode.MARKDOWN)
    except ValueError:
        await message.answer("❌ Invalid User ID. Please enter a valid numeric ID.", reply_markup=get_admin_menu_keyboard())
        await state.clear()

@dp.message(AdminState.waiting_for_chat_message, ~F.text.startswith("/"), ~F.text.in_(MENU_BUTTONS))
async def process_chat_message_step(message: Message, state: FSMContext):
    data = await state.get_data()
    target_id = data['target_user_id']

    try:
        await message.copy_to(chat_id=target_id)
        await message.answer(f"✅ **Message successfully sent to User `{target_id}`!**", parse_mode=ParseMode.MARKDOWN, reply_markup=get_admin_menu_keyboard())
    except Exception as e:
        await message.answer(f"❌ Failed to send message to User `{target_id}`.\n\nError: `{e}`", parse_mode=ParseMode.MARKDOWN, reply_markup=get_admin_menu_keyboard())

    await state.clear()

@dp.message(AdminState.waiting_for_add_balance, ~F.text.startswith("/"), ~F.text.in_(MENU_BUTTONS))
async def process_add_balance_step(message: Message, state: FSMContext):
    try:
        _, user_id_str, amount_str = f"cmd {message.text.strip()}".split()
        user_id = int(user_id_str)
        amount = float(amount_str)
        await ensure_user(user_id)
        
        user_data = await get_user_data(user_id)
        amt_str = format_currency(amount, user_data['currency'])

        async with db_pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("UPDATE users SET balance = balance + $1 WHERE user_id=$2", amount, user_id)
                await conn.execute("INSERT INTO transactions (user_id, type, amount, note) VALUES ($1, $2, $3, $4)", user_id, "admin_add", amount, "Admin balance add")
        
        invalidate_user_cache(user_id)
        await message.answer(f"✅ Added ₹{amount} to User `{user_id}`", parse_mode=ParseMode.MARKDOWN, reply_markup=get_admin_menu_keyboard())
        asyncio.create_task(send_user_notification(user_id, f"💰 Admin added {amt_str} to your balance."))
    except Exception as e:
        await message.answer(f"❌ Format error: {e}. Please send in format: `USER_ID AMOUNT`", parse_mode=ParseMode.MARKDOWN)
    await state.clear()

@dp.message(AdminState.waiting_for_cut_balance, ~F.text.startswith("/"), ~F.text.in_(MENU_BUTTONS))
async def process_cut_balance_step(message: Message, state: FSMContext):
    try:
        _, user_id_str, amount_str = f"cmd {message.text.strip()}".split()
        user_id = int(user_id_str)
        amount = float(amount_str)

        current_balance = await get_balance(user_id)
        if amount > current_balance:
            await message.answer(f"❌ Cannot cut ₹{amount}. User's balance is only ₹{current_balance:.2f}.", reply_markup=get_admin_menu_keyboard())
            await state.clear()
            return

        user_data = await get_user_data(user_id)
        amt_str = format_currency(amount, user_data['currency'])

        async with db_pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("UPDATE users SET balance = balance - $1 WHERE user_id=$2", amount, user_id)
                await conn.execute("INSERT INTO transactions (user_id, type, amount, note) VALUES ($1, $2, $3, $4)", user_id, "admin_cut", -amount, "Admin balance cut")

        invalidate_user_cache(user_id)
        await message.answer(f"✅ Cut ₹{amount} from User `{user_id}`", parse_mode=ParseMode.MARKDOWN, reply_markup=get_admin_menu_keyboard())
        asyncio.create_task(send_user_notification(user_id, f"⚠️ Admin deducted {amt_str} from your balance."))
    except Exception as e:
        await message.answer(f"❌ Format error: {e}. Please send in format: `USER_ID AMOUNT`", parse_mode=ParseMode.MARKDOWN)
    await state.clear()

@dp.message(AdminState.waiting_for_check_balance, ~F.text.startswith("/"), ~F.text.in_(MENU_BUTTONS))
async def process_check_balance_step(message: Message, state: FSMContext):
    try:
        target_id = int(message.text.strip())
        user_data = await get_user_data(target_id)
        bal = user_data['balance'] if user_data else 0.0
        upi = user_data['upi'] if user_data else "None"
        usdt = user_data['usdt_address'] if user_data else "None"
        banned = await is_banned(target_id)
        status = "🔴 Banned" if banned else "🟢 Active"
        await message.answer(f"👤 **User ID:** `{target_id}`\n💰 **Balance:** ₹{bal:.2f}\n🏦 **UPI:** `{upi}`\n🪙 **USDT:** `{usdt}`\n📌 **Status:** {status}", parse_mode=ParseMode.MARKDOWN, reply_markup=get_admin_menu_keyboard())
    except ValueError:
        await message.answer("❌ Invalid User ID.", reply_markup=get_admin_menu_keyboard())
    await state.clear()

@dp.message(AdminState.waiting_for_user_transactions, ~F.text.startswith("/"), ~F.text.in_(MENU_BUTTONS))
async def process_user_transactions_step(message: Message, state: FSMContext):
    try:
        target_id = int(message.text.strip())
        user_data = await get_user_data(target_id)
        bal = user_data['balance'] if user_data else 0.0
        upi = user_data['upi'] if user_data else "None"

        async with db_pool.acquire() as conn:
            rows = await conn.fetch("SELECT type, amount, note, created_at FROM transactions WHERE user_id=$1 ORDER BY id DESC LIMIT 10", target_id)

        text = (
            f"👤 <b>User ID:</b> <code>{target_id}</code>\n"
            f"💰 <b>Balance:</b> ₹{bal:.2f}\n"
            f"🏦 <b>UPI:</b> <code>{upi}</code>\n\n"
            f"📜 <b>Recent Transactions:</b>\n\n"
        )
        if not rows:
            text += "<i>No transaction history found for this user.</i>"
        else:
            for r in rows:
                sign = "+" if r['amount'] >= 0 else ""
                text += f"• {sign}₹{r['amount']:.2f} | {r['type']}\n  Note: {r['note']}\n  Date: {r['created_at'].strftime('%Y-%m-%d %H:%M:%S')}\n\n"

        await message.answer(text, parse_mode=ParseMode.HTML, reply_markup=get_admin_menu_keyboard())
    except ValueError:
        await message.answer("❌ Invalid User ID.", reply_markup=get_admin_menu_keyboard())
    await state.clear()

@dp.message(AdminState.waiting_for_ban_user, ~F.text.startswith("/"), ~F.text.in_(MENU_BUTTONS))
async def process_ban_user_step(message: Message, state: FSMContext):
    try:
        target_id = int(message.text.strip())
        if target_id == ADMIN_ID:
            await message.answer("❌ You cannot ban yourself!", reply_markup=get_admin_menu_keyboard())
            await state.clear()
            return

        async with db_pool.acquire() as conn:
            await conn.execute("INSERT INTO banned_users (user_id) VALUES ($1) ON CONFLICT DO NOTHING", target_id)

        BANNED_USERS_CACHE.add(target_id)
        await message.answer(f"🚫 **User `{target_id}` has been banned.**", parse_mode=ParseMode.MARKDOWN, reply_markup=get_admin_menu_keyboard())
        asyncio.create_task(send_user_notification(target_id, "🚫 You have been banned from using this bot."))
    except ValueError:
        await message.answer("❌ Invalid User ID.", reply_markup=get_admin_menu_keyboard())
    await state.clear()

@dp.message(AdminState.waiting_for_unban_user, ~F.text.startswith("/"), ~F.text.in_(MENU_BUTTONS))
async def process_unban_user_step(message: Message, state: FSMContext):
    try:
        target_id = int(message.text.strip())
        async with db_pool.acquire() as conn:
            await conn.execute("DELETE FROM banned_users WHERE user_id=$1", target_id)

        BANNED_USERS_CACHE.discard(target_id)
        await message.answer(f"✅ **User `{target_id}` has been unbanned.**", parse_mode=ParseMode.MARKDOWN, reply_markup=get_admin_menu_keyboard())
        asyncio.create_task(send_user_notification(target_id, "🎉 Your ban has been lifted! You can now use the bot again."))
    except ValueError:
        await message.answer("❌ Invalid User ID.", reply_markup=get_admin_menu_keyboard())
    await state.clear()

@dp.message(AdminState.waiting_for_broadcast, ~F.text.startswith("/"), ~F.text.in_(MENU_BUTTONS))
async def process_broadcast_step(message: Message, state: FSMContext):
    async with db_pool.acquire() as conn:
        users = await conn.fetch("SELECT user_id FROM users")

    if not users:
        await message.answer("📭 No users found in database.", reply_markup=get_admin_menu_keyboard())
        await state.clear()
        return

    status_msg = await message.answer(f"🚀 **Starting Broadcast** to {len(users)} users...")
    success = 0
    failed = 0

    for r in users:
        uid = r['user_id']
        try:
            await message.copy_to(chat_id=uid)
            success += 1
            await asyncio.sleep(0.05)
        except Exception:
            failed += 1

    await status_msg.edit_text(
        f"📢 **Broadcast Finished!**\n\n"
        f"✅ **Sent:** {success}\n"
        f"❌ **Failed/Blocked:** {failed}\n"
        f"👥 **Total:** {len(users)}"
    )
    await state.clear()

@dp.message(AdminState.waiting_for_update_rewards, ~F.text.startswith("/"), ~F.text.in_(MENU_BUTTONS))
async def process_update_rewards_step(message: Message, state: FSMContext):
    try:
        new_reward = float(message.text.strip())
        async with db_pool.acquire() as conn:
            await conn.execute("UPDATE tasks SET reward=$1", new_reward)
        await message.answer(f"💰 **Success!** Reward for ALL tasks updated to **₹{new_reward:.2f}**.", parse_mode=ParseMode.MARKDOWN, reply_markup=get_admin_menu_keyboard())
    except ValueError:
        await message.answer("❌ Invalid reward amount.", reply_markup=get_admin_menu_keyboard())
    await state.clear()

@dp.message(AdminState.waiting_for_remove_task, ~F.text.startswith("/"), ~F.text.in_(MENU_BUTTONS))
async def process_remove_task_step(message: Message, state: FSMContext):
    try:
        task_id = int(message.text.strip())
        async with db_pool.acquire() as conn:
            task = await conn.fetchrow("SELECT id FROM tasks WHERE id=$1", task_id)
            if not task:
                await message.answer(f"❌ Task #{task_id} does not exist.", reply_markup=get_admin_menu_keyboard())
                await state.clear()
                return
            async with conn.transaction():
                await conn.execute("DELETE FROM task_assignments WHERE task_id=$1", task_id)
                await conn.execute("DELETE FROM tasks WHERE id=$1", task_id)
        await message.answer(f"🗑️ **Task #{task_id}** permanently removed.", parse_mode=ParseMode.MARKDOWN, reply_markup=get_admin_menu_keyboard())
    except ValueError:
        await message.answer("❌ Invalid Task ID.", reply_markup=get_admin_menu_keyboard())
    await state.clear()

# ============================================
# USER INLINE SUBMIT & CANCEL SYSTEM
# ============================================

@dp.callback_query(F.data == "link_upi")
async def start_link_upi(call: CallbackQuery, state: FSMContext):
    try:
        await call.answer()
    except Exception:
        pass
    await state.set_state(UserState.setting_upi)
    await call.message.answer('<tg-emoji emoji-id="5902449142575141204">🔡</tg-emoji> Send your UPI ID below:\n\n<i>Example: username@upi or 9876543210@paytm</i>', parse_mode=ParseMode.HTML)

@dp.callback_query(F.data == "link_usdt")
async def start_link_usdt(call: CallbackQuery, state: FSMContext):
    try:
        await call.answer()
    except Exception:
        pass
    await state.set_state(UserState.setting_usdt)
    await call.message.answer('<tg-emoji emoji-id="5902449142575141204">🪙</tg-emoji> Send your <b>USDT BEP-20</b> address below:\n\n<i>Example: 0x1234567890abcdef1234567890abcdef12345678</i>', parse_mode=ParseMode.HTML)

@dp.callback_query(F.data == "choose_withdraw_method")
async def choose_withdraw_method_handler(call: CallbackQuery):
    text = "<tg-emoji emoji-id=\"5445353829304387411\">💳</tg-emoji> <b>Select Withdrawal Method:</b>"
    try:
        await call.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=get_withdraw_options_keyboard())
        await call.answer()
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
        try:
            await call.answer("❌ Please link your UPI ID first before withdrawing via UPI!", show_alert=True)
        except Exception:
            pass
        return

    MIN_WITHDRAW = 150.0
    if bal < MIN_WITHDRAW:
        min_withdraw_str = format_currency(MIN_WITHDRAW, curr)
        bal_str = format_currency(bal, curr)
        try:
            await call.answer(f"❌ Minimum withdrawal is {min_withdraw_str}. Current Balance: {bal_str}", show_alert=True)
        except Exception:
            pass
        return

    async with db_pool.acquire() as conn:
        existing_pending = await conn.fetchrow(
            "SELECT id FROM withdrawals WHERE user_id = $1 AND status = 'pending'",
            user_id
        )
        if existing_pending:
            try:
                await call.answer("⚠️ You already have a pending withdrawal request! Please wait for it to be processed.", show_alert=True)
            except Exception:
                pass
            return

        withdraw_id = await conn.fetchval(
            "INSERT INTO withdrawals(user_id, amount, method, payment_address) VALUES ($1, $2, 'UPI', $3) RETURNING id",
            user_id, bal, upi
        )

    kb = InlineKeyboardBuilder()
    kb.button(
        text='Pay', 
        callback_data=f'pay:{withdraw_id}:{user_id}:{bal}',
        icon_custom_emoji_id="5444856076954520455",
        style="success"
    )
    kb.button(
        text='Reject', 
        callback_data=f'reject:{withdraw_id}:{user_id}',
        icon_custom_emoji_id="5274099962655816924",
        style="danger"
    )
    
    await bot.send_message(
        ADMIN_ID,
        f'<tg-emoji emoji-id="5417924076503062111">💰</tg-emoji> <b>WITHDRAWAL REQUEST #{withdraw_id} (UPI)</b>\n\n'
        f'<tg-emoji emoji-id="5870458774455587120">👤</tg-emoji> @{call.from_user.username}\n'
        f'<tg-emoji emoji-id="5197269100878907942">✍️</tg-emoji> <code>{user_id}</code>\n'
        f'<tg-emoji emoji-id="5417924076503062111">💰</tg-emoji> Amount: ₹{bal:.2f}\n'
        f'<tg-emoji emoji-id="6152069549442208798">🏦</tg-emoji> UPI: <code>{upi}</code>',
        reply_markup=kb.as_markup(),
        parse_mode=ParseMode.HTML
    )

    bal_display = format_currency(bal, curr)
    try:
        await call.message.edit_text(f'<tg-emoji emoji-id="5195033767969839232">🚀</tg-emoji> Withdrawal request of {bal_display} sent to admin using UPI: <code>{upi}</code>', parse_mode=ParseMode.HTML)
        await call.answer()
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
        try:
            await call.answer("❌ Please link your USDT BEP-20 address first before withdrawing!", show_alert=True)
        except Exception:
            pass
        return

    MIN_WITHDRAW = 150.0
    if bal < MIN_WITHDRAW:
        min_withdraw_str = format_currency(MIN_WITHDRAW, curr)
        bal_str = format_currency(bal, curr)
        try:
            await call.answer(f"❌ Minimum withdrawal is {min_withdraw_str}. Current Balance: {bal_str}", show_alert=True)
        except Exception:
            pass
        return

    async with db_pool.acquire() as conn:
        existing_pending = await conn.fetchrow(
            "SELECT id FROM withdrawals WHERE user_id = $1 AND status = 'pending'",
            user_id
        )
        if existing_pending:
            try:
                await call.answer("⚠️ You already have a pending withdrawal request! Please wait for it to be processed.", show_alert=True)
            except Exception:
                pass
            return

        withdraw_id = await conn.fetchval(
            "INSERT INTO withdrawals(user_id, amount, method, payment_address) VALUES ($1, $2, 'USDT BEP-20', $3) RETURNING id",
            user_id, bal, usdt
        )

    kb = InlineKeyboardBuilder()
    kb.button(
        text='Pay', 
        callback_data=f'pay:{withdraw_id}:{user_id}:{bal}',
        icon_custom_emoji_id="5444856076954520455",
        style="success"
    )
    kb.button(
        text='Reject', 
        callback_data=f'reject:{withdraw_id}:{user_id}',
        icon_custom_emoji_id="5274099962655816924",
        style="danger"
    )
    
    usdt_amount = bal / USD_TO_INR
    await bot.send_message(
        ADMIN_ID,
        f'<tg-emoji emoji-id="5417924076503062111">💰</tg-emoji> <b>WITHDRAWAL REQUEST #{withdraw_id} (USDT BEP-20)</b>\n\n'
        f'<tg-emoji emoji-id="5870458774455587120">👤</tg-emoji> @{call.from_user.username}\n'
        f'<tg-emoji emoji-id="5197269100878907942">✍️</tg-emoji> <code>{user_id}</code>\n'
        f'<tg-emoji emoji-id="5417924076503062111">💰</tg-emoji> Amount: ₹{bal:.2f} (~${usdt_amount:.2f} USDT)\n'
        f'<tg-emoji emoji-id="5197434882321567830">🪙</tg-emoji> USDT BEP-20: <code>{usdt}</code>',
        reply_markup=kb.as_markup(),
        parse_mode=ParseMode.HTML
    )

    bal_display = format_currency(bal, curr)
    try:
        await call.message.edit_text(f'<tg-emoji emoji-id="5195033767969839232">🚀</tg-emoji> Withdrawal request of {bal_display} (~${usdt_amount:.2f} USDT) sent to admin using USDT address: <code>{usdt}</code>', parse_mode=ParseMode.HTML)
        await call.answer()
    except Exception as e:
        print(f"Error editing withdraw msg: {e}")

@dp.callback_query(F.data == "user_submit_task")
async def inline_submit_task(call: CallbackQuery, state: FSMContext):
    user_id = call.from_user.id
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow('SELECT ta.task_id, t.status FROM task_assignments ta JOIN tasks t ON ta.task_id = t.id WHERE ta.user_id=$1', user_id)
    
    if not row:
        try:
            await call.answer('❌ You do not have any active task.', show_alert=True)
        except Exception:
            pass
        return
    if row['status'] == 'pending_review':
        try:
            await call.answer('⏳ You have already submitted this task.', show_alert=True)
        except Exception:
            pass
        return
        
    await state.set_state(UserState.submitting_task)
    
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except:
        pass
    await call.message.answer('<tg-emoji emoji-id="5206607081334906820">✔️</tg-emoji> Send screenshot or proof of completed task.', parse_mode=ParseMode.HTML)
    try:
        await call.answer()
    except Exception:
        pass

@dp.callback_query(F.data == "user_cancel_task")
async def inline_cancel_task(call: CallbackQuery, state: FSMContext):
    await state.clear()
    user_id = call.from_user.id
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow('SELECT ta.task_id, t.status FROM task_assignments ta JOIN tasks t ON ta.task_id = t.id WHERE ta.user_id=$1', user_id)
        if not row:
            try:
                await call.answer("❌ You don't have any active task to cancel.", show_alert=True)
            except Exception:
                pass
            return
        
        if row['status'] == 'pending_review':
            try:
                await call.answer("❌ Cannot cancel a task already submitted for admin review.", show_alert=True)
            except Exception:
                pass
            return

        task_id = row['task_id']
        async with conn.transaction():
            await conn.execute('DELETE FROM task_assignments WHERE user_id=$1', user_id)
            await conn.execute("UPDATE tasks SET status='available' WHERE id=$1", task_id)
            
    try:
        await call.message.edit_text(f'<tg-emoji emoji-id="6217663806110175239">✅</tg-emoji> Task #{task_id} has been cancelled and returned to the pool.', parse_mode=ParseMode.HTML)
        await call.answer()
    except Exception:
        pass

@dp.message(UserState.submitting_task, F.photo | F.text, ~F.text.startswith("/") if F.text else True, ~F.text.in_(MENU_BUTTONS) if F.text else True)
async def handle_task_submission(message: Message, state: FSMContext):
    user_id = message.from_user.id
    async with db_pool.acquire() as conn:
        task = await conn.fetchrow('SELECT t.id, t.title, t.details, t.reward FROM task_assignments ta JOIN tasks t ON ta.task_id = t.id WHERE ta.user_id=$1', user_id)
    if not task:
        await state.clear()
        sent_msg = await message.answer('❌ No active task found.', reply_markup=get_main_menu_keyboard())
        await state.update_data(last_menu_msg_id=sent_msg.message_id)
        return
    
    task_id = task['id']
    title = task['title']
    details = task['details']
    reward = task['reward']

    try:
        parts = details.split(" | ")
        email = parts[0].replace("Email: ", "").strip()
        password = parts[1].replace("Pass: ", "").strip()
    except Exception:
        email = title.replace("Login to ", "").strip()
        password = "TaskVerse@#"

    # Real-Time Verification with MyEmailVerifier/Emailable
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

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text='Approve', callback_data=f'taskapprove:{task_id}:{user_id}:{reward}', icon_custom_emoji_id="6217663806110175239", style="success"),
        InlineKeyboardButton(text='Decline', callback_data=f'taskdecline:{task_id}:{user_id}', icon_custom_emoji_id="5274099962655816924", style="danger")
    ]])

    admin_msg_text = (
        f'<tg-emoji emoji-id="5206607081334906820">📤</tg-emoji> <b>Task Submission #{task_id}</b>\n\n'
        f'<tg-emoji emoji-id="5870458774455587120">👤</tg-emoji> <b>User:</b> @{message.from_user.username} (<code>{user_id}</code>)\n'
        f'📧 <b>Email:</b> <code>{email}</code>\n'
        f'<tg-emoji emoji-id="6005570495603282482">🔑</tg-emoji> <b>Password:</b> <code>{password}</code>\n'
        f'<tg-emoji emoji-id="5417924076503062111">💰</tg-emoji> <b>Reward:</b> ₹{reward}'
    )

    if message.photo:
        await bot.send_photo(ADMIN_ID, photo=message.photo[-1].file_id, caption=admin_msg_text, reply_markup=kb, parse_mode=ParseMode.HTML)
    else:
        proof_text = f"\n\nProof: {message.text}"
        await bot.send_message(ADMIN_ID, admin_msg_text + proof_text, reply_markup=kb, parse_mode=ParseMode.HTML)
    
    sent_msg = await message.answer(
        f'<tg-emoji emoji-id="5206607081334906820">✔️</tg-emoji> Task #{task_id} submission sent for admin review.\n\n'
        f'<tg-emoji emoji-id="5447644880824181073">⚠️</tg-emoji> <b>Important:</b> Please make sure to <b>logout</b> of this account from your device!', 
        reply_markup=get_main_menu_keyboard(), 
        parse_mode=ParseMode.HTML
    )
    await state.clear()
    await state.update_data(last_menu_msg_id=sent_msg.message_id)

# ============================================
# UNIFIED SELL APPROVE & DECLINE HANDLERS
# ============================================

@dp.callback_query(F.data.startswith("sellapprove_db:"))
async def approve_sell_unified(call: CallbackQuery):
    try:
        await call.answer()
    except Exception:
        pass

    _, sell_id_str, user_id_str, amount_str = call.data.split(":")
    sell_id = int(sell_id_str)
    user_id = int(user_id_str)
    amount = float(amount_str)

    async with db_pool.acquire() as conn:
        await ensure_user(user_id, conn=conn)

        status = await conn.fetchval("SELECT status FROM pending_sells WHERE id=$1", sell_id)
        if status != 'pending_review':
            return

        async with conn.transaction():
            await conn.execute("UPDATE users SET balance = balance + $1 WHERE user_id=$2", amount, user_id)
            await conn.execute("INSERT INTO transactions (user_id, type, amount, note) VALUES ($1, $2, $3, $4)", user_id, "sell", amount, f"Gmail sell #{sell_id} approved")
            await conn.execute("UPDATE pending_sells SET status='approved' WHERE id=$1", sell_id)

            referred_by = await conn.fetchval("SELECT referred_by FROM users WHERE user_id=$1", user_id)

        if referred_by:
            await ensure_user(referred_by, conn=conn)
            ref_reward = 5.0
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

@dp.callback_query(F.data.startswith("selldecline_db:"))
async def decline_sell_unified(call: CallbackQuery, state: FSMContext):
    try:
        await call.answer()
    except Exception:
        pass

    _, sell_id_str, user_id_str = call.data.split(":")
    sell_id = int(sell_id_str)
    user_id = int(user_id_str)

    await ensure_user(user_id)

    async with db_pool.acquire() as conn:
        status = await conn.fetchval("SELECT status FROM pending_sells WHERE id=$1", sell_id)
        if status != 'pending_review':
            return

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

@dp.callback_query(F.data.startswith("taskapprove:"))
async def approve_task(call: CallbackQuery):
    try:
        await call.answer()
    except Exception:
        pass

    _, task_id_str, callback_user_id, reward_str = call.data.split(":")
    task_id = int(task_id_str)
    reward = float(reward_str)
    
    async with db_pool.acquire() as conn:
        current_status = await conn.fetchval("SELECT status FROM tasks WHERE id=$1", task_id)
        if current_status != 'pending_review':
            return

        assigned_user_id = await conn.fetchval("SELECT user_id FROM task_assignments WHERE task_id=$1", task_id)
        user_id = assigned_user_id if assigned_user_id else int(callback_user_id)

        task_details = await conn.fetchval("SELECT details FROM tasks WHERE id=$1", task_id)
        try:
            task_email = task_details.split(" | ")[0].replace("Email: ", "").strip()
        except Exception:
            task_email = f"Task Account"

        await ensure_user(user_id, conn=conn)

        async with conn.transaction():
            await conn.execute("UPDATE users SET balance = balance + $1 WHERE user_id=$2", reward, user_id)
            await conn.execute("INSERT INTO transactions (user_id, type, amount, note) VALUES ($1, $2, $3, $4)", user_id, "task", reward, f"{task_email} #{task_id}")
            await conn.execute("DELETE FROM task_assignments WHERE task_id=$1", task_id)
            await conn.execute("UPDATE tasks SET status='completed' WHERE id=$1", task_id)

            referred_by = await conn.fetchval("SELECT referred_by FROM users WHERE user_id=$1", user_id)

        if referred_by:
            await ensure_user(referred_by, conn=conn)
            ref_reward = 7.0
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

@dp.callback_query(F.data.startswith("taskdecline:"))
async def decline_task(call: CallbackQuery, state: FSMContext):
    try:
        await call.answer()
    except Exception:
        pass

    _, task_id_str, user_id_str = call.data.split(":")
    task_id = int(task_id_str)
    user_id = int(user_id_str)
    
    await ensure_user(user_id)

    async with db_pool.acquire() as conn:
        current_status = await conn.fetchval("SELECT status FROM tasks WHERE id=$1", task_id)
        if current_status != 'pending_review':
            return

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

@dp.callback_query(F.data.startswith("pay:"))
async def pay_withdraw(call: CallbackQuery):
    try:
        await call.answer()
    except Exception:
        pass

    _, withdrawal_id, user_id, amount = call.data.split(":")
    withdrawal_id = int(withdrawal_id)
    user_id = int(user_id)
    amount = float(amount)

    async with db_pool.acquire() as conn:
        await ensure_user(user_id, conn=conn)

        status = await conn.fetchval("SELECT status FROM withdrawals WHERE id=$1", withdrawal_id)
        if status != 'pending':
            return

        async with conn.transaction():
            await conn.execute("UPDATE users SET balance = balance - $1 WHERE user_id=$2", amount, user_id)
            await conn.execute("UPDATE withdrawals SET status='paid' WHERE id=$1", withdrawal_id)
            await conn.execute("INSERT INTO transactions (user_id, type, amount, note) VALUES ($1, $2, $3, $4)", user_id, "withdrawal", -amount, "Withdrawal paid")
            
    invalidate_user_cache(user_id)
    await edit_admin_message(call, '✅ Withdrawal Paid')

    async def notify_user():
        user_data = await get_user_data(user_id)
        formatted_amt = format_currency(amount, user_data['currency'])
        await send_user_notification(user_id, f"🎉 Withdrawal of {formatted_amt} has been paid.")

    asyncio.create_task(notify_user())

@dp.callback_query(F.data.startswith("reject:"))
async def reject_withdraw(call: CallbackQuery):
    try:
        await call.answer()
    except Exception:
        pass

    _, withdrawal_id, user_id = call.data.split(":")
    withdrawal_id = int(withdrawal_id)
    user_id = int(user_id)
    
    async with db_pool.acquire() as conn:
        status = await conn.fetchval("SELECT status FROM withdrawals WHERE id=$1", withdrawal_id)
        if status != 'pending':
            return

        await conn.execute("UPDATE withdrawals SET status='rejected' WHERE id=$1", withdrawal_id)
        
    await edit_admin_message(call, '⚠️ Withdrawal Rejected')
    asyncio.create_task(send_user_notification(user_id, '<tg-emoji emoji-id="5447644880824181073">⚠️</tg-emoji> Your withdrawal request was rejected.', parse_mode=ParseMode.HTML))

# ============================================
# OPTIMIZED AUTO EXPIRE TASKS ENGINE
# ============================================

async def auto_expire_tasks():
    while True:
        try:
            expired_tasks = []
            async with db_pool.acquire() as conn:
                rows = await conn.fetch('''
                    SELECT ta.task_id, ta.user_id, ta.assigned_at 
                    FROM task_assignments ta
                    JOIN tasks t ON ta.task_id = t.id
                    WHERE t.status != 'pending_review'
                ''')
                
                now = datetime.utcnow()
                for r in rows:
                    if now - r['assigned_at'] > timedelta(minutes=30):
                        expired_tasks.append((r['task_id'], r['user_id']))

                if expired_tasks:
                    task_ids = [t[0] for t in expired_tasks]
                    async with conn.transaction():
                        await conn.execute('DELETE FROM task_assignments WHERE task_id = ANY($1::int[])', task_ids)
                        await conn.execute("UPDATE tasks SET status='available' WHERE id = ANY($1::int[])", task_ids)

            for task_id, user_id in expired_tasks:
                asyncio.create_task(send_user_notification(
                    user_id, 
                    f'<tg-emoji emoji-id="5195033767969839232">🚀</tg-emoji> Task #{task_id} has expired after 30 minutes.\nThe task was returned to the pool.\n\nUse "Get Task" to get a new task.', 
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
