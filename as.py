import asyncio
from datetime import datetime, timedelta
import os
import urllib.parse
import time
import re
import json
import secrets
import string
import hashlib
import hmac
import aiohttp
from aiohttp import web

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
    ChatMemberUpdated,
    WebAppInfo
)
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder
from aiogram.exceptions import TelegramForbiddenError, TelegramBadRequest
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

# ============================================
# CONFIGURATION & INITIALIZATION
# ============================================

BOT_TOKEN = os.environ.get('BOT_TOKEN')
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable is not set. Set it before starting the bot (never hardcode it in source).")
_admin_id_raw = os.environ.get('ADMIN_ID')
if not _admin_id_raw:
    raise RuntimeError("ADMIN_ID environment variable is not set. Set it before starting the bot.")
ADMIN_ID = int(_admin_id_raw)
DATABASE_URL = os.environ.get('DATABASE_URL')
WORKER_BOT_TOKEN = os.environ.get('WORKER_BOT_TOKEN', '').strip()

# Webhook configuration (fast path). If WEBHOOK_URL / RENDER_EXTERNAL_URL isn't set,
# the bot automatically falls back to the old long-polling behavior below.
WEBHOOK_PATH = f"/webhook/{BOT_TOKEN}"
WEBHOOK_SECRET = os.environ.get('WEBHOOK_SECRET', 'change_this_secret')
_base_url = (os.environ.get('WEBHOOK_URL') or os.environ.get('RENDER_EXTERNAL_URL', '')).rstrip('/')
WEBHOOK_URL = f"{_base_url}{WEBHOOK_PATH}" if _base_url else None
WEBAPP_URL = f"{_base_url}/webapp/" if _base_url else None

# Currency Conversion Rate (1 USD/USDT = 96.30 INR)
USD_TO_INR = 96.30

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

db_pool = None
HTTP_SESSION = None  # shared aiohttp session (reused across requests for speed)
BANNED_USERS_CACHE = set()
SUPPORT_REQUESTS_CACHE = {}  # In-memory store: {user_id: {"username": str, "message": str}}
MUST_JOIN_CHANNEL = None
BOT_USERNAME = "GmailEarnexBot"
BOT_STATUS = True           # True = ON, False = OFF
BOT_OFF_MESSAGE = "<tg-emoji emoji-id=\"5420323339723881652\">⚠️</tg-emoji> Bot is Currently Off, Wait For Admin To On The Bot"   # Shown to users while bot is OFF; admin can customize it
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

# TUTORIAL VIDEO LINKS (set by admin via 🤷‍♂️Videos panel; None = no button shown)
TASK_VIDEO_LINK = None
SELL_VIDEO_LINK = None
HOWTO_VIDEO_LINK = None

# VALIDATOR CONFIGURATION
EMAILABLE_API_KEY = "netnit_EFo0B5lNYvUsZJ5eHMxX4SBNvT12Uq71"
VALIDATOR_ENABLED = True     # True = Active, False = Deactivated
VALIDATOR_PROVIDER = "netnit"  # "netnit", "myemailverifier", or "emailable"

# IN-MEMORY SPEED CACHES
JOINED_CACHE = {}     # {user_id: timestamp_joined}
USER_CACHE = {}       # {user_id: dict_data}

# List of all menu buttons to prevent state bleeding
MENU_BUTTONS = {
    "Get Task", "Balance", "Sell Gmail", "History", "Referrals", "My Accounts", "Settings", "Support", "Cancel", "Main Menu",
    "Add Task", "Tasks", "Available Tasks", "Pending Reviews", "Pending Withdrawals", "Chat", "Unassign Tasks", "Find ID", "Add Balance", 
    "Cut Balance", "Check Balance", "Top Balances", "Ban User", "Unban User",
    "Broadcast", "Change Values", "Remove Task", "Transactions", "View Stats",
    "Must Join Channel", "🔴 Bot Status: OFF", "🟢 Bot Status: ON", "🟢 Ref Status: ON", "🔴 Ref Status: OFF", "Validator", "Transfer Admin",
    "🟢 Ultra Status: ON", "🔴 Ultra Status: OFF", "Manage Workers", "Dustbin", "Videos"
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
    if VALIDATOR_PROVIDER == "netnit":
        return "https://apikey.netnit.net/fastcheck"
    elif VALIDATOR_PROVIDER == "emailable":
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
                "<tg-emoji emoji-id=\"5305265301917549162\">📤</tg-emoji><i>Verifying Your Gmail From Official Google</i><tg-emoji emoji-id=\"5201691993775818138\">🚀</tg-emoji>",
                parse_mode=ParseMode.HTML
            )
        except Exception:
            pass

    is_valid_email = False

    try:
        session = HTTP_SESSION if HTTP_SESSION and not HTTP_SESSION.closed else aiohttp.ClientSession()
        _own_session = session is not HTTP_SESSION
        try:
            if VALIDATOR_PROVIDER == "netnit":
                url = "https://apikey.netnit.net/fastcheck"
                headers = {
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {EMAILABLE_API_KEY}"
                }
                payload = {
                    "mail": [email]
                }
                async with session.post(url, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=12.0)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        results = data.get("results", [])
                        for res in results:
                            target_email = str(res.get("email", "")).strip().lower()
                            if target_email == email or target_email == email.lower():
                                if str(res.get("status", "")).strip().lower() == "good":
                                    is_valid_email = True
                                break
                    else:
                        print(f"Validator HTTP Error (netnit): {resp.status}")
            else:
                if VALIDATOR_PROVIDER == "emailable":
                    url = f"https://api.emailable.com/v1/verify?email={urllib.parse.quote(email)}&api_key={urllib.parse.quote(EMAILABLE_API_KEY)}"
                else:
                    url = f"https://api.myemailverifier.com/api/validate_single.php?apikey={urllib.parse.quote(EMAILABLE_API_KEY)}&email={urllib.parse.quote(email)}"

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
        finally:
            if _own_session:
                await session.close()
    except Exception as e:
        print(f"Validator Exception ({VALIDATOR_PROVIDER}): {e}")

    if verify_msg:
        try:
            if is_valid_email:
                await verify_msg.edit_text("<tg-emoji emoji-id=\"6217663806110175239\">✅</tg-emoji><i>Gmail Verified Successfully!</i><tg-emoji emoji-id=\"5461151367559141950\">🎉</tg-emoji>", parse_mode=ParseMode.HTML)
            else:
                await verify_msg.edit_text("<tg-emoji emoji-id=\"5274099962655816924\">❌</tg-emoji><i>Gmail Verification Failed - Account Not Found!</i>", parse_mode=ParseMode.HTML)
        except Exception:
            pass

    return is_valid_email

# ============================================
# AIOHTTP SERVER (health check + webhook fast-path)
# ============================================

async def health(request):
    return web.Response(text="Bot is running!")

# ============================================
# MINI APP (Telegram WebApp) JSON API
# ============================================

def validate_init_data(init_data: str, max_age_seconds: int = 86400):
    """
    Validates a Telegram WebApp `initData` string per Telegram's official algorithm:
    https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
    Returns the parsed `user` dict on success, or None if missing/invalid/expired.
    """
    if not init_data:
        return None
    try:
        parsed = dict(urllib.parse.parse_qsl(init_data, strict_parsing=True))
    except Exception:
        return None

    received_hash = parsed.pop('hash', None)
    if not received_hash:
        return None

    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
    secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    computed_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(computed_hash, received_hash):
        return None

    try:
        auth_date = int(parsed.get('auth_date', 0))
    except ValueError:
        return None
    if time.time() - auth_date > max_age_seconds:
        return None

    user_json = parsed.get('user')
    if not user_json:
        return None
    try:
        return json.loads(user_json)
    except Exception:
        return None

def _get_authenticated_user(request: web.Request):
    init_data = request.headers.get('X-Telegram-Init-Data') or request.query.get('initData')
    return validate_init_data(init_data)

def json_error(message: str, status: int = 400):
    return web.json_response({"ok": False, "error": message}, status=status)

async def api_me(request: web.Request):
    user = _get_authenticated_user(request)
    if not user:
        return json_error("Unauthorized. Please open this from inside the bot.", 401)
    user_id = user['id']

    if await is_banned(user_id):
        return json_error("You are banned from using this bot.", 403)

    await ensure_user(user_id)
    data = await get_user_data(user_id)
    curr = data['currency']

    async with db_pool.acquire() as conn:
        current_task = await conn.fetchrow('''
            SELECT t.id, t.title, t.details, t.reward, t.status, ta.assigned_at
            FROM task_assignments ta JOIN tasks t ON ta.task_id = t.id
            WHERE ta.user_id=$1 ORDER BY ta.assigned_at DESC LIMIT 1
        ''', user_id)

        total_earning = await conn.fetchval('''
            SELECT COALESCE(SUM(amount), 0) FROM transactions
            WHERE user_id=$1 AND type IN ('task', 'sell', 'referral') AND amount > 0
        ''', user_id)

    task_payload = None
    if current_task and current_task['status'] in ('assigned', 'pending_review'):
        try:
            parts = current_task['details'].split(" | ")
            email = parts[0].replace("Email: ", "").strip()
            password = parts[1].replace("Pass: ", "").strip()
        except Exception:
            email = current_task['title'].replace("Login to ", "").strip()
            password = "See Admin"
        expire_at = None
        if current_task['status'] == 'assigned':
            expire_at = (current_task['assigned_at'] + timedelta(minutes=30)).isoformat() + "Z"
        task_payload = {
            "id": current_task['id'],
            "email": email,
            "password": password,
            "reward_display": format_currency(current_task['reward'], curr),
            "status": current_task['status'],
            "expires_at": expire_at
        }

    return web.json_response({
        "ok": True,
        "user_id": user_id,
        "currency": curr,
        "balance_display": format_currency(data['balance'], curr),
        "upi": data['upi'],
        "usdt_address": data['usdt_address'],
        "ultra_number": data['ultra_number'],
        "min_withdrawal_display": format_currency(MIN_WITHDRAWAL_AMT, curr),
        "total_earning_display": format_currency(total_earning, curr),
        "task_rate_display": format_currency(DEFAULT_TASK_RATE, curr),
        "sell_rate_display": format_currency(GMAIL_SELL_RATE, curr),
        "sell_enabled": SELL_GMAIL_STATUS,
        "ultra_enabled": ULTRA_STATUS,
        "fees": {
            "upi": format_currency(UPI_FEES, curr),
            "usdt": format_currency(USDT_FEES, curr),
            "ultra": format_currency(ULTRA_FEES, curr)
        },
        "current_task": task_payload,
        "single_task_status": SINGLE_TASK_STATUS,
        "bot_status": BOT_STATUS,
        "bot_off_message": None if BOT_STATUS else BOT_OFF_MESSAGE
    })

async def api_claim_task(request: web.Request):
    user = _get_authenticated_user(request)
    if not user:
        return json_error("Unauthorized. Please open this from inside the bot.", 401)
    user_id = user['id']

    if not BOT_STATUS:
        return json_error(BOT_OFF_MESSAGE, 503)
    if await is_banned(user_id):
        return json_error("You are banned from using this bot.", 403)
    if not await check_user_joined_channel(user_id):
        return json_error("Please join our channel first, then try again.", 403)

    await ensure_user(user_id)
    user_data = await get_user_data(user_id)
    user_curr = user_data['currency']

    async with db_pool.acquire() as conn:
        existing = await conn.fetchrow('''
            SELECT t.id, t.title, t.details, t.reward, t.status, a.assigned_at
            FROM task_assignments a JOIN tasks t ON a.task_id = t.id
            WHERE a.user_id=$1 ORDER BY a.assigned_at DESC LIMIT 1
        ''', user_id)

        if existing:
            task_status = existing['status']
            if task_status == 'pending_review':
                if SINGLE_TASK_STATUS:
                    return json_error("Your task submission is under admin review. Please wait for approval.", 409)
                # SINGLE_TASK_STATUS is off (unlimited concurrent tasks) -> fall through and let the user claim another task.
            elif task_status == 'assigned':
                expire_time = existing['assigned_at'] + timedelta(minutes=30)
                if (expire_time - datetime.utcnow()).total_seconds() > 0:
                    return json_error("You already have an active task. Refresh to see it.", 409)
                else:
                    async with conn.transaction():
                        await conn.execute('DELETE FROM task_assignments WHERE user_id=$1 AND task_id=$2', user_id, existing['id'])
                        await conn.execute('UPDATE tasks SET status=$1 WHERE id=$2', 'available', existing['id'])

        task = await conn.fetchrow("SELECT id, title, details, reward FROM tasks WHERE status='available' ORDER BY RANDOM() LIMIT 1")
        if not task:
            return json_error("No tasks available right now.", 404)

        task_id = task['id']
        title = task['title']
        details = task['details']
        reward = task['reward']

        try:
            parts = details.split(" | ")
            username = parts[0].replace("Email: ", "").strip()
        except Exception:
            username = title.replace("Login to ", "").strip()

        password = DEFAULT_TASK_PASS if DEFAULT_TASK_PASS_STATUS else generate_random_password(12)
        new_details = f"Email: {username} | Pass: {password}"

        async with conn.transaction():
            await conn.execute("UPDATE tasks SET status='assigned', details=$1 WHERE id=$2", new_details, task_id)
            # message_id=0: this task card has no linked chat message since it was claimed via the Mini App.
            await conn.execute('INSERT INTO task_assignments(task_id, user_id, message_id) VALUES ($1, $2, 0)', task_id, user_id)
            await conn.execute('INSERT INTO task_history(task_id, user_id, password_used) VALUES ($1, $2, $3)', task_id, user_id, password)

    return web.json_response({
        "ok": True,
        "task": {
            "id": task_id,
            "email": username,
            "password": password,
            "reward_display": format_currency(reward, user_curr),
            "status": "assigned",
            "expires_at": (datetime.utcnow() + timedelta(minutes=30)).isoformat() + "Z"
        }
    })

async def api_submit_task(request: web.Request):
    user = _get_authenticated_user(request)
    if not user:
        return json_error("Unauthorized. Please open this from inside the bot.", 401)
    user_id = user['id']

    async with db_pool.acquire() as conn:
        task = await conn.fetchrow('''
            SELECT t.id, t.title, t.details, t.reward
            FROM task_assignments ta JOIN tasks t ON ta.task_id = t.id
            WHERE ta.user_id=$1 AND t.status = 'assigned'
            ORDER BY ta.assigned_at DESC LIMIT 1
        ''', user_id)

    if not task:
        return json_error("No active assigned task found to submit.", 404)

    task_id = task['id']
    title = task['title']
    details = task['details']

    try:
        parts = details.split(" | ")
        email = parts[0].replace("Email: ", "").strip()
    except Exception:
        email = title.replace("Login to ", "").strip()

    is_valid = await is_gmail_registered(email, user_id=user_id)
    if not is_valid:
        return json_error(f"This Gmail account ({email}) does not exist on Google. Please create it first, then submit again.", 422)

    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE tasks SET status='pending_review' WHERE id=$1", task_id)

    admin_kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text='Approve', icon_custom_emoji_id="6217663806110175239", callback_data=f'ta:{task_id}', style="success"),
        InlineKeyboardButton(text='Decline', icon_custom_emoji_id="5274099962655816924", callback_data=f'td:{task_id}', style="danger")
    ]])
    try:
        await bot.send_message(
            ADMIN_ID,
            f"📥 <b>Task Submitted for Review (via Mini App)</b>\n\n🆔 #{task_id}\n<tg-emoji emoji-id=\"5253742260054409879\">📧</tg-emoji> <code>{email}</code>\n<tg-emoji emoji-id=\"5870458774455587120\">👤</tg-emoji> User: <code>{user_id}</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=admin_kb
        )
    except Exception:
        pass

    return web.json_response({"ok": True, "message": "Task submitted for admin review!"})

async def api_cancel_task(request: web.Request):
    user = _get_authenticated_user(request)
    if not user:
        return json_error("Unauthorized. Please open this from inside the bot.", 401)
    user_id = user['id']

    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT ta.task_id, t.status FROM task_assignments ta JOIN tasks t ON ta.task_id = t.id WHERE ta.user_id=$1 AND t.status != 'completed' ORDER BY ta.assigned_at DESC LIMIT 1",
            user_id
        )
        if not row:
            return json_error("You don't have any active task to cancel.", 404)
        if row['status'] == 'pending_review':
            return json_error("Cannot cancel a task already submitted for admin review.", 409)

        task_id = row['task_id']
        async with conn.transaction():
            await conn.execute('DELETE FROM task_assignments WHERE user_id=$1 AND task_id=$2', user_id, task_id)
            await conn.execute("UPDATE tasks SET status='available' WHERE id=$1", task_id)

    return web.json_response({"ok": True, "message": f"Task #{task_id} cancelled and returned to the pool."})

async def api_sell_gmail(request: web.Request):
    user = _get_authenticated_user(request)
    if not user:
        return json_error("Unauthorized. Please open this from inside the bot.", 401)
    user_id = user['id']

    if not SELL_GMAIL_STATUS:
        return json_error("Selling Gmail is currently disabled by admin.", 403)

    try:
        body = await request.json()
    except Exception:
        return json_error("Invalid request body.")

    username_input = (body.get('username') or '').strip()
    password = (body.get('password') or '').strip()
    if not username_input or not password:
        return json_error("Username and password are required.")

    if "@gmail.com" not in username_input.lower() and "@" not in username_input:
        username = f"{username_input}@gmail.com"
    else:
        username = username_input

    search_pattern = f"%{username.lower()}%"
    async with db_pool.acquire() as conn:
        existing_sell = await conn.fetchval("SELECT id FROM pending_sells WHERE LOWER(details) LIKE $1", search_pattern)
        existing_task = await conn.fetchval("SELECT id FROM tasks WHERE LOWER(title) LIKE $1 OR LOWER(details) LIKE $1", search_pattern)

    if existing_sell or existing_task:
        return json_error("This email is already in the database. You cannot sell the same email twice.", 409)

    is_valid = await is_gmail_registered(username, user_id=user_id)
    if not is_valid:
        return json_error(f"This Gmail account ({username}) does not exist on Google.", 422)

    details = f"Username: {username}\nPassword: {password}"
    async with db_pool.acquire() as conn:
        sell_id = await conn.fetchval(
            "INSERT INTO pending_sells (user_id, details, amount) VALUES ($1, $2, $3) RETURNING id",
            user_id, details, GMAIL_SELL_RATE
        )

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Approve", icon_custom_emoji_id="6217663806110175239", callback_data=f"sa:{sell_id}", style="success"),
        InlineKeyboardButton(text="Decline", icon_custom_emoji_id="5274099962655816924", callback_data=f"sd:{sell_id}", style="danger")
    ]])
    try:
        await bot.send_message(
            ADMIN_ID,
            f"<tg-emoji emoji-id=\"5377548235709619284\">📨</tg-emoji> <b>New Gmail Sell Request #{sell_id} (via Mini App)</b>\n\n<tg-emoji emoji-id=\"5870458774455587120\">👤</tg-emoji> Seller: <code>{user_id}</code>\n<tg-emoji emoji-id=\"5253742260054409879\">📧</tg-emoji> <code>{username}</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=kb
        )
    except Exception:
        pass

    return web.json_response({"ok": True, "message": "Your Gmail was submitted for admin review!"})

async def api_withdraw(request: web.Request):
    user = _get_authenticated_user(request)
    if not user:
        return json_error("Unauthorized. Please open this from inside the bot.", 401)
    user_id = user['id']

    if not BOT_STATUS:
        return json_error(BOT_OFF_MESSAGE, 503)

    try:
        body = await request.json()
    except Exception:
        return json_error("Invalid request body.")

    method = (body.get('method') or '').lower()
    address = (body.get('address') or '').strip()
    if method not in ('upi', 'usdt', 'ultra'):
        return json_error("Invalid withdrawal method.")
    if method == 'ultra' and not ULTRA_STATUS:
        return json_error("Ultra Gateway is currently disabled.", 403)

    user_data = await get_user_data(user_id)
    bal = user_data['balance'] if user_data else 0.0
    curr = user_data['currency'] if user_data else "USD"

    column = {'upi': 'upi', 'usdt': 'usdt_address', 'ultra': 'ultra_number'}[method]
    if address:
        async with db_pool.acquire() as conn:
            await conn.execute(f"UPDATE users SET {column}=$1 WHERE user_id=$2", address, user_id)
        invalidate_user_cache(user_id)
        user_data = await get_user_data(user_id)

    saved_address = user_data.get(column)
    if not saved_address or saved_address == "None":
        return json_error(f"Please provide your {method.upper()} address to withdraw.", 400)

    if bal < MIN_WITHDRAWAL_AMT:
        return json_error(f"Minimum withdrawal is {format_currency(MIN_WITHDRAWAL_AMT, curr)}. Current balance: {format_currency(bal, curr)}", 400)

    total_deducted = bal
    fee = {'upi': UPI_FEES, 'usdt': USDT_FEES, 'ultra': ULTRA_FEES}[method]
    payout_amount = bal - fee

    if method in ('upi', 'usdt'):
        method_label = 'UPI' if method == 'upi' else 'USDT BEP-20'
        async with db_pool.acquire() as conn:
            existing_pending = await conn.fetchrow("SELECT id FROM withdrawals WHERE user_id=$1 AND status='pending'", user_id)
            if existing_pending:
                return json_error("Your previous withdrawal is already pending.", 409)

            withdraw_id = None
            try:
                async with conn.transaction():
                    new_balance = await conn.fetchval(
                        "UPDATE users SET balance = balance - $2 WHERE user_id=$1 AND balance >= $2 RETURNING balance",
                        user_id, total_deducted
                    )
                    if new_balance is None:
                        raise ValueError("balance_changed")
                    withdraw_id = await conn.fetchval(
                        "INSERT INTO withdrawals(user_id, amount, method, payment_address) VALUES ($1, $2, $3, $4) RETURNING id",
                        user_id, payout_amount, method_label, saved_address
                    )
                    await conn.execute(
                        "INSERT INTO transactions (user_id, type, amount, note) VALUES ($1, $2, $3, $4)",
                        user_id, "withdrawal_pending", -total_deducted,
                        f"{method_label} Withdrawal #{withdraw_id} pending (Payout: {payout_amount:.2f}, Fee: {fee:.2f})"
                    )
            except asyncpg.exceptions.UniqueViolationError:
                return json_error("Your previous withdrawal is already pending.", 409)
            except ValueError:
                return json_error("Your balance changed just now. Please try again.", 409)

        invalidate_user_cache(user_id)
        try:
            await bot.send_message(
                ADMIN_ID,
                f"<tg-emoji emoji-id=\"5444856076954520455\">💸</tg-emoji> <b>New Withdrawal Request (via Mini App)</b>\n\n🆔 #{withdraw_id}\n<tg-emoji emoji-id=\"5870458774455587120\">👤</tg-emoji> <code>{user_id}</code>\n<tg-emoji emoji-id=\"5445353829304387411\">💳</tg-emoji> {method_label}: <code>{saved_address}</code>\n<tg-emoji emoji-id=\"5417924076503062111\">💰</tg-emoji> Payout: {format_currency(payout_amount, curr)}",
                parse_mode=ParseMode.HTML
            )
        except Exception:
            pass

        return web.json_response({"ok": True, "message": f"Withdrawal request submitted! Payout: {format_currency(payout_amount, curr)}"})

    else:
        # Ultra Gateway: instant payout. Reserve the balance BEFORE calling the external API,
        # refunding on failure - same pattern used by the in-chat handler.
        async with db_pool.acquire() as conn:
            new_balance = await conn.fetchval(
                "UPDATE users SET balance = balance - $2 WHERE user_id=$1 AND balance >= $2 RETURNING balance",
                user_id, total_deducted
            )
        if new_balance is None:
            return json_error("Your balance changed just now. Please try again.", 409)
        invalidate_user_cache(user_id)

        url = f"https://ultra-pay.store/APIs/api?token={urllib.parse.quote(ULTRA_TOKEN)}&key={urllib.parse.quote(ULTRA_KEY)}&paytoNumber={urllib.parse.quote(saved_address)}&amount={payout_amount:.2f}&comment=iGmail Pay"

        api_success = False
        api_reason = "Unknown Error"
        try:
            session = HTTP_SESSION if HTTP_SESSION and not HTTP_SESSION.closed else aiohttp.ClientSession()
            _own_session = session is not HTTP_SESSION
            try:
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
            finally:
                if _own_session:
                    await session.close()
        except Exception as e:
            api_reason = f"Connection error: {e}"

        if api_success:
            async with db_pool.acquire() as conn:
                async with conn.transaction():
                    await conn.execute("INSERT INTO withdrawals(user_id, amount, method, payment_address, status) VALUES ($1, $2, 'Ultra Gateway', $3, 'paid')", user_id, payout_amount, saved_address)
                    await conn.execute("INSERT INTO transactions (user_id, type, amount, note) VALUES ($1, $2, $3, $4)", user_id, "withdrawal", -total_deducted, "Ultra Gateway instant payout paid")
            return web.json_response({"ok": True, "message": f"Instant payment successful! {format_currency(payout_amount, curr)} sent."})
        else:
            async with db_pool.acquire() as conn:
                await conn.execute("UPDATE users SET balance = balance + $1 WHERE user_id=$2", total_deducted, user_id)
            invalidate_user_cache(user_id)
            return json_error(f"Ultra Gateway payment failed: {api_reason}. Your balance was not deducted.", 502)

