import asyncio
from datetime import datetime, timedelta
import os
from threading import Thread
from flask import Flask

import asyncpg
from aiogram import Bot, Dispatcher, F
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject, ChatMemberUpdatedFilter, IS_MEMBER, IS_NOT_MEMBER, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ChatMemberUpdated
)
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder

# ============================================
# CONFIGURATION & INITIALIZATION
# ============================================

BOT_TOKEN = os.environ.get('BOT_TOKEN', '8970788656:AAGmGCBKEAhNSpaW0YTv7zztcLPTTQwYRGo')
ADMIN_ID = int(os.environ.get('ADMIN_ID', 6237763207))
DATABASE_URL = os.environ.get('DATABASE_URL')

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

db_pool = None
BANNED_USERS_CACHE = set()
MUST_JOIN_CHANNEL = None

# List of all menu buttons to prevent state bleeding
MENU_BUTTONS = {
    "✍️ Get Task", "💰 Balance", "📨 Sell Gmail", "📜 History", "🛠 Support", "🚫 Cancel", "🏠 Main Menu",
    "➕ Add Task", "📥 Pending Reviews", "💬 Chat", "🗑 Unassign Tasks", "🔍 Find ID", "➕ Add Balance", 
    "➖ Cut Balance", "🔎 Check Balance", "🏆 Top Balances", "🚫 Ban User", "✅ Unban User",
    "📢 Broadcast", "🏷 Update All Rewards", "🗑 Remove Task", "💳 Transactions", "📊 View Stats",
    "📢 Must Join Channel"
}

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
        
    db_pool = await asyncpg.create_pool(dsn=url, ssl='require')
    
    async with db_pool.acquire() as conn:
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY, 
                balance DOUBLE PRECISION DEFAULT 0,
                upi TEXT DEFAULT 'None'
            )
        ''')
        await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS upi TEXT DEFAULT 'None'")

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
                upi TEXT, 
                status TEXT DEFAULT 'pending', 
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
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
    global BANNED_USERS_CACHE, MUST_JOIN_CHANNEL
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT user_id FROM banned_users")
        BANNED_USERS_CACHE = {r['user_id'] for r in rows}
        
        channel_val = await conn.fetchval("SELECT value FROM bot_settings WHERE key='must_join_channel'")
        MUST_JOIN_CHANNEL = channel_val if channel_val else None

# ============================================
# HELPERS & KEYBOARDS
# ============================================

async def ensure_user(user_id: int):
    async with db_pool.acquire() as conn:
        await conn.execute("INSERT INTO users (user_id, balance, upi) VALUES ($1, 0, 'None') ON CONFLICT (user_id) DO NOTHING", user_id)

async def get_user_data(user_id: int):
    await ensure_user(user_id)
    async with db_pool.acquire() as conn:
        return await conn.fetchrow("SELECT balance, upi FROM users WHERE user_id=$1", user_id)

async def get_balance(user_id: int) -> float:
    data = await get_user_data(user_id)
    return data['balance'] if data else 0.0

async def is_banned(user_id: int) -> bool:
    return user_id in BANNED_USERS_CACHE

async def check_user_joined_channel(user_id: int) -> bool:
    if not MUST_JOIN_CHANNEL:
        return True
    try:
        member = await bot.get_chat_member(chat_id=MUST_JOIN_CHANNEL, user_id=user_id)
        return member.status in ['creator', 'administrator', 'member']
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
        text="Support",
        callback_data="menu_support",
        icon_custom_emoji_id="5274099962655816924",
        style="danger"
    )
    kb.adjust(2, 2, 1)
    return kb.as_markup()

def get_admin_menu_keyboard():
    kb = ReplyKeyboardBuilder()
    kb.button(text="➕ Add Task", style="success")
    kb.button(text="📥 Pending Reviews", style="primary")
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
    kb.button(text="🏠 Main Menu", style="primary")
    kb.adjust(2, 3, 2, 2, 2, 2, 2, 2, 2, 1)
    return kb.as_markup(resize_keyboard=True)

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

def get_balance_inline_keyboard(upi_set: bool):
    kb = InlineKeyboardBuilder()
    link_text = "Change UPI" if upi_set else "Link UPI"
    
    kb.button(
        text=f"{link_text}", 
        callback_data="link_upi", 
        icon_custom_emoji_id="5364109867156001787",
        style="primary"
    )
    kb.button(
        text="Withdraw", 
        callback_data="inline_withdraw", 
        icon_custom_emoji_id="5444856076954520455",
        style="success"
    )
    kb.button(
        text="Back",
        callback_data="menu_back",
        icon_custom_emoji_id="6039539366177541657"
    )
    kb.adjust(2, 1)
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
# GLOBAL BAN & MUST-JOIN MIDDLEWARES
# ============================================

@dp.message.outer_middleware()
async def global_message_middleware(handler, event: Message, data):
    if not event.from_user:
        return await handler(event, data)

    user_id = event.from_user.id

    if user_id == ADMIN_ID:
        return await handler(event, data)
        
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
        
    if await is_banned(user_id):
        await event.answer("🚫 You are banned from using this bot.", show_alert=True)
        return

    if event.data == "check_must_join":
        return await handler(event, data)

    if MUST_JOIN_CHANNEL and not await check_user_joined_channel(user_id):
        await event.answer("⚠️ You must join our channel first to use the bot!", show_alert=True)
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
        await call.answer("❌ You haven't joined the channel yet! Please join and try again.", show_alert=True)

@dp.chat_member(ChatMemberUpdatedFilter(IS_MEMBER >> IS_NOT_MEMBER))
async def user_left_channel(event: ChatMemberUpdated):
    user_id = event.from_user.id
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
# START & GLOBAL CANCEL (HIGHEST PRIORITY)
# ============================================

@dp.message(Command("start"), StateFilter("*"))
async def start(message: Message, state: FSMContext):
    data = await state.get_data()
    last_msg_id = data.get("last_menu_msg_id")
    if last_msg_id:
        try:
            await bot.delete_message(chat_id=message.chat.id, message_id=last_msg_id)
        except Exception:
            pass

    await state.clear()
    await ensure_user(message.from_user.id)
    
    text = (
        '<tg-emoji emoji-id="5458904472598095631">👋</tg-emoji> <b>Welcome back.</b>\n\n'
        'Choose an option from the menu below:'
    )
    
    sent_msg = await message.answer(text, parse_mode=ParseMode.HTML, reply_markup=get_main_menu_keyboard())
    await state.update_data(last_menu_msg_id=sent_msg.message_id)

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
    await call.answer()

@dp.callback_query(F.data == "menu_get_task")
async def cb_get_task(call: CallbackQuery, state: FSMContext):
    await state.clear()
    user_id = call.from_user.id
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
                await call.answer()
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

                txt = (
                    f'<tg-emoji emoji-id="5447644880824181073">⚠️</tg-emoji> <b>You already have an active task.</b>\n\n'
                    f'<tg-emoji emoji-id="5310278924616356636">🎯</tg-emoji> <b>Your Current Task</b>\n\n'
                    f'<tg-emoji emoji-id="5197269100878907942">✍️</tg-emoji> #{task_id}\n'
                    f'<tg-emoji emoji-id="5870458774455587120">👤</tg-emoji> <b>Email:</b> {username} | <tg-emoji emoji-id="6005570495603282482">🔑</tg-emoji> <b>Password:</b> <code>{password}</code>\n'
                    f'<tg-emoji emoji-id="5417924076503062111">💰</tg-emoji> <b>Reward:</b> ₹{existing["reward"]}\n\n'
                    f'<tg-emoji emoji-id="5195033767969839232">🚀</tg-emoji> Time Remaining: {mins}m {secs}s'
                )
                try:
                    await call.message.edit_text(txt, parse_mode=ParseMode.HTML, reply_markup=get_task_action_keyboard())
                except:
                    await call.message.answer(txt, parse_mode=ParseMode.HTML, reply_markup=get_task_action_keyboard())
                await state.update_data(last_menu_msg_id=call.message.message_id)
                await call.answer()
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
            await call.answer()
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

    txt = (
        f'<tg-emoji emoji-id="5310278924616356636">🎯</tg-emoji> <b>Task #{task_id}</b>\n\n'
        f'<tg-emoji emoji-id="5870458774455587120">👤</tg-emoji> <b>Email:</b> {username} | <tg-emoji emoji-id="6005570495603282482">🔑</tg-emoji> <b>Password:</b> <code>{password}</code>\n'
        f'<tg-emoji emoji-id="5417924076503062111">💰</tg-emoji> <b>Reward:</b> ₹{reward}\n\n'
        f'<tg-emoji emoji-id="5195033767969839232">🚀</tg-emoji> You have ONLY 30 MINUTES to complete this task.'
    )
    try:
        await call.message.edit_text(txt, parse_mode=ParseMode.HTML, reply_markup=get_task_action_keyboard())
    except:
        await call.message.answer(txt, parse_mode=ParseMode.HTML, reply_markup=get_task_action_keyboard())
    await state.update_data(last_menu_msg_id=call.message.message_id)
    await call.answer()

@dp.callback_query(F.data == "menu_balance")
async def cb_balance(call: CallbackQuery, state: FSMContext):
    await state.clear()
    user_data = await get_user_data(call.from_user.id)
    bal = user_data['balance'] if user_data else 0.0
    upi = user_data['upi'] if user_data and user_data['upi'] else "None"
    upi_set = upi != "None" and upi != ""
    
    text = (
        f'<tg-emoji emoji-id="5445353829304387411">💳</tg-emoji> <b>Balance</b>\n\n'
        f'<tg-emoji emoji-id="5278467510604160626">💵</tg-emoji><b>Available:</b> ₹{bal:.2f}\n'
        f'<tg-emoji emoji-id="6278557702109013266">🏦</tg-emoji><b>UPI:</b> <code>{upi}</code>'
    )
    
    try:
        await call.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=get_balance_inline_keyboard(upi_set))
    except Exception:
        await call.message.answer(text, parse_mode=ParseMode.HTML, reply_markup=get_balance_inline_keyboard(upi_set))
    await state.update_data(last_menu_msg_id=call.message.message_id)
    await call.answer()

@dp.callback_query(F.data == "menu_sell_gmail")
async def cb_sell_gmail(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(UserState.selling_username)
    txt = (
        '<tg-emoji emoji-id="5445221832074483553">🏷️</tg-emoji> <b>Sell Price 30₹/Gmail</b>\n\n'
        '<tg-emoji emoji-id="5377548235709619284">🤑</tg-emoji> <b>Step 1/2:</b> Please send the Gmail <b>Username</b> (e.g., <code>example@gmail.com</code>):'
    )
    try:
        await call.message.edit_text(txt, parse_mode=ParseMode.HTML, reply_markup=get_back_inline_keyboard())
    except:
        await call.message.answer(txt, parse_mode=ParseMode.HTML, reply_markup=get_back_inline_keyboard())
    await state.update_data(last_menu_msg_id=call.message.message_id)
    await call.answer()

@dp.callback_query(F.data == "menu_history")
async def cb_history(call: CallbackQuery, state: FSMContext):
    await state.clear()
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT type, amount, note, created_at FROM transactions WHERE user_id=$1 ORDER BY id DESC LIMIT 10", call.from_user.id)
    if not rows:
        txt = "📭 No transactions found."
        try:
            await call.message.edit_text(txt, reply_markup=get_back_inline_keyboard())
        except:
            await call.message.answer(txt, reply_markup=get_back_inline_keyboard())
        await state.update_data(last_menu_msg_id=call.message.message_id)
        await call.answer()
        return
    text = '<tg-emoji emoji-id="5440410042773824003">📜</tg-emoji> <b>Last Transactions</b>\n\n'
    for r in rows:
        sign = "+" if r['amount'] >= 0 else ""
        text += f"• {sign}₹{r['amount']:.2f} | {r['type']}\n{r['note']}\n{r['created_at'].strftime('%Y-%m-%d %H:%M:%S')}\n\n"
    
    try:
        await call.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=get_back_inline_keyboard())
    except:
        await call.message.answer(text, parse_mode=ParseMode.HTML, reply_markup=get_back_inline_keyboard())
    await state.update_data(last_menu_msg_id=call.message.message_id)
    await call.answer()

@dp.callback_query(F.data == "menu_support")
async def cb_support(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(UserState.waiting_for_support)
    txt = (
        "🛠 <b>Customer Support</b>\n\n"
        "Please send your help message or describe your issue below. Our admin team will look into it shortly."
    )
    try:
        await call.message.edit_text(txt, parse_mode=ParseMode.HTML, reply_markup=get_support_cancel_keyboard())
    except:
        await call.message.answer(txt, parse_mode=ParseMode.HTML, reply_markup=get_support_cancel_keyboard())
    await state.update_data(last_menu_msg_id=call.message.message_id)
    await call.answer()

# ============================================
# SUPPORT SYSTEM
# ============================================

@dp.message(F.text == "🛠 Support", StateFilter("*"))
async def support_button_handler(message: Message, state: FSMContext):
    await state.clear()
    await state.set_state(UserState.waiting_for_support)
    sent_msg = await message.answer(
        "🛠 <b>Customer Support</b>\n\n"
        "Please send your help message or describe your issue below. Our admin team will look into it shortly.",
        parse_mode=ParseMode.HTML,
        reply_markup=get_support_cancel_keyboard()
    )
    await state.update_data(last_menu_msg_id=sent_msg.message_id)

@dp.callback_query(F.data == "cancel_support")
async def cancel_support_callback(call: CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        await call.message.edit_text("❌ Support request cancelled.", reply_markup=None)
    except:
        pass
    sent_msg = await call.message.answer("🏠 Returned to Main Menu.", reply_markup=get_main_menu_keyboard())
    await state.update_data(last_menu_msg_id=sent_msg.message_id)
    try:
        await call.answer()
    except:
        pass

@dp.message(UserState.waiting_for_support, F.text, ~F.text.startswith("/"), ~F.text.in_(MENU_BUTTONS))
async def process_user_support_message(message: Message, state: FSMContext):
    user_id = message.from_user.id
    username = f"@{message.from_user.username}" if message.from_user.username else "No Username"
    user_msg = message.text.strip()

    admin_text = (
        f"🛠 <b>New Support Request</b>\n\n"
        f"👤 <b>User:</b> {username}\n"
        f"🆔 <b>User ID:</b> <code>{user_id}</code>\n\n"
        f"💬 <b>Message:</b>\n{user_msg}"
    )

    try:
        await bot.send_message(ADMIN_ID, admin_text, parse_mode=ParseMode.HTML)
    except Exception as e:
        print(f"Failed to forward support message to admin: {e}")

    sent_msg = await message.answer(
        "✅ <b>Your help message has been sent directly to the admin!</b> We will get back to you soon.",
        parse_mode=ParseMode.HTML,
        reply_markup=get_main_menu_keyboard()
    )
    await state.clear()
    await state.update_data(last_menu_msg_id=sent_msg.message_id)

# ============================================
# USER MENU ACTIONS (COMMAND FALLBACKS)
# ============================================

@dp.message(Command('task'), StateFilter("*"))
async def get_task(message: Message, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id
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
                sent_msg = await message.answer('<tg-emoji emoji-id="5195033767969839232">🚀</tg-emoji> Your task submission is currently under admin review. Please wait for approval.', reply_markup=get_main_menu_keyboard(), parse_mode=ParseMode.HTML)
                await state.update_data(last_menu_msg_id=sent_msg.message_id)
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

                sent_msg = await message.answer(
                    f'<tg-emoji emoji-id="5447644880824181073">⚠️</tg-emoji> <b>You already have an active task.</b>\n\n'
                    f'<tg-emoji emoji-id="5310278924616356636">🎯</tg-emoji> <b>Your Current Task</b>\n\n'
                    f'<tg-emoji emoji-id="5197269100878907942">✍️</tg-emoji> #{task_id}\n'
                    f'<tg-emoji emoji-id="5870458774455587120">👤</tg-emoji> <b>Email:</b> {username} | <tg-emoji emoji-id="6005570495603282482">🔑</tg-emoji> <b>Password:</b> <code>{password}</code>\n'
                    f'<tg-emoji emoji-id="5417924076503062111">💰</tg-emoji> <b>Reward:</b> ₹{existing["reward"]}\n\n'
                    f'<tg-emoji emoji-id="5195033767969839232">🚀</tg-emoji> Time Remaining: {mins}m {secs}s', 
                    parse_mode=ParseMode.HTML,
                    reply_markup=get_task_action_keyboard()
                )
                await state.update_data(last_menu_msg_id=sent_msg.message_id)
                return
            else:
                async with conn.transaction():
                    await conn.execute('DELETE FROM task_assignments WHERE user_id=$1', user_id)
                    await conn.execute('UPDATE tasks SET status=$1 WHERE id=$2', 'available', task_id)

        task = await conn.fetchrow("SELECT id, title, details, reward FROM tasks WHERE status='available' ORDER BY RANDOM() LIMIT 1")
        if not task:
            sent_msg = await message.answer('📭 No tasks available right now.', reply_markup=get_main_menu_keyboard())
            await state.update_data(last_menu_msg_id=sent_msg.message_id)
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

    sent_msg = await message.answer(
        f'<tg-emoji emoji-id="5310278924616356636">🎯</tg-emoji> <b>Task #{task_id}</b>\n\n'
        f'<tg-emoji emoji-id="5870458774455587120">👤</tg-emoji> <b>Email:</b> {username} | <tg-emoji emoji-id="6005570495603282482">🔑</tg-emoji> <b>Password:</b> <code>{password}</code>\n'
        f'<tg-emoji emoji-id="5417924076503062111">💰</tg-emoji> <b>Reward:</b> ₹{reward}\n\n'
        f'<tg-emoji emoji-id="5195033767969839232">🚀</tg-emoji> You have ONLY 30 MINUTES to complete this task.', 
        parse_mode=ParseMode.HTML, 
        reply_markup=get_task_action_keyboard()
    )
    await state.update_data(last_menu_msg_id=sent_msg.message_id)

@dp.message(Command("balance"), StateFilter("*"))
async def balance(message: Message, state: FSMContext):
    await state.clear()
    user_data = await get_user_data(message.from_user.id)
    bal = user_data['balance'] if user_data else 0.0
    upi = user_data['upi'] if user_data and user_data['upi'] else "None"

    upi_set = upi != "None" and upi != ""
    
    text = (
        f'<tg-emoji emoji-id="5445353829304387411">💳</tg-emoji> <b>Balance</b>\n\n'
        f'<tg-emoji emoji-id="5278467510604160626">💵</tg-emoji><b>Available:</b> ₹{bal:.2f}\n'
        f'<tg-emoji emoji-id="6278557702109013266">🏦</tg-emoji><b>UPI:</b> <code>{upi}</code>'
    )
    
    sent_msg = await message.answer(text, parse_mode=ParseMode.HTML, reply_markup=get_balance_inline_keyboard(upi_set))
    await state.update_data(last_menu_msg_id=sent_msg.message_id)

@dp.message(Command("sell"), StateFilter("*"))
async def sell(message: Message, state: FSMContext):
    await state.clear()
    await state.set_state(UserState.selling_username)
    sent_msg = await message.answer(
        '<tg-emoji emoji-id="5445221832074483553">🏷️</tg-emoji> <b>Sell Price 30₹/Gmail</b>\n\n'
        '<tg-emoji emoji-id="5377548235709619284">🤑</tg-emoji> <b>Step 1/2:</b> Please send the Gmail <b>Username</b> (e.g., <code>example@gmail.com</code>):',
        parse_mode=ParseMode.HTML,
        reply_markup=get_back_inline_keyboard()
    )
    await state.update_data(last_menu_msg_id=sent_msg.message_id)

@dp.message(UserState.selling_username, F.text, ~F.text.startswith("/"), ~F.text.in_(MENU_BUTTONS))
async def process_sell_username(message: Message, state: FSMContext):
    username = message.text.strip()
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
        f"📨 <b>New Gmail Sell Request #{sell_id}</b>\n\n"
        f"👤 <b>Seller:</b> @{message.from_user.username} (<code>{user_id}</code>)\n"
        f"📧 <b>Username:</b> <code>{username}</code>\n"
        f"🔑 <b>Password:</b> <code>{password}</code>\n"
        f"💰 <b>Payout Rate:</b> ₹{rate:.2f}"
    )

    await bot.send_message(
        ADMIN_ID, 
        admin_message_text, 
        reply_markup=kb, 
        parse_mode=ParseMode.HTML
    )

    sent_msg = await message.answer(
        '<tg-emoji emoji-id="6217663806110175239">✅</tg-emoji> Your account details have been sent for admin review.\n\n'
        '<tg-emoji emoji-id="5447644880824181073">⚠️</tg-emoji> <b>Important:</b> Please make sure to <b>logout</b> of this account from your device!', 
        reply_markup=get_main_menu_keyboard(), 
        parse_mode=ParseMode.HTML
    )
    await state.clear()
    await state.update_data(last_menu_msg_id=sent_msg.message_id)

@dp.message(Command("history"), StateFilter("*"))
async def history(message: Message, state: FSMContext):
    await state.clear()
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT type, amount, note, created_at FROM transactions WHERE user_id=$1 ORDER BY id DESC LIMIT 10", message.from_user.id)
    if not rows:
        sent_msg = await message.answer("📭 No transactions found.", reply_markup=get_back_inline_keyboard())
        await state.update_data(last_menu_msg_id=sent_msg.message_id)
        return
    text = '<tg-emoji emoji-id="5440410042773824003">📜</tg-emoji> <b>Last Transactions</b>\n\n'
    for r in rows:
        sign = "+" if r['amount'] >= 0 else ""
        text += f"• {sign}₹{r['amount']:.2f} | {r['type']}\n{r['note']}\n{r['created_at'].strftime('%Y-%m-%d %H:%M:%S')}\n\n"
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

@dp.message(F.text == "➕ Add Task", StateFilter("*"))
async def admin_btn_add_task(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.set_state(AdminState.waiting_for_add_task)
    await message.answer("📧 Send the email/username to add as a task (e.g. `example@gmail.com`):", parse_mode=ParseMode.MARKDOWN)

@dp.message(F.text == "📥 Pending Reviews", StateFilter("*"))
async def admin_btn_pending_reviews(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.clear()
        
    async with db_pool.acquire() as conn:
        task_rows = await conn.fetch('''
            SELECT t.id, t.title, t.reward, ta.user_id 
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
        
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text='Approve', callback_data=f'taskapprove:{task_id}:{user_id}:{reward}', icon_custom_emoji_id="6217663806110175239", style="success"),
            InlineKeyboardButton(text='Decline', callback_data=f'taskdecline:{task_id}:{user_id}', icon_custom_emoji_id="5274099962655816924", style="danger")
        ]])
        
        await message.answer(
            f'📤 <b>Pending Task Submission</b>\n\n'
            f'👤 <b>User ID:</b> <code>{user_id}</code>\n'
            f'<tg-emoji emoji-id="5197269100878907942">✍️</tg-emoji> <b>Task #{task_id}</b>\n'
            f'📌 <b>Title:</b> {title}\n'
            f'<tg-emoji emoji-id="5417924076503062111">💰</tg-emoji> <b>Reward:</b> ₹{reward}',
            reply_markup=kb,
            parse_mode=ParseMode.HTML
        )

    for r in sell_rows:
        sell_id = r['id']
        user_id = r['user_id']
        details = r['details']
        amount = r['amount']

        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="Approve", callback_data=f"sellapprove_db:{sell_id}:{user_id}:{amount}", icon_custom_emoji_id="6217663806110175239", style="success"),
            InlineKeyboardButton(text="Decline", callback_data=f"selldecline_db:{sell_id}:{user_id}", icon_custom_emoji_id="5274099962655816924", style="danger")
        ]])

        await message.answer(
            f'📦 <b>Pending Gmail Sell Request #{sell_id}</b>\n\n'
            f'👤 <b>User ID:</b> <code>{user_id}</code>\n'
            f'<tg-emoji emoji-id="5417924076503062111">💰</tg-emoji> <b>Rate:</b> ₹{amount:.2f}\n\n'
            f'📝 <b>Details:</b>\n{details}',
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
    await state.set_state(AdminState.waiting_for_unassign_user_id)
    await call.message.answer("👤 Send the numeric **User ID** whose task you want to unassign:", parse_mode=ParseMode.MARKDOWN)
    try:
        await call.answer()
    except:
        pass

@dp.callback_query(F.data == "unassign_all_users")
async def process_unassign_all_users(call: CallbackQuery):
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
            except:
                pass
            return

        task_ids = [r['task_id'] for r in assigned_tasks]
        
        async with conn.transaction():
            await conn.execute("DELETE FROM task_assignments WHERE task_id = ANY($1::int[])", task_ids)
            await conn.execute("UPDATE tasks SET status='available' WHERE id = ANY($1::int[])", task_ids)

    await edit_admin_message(call, "✅ <b>Successfully unassigned task(s) and returned them to the pool.</b>")
    try:
        await call.answer("Unassigned all tasks successfully!", show_alert=True)
    except:
        pass

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
        pending_sells = await conn.fetchval("SELECT COUNT(*) FROM pending_sells WHERE status='pending_review'")
        completed_tasks = await conn.fetchval("SELECT COUNT(*) FROM tasks WHERE status='completed'")

    total_pending = (pending_review_tasks or 0) + (pending_sells or 0)

    text = (
        f"📊 <b>Bot Task & User Statistics</b>\n\n"
        f"👥 <b>Total Users (started bot):</b> <code>{total_users}</code>\n\n"
        f"📋 <b>Total Tasks Added:</b> <code>{total_tasks}</code>\n"
        f"🟢 <b>Available (Unassigned Pool):</b> <code>{avail_tasks}</code>\n"
        f"💼 <b>Assigned (Active with Users):</b> <code>{assigned_tasks}</code>\n"
        f"⏳ <b>Pending Review (Tasks + Sells):</b> <code>{total_pending}</code>\n"
        f"✅ <b>Completed (Approved):</b> <code>{completed_tasks}</code>"
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
# INPUT PROCESSORS FOR STATES (FILTER OUT BUTTON CLICKS)
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
        try:
            await bot.send_message(target_id, f"⚠️ Your current Task #{task_id} has been unassigned by the admin and returned to the pool.")
        except:
            pass
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

    sent_msg = await message.answer(f'<tg-emoji emoji-id="6217663806110175239">✅</tg-emoji> Your UPI ID has been linked to: <code>{upi_input}</code>', parse_mode=ParseMode.HTML, reply_markup=get_main_menu_keyboard())
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

@dp.message(AdminState.waiting_for_add_task, ~F.text.startswith("/"), ~F.text.in_(MENU_BUTTONS))
async def process_add_task_step(message: Message, state: FSMContext):
    username_input = message.text.strip()
    username = f"{username_input}@gmail.com" if "@" not in username_input else username_input
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
    await state.clear()

@dp.message(AdminState.waiting_for_add_balance, ~F.text.startswith("/"), ~F.text.in_(MENU_BUTTONS))
async def process_add_balance_step(message: Message, state: FSMContext):
    try:
        _, user_id_str, amount_str = f"cmd {message.text.strip()}".split()
        user_id = int(user_id_str)
        amount = float(amount_str)
        await ensure_user(user_id)
        async with db_pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("UPDATE users SET balance = balance + $1 WHERE user_id=$2", amount, user_id)
                await conn.execute("INSERT INTO transactions (user_id, type, amount, note) VALUES ($1, $2, $3, $4)", user_id, "admin_add", amount, "Admin balance add")
        await message.answer(f"✅ Added ₹{amount} to User `{user_id}`", parse_mode=ParseMode.MARKDOWN, reply_markup=get_admin_menu_keyboard())
        try:
            await bot.send_message(user_id, f"💰 Admin added ₹{amount} to your balance.")
        except:
            pass
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

        async with db_pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("UPDATE users SET balance = balance - $1 WHERE user_id=$2", amount, user_id)
                await conn.execute("INSERT INTO transactions (user_id, type, amount, note) VALUES ($1, $2, $3, $4)", user_id, "admin_cut", -amount, "Admin balance cut")

        await message.answer(f"✅ Cut ₹{amount} from User `{user_id}`", parse_mode=ParseMode.MARKDOWN, reply_markup=get_admin_menu_keyboard())
        try:
            await bot.send_message(user_id, f"⚠️ Admin deducted ₹{amount} from your balance.")
        except:
            pass
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
        banned = await is_banned(target_id)
        status = "🔴 Banned" if banned else "🟢 Active"
        await message.answer(f"👤 **User ID:** `{target_id}`\n💰 **Balance:** ₹{bal:.2f}\n🏦 **UPI:** `{upi}`\n📌 **Status:** {status}", parse_mode=ParseMode.MARKDOWN, reply_markup=get_admin_menu_keyboard())
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
        try:
            await bot.send_message(target_id, "🚫 You have been banned from using this bot.")
        except:
            pass
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
        try:
            await bot.send_message(target_id, "🎉 Your ban has been lifted! You can now use the bot again.")
        except:
            pass
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
    except:
        pass
    await state.set_state(UserState.setting_upi)
    await call.message.answer('<tg-emoji emoji-id="5364109867156001787">🔡</tg-emoji> Send your UPI ID below:\n\n<i>Example: username@upi or 9876543210@paytm</i>', parse_mode=ParseMode.HTML)

@dp.callback_query(F.data == "inline_withdraw")
async def inline_withdraw_handler(call: CallbackQuery):
    user_id = call.from_user.id
    user_data = await get_user_data(user_id)
    bal = user_data['balance'] if user_data else 0.0
    upi = user_data['upi'] if user_data else "None"

    if upi == "None" or not upi:
        try:
            await call.answer("❌ Please link your UPI ID first before withdrawing!", show_alert=True)
        except:
            pass
        return

    MIN_WITHDRAW = 150.0
    if bal < MIN_WITHDRAW:
        try:
            await call.answer(f"❌ Minimum withdrawal is ₹{MIN_WITHDRAW:.0f}. Current Balance: ₹{bal:.2f}", show_alert=True)
        except:
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
            except:
                pass
            return

        withdraw_id = await conn.fetchval(
            'INSERT INTO withdrawals(user_id, amount, upi) VALUES ($1, $2, $3) RETURNING id',
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
        f'<tg-emoji emoji-id="5417924076503062111">💰</tg-emoji> <b>WITHDRAWAL REQUEST #{withdraw_id}</b>\n\n'
        f'<tg-emoji emoji-id="5870458774455587120">👤</tg-emoji> @{call.from_user.username}\n'
        f'<tg-emoji emoji-id="5197269100878907942">✍️</tg-emoji> <code>{user_id}</code>\n'
        f'<tg-emoji emoji-id="5417924076503062111">💰</tg-emoji> Amount: ₹{bal:.2f}\n'
        f'<tg-emoji emoji-id="6152069549442208798">🤑</tg-emoji> UPI: <code>{upi}</code>',
        reply_markup=kb.as_markup(),
        parse_mode=ParseMode.HTML
    )

    try:
        await call.message.edit_text(f'<tg-emoji emoji-id="5195033767969839232">🚀</tg-emoji> Withdrawal request of ₹{bal:.2f} sent to admin using UPI: <code>{upi}</code>', parse_mode=ParseMode.HTML)
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
        except:
            pass
        return
    if row['status'] == 'pending_review':
        try:
            await call.answer('⏳ You have already submitted this task.', show_alert=True)
        except:
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
    except:
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
            except:
                pass
            return
        
        if row['status'] == 'pending_review':
            try:
                await call.answer("❌ Cannot cancel a task already submitted for admin review.", show_alert=True)
            except:
                pass
            return

        task_id = row['task_id']
        async with conn.transaction():
            await conn.execute('DELETE FROM task_assignments WHERE user_id=$1', user_id)
            await conn.execute("UPDATE tasks SET status='available' WHERE id=$1", task_id)
            
    try:
        await call.message.edit_text(f'<tg-emoji emoji-id="6217663806110175239">✅</tg-emoji> Task #{task_id} has been cancelled and returned to the pool.', parse_mode=ParseMode.HTML)
        await call.answer()
    except:
        pass

@dp.message(UserState.submitting_task, F.photo | F.text, ~F.text.startswith("/") if F.text else True, ~F.text.in_(MENU_BUTTONS) if F.text else True)
async def handle_task_submission(message: Message, state: FSMContext):
    user_id = message.from_user.id
    async with db_pool.acquire() as conn:
        task = await conn.fetchrow('SELECT t.id, t.title, t.reward FROM task_assignments ta JOIN tasks t ON ta.task_id = t.id WHERE ta.user_id=$1', user_id)
    if not task:
        await state.clear()
        sent_msg = await message.answer('❌ No active task found.', reply_markup=get_main_menu_keyboard())
        await state.update_data(last_menu_msg_id=sent_msg.message_id)
        return
    
    task_id = task['id']
    title = task['title']
    reward = task['reward']
    
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE tasks SET status='pending_review' WHERE id=$1", task_id)

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text='Approve', callback_data=f'taskapprove:{task_id}:{user_id}:{reward}', icon_custom_emoji_id="6217663806110175239", style="success"),
        InlineKeyboardButton(text='Decline', callback_data=f'taskdecline:{task_id}:{user_id}', icon_custom_emoji_id="5274099962655816924", style="danger")
    ]])
    if message.photo:
        await bot.send_photo(ADMIN_ID, photo=message.photo[-1].file_id, caption=f'<tg-emoji emoji-id="5206607081334906820">✔️</tg-emoji> <b>Task Submission</b>\n\n👤 User: @{message.from_user.username}\n<tg-emoji emoji-id="5197269100878907942">✍️</tg-emoji> Task #{task_id}\n📌 {title}\n<tg-emoji emoji-id="5417924076503062111">💰</tg-emoji> Reward: ₹{reward}', reply_markup=kb, parse_mode=ParseMode.HTML)
    else:
        await bot.send_message(ADMIN_ID, f'<tg-emoji emoji-id="5206607081334906820">✔️</tg-emoji> <b>Task Submission</b>\n\n👤 User: @{message.from_user.username}\n<tg-emoji emoji-id="5197269100878907942">✍️</tg-emoji> Task #{task_id}\n📌 {title}\n<tg-emoji emoji-id="5417924076503062111">💰</tg-emoji> Reward: ₹{reward}\n\nProof: {message.text}', reply_markup=kb, parse_mode=ParseMode.HTML)
    
    sent_msg = await message.answer('<tg-emoji emoji-id="5206607081334906820">✔️</tg-emoji> Submission sent for admin review.', reply_markup=get_main_menu_keyboard(), parse_mode=ParseMode.HTML)
    await state.clear()
    await state.update_data(last_menu_msg_id=sent_msg.message_id)