async def api_transactions(request: web.Request):
    user = _get_authenticated_user(request)
    if not user:
        return json_error("Unauthorized. Please open this from inside the bot.", 401)
    user_id = user['id']

    if await is_banned(user_id):
        return json_error("You are banned from using this bot.", 403)

    await ensure_user(user_id)
    user_data = await get_user_data(user_id)
    curr = user_data['currency']

    try:
        page = max(1, int(request.query.get('page', 1)))
    except (TypeError, ValueError):
        page = 1
    try:
        page_size = min(max(1, int(request.query.get('page_size', 10))), 50)
    except (TypeError, ValueError):
        page_size = 10

    async with db_pool.acquire() as conn:
        total_items = await conn.fetchval('SELECT COUNT(*) FROM transactions WHERE user_id=$1', user_id)
        total_pages = max(1, (total_items + page_size - 1) // page_size)
        if page > total_pages:
            page = total_pages
        offset = (page - 1) * page_size

        tx_rows = await conn.fetch('''
            SELECT type, amount, note, created_at
            FROM transactions
            WHERE user_id=$1
            ORDER BY id DESC
            LIMIT $2 OFFSET $3
        ''', user_id, page_size, offset)

    items = []
    for tx in tx_rows:
        amt = tx['amount']
        raw_type = (tx['type'] or 'general').lower()
        if raw_type in ('withdrawal', 'withdrawal_paid'):
            tx_type = 'Withdrawal'
        elif raw_type == 'withdrawal_pending':
            tx_type = 'Withdrawal Pending'
        elif raw_type == 'withdrawal_rejected':
            tx_type = 'Withdrawal Rejected'
        elif raw_type == 'refund':
            tx_type = 'Refund'
        elif raw_type == 'task':
            tx_type = 'Task Reward'
        elif raw_type == 'sell':
            tx_type = 'Gmail Sale'
        elif raw_type == 'referral':
            tx_type = 'Referral Bonus'
        else:
            tx_type = raw_type.replace('_', ' ').title()

        items.append({
            "type": tx_type,
            "amount": amt,
            "amount_display": format_currency(abs(amt), curr),
            "positive": amt >= 0,
            "note": tx['note'],
            "created_at": tx['created_at'].isoformat() + "Z" if tx['created_at'] else None
        })

    return web.json_response({
        "ok": True,
        "transactions": items,
        "page": page,
        "page_size": page_size,
        "total_items": total_items,
        "total_pages": total_pages
    })

def register_webapp_routes(app: web.Application):
    app.router.add_get('/api/me', api_me)
    app.router.add_post('/api/tasks/claim', api_claim_task)
    app.router.add_post('/api/tasks/submit', api_submit_task)
    app.router.add_post('/api/tasks/cancel', api_cancel_task)
    app.router.add_post('/api/sell', api_sell_gmail)
    app.router.add_post('/api/withdraw', api_withdraw)
    app.router.add_get('/api/transactions', api_transactions)
    webapp_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'webapp')
    if os.path.isdir(webapp_dir):
        app.router.add_get('/webapp/', lambda r: web.FileResponse(os.path.join(webapp_dir, 'index.html')))
        app.router.add_static('/webapp/', webapp_dir, show_index=False)

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
    waiting_for_broadcast_target = State()
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
    waiting_for_giveaway_message = State()
    waiting_for_giveaway_emoji = State()
    waiting_for_giveaway_target = State()
    waiting_for_dustbin_replace = State()
    waiting_for_video_link = State()
    waiting_for_bot_off_message = State()

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
        min_size=5, 
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
        await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS last_active TIMESTAMP DEFAULT CURRENT_TIMESTAMP")

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
        # Guarantees at the database level that a user can never have two 'pending' withdrawals
        # at once, even if two requests race past an in-app check at the same instant.
        await conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_withdrawals_one_pending_per_user ON withdrawals (user_id) WHERE status = 'pending'")
        
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

        await conn.execute('''
            CREATE TABLE IF NOT EXISTS giveaways (
                id SERIAL PRIMARY KEY,
                emoji_type TEXT DEFAULT 'dice',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS giveaway_plays (
                id SERIAL PRIMARY KEY,
                giveaway_id INT,
                user_id BIGINT,
                value INT,
                reward DOUBLE PRECISION,
                played_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(giveaway_id, user_id)
            )
        ''')
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS dustbin_tasks (
                id SERIAL PRIMARY KEY,
                title TEXT,
                details TEXT,
                reward DOUBLE PRECISION,
                added_by BIGINT DEFAULT NULL,
                removed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

async def load_settings_and_cache():
    global BANNED_USERS_CACHE, MUST_JOIN_CHANNEL, BOT_USERNAME, BOT_STATUS, BOT_OFF_MESSAGE, REF_STATUS, ULTRA_STATUS, SINGLE_TASK_STATUS, SELL_GMAIL_STATUS, EMAILABLE_API_KEY, VALIDATOR_ENABLED, VALIDATOR_PROVIDER, ADMIN_ID
    global DEFAULT_TASK_RATE, GMAIL_SELL_RATE, MIN_WITHDRAWAL_AMT, DEFAULT_TASK_PASS, DEFAULT_TASK_PASS_STATUS, UPI_FEES, USDT_FEES, ULTRA_FEES, ULTRA_TOKEN, ULTRA_KEY
    global TASK_VIDEO_LINK, SELL_VIDEO_LINK, HOWTO_VIDEO_LINK
    
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT user_id FROM banned_users")
        BANNED_USERS_CACHE = {r['user_id'] for r in rows}
        
        channel_val = await conn.fetchval("SELECT value FROM bot_settings WHERE key='must_join_channel'")
        MUST_JOIN_CHANNEL = channel_val if channel_val else None

        status_val = await conn.fetchval("SELECT value FROM bot_settings WHERE key='bot_status'")
        BOT_STATUS = (status_val != 'off')

        off_msg_val = await conn.fetchval("SELECT value FROM bot_settings WHERE key='bot_off_message'")
        if off_msg_val:
            BOT_OFF_MESSAGE = off_msg_val

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

        tv_link = await conn.fetchval("SELECT value FROM bot_settings WHERE key='video_task_link'")
        TASK_VIDEO_LINK = tv_link if tv_link else None

        sv_link = await conn.fetchval("SELECT value FROM bot_settings WHERE key='video_sell_link'")
        SELL_VIDEO_LINK = sv_link if sv_link else None

        hv_link = await conn.fetchval("SELECT value FROM bot_settings WHERE key='video_howto_link'")
        HOWTO_VIDEO_LINK = hv_link if hv_link else None

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

async def cleanup_last_menu(message: Message, state: FSMContext):
    """Deletes the previous prompt/menu message (tracked via 'last_menu_msg_id')
    before a flow's completion reply is sent. This is what stops the bot from
    showing a stale prompt (with its old Cancel/Back button) at the same time
    as a brand-new confirmation+menu message — i.e. the 'double menu' bug."""
    data = await state.get_data()
    old_msg_id = data.get('last_menu_msg_id')
    if old_msg_id:
        try:
            await bot.delete_message(chat_id=message.chat.id, message_id=old_msg_id)
        except Exception:
            pass

_LAST_ACTIVE_WRITE = {}  # {user_id: last_write_timestamp} - throttles redundant writes

async def update_last_active(user_id: int):
    now = time.time()
    if now - _LAST_ACTIVE_WRITE.get(user_id, 0) < 60:
        return  # already written within the last minute - broadcast windows are hour-based, so this is lossless
    _LAST_ACTIVE_WRITE[user_id] = now
    try:
        async with db_pool.acquire() as conn:
            await conn.execute(
                "UPDATE users SET last_active = CURRENT_TIMESTAMP WHERE user_id=$1",
                user_id
            )
    except Exception:
        pass

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
    cached = USER_CACHE.get(user_id)
    if cached is not None:
        return cached
    await ensure_user(user_id)
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT balance, upi, usdt_address, ultra_number, notifications_enabled, currency, referred_by, referral_earnings FROM users WHERE user_id=$1", 
            user_id
        )
        data = dict(row) if row else None
        if data is not None:
            USER_CACHE[user_id] = data
        return data

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
    kb.button(text="Join Channel", icon_custom_emoji_id="5332724926216428039", url=channel_url)
    kb.button(
        text="Joined / Verify", icon_custom_emoji_id="6217663806110175239", 
        callback_data="check_must_join",
        style="success"
    )
    kb.adjust(1, 1)
    return kb.as_markup()

def get_main_menu_keyboard():
    kb = InlineKeyboardBuilder()
    kb.button(
        text="Get Task", icon_custom_emoji_id="5197269100878907942",
        callback_data="menu_get_task",
        style="success"
    )
    kb.button(
        text="Balance", icon_custom_emoji_id="5417924076503062111",
        callback_data="menu_balance",
        style="primary"
    )
    kb.button(
        text="Sell Gmail", icon_custom_emoji_id="5377548235709619284",
        callback_data="menu_sell_gmail",
        style="success"
    )
    kb.button(
        text="History", icon_custom_emoji_id="5440410042773824003",
        callback_data="menu_history",
        style="primary"
    )
    kb.button(
        text="Referrals", icon_custom_emoji_id="5391292736647209211",
        callback_data="menu_referrals",
        style="success"
    )
    kb.button(
        text="My Accounts",
        icon_custom_emoji_id="6300651782678781823",
        callback_data="menu_my_accounts",
        style="primary"
    )
    if WEBAPP_URL:
        kb.button(
            text="Open Mini App", icon_custom_emoji_id="5201691993775818138",
            web_app=WebAppInfo(url=WEBAPP_URL)
        )
    kb.button(
        text="Settings", icon_custom_emoji_id="5893161718179173515",
        callback_data="menu_settings"
    )
    kb.button(
        text="Support", icon_custom_emoji_id="5471960722206366390",
        callback_data="menu_support",
        style="danger"
    )
    if HOWTO_VIDEO_LINK:
        kb.button(
            text="🎬 How To Use Bot",
            url=HOWTO_VIDEO_LINK,
            style="primary"
        )
        kb.adjust(2, 2, 2, 1, 1, 2) if WEBAPP_URL else kb.adjust(2, 2, 2, 1, 2)
    else:
        kb.adjust(2, 2, 2, 1, 1, 1) if WEBAPP_URL else kb.adjust(2, 2, 2, 1, 1)
    return kb.as_markup()

def get_add_task_type_keyboard():
    kb = InlineKeyboardBuilder()
    kb.button(
        text="Single Add", icon_custom_emoji_id="5397916757333654639", 
        callback_data="admin_add_task_single", 
        style="success"
    )
    kb.button(
        text="Bulk Add", icon_custom_emoji_id="5472027899789843495", 
        callback_data="admin_add_task_bulk", 
        style="primary"
    )
    kb.adjust(2)
    return kb.as_markup()

def get_referral_inline_keyboard(user_id: int):
    invite_link = f"https://t.me/{BOT_USERNAME}?start={user_id}"
    custom_share_text = "<tg-emoji emoji-id=\"5201691993775818138\">🚀</tg-emoji>Join Gmail Earnex and Start Earning Money!<tg-emoji emoji-id=\"6217663806110175239\">✅</tg-emoji>"
    share_url = f"https://t.me/share/url?url={urllib.parse.quote(invite_link)}&text={urllib.parse.quote(custom_share_text)}"
    
    kb = InlineKeyboardBuilder()
    kb.button(
        text="Copy link", icon_custom_emoji_id="5271604874419647061",
        copy_text=CopyTextButton(text=invite_link),
        style="primary"
    )
    kb.button(
        text="Share link", icon_custom_emoji_id="5305265301917549162",
        url=share_url,
        style="primary"
    )
    kb.button(
        text="Back", icon_custom_emoji_id="5875082500023258804",
        callback_data="menu_back"
    )
    kb.adjust(2, 1)
    return kb.as_markup()

def get_settings_keyboard(notif_enabled: bool, currency: str):
    kb = InlineKeyboardBuilder()
    notif_text = "Notifications: ON" if notif_enabled else "Notifications: OFF"
    curr_text = f"Currency: {currency} ({'$' if currency=='USD' else '₹'})"
    notif_icon = "6217663806110175239" if notif_enabled else "5274099962655816924"

    kb.button(text=notif_text, icon_custom_emoji_id=notif_icon, callback_data="toggle_notif", style="primary")
    kb.button(text=curr_text, icon_custom_emoji_id="5197434882321567830", callback_data="toggle_currency", style="primary")
    kb.button(
        text="Back", icon_custom_emoji_id="5875082500023258804",
        callback_data="menu_back"
    )
    kb.adjust(1, 1, 1)
    return kb.as_markup()

def get_admin_menu_keyboard():
    kb = ReplyKeyboardBuilder()
    
    kb.button(text="Add Task", icon_custom_emoji_id="5397916757333654639", style="success")
    kb.button(text="Tasks", icon_custom_emoji_id="5197269100878907942", style="primary")
    
    kb.button(text="Available Tasks", icon_custom_emoji_id="5416081784641168838", style="primary")
    kb.button(text="Pending Reviews", icon_custom_emoji_id="5395444784611480792", style="primary")
    
    kb.button(text="Pending Withdrawals", icon_custom_emoji_id="5444856076954520455", style="primary")
    kb.button(text="Chat", icon_custom_emoji_id="5265079444707486638", style="primary")
    
    kb.button(text="Unassign Tasks", icon_custom_emoji_id="5262529363710060188", style="danger")
    kb.button(text="Find ID", icon_custom_emoji_id="5307843983102204243", style="primary")
    
    kb.button(text="Add Balance", icon_custom_emoji_id="5397916757333654639", style="success")
    kb.button(text="Cut Balance", icon_custom_emoji_id="5240241223632954241", style="danger")
    kb.button(text="Check Balance", icon_custom_emoji_id="5215420556089776398", style="primary")
    kb.button(text="Top Balances", icon_custom_emoji_id="6183525361637136222", style="primary")
    kb.button(text="Ban User", icon_custom_emoji_id="5240241223632954241", style="danger")
    kb.button(text="Unban User", icon_custom_emoji_id="6217663806110175239", style="success")
    kb.button(text="Broadcast", icon_custom_emoji_id="5332724926216428039", style="primary")
    kb.button(text="Change Values", icon_custom_emoji_id="5893161718179173515", style="primary")
    kb.button(text="Remove Task", icon_custom_emoji_id="5262529363710060188", style="danger")
    kb.button(text="Transactions", icon_custom_emoji_id="5445353829304387411", style="primary")
    kb.button(text="View Stats", icon_custom_emoji_id="5244837092042750681", style="primary")
    kb.button(text="Must Join Channel", icon_custom_emoji_id="5332724926216428039", style="primary")
    
    status_btn_text = "🟢 Bot Status: ON" if BOT_STATUS else "🔴 Bot Status: OFF"
    kb.button(text=status_btn_text, style="danger" if BOT_STATUS else "success")
    
    ref_btn_text = "🟢 Ref Status: ON" if REF_STATUS else "🔴 Ref Status: OFF"
    kb.button(text=ref_btn_text, style="success" if REF_STATUS else "danger")
    
    kb.button(text="Validator", icon_custom_emoji_id="5893161718179173515", style="primary")
    ultra_btn_text = "🟢 Ultra Status: ON" if ULTRA_STATUS else "🔴 Ultra Status: OFF"
    kb.button(text=ultra_btn_text, style="success" if ULTRA_STATUS else "danger")

    kb.button(text="Dustbin", icon_custom_emoji_id="5309832892262654231", style="danger")
    kb.button(text="Videos", icon_custom_emoji_id="5305265301917549162", style="primary")

    kb.button(text="Manage Workers", icon_custom_emoji_id="5264713049637409446", style="primary")
    kb.button(text="Transfer Admin", icon_custom_emoji_id="5217822164362739968", style="danger")

    kb.button(text="Main Menu", icon_custom_emoji_id="5416041192905265756", style="primary")
    
    kb.adjust(2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 1)
    return kb.as_markup(resize_keyboard=True)

def get_pending_reviews_inline_keyboard():
    kb = InlineKeyboardBuilder()
    kb.button(
        text="Sell Gmail", icon_custom_emoji_id="5377548235709619284",
        callback_data="admin_view_pending_sells",
        style="primary"
    )
    kb.button(
        text="Task Gmail", icon_custom_emoji_id="5197269100878907942",
        callback_data="admin_view_pending_tasks",
        style="primary"
    )
    kb.adjust(2)
    return kb.as_markup()

def get_pending_withdrawals_inline_keyboard():
    kb = InlineKeyboardBuilder()
    kb.button(
        text="UPI", icon_custom_emoji_id="6291696801636424911",
        callback_data="admin_view_pending_withdraw_upi",
        style="primary"
    )
    kb.button(
        text="USDT BEP-20", icon_custom_emoji_id="5197434882321567830",
        callback_data="admin_view_pending_withdraw_usdt",
        style="primary"
    )
    if ULTRA_STATUS:
        kb.button(
            text="Ultra Gateway", icon_custom_emoji_id="5195033767969839232",
            callback_data="admin_view_pending_withdraw_ultra",
            style="primary"
        )
        kb.adjust(3)
    else:
        kb.adjust(2)
    return kb.as_markup()

def get_change_values_inline_keyboard():
    kb = InlineKeyboardBuilder()
    kb.button(
        text="1. Change Tasks Rate", icon_custom_emoji_id="5417924076503062111",
        callback_data="admin_change_tasks_rate",
        style="primary"
    )
    kb.button(
        text="2. Change Sell Rate", icon_custom_emoji_id="5377548235709619284",
        callback_data="admin_change_sell_rate",
        style="primary"
    )
    kb.button(
        text="3. Change Min. Withdrawal", icon_custom_emoji_id="5444856076954520455",
        callback_data="admin_change_min_withdraw",
        style="primary"
    )
    kb.button(
        text="4. Change Task Password", icon_custom_emoji_id="6005570495603282482",
        callback_data="admin_change_task_pass",
        style="primary"
    )
    
    pass_mode_text = f"🔑 5. Password Mode: {'🟢 Fixed (Default)' if DEFAULT_TASK_PASS_STATUS else '🔴 Random'}"
    kb.button(
        text=pass_mode_text,
        callback_data="admin_toggle_task_pass_mode",
        style="success" if DEFAULT_TASK_PASS_STATUS else "danger"
    )

    kb.button(
        text="6. Change Fees", icon_custom_emoji_id="5417924076503062111",
        callback_data="admin_change_fees",
        style="primary"
    )
    kb.button(
        text="7. Change Ultra", icon_custom_emoji_id="5195033767969839232",
        callback_data="admin_change_ultra",
        style="primary"
    )
    
    single_task_btn_text = f"✍️ 8. Single Tasks: {'🟢 ON' if SINGLE_TASK_STATUS else '🔴 OFF'}"
    kb.button(
        text=single_task_btn_text,
        callback_data="admin_toggle_single_task",
        style="success" if SINGLE_TASK_STATUS else "danger"
    )

    sell_gmail_btn_text = f"📨 9. Sell Gmail: {'🟢 ON' if SELL_GMAIL_STATUS else '🔴 OFF'}"
    kb.button(
        text=sell_gmail_btn_text,
        callback_data="admin_toggle_sell_gmail",
        style="success" if SELL_GMAIL_STATUS else "danger"
    )

    kb.adjust(2, 2, 2, 2, 1)
    return kb.as_markup()

def get_validator_admin_inline_keyboard():
    kb = InlineKeyboardBuilder()
    status_toggle_text = "🔴 Deactivate" if VALIDATOR_ENABLED else "🟢 Activate"
    status_style = "danger" if VALIDATOR_ENABLED else "success"

    kb.button(
        text="Change Key", icon_custom_emoji_id="6005570495603282482", 
        callback_data="admin_validator_change_key", 
        style="primary"
    )
    kb.button(
        text="🔄 Change Provider", 
        callback_data="admin_validator_change_provider", 
        style="primary"
    )
    kb.button(
        text=status_toggle_text, 
        callback_data="admin_validator_toggle_status", 
        style=status_style
    )
    kb.adjust(2, 1)
    return kb.as_markup()

UNASSIGN_MENU_TEXT = (
    "<tg-emoji emoji-id=\"5262529363710060188\">🗑</tg-emoji> <b>Unassign Active Tasks</b>\n\n"
    "Choose an option below:\n"
    "• <b>User ID:</b> Unassign current active task of a specific user.\n"
    "• <b>All Users:</b> Unassign all active tasks across all users and return them to the pool."
)

def get_unassign_inline_keyboard():
    kb = InlineKeyboardBuilder()
    kb.button(
        text="User ID", icon_custom_emoji_id="5870458774455587120", 
        callback_data="unassign_by_user_id", 
        style="primary"
    )
    kb.button(
        text="All Users", icon_custom_emoji_id="5391292736647209211", 
        callback_data="unassign_all_users", 
        style="danger"
    )
    kb.adjust(2)
    return kb.as_markup()

def get_balance_inline_keyboard(upi_set: bool, usdt_set: bool, ultra_set: bool = False):
    kb = InlineKeyboardBuilder()
    upi_link_text = "Change UPI" if upi_set else "Link UPI"
    usdt_link_text = "Change USDT" if usdt_set else "Link USDT BEP-20"

    kb.button(text=upi_link_text, icon_custom_emoji_id="6291696801636424911", callback_data="link_upi", style="primary")
    kb.button(text=usdt_link_text, icon_custom_emoji_id="5197434882321567830", callback_data="link_usdt", style="primary")
    
    if ULTRA_STATUS:
        ultra_link_text = "Change Ultra" if ultra_set else "Link Ultra Gateway"
        kb.button(text=ultra_link_text, icon_custom_emoji_id="5195033767969839232", callback_data="link_ultra", style="primary")

    kb.button(
        text="Withdraw", icon_custom_emoji_id="5444856076954520455", 
        callback_data="choose_withdraw_method", 
        style="success"
    )
    kb.button(
        text="Back", icon_custom_emoji_id="5875082500023258804",
        callback_data="menu_back"
    )
    if ULTRA_STATUS:
        kb.adjust(2, 1, 1, 1)
    else:
        kb.adjust(2, 1, 1)
    return kb.as_markup()

def get_withdraw_options_keyboard():
    kb = InlineKeyboardBuilder()
    kb.button(text=f"Withdraw via UPI (Fee: ₹{UPI_FEES:.2f})", icon_custom_emoji_id="6291696801636424911", callback_data="withdraw_upi", style="success")
    kb.button(text=f"Withdraw via USDT BEP-20 (Fee: ₹{USDT_FEES:.2f})", icon_custom_emoji_id="5197434882321567830", callback_data="withdraw_usdt", style="success")
    if ULTRA_STATUS:
        kb.button(text=f"Withdraw via Ultra Gateway (0 Fees)", icon_custom_emoji_id="5195033767969839232", callback_data="withdraw_ultra", style="success")
    kb.button(
        text="Back", icon_custom_emoji_id="5875082500023258804",
        callback_data="menu_balance"
    )
    if ULTRA_STATUS:
        kb.adjust(1, 1, 1, 1)
    else:
        kb.adjust(1, 1, 1)
    return kb.as_markup()

def get_back_inline_keyboard(callback_data: str = "menu_back"):
    kb = InlineKeyboardBuilder()
    kb.button(
        text="Back", icon_custom_emoji_id="5875082500023258804",
        callback_data=callback_data
    )
    kb.adjust(1)
    return kb.as_markup()