# ============================================
# UNIFIED SELL APPROVE & DECLINE HANDLERS
# ============================================

@dp.callback_query(F.data.startswith("sellapprove_db:"))
async def approve_sell_unified(call: CallbackQuery):
    _, sell_id_str, user_id_str, amount_str = call.data.split(":")
    sell_id = int(sell_id_str)
    user_id = int(user_id_str)
    amount = float(amount_str)

    async with db_pool.acquire() as conn:
        status = await conn.fetchval("SELECT status FROM pending_sells WHERE id=$1", sell_id)
        if status != 'pending_review':
            try:
                await call.answer("⚠️ This sell request has already been processed!", show_alert=True)
            except:
                pass
            return

        async with conn.transaction():
            await conn.execute("UPDATE users SET balance = balance + $1 WHERE user_id=$2", amount, user_id)
            await conn.execute("INSERT INTO transactions (user_id, type, amount, note) VALUES ($1, $2, $3, $4)", user_id, "sell", amount, f"Gmail sell #{sell_id} approved")
            await conn.execute("UPDATE pending_sells SET status='approved' WHERE id=$1", sell_id)

    await edit_admin_message(call, '✅ Sell Request Approved')
    try:
        await bot.send_message(user_id, f"🎉 Your Gmail sell request #{sell_id} was approved!\n+₹{amount} added to your balance.")
    except:
        pass

@dp.callback_query(F.data.startswith("selldecline_db:"))
async def decline_sell_unified(call: CallbackQuery, state: FSMContext):
    _, sell_id_str, user_id_str = call.data.split(":")
    sell_id = int(sell_id_str)
    user_id = int(user_id_str)

    async with db_pool.acquire() as conn:
        status = await conn.fetchval("SELECT status FROM pending_sells WHERE id=$1", sell_id)
        if status != 'pending_review':
            try:
                await call.answer("⚠️ This sell request has already been processed!", show_alert=True)
            except:
                pass
            return

    await state.set_state(AdminState.waiting_for_sell_reject_reason)
    await state.update_data(
        sell_id=sell_id,
        user_id=user_id, 
        admin_msg_id=call.message.message_id,
        is_photo=bool(call.message.photo)
    )
    await call.message.answer('<tg-emoji emoji-id="5447644880824181073">⚠️</tg-emoji> <b>Please reply with the reason for declining this sell request:</b>', parse_mode=ParseMode.HTML)
    try:
        await call.answer()
    except:
        pass

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

    try:
        await bot.send_message(user_id, f'<tg-emoji emoji-id="5447644880824181073">⚠️</tg-emoji> <b>Your sell request was declined.</b>\n\n<tg-emoji emoji-id="4956475826762679249">💬</tg-emoji> <b>Reason:</b> {reason}', parse_mode=ParseMode.HTML)
    except:
        pass

    await message.answer('<tg-emoji emoji-id="6217663806110175239">✅</tg-emoji> Rejection reason sent to user.', parse_mode=ParseMode.HTML)
    await state.clear()