def get_task_action_keyboard():
    rows = [[
        InlineKeyboardButton(
            text="✔️ Submit", 
            callback_data="user_submit_task", 
            style="success"
        ),
        InlineKeyboardButton(
            text="Cancel", icon_custom_emoji_id="5240241223632954241", 
            callback_data="user_cancel_task", 
            style="danger"
        )
    ]]
    if TASK_VIDEO_LINK:
        rows.append([InlineKeyboardButton(text="Tutorial Video", icon_custom_emoji_id="5282843764451195532", url=TASK_VIDEO_LINK)])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def get_support_cancel_keyboard():
    kb = InlineKeyboardBuilder()
    kb.button(
        text="Back", icon_custom_emoji_id="5875082500023258804",
        callback_data="menu_back"
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
        text = "<tg-emoji emoji-id=\"5197269100878907942\">📋</tg-emoji> <b>All System Tasks</b>\n\n<tg-emoji emoji-id=\"5262831879731555779\">📭</tg-emoji> No tasks found in the database."
    else:
        text = (
            f"<tg-emoji emoji-id=\"5197269100878907942\">📋</tg-emoji> <b>All System Tasks</b>\n"
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
                status_str = "<tg-emoji emoji-id=\"5416081784641168838\">🟢</tg-emoji> Available"
            elif status_raw == 'assigned':
                status_str = "🔵 Assigned"
            elif status_raw == 'pending_review':
                status_str = "🟡 Under Review"
            elif status_raw == 'completed':
                status_str = "<tg-emoji emoji-id=\"6217663806110175239\">✅</tg-emoji> Completed"
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
                f"<tg-emoji emoji-id=\"5253742260054409879\">📧</tg-emoji> <b>Gmail:</b> <code>{email}</code>\n"
                f"<tg-emoji emoji-id=\"5870458774455587120\">👤</tg-emoji> <b>User Info:</b> {user_info_str}\n"
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
        text = "<tg-emoji emoji-id=\"5416081784641168838\">🟢</tg-emoji> <b>Available & Assigned Tasks</b>\n\n<tg-emoji emoji-id=\"5262831879731555779\">📭</tg-emoji> No available or assigned tasks found in the database."
    else:
        text = (
            f"<tg-emoji emoji-id=\"5416081784641168838\">🟢</tg-emoji> <b>Available & Assigned Tasks</b>\n"
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
                status_str = "<tg-emoji emoji-id=\"5416081784641168838\">🟢</tg-emoji> Available"
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
                f"🆔 <b>Task #{task_id}</b> | <tg-emoji emoji-id=\"5237699328843200968\">📌</tg-emoji> <b>Type:</b> {status_str}\n"
                f"<tg-emoji emoji-id=\"5253742260054409879\">📧</tg-emoji> <b>Gmail:</b> <code>{email}</code>\n"
                f"<tg-emoji emoji-id=\"5870458774455587120\">👤</tg-emoji> <b>User Info:</b> {user_info_str}\n"
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

    header_title = f"<tg-emoji emoji-id=\"5445353829304387411\">💳</tg-emoji> <b>Transaction History (User <code>{target_user_id}</code>)</b>" if is_admin else '<tg-emoji emoji-id=\"5440410042773824003\">📜</tg-emoji> <b>Transaction History</b>'

    if total_items == 0:
        text = f"{header_title}\n\n<tg-emoji emoji-id=\"5262831879731555779\">📭</tg-emoji> No transaction records found."
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

            type_emoji = "<tg-emoji emoji-id=\"5416081784641168838\">🟢</tg-emoji>" if amt >= 0 else "<tg-emoji emoji-id=\"5411225014148014586\">🔴</tg-emoji>"
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
        kb.row(InlineKeyboardButton(text="Back", icon_custom_emoji_id="5875082500023258804", callback_data="menu_back"))

    return text, kb.as_markup()

# ============================================
# GLOBAL BAN, BOT STATUS & MUST-JOIN MIDDLEWARES
# ============================================

@dp.message.outer_middleware()
async def global_message_middleware(handler, event: Message, data):
    if not event.from_user:
        return await handler(event, data)

    user_id = event.from_user.id

    asyncio.create_task(update_last_active(user_id))

    if user_id == ADMIN_ID:
        return await handler(event, data)
        
    if not BOT_STATUS:
        await event.answer(BOT_OFF_MESSAGE)
        return

    if await is_banned(user_id):
        await event.answer("🚫 You are banned from using this bot.")
        return

    if MUST_JOIN_CHANNEL and not await check_user_joined_channel(user_id):
        await event.answer(
            f'❗️ <b>You must join our main channel to use this bot!</b>\n\n'
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

    asyncio.create_task(update_last_active(user_id))

    if user_id == ADMIN_ID:
        return await handler(event, data)
        
    if not BOT_STATUS:
        try:
            await event.answer(BOT_OFF_MESSAGE, show_alert=True)
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
            f'<tg-emoji emoji-id=\"6217663806110175239\">✅</tg-emoji> <b>Verification successful! You can now use the bot.</b>',
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
            '❗️ <b>You left our official channel!</b>\n\nAccess to the bot has been paused. Rejoin the channel to use the bot again.',
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
                '<tg-emoji emoji-id=\"5195448447062251797\">👋</tg-emoji> <b>Welcome to Gmail Earnex!</b>\n\n'
                '💵 <b>Default Currency Selected:</b> <code>USD ($)</code>\n'
                '<tg-emoji emoji-id=\"5893161718179173515\">⚙️</tg-emoji> <i>You can change your currency anytime in <b>Settings</b>.</i>\n\n'
                'Choose an option from the menu below:'
            )
        else:
            text = (
                '<tg-emoji emoji-id=\"5195448447062251797\">👋</tg-emoji> <b>Welcome back.</b>\n\n'
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
    sent_msg = await message.answer('❗️ Current operation cancelled.', reply_markup=get_main_menu_keyboard(), parse_mode=ParseMode.HTML)
    await state.update_data(last_menu_msg_id=sent_msg.message_id)

@dp.message(F.text == "Main Menu", StateFilter("*"))
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
        '<tg-emoji emoji-id=\"5195448447062251797\">👋</tg-emoji> <b>Welcome back.</b>\n\n'
        'Choose an option from the menu below:'
    )
    try:
        await call.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=get_main_menu_keyboard())
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            sent_msg = await call.message.answer(text, parse_mode=ParseMode.HTML, reply_markup=get_main_menu_keyboard())
            await state.update_data(last_menu_msg_id=sent_msg.message_id)
        else:
            await state.update_data(last_menu_msg_id=call.message.message_id)
    else:
        await state.update_data(last_menu_msg_id=call.message.message_id)

@dp.callback_query(F.data == "back_admin_menu")
async def cb_back_admin_menu(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    await call.answer()
    await state.clear()
    try:
        await call.message.edit_text(
            "<tg-emoji emoji-id=\"5471960722206366390\">🛠</tg-emoji> <b>Returned to Admin Menu.</b>\n\nChoose an option from the menu below.",
            parse_mode=ParseMode.HTML,
            reply_markup=None
        )
    except Exception:
        pass
    await call.message.answer("🏠 Returned to Admin Menu.", reply_markup=get_admin_menu_keyboard())

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
        f'<tg-emoji emoji-id=\"5391292736647209211\">👥</tg-emoji> <b>My Referrals</b>\n'
        f'━━━━━━━━━━━━━━━━━━\n'
        f'<b>Total earnings:</b> {formatted_earnings}\n'
        f'<b>Invited users:</b> {invited_users_count}\n'
        f'<b>Approved referral accounts:</b> {approved_ref_accounts}\n'
        f'━━━━━━━━━━━━━━━━━━\n'
        f'ℹ️ <b>How it works</b>\n'
        f'Share your invite link. Every time someone you invited gets a Gmail account accepted, you earn a cash referral reward — for a lifetime. No limit, it never expires.\n\n'
        f'💵 <b>Referral Rewards</b>\n'
        f'Sell Gmail accepted account: {rate_sell}\n'
        f'Task Gmail accepted account: {rate_task}\n'
        f'Paid on every accepted account from your referrals — for life.\n\n'
        f'<tg-emoji emoji-id=\"5271604874419647061\">🔗</tg-emoji> <b>Your invite link:</b>\n'
        f'<code>{invite_link}</code>'
    )

    try:
        await call.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=get_referral_inline_keyboard(user_id))
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
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
            status_str = "<tg-emoji emoji-id=\"5416081784641168838\">🟢</tg-emoji> Approved & Paid"
        else:
            status_str = "<tg-emoji emoji-id=\"5411225014148014586\">🔴</tg-emoji> Declined / Rejected"

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
            status_str = "<tg-emoji emoji-id=\"5416081784641168838\">🟢</tg-emoji> Approved & Paid"
        else:
            status_str = "<tg-emoji emoji-id=\"5411225014148014586\">🔴</tg-emoji> Declined / Rejected"

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
            'status': "<tg-emoji emoji-id=\"5416081784641168838\">🟢</tg-emoji> Approved & Paid",
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
            '<tg-emoji emoji-id=\"5350387571199319521\">🏷</tg-emoji>️ <b>My Accounts</b>\n\n'
            "<tg-emoji emoji-id=\"5262831879731555779\">📭</tg-emoji> You haven't submitted any Gmail accounts yet."
        )
    else:
        text = (
            f'<tg-emoji emoji-id=\"5350387571199319521\">🏷</tg-emoji>️ <b>My Accounts</b>\n'
            f'You have <b>{total_items}</b> submitted Gmail accounts.\n'
            f'Showing <b>{start_idx + 1}-{end_idx}</b> of <b>{total_items}</b>.\n\n'
        )

        for item in page_items:
            date_fmt = item['date'].strftime("%b %d %I:%M %p")
            text += (
                f"<code>{item['title']}</code>\n"
                f"<tg-emoji emoji-id=\"5237699328843200968\">📌</tg-emoji> <b>Type:</b> {item['type']}\n"
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

    kb.row(InlineKeyboardButton(text="Back", icon_custom_emoji_id="5875082500023258804", callback_data="menu_back"))

    return text, kb.as_markup()

@dp.callback_query(F.data == "menu_my_accounts")
async def cb_my_accounts(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.clear()
    text, reply_markup = await render_my_accounts_page(call.from_user.id, page=1)
    try:
        await call.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
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
        '<tg-emoji emoji-id=\"5893161718179173515\">⚙️</tg-emoji> <b>Settings</b>\n\n'
        '<tg-emoji emoji-id=\"5893161718179173515\">⚙️</tg-emoji> Customize your preferences using the options below:'
    )
    try:
        await call.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=get_settings_keyboard(notif, curr))
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
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
                    txt = '<tg-emoji emoji-id=\"5201691993775818138\">🚀</tg-emoji> Your task submission is currently under admin review. Please wait for approval before taking another task.'
                    try:
                        await call.message.edit_text(txt, reply_markup=get_main_menu_keyboard(), parse_mode=ParseMode.HTML)
                    except TelegramBadRequest as e:
                        if "message is not modified" not in str(e):
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
                        f'<tg-emoji emoji-id=\"5420323339723881652\">⚠️</tg-emoji> <b>You already have an active task.</b>\n\n'
                        f'<tg-emoji emoji-id=\"5310278924616356636\">🎯</tg-emoji> <b>Your Current Task</b>\n\n'
                        f'🆔 #{task_id}\n'
                        f'<tg-emoji emoji-id=\"5253742260054409879\">📧</tg-emoji> <b>Email:</b> {username} | <tg-emoji emoji-id=\"6005570495603282482\">🔑</tg-emoji> <b>Password:</b> <code>{password}</code>\n'
                        f'<tg-emoji emoji-id=\"5417924076503062111\">💰</tg-emoji> <b>Reward:</b> {reward_str}\n\n'
                        f'<tg-emoji emoji-id=\"5201691993775818138\">🚀</tg-emoji> Time Remaining: {mins}m {secs}s'
                    )
                    try:
                        await call.message.edit_text(txt, parse_mode=ParseMode.HTML, reply_markup=get_task_action_keyboard())
                        await conn.execute("UPDATE task_assignments SET message_id=$1 WHERE task_id=$2", call.message.message_id, task_id)
                    except TelegramBadRequest as e:
                        if "message is not modified" not in str(e):
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
            txt = f'<tg-emoji emoji-id=\"5262831879731555779\">📭</tg-emoji> No tasks available right now.'
            try:
                await call.message.edit_text(txt, reply_markup=get_main_menu_keyboard())
            except TelegramBadRequest as e:
                if "message is not modified" not in str(e):
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
        f'<tg-emoji emoji-id=\"5310278924616356636\">🎯</tg-emoji> <b>Task #{task_id}</b>\n\n'
        f'<tg-emoji emoji-id=\"5253742260054409879\">📧</tg-emoji> <b>Email:</b> {username} | <tg-emoji emoji-id=\"6005570495603282482\">🔑</tg-emoji> <b>Password:</b> <code>{password}</code>\n'
        f'<tg-emoji emoji-id=\"5417924076503062111\">💰</tg-emoji> <b>Reward:</b> {reward_str}\n\n'
        f'<tg-emoji emoji-id=\"5201691993775818138\">🚀</tg-emoji> You have ONLY 30 MINUTES to complete this task.'
    )
    try:
        await call.message.edit_text(txt, parse_mode=ParseMode.HTML, reply_markup=get_task_action_keyboard())
        async with db_pool.acquire() as conn:
            await conn.execute("UPDATE task_assignments SET message_id=$1 WHERE task_id=$2", call.message.message_id, task_id)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
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
    
    ultra_line = f'\n<tg-emoji emoji-id=\"5195033767969839232\">⚡️</tg-emoji> <b>Ultra Gateway:</b> <code>{ultra}</code>' if ULTRA_STATUS else ""
    
    text = (
        f'<tg-emoji emoji-id=\"5445353829304387411\">💳</tg-emoji> <b>Balance</b>\n\n'
        f'💵 <b>Available:</b> {formatted_bal}\n'
        f'<tg-emoji emoji-id=\"6291696801636424911\">🏦</tg-emoji> <b>UPI:</b> <code>{upi}</code>\n'
        f'<tg-emoji emoji-id=\"5197434882321567830\">🪙</tg-emoji> <b>USDT BEP-20:</b> <code>{usdt}</code>'
        f'{ultra_line}'
    )
    
    try:
        await call.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=get_balance_inline_keyboard(upi_set, usdt_set, ultra_set))
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
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
        f'<tg-emoji emoji-id=\"5350387571199319521\">🏷</tg-emoji>️ <b>Sell Price {rate_str}/Gmail</b>\n\n'
        '🤑 <b>Step 1/2:</b> Please send the Gmail <b>Username</b> (e.g., <code>example@gmail.com</code>):'
    )
    sell_kb = InlineKeyboardBuilder()
    if SELL_VIDEO_LINK:
        sell_kb.button(text="Tutorial Video", icon_custom_emoji_id="5282843764451195532", url=SELL_VIDEO_LINK)
    sell_kb.button(text="Back", icon_custom_emoji_id="5875082500023258804", callback_data="menu_back")
    sell_kb.adjust(1)
    sell_markup = sell_kb.as_markup()
    try:
        await call.message.edit_text(txt, parse_mode=ParseMode.HTML, reply_markup=sell_markup)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            await call.message.answer(txt, parse_mode=ParseMode.HTML, reply_markup=sell_markup)
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
            "<tg-emoji emoji-id=\"5274099962655816924\">❌</tg-emoji> <b>This email is already in the database. You cannot sell the same email twice.</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=get_main_menu_keyboard()
        )
        await state.clear()
        return

    is_valid = await is_gmail_registered(username, user_id=message.from_user.id)
    if not is_valid:
        retry_msg = await message.answer(
            f"<tg-emoji emoji-id=\"5274099962655816924\">❌</tg-emoji> This Gmail account ({username}) does not exist on Google!\n\n"
            f"Please Provide Valid Gmail Username, then try again.",
            parse_mode=ParseMode.HTML,
            reply_markup=get_back_inline_keyboard()
        )
        await state.update_data(last_menu_msg_id=retry_msg.message_id)
        return

    # Only remove the previous prompt now that validation has actually succeeded
    await cleanup_last_menu(message, state)

    await state.update_data(sell_username=username)
    await state.set_state(UserState.selling_password)
    sent_msg = await message.answer(
        '<tg-emoji emoji-id=\"6005570495603282482\">🔑</tg-emoji> <b>Step 2/2:</b> Now send the <b>Password</b> for this Gmail account:',
        parse_mode=ParseMode.HTML,
        reply_markup=get_back_inline_keyboard()
    )
    await state.update_data(last_menu_msg_id=sent_msg.message_id)

@dp.message(UserState.selling_password, F.text, ~F.text.startswith("/"), ~F.text.in_(MENU_BUTTONS))
async def process_sell_password(message: Message, state: FSMContext):
    await cleanup_last_menu(message, state)
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
        InlineKeyboardButton(text="Approve", icon_custom_emoji_id="6217663806110175239", callback_data=f"sa:{sell_id}", style="success"),
        InlineKeyboardButton(text="Decline", icon_custom_emoji_id="5274099962655816924", callback_data=f"sd:{sell_id}", style="danger")
    ]])

    admin_message_text = (
        f'<tg-emoji emoji-id=\"5377548235709619284\">📨</tg-emoji> <b>New Gmail Sell Request #{sell_id}</b>\n\n'
        f'<tg-emoji emoji-id=\"5870458774455587120\">👤</tg-emoji> <b>Seller:</b> @{message.from_user.username} (<code>{user_id}</code>)\n'
        f'<tg-emoji emoji-id=\"5253742260054409879\">📧</tg-emoji> <b>Username:</b> <code>{username}</code>\n'
        f'<tg-emoji emoji-id=\"6005570495603282482\">🔑</tg-emoji> <b>Password:</b> <code>{password}</code>\n'
        f'<tg-emoji emoji-id=\"5417924076503062111\">💰</tg-emoji> <b>Payout Rate:</b> ₹{rate:.2f}'
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
                f'<tg-emoji emoji-id=\"5472027899789843495\">📦</tg-emoji> <b>New Gmail Sell Request Stock!</b>\n\n'
                f'<tg-emoji emoji-id=\"5244837092042750681\">📊</tg-emoji> <b>Available Stock:</b> <code>{current_stock}</code>\n\n'
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
        f'<tg-emoji emoji-id=\"6217663806110175239\">✅</tg-emoji> Your Gmail sell account details (Request #{sell_id}) have been sent for admin review.\n\n'
        f'<tg-emoji emoji-id=\"5420323339723881652\">⚠️</tg-emoji> <b>Important:</b> Please make sure to <b>logout</b> of this account from your device!', 
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
        except TelegramBadRequest as e:
            if "message is not modified" not in str(e):
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
        '<tg-emoji emoji-id=\"5471960722206366390\">🛠</tg-emoji> <b>Support Center</b>\n\n'
        'Please type and send your question, issue, or message below.\n\n'
        'An admin will be notified and respond to you as soon as possible.'
    )
    try:
        await call.message.edit_text(txt, parse_mode=ParseMode.HTML, reply_markup=get_support_cancel_keyboard())
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            await call.message.answer(txt, parse_mode=ParseMode.HTML, reply_markup=get_support_cancel_keyboard())
    await state.update_data(last_menu_msg_id=call.message.message_id)

@dp.message(UserState.waiting_for_support, ~F.text.startswith("/") if F.text else True, ~F.text.in_(MENU_BUTTONS) if F.text else True)
async def process_user_support_message(message: Message, state: FSMContext):
    await cleanup_last_menu(message, state)
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

    admin_init_text = "<tg-emoji emoji-id=\"5471960722206366390\">🛠</tg-emoji> <b>A new support request</b>"

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
            "<tg-emoji emoji-id=\"6217663806110175239\">✅</tg-emoji> <b>Your support message has been delivered to our team!</b>\n\nWe will get back to you shortly.",
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
        f"<tg-emoji emoji-id=\"5471960722206366390\">🛠</tg-emoji> <b>Support Request</b>\n\n"
        f"<tg-emoji emoji-id=\"5870458774455587120\">👤</tg-emoji> <b>Username:</b> {username}\n"
        f"🆔 <b>User ID:</b> <code>{target_user_id}</code>\n"
        f"💬 <b>Support Message:</b>\n{msg_text}"
    )

    action_kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="💬 Reply User",
                callback_data=f"sr:{target_user_id}",
                style="primary"
            )
        ],
        [
            InlineKeyboardButton(
                text="Ban User", icon_custom_emoji_id="5240241223632954241",
                callback_data=f"ban_supp:{target_user_id}",
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
                style="primary"
            )
        ]
    ])

    try:
        if call.message.photo:
            current_caption = call.message.caption or ""
            await call.message.edit_caption(
                caption=current_caption + "\n\n<tg-emoji emoji-id=\"5240241223632954241\">🚫</tg-emoji> <b>User Banned</b>",
                reply_markup=action_kb,
                parse_mode=ParseMode.HTML
            )
        else:
            current_text = call.message.text or ""
            await call.message.edit_text(
                text=current_text + "\n\n<tg-emoji emoji-id=\"5240241223632954241\">🚫</tg-emoji> <b>User Banned</b>",
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
    await cleanup_last_menu(message, state)
    data = await state.get_data()
    target_user_id = data.get('reply_target_user_id')

    if not target_user_id:
        await message.answer("❌ Error: Reply target lost.", reply_markup=get_admin_menu_keyboard())
        await state.clear()
        return

    reply_header = "<tg-emoji emoji-id=\"5471960722206366390\">🛠</tg-emoji> <b>Support Reply from Admin:</b>\n\n"

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
            f"<tg-emoji emoji-id=\"6217663806110175239\">✅</tg-emoji> <b>Reply successfully sent to User <code>{target_user_id}</code>!</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=get_admin_menu_keyboard()
        )
    except Exception as e:
        await message.answer(
            f"<tg-emoji emoji-id=\"5274099962655816924\">❌</tg-emoji> Failed to send reply to User <code>{target_user_id}</code>.\n\nError: <code>{e}</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=get_admin_menu_keyboard()
        )

    await state.clear()

# ============================================
# USER PAYMENT ADDRESS SETTERS
# ============================================

@dp.message(UserState.setting_upi, ~F.text.startswith("/"), ~F.text.in_(MENU_BUTTONS))
async def process_setting_upi(message: Message, state: FSMContext):
    await cleanup_last_menu(message, state)
    upi_input = message.text.strip()
    user_id = message.from_user.id

    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE users SET upi=$1 WHERE user_id=$2", upi_input, user_id)

    invalidate_user_cache(user_id)
    await message.answer(
        f"<tg-emoji emoji-id=\"6217663806110175239\">✅</tg-emoji> <b>UPI ID Updated Successfully!</b>\n\n<code>{upi_input}</code>",
        parse_mode=ParseMode.HTML,
        reply_markup=get_main_menu_keyboard()
    )
    await state.clear()

@dp.message(UserState.setting_usdt, ~F.text.startswith("/"), ~F.text.in_(MENU_BUTTONS))
async def process_setting_usdt(message: Message, state: FSMContext):
    await cleanup_last_menu(message, state)
    usdt_input = message.text.strip()
    user_id = message.from_user.id

    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE users SET usdt_address=$1 WHERE user_id=$2", usdt_input, user_id)

    invalidate_user_cache(user_id)
    await message.answer(
        f"<tg-emoji emoji-id=\"6217663806110175239\">✅</tg-emoji> <b>USDT BEP-20 Address Updated Successfully!</b>\n\n<code>{usdt_input}</code>",
        parse_mode=ParseMode.HTML,
        reply_markup=get_main_menu_keyboard()
    )
    await state.clear()