# ============================================
# TASK APPROVE & DECLINE HANDLERS
# ============================================

@dp.callback_query(F.data.startswith("taskapprove:"))
async def approve_task(call: CallbackQuery):
    _, task_id_str, callback_user_id, reward_str = call.data.split(":")
    task_id = int(task_id_str)
    reward = float(reward_str)
    
    async with db_pool.acquire() as conn:
        current_status = await conn.fetchval("SELECT status FROM tasks WHERE id=$1", task_id)
        if current_status != 'pending_review':
            try:
                await call.answer("⚠️ Task has already been processed!", show_alert=True)
            except:
                pass
            return

        assigned_user_id = await conn.fetchval("SELECT user_id FROM task_assignments WHERE task_id=$1", task_id)
        user_id = assigned_user_id if assigned_user_id else int(callback_user_id)

        async with conn.transaction():
            await conn.execute("UPDATE users SET balance = balance + $1 WHERE user_id=$2", reward, user_id)
            await conn.execute("INSERT INTO transactions (user_id, type, amount, note) VALUES ($1, $2, $3, $4)", user_id, "task", reward, f"Task #{task_id}")
            await conn.execute("DELETE FROM task_assignments WHERE task_id=$1", task_id)
            await conn.execute("UPDATE tasks SET status='completed' WHERE id=$1", task_id)
            
    await edit_admin_message(call, '✅ Task Approved')
    try:
        await bot.send_message(user_id, f"🎉 Task approved!\n+₹{reward} added to your balance.")
    except Exception as e:
        print(f"Error notifying user on approval: {e}")

@dp.callback_query(F.data.startswith("taskdecline:"))
async def decline_task(call: CallbackQuery, state: FSMContext):
    _, task_id_str, user_id_str = call.data.split(":")
    task_id = int(task_id_str)
    user_id = int(user_id_str)
    
    async with db_pool.acquire() as conn:
        current_status = await conn.fetchval("SELECT status FROM tasks WHERE id=$1", task_id)
        if current_status != 'pending_review':
            try:
                await call.answer("⚠️ Task has already been processed!", show_alert=True)
            except:
                pass
            return

    await state.set_state(AdminState.waiting_for_task_reject_reason)
    await state.update_data(
        task_id=task_id, 
        user_id=user_id, 
        admin_msg_id=call.message.message_id,
        is_photo=bool(call.message.photo)
    )
    await call.message.answer(f'<tg-emoji emoji-id="5447644880824181073">⚠️</tg-emoji> <b>Please reply with the reason for declining Task #{task_id}:</b>', parse_mode=ParseMode.HTML)
    try:
        await call.answer()
    except:
        pass

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

    try:
        await bot.send_message(user_id, f'<tg-emoji emoji-id="5447644880824181073">⚠️</tg-emoji> <b>Your submission for Task #{task_id} was declined.</b>\n\n<tg-emoji emoji-id="4956475826762679249">💬</tg-emoji> <b>Reason:</b> {reason}\n\n<tg-emoji emoji-id="5251203410396458957">🛡</tg-emoji> The task has been returned to the pool.', parse_mode=ParseMode.HTML)
    except:
        pass

    await message.answer('<tg-emoji emoji-id="6217663806110175239">✅</tg-emoji> Rejection reason recorded and user notified.', parse_mode=ParseMode.HTML)
    await state.clear()