@dp.message(UserState.setting_ultra, ~F.text.startswith("/"), ~F.text.in_(MENU_BUTTONS))
async def process_setting_ultra(message: Message, state: FSMContext):
    await cleanup_last_menu(message, state)
    ultra_input = message.text.strip()
    user_id = message.from_user.id

    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE users SET ultra_number=$1 WHERE user_id=$2", ultra_input, user_id)

    invalidate_user_cache(user_id)
    await message.answer(
        f"<tg-emoji emoji-id=\"6217663806110175239\">✅</tg-emoji> <b>Ultra Gateway Number Updated Successfully!</b>\n\n<code>{ultra_input}</code>\n"
        f"<tg-emoji emoji-id=\"5447410659077661506\">🌐</tg-emoji> <b>Ultra Gateway:</b> https://ultra-pay.store",
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
        text = "<tg-emoji emoji-id=\"5264713049637409446\">👷</tg-emoji> <b>Manage Workers</b>\n\n<tg-emoji emoji-id=\"5262831879731555779\">📭</tg-emoji> No active workers registered in database yet."
        kb = InlineKeyboardBuilder()
        return text, kb.as_markup()

    text = f"<tg-emoji emoji-id=\"5264713049637409446\">👷</tg-emoji> <b>Manage Workers ({len(workers)} Registered)</b>\n\nSelect a worker below to manage full permissions:\n"
    kb = InlineKeyboardBuilder()

    for idx, w in enumerate(workers, start=1):
        status_icon = "<tg-emoji emoji-id=\"5416081784641168838\">🟢</tg-emoji>" if w['is_active'] else "<tg-emoji emoji-id=\"5411225014148014586\">🔴</tg-emoji>"
        sell_icon = "<tg-emoji emoji-id=\"5377548235709619284\">📨</tg-emoji>" if w['can_sell_gmail'] else "<tg-emoji emoji-id=\"5240241223632954241\">🚫</tg-emoji>"
        worker_display_name = w['name'] if w['name'] else f"Worker ({w['worker_id']})"
        btn_label = f"#{idx} {worker_display_name} {status_icon}{sell_icon}"
        kb.button(text=btn_label, callback_data=f"adm_work_view:{w['worker_id']}")

    kb.adjust(1)
    return text, kb.as_markup()

@dp.message(F.text == "Manage Workers", StateFilter("*"))
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

    status_str = "<tg-emoji emoji-id=\"5416081784641168838\">🟢</tg-emoji> ACTIVE (ON)" if w['is_active'] else "<tg-emoji emoji-id=\"5411225014148014586\">🔴</tg-emoji> DISABLED (OFF)"
    sell_str = "<tg-emoji emoji-id=\"5416081784641168838\">🟢</tg-emoji> ENABLED (Can review sell & get alerts)" if w['can_sell_gmail'] else "<tg-emoji emoji-id=\"5411225014148014586\">🔴</tg-emoji> DISABLED (Sell hidden)"

    text = (
        f"<tg-emoji emoji-id=\"5264713049637409446\">👷</tg-emoji> <b>Worker Control Panel</b>\n\n"
        f"🆔 <b>Worker ID:</b> <code>{w['worker_id']}</code>\n"
        f"<tg-emoji emoji-id=\"5350387571199319521\">🏷</tg-emoji> <b>Name:</b> {w['name']}\n"
        f"<tg-emoji emoji-id=\"5195033767969839232\">⚡️</tg-emoji> <b>Bot Access:</b> {status_str}\n"
        f"<tg-emoji emoji-id=\"5377548235709619284\">📨</tg-emoji> <b>Sell Gmail Feature:</b> {sell_str}\n\n"
        f"Use the buttons below to toggle permissions:"
    )

    kb = InlineKeyboardBuilder()
    toggle_access_label = "🔴 Turn Worker OFF" if w['is_active'] else "🟢 Turn Worker ON"
    toggle_sell_label = "🚫 Disable Sell Gmail" if w['can_sell_gmail'] else "📨 Enable Sell Gmail"

    kb.button(text=toggle_access_label, callback_data=f"adm_work_tog_access:{worker_id}")
    kb.button(text=toggle_sell_label, callback_data=f"adm_work_tog_sell:{worker_id}")
    kb.button(text="Back to Workers", icon_custom_emoji_id="5875082500023258804", callback_data="adm_work_back")
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
        await message.answer("<tg-emoji emoji-id=\"5420323339723881652\">⚠️</tg-emoji> <b>Usage:</b> <code>/name #1 Karim</code>", parse_mode=ParseMode.HTML)
        return

    parts = args.split(maxsplit=1)
    target_tag = parts[0]
    new_name = parts[1] if len(parts) > 1 else ""

    if not new_name:
        await message.answer("<tg-emoji emoji-id=\"5420323339723881652\">⚠️</tg-emoji> Please provide a name. <i>Example:</i> <code>/name #1 Karim</code>", parse_mode=ParseMode.HTML)
        return

    index_match = re.search(r'\d+', target_tag)
    if not index_match:
        await message.answer("<tg-emoji emoji-id=\"5274099962655816924\">❌</tg-emoji> Invalid format. Use <code>/name #1 NewName</code>", parse_mode=ParseMode.HTML)
        return

    target_index = int(index_match.group(0))

    async with db_pool.acquire() as conn:
        active_workers = await conn.fetch("SELECT worker_id, name FROM worker_permissions WHERE is_deleted = FALSE ORDER BY created_at ASC, worker_id ASC")
        
        if target_index < 1 or target_index > len(active_workers):
            await message.answer(f"<tg-emoji emoji-id=\"5274099962655816924\">❌</tg-emoji> Worker <b>#{target_index}</b> not found. Active count: {len(active_workers)}", parse_mode=ParseMode.HTML)
            return

        target_worker = active_workers[target_index - 1]
        target_worker_id = target_worker['worker_id']

        await conn.execute("UPDATE worker_permissions SET name=$1 WHERE worker_id=$2", new_name, target_worker_id)

    await message.answer(f"<tg-emoji emoji-id=\"6217663806110175239\">✅</tg-emoji> <b>Worker #{target_index} (ID: <code>{target_worker_id}</code>) name updated to:</b> <code>{new_name}</code>", parse_mode=ParseMode.HTML)

@dp.message(Command("delete"))
async def admin_cmd_delete_worker(message: Message, command: CommandObject):
    if message.from_user.id != ADMIN_ID:
        return

    args = (command.args or "").strip()
    if not args:
        await message.answer("<tg-emoji emoji-id=\"5420323339723881652\">⚠️</tg-emoji> <b>Usage:</b> <code>/delete #1</code>", parse_mode=ParseMode.HTML)
        return

    index_match = re.search(r'\d+', args)
    if not index_match:
        await message.answer("<tg-emoji emoji-id=\"5274099962655816924\">❌</tg-emoji> Invalid format. Use <code>/delete #1</code>", parse_mode=ParseMode.HTML)
        return

    target_index = int(index_match.group(0))

    async with db_pool.acquire() as conn:
        active_workers = await conn.fetch("SELECT worker_id, name FROM worker_permissions WHERE is_deleted = FALSE ORDER BY created_at ASC, worker_id ASC")

        if target_index < 1 or target_index > len(active_workers):
            await message.answer(f"<tg-emoji emoji-id=\"5274099962655816924\">❌</tg-emoji> Worker <b>#{target_index}</b> not found. Active count: {len(active_workers)}", parse_mode=ParseMode.HTML)
            return

        target_worker = active_workers[target_index - 1]
        target_worker_id = target_worker['worker_id']

        # Complete off (is_active=FALSE) and mark as deleted
        await conn.execute("UPDATE worker_permissions SET is_active = FALSE, is_deleted = TRUE WHERE worker_id = $1", target_worker_id)

    await message.answer(
        f"<tg-emoji emoji-id=\"5262529363710060188\">🗑</tg-emoji> <b>Worker #{target_index} (ID: <code>{target_worker_id}</code>) is now OFF and deleted from Manage Workers!</b>\n"
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
        await message.answer("<tg-emoji emoji-id=\"5420323339723881652\">⚠️</tg-emoji> <b>Usage:</b> <code>/recover #-1</code>", parse_mode=ParseMode.HTML)
        return

    index_match = re.search(r'\d+', args)
    if not index_match:
        await message.answer("<tg-emoji emoji-id=\"5274099962655816924\">❌</tg-emoji> Invalid format. Use <code>/recover #-1</code>", parse_mode=ParseMode.HTML)
        return

    target_index = int(index_match.group(0))

    async with db_pool.acquire() as conn:
        deleted_workers = await conn.fetch("SELECT worker_id, name FROM worker_permissions WHERE is_deleted = TRUE ORDER BY created_at ASC, worker_id ASC")

        if not deleted_workers:
            await message.answer("<tg-emoji emoji-id=\"5262831879731555779\">📭</tg-emoji> No deleted workers available to recover.", parse_mode=ParseMode.HTML)
            return

        if target_index < 1 or target_index > len(deleted_workers):
            await message.answer(f"<tg-emoji emoji-id=\"5274099962655816924\">❌</tg-emoji> Deleted worker record with index #{target_index} not found. (Total deleted: {len(deleted_workers)})", parse_mode=ParseMode.HTML)
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
        f"<tg-emoji emoji-id=\"6217663806110175239\">✅</tg-emoji> <b>Worker recovered successfully!</b>\n\n"
        f"🆔 <b>Worker ID:</b> <code>{target_worker_id}</code>\n"
        f"<tg-emoji emoji-id=\"5350387571199319521\">🏷</tg-emoji> <b>Assigned Index:</b> <b>#{active_count}</b> (Latest Free ID)\n"
        f"<tg-emoji emoji-id=\"5195033767969839232\">⚡️</tg-emoji> <b>Status:</b> <tg-emoji emoji-id=\"5416081784641168838\">🟢</tg-emoji> Active & Restored to Manage Workers",
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
        "<tg-emoji emoji-id=\"5471960722206366390\">🛠</tg-emoji> <b>Admin Control Panel</b>\n\nChoose an action from the admin menu below:",
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

    status_str = "<tg-emoji emoji-id=\"5416081784641168838\">🟢</tg-emoji> <b>Referral rewards are now ENABLED!</b>" if REF_STATUS else "<tg-emoji emoji-id=\"5411225014148014586\">🔴</tg-emoji> <b>Referral rewards are now SILENTLY DISABLED!</b> (Users won't receive bonuses upon approvals)"
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

    status_str = "<tg-emoji emoji-id=\"5416081784641168838\">🟢</tg-emoji> <b>Ultra Gateway is now ON and available in withdrawal options!</b>" if ULTRA_STATUS else "<tg-emoji emoji-id=\"5411225014148014586\">🔴</tg-emoji> <b>Ultra Gateway is now OFF and hidden from withdrawal options!</b>"
    await message.answer(status_str, parse_mode=ParseMode.HTML, reply_markup=get_admin_menu_keyboard())

@dp.message(F.text.in_({"🔴 Bot Status: OFF", "🟢 Bot Status: ON"}), StateFilter("*"))
async def admin_btn_toggle_bot_status(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return

    global BOT_STATUS

    if BOT_STATUS:
        # Bot is currently ON -> admin wants to turn it OFF. Ask for the message to show users first.
        await state.clear()
        await state.set_state(AdminState.waiting_for_bot_off_message)
        await message.answer(
            "<tg-emoji emoji-id=\"5411225014148014586\">🔴</tg-emoji> <b>Turning the Bot OFF</b>\n\n"
            "Send the message you want shown to all users while the bot is off:",
            parse_mode=ParseMode.HTML
        )
        return

    # Bot is currently OFF -> admin wants to turn it back ON.
    BOT_STATUS = True
    async with db_pool.acquire() as conn:
        await conn.execute("INSERT INTO bot_settings (key, value) VALUES ('bot_status', 'on') ON CONFLICT (key) DO UPDATE SET value = 'on'")

    await message.answer("<tg-emoji emoji-id=\"5416081784641168838\">🟢</tg-emoji> <b>Bot is now ON and accessible to all users!</b>", parse_mode=ParseMode.HTML, reply_markup=get_admin_menu_keyboard())

@dp.message(AdminState.waiting_for_bot_off_message, ~F.text.startswith("/"), ~F.text.in_(MENU_BUTTONS))
async def process_bot_off_message(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return

    global BOT_STATUS, BOT_OFF_MESSAGE
    BOT_OFF_MESSAGE = message.text.strip()
    BOT_STATUS = False

    async with db_pool.acquire() as conn:
        await conn.execute("INSERT INTO bot_settings (key, value) VALUES ('bot_status', 'off') ON CONFLICT (key) DO UPDATE SET value = 'off'")
        await conn.execute("INSERT INTO bot_settings (key, value) VALUES ('bot_off_message', $1) ON CONFLICT (key) DO UPDATE SET value = $1", BOT_OFF_MESSAGE)

    await state.clear()
    await message.answer(
        f"<tg-emoji emoji-id=\"5411225014148014586\">🔴</tg-emoji> <b>Bot is now OFF.</b>\n\nUsers will see:\n\n{BOT_OFF_MESSAGE}",
        parse_mode=ParseMode.HTML,
        reply_markup=get_admin_menu_keyboard()
    )

@dp.message(F.text == "Tasks", StateFilter("*"))
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

@dp.message(F.text == "Available Tasks", StateFilter("*"))
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

@dp.message(F.text == "Validator", StateFilter("*"))
async def admin_btn_validator_menu(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.clear()
    
    val_status_str = "<tg-emoji emoji-id=\"5416081784641168838\">🟢</tg-emoji> <b>Active</b>" if VALIDATOR_ENABLED else "<tg-emoji emoji-id=\"5411225014148014586\">🔴</tg-emoji> <b>Deactivated</b>"
    if VALIDATOR_PROVIDER == "netnit":
        provider_name = "NetNit (FastCheck)"
    elif VALIDATOR_PROVIDER == "emailable":
        provider_name = "Emailable"
    else:
        provider_name = "MyEmailVerifier"
    provider_url = get_provider_url()

    text = (
        f"<tg-emoji emoji-id=\"5893161718179173515\">⚙️</tg-emoji> <b>Gmail Validator Management</b>\n\n"
        f"<tg-emoji emoji-id=\"5447410659077661506\">🌐</tg-emoji> <b>Current Provider:</b> <code>{provider_name}</code>\n"
        f"<tg-emoji emoji-id=\"5271604874419647061\">🔗</tg-emoji> <b>Provider Endpoint:</b> <code>{provider_url}</code>\n"
        f"<tg-emoji emoji-id=\"6005570495603282482\">🔑</tg-emoji> <b>Current API Key:</b> <code>{EMAILABLE_API_KEY}</code>\n"
        f"<tg-emoji emoji-id=\"5237699328843200968\">📌</tg-emoji> <b>Validator Status:</b> {val_status_str}\n\n"
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

    status_text = "<tg-emoji emoji-id=\"5416081784641168838\">🟢</tg-emoji> <b>Active</b>" if VALIDATOR_ENABLED else "<tg-emoji emoji-id=\"5411225014148014586\">🔴</tg-emoji> <b>Deactivated</b>"
    if VALIDATOR_PROVIDER == "netnit":
        provider_name = "NetNit (FastCheck)"
    elif VALIDATOR_PROVIDER == "emailable":
        provider_name = "Emailable"
    else:
        provider_name = "MyEmailVerifier"
    provider_url = get_provider_url()

    text = (
        f"<tg-emoji emoji-id=\"5893161718179173515\">⚙️</tg-emoji> <b>Gmail Validator Management</b>\n\n"
        f"<tg-emoji emoji-id=\"5447410659077661506\">🌐</tg-emoji> <b>Current Provider:</b> <code>{provider_name}</code>\n"
        f"<tg-emoji emoji-id=\"5271604874419647061\">🔗</tg-emoji> <b>Provider Endpoint:</b> <code>{provider_url}</code>\n"
        f"<tg-emoji emoji-id=\"6005570495603282482\">🔑</tg-emoji> <b>Current API Key:</b> <code>{EMAILABLE_API_KEY}</code>\n"
        f"<tg-emoji emoji-id=\"5237699328843200968\">📌</tg-emoji> <b>Validator Status:</b> {status_text}\n\n"
        f"Use the buttons below to configure the email validator:"
    )

    try:
        await call.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=get_validator_admin_inline_keyboard())
    except Exception:
        pass

@dp.callback_query(F.data == "admin_validator_change_provider")
async def cb_admin_validator_change_provider(call: CallbackQuery):
    global VALIDATOR_PROVIDER
    if VALIDATOR_PROVIDER == "netnit":
        VALIDATOR_PROVIDER = "myemailverifier"
    elif VALIDATOR_PROVIDER == "myemailverifier":
        VALIDATOR_PROVIDER = "emailable"
    else:
        VALIDATOR_PROVIDER = "netnit"

    async with db_pool.acquire() as conn:
        await conn.execute("INSERT INTO bot_settings (key, value) VALUES ('validator_provider', $1) ON CONFLICT (key) DO UPDATE SET value = $1", VALIDATOR_PROVIDER)

    status_text = "<tg-emoji emoji-id=\"5416081784641168838\">🟢</tg-emoji> <b>Active</b>" if VALIDATOR_ENABLED else "<tg-emoji emoji-id=\"5411225014148014586\">🔴</tg-emoji> <b>Deactivated</b>"
    if VALIDATOR_PROVIDER == "netnit":
        provider_name = "NetNit (FastCheck)"
    elif VALIDATOR_PROVIDER == "emailable":
        provider_name = "Emailable"
    else:
        provider_name = "MyEmailVerifier"
    provider_url = get_provider_url()

    await call.answer(f"Switched provider to {provider_name}!", show_alert=True)

    text = (
        f"<tg-emoji emoji-id=\"5893161718179173515\">⚙️</tg-emoji> <b>Gmail Validator Management</b>\n\n"
        f"<tg-emoji emoji-id=\"5447410659077661506\">🌐</tg-emoji> <b>Current Provider:</b> <code>{provider_name}</code>\n"
        f"<tg-emoji emoji-id=\"5271604874419647061\">🔗</tg-emoji> <b>Provider Endpoint:</b> <code>{provider_url}</code>\n"
        f"<tg-emoji emoji-id=\"6005570495603282482\">🔑</tg-emoji> <b>Current API Key:</b> <code>{EMAILABLE_API_KEY}</code>\n"
        f"<tg-emoji emoji-id=\"5237699328843200968\">📌</tg-emoji> <b>Validator Status:</b> {status_text}\n\n"
        f"Use the buttons below to configure the email validator:"
    )

    try:
        await call.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=get_validator_admin_inline_keyboard())
    except Exception:
        pass

@dp.callback_query(F.data == "admin_validator_change_key")
async def cb_admin_validator_change_key(call: CallbackQuery, state: FSMContext):
    await call.answer()
    if VALIDATOR_PROVIDER == "netnit":
        provider_name = "NetNit (FastCheck)"
    elif VALIDATOR_PROVIDER == "emailable":
        provider_name = "Emailable"
    else:
        provider_name = "MyEmailVerifier"

    await state.set_state(AdminState.waiting_for_validator_key)
    await call.message.answer(
        f"<tg-emoji emoji-id=\"6005570495603282482\">🔑</tg-emoji> <b>Send the new API key for {provider_name}:</b>",
        parse_mode=ParseMode.HTML
    )

@dp.message(AdminState.waiting_for_validator_key, ~F.text.startswith("/"), ~F.text.in_(MENU_BUTTONS))
async def process_change_validator_key(message: Message, state: FSMContext):
    await cleanup_last_menu(message, state)
    global EMAILABLE_API_KEY
    new_key = message.text.strip()

    EMAILABLE_API_KEY = new_key

    async with db_pool.acquire() as conn:
        await conn.execute("INSERT INTO bot_settings (key, value) VALUES ('emailable_api_key', $1) ON CONFLICT (key) DO UPDATE SET value = $1", new_key)

    if VALIDATOR_PROVIDER == "netnit":
        provider_name = "NetNit (FastCheck)"
    elif VALIDATOR_PROVIDER == "emailable":
        provider_name = "Emailable"
    else:
        provider_name = "MyEmailVerifier"

    await message.answer(
        f"<tg-emoji emoji-id=\"6217663806110175239\">✅</tg-emoji> <b>{provider_name} API Key Updated Successfully!</b>\n\n"
        f"<tg-emoji emoji-id=\"6005570495603282482\">🔑</tg-emoji> <b>New Key:</b> <code>{new_key}</code>",
        parse_mode=ParseMode.HTML,
        reply_markup=get_admin_menu_keyboard()
    )
    await state.clear()

@dp.message(F.text == "Transfer Admin", StateFilter("*"))
async def admin_btn_transfer_admin(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.set_state(AdminState.waiting_for_transfer_admin_id)
    await message.answer(
        "👑 <b>Transfer Admin Privileges</b>\n\n"
        "Send the numeric <b>User ID</b> of the user you want to transfer full adminship to:\n\n"
        "<i><tg-emoji emoji-id=\"5420323339723881652\">⚠️</tg-emoji> Warning: Once transferred, your current user ID will no longer have access to the admin panel!</i>",
        parse_mode=ParseMode.HTML
    )

@dp.message(AdminState.waiting_for_transfer_admin_id, ~F.text.startswith("/"), ~F.text.in_(MENU_BUTTONS))
async def process_transfer_admin_id_step(message: Message, state: FSMContext):
    await cleanup_last_menu(message, state)
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

@dp.message(F.text == "Add Task", StateFilter("*"))
async def admin_btn_add_task(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.clear()
    await message.answer(
        "<tg-emoji emoji-id=\"5397916757333654639\">➕</tg-emoji> <b>Add Tasks Options</b>\n\n"
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
    await call.message.answer("<tg-emoji emoji-id=\"5253742260054409879\">📧</tg-emoji> Send the email/username to add as a task (e.g. <code>example@gmail.com</code>):", parse_mode=ParseMode.HTML)

@dp.callback_query(F.data == "admin_add_task_bulk")
async def cb_admin_add_task_bulk(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.set_state(AdminState.waiting_for_bulk_add_task)
    await call.message.answer(
        "<tg-emoji emoji-id=\"5472027899789843495\">📦</tg-emoji> <b>Bulk Task Addition</b>\n\n"
        "Send the list of email usernames separated by line breaks, spaces, or commas:\n\n"
        "<i>Example:</i>\n"
        "<code>john</code>\n"
        "<code>adarsh</code>\n"
        "<code>mayank</code>",
        parse_mode=ParseMode.HTML
    )

@dp.message(AdminState.waiting_for_bulk_add_task, ~F.text.startswith("/"), ~F.text.in_(MENU_BUTTONS))
async def process_bulk_add_task_step(message: Message, state: FSMContext):
    await cleanup_last_menu(message, state)
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

    status_msg = await message.answer(f"<tg-emoji emoji-id=\"5382194935057372936\">⏳</tg-emoji> <b>Adding tasks in bulk... 0/{total_items}</b>", parse_mode=ParseMode.HTML)

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
                    await status_msg.edit_text(f"<tg-emoji emoji-id=\"5382194935057372936\">⏳</tg-emoji> <b>Bulk Adding Progress: {idx}/{total_items}</b>", parse_mode=ParseMode.HTML)
                except Exception:
                    pass

    await status_msg.edit_text(
        f"<tg-emoji emoji-id=\"6217663806110175239\">✅</tg-emoji> <b>Bulk Task Addition Completed!</b>\n\n"
        f"<tg-emoji emoji-id=\"5244837092042750681\">📊</tg-emoji> <b>Total Processed:</b> {total_items}\n"
        f"<tg-emoji emoji-id=\"5416081784641168838\">🟢</tg-emoji> <b>Successfully Added:</b> {added_count}\n"
        f"<tg-emoji emoji-id=\"5420323339723881652\">⚠️</tg-emoji> <b>Skipped (Duplicates):</b> {skipped_count}",
        parse_mode=ParseMode.HTML,
        reply_markup=get_admin_menu_keyboard()
    )
    await state.clear()

@dp.message(AdminState.waiting_for_add_task, ~F.text.startswith("/"), ~F.text.in_(MENU_BUTTONS))
async def process_add_task_step(message: Message, state: FSMContext):
    await cleanup_last_menu(message, state)
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
            text="Confirm", icon_custom_emoji_id="6217663806110175239", 
            callback_data="confirm_add_duplicate_task", 
            style="success"
        )
        kb.button(
            text="Back", icon_custom_emoji_id="5875082500023258804",
            callback_data="cancel_add_duplicate_task", 
            style="danger"
        )
        kb.adjust(2)

        await message.answer(
            f'<tg-emoji emoji-id=\"5420323339723881652\">⚠️</tg-emoji> <b>This task (<code>{username}</code>) already exists in database!</b>\n\n'
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
        f'<tg-emoji emoji-id=\"6217663806110175239\">✅</tg-emoji> <b>Task Added Successfully!</b>\n\n'
        f'🆔 <b>Task ID:</b> <code>#{task_id}</code>\n'
        f'<tg-emoji emoji-id=\"5253742260054409879\">📧</tg-emoji> <b>Email:</b> <code>{username}</code>\n'
        f'<tg-emoji emoji-id=\"6005570495603282482\">🔑</tg-emoji> <b>Password:</b> <code>{password}</code>\n'
        f'<tg-emoji emoji-id=\"5417924076503062111\">💰</tg-emoji> <b>Reward:</b> ₹{default_reward}', 
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

@dp.message(F.text == "Pending Reviews", StateFilter("*"))
async def admin_btn_pending_reviews(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.clear()
        
    async with db_pool.acquire() as conn:
        task_count = await conn.fetchval("SELECT COUNT(*) FROM tasks WHERE status = 'pending_review'") or 0
        sell_count = await conn.fetchval("SELECT COUNT(*) FROM pending_sells WHERE status = 'pending_review'") or 0

    total_pending = task_count + sell_count
        
    if total_pending == 0:
        await message.answer("<tg-emoji emoji-id=\"5262831879731555779\">📭</tg-emoji> <b>No pending reviews (tasks or sell requests) found!</b>", parse_mode=ParseMode.HTML, reply_markup=get_admin_menu_keyboard())
        return

    text = (
        f"📥 <b>Pending Reviews Dashboard</b>\n\n"
        f"<tg-emoji emoji-id=\"5377548235709619284\">📨</tg-emoji> <b>Pending Sell Gmail:</b> <code>{sell_count}</code>\n"
        f"<tg-emoji emoji-id=\"5197269100878907942\">✍️</tg-emoji> <b>Pending Task Gmail:</b> <code>{task_count}</code>\n\n"
        f"Click an option below to view requests:"
    )

    await message.answer(
        text, 
        parse_mode=ParseMode.HTML, 
        reply_markup=get_pending_reviews_inline_keyboard()
    )

@dp.callback_query(F.data == "back_pending_reviews")
async def cb_back_pending_reviews(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    await call.answer()
    await state.clear()

    async with db_pool.acquire() as conn:
        task_count = await conn.fetchval("SELECT COUNT(*) FROM tasks WHERE status = 'pending_review'") or 0
        sell_count = await conn.fetchval("SELECT COUNT(*) FROM pending_sells WHERE status = 'pending_review'") or 0

    total_pending = task_count + sell_count

    if total_pending == 0:
        text = "<tg-emoji emoji-id=\"5262831879731555779\">📭</tg-emoji> <b>No pending reviews (tasks or sell requests) found!</b>"
        kb = get_back_inline_keyboard("back_admin_menu")
    else:
        text = (
            f"📥 <b>Pending Reviews Dashboard</b>\n\n"
            f"<tg-emoji emoji-id=\"5377548235709619284\">📨</tg-emoji> <b>Pending Sell Gmail:</b> <code>{sell_count}</code>\n"
            f"<tg-emoji emoji-id=\"5197269100878907942\">✍️</tg-emoji> <b>Pending Task Gmail:</b> <code>{task_count}</code>\n\n"
            f"Click an option below to view requests:"
        )
        kb = get_pending_reviews_inline_keyboard()

    try:
        await call.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            await call.message.answer(text, parse_mode=ParseMode.HTML, reply_markup=kb)

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
            await call.message.edit_text("<tg-emoji emoji-id=\"5262831879731555779\">📭</tg-emoji> <b>No pending sell Gmail requests found!</b>", parse_mode=ParseMode.HTML, reply_markup=get_back_inline_keyboard("back_pending_reviews"))
        except TelegramBadRequest as e:
            if "message is not modified" not in str(e):
                await call.message.answer("<tg-emoji emoji-id=\"5262831879731555779\">📭</tg-emoji> <b>No pending sell Gmail requests found!</b>", parse_mode=ParseMode.HTML, reply_markup=get_back_inline_keyboard("back_pending_reviews"))
        return

    await call.message.answer(f"<tg-emoji emoji-id=\"5377548235709619284\">📨</tg-emoji> <b>Displaying {len(sell_rows)} pending Gmail sell request(s):</b>", parse_mode=ParseMode.HTML)

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
                
            formatted_details = f"<tg-emoji emoji-id=\"5253742260054409879\">📧</tg-emoji> <b>Username:</b> <code>{username}</code>\n<tg-emoji emoji-id=\"6005570495603282482\">🔑</tg-emoji> <b>Password:</b> <code>{password}</code>"
        except Exception:
            formatted_details = f"<code>{details}</code>"

        claimed_str = f"\n<tg-emoji emoji-id=\"5264713049637409446\">👷</tg-emoji> <b>Claimed By Worker:</b> <code>{claimed_by}</code>" if claimed_by else "\n<tg-emoji emoji-id=\"5472027899789843495\">📦</tg-emoji> <b>Status:</b> <tg-emoji emoji-id=\"5416081784641168838\">🟢</tg-emoji> Unclaimed Stock"

        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="Approve", icon_custom_emoji_id="6217663806110175239", callback_data=f"sa:{sell_id}", style="success"),
            InlineKeyboardButton(text="Decline", icon_custom_emoji_id="5274099962655816924", callback_data=f"sd:{sell_id}", style="danger")
        ]])

        await call.message.answer(
            f'<tg-emoji emoji-id=\"5472027899789843495\">📦</tg-emoji> <b>Pending Gmail Sell Request #{sell_id}</b>\n\n'
            f'<tg-emoji emoji-id=\"5870458774455587120\">👤</tg-emoji> <b>User ID:</b> <code>{user_id}</code>\n'
            f'<tg-emoji emoji-id=\"5417924076503062111\">💰</tg-emoji> <b>Rate:</b> ₹{amount:.2f}'
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
            await call.message.edit_text("<tg-emoji emoji-id=\"5262831879731555779\">📭</tg-emoji> <b>No pending task submissions found!</b>", parse_mode=ParseMode.HTML, reply_markup=get_back_inline_keyboard("back_pending_reviews"))
        except TelegramBadRequest as e:
            if "message is not modified" not in str(e):
                await call.message.answer("<tg-emoji emoji-id=\"5262831879731555779\">📭</tg-emoji> <b>No pending task submissions found!</b>", parse_mode=ParseMode.HTML, reply_markup=get_back_inline_keyboard("back_pending_reviews"))
        return

    await call.message.answer(f"<tg-emoji emoji-id=\"5197269100878907942\">✍️</tg-emoji> <b>Displaying {len(task_rows)} pending task submission(s):</b>", parse_mode=ParseMode.HTML)

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
            InlineKeyboardButton(text='Approve', icon_custom_emoji_id="6217663806110175239", callback_data=f'ta:{task_id}', style="success"),
            InlineKeyboardButton(text='Decline', icon_custom_emoji_id="5274099962655816924", callback_data=f'td:{task_id}', style="danger")
        ]])
        
        await call.message.answer(
            f'<tg-emoji emoji-id=\"5305265301917549162\">📤</tg-emoji> <b>Pending Task Submission</b>\n\n'
            f'<tg-emoji emoji-id=\"5870458774455587120\">👤</tg-emoji> <b>User ID:</b> <code>{user_id}</code>\n'
            f'🆔 <b>Task #{task_id}</b>\n'
            f'<tg-emoji emoji-id=\"5253742260054409879\">📧</tg-emoji> <b>Email:</b> <code>{email}</code>\n'
            f'<tg-emoji emoji-id=\"6005570495603282482\">🔑</tg-emoji> <b>Password:</b> <code>{password}</code>\n'
            f'<tg-emoji emoji-id=\"5417924076503062111\">💰</tg-emoji> <b>Reward:</b> ₹{reward}',
            reply_markup=kb,
            parse_mode=ParseMode.HTML
        )

@dp.message(F.text == "Pending Withdrawals", StateFilter("*"))
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
        await message.answer("<tg-emoji emoji-id=\"5262831879731555779\">📭</tg-emoji> <b>No pending withdrawal requests found!</b>", parse_mode=ParseMode.HTML, reply_markup=get_admin_menu_keyboard())
        return

    ultra_line = f"\n<tg-emoji emoji-id=\"5195033767969839232\">⚡️</tg-emoji> <b>Ultra Gateway Pending:</b> <code>{ultra_count}</code>" if ULTRA_STATUS else ""

    text = (
        f"<tg-emoji emoji-id=\"5444856076954520455\">💸</tg-emoji> <b>Pending Withdrawals Dashboard</b>\n\n"
        f"<tg-emoji emoji-id=\"6291696801636424911\">🏦</tg-emoji> <b>UPI Pending:</b> <code>{upi_count}</code>\n"
        f"<tg-emoji emoji-id=\"5197434882321567830\">🪙</tg-emoji> <b>USDT BEP-20 Pending:</b> <code>{usdt_count}</code>"
        f"{ultra_line}\n\n"
        f"Select a method below to review requests:"
    )

    await message.answer(
        text, 
        parse_mode=ParseMode.HTML, 
        reply_markup=get_pending_withdrawals_inline_keyboard()
    )

@dp.callback_query(F.data == "back_pending_withdrawals")
async def cb_back_pending_withdrawals(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    await call.answer()
    await state.clear()

    async with db_pool.acquire() as conn:
        upi_count = await conn.fetchval("SELECT COUNT(*) FROM withdrawals WHERE status = 'pending' AND method ILIKE '%UPI%'") or 0
        usdt_count = await conn.fetchval("SELECT COUNT(*) FROM withdrawals WHERE status = 'pending' AND method ILIKE '%USDT%'") or 0
        ultra_count = await conn.fetchval("SELECT COUNT(*) FROM withdrawals WHERE status = 'pending' AND method ILIKE '%Ultra%'") or 0

    total_pending = upi_count + usdt_count + ultra_count

    if total_pending == 0:
        text = "<tg-emoji emoji-id=\"5262831879731555779\">📭</tg-emoji> <b>No pending withdrawal requests found!</b>"
        kb = get_back_inline_keyboard("back_admin_menu")
    else:
        ultra_line = f"\n<tg-emoji emoji-id=\"5195033767969839232\">⚡️</tg-emoji> <b>Ultra Gateway Pending:</b> <code>{ultra_count}</code>" if ULTRA_STATUS else ""
        text = (
            f"<tg-emoji emoji-id=\"5444856076954520455\">💸</tg-emoji> <b>Pending Withdrawals Dashboard</b>\n\n"
            f"<tg-emoji emoji-id=\"6291696801636424911\">🏦</tg-emoji> <b>UPI Pending:</b> <code>{upi_count}</code>\n"
            f"<tg-emoji emoji-id=\"5197434882321567830\">🪙</tg-emoji> <b>USDT BEP-20 Pending:</b> <code>{usdt_count}</code>"
            f"{ultra_line}\n\n"
            f"Select a method below to review requests:"
        )
        kb = get_pending_withdrawals_inline_keyboard()

    try:
        await call.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            await call.message.answer(text, parse_mode=ParseMode.HTML, reply_markup=kb)

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
            await call.message.edit_text(f"<tg-emoji emoji-id=\"5262831879731555779\">📭</tg-emoji> <b>No pending withdrawal requests for {display_label}!</b>", parse_mode=ParseMode.HTML, reply_markup=get_back_inline_keyboard("back_pending_withdrawals"))
        except TelegramBadRequest as e:
            if "message is not modified" not in str(e):
                await call.message.answer(f"<tg-emoji emoji-id=\"5262831879731555779\">📭</tg-emoji> <b>No pending withdrawal requests for {display_label}!</b>", parse_mode=ParseMode.HTML, reply_markup=get_back_inline_keyboard("back_pending_withdrawals"))
        return

    await call.message.answer(f"<tg-emoji emoji-id=\"5444856076954520455\">💸</tg-emoji> <b>Displaying {len(withdraw_rows)} pending withdrawal request(s) for {display_label}:</b>", parse_mode=ParseMode.HTML)

    for r in withdraw_rows:
        withdraw_id = r['id']
        user_id = r['user_id']
        amount = r['amount']
        method = r['method'] or display_label
        payment_address = r['payment_address'] or 'None'
        
        extra_usdt_info = f" (~${(amount / USD_TO_INR):.2f} USDT)" if "usdt" in method.lower() else ""

        kb = InlineKeyboardBuilder()
        kb.button(
            text="Pay", icon_custom_emoji_id="5444856076954520455", 
            callback_data=f"wp:{withdraw_id}", 
            style="success"
        )
        kb.button(
            text="Reject", icon_custom_emoji_id="5274099962655816924", 
            callback_data=f"wr:{withdraw_id}", 
            style="danger"
        )
        kb.adjust(2)

        address_emoji = '<tg-emoji emoji-id=\"6291696801636424911\">🏦</tg-emoji>' if "upi" in method.lower() else ('<tg-emoji emoji-id=\"5197434882321567830\">🪙</tg-emoji>' if "usdt" in method.lower() else '<tg-emoji emoji-id=\"5195033767969839232\">⚡️</tg-emoji>')

        await call.message.answer(
            f'<tg-emoji emoji-id=\"5417924076503062111\">💰</tg-emoji> <b>WITHDRAWAL REQUEST #{withdraw_id}</b>\n\n'
            f'🆔 <b>User ID:</b> <code>{user_id}</code>\n'
            f'<tg-emoji emoji-id=\"5445353829304387411\">💳</tg-emoji> <b>Method:</b> <code>{method}</code>\n'
            f'<tg-emoji emoji-id=\"5417924076503062111\">💰</tg-emoji> <b>Amount:</b> ₹{amount:.2f}{extra_usdt_info}\n'
            f'{address_emoji} <b>Address:</b> <code>{payment_address}</code>\n'
            f'📅 <b>Date:</b> {r["created_at"].strftime("%Y-%m-%d %H:%M:%S")}',
            reply_markup=kb.as_markup(),
            parse_mode=ParseMode.HTML
        )

@dp.message(F.text == "Chat", StateFilter("*"))
async def admin_btn_chat(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.set_state(AdminState.waiting_for_chat_user_id)
    await message.answer("💬 Send the numeric **User ID** you want to message:", parse_mode=ParseMode.MARKDOWN)

@dp.message(AdminState.waiting_for_chat_user_id, ~F.text.startswith("/"), ~F.text.in_(MENU_BUTTONS))
async def process_chat_user_id_step(message: Message, state: FSMContext):
    await cleanup_last_menu(message, state)
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
    await cleanup_last_menu(message, state)
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
        await message.answer(f"<tg-emoji emoji-id=\"6217663806110175239\">✅</tg-emoji> **Message successfully sent to User `{target_user_id}`!**", parse_mode=ParseMode.MARKDOWN, reply_markup=get_admin_menu_keyboard())
    except Exception as e:
        await message.answer(f"<tg-emoji emoji-id=\"5274099962655816924\">❌</tg-emoji> Failed to send message to User `{target_user_id}`.\n\nError: `{e}`", parse_mode=ParseMode.MARKDOWN, reply_markup=get_admin_menu_keyboard())

    await state.clear()

@dp.message(F.text == "Unassign Tasks", StateFilter("*"))
async def admin_btn_unassign_tasks(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.clear()
    await message.answer(
        UNASSIGN_MENU_TEXT,
        parse_mode=ParseMode.HTML,
        reply_markup=get_unassign_inline_keyboard()
    )

@dp.callback_query(F.data == "back_unassign_menu")
async def cb_back_unassign_menu(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    await call.answer()
    await state.clear()
    try:
        await call.message.edit_text(UNASSIGN_MENU_TEXT, parse_mode=ParseMode.HTML, reply_markup=get_unassign_inline_keyboard())
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            await call.message.answer(UNASSIGN_MENU_TEXT, parse_mode=ParseMode.HTML, reply_markup=get_unassign_inline_keyboard())

@dp.callback_query(F.data == "unassign_by_user_id")
async def start_unassign_user_id(call: CallbackQuery, state: FSMContext):
    await call.answer()
    if call.from_user.id != ADMIN_ID:
        return
    await state.set_state(AdminState.waiting_for_unassign_user_id)
    await call.message.answer("<tg-emoji emoji-id=\"5870458774455587120\">👤</tg-emoji> Send the numeric **User ID** whose task you want to unassign:", parse_mode=ParseMode.MARKDOWN)

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
                await call.message.edit_text("<tg-emoji emoji-id=\"5262831879731555779\">📭</tg-emoji> <b>No active assigned tasks found to unassign.</b>", parse_mode=ParseMode.HTML, reply_markup=get_back_inline_keyboard("back_unassign_menu"))
            except TelegramBadRequest as e:
                if "message is not modified" not in str(e):
                    await call.message.answer("<tg-emoji emoji-id=\"5262831879731555779\">📭</tg-emoji> <b>No active assigned tasks found to unassign.</b>", parse_mode=ParseMode.HTML, reply_markup=get_back_inline_keyboard("back_unassign_menu"))
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
        await call.message.edit_text(f"<tg-emoji emoji-id=\"6217663806110175239\">✅</tg-emoji> <b>Successfully unassigned {count} active task(s) from all users, removed active task messages, and returned them to the pool.</b>", parse_mode=ParseMode.HTML, reply_markup=get_back_inline_keyboard("back_unassign_menu"))
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            await call.message.answer(f"<tg-emoji emoji-id=\"6217663806110175239\">✅</tg-emoji> <b>Successfully unassigned {count} active task(s) from all users, removed active task messages, and returned them to the pool.</b>", parse_mode=ParseMode.HTML, reply_markup=get_back_inline_keyboard("back_unassign_menu"))

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
            '<tg-emoji emoji-id=\"5420323339723881652\">⚠️</tg-emoji> <b>Your active task has been unassigned by the admin and returned to the pool.</b>\n\nChoose an option from the menu below:',
            reply_markup=get_main_menu_keyboard(),
            parse_mode=ParseMode.HTML
        ))

@dp.message(AdminState.waiting_for_unassign_user_id, ~F.text.startswith("/"), ~F.text.in_(MENU_BUTTONS))
async def process_unassign_user_id_step(message: Message, state: FSMContext):
    await cleanup_last_menu(message, state)
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
                await message.answer(f"<tg-emoji emoji-id=\"5262831879731555779\">📭</tg-emoji> User `{target_id}` does not have any active task assigned.", parse_mode=ParseMode.MARKDOWN, reply_markup=get_admin_menu_keyboard())
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

        await message.answer(f"<tg-emoji emoji-id=\"6217663806110175239\">✅</tg-emoji> **Successfully unassigned Task #{task_id} from User `{target_id}`, removed task message, and returned it to pool.**", parse_mode=ParseMode.MARKDOWN, reply_markup=get_admin_menu_keyboard())
        
        asyncio.create_task(send_user_notification(
            target_id,
            '<tg-emoji emoji-id=\"5420323339723881652\">⚠️</tg-emoji> <b>Your active task has been unassigned by the admin and returned to the pool.</b>\n\nChoose an option from the menu below:',
            reply_markup=get_main_menu_keyboard(),
            parse_mode=ParseMode.HTML
        ))
    except ValueError:
        await message.answer("❌ Invalid User ID.", reply_markup=get_admin_menu_keyboard())
    await state.clear()

# ============================================
# DUSTBIN SYSTEM
# ============================================

def get_dustbin_menu_keyboard():
    kb = InlineKeyboardBuilder()
    kb.button(text="Clear Dust", icon_custom_emoji_id="5231361378748472914", callback_data="dustbin_clear", style="danger")
    kb.button(text="See Dustbin", icon_custom_emoji_id="5262529363710060188", callback_data="dustbin_view:1", style="primary")
    kb.adjust(2)
    return kb.as_markup()

def get_dustbin_clear_confirm_keyboard():
    kb = InlineKeyboardBuilder()
    kb.button(text="Yes, Clear It", icon_custom_emoji_id="6217663806110175239", callback_data="dustbin_clear_confirm", style="danger")
    kb.button(text="Cancel", icon_custom_emoji_id="5274099962655816924", callback_data="dustbin_clear_cancel", style="primary")
    kb.adjust(2)
    return kb.as_markup()

DUSTBIN_MENU_TEXT = (
    "<tg-emoji emoji-id=\"5309832892262654231\">🤖</tg-emoji> <b>Dustbin</b>\n\n"
    "• <b>Clear Dust:</b> Moves every <b>Available</b> and <b>Assigned</b> task into the Dustbin "
    "(any user with an active assigned task is notified and returned to the main menu). "
    "<b>Pending Review</b> and <b>Completed</b> tasks are never touched.\n"
    "• <b>See Dustbin:</b> Browse dustbinned tasks and Restore, Delete, or Replace their Gmail username."
)

async def render_dustbin_menu(call: CallbackQuery):
    try:
        await call.message.edit_text(DUSTBIN_MENU_TEXT, parse_mode=ParseMode.HTML, reply_markup=get_dustbin_menu_keyboard())
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            await call.message.answer(DUSTBIN_MENU_TEXT, parse_mode=ParseMode.HTML, reply_markup=get_dustbin_menu_keyboard())

@dp.message(F.text == "Dustbin", StateFilter("*"))
async def admin_btn_dustbin(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.clear()
    await message.answer(
        DUSTBIN_MENU_TEXT,
        parse_mode=ParseMode.HTML,
        reply_markup=get_dustbin_menu_keyboard()
    )

@dp.callback_query(F.data == "back_dustbin_menu")
async def cb_back_dustbin_menu(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    await call.answer()
    await state.clear()
    await render_dustbin_menu(call)

@dp.callback_query(F.data == "dustbin_clear")
async def cb_dustbin_clear(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    await call.answer()
    await state.clear()

    async with db_pool.acquire() as conn:
        avail_count = await conn.fetchval("SELECT COUNT(*) FROM tasks WHERE status='available'") or 0
        assigned_count = await conn.fetchval("SELECT COUNT(*) FROM tasks WHERE status='assigned'") or 0

    total_count = avail_count + assigned_count

    if total_count == 0:
        try:
            await call.message.edit_text("<tg-emoji emoji-id=\"5262831879731555779\">📭</tg-emoji> <b>No Available or Assigned tasks found to move to the Dustbin.</b>", parse_mode=ParseMode.HTML, reply_markup=get_back_inline_keyboard("back_dustbin_menu"))
        except TelegramBadRequest as e:
            if "message is not modified" not in str(e):
                await call.message.answer("<tg-emoji emoji-id=\"5262831879731555779\">📭</tg-emoji> <b>No Available or Assigned tasks found to move to the Dustbin.</b>", parse_mode=ParseMode.HTML, reply_markup=get_back_inline_keyboard("back_dustbin_menu"))
        return

    text = (
        "🧹 <b>Confirm Clear Dust</b>\n\n"
        f"<tg-emoji emoji-id=\"5416081784641168838\">🟢</tg-emoji> <b>Available Tasks:</b> <code>{avail_count}</code>\n"
        f"🟡 <b>Assigned Tasks:</b> <code>{assigned_count}</code>\n"
        f"<tg-emoji emoji-id=\"5472027899789843495\">📦</tg-emoji> <b>Total to move to Dustbin:</b> <code>{total_count}</code>\n\n"
        "<tg-emoji emoji-id=\"5420323339723881652\">⚠️</tg-emoji> Users with an active assigned task will be notified and their task message removed.\n"
        "<tg-emoji emoji-id=\"6217663806110175239\">✅</tg-emoji> <b>Pending Review</b> and <b>Completed</b> tasks will <u>not</u> be touched.\n\n"
        "Proceed?"
    )
    try:
        await call.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=get_dustbin_clear_confirm_keyboard())
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            await call.message.answer(text, parse_mode=ParseMode.HTML, reply_markup=get_dustbin_clear_confirm_keyboard())

@dp.callback_query(F.data == "dustbin_clear_cancel")
async def cb_dustbin_clear_cancel(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    await call.answer("Cancelled.")
    await state.clear()
    await render_dustbin_menu(call)

@dp.callback_query(F.data == "dustbin_clear_confirm")
async def cb_dustbin_clear_confirm(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    await call.answer()
    await state.clear()

    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT id, title, details, reward, added_by, status FROM tasks WHERE status IN ('available', 'assigned')")

        if not rows:
            try:
                await call.message.edit_text("<tg-emoji emoji-id=\"5262831879731555779\">📭</tg-emoji> <b>No Available or Assigned tasks found to move to the Dustbin.</b>", parse_mode=ParseMode.HTML, reply_markup=get_back_inline_keyboard("back_dustbin_menu"))
            except TelegramBadRequest as e:
                if "message is not modified" not in str(e):
                    await call.message.answer("<tg-emoji emoji-id=\"5262831879731555779\">📭</tg-emoji> <b>No Available or Assigned tasks found to move to the Dustbin.</b>", parse_mode=ParseMode.HTML, reply_markup=get_back_inline_keyboard("back_dustbin_menu"))
            return

        task_ids = [r['id'] for r in rows]

        assigned_rows = await conn.fetch('''
            SELECT ta.task_id, ta.user_id, ta.message_id
            FROM task_assignments ta
            WHERE ta.task_id = ANY($1::int[])
        ''', task_ids)

        async with conn.transaction():
            for r in rows:
                await conn.execute(
                    "INSERT INTO dustbin_tasks (title, details, reward, added_by) VALUES ($1, $2, $3, $4)",
                    r['title'], r['details'], r['reward'], r['added_by']
                )
            await conn.execute("DELETE FROM task_assignments WHERE task_id = ANY($1::int[])", task_ids)
            await conn.execute("DELETE FROM tasks WHERE id = ANY($1::int[])", task_ids)

    total_count = len(task_ids)
    unassigned_count = len(assigned_rows)

    result_text = (
        f"<tg-emoji emoji-id=\"6217663806110175239\">✅</tg-emoji> <b>Dustbin Cleared!</b>\n\n"
        f"<tg-emoji emoji-id=\"5262529363710060188\">🗑</tg-emoji> <b>Total tasks moved to Dustbin:</b> <code>{total_count}</code>\n"
        f"<tg-emoji emoji-id=\"5870458774455587120\">👤</tg-emoji> <b>Active users unassigned & notified:</b> <code>{unassigned_count}</code>\n\n"
        f"ℹ️ <i>Pending Review and Completed tasks were left untouched.</i>"
    )

    try:
        await call.message.edit_text(
            result_text,
            parse_mode=ParseMode.HTML,
            reply_markup=get_back_inline_keyboard("back_dustbin_menu")
        )
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            await call.message.answer(
                result_text,
                parse_mode=ParseMode.HTML,
                reply_markup=get_back_inline_keyboard("back_dustbin_menu")
            )

    for r in assigned_rows:
        uid = r['user_id']
        mid = r['message_id']
        if mid:
            try:
                await bot.delete_message(chat_id=uid, message_id=mid)
            except Exception:
                pass
        asyncio.create_task(send_user_notification(
            uid,
            '<tg-emoji emoji-id=\"5420323339723881652\">⚠️</tg-emoji> <b>Your active task has been unassigned by the admin and returned to the pool.</b>\n\nChoose an option from the menu below:',
            reply_markup=get_main_menu_keyboard(),
            parse_mode=ParseMode.HTML
        ))

def format_dustbin_item_text(row, index: int, total: int) -> str:
    try:
        parts = row['details'].split(" | ")
        username = parts[0].replace("Email: ", "").strip()
        password = parts[1].replace("Pass: ", "").strip()
    except Exception:
        username = row['title'].replace("Login to ", "").strip()
        password = "See Admin"

    removed_str = row['removed_at'].strftime("%b %d, %Y %I:%M %p") if row['removed_at'] else "Unknown"

    return (
        f"<tg-emoji emoji-id=\"5262529363710060188\">🗑</tg-emoji> <b>Dustbin Item {index}/{total}</b>\n\n"
        f"<tg-emoji emoji-id=\"5253742260054409879\">📧</tg-emoji> <b>Email:</b> <code>{username}</code>\n"
        f"<tg-emoji emoji-id=\"6005570495603282482\">🔑</tg-emoji> <b>Password:</b> <code>{password}</code>\n"
        f"<tg-emoji emoji-id=\"5417924076503062111\">💰</tg-emoji> <b>Reward:</b> ₹{row['reward']:.2f}\n"
        f"🗓 <b>Removed At:</b> {removed_str}"
    )

def get_dustbin_item_keyboard(dustbin_id: int, page: int, total: int):
    kb = InlineKeyboardBuilder()
    nav_row = []
    if page > 1:
        nav_row.append(InlineKeyboardButton(text="<- Prev", callback_data=f"dustbin_view:{page - 1}"))
    nav_row.append(InlineKeyboardButton(text=f"{page}/{total}", callback_data="noop"))
    if page < total:
        nav_row.append(InlineKeyboardButton(text="Next ->", callback_data=f"dustbin_view:{page + 1}"))
    kb.row(*nav_row)
    kb.row(
        InlineKeyboardButton(text="Restore", icon_custom_emoji_id="5237699328843200968", callback_data=f"dustbin_restore:{dustbin_id}:{page}", style="success"),
        InlineKeyboardButton(text="Delete", icon_custom_emoji_id="5262529363710060188", callback_data=f"dustbin_delete:{dustbin_id}:{page}", style="danger"),
        InlineKeyboardButton(text="Replace", icon_custom_emoji_id="5348227245599105972", callback_data=f"dustbin_replace:{dustbin_id}:{page}", style="primary")
    )
    kb.row(InlineKeyboardButton(text="Back", icon_custom_emoji_id="5875082500023258804", callback_data="menu_back"))
    return kb.as_markup()

async def render_dustbin_page(page: int):
    """Returns (text, keyboard) for a single dustbin item at the given page, or an
    'empty' message if the dustbin has nothing left. Shared by view/restore/delete/replace
    so that acting on one item can jump straight to showing the next one."""
    async with db_pool.acquire() as conn:
        total = await conn.fetchval("SELECT COUNT(*) FROM dustbin_tasks")
        if total == 0:
            return "<tg-emoji emoji-id=\"5262831879731555779\">📭</tg-emoji> <b>Dustbin is empty.</b>", get_back_inline_keyboard("back_dustbin_menu")

        page = max(1, min(page, total))
        row = await conn.fetchrow(
            "SELECT id, title, details, reward, added_by, removed_at FROM dustbin_tasks ORDER BY id ASC OFFSET $1 LIMIT 1",
            page - 1
        )

    text = format_dustbin_item_text(row, page, total)
    kb = get_dustbin_item_keyboard(row['id'], page, total)
    return text, kb

@dp.callback_query(F.data.startswith("dustbin_view:"))
async def cb_dustbin_view(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    await call.answer()
    await state.clear()

    page = int(call.data.split(":", 1)[1])
    text, kb = await render_dustbin_page(page)

    try:
        await call.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            await call.message.answer(text, parse_mode=ParseMode.HTML, reply_markup=kb)

@dp.callback_query(F.data.startswith("dustbin_restore:"))
async def cb_dustbin_restore(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    parts = call.data.split(":")
    dustbin_id = int(parts[1])
    page = int(parts[2]) if len(parts) > 2 else 1

    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT title, details, reward, added_by FROM dustbin_tasks WHERE id=$1", dustbin_id)
        if not row:
            await call.answer("⚠️ This item is no longer in the Dustbin.", show_alert=True)
            return

        async with conn.transaction():
            await conn.execute(
                "INSERT INTO tasks (title, details, reward, status, added_by) VALUES ($1, $2, $3, 'available', $4)",
                row['title'], row['details'], row['reward'], row['added_by']
            )
            await conn.execute("DELETE FROM dustbin_tasks WHERE id=$1", dustbin_id)

    await call.answer("♻️ Task restored to the pool!")

    text, kb = await render_dustbin_page(page)
    try:
        await call.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            await call.message.answer(text, parse_mode=ParseMode.HTML, reply_markup=kb)
    except Exception:
        pass

@dp.callback_query(F.data.startswith("dustbin_delete:"))
async def cb_dustbin_delete(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    dustbin_id = int(call.data.split(":", 1)[1])

    async with db_pool.acquire() as conn:
        deleted = await conn.fetchval("DELETE FROM dustbin_tasks WHERE id=$1 RETURNING id", dustbin_id)

    if not deleted:
        await call.answer("⚠️ This item is no longer in the Dustbin.", show_alert=True)
        return

    await call.answer("🗑 Task permanently deleted!", show_alert=True)
    try:
        await call.message.edit_text("<tg-emoji emoji-id=\"5262529363710060188\">🗑</tg-emoji> <b>Task has been permanently deleted from the Dustbin.</b>", parse_mode=ParseMode.HTML, reply_markup=get_back_inline_keyboard("back_dustbin_menu"))
    except Exception:
        pass

@dp.callback_query(F.data.startswith("dustbin_replace:"))
async def cb_dustbin_replace(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    dustbin_id = int(call.data.split(":", 1)[1])

    async with db_pool.acquire() as conn:
        exists = await conn.fetchval("SELECT id FROM dustbin_tasks WHERE id=$1", dustbin_id)
    if not exists:
        await call.answer("⚠️ This item is no longer in the Dustbin.", show_alert=True)
        return

    await call.answer()
    await state.set_state(AdminState.waiting_for_dustbin_replace)
    await state.update_data(dustbin_replace_id=dustbin_id)
    sent = await call.message.answer(
        "✏️ Send the new Gmail <b>Username</b> to replace it for this Dustbin task (e.g. <code>example@gmail.com</code>):",
        parse_mode=ParseMode.HTML
    )
    await state.update_data(last_menu_msg_id=sent.message_id)

@dp.message(AdminState.waiting_for_dustbin_replace, F.text, ~F.text.startswith("/"), ~F.text.in_(MENU_BUTTONS))
async def process_dustbin_replace(message: Message, state: FSMContext):
    await cleanup_last_menu(message, state)
    if message.from_user.id != ADMIN_ID:
        return

    data = await state.get_data()
    dustbin_id = data.get("dustbin_replace_id")
    await state.clear()

    username_input = message.text.strip()
    if "@gmail.com" not in username_input.lower() and "@" not in username_input:
        username = f"{username_input}@gmail.com"
    else:
        username = username_input

    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT details FROM dustbin_tasks WHERE id=$1", dustbin_id)
        if not row:
            await message.answer("⚠️ This item is no longer in the Dustbin.", reply_markup=get_admin_menu_keyboard())
            return

        if DEFAULT_TASK_PASS_STATUS:
            password = DEFAULT_TASK_PASS
        else:
            password = generate_random_password(12)

        new_details = f"Email: {username} | Pass: {password}"
        new_title = f"Login to {username}"

        await conn.execute(
            "UPDATE dustbin_tasks SET title=$1, details=$2 WHERE id=$3",
            new_title, new_details, dustbin_id
        )

    await message.answer(
        f"<tg-emoji emoji-id=\"6217663806110175239\">✅</tg-emoji> <b>Dustbin task's Gmail username replaced!</b>\n\n<tg-emoji emoji-id=\"5253742260054409879\">📧</tg-emoji> <code>{username}</code>",
        parse_mode=ParseMode.HTML,
        reply_markup=get_admin_menu_keyboard()
    )

# ============================================
# TUTORIAL VIDEOS SYSTEM
# ============================================

def get_videos_menu_keyboard():
    kb = InlineKeyboardBuilder()
    task_status = "<tg-emoji emoji-id=\"5416081784641168838\">🟢</tg-emoji> Set" if TASK_VIDEO_LINK else "<tg-emoji emoji-id=\"5411225014148014586\">🔴</tg-emoji> Not Set"
    sell_status = "<tg-emoji emoji-id=\"5416081784641168838\">🟢</tg-emoji> Set" if SELL_VIDEO_LINK else "<tg-emoji emoji-id=\"5411225014148014586\">🔴</tg-emoji> Not Set"
    howto_status = "<tg-emoji emoji-id=\"5416081784641168838\">🟢</tg-emoji> Set" if HOWTO_VIDEO_LINK else "<tg-emoji emoji-id=\"5411225014148014586\">🔴</tg-emoji> Not Set"
    kb.button(text=f"Tasks Video: {task_status}", icon_custom_emoji_id="5197269100878907942", callback_data="video_set:tasks", style="primary")
    kb.button(text=f"Sell Video: {sell_status}", icon_custom_emoji_id="5377548235709619284", callback_data="video_set:sell", style="primary")
    kb.button(text=f"How To Use Bot Video: {howto_status}", icon_custom_emoji_id="5436113877181941026", callback_data="video_set:howto", style="primary")
    kb.adjust(1, 1, 1)
    return kb.as_markup()

@dp.message(F.text == "Videos", StateFilter("*"))
async def admin_btn_videos(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.clear()
    await message.answer(
        "🤷‍♂️ <b>Tutorial Video Links</b>\n\n"
        "Set a video link (YouTube, Telegram post, etc.) for each section below. "
        "The related button only appears to users once a link is set.\n\n"
        "Tap a section to set or update its link.",
        parse_mode=ParseMode.HTML,
        reply_markup=get_videos_menu_keyboard()
    )

@dp.callback_query(F.data.startswith("video_set:"))
async def cb_video_set(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    await call.answer()

    video_key = call.data.split(":", 1)[1]
    labels = {"tasks": "Tasks", "sell": "Sell Gmail", "howto": "How To Use Bot"}
    label = labels.get(video_key, video_key)

    await state.set_state(AdminState.waiting_for_video_link)
    await state.update_data(video_key=video_key)
    sent = await call.message.answer(
        f"🎬 Send the new video link for <b>{label}</b>.\n\n"
        f"Send <code>remove</code> to clear the current link and hide the button.",
        parse_mode=ParseMode.HTML
    )
    await state.update_data(last_menu_msg_id=sent.message_id)

@dp.message(AdminState.waiting_for_video_link, F.text, ~F.text.startswith("/"), ~F.text.in_(MENU_BUTTONS))
async def process_video_link(message: Message, state: FSMContext):
    await cleanup_last_menu(message, state)
    if message.from_user.id != ADMIN_ID:
        return

    global TASK_VIDEO_LINK, SELL_VIDEO_LINK, HOWTO_VIDEO_LINK

    data = await state.get_data()
    video_key = data.get("video_key")
    await state.clear()

    text_input = message.text.strip()
    clearing = text_input.lower() in ("remove", "off", "-", "clear", "none")
    new_value = None if clearing else text_input

    key_map = {
        "tasks": "video_task_link",
        "sell": "video_sell_link",
        "howto": "video_howto_link"
    }
    labels = {"tasks": "Tasks", "sell": "Sell Gmail", "howto": "How To Use Bot"}
    setting_key = key_map.get(video_key)
    label = labels.get(video_key, video_key)

    if not setting_key:
        await message.answer("⚠️ Invalid video section. Please try again from the Videos menu.", reply_markup=get_admin_menu_keyboard())
        return

    async with db_pool.acquire() as conn:
        if clearing:
            await conn.execute("DELETE FROM bot_settings WHERE key=$1", setting_key)
        else:
            await conn.execute(
                "INSERT INTO bot_settings (key, value) VALUES ($1, $2) ON CONFLICT (key) DO UPDATE SET value = $2",
                setting_key, new_value
            )

    if video_key == "tasks":
        TASK_VIDEO_LINK = new_value
    elif video_key == "sell":
        SELL_VIDEO_LINK = new_value
    elif video_key == "howto":
        HOWTO_VIDEO_LINK = new_value

    if clearing:
        await message.answer(f"<tg-emoji emoji-id=\"5262529363710060188\">🗑</tg-emoji> <b>{label}</b> video link removed. The button will no longer appear.", parse_mode=ParseMode.HTML, reply_markup=get_admin_menu_keyboard())
    else:
        await message.answer(f"<tg-emoji emoji-id=\"6217663806110175239\">✅</tg-emoji> <b>{label}</b> video link updated!\n\n<tg-emoji emoji-id=\"5271604874419647061\">🔗</tg-emoji> {new_value}", parse_mode=ParseMode.HTML, reply_markup=get_admin_menu_keyboard())

@dp.message(F.text == "Find ID", StateFilter("*"))
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
    await cleanup_last_menu(message, state)
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
        await message.answer(f"<tg-emoji emoji-id=\"5262831879731555779\">📭</tg-emoji> <b>No records found matching:</b> <code>{query}</code>", parse_mode=ParseMode.HTML, reply_markup=get_admin_menu_keyboard())
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
            'available': '<tg-emoji emoji-id=\"5416081784641168838\">🟢</tg-emoji>',
            'assigned': '🔵',
            'pending_review': '🟡',
            'completed': '<tg-emoji emoji-id=\"6217663806110175239\">✅</tg-emoji>'
        }.get(task_match['status'], '⚪️')

        text += (
            f"<tg-emoji emoji-id=\"5197269100878907942\">📋</tg-emoji> <b>Task Record</b>\n"
            f"• 🆔 <b>Task ID:</b> <code>#{task_match['id']}</code>\n"
            f"• <tg-emoji emoji-id=\"5237699328843200968\">📌</tg-emoji> <b>Status:</b> {status_emoji} <b>{task_match['status'].upper()}</b>\n"
            f"• <tg-emoji emoji-id=\"5870458774455587120\">👤</tg-emoji> <b>Assigned User:</b> {assigned_str}\n"
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
            'approved': '<tg-emoji emoji-id=\"5416081784641168838\">🟢</tg-emoji>',
            'declined': '<tg-emoji emoji-id=\"5411225014148014586\">🔴</tg-emoji>'
        }.get(sell_match['status'], '⚪️')

        text += (
            f"<tg-emoji emoji-id=\"5472027899789843495\">📦</tg-emoji> <b>Sell Request Record</b>\n"
            f"• 🆔 <b>Sell ID:</b> <code>#{sell_match['id']}</code>\n"
            f"• <tg-emoji emoji-id=\"5237699328843200968\">📌</tg-emoji> <b>Status:</b> {status_emoji} <b>{sell_match['status'].upper()}</b>\n"
            f"• <tg-emoji emoji-id=\"5870458774455587120\">👤</tg-emoji> <b>Seller:</b> {s_name} (<code>{seller_id}</code>)\n"
            f"• <tg-emoji emoji-id=\"5417924076503062111\">💰</tg-emoji> <b>Amount:</b> ₹{sell_match['amount']:.2f}\n"
            f"• 📝 <b>Details:</b>\n<code>{sell_match['details']}</code>\n"
        )

    kb = InlineKeyboardBuilder()
    if target_task_id:
        kb.button(
            text="ViewPast", icon_custom_emoji_id="5440410042773824003",
            callback_data=f"view_past_task:{target_task_id}",
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
            f"<tg-emoji emoji-id=\"5262831879731555779\">📭</tg-emoji> <b>No past assignment history recorded for Task #{task_id}.</b>",
            parse_mode=ParseMode.HTML
        )
        return

    history_text = (
        f"<tg-emoji emoji-id=\"5440410042773824003\">📜</tg-emoji> <b>Past Assignment History for Task #{task_id}</b>\n"
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
            f"• <tg-emoji emoji-id=\"5870458774455587120\">👤</tg-emoji> <b>User:</b> {user_display} (<code>{u_id}</code>)\n"
            f"• <tg-emoji emoji-id=\"6005570495603282482\">🔑</tg-emoji> <b>Password:</b> <code>{pwd}</code>\n"
            f"• 📅 <b>Assigned At:</b> {dt_str}\n"
            f"────────────────────\n"
        )

    await call.message.answer(history_text, parse_mode=ParseMode.HTML)

@dp.message(F.text == "Add Balance", StateFilter("*"))
async def admin_btn_add_balance(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.set_state(AdminState.waiting_for_add_balance)
    await message.answer("<tg-emoji emoji-id=\"5417924076503062111\">💰</tg-emoji> Send the User ID and Amount separated by space:\n\n<i>Example: 123456789 50</i>", parse_mode=ParseMode.HTML)

@dp.message(AdminState.waiting_for_add_balance, ~F.text.startswith("/"), ~F.text.in_(MENU_BUTTONS))
async def process_add_balance_step(message: Message, state: FSMContext):
    await cleanup_last_menu(message, state)
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
        await message.answer(f"<tg-emoji emoji-id=\"6217663806110175239\">✅</tg-emoji> **Added ₹{amount:.2f} to User `{target_id}`'s balance.**", parse_mode=ParseMode.MARKDOWN, reply_markup=get_admin_menu_keyboard())
        
        asyncio.create_task(send_user_notification(
            target_id,
            f"<tg-emoji emoji-id=\"5417924076503062111\">💰</tg-emoji> <b>Admin credited your balance!</b>\n+₹{amount:.2f} added to your account.",
            parse_mode=ParseMode.HTML
        ))
    except Exception as e:
        await message.answer(f"<tg-emoji emoji-id=\"5274099962655816924\">❌</tg-emoji> Invalid format or error: `{e}`", parse_mode=ParseMode.MARKDOWN, reply_markup=get_admin_menu_keyboard())
    await state.clear()

@dp.message(F.text == "Cut Balance", StateFilter("*"))
async def admin_btn_cut_balance(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.set_state(AdminState.waiting_for_cut_balance)
    await message.answer("<tg-emoji emoji-id=\"5420323339723881652\">⚠️</tg-emoji> Send the User ID and Amount to deduct separated by space:\n\n<i>Example: 123456789 20</i>", parse_mode=ParseMode.HTML)

@dp.message(AdminState.waiting_for_cut_balance, ~F.text.startswith("/"), ~F.text.in_(MENU_BUTTONS))
async def process_cut_balance_step(message: Message, state: FSMContext):
    await cleanup_last_menu(message, state)
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
        await message.answer(f"<tg-emoji emoji-id=\"6217663806110175239\">✅</tg-emoji> **Deducted ₹{amount:.2f} from User `{target_id}`'s balance.**", parse_mode=ParseMode.MARKDOWN, reply_markup=get_admin_menu_keyboard())
        
        async with db_pool.acquire() as conn:
            target_user_data = await conn.fetchrow("SELECT currency FROM users WHERE user_id=$1", target_id)
            target_curr = target_user_data['currency'] if target_user_data else "USD"
        
        formatted_deduct_amt = format_currency(amount, target_curr)
        asyncio.create_task(send_user_notification(
            target_id,
            f"<tg-emoji emoji-id=\"5420323339723881652\">⚠️</tg-emoji> <b>Admin deducted from your balance!</b>\n-{formatted_deduct_amt} deducted from your account.",
            parse_mode=ParseMode.HTML
        ))
    except Exception as e:
        await message.answer(f"<tg-emoji emoji-id=\"5274099962655816924\">❌</tg-emoji> Invalid format or error: `{e}`", parse_mode=ParseMode.MARKDOWN, reply_markup=get_admin_menu_keyboard())
    await state.clear()

@dp.message(F.text == "Check Balance", StateFilter("*"))
async def admin_btn_check_balance(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.set_state(AdminState.waiting_for_check_balance)
    await message.answer("🔎 Send the numeric User ID to check:", parse_mode=ParseMode.MARKDOWN)

@dp.message(AdminState.waiting_for_check_balance, ~F.text.startswith("/"), ~F.text.in_(MENU_BUTTONS))
async def process_check_balance_step(message: Message, state: FSMContext):
    await cleanup_last_menu(message, state)
    try:
        target_id = int(message.text.strip())
        user_data = await get_user_data(target_id)
        if not user_data:
            await message.answer(f"<tg-emoji emoji-id=\"5262831879731555779\">📭</tg-emoji> User `{target_id}` not found in database.", parse_mode=ParseMode.MARKDOWN, reply_markup=get_admin_menu_keyboard())
            await state.clear()
            return

        bal = user_data['balance']
        upi = user_data['upi']
        usdt = user_data['usdt_address']

        await message.answer(
            f"<tg-emoji emoji-id=\"5870458774455587120\">👤</tg-emoji> **User Information for `{target_id}`:**\n\n"
            f"• **Balance:** ₹{bal:.2f}\n"
            f"• **UPI:** `{upi}`\n"
            f"• **USDT BEP-20:** `{usdt}`",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=get_admin_menu_keyboard()
        )
    except ValueError:
        await message.answer("❌ Invalid User ID.", reply_markup=get_admin_menu_keyboard())
    await state.clear()

@dp.message(F.text == "Top Balances", StateFilter("*"))
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

@dp.message(F.text == "Transactions", StateFilter("*"))
async def admin_btn_transactions(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.set_state(AdminState.waiting_for_user_transactions)
    await message.answer("<tg-emoji emoji-id=\"5445353829304387411\">💳</tg-emoji> Send the User ID to check their transaction history:", parse_mode=ParseMode.MARKDOWN)

@dp.message(AdminState.waiting_for_user_transactions, ~F.text.startswith("/"), ~F.text.in_(MENU_BUTTONS))
async def process_user_transactions_step(message: Message, state: FSMContext):
    await cleanup_last_menu(message, state)
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

@dp.message(F.text == "View Stats", StateFilter("*"))
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
        f"<tg-emoji emoji-id=\"5244837092042750681\">📊</tg-emoji> <b>Bot Task & User Statistics</b>\n\n"
        f"<tg-emoji emoji-id=\"5391292736647209211\">👥</tg-emoji> <b>Total Users (started bot):</b> <code>{total_users}</code>\n\n"
        f"<tg-emoji emoji-id=\"5197269100878907942\">📋</tg-emoji> <b>Total Tasks Added:</b> <code>{total_tasks}</code>\n"
        f"<tg-emoji emoji-id=\"5416081784641168838\">🟢</tg-emoji> <b>Available Tasks Pool:</b> <code>{avail_tasks}</code>\n"
        f"💼 <b>Assigned Tasks:</b> <code>{assigned_tasks}</code>\n"
        f"<tg-emoji emoji-id=\"5382194935057372936\">⏳</tg-emoji> <b>Pending Review (Tasks + Sells):</b> <code>{total_pending}</code>\n"
        f"<tg-emoji emoji-id=\"6217663806110175239\">✅</tg-emoji> <b>Completed Tasks:</b> <code>{completed_tasks or 0}</code>\n"
        f"<tg-emoji emoji-id=\"5472027899789843495\">📦</tg-emoji> <b>Completed Sell Gmail:</b> <code>{completed_sells or 0}</code>\n"
        f"<tg-emoji emoji-id=\"5444856076954520455\">💸</tg-emoji> <b>Pending Withdrawals:</b> <code>{pending_withdrawals or 0}</code>"
    )
    await message.answer(text, parse_mode=ParseMode.HTML, reply_markup=get_admin_menu_keyboard())

@dp.message(F.text == "Ban User", StateFilter("*"))
async def admin_btn_ban_user(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.set_state(AdminState.waiting_for_ban_user)
    await message.answer("<tg-emoji emoji-id=\"5240241223632954241\">🚫</tg-emoji> Send the numeric User ID to ban:", parse_mode=ParseMode.MARKDOWN)

@dp.message(AdminState.waiting_for_ban_user, ~F.text.startswith("/"), ~F.text.in_(MENU_BUTTONS))
async def process_ban_user_step(message: Message, state: FSMContext):
    await cleanup_last_menu(message, state)
    try:
        target_id = int(message.text.strip())
        if target_id == ADMIN_ID:
            await message.answer("❌ You cannot ban yourself!", reply_markup=get_admin_menu_keyboard())
            await state.clear()
            return

        if target_id in BANNED_USERS_CACHE:
            await message.answer(f"<tg-emoji emoji-id=\"5420323339723881652\">⚠️</tg-emoji> User `{target_id}` is already banned.", parse_mode=ParseMode.MARKDOWN, reply_markup=get_admin_menu_keyboard())
            await state.clear()
            return

        async with db_pool.acquire() as conn:
            await conn.execute("INSERT INTO banned_users (user_id) VALUES ($1) ON CONFLICT DO NOTHING", target_id)

        BANNED_USERS_CACHE.add(target_id)
        await message.answer(f"<tg-emoji emoji-id=\"5240241223632954241\">🚫</tg-emoji> **User `{target_id}` has been banned!**", parse_mode=ParseMode.MARKDOWN, reply_markup=get_admin_menu_keyboard())
    except ValueError:
        await message.answer("❌ Invalid User ID.", reply_markup=get_admin_menu_keyboard())
    await state.clear()

@dp.message(F.text == "Unban User", StateFilter("*"))
async def admin_btn_unban_user(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.set_state(AdminState.waiting_for_unban_user)
    await message.answer("<tg-emoji emoji-id=\"6217663806110175239\">✅</tg-emoji> Send the numeric User ID to unban:", parse_mode=ParseMode.MARKDOWN)

@dp.message(AdminState.waiting_for_unban_user, ~F.text.startswith("/"), ~F.text.in_(MENU_BUTTONS))
async def process_unban_user_step(message: Message, state: FSMContext):
    await cleanup_last_menu(message, state)
    try:
        target_id = int(message.text.strip())

        async with db_pool.acquire() as conn:
            res = await conn.execute("DELETE FROM banned_users WHERE user_id=$1", target_id)

        if res == "DELETE 0" and target_id not in BANNED_USERS_CACHE:
            await message.answer(f"<tg-emoji emoji-id=\"5420323339723881652\">⚠️</tg-emoji> User `{target_id}` is not currently banned, so they cannot be unbanned.", parse_mode=ParseMode.MARKDOWN, reply_markup=get_admin_menu_keyboard())
            await state.clear()
            return

        BANNED_USERS_CACHE.discard(target_id)
        await message.answer(f"<tg-emoji emoji-id=\"6217663806110175239\">✅</tg-emoji> **User `{target_id}` has been unbanned!**", parse_mode=ParseMode.MARKDOWN, reply_markup=get_admin_menu_keyboard())
    except ValueError:
        await message.answer("❌ Invalid User ID.", reply_markup=get_admin_menu_keyboard())
    await state.clear()

@dp.message(F.text == "Broadcast", StateFilter("*"))
async def admin_btn_broadcast(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.clear()
    kb = InlineKeyboardBuilder()
    kb.button(text="Message", icon_custom_emoji_id="5434144690511290129", callback_data="bc_type:message", style="primary")
    kb.button(text="Giveaway", icon_custom_emoji_id="5461151367559141950", callback_data="bc_type:giveaway", style="success")
    kb.adjust(2)
    await message.answer(
        "<tg-emoji emoji-id=\"5332724926216428039\">📢</tg-emoji> <b>Broadcast Center</b>\n\n"
        "Choose what you want to send out to users:",
        parse_mode=ParseMode.HTML,
        reply_markup=kb.as_markup()
    )

@dp.callback_query(F.data == "bc_type:message")
async def cb_broadcast_type_message(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    await call.answer()
    await state.set_state(AdminState.waiting_for_broadcast)
    try:
        await call.message.edit_text("📢 Send or forward the broadcast message below:")
    except Exception:
        await call.message.answer("📢 Send or forward the broadcast message below:")

@dp.callback_query(F.data == "bc_type:giveaway")
async def cb_broadcast_type_giveaway(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    await call.answer()
    await state.set_state(AdminState.waiting_for_giveaway_message)
    try:
        await call.message.edit_text("🎉 Send or forward the Giveaway message below (text, photo, etc.):")
    except Exception:
        await call.message.answer("🎉 Send or forward the Giveaway message below (text, photo, etc.):")

@dp.message(AdminState.waiting_for_broadcast, ~F.text.in_(MENU_BUTTONS))
async def process_broadcast_message(message: Message, state: FSMContext):
    await cleanup_last_menu(message, state)
    if message.from_user.id != ADMIN_ID:
        return

    # Detect if the admin forwarded this message to us (vs. typed/sent it directly).
    # Covers both the newer Bot API (forward_origin) and older fields
    # (forward_date / forward_from / forward_from_chat) for compatibility.
    is_forwarded = bool(
        getattr(message, "forward_origin", None)
        or getattr(message, "forward_date", None)
        or getattr(message, "forward_from", None)
        or getattr(message, "forward_from_chat", None)
    )

    await state.update_data(
        broadcast_chat_id=message.chat.id,
        broadcast_message_id=message.message_id,
        broadcast_is_forwarded=is_forwarded
    )
    await state.set_state(AdminState.waiting_for_broadcast_target)

    kb = InlineKeyboardBuilder()
    kb.button(text="24 Hours", icon_custom_emoji_id="5391032818111363540", callback_data="bcdur:24")
    kb.button(text="48 Hours", icon_custom_emoji_id="5391032818111363540", callback_data="bcdur:48")
    kb.button(text="72 Hours", icon_custom_emoji_id="5391032818111363540", callback_data="bcdur:72")
    kb.button(text="All Users", icon_custom_emoji_id="5391292736647209211", callback_data="bcdur:all")
    kb.adjust(1)

    await message.answer(
        "<tg-emoji emoji-id=\"5332724926216428039\">📢</tg-emoji> <b>Select the target audience for this broadcast:</b>\n\n"
        "Choose which users (based on their last activity) should receive this message.",
        parse_mode=ParseMode.HTML,
        reply_markup=kb.as_markup()
    )

@dp.callback_query(F.data.startswith("bcdur:"), AdminState.waiting_for_broadcast_target)
async def process_broadcast_target_selection(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return

    await call.answer()

    data = await state.get_data()
    from_chat_id = data.get("broadcast_chat_id")
    message_id = data.get("broadcast_message_id")
    is_forwarded = data.get("broadcast_is_forwarded", False)

    if not from_chat_id or not message_id:
        try:
            await call.message.edit_text("⚠️ Broadcast session expired. Please start again.")
        except Exception:
            pass
        await state.clear()
        return

    duration_key = call.data.split(":", 1)[1]

    duration_labels = {
        "24": "Users active in the last 24 Hours",
        "48": "Users active in the last 48 Hours",
        "72": "Users active in the last 72 Hours",
        "all": "All Users"
    }
    label = duration_labels.get(duration_key, "All Users")

    async with db_pool.acquire() as conn:
        if duration_key == "24":
            users = await conn.fetch("SELECT user_id FROM users WHERE last_active >= NOW() - INTERVAL '24 hours'")
        elif duration_key == "48":
            users = await conn.fetch("SELECT user_id FROM users WHERE last_active >= NOW() - INTERVAL '48 hours'")
        elif duration_key == "72":
            users = await conn.fetch("SELECT user_id FROM users WHERE last_active >= NOW() - INTERVAL '72 hours'")
        else:
            users = await conn.fetch("SELECT user_id FROM users")

    if not users:
        try:
            await call.message.edit_text(
                f"<tg-emoji emoji-id=\"5262831879731555779\">📭</tg-emoji> No users found for: <b>{label}</b>",
                parse_mode=ParseMode.HTML,
                reply_markup=get_back_inline_keyboard("back_admin_menu")
            )
        except Exception:
            pass
        await state.clear()
        return

    total_users = len(users)
    try:
        status_msg = await call.message.edit_text(
            f"<tg-emoji emoji-id=\"5382194935057372936\">⏳</tg-emoji> <b>Broadcast in progress...</b>\n"
            f"<tg-emoji emoji-id=\"5310278924616356636\">🎯</tg-emoji> Target: <b>{label}</b>\n"
            f"Total targets: <b>{total_users}</b>",
            parse_mode=ParseMode.HTML
        )
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            status_msg = await call.message.answer(
                f"<tg-emoji emoji-id=\"5382194935057372936\">⏳</tg-emoji> <b>Broadcast in progress...</b>\n"
                f"<tg-emoji emoji-id=\"5310278924616356636\">🎯</tg-emoji> Target: <b>{label}</b>\n"
                f"Total targets: <b>{total_users}</b>",
                parse_mode=ParseMode.HTML
            )
        else:
            status_msg = call.message

    success_count = 0
    fail_count = 0

    for idx, u in enumerate(users, start=1):
        target_id = u['user_id']
        try:
            if is_forwarded:
                await bot.forward_message(
                    chat_id=target_id,
                    from_chat_id=from_chat_id,
                    message_id=message_id
                )
            else:
                await bot.copy_message(
                    chat_id=target_id,
                    from_chat_id=from_chat_id,
                    message_id=message_id
                )
            success_count += 1
        except TelegramForbiddenError:
            fail_count += 1
        except Exception:
            fail_count += 1

        if idx % 20 == 0 or idx == total_users:
            try:
                await status_msg.edit_text(
                    f"<tg-emoji emoji-id=\"5382194935057372936\">⏳</tg-emoji> <b>Broadcasting...</b> ({idx}/{total_users})\n"
                    f"<tg-emoji emoji-id=\"5310278924616356636\">🎯</tg-emoji> Target: <b>{label}</b>\n\n"
                    f"<tg-emoji emoji-id=\"5416081784641168838\">🟢</tg-emoji> Success: <b>{success_count}</b>\n"
                    f"<tg-emoji emoji-id=\"5411225014148014586\">🔴</tg-emoji> Failed: <b>{fail_count}</b>",
                    parse_mode=ParseMode.HTML
                )
            except Exception:
                pass

        await asyncio.sleep(0.04)

    await status_msg.edit_text(
        f"<tg-emoji emoji-id=\"6217663806110175239\">✅</tg-emoji> <b>Broadcast Completed!</b>\n\n"
        f"<tg-emoji emoji-id=\"5310278924616356636\">🎯</tg-emoji> <b>Target:</b> {label}\n"
        f"<tg-emoji emoji-id=\"5244837092042750681\">📊</tg-emoji> <b>Total Users Processed:</b> {total_users}\n"
        f"<tg-emoji emoji-id=\"5416081784641168838\">🟢</tg-emoji> <b>Successfully Sent:</b> {success_count}\n"
        f"<tg-emoji emoji-id=\"5411225014148014586\">🔴</tg-emoji> <b>Failed / Blocked:</b> {fail_count}",
        parse_mode=ParseMode.HTML,
        reply_markup=get_back_inline_keyboard("back_admin_menu")
    )
    await bot.send_message(
        ADMIN_ID,
        "🏠 Back to Admin Menu",
        reply_markup=get_admin_menu_keyboard()
    )
    await state.clear()

# ============================================
# GIVEAWAY SYSTEM
# ============================================

@dp.message(AdminState.waiting_for_giveaway_message, ~F.text.in_(MENU_BUTTONS))
async def process_giveaway_message(message: Message, state: FSMContext):
    await cleanup_last_menu(message, state)
    if message.from_user.id != ADMIN_ID:
        return

    await state.update_data(
        giveaway_chat_id=message.chat.id,
        giveaway_message_id=message.message_id
    )
    await state.set_state(AdminState.waiting_for_giveaway_emoji)

    kb = InlineKeyboardBuilder()
    kb.button(text="Dice", icon_custom_emoji_id="5890971177484029249", callback_data="gwemoji:dice", style="primary")
    kb.button(text="Bowling", icon_custom_emoji_id="5891120371762990493", callback_data="gwemoji:bowling", style="primary")
    kb.adjust(2)

    sent = await message.answer(
        "<tg-emoji emoji-id=\"5461151367559141950\">🎉</tg-emoji> <b>Choose the Giveaway game:</b>\n\n"
        "Users will tap a button to roll and win a random reward (₹1-₹6).",
        parse_mode=ParseMode.HTML,
        reply_markup=kb.as_markup()
    )
    await state.update_data(last_menu_msg_id=sent.message_id)

@dp.callback_query(F.data.startswith("gwemoji:"), AdminState.waiting_for_giveaway_emoji)
async def process_giveaway_emoji_selection(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    await call.answer()

    emoji_key = call.data.split(":", 1)[1]
    await state.update_data(giveaway_emoji_key=emoji_key)
    await state.set_state(AdminState.waiting_for_giveaway_target)

    kb = InlineKeyboardBuilder()
    kb.button(text="24 Hours", icon_custom_emoji_id="5391032818111363540", callback_data="gwdur:24")
    kb.button(text="48 Hours", icon_custom_emoji_id="5391032818111363540", callback_data="gwdur:48")
    kb.button(text="72 Hours", icon_custom_emoji_id="5391032818111363540", callback_data="gwdur:72")
    kb.button(text="All Users", icon_custom_emoji_id="5391292736647209211", callback_data="gwdur:all")
    kb.adjust(1)

    try:
        await call.message.edit_text(
            "<tg-emoji emoji-id=\"5461151367559141950\">🎉</tg-emoji> <b>Select the target audience for this Giveaway:</b>\n\n"
            "Choose which users (based on their last activity) should receive this Giveaway.",
            parse_mode=ParseMode.HTML,
            reply_markup=kb.as_markup()
        )
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            await call.message.answer(
                "<tg-emoji emoji-id=\"5461151367559141950\">🎉</tg-emoji> <b>Select the target audience for this Giveaway:</b>\n\n"
                "Choose which users (based on their last activity) should receive this Giveaway.",
                parse_mode=ParseMode.HTML,
                reply_markup=kb.as_markup()
            )

@dp.callback_query(F.data.startswith("gwdur:"), AdminState.waiting_for_giveaway_target)
async def process_giveaway_target_selection(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    await call.answer()

    data = await state.get_data()
    from_chat_id = data.get("giveaway_chat_id")
    message_id = data.get("giveaway_message_id")
    emoji_key = data.get("giveaway_emoji_key", "dice")
    emoji_char = "🎳" if emoji_key == "bowling" else "🎲"

    if not from_chat_id or not message_id:
        try:
            await call.message.edit_text("⚠️ Giveaway session expired. Please start again.")
        except Exception:
            pass
        await state.clear()
        return

    duration_key = call.data.split(":", 1)[1]
    duration_labels = {
        "24": "Users active in the last 24 Hours",
        "48": "Users active in the last 48 Hours",
        "72": "Users active in the last 72 Hours",
        "all": "All Users"
    }
    label = duration_labels.get(duration_key, "All Users")

    async with db_pool.acquire() as conn:
        if duration_key == "24":
            users = await conn.fetch("SELECT user_id FROM users WHERE last_active >= NOW() - INTERVAL '24 hours'")
        elif duration_key == "48":
            users = await conn.fetch("SELECT user_id FROM users WHERE last_active >= NOW() - INTERVAL '48 hours'")
        elif duration_key == "72":
            users = await conn.fetch("SELECT user_id FROM users WHERE last_active >= NOW() - INTERVAL '72 hours'")
        else:
            users = await conn.fetch("SELECT user_id FROM users")

    if not users:
        try:
            await call.message.edit_text(
                f"<tg-emoji emoji-id=\"5262831879731555779\">📭</tg-emoji> No users found for: <b>{label}</b>",
                parse_mode=ParseMode.HTML,
                reply_markup=get_back_inline_keyboard("back_admin_menu")
            )
        except Exception:
            pass
        await state.clear()
        return

    async with db_pool.acquire() as conn:
        giveaway_id = await conn.fetchval(
            "INSERT INTO giveaways (emoji_type) VALUES ($1) RETURNING id",
            emoji_key
        )

    play_kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=f"{emoji_char} Try Your Luck!", callback_data=f"gwplay:{giveaway_id}", style="success")
    ]])

    total_users = len(users)
    try:
        status_msg = await call.message.edit_text(
            f"<tg-emoji emoji-id=\"5382194935057372936\">⏳</tg-emoji> <b>Giveaway broadcast in progress...</b>\n"
            f"<tg-emoji emoji-id=\"5310278924616356636\">🎯</tg-emoji> Target: <b>{label}</b>\n"
            f"Total targets: <b>{total_users}</b>",
            parse_mode=ParseMode.HTML
        )
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            status_msg = await call.message.answer(
                f"<tg-emoji emoji-id=\"5382194935057372936\">⏳</tg-emoji> <b>Giveaway broadcast in progress...</b>\n"
                f"<tg-emoji emoji-id=\"5310278924616356636\">🎯</tg-emoji> Target: <b>{label}</b>\n"
                f"Total targets: <b>{total_users}</b>",
                parse_mode=ParseMode.HTML
            )
        else:
            status_msg = call.message

    success_count = 0
    fail_count = 0

    for idx, u in enumerate(users, start=1):
        target_id = u['user_id']
        try:
            await bot.copy_message(
                chat_id=target_id,
                from_chat_id=from_chat_id,
                message_id=message_id,
                reply_markup=play_kb
            )
            success_count += 1
        except TelegramForbiddenError:
            fail_count += 1
        except Exception:
            fail_count += 1

        if idx % 20 == 0 or idx == total_users:
            try:
                await status_msg.edit_text(
                    f"<tg-emoji emoji-id=\"5382194935057372936\">⏳</tg-emoji> <b>Broadcasting Giveaway...</b> ({idx}/{total_users})\n"
                    f"<tg-emoji emoji-id=\"5310278924616356636\">🎯</tg-emoji> Target: <b>{label}</b>\n\n"
                    f"<tg-emoji emoji-id=\"5416081784641168838\">🟢</tg-emoji> Success: <b>{success_count}</b>\n"
                    f"<tg-emoji emoji-id=\"5411225014148014586\">🔴</tg-emoji> Failed: <b>{fail_count}</b>",
                    parse_mode=ParseMode.HTML
                )
            except Exception:
                pass

        await asyncio.sleep(0.04)

    await status_msg.edit_text(
        f"<tg-emoji emoji-id=\"6217663806110175239\">✅</tg-emoji> <b>Giveaway #{giveaway_id} Broadcast Completed!</b>\n\n"
        f"<tg-emoji emoji-id=\"5310278924616356636\">🎯</tg-emoji> <b>Target:</b> {label}\n"
        f"🎮 <b>Game:</b> {emoji_char}\n"
        f"<tg-emoji emoji-id=\"5244837092042750681\">📊</tg-emoji> <b>Total Users Processed:</b> {total_users}\n"
        f"<tg-emoji emoji-id=\"5416081784641168838\">🟢</tg-emoji> <b>Successfully Sent:</b> {success_count}\n"
        f"<tg-emoji emoji-id=\"5411225014148014586\">🔴</tg-emoji> <b>Failed / Blocked:</b> {fail_count}",
        parse_mode=ParseMode.HTML,
        reply_markup=get_back_inline_keyboard("back_admin_menu")
    )
    await bot.send_message(
        ADMIN_ID,
        "🏠 Back to Admin Menu",
        reply_markup=get_admin_menu_keyboard()
    )
    await state.clear()

@dp.callback_query(F.data.startswith("gwplay:"))
async def cb_giveaway_play(call: CallbackQuery):
    user_id = call.from_user.id
    giveaway_id = int(call.data.split(":", 1)[1])

    async with db_pool.acquire() as conn:
        already = await conn.fetchval(
            "SELECT id FROM giveaway_plays WHERE giveaway_id=$1 AND user_id=$2",
            giveaway_id, user_id
        )
        if already:
            await call.answer("⚠️ You already played this Giveaway!", show_alert=True)
            return

        giveaway_row = await conn.fetchrow("SELECT emoji_type FROM giveaways WHERE id=$1", giveaway_id)

    if not giveaway_row:
        await call.answer("⚠️ This Giveaway is no longer valid.", show_alert=True)
        return

    await call.answer()

    # Button disappears immediately - it should only be usable once
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    emoji_char = "🎳" if giveaway_row['emoji_type'] == "bowling" else "🎲"

    try:
        dice_msg = await bot.send_dice(chat_id=user_id, emoji=emoji_char)
        value = dice_msg.dice.value
    except Exception:
        return

    await asyncio.sleep(4)

    reward = float(value)

    async with db_pool.acquire() as conn:
        insert_result = await conn.execute(
            "INSERT INTO giveaway_plays (giveaway_id, user_id, value, reward) VALUES ($1, $2, $3, $4) ON CONFLICT (giveaway_id, user_id) DO NOTHING",
            giveaway_id, user_id, value, reward
        )
        if insert_result != "INSERT 0 1":
            return  # Already credited by a concurrent tap - avoid double-paying

        await ensure_user(user_id, conn=conn)
        await conn.execute("UPDATE users SET balance = balance + $1 WHERE user_id=$2", reward, user_id)
        await conn.execute(
            "INSERT INTO transactions (user_id, type, amount, note) VALUES ($1, $2, $3, $4)",
            user_id, "giveaway", reward, f"Giveaway #{giveaway_id} - {emoji_char} rolled {value}"
        )

    invalidate_user_cache(user_id)

    user_data = await get_user_data(user_id)
    reward_str = format_currency(reward, user_data['currency'] if user_data else 'INR')

    await bot.send_message(
        user_id,
        f"<tg-emoji emoji-id=\"5461151367559141950\">🎉</tg-emoji> You Win {reward_str}, Reward Has Been Credited To Your Balance!",
        parse_mode=ParseMode.HTML
    )

    await bot.send_message(
        ADMIN_ID,
        f"<tg-emoji emoji-id=\"5461151367559141950\">🎉</tg-emoji> <code>{user_id}</code> Wins ₹{reward:.2f}!",
        parse_mode=ParseMode.HTML
    )

@dp.message(F.text == "Change Values", StateFilter("*"))
async def admin_btn_change_values(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.clear()
    
    task_usd = DEFAULT_TASK_RATE / USD_TO_INR
    sell_usd = GMAIL_SELL_RATE / USD_TO_INR
    min_w_usd = MIN_WITHDRAWAL_AMT / USD_TO_INR

    text = (
        f"<tg-emoji emoji-id=\"5893161718179173515\">⚙️</tg-emoji> <b>Change System Rates & Limits</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📝 <b>Tasks Rate:</b> ₹{DEFAULT_TASK_RATE:.2f} <i>(${task_usd:.2f})</i>\n"
        f"<tg-emoji emoji-id=\"5377548235709619284\">📨</tg-emoji> <b>Sell Rate:</b> ₹{GMAIL_SELL_RATE:.2f} <i>(${sell_usd:.2f})</i>\n"
        f"<tg-emoji emoji-id=\"5444856076954520455\">💸</tg-emoji> <b>Min. Withdrawal:</b> ₹{MIN_WITHDRAWAL_AMT:.2f} <i>(${min_w_usd:.2f})</i>\n"
        f"<tg-emoji emoji-id=\"6005570495603282482\">🔑</tg-emoji> <b>Task Password:</b> <code>{DEFAULT_TASK_PASS}</code>\n"
        f"🔒 <b>Password Mode:</b> {'<tg-emoji emoji-id=\"5416081784641168838\">🟢</tg-emoji> Fixed Default' if DEFAULT_TASK_PASS_STATUS else '<tg-emoji emoji-id=\"5411225014148014586\">🔴</tg-emoji> Random'}\n"
        f"<tg-emoji emoji-id=\"5350387571199319521\">🏷</tg-emoji> <b>Fees:</b> UPI ₹{UPI_FEES:.2f} • USDT ₹{USDT_FEES:.2f} • Ultra ₹{ULTRA_FEES:.2f}\n"
        f"<tg-emoji emoji-id=\"5195033767969839232\">⚡️</tg-emoji> <b>Ultra Token:</b> <code>{ULTRA_TOKEN}</code>\n"
        f"<tg-emoji emoji-id=\"5197269100878907942\">✍️</tg-emoji> <b>Single Tasks:</b> {'<tg-emoji emoji-id=\"5416081784641168838\">🟢</tg-emoji> ON' if SINGLE_TASK_STATUS else '<tg-emoji emoji-id=\"5411225014148014586\">🔴</tg-emoji> OFF (Unlimited)'}\n"
        f"<tg-emoji emoji-id=\"5377548235709619284\">📨</tg-emoji> <b>Sell Gmail:</b> {'<tg-emoji emoji-id=\"5416081784641168838\">🟢</tg-emoji> ON' if SELL_GMAIL_STATUS else '<tg-emoji emoji-id=\"5411225014148014586\">🔴</tg-emoji> OFF'}\n"
        f"━━━━━━━━━━━━━━━━━━\n\n"
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
        f"<tg-emoji emoji-id=\"5893161718179173515\">⚙️</tg-emoji> <b>Change System Rates & Limits</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📝 <b>Tasks Rate:</b> ₹{DEFAULT_TASK_RATE:.2f} <i>(${task_usd:.2f})</i>\n"
        f"<tg-emoji emoji-id=\"5377548235709619284\">📨</tg-emoji> <b>Sell Rate:</b> ₹{GMAIL_SELL_RATE:.2f} <i>(${sell_usd:.2f})</i>\n"
        f"<tg-emoji emoji-id=\"5444856076954520455\">💸</tg-emoji> <b>Min. Withdrawal:</b> ₹{MIN_WITHDRAWAL_AMT:.2f} <i>(${min_w_usd:.2f})</i>\n"
        f"<tg-emoji emoji-id=\"6005570495603282482\">🔑</tg-emoji> <b>Task Password:</b> <code>{DEFAULT_TASK_PASS}</code>\n"
        f"🔒 <b>Password Mode:</b> {'<tg-emoji emoji-id=\"5416081784641168838\">🟢</tg-emoji> Fixed Default' if DEFAULT_TASK_PASS_STATUS else '<tg-emoji emoji-id=\"5411225014148014586\">🔴</tg-emoji> Random'}\n"
        f"<tg-emoji emoji-id=\"5350387571199319521\">🏷</tg-emoji> <b>Fees:</b> UPI ₹{UPI_FEES:.2f} • USDT ₹{USDT_FEES:.2f} • Ultra ₹{ULTRA_FEES:.2f}\n"
        f"<tg-emoji emoji-id=\"5195033767969839232\">⚡️</tg-emoji> <b>Ultra Token:</b> <code>{ULTRA_TOKEN}</code>\n"
        f"<tg-emoji emoji-id=\"5197269100878907942\">✍️</tg-emoji> <b>Single Tasks:</b> {'<tg-emoji emoji-id=\"5416081784641168838\">🟢</tg-emoji> ON' if SINGLE_TASK_STATUS else '<tg-emoji emoji-id=\"5411225014148014586\">🔴</tg-emoji> OFF (Unlimited)'}\n"
        f"<tg-emoji emoji-id=\"5377548235709619284\">📨</tg-emoji> <b>Sell Gmail:</b> {'<tg-emoji emoji-id=\"5416081784641168838\">🟢</tg-emoji> ON' if SELL_GMAIL_STATUS else '<tg-emoji emoji-id=\"5411225014148014586\">🔴</tg-emoji> OFF'}\n"
        f"━━━━━━━━━━━━━━━━━━\n\n"
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
        f"<tg-emoji emoji-id=\"5893161718179173515\">⚙️</tg-emoji> <b>Change System Rates & Limits</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📝 <b>Tasks Rate:</b> ₹{DEFAULT_TASK_RATE:.2f} <i>(${task_usd:.2f})</i>\n"
        f"<tg-emoji emoji-id=\"5377548235709619284\">📨</tg-emoji> <b>Sell Rate:</b> ₹{GMAIL_SELL_RATE:.2f} <i>(${sell_usd:.2f})</i>\n"
        f"<tg-emoji emoji-id=\"5444856076954520455\">💸</tg-emoji> <b>Min. Withdrawal:</b> ₹{MIN_WITHDRAWAL_AMT:.2f} <i>(${min_w_usd:.2f})</i>\n"
        f"<tg-emoji emoji-id=\"6005570495603282482\">🔑</tg-emoji> <b>Task Password:</b> <code>{DEFAULT_TASK_PASS}</code>\n"
        f"🔒 <b>Password Mode:</b> {'<tg-emoji emoji-id=\"5416081784641168838\">🟢</tg-emoji> Fixed Default' if DEFAULT_TASK_PASS_STATUS else '<tg-emoji emoji-id=\"5411225014148014586\">🔴</tg-emoji> Random'}\n"
        f"<tg-emoji emoji-id=\"5350387571199319521\">🏷</tg-emoji> <b>Fees:</b> UPI ₹{UPI_FEES:.2f} • USDT ₹{USDT_FEES:.2f} • Ultra ₹{ULTRA_FEES:.2f}\n"
        f"<tg-emoji emoji-id=\"5195033767969839232\">⚡️</tg-emoji> <b>Ultra Token:</b> <code>{ULTRA_TOKEN}</code>\n"
        f"<tg-emoji emoji-id=\"5197269100878907942\">✍️</tg-emoji> <b>Single Tasks:</b> {'<tg-emoji emoji-id=\"5416081784641168838\">🟢</tg-emoji> ON' if SINGLE_TASK_STATUS else '<tg-emoji emoji-id=\"5411225014148014586\">🔴</tg-emoji> OFF (Unlimited)'}\n"
        f"<tg-emoji emoji-id=\"5377548235709619284\">📨</tg-emoji> <b>Sell Gmail:</b> {'<tg-emoji emoji-id=\"5416081784641168838\">🟢</tg-emoji> ON' if SELL_GMAIL_STATUS else '<tg-emoji emoji-id=\"5411225014148014586\">🔴</tg-emoji> OFF'}\n"
        f"━━━━━━━━━━━━━━━━━━\n\n"
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
        f"<tg-emoji emoji-id=\"5893161718179173515\">⚙️</tg-emoji> <b>Change System Rates & Limits</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📝 <b>Tasks Rate:</b> ₹{DEFAULT_TASK_RATE:.2f} <i>(${task_usd:.2f})</i>\n"
        f"<tg-emoji emoji-id=\"5377548235709619284\">📨</tg-emoji> <b>Sell Rate:</b> ₹{GMAIL_SELL_RATE:.2f} <i>(${sell_usd:.2f})</i>\n"
        f"<tg-emoji emoji-id=\"5444856076954520455\">💸</tg-emoji> <b>Min. Withdrawal:</b> ₹{MIN_WITHDRAWAL_AMT:.2f} <i>(${min_w_usd:.2f})</i>\n"
        f"<tg-emoji emoji-id=\"6005570495603282482\">🔑</tg-emoji> <b>Task Password:</b> <code>{DEFAULT_TASK_PASS}</code>\n"
        f"🔒 <b>Password Mode:</b> {'<tg-emoji emoji-id=\"5416081784641168838\">🟢</tg-emoji> Fixed Default' if DEFAULT_TASK_PASS_STATUS else '<tg-emoji emoji-id=\"5411225014148014586\">🔴</tg-emoji> Random'}\n"
        f"<tg-emoji emoji-id=\"5350387571199319521\">🏷</tg-emoji> <b>Fees:</b> UPI ₹{UPI_FEES:.2f} • USDT ₹{USDT_FEES:.2f} • Ultra ₹{ULTRA_FEES:.2f}\n"
        f"<tg-emoji emoji-id=\"5195033767969839232\">⚡️</tg-emoji> <b>Ultra Token:</b> <code>{ULTRA_TOKEN}</code>\n"
        f"<tg-emoji emoji-id=\"5197269100878907942\">✍️</tg-emoji> <b>Single Tasks:</b> {'<tg-emoji emoji-id=\"5416081784641168838\">🟢</tg-emoji> ON' if SINGLE_TASK_STATUS else '<tg-emoji emoji-id=\"5411225014148014586\">🔴</tg-emoji> OFF (Unlimited)'}\n"
        f"<tg-emoji emoji-id=\"5377548235709619284\">📨</tg-emoji> <b>Sell Gmail:</b> {'<tg-emoji emoji-id=\"5416081784641168838\">🟢</tg-emoji> ON' if SELL_GMAIL_STATUS else '<tg-emoji emoji-id=\"5411225014148014586\">🔴</tg-emoji> OFF'}\n"
        f"━━━━━━━━━━━━━━━━━━\n\n"
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
        f"<tg-emoji emoji-id=\"5417924076503062111\">💰</tg-emoji> <b>Current Tasks Rate:</b> ₹{DEFAULT_TASK_RATE:.2f}\n\n"
        f"Send the new rate for Tasks in INR (e.g. <code>40.0</code>):",
        parse_mode=ParseMode.HTML
    )

@dp.message(AdminState.waiting_for_change_tasks_rate, ~F.text.startswith("/"), ~F.text.in_(MENU_BUTTONS))
async def process_change_tasks_rate_step(message: Message, state: FSMContext):
    await cleanup_last_menu(message, state)
    global DEFAULT_TASK_RATE
    try:
        new_rate = float(message.text.strip())
        DEFAULT_TASK_RATE = new_rate

        async with db_pool.acquire() as conn:
            await conn.execute("INSERT INTO bot_settings (key, value) VALUES ('default_task_rate', $1) ON CONFLICT (key) DO UPDATE SET value = $1", str(new_rate))
            await conn.execute("UPDATE tasks SET reward=$1 WHERE status='available'", new_rate)

        await message.answer(
            f"<tg-emoji emoji-id=\"6217663806110175239\">✅</tg-emoji> <b>Tasks Rate Updated Successfully!</b>\n\nNew Rate: ₹{new_rate:.2f}\n(All available tasks updated as well)",
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
        f"<tg-emoji emoji-id=\"5377548235709619284\">📨</tg-emoji> <b>Current Gmail Sell Rate:</b> ₹{GMAIL_SELL_RATE:.2f}\n\n"
        f"Send the new Sell Gmail rate in INR (e.g. <code>35.0</code>):",
        parse_mode=ParseMode.HTML
    )

@dp.message(AdminState.waiting_for_change_sell_rate, ~F.text.startswith("/"), ~F.text.in_(MENU_BUTTONS))
async def process_change_sell_rate_step(message: Message, state: FSMContext):
    await cleanup_last_menu(message, state)
    global GMAIL_SELL_RATE
    try:
        new_rate = float(message.text.strip())
        GMAIL_SELL_RATE = new_rate

        async with db_pool.acquire() as conn:
            await conn.execute("INSERT INTO bot_settings (key, value) VALUES ('gmail_sell_rate', $1) ON CONFLICT (key) DO UPDATE SET value = $1", str(new_rate))

        await message.answer(
            f"<tg-emoji emoji-id=\"6217663806110175239\">✅</tg-emoji> <b>Gmail Sell Rate Updated Successfully!</b>\n\nNew Rate: ₹{new_rate:.2f}",
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
        f"<tg-emoji emoji-id=\"5444856076954520455\">💸</tg-emoji> <b>Current Minimum Withdrawal:</b> ₹{MIN_WITHDRAWAL_AMT:.2f}\n\n"
        f"Send the new Minimum Withdrawal limit in INR (e.g. <code>100.0</code>):",
        parse_mode=ParseMode.HTML
    )

@dp.message(AdminState.waiting_for_change_min_withdraw, ~F.text.startswith("/"), ~F.text.in_(MENU_BUTTONS))
async def process_change_min_withdraw_step(message: Message, state: FSMContext):
    await cleanup_last_menu(message, state)
    global MIN_WITHDRAWAL_AMT
    try:
        new_min = float(message.text.strip())
        MIN_WITHDRAWAL_AMT = new_min

        async with db_pool.acquire() as conn:
            await conn.execute("INSERT INTO bot_settings (key, value) VALUES ('min_withdrawal_rate', $1) ON CONFLICT (key) DO UPDATE SET value = $1", str(new_min))

        await message.answer(
            f"<tg-emoji emoji-id=\"6217663806110175239\">✅</tg-emoji> <b>Minimum Withdrawal Updated Successfully!</b>\n\nNew Minimum Limit: ₹{new_min:.2f}",
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
        f"<tg-emoji emoji-id=\"6005570495603282482\">🔑</tg-emoji> <b>Current Task Password:</b> <code>{DEFAULT_TASK_PASS}</code>\n\n"
        f"Send the new default password for newly added tasks (e.g. <code>TaskVerseX</code>):",
        parse_mode=ParseMode.HTML
    )

@dp.message(AdminState.waiting_for_change_task_pass, ~F.text.startswith("/"), ~F.text.in_(MENU_BUTTONS))
async def process_change_task_pass_step(message: Message, state: FSMContext):
    await cleanup_last_menu(message, state)
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
        f"<tg-emoji emoji-id=\"6217663806110175239\">✅</tg-emoji> <b>Default Task Password Updated Successfully!</b>\n\nNew Password: <code>{new_pass}</code>",
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
        f"<tg-emoji emoji-id=\"5350387571199319521\">🏷</tg-emoji> <b>Current Fees Settings:</b>\n"
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
    await cleanup_last_menu(message, state)
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
            f"<tg-emoji emoji-id=\"6217663806110175239\">✅</tg-emoji> <b>Fees Updated Successfully!</b>\n\n" + "\n".join(updated),
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
        f"<tg-emoji emoji-id=\"5195033767969839232\">⚡️</tg-emoji> <b>Current Ultra Gateway Settings:</b>\n\n"
        f"<tg-emoji emoji-id=\"6005570495603282482\">🔑</tg-emoji> <b>API Token:</b> <code>{ULTRA_TOKEN}</code>\n"
        f"🔐 <b>API Key:</b> <code>{ULTRA_KEY}</code>\n\n"
        f"Send the new API Token (or token and key separated by space):",
        parse_mode=ParseMode.HTML
    )

@dp.message(AdminState.waiting_for_change_ultra_token, ~F.text.startswith("/"), ~F.text.in_(MENU_BUTTONS))
async def process_change_ultra_token_step(message: Message, state: FSMContext):
    await cleanup_last_menu(message, state)
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
        f"<tg-emoji emoji-id=\"6217663806110175239\">✅</tg-emoji> <b>Ultra Gateway API Configuration Updated!</b>\n\n"
        f"<tg-emoji emoji-id=\"6005570495603282482\">🔑</tg-emoji> <b>Token:</b> <code>{ULTRA_TOKEN}</code>\n"
        f"🔐 <b>Key:</b> <code>{ULTRA_KEY}</code>",
        parse_mode=ParseMode.HTML,
        reply_markup=get_admin_menu_keyboard()
    )
    await state.clear()

@dp.message(F.text == "Remove Task", StateFilter("*"))
async def admin_btn_remove_task(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.set_state(AdminState.waiting_for_remove_task)
    await message.answer("<tg-emoji emoji-id=\"5262529363710060188\">🗑</tg-emoji> Send the Task ID to remove (e.g. `3`):", parse_mode=ParseMode.MARKDOWN)

@dp.message(AdminState.waiting_for_remove_task, ~F.text.startswith("/"), ~F.text.in_(MENU_BUTTONS))
async def process_remove_task_step(message: Message, state: FSMContext):
    await cleanup_last_menu(message, state)
    try:
        task_id = int(message.text.strip())
        async with db_pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("DELETE FROM task_assignments WHERE task_id=$1", task_id)
                res = await conn.execute("DELETE FROM tasks WHERE id=$1", task_id)

        if res == "DELETE 0":
            await message.answer(f"<tg-emoji emoji-id=\"5262831879731555779\">📭</tg-emoji> Task `#{task_id}` not found.", parse_mode=ParseMode.MARKDOWN, reply_markup=get_admin_menu_keyboard())
        else:
            await message.answer(f"<tg-emoji emoji-id=\"6217663806110175239\">✅</tg-emoji> **Task `#{task_id}` removed successfully!**", parse_mode=ParseMode.MARKDOWN, reply_markup=get_admin_menu_keyboard())
    except ValueError:
        await message.answer("❌ Invalid Task ID.", reply_markup=get_admin_menu_keyboard())
    await state.clear()

@dp.message(Command("mustjoin"), StateFilter("*"))
@dp.message(F.text == "Must Join Channel", StateFilter("*"))
async def set_must_join_command(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.set_state(AdminState.waiting_for_channel_link)
    current = MUST_JOIN_CHANNEL if MUST_JOIN_CHANNEL else "Disabled"
    await message.answer(
        f"<tg-emoji emoji-id=\"5332724926216428039\">📢</tg-emoji> <b>Must Join Channel Settings</b>\n\n"
        f"Currently set to: <code>{current}</code>\n\n"
        f"Send the channel username (e.g. <code>@MyChannel</code>) or link (e.g. <code>https://t.me/MyChannel</code>).\n\n"
        f"<i>Type <code>none</code> to disable forced channel joining.</i>",
        parse_mode=ParseMode.HTML
    )

@dp.message(AdminState.waiting_for_channel_link, ~F.text.startswith("/"), ~F.text.in_(MENU_BUTTONS))
async def process_must_join_channel_step(message: Message, state: FSMContext):
    await cleanup_last_menu(message, state)
    global MUST_JOIN_CHANNEL
    val = message.text.strip()

    if val.lower() == "none":
        MUST_JOIN_CHANNEL = None
        db_val = "off"
        msg = "<tg-emoji emoji-id=\"6217663806110175239\">✅</tg-emoji> <b>Forced channel join disabled.</b>"
    else:
        if "/" in val:
            val = "@" + val.split("/")[-1].replace("@", "")
        elif not val.startswith("@"):
            val = "@" + val

        MUST_JOIN_CHANNEL = val
        db_val = val
        msg = f"<tg-emoji emoji-id=\"6217663806110175239\">✅</tg-emoji> <b>Must join channel updated to:</b> <code>{val}</code>"

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
    await call.message.answer('<tg-emoji emoji-id=\"6291696801636424911\">🏦</tg-emoji> Send your UPI ID below:\n\n<i>Example: username@upi or 9876543210@paytm</i>', parse_mode=ParseMode.HTML)

@dp.callback_query(F.data == "link_usdt")
async def start_link_usdt(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.set_state(UserState.setting_usdt)
    await call.message.answer('<tg-emoji emoji-id=\"5197434882321567830\">🪙</tg-emoji> Send your <b>USDT BEP-20</b> address below:\n\n<i>Example: 0x1234567890abcdef1234567890abcdef12345678</i>', parse_mode=ParseMode.HTML)

@dp.callback_query(F.data == "link_ultra")
async def start_link_ultra(call: CallbackQuery, state: FSMContext):
    if not ULTRA_STATUS:
        await call.answer("❌ Ultra Gateway is currently disabled by Admin!", show_alert=True)
        return
    await call.answer()
    await state.set_state(UserState.setting_ultra)
    await call.message.answer(
        '<tg-emoji emoji-id=\"5195033767969839232\">⚡️</tg-emoji> Send your <b>Ultra Gateway Number</b> below:\n\n'
        '<tg-emoji emoji-id=\"5447410659077661506\">🌐</tg-emoji> <i>Register/Get your Ultra Gateway account here:</i> https://ultra-pay.store', 
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

        withdraw_id = None
        try:
            async with conn.transaction():
                # Atomic decrement guarded by balance >= amount: never overwrites a balance that
                # may have changed since it was first read, and never goes negative.
                new_balance = await conn.fetchval(
                    "UPDATE users SET balance = balance - $2 WHERE user_id=$1 AND balance >= $2 RETURNING balance",
                    user_id, total_deducted
                )
                if new_balance is None:
                    raise ValueError("balance_changed")

                withdraw_id = await conn.fetchval(
                    "INSERT INTO withdrawals(user_id, amount, method, payment_address) VALUES ($1, $2, 'UPI', $3) RETURNING id",
                    user_id, payout_amount, upi
                )
                await conn.execute(
                    "INSERT INTO transactions (user_id, type, amount, note) VALUES ($1, $2, $3, $4)",
                    user_id, "withdrawal_pending", -total_deducted, f"UPI Withdrawal #{withdraw_id} pending (Payout: ₹{payout_amount:.2f}, Fee: ₹{UPI_FEES:.2f})"
                )
        except asyncpg.exceptions.UniqueViolationError:
            # The database-level guard caught a double-submit race that slipped past the check above.
            await call.answer("Your Previous Withdrawal is Already Pending, Please Wait it to be Processed", show_alert=True)
            return
        except ValueError:
            await call.answer("⚠️ Your balance changed just now. Please try again.", show_alert=True)
            return

    await call.answer()
    invalidate_user_cache(user_id)

    kb = InlineKeyboardBuilder()
    kb.button(
        text='💸 Pay', 
        callback_data=f'wp:{withdraw_id}',
        style="success"
    )
    kb.button(
        text='❌ Reject', 
        callback_data=f'wr:{withdraw_id}',
        style="danger"
    )
    kb.adjust(2)
    
    await bot.send_message(
        ADMIN_ID,
        f'<tg-emoji emoji-id=\"5417924076503062111\">💰</tg-emoji> <b>WITHDRAWAL REQUEST #{withdraw_id} (UPI)</b>\n\n'
        f'<tg-emoji emoji-id=\"5870458774455587120\">👤</tg-emoji> @{call.from_user.username}\n'
        f'🆔 <code>{user_id}</code>\n'
        f'<tg-emoji emoji-id=\"5417924076503062111\">💰</tg-emoji> Net Payout: ₹{payout_amount:.2f} (Fee Charged: ₹{UPI_FEES:.2f})\n'
        f'<tg-emoji emoji-id=\"6291696801636424911\">🏦</tg-emoji> UPI: <code>{upi}</code>',
        reply_markup=kb.as_markup(),
        parse_mode=ParseMode.HTML
    )

    payout_display = format_currency(payout_amount, curr)
    fee_display = format_currency(UPI_FEES, curr)
    try:
        await call.message.edit_text(
            f'<tg-emoji emoji-id=\"5201691993775818138\">🚀</tg-emoji> Withdrawal request submitted!\n\n'
            f'<tg-emoji emoji-id=\"5417924076503062111\">💰</tg-emoji> <b>Net Payout:</b> {payout_display}\n'
            f'<tg-emoji emoji-id=\"5350387571199319521\">🏷</tg-emoji> <b>Deducted Fee:</b> {fee_display}\n'
            f'<tg-emoji emoji-id=\"6291696801636424911\">🏦</tg-emoji> <b>UPI ID:</b> <code>{upi}</code>',
            parse_mode=ParseMode.HTML,
            reply_markup=get_back_inline_keyboard()
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

        withdraw_id = None
        try:
            async with conn.transaction():
                new_balance = await conn.fetchval(
                    "UPDATE users SET balance = balance - $2 WHERE user_id=$1 AND balance >= $2 RETURNING balance",
                    user_id, total_deducted
                )
                if new_balance is None:
                    raise ValueError("balance_changed")

                withdraw_id = await conn.fetchval(
                    "INSERT INTO withdrawals(user_id, amount, method, payment_address) VALUES ($1, $2, 'USDT BEP-20', $3) RETURNING id",
                    user_id, payout_amount, usdt
                )
                await conn.execute(
                    "INSERT INTO transactions (user_id, type, amount, note) VALUES ($1, $2, $3, $4)",
                    user_id, "withdrawal_pending", -total_deducted, f"USDT Withdrawal #{withdraw_id} pending (Payout: ₹{payout_amount:.2f}, Fee: ₹{USDT_FEES:.2f})"
                )
        except asyncpg.exceptions.UniqueViolationError:
            await call.answer("Your Previous Withdrawal is Already Pending, Please Wait it to be Processed", show_alert=True)
            return
        except ValueError:
            await call.answer("⚠️ Your balance changed just now. Please try again.", show_alert=True)
            return

    await call.answer()
    invalidate_user_cache(user_id)

    kb = InlineKeyboardBuilder()
    kb.button(
        text='💸 Pay', 
        callback_data=f'wp:{withdraw_id}',
        style="success"
    )
    kb.button(
        text='❌ Reject', 
        callback_data=f'wr:{withdraw_id}',
        style="danger"
    )
    kb.adjust(2)
    
    usdt_amount = payout_amount / USD_TO_INR
    await bot.send_message(
        ADMIN_ID,
        f'<tg-emoji emoji-id=\"5417924076503062111\">💰</tg-emoji> <b>WITHDRAWAL REQUEST #{withdraw_id} (USDT BEP-20)</b>\n\n'
        f'<tg-emoji emoji-id=\"5870458774455587120\">👤</tg-emoji> @{call.from_user.username}\n'
        f'🆔 <code>{user_id}</code>\n'
        f'<tg-emoji emoji-id=\"5417924076503062111\">💰</tg-emoji> Net Payout: ₹{payout_amount:.2f} (~${usdt_amount:.2f} USDT) (Fee Charged: ₹{USDT_FEES:.2f})\n'
        f'<tg-emoji emoji-id=\"5197434882321567830\">🪙</tg-emoji> USDT BEP-20: <code>{usdt}</code>',
        reply_markup=kb.as_markup(),
        parse_mode=ParseMode.HTML
    )

    payout_display = format_currency(payout_amount, curr)
    fee_display = format_currency(USDT_FEES, curr)
    try:
        await call.message.edit_text(
            f'<tg-emoji emoji-id=\"5201691993775818138\">🚀</tg-emoji> Withdrawal request submitted!\n\n'
            f'<tg-emoji emoji-id=\"5417924076503062111\">💰</tg-emoji> <b>Net Payout:</b> {payout_display} (~${usdt_amount:.2f} USDT)\n'
            f'<tg-emoji emoji-id=\"5350387571199319521\">🏷</tg-emoji> <b>Deducted Fee:</b> {fee_display}\n'
            f'<tg-emoji emoji-id=\"5197434882321567830\">🪙</tg-emoji> <b>USDT Address:</b> <code>{usdt}</code>',
            parse_mode=ParseMode.HTML,
            reply_markup=get_back_inline_keyboard()
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

    # Reserve (atomically deduct) the balance BEFORE calling the external payment API.
    # This closes the double-submit race: a second rapid tap will find insufficient
    # balance here and stop, instead of both taps calling the real payment API.
    async with db_pool.acquire() as conn:
        new_balance = await conn.fetchval(
            "UPDATE users SET balance = balance - $2 WHERE user_id=$1 AND balance >= $2 RETURNING balance",
            user_id, bal
        )
    if new_balance is None:
        await call.answer("⚠️ Your balance changed just now. Please try again.", show_alert=True)
        return
    invalidate_user_cache(user_id)

    url = f"https://ultra-pay.store/APIs/api?token={urllib.parse.quote(ULTRA_TOKEN)}&key={urllib.parse.quote(ULTRA_KEY)}&paytoNumber={urllib.parse.quote(ultra_num)}&amount={payout_amount:.2f}&comment=iGmail Pay"

    await call.answer("⚡ Processing instant payment via Ultra Gateway...", show_alert=False)

    api_success = False
    api_reason = "Unknown Error"

    try:
        session = HTTP_SESSION if HTTP_SESSION and not HTTP_SESSION.closed else aiohttp.ClientSession()
        _own_session = session is not HTTP_SESSION
        try:
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
        finally:
            if _own_session:
                await session.close()
    except Exception as e:
        api_reason = f"Connection error: {e}"

    if api_success:
        async with db_pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("INSERT INTO withdrawals(user_id, amount, method, payment_address, status) VALUES ($1, $2, 'Ultra Gateway', $3, 'paid')", user_id, payout_amount, ultra_num)
                await conn.execute("INSERT INTO transactions (user_id, type, amount, note) VALUES ($1, $2, $3, $4)", user_id, "withdrawal", -bal, "Ultra Gateway instant payout paid")

        bal_display = format_currency(payout_amount, curr)
        msg_text = (
            f"<tg-emoji emoji-id=\"5461151367559141950\">🎉</tg-emoji> <b>Instant Payment Successful!</b>\n\n"
            f"<tg-emoji emoji-id=\"5195033767969839232\">⚡️</tg-emoji> <b>Method:</b> Ultra Gateway\n"
            f"📱 <b>Number:</b> <code>{ultra_num}</code>\n"
            f"<tg-emoji emoji-id=\"5417924076503062111\">💰</tg-emoji> <b>Amount Transferred:</b> {bal_display}\n\n"
            f"<tg-emoji emoji-id=\"5447410659077661506\">🌐</tg-emoji> <i>Gateway:</i> https://ultra-pay.store"
        )
        try:
            await call.message.edit_text(msg_text, parse_mode=ParseMode.HTML, reply_markup=get_back_inline_keyboard())
        except TelegramBadRequest as e:
            if "message is not modified" not in str(e):
                await call.message.answer(msg_text, parse_mode=ParseMode.HTML, reply_markup=get_back_inline_keyboard())
    else:
        # Payment failed - refund the balance that was reserved before the API call.
        async with db_pool.acquire() as conn:
            await conn.execute("UPDATE users SET balance = balance + $1 WHERE user_id=$2", bal, user_id)
        invalidate_user_cache(user_id)

        fail_msg = (
            f"<tg-emoji emoji-id=\"5274099962655816924\">❌</tg-emoji> <b>Ultra Gateway Instant Payment Failed!</b>\n\n"
            f"💬 <b>Reason:</b> <code>{api_reason}</code>\n\n"
            f"<i>Your balance was not deducted. Please check your Ultra Gateway number or try again later.</i>\n"
            f"<tg-emoji emoji-id=\"5447410659077661506\">🌐</tg-emoji> <i>Gateway link:</i> https://ultra-pay.store"
        )
        try:
            await call.message.edit_text(fail_msg, parse_mode=ParseMode.HTML, reply_markup=get_back_inline_keyboard())
        except TelegramBadRequest as e:
            if "message is not modified" not in str(e):
                await call.message.answer(fail_msg, parse_mode=ParseMode.HTML, reply_markup=get_back_inline_keyboard())

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
    await call.message.answer('✔️ Send screenshot or proof of completed task.', parse_mode=ParseMode.HTML)

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
        await call.message.edit_text(
            f'<tg-emoji emoji-id=\"6217663806110175239\">✅</tg-emoji> Task #{task_id} has been cancelled and returned to the pool.',
            parse_mode=ParseMode.HTML,
            reply_markup=get_back_inline_keyboard()
        )
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            await call.message.answer(
                f'<tg-emoji emoji-id=\"6217663806110175239\">✅</tg-emoji> Task #{task_id} has been cancelled and returned to the pool.',
                parse_mode=ParseMode.HTML,
                reply_markup=get_back_inline_keyboard()
            )

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
            f"<tg-emoji emoji-id=\"5274099962655816924\">❌</tg-emoji> <b>This Gmail account (<code>{email}</code>) does not exist on Google!</b>\n\n"
            f"Please create <code>{email}</code> first on Google, then submit your proof again.",
            parse_mode=ParseMode.HTML
        )
        return

    # Only remove the task card now that validation has actually succeeded
    await cleanup_last_menu(message, state)

    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE tasks SET status='pending_review' WHERE id=$1", task_id)

    admin_kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text='Approve', icon_custom_emoji_id="6217663806110175239", callback_data=f'ta:{task_id}', style="success"),
        InlineKeyboardButton(text='Decline', icon_custom_emoji_id="5274099962655816924", callback_data=f'td:{task_id}', style="danger")
    ]])

    admin_msg_text = (
        f'<tg-emoji emoji-id=\"5305265301917549162\">📤</tg-emoji> <b>Task Submission #{task_id}</b>\n\n'
        f'<tg-emoji emoji-id=\"5870458774455587120\">👤</tg-emoji> <b>User:</b> @{message.from_user.username} (<code>{user_id}</code>)\n\n'
        f'<tg-emoji emoji-id=\"5253742260054409879\">📧</tg-emoji> <b>Email:</b>\n<code>{email}</code>\n\n'
        f'<tg-emoji emoji-id=\"6005570495603282482\">🔑</tg-emoji> <b>Password:</b>\n<code>{password}</code>\n\n'
        f'<tg-emoji emoji-id=\"5417924076503062111\">💰</tg-emoji> <b>Reward:</b> ₹{reward}'
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
                        InlineKeyboardButton(text="Approve", icon_custom_emoji_id="6217663806110175239", callback_data=f"w_ta:{task_id}", style="success"),
                        InlineKeyboardButton(text="Decline", icon_custom_emoji_id="5274099962655816924", callback_data=f"w_td:{task_id}", style="danger")
                    ]])
                    worker_msg_text = (
                        f'<tg-emoji emoji-id=\"5305265301917549162\">📤</tg-emoji> <b>New Task Submission #{task_id}</b>\n\n'
                        f'<tg-emoji emoji-id=\"5253742260054409879\">📧</tg-emoji> <b>Email:</b>\n<code>{email}</code>\n\n'
                        f'<tg-emoji emoji-id=\"6005570495603282482\">🔑</tg-emoji> <b>Password:</b>\n<code>{password}</code>'
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
        f'✔️ Task #{task_id} submission sent for review.\n\n'
        f'<tg-emoji emoji-id=\"5420323339723881652\">⚠️</tg-emoji> <b>Important:</b> Please make sure to <b>logout</b> of this account from your device!', 
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
        async with conn.transaction():
            # Atomically claim the row: the UPDATE only affects a row still in pending_review,
            # so a duplicate/resent callback that arrives a moment later finds 0 rows and bails out
            # instead of crediting the balance twice.
            sell_data = await conn.fetchrow(
                "UPDATE pending_sells SET status='approved' WHERE id=$1 AND status='pending_review' RETURNING user_id, amount",
                sell_id
            )
            if not sell_data:
                await call.answer("⚠️ This request is already processed!", show_alert=True)
                return

            await call.answer()
            user_id = sell_data['user_id']
            amount = sell_data['amount']

            await ensure_user(user_id, conn=conn)

            await conn.execute("UPDATE users SET balance = balance + $1 WHERE user_id=$2", amount, user_id)
            await conn.execute("INSERT INTO transactions (user_id, type, amount, note) VALUES ($1, $2, $3, $4)", user_id, "sell", amount, f"Gmail sell #{sell_id} approved")

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
                    f'🎉 Your referral <code>{user_id}</code> sell gmail got approved and <b>{ref_amt_str}</b> credited to your balance!'
                )
                await send_user_notification(referred_by, notif_text, parse_mode=ParseMode.HTML)
            
            asyncio.create_task(notify_ref())

    invalidate_user_cache(user_id)
    await edit_admin_message(call, '<tg-emoji emoji-id=\"6217663806110175239\">✅</tg-emoji> Sell Request Approved')
    
    async def notify_user():
        user_data = await get_user_data(user_id)
        formatted_amt = format_currency(amount, user_data['currency'])
        await send_user_notification(user_id, f"<tg-emoji emoji-id=\"5461151367559141950\">🎉</tg-emoji> Your Gmail sell request #{sell_id} was approved!\n+{formatted_amt} added to your balance.")

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
    await call.message.answer('<tg-emoji emoji-id=\"5420323339723881652\">⚠️</tg-emoji> <b>Please reply with the reason for declining this sell request:</b>', parse_mode=ParseMode.HTML)

@dp.message(AdminState.waiting_for_sell_reject_reason, ~F.text.startswith("/"), ~F.text.in_(MENU_BUTTONS))
async def process_sell_reject_reason(message: Message, state: FSMContext):
    await cleanup_last_menu(message, state)
    data = await state.get_data()
    sell_id = data.get('sell_id')
    user_id = data['user_id']
    admin_msg_id = data['admin_msg_id']
    is_photo = data['is_photo']
    reason = message.text.strip()

    if sell_id:
        async with db_pool.acquire() as conn:
            await conn.execute("UPDATE pending_sells SET status='declined' WHERE id=$1", sell_id)

    new_text = f'<tg-emoji emoji-id=\"5420323339723881652\">⚠️</tg-emoji> <b>Sell request declined.</b>\n<b>Reason:</b> {reason}'
    try:
        if is_photo:
            await bot.edit_message_caption(chat_id=message.chat.id, message_id=admin_msg_id, caption=new_text, reply_markup=None, parse_mode=ParseMode.HTML)
        else:
            await bot.edit_message_text(chat_id=message.chat.id, message_id=admin_msg_id, text=new_text, reply_markup=None, parse_mode=ParseMode.HTML)
    except Exception as e:
        print(f"Error editing admin msg: {e}")

    asyncio.create_task(send_user_notification(
        user_id, 
        f'<tg-emoji emoji-id=\"5420323339723881652\">⚠️</tg-emoji> <b>Your sell request #{sell_id} was declined.</b>\n\n💬 <b>Reason:</b> {reason}', 
        parse_mode=ParseMode.HTML
    ))

    await message.answer('<tg-emoji emoji-id=\"6217663806110175239\">✅</tg-emoji> Rejection reason sent to user.', parse_mode=ParseMode.HTML)
    await state.clear()

# ============================================
# TASK APPROVE & DECLINE HANDLERS
# ============================================

@dp.callback_query(F.data.startswith("ta:"))
async def approve_task(call: CallbackQuery):
    task_id = int(call.data.split(":")[1])
    
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            # Atomically claim the row (only affects a row still in pending_review), closing the
            # race where a resent/duplicate callback could otherwise credit the reward twice.
            task_data = await conn.fetchrow(
                "UPDATE tasks SET status='completed' WHERE id=$1 AND status='pending_review' RETURNING reward, details",
                task_id
            )
            if not task_data:
                await call.answer("⚠️ This request is already processed!", show_alert=True)
                return

            reward = task_data['reward']
            assigned_user_id = await conn.fetchval("SELECT user_id FROM task_assignments WHERE task_id=$1", task_id)
            if not assigned_user_id:
                # Data inconsistency: no assignment row for this task. Revert the status change
                # instead of silently leaving it stuck, and surface this to the admin so it can be checked.
                await conn.execute("UPDATE tasks SET status='pending_review' WHERE id=$1", task_id)
                await call.answer("⚠️ Error: No assigned user found for this task. It has been left in Pending Review — please check it manually.", show_alert=True)
                return

            await call.answer()
            user_id = assigned_user_id
            try:
                task_email = task_data['details'].split(" | ")[0].replace("Email: ", "").strip()
            except Exception:
                task_email = "Task Account"

            await ensure_user(user_id, conn=conn)

            await conn.execute("UPDATE users SET balance = balance + $1 WHERE user_id=$2", reward, user_id)
            await conn.execute("INSERT INTO transactions (user_id, type, amount, note) VALUES ($1, $2, $3, $4)", user_id, "task", reward, f"{task_email} #{task_id}")
            await conn.execute("DELETE FROM task_assignments WHERE task_id=$1", task_id)

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
                    f'🎉 Your referral <code>{user_id}</code> task gmail got approved and <b>{ref_amt_str}</b> credited to your balance!'
                )
                await send_user_notification(referred_by, notif_text, parse_mode=ParseMode.HTML)

            asyncio.create_task(notify_ref())
            
    invalidate_user_cache(user_id)
    await edit_admin_message(call, '<tg-emoji emoji-id=\"6217663806110175239\">✅</tg-emoji> Task Approved')

    async def notify_user():
        user_data = await get_user_data(user_id)
        formatted_reward = format_currency(reward, user_data['currency'])
        await send_user_notification(user_id, f"<tg-emoji emoji-id=\"5461151367559141950\">🎉</tg-emoji> Task #{task_id} approved!\n+{formatted_reward} added to your balance.")

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
    await call.message.answer(f'<tg-emoji emoji-id=\"5420323339723881652\">⚠️</tg-emoji> <b>Please reply with the reason for declining Task #{task_id}:</b>', parse_mode=ParseMode.HTML)

@dp.message(AdminState.waiting_for_task_reject_reason, ~F.text.startswith("/"), ~F.text.in_(MENU_BUTTONS))
async def process_task_reject_reason(message: Message, state: FSMContext):
    await cleanup_last_menu(message, state)
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

    new_text = f'<tg-emoji emoji-id=\"5420323339723881652\">⚠️</tg-emoji> <b>Task #{task_id} declined.</b>\n<b>Reason:</b> {reason}'
    try:
        if is_photo:
            await bot.edit_message_caption(chat_id=message.chat.id, message_id=admin_msg_id, caption=new_text, reply_markup=None, parse_mode=ParseMode.HTML)
        else:
            await bot.edit_message_text(chat_id=message.chat.id, message_id=admin_msg_id, text=new_text, reply_markup=None, parse_mode=ParseMode.HTML)
    except Exception as e:
        print(f"Error editing admin msg: {e}")

    asyncio.create_task(send_user_notification(
        user_id, 
        f'<tg-emoji emoji-id=\"5420323339723881652\">⚠️</tg-emoji> <b>Your submission for Task #{task_id} was declined.</b>\n\n💬 <b>Reason:</b> {reason}\n\n🛡 The task has been returned to the pool.', 
        parse_mode=ParseMode.HTML
    ))

    await message.answer('<tg-emoji emoji-id=\"6217663806110175239\">✅</tg-emoji> Rejection reason recorded and user notified.', parse_mode=ParseMode.HTML)
    await state.clear()

# ============================================
# WITHDRAWAL CALLBACKS (ADMIN SIDE)
# ============================================

@dp.callback_query(F.data.startswith("wp:"))
async def pay_withdraw(call: CallbackQuery):
    withdrawal_id = int(call.data.split(":")[1])

    async with db_pool.acquire() as conn:
        async with conn.transaction():
            w_data = await conn.fetchrow(
                "UPDATE withdrawals SET status='paid' WHERE id=$1 AND status='pending' RETURNING user_id, amount",
                withdrawal_id
            )
            if not w_data:
                await call.answer("⚠️ This request is already processed!", show_alert=True)
                return

            await call.answer()
            user_id = w_data['user_id']
            payout_amount = w_data['amount']

            await conn.execute(
                "UPDATE transactions SET type='withdrawal', note=$1 WHERE user_id=$2 AND note LIKE $3",
                "Withdrawal paid", user_id, f"%Withdrawal #{withdrawal_id}%"
            )

    invalidate_user_cache(user_id)
    await edit_admin_message(call, '<tg-emoji emoji-id=\"6217663806110175239\">✅</tg-emoji> Withdrawal Paid')

    async def notify_user():
        user_data = await get_user_data(user_id)
        formatted_amt = format_currency(payout_amount, user_data['currency'])
        await send_user_notification(user_id, f"<tg-emoji emoji-id=\"5461151367559141950\">🎉</tg-emoji> Your withdrawal request of {formatted_amt} has been approved and paid!")

    asyncio.create_task(notify_user())

@dp.callback_query(F.data.startswith("wr:"))
async def reject_withdraw(call: CallbackQuery):
    withdrawal_id = int(call.data.split(":")[1])
    
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            w_data = await conn.fetchrow(
                "UPDATE withdrawals SET status='rejected' WHERE id=$1 AND status='pending' RETURNING user_id, amount, method",
                withdrawal_id
            )
            if not w_data:
                await call.answer("⚠️ This request is already processed!", show_alert=True)
                return

            await call.answer()
            user_id = w_data['user_id']
            payout_amount = w_data['amount']
            method = (w_data['method'] or 'UPI').lower()

            fee = UPI_FEES if 'upi' in method else (USDT_FEES if 'usdt' in method else ULTRA_FEES)
            refund_total = payout_amount + fee

            await conn.execute("UPDATE users SET balance = balance + $1 WHERE user_id=$2", refund_total, user_id)
            # Keep the original ledger entry (relabelled) instead of deleting it, so the
            # transaction history still shows the withdrawal that triggered this refund.
            await conn.execute(
                "UPDATE transactions SET type='withdrawal_rejected', note = note || ' [REJECTED]' WHERE user_id=$1 AND note LIKE $2",
                user_id, f"%Withdrawal #{withdrawal_id}%"
            )
            await conn.execute(
                "INSERT INTO transactions (user_id, type, amount, note) VALUES ($1, $2, $3, $4)",
                user_id, "refund", refund_total, f"Refund for rejected withdrawal #{withdrawal_id}"
            )

    invalidate_user_cache(user_id)
    await edit_admin_message(call, '<tg-emoji emoji-id=\"5420323339723881652\">⚠️</tg-emoji> Withdrawal Rejected (Balance Refunded)')
    
    async def notify_user_refund():
        user_data = await get_user_data(user_id)
        formatted_amt = format_currency(refund_total, user_data['currency'])
        await send_user_notification(
            user_id, 
            f'<tg-emoji emoji-id=\"5420323339723881652\">⚠️</tg-emoji> Your withdrawal request #{withdrawal_id} was rejected.\n'
            f'<tg-emoji emoji-id=\"5417924076503062111\">💰</tg-emoji> <b>{formatted_amt}</b> has been refunded back to your balance.', 
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
                    SELECT ta.task_id, ta.user_id, ta.assigned_at, ta.message_id 
                    FROM task_assignments ta
                    JOIN tasks t ON ta.task_id = t.id
                    WHERE t.status = 'assigned'
                ''')
                
                now = datetime.utcnow()
                for r in rows_30m:
                    if now - r['assigned_at'] > timedelta(minutes=30):
                        expired_30m.append((r['task_id'], r['user_id'], r['message_id']))

                if expired_30m:
                    task_ids_30m = [t[0] for t in expired_30m]
                    async with conn.transaction():
                        await conn.execute('DELETE FROM task_assignments WHERE task_id = ANY($1::int[])', task_ids_30m)
                        await conn.execute("UPDATE tasks SET status='available' WHERE id = ANY($1::int[])", task_ids_30m)

            for task_id, user_id, msg_id in expired_30m:
                if msg_id:
                    try:
                        await bot.delete_message(chat_id=user_id, message_id=msg_id)
                    except Exception:
                        pass
                asyncio.create_task(send_user_notification(
                    user_id, 
                    f'<tg-emoji emoji-id=\"5201691993775818138\">🚀</tg-emoji> Task #{task_id} time limit expired (30 mins).\nThe task was returned to the pool.', 
                    reply_markup=get_main_menu_keyboard(), 
                    parse_mode=ParseMode.HTML
                ))

            expired_lifetime_tasks = []
            async with db_pool.acquire() as conn:
                rows_lifetime = await conn.fetch('''
                    SELECT t.id, t.title, t.details, t.created_at, ta.user_id, ta.message_id 
                    FROM tasks t
                    LEFT JOIN task_assignments ta ON t.id = ta.task_id
                    WHERE t.status NOT IN ('completed', 'pending_review')
                ''')

                now = datetime.utcnow()
                for r in rows_lifetime:
                    created_at = r['created_at'] or now
                    if now - created_at > timedelta(hours=23, minutes=30):
                        expired_lifetime_tasks.append({
                            'id': r['id'],
                            'details': r['details'],
                            'user_id': r['user_id'],
                            'message_id': r['message_id']
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
                    msg_id = item.get('message_id')
                    if msg_id:
                        try:
                            await bot.delete_message(chat_id=assigned_u, message_id=msg_id)
                        except Exception:
                            pass

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

async def on_startup(app: web.Application):
    await bot.set_webhook(
        WEBHOOK_URL,
        secret_token=WEBHOOK_SECRET,
        drop_pending_updates=True,
        allowed_updates=dp.resolve_used_update_types()
    )
    print(f'🤖 Webhook set: {WEBHOOK_URL}')

async def on_shutdown(app: web.Application):
    if HTTP_SESSION:
        await HTTP_SESSION.close()

async def main():
    global HTTP_SESSION
    await init_db()
    await load_settings_and_cache()
    HTTP_SESSION = aiohttp.ClientSession()
    asyncio.create_task(auto_expire_tasks())

    port = int(os.environ.get("PORT", 8080))

    if WEBHOOK_URL:
        # FAST PATH: Telegram pushes updates to us directly - removes the getUpdates round-trip
        app = web.Application()
        app.router.add_get('/', health)
        register_webapp_routes(app)
        SimpleRequestHandler(
            dispatcher=dp,
            bot=bot,
            secret_token=WEBHOOK_SECRET
        ).register(app, path=WEBHOOK_PATH)
        setup_application(app, dp, bot=bot)
        app.on_startup.append(on_startup)
        app.on_shutdown.append(on_shutdown)

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, host='0.0.0.0', port=port)
        await site.start()
        print(f'🤖 Bot connected to Supabase PostgreSQL and running via WEBHOOK on Render (port {port})...')
        while True:
            await asyncio.sleep(3600)
    else:
        # FALLBACK: WEBHOOK_URL not set -> behaves exactly like the original long-polling bot
        app = web.Application()
        app.router.add_get('/', health)
        register_webapp_routes(app)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, host='0.0.0.0', port=port)
        await site.start()

        print('🤖 Bot connected to Supabase PostgreSQL and polling 24/7 on Render...')
        try:
            await dp.start_polling(bot)
        finally:
            if HTTP_SESSION:
                await HTTP_SESSION.close()

if __name__ == '__main__':
    asyncio.run(main())