# ============================================
# WITHDRAWAL CALLBACKS (ADMIN SIDE)
# ============================================

@dp.callback_query(F.data.startswith("pay:"))
async def pay_withdraw(call: CallbackQuery):
    _, withdrawal_id, user_id, amount = call.data.split(":")
    withdrawal_id = int(withdrawal_id)
    user_id = int(user_id)
    amount = float(amount)
    
    async with db_pool.acquire() as conn:
        status = await conn.fetchval("SELECT status FROM withdrawals WHERE id=$1", withdrawal_id)
        if status != 'pending':
            try:
                await call.answer("⚠️ This withdrawal request has already been processed!", show_alert=True)
            except:
                pass
            return

        async with conn.transaction():
            await conn.execute("UPDATE users SET balance = balance - $1 WHERE user_id=$2", amount, user_id)
            await conn.execute("UPDATE withdrawals SET status='paid' WHERE id=$1", withdrawal_id)
            await conn.execute("INSERT INTO transactions (user_id, type, amount, note) VALUES ($1, $2, $3, $4)", user_id, "withdrawal", -amount, "Withdrawal paid")
            
    await edit_admin_message(call, '✅ Withdrawal Paid')
    try:
        await bot.send_message(user_id, f"🎉 Withdrawal of ₹{amount} has been paid.")
    except:
        pass

@dp.callback_query(F.data.startswith("reject:"))
async def reject_withdraw(call: CallbackQuery):
    _, withdrawal_id, user_id = call.data.split(":")
    withdrawal_id = int(withdrawal_id)
    user_id = int(user_id)
    
    async with db_pool.acquire() as conn:
        status = await conn.fetchval("SELECT status FROM withdrawals WHERE id=$1", withdrawal_id)
        if status != 'pending':
            try:
                await call.answer("⚠️ This withdrawal request has already been processed!", show_alert=True)
            except:
                pass
            return

        await conn.execute("UPDATE withdrawals SET status='rejected' WHERE id=$1", withdrawal_id)
        
    await edit_admin_message(call, '⚠️ Withdrawal Rejected')
    try:
        await bot.send_message(user_id, '<tg-emoji emoji-id="5447644880824181073">⚠️</tg-emoji> Your withdrawal request was rejected.', parse_mode=ParseMode.HTML)
    except:
        pass

# ============================================
# AUTO EXPIRE TASKS ENGINE
# ============================================

async def auto_expire_tasks():
    while True:
        try:
            async with db_pool.acquire() as conn:
                rows = await conn.fetch('''
                    SELECT ta.task_id, ta.user_id, ta.assigned_at 
                    FROM task_assignments ta
                    JOIN tasks t ON ta.task_id = t.id
                    WHERE t.status != 'pending_review'
                ''')
                for r in rows:
                    task_id = r['task_id']
                    user_id = r['user_id']
                    assigned_time = r['assigned_at']
                    
                    if datetime.utcnow() - assigned_time > timedelta(minutes=30):
                        async with conn.transaction():
                            await conn.execute('DELETE FROM task_assignments WHERE task_id=$1', task_id)
                            await conn.execute("UPDATE tasks SET status='available' WHERE id=$1", task_id)
                        try:
                            await bot.send_message(user_id, f'<tg-emoji emoji-id="5195033767969839232">🚀</tg-emoji> Task #{task_id} has expired after 30 minutes.\nThe task was returned to the pool.\n\nUse "Get Task" to get a new task.', reply_markup=get_main_menu_keyboard(), parse_mode=ParseMode.HTML)
                        except:
                            pass
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
