import asyncio
from datetime import datetime, timedelta, timezone
import os
import urllib.parse
import time
import re
import json
import secrets
import string
import hashlib
import hmac
import base64
import struct
import io
import html
import traceback
import smtplib
import socket
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
    InputMediaPhoto,
    InputMediaVideo
)
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder
from aiogram.exceptions import TelegramForbiddenError, TelegramBadRequest
from aiogram.client.session.middlewares.base import BaseRequestMiddleware
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

# Currency Conversion Rate (1 USD/USDT = 96.30 INR)
USD_TO_INR = 96.30

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

db_pool = None
HTTP_SESSION = None  # shared aiohttp session (reused across requests for speed)
BANNED_USERS_CACHE = set()
SUPPORT_REQUESTS_CACHE = {}  # In-memory store: {user_id: {"username": str, "message": str}}
MUST_JOIN_CHANNEL = None
PAYOUT_CHANNEL = None        # Channel where UPI/USDT withdrawal requests are posted for owner/admin approval
BOT_USERNAME = "GmailEarnexBot"
BOT_STATUS = True           # True = ON, False = OFF
BOT_OFF_MESSAGE = "<tg-emoji emoji-id=\"5420323339723881652\">⚠️</tg-emoji> Bot is Currently Off, Wait For Admin To On The Bot"   # Shown to users while bot is OFF; admin can customize it
REF_STATUS = True           # True = ON, False = OFF (Silent Referral Disabling)
ULTRA_STATUS = True         # True = ON, False = OFF
SINGLE_TASK_STATUS = True   # True = 1/1 task (must wait for review), False = Unlimited tasks concurrently
SELL_GMAIL_STATUS = True    # True = Enabled, False = Disabled
DEFAULT_TASK_PASS_STATUS = True  # True = Fixed Default Password, False = Random Generated Password

# 2FA STEP TOGGLES (per-flow)
#   ON  (default): Username -> Password(sell only) -> 2FA Key/QR -> OTP -> App Password -> Submitted
#   OFF: Task   -> Username validated -> Submitted immediately (no password/2FA/App Password asked)
#        Sell   -> Username validated -> Password -> Submitted immediately (no 2FA/App Password asked)
TWOFA_TASK_STATUS = True
TWOFA_SELL_STATUS = True

# APP PASSWORD "NOT FOUND" HELP CONTENT (shown when a user taps the
# "App Password Not Found" button on the App Password step of Sell/Task).
# Set/updated by the admin via /set app. None/[] = nothing configured yet.
APP_PASS_NOT_FOUND_TEXT = None    # caption/text shown to the user
APP_PASS_NOT_FOUND_MEDIA = []     # list of {"type": "photo"|"video", "file_id": str} - supports multiple items

# 2FA "COULDN'T VERIFY" HELP CONTENT (shown when a user taps the
# "Couldn't Verify" button on the 2FA step of Sell/Task).
# Set/updated by the admin via /set verify. None/[] = nothing configured yet.
TWOFA_NOT_VERIFIED_TEXT = None    # caption/text shown to the user
TWOFA_NOT_VERIFIED_MEDIA = []     # list of {"type": "photo"|"video", "file_id": str} - supports multiple items

# GLOBAL DYNAMIC RATES & DEFAULTS
DEFAULT_TASK_RATE = 50.0
GMAIL_SELL_RATE = 30.0
MIN_WITHDRAWAL_AMT = 150.0
DEFAULT_TASK_PASS = "TaskVerse@#"
REFERRAL_SELL_BONUS = 5.0
REFERRAL_TASK_BONUS = 7.0
REFERRAL_COMMISSION_PCT = 30.0                 # % of the referred user's accepted reward (admin-controlled)
REF_LB_REWARDS = [100.0, 50.0, 25.0]           # INR prizes for rank 1, 2, 3... (admin-controlled)
REF_LB_START = None                            # start of the current leaderboard period (None = all time)
REF_LB_PAYING = False                          # prevents a double payout on rapid taps
REF_LB_END = None                              # end of the current leaderboard round, naive UTC (None = no end date)
REF_LB_NOTIFIED_FOR = None                     # the end time the admin was already alerted about

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
VALIDATOR_PROVIDER = "netnit"  # "netnit" (Third Party) or "manual"

# Manual validator (no API key - direct SMTP RCPT probe against Gmail's own mail server, nothing is ever sent)
MANUAL_VALIDATOR_MX = "gmail-smtp-in.l.google.com"
MANUAL_VALIDATOR_FROM = "probe@example.com"
MANUAL_VALIDATOR_HELO = "example.com"
MANUAL_VALIDATOR_TIMEOUT = 10

# IN-MEMORY SPEED CACHES
JOINED_CACHE = {}     # {user_id: timestamp_joined}
USER_CACHE = {}       # {user_id: dict_data}

# List of all menu buttons to prevent state bleeding
MENU_BUTTONS = {
    "Get Task", "Balance", "Sell Gmail", "History", "Referrals", "My Accounts", "Settings", "Support", "Cancel", "Main Menu",
    "Add Task", "Tasks", "Available Tasks", "Pending Reviews", "Pending Withdrawals", "Chat", "Unassign Tasks", "Find ID", "Add Balance",
    "Cut Balance", "Check Balance", "Top Balances", "Ban User", "Unban User",
    "Broadcast", "Change Values", "Remove Task", "Transactions", "View Stats",
    "Must Join Channel", "Payout Channel", "🔴 Bot Status: OFF", "🟢 Bot Status: ON", "🟢 Ref Status: ON", "🔴 Ref Status: OFF", "Validator", "Transfer Admin",
    "🟢 Ultra Status: ON", "🔴 Ultra Status: OFF", "Manage Workers", "Dustbin", "Videos", "Holder", "Referral Panel",
    "Bot Status: ON", "Bot Status: OFF", "Ref Status: ON", "Ref Status: OFF", "Ultra Status: ON", "Ultra Status: OFF"
}

# ============================================
# BULK GMAIL SELL CONFIGURATION
# ============================================
BULK_SELL_MAX_ITEMS = 15   # max Gmails per bulk-sell batch (keeps processing time reasonable)

def generate_bulk_tag() -> str:
    """Short unique tag shared by every item submitted in one Bulk Sell batch, e.g. BULK-H73J9."""
    chars = string.ascii_uppercase + string.digits
    return "BULK-" + ''.join(secrets.choice(chars) for _ in range(5))

def render_bulk_progress_bar(current: int, total: int, width: int = 10) -> str:
    filled = int(width * current / total) if total else 0
    filled = max(0, min(width, filled))
    return "▰" * filled + "▱" * (width - filled)

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

PROVIDER_DISPLAY_NAMES = {
    "netnit": "Third Party",
    "manual": "Manual (Direct SMTP)",
}

def get_provider_name(provider: str = None) -> str:
    return PROVIDER_DISPLAY_NAMES.get(provider or VALIDATOR_PROVIDER, "Third Party")

def _manual_smtp_check(email: str) -> bool:
    """Adapted from the standalone gmail_exists.py checker: RCPT TO probe against
    Gmail's own mail server, then RSET before disconnecting - no email is ever sent."""
    try:
        with smtplib.SMTP(MANUAL_VALIDATOR_MX, 25, timeout=MANUAL_VALIDATOR_TIMEOUT) as server:
            server.ehlo(MANUAL_VALIDATOR_HELO)
            code, _ = server.mail(MANUAL_VALIDATOR_FROM)
            if code != 250:
                return False
            code, _msg = server.rcpt(email)
            server.rset()
            return code == 250
    except (socket.timeout, TimeoutError):
        print("Manual validator: timed out (port 25 may be blocked on this host/network)")
        return False
    except (smtplib.SMTPException, OSError) as exc:
        print(f"Manual validator error: {exc}")
        return False
    except Exception as exc:
        print(f"Manual validator error: {exc}")
        return False

async def manual_gmail_check(email: str) -> bool:
    try:
        return await asyncio.wait_for(asyncio.to_thread(_manual_smtp_check, email), timeout=20.0)
    except Exception as e:
        print(f"Manual validator exception: {e}")
        return False

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

    if VALIDATOR_PROVIDER == "manual":
        is_valid_email = await manual_gmail_check(email)
        if verify_msg:
            try:
                if is_valid_email:
                    await verify_msg.edit_text("<tg-emoji emoji-id=\"6217663806110175239\">✅</tg-emoji><i>Gmail Verified Successfully!</i><tg-emoji emoji-id=\"5461151367559141950\">🎉</tg-emoji>", parse_mode=ParseMode.HTML)
                else:
                    await verify_msg.edit_text("<tg-emoji emoji-id=\"5274099962655816924\">❌</tg-emoji><i>Gmail Verification Failed - Account Not Found!</i>", parse_mode=ParseMode.HTML)
            except Exception:
                pass
        return is_valid_email

    try:
        session = HTTP_SESSION if HTTP_SESSION and not HTTP_SESSION.closed else aiohttp.ClientSession()
        _own_session = session is not HTTP_SESSION
        try:
            # Only provider left besides "manual" is "netnit" (shown to admins as "Third Party")
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
# PREMIUM EMOJI ENGINE  (one place to manage EVERY emoji in the bot)
# ============================================
# HOW IT WORKS
#   * PREMIUM_EMOJI maps a normal emoji -> its premium (custom) emoji id.
#   * Every outgoing message / caption is scanned automatically and each normal emoji that has an id
#     here is sent as a premium emoji (see PremiumEmojiMiddleware below). Nothing else to change.
#   * Messages that were sent without parse_mode (plain text) are upgraded to HTML automatically
#     when they contain no "<" or "&", so premium emoji work in those too.
#   * Emoji with an EMPTY id ('') simply stay as normal emoji until you paste an id.
#   * Don't know an id? Send /emojiid to the bot (admin only), then send/forward the premium emoji:
#     the bot replies with ready-to-paste lines for this dictionary.
#
# Telegram limitation: popup alerts (callback_query.answer) and the text of buttons cannot contain
# custom emoji, so those stay normal. Buttons use icon_custom_emoji_id instead (see bk() below).
PREMIUM_EMOJI = {
    # ---------- ready (taken from your existing code) ----------
    "⚠️": "5420323339723881652",
    "✅": "6217663806110175239",
    "❌": "5274099962655816924",
    "📤": "5305265301917549162",
    "🚀": "5201691993775818138",
    "🎉": "5461151367559141950",
    "📊": "5244837092042750681",
    "🗑": "5262529363710060188",
    "📋": "5807510737332998730",
    "✍️": "5807510737332998730",
    "📭": "5262831879731555779",
    "🟢": "5416081784641168838",
    "🔴": "5411225014148014586",
    "📧": "5253742260054409879",
    "👤": "5870458774455587120",
    "📌": "5237699328843200968",
    "💳": "5445353829304387411",
    "📜": "5440410042773824003",
    "👋": "5195448447062251797",
    "⚙️": "5893161718179173515",
    "🛠": "5471960722206366390",
    "👥": "5391292736647209211",
    "🔗": "5271604874419647061",
    "🏷": "5350387571199319521",
    "🎯": "5310278924616356636",
    "🔑": "6005570495603282482",
    "💰": "5417924076503062111",
    "⚡": "5195033767969839232",
    "🏦": "6291696801636424911",
    "🪙": "5197434882321567830",
    "🚫": "5240241223632954241",
    "🌐": "5447410659077661506",
    "👷": "5264713049637409446",
    "📨": "5377548235709619284",
    "➕": "5397916757333654639",
    "📦": "5472027899789843495",
    "⏳": "5382194935057372936",
    "⌛": "5382194935057372936",
    "💸": "5444856076954520455",
    "🤖": "5309832892262654231",
    "📢": "5332724926216428039",
    "✔️": "6217663806110175239",
    # ---------- reused from your BUTTON icons (same meaning) - please double-check ----------
    "💬": "5265079444707486638",   # Chat button
    "🔍": "5307843983102204243",   # Find ID button
    "🔎": "5307843983102204243",   # Find ID button
    "🏆": "6183525361637136222",   # Top Balances button
    "🎬": "5282843764451195532",   # Tutorial Video button
    "🏠": "5416041192905265756",   # Main Menu button
    "👑": "5217822164362739968",   # Transfer Admin button
    # ---------- NEEDED: paste the premium emoji id between the quotes ----------
    "🆔": "5262899529761433210",
    "🟡": "5778202206922608769",
    "🔵": "5839380464116175529",
    "🟣": "5839042506024555846",
    "⚪": "5778335621491723621",
    "📝": "5262899529761433210",
    "🔒": "5197288647275071607",
    "🔓": "5332455502917949981",
    "📥": "5443127283898405358",
    "📅": "5274055917766202507",
    "🔐": "5296369303661067030",
    "🛡": "5379999674193172777",
    "🔄": "5190806721286657692",
    "🔢": "5253596497454310931",
    "📲": "5413683612342034207",
    "❗": "5314504236132747481",
    "💵": "5039789890133296083",
    "ℹ️": "5039649904264217620",
    "🤷": "5264985445053245222",
    "✉️": "5262529363710060188",
    "💡": "5262844652964303985",
    "📍": "5330320040883411678",
    "🤑": "5312123810638483121",
    "👁": "5210956306952758910",
    "🧹": "5445267414562389170",
    "🗓": "5274055917766202507",
    "♻️": "5375338737028841420",
    "✏️": "5395444784611480792",
    "💼": "5445221832074483553",
    "🎮": "5278467510604160626",
    "📱": "5413683612342034207",
    "⏰": "5382194935057372936",
    "🎲": "5012828713968993776",
    "🎳": "5012551795952585338",
}

def _pe_norm(s: str) -> str:
    return s.replace("\ufe0f", "")

_PE_MAP = {_pe_norm(k): v for k, v in PREMIUM_EMOJI.items() if v}

def pe_id(emoji: str) -> str:
    """Premium id for a normal emoji ('' when none is set)."""
    return _PE_MAP.get(_pe_norm(emoji), "")

def _build_pe_regex():
    keys = sorted(_PE_MAP.keys(), key=len, reverse=True)
    if not keys:
        return None
    return re.compile("|".join("".join(re.escape(ch) + "\ufe0f?" for ch in k) for k in keys))

_PE_RE = _build_pe_regex()
# parts of an HTML message that must never be touched: existing premium tags, <code>/<pre>, any tag
_PE_SKIP = re.compile(r"<tg-emoji\b[^>]*>.*?</tg-emoji>|<(code|pre)\b[^>]*>.*?</\1>|<[^>]*>", re.S | re.I)
_PE_MAX_ENTITIES = 95   # Telegram allows 100 entities per message

def pe(text: str) -> str:
    """Turn every normal emoji (that has an id in PREMIUM_EMOJI) inside an HTML string into a premium emoji."""
    if not text or _PE_RE is None:
        return text
    used = len(re.findall(r"<[a-zA-Z]", text))

    def _conv(chunk: str) -> str:
        nonlocal used
        def _rep(m):
            nonlocal used
            if used >= _PE_MAX_ENTITIES:
                return m.group(0)
            used += 1
            return f'<tg-emoji emoji-id="{_PE_MAP[_pe_norm(m.group(0))]}">{m.group(0)}</tg-emoji>'
        return _PE_RE.sub(_rep, chunk)

    out, pos = [], 0
    for m in _PE_SKIP.finditer(text):
        out.append(_conv(text[pos:m.start()]))
        out.append(m.group(0))
        pos = m.end()
    out.append(_conv(text[pos:]))
    return "".join(out)

def plain_text(html_text: str) -> str:
    """HTML -> plain text (for popup alerts, which cannot show formatting or premium emoji)."""
    return html.unescape(re.sub(r"<[^>]+>", "", html_text or ""))

def bk(emoji: str, label: str) -> dict:
    """Button helper: premium icon when an id exists, otherwise the normal emoji inside the label.
    Usage: kb.button(**bk("💬", "Reply User"), callback_data="x")"""
    icon = pe_id(emoji)
    if icon:
        return {"text": label, "icon_custom_emoji_id": icon}
    return {"text": f"{emoji} {label}"}

class PremiumEmojiMiddleware(BaseRequestMiddleware):
    """Runs on every Bot API request and swaps normal emoji for premium ones in text / caption."""
    async def __call__(self, make_request, bot, method):
        try:
            for field in ("text", "caption"):
                value = getattr(method, field, None)
                if not isinstance(value, str) or not value:
                    continue
                if getattr(method, "entities" if field == "text" else "caption_entities", None):
                    continue
                if not hasattr(method, "parse_mode"):
                    continue  # e.g. answerCallbackQuery popups: no formatting possible
                mode = getattr(method, "parse_mode", None)
                mode = getattr(mode, "value", mode)
                if mode == "HTML":
                    setattr(method, field, pe(value))
                elif not isinstance(mode, str) and "<" not in value and "&" not in value:
                    new_value = pe(value)
                    if new_value != value:
                        setattr(method, field, new_value)
                        method.parse_mode = "HTML"
        except Exception as exc:  # never block a message because of emoji conversion
            print(f"[premium-emoji] skipped: {exc}")
        return await make_request(bot, method)

bot.session.middleware(PremiumEmojiMiddleware())

def make_bot(token: str) -> Bot:
    """Create an extra Bot (e.g. the worker bot) that also gets premium emoji."""
    b = Bot(token=token)
    b.session.middleware(PremiumEmojiMiddleware())
    return b

# ============================================
# NEW: PREMIUM EMOJI HELPERS
# ============================================

def tg(emoji_id: str, fallback: str) -> str:
    """Premium (custom) emoji tag. Falls back to the normal emoji when no id is set."""
    return f'<tg-emoji emoji-id="{emoji_id}">{fallback}</tg-emoji>' if emoji_id else fallback

# >>> PREMIUM EMOJI IDS FOR THE NEW FEATURES <<<
# These now read their ids from PREMIUM_EMOJI (top of the emoji engine) - paste ids there.
NEW_EMOJI_IDS = {
    "lock":     pe_id("🔐"),   # 🔐  2FA / secure          (edit the id in PREMIUM_EMOJI above)
    "otp":      pe_id("🔢"),   # 🔢  OTP code
    "refresh":  pe_id("🔄"),   # 🔄  refresh code button
    "qr":       pe_id("📲"),   # 📲  authenticator QR
    "shield":   pe_id("🛡"),   # 🛡  app password
    "hold":     pe_id("🔒"),   # 🔒  hold balance
    "holder":   pe_id("📥"),   # 📥  holder / send to holder
    "release":  pe_id("🔓"),   # 🔓  release button
    "calendar": pe_id("📅"),   # 📅  release date
}

def E(key: str, fallback: str) -> str:
    return tg(NEW_EMOJI_IDS.get(key, ""), fallback)

def BI(key: str):
    """Button icon id for a new emoji (None when not configured)."""
    return NEW_EMOJI_IDS.get(key) or None

def _kb_btn(kb, text: str, icon=None, **kw):
    if icon:
        kw["icon_custom_emoji_id"] = icon
    kb.button(text=text, **kw)

# Already-existing premium emojis re-used by the new screens
E_OK = tg("6217663806110175239", "✅")
E_NO = tg("5274099962655816924", "❌")
E_WARN = tg("5420323339723881652", "⚠️")
E_KEY = tg("6005570495603282482", "🔑")
E_MAIL = tg("5253742260054409879", "📧")
E_MONEY = tg("5417924076503062111", "💰")
E_BOX = tg("5472027899789843495", "📦")
E_TIME = tg("5382194935057372936", "⏳")
E_USER = tg("5870458774455587120", "👤")
E_PARTY = tg("5461151367559141950", "🎉")
E_EMPTY = tg("5262831879731555779", "📭")
E_ROCKET = tg("5201691993775818138", "🚀")
E_UP = tg("5305265301917549162", "📤")
E_GREEN = tg("5416081784641168838", "🟢")
E_RED = tg("5411225014148014586", "🔴")

ICON_OK = "6217663806110175239"
ICON_NO = "5274099962655816924"
ICON_CANCEL = "5240241223632954241"
ICON_BACK = "5875082500023258804"
ICON_COPY = "5271604874419647061"

# New-feature emojis (resolved at import time from NEW_EMOJI_IDS)
E_LOCK = E("lock", "🔐")
E_OTP = E("otp", "🔢")
E_QR = E("qr", "📲")
E_SHIELD = E("shield", "🛡")
E_HOLD = E("hold", "🔒")
E_HOLDER = E("holder", "📥")
E_RELEASE = E("release", "🔓")
E_CAL = E("calendar", "📅")

# ============================================
# NEW: GENERIC SMALL HELPERS
# ============================================

HOLD_DAYS = 3          # every Task / Sell Gmail stays in hold for this many days (release is MANUAL by admin)
ACTIVE_SUBMISSIONS = set()   # user ids currently verifying an app password (prevents double submit)

def _esc(value) -> str:
    return html.escape("" if value is None else str(value))

# ---------- INDIA TIME (IST, UTC+5:30) ----------
# Everything is STORED in UTC (database + internal logic) and always SHOWN to people in India time.
IST = timezone(timedelta(hours=5, minutes=30))
os.environ['TZ'] = 'Asia/Kolkata'
try:
    time.tzset()
except Exception:
    pass

def to_ist(dt):
    """Naive datetimes from the database/utcnow() are UTC -> converted to India time."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(IST)

def now_ist():
    return datetime.now(IST)

def fmt_dt(dt) -> str:
    try:
        return to_ist(dt).strftime("%d %b %Y, %I:%M %p")
    except Exception:
        return "-"

def fmt_left(end) -> str:
    """Countdown until `end` (naive UTC datetime)."""
    secs = int((end - datetime.utcnow()).total_seconds())
    if secs <= 0:
        return "ended"
    days, rem = divmod(secs, 86400)
    hours, rem = divmod(rem, 3600)
    mins = rem // 60
    if days:
        return f"{days}d {hours}h {mins}m left"
    if hours:
        return f"{hours}h {mins}m left"
    return f"{mins}m left"

def fmt_remaining(release_at) -> str:
    try:
        secs = int((release_at - datetime.utcnow()).total_seconds())
    except Exception:
        return ""
    if secs <= 0:
        return "hold period complete"
    days, rem = divmod(secs, 86400)
    hours, rem = divmod(rem, 3600)
    mins = rem // 60
    if days:
        return f"{days}d {hours}h left"
    if hours:
        return f"{hours}h {mins}m left"
    return f"{mins}m left"

def chunk_blocks(blocks, limit: int = 3500, sep: str = "\n\n"):
    """Packs text blocks into messages that stay below Telegram's 4096 char limit."""
    chunks, current = [], ""
    for b in blocks:
        candidate = b if not current else current + sep + b
        if len(candidate) > limit and current:
            chunks.append(current)
            current = b
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks

# ============================================
# NEW: 2FA (TOTP) + QR DECODING  (logic taken from the 2FA key bot)
# ============================================

def get_remaining_seconds() -> int:
    return 30 - (int(time.time()) % 30)

def extract_secret(raw_data: str):
    """Accepts a plain base32 secret (spaces/dashes allowed) or an otpauth:// link."""
    if not raw_data:
        return None
    raw_data = raw_data.strip()
    if raw_data.lower().startswith("otpauth://"):
        try:
            parsed = urllib.parse.urlparse(raw_data)
            query = urllib.parse.parse_qs(parsed.query)
            if "secret" in query:
                raw_data = query["secret"][0].strip()
            else:
                return None
        except Exception:
            return None
    if not re.fullmatch(r"[A-Za-z2-7\s\-=]+", raw_data):
        return None
    # A key is either one token, or groups of 4 characters (how Google shows it). Normal sentences are rejected.
    if re.search(r"[\s\-]", raw_data):
        groups = [g for g in re.split(r"[\s\-]+", raw_data.strip("=")) if g]
        if not groups or any(len(g) != 4 for g in groups[:-1]) or not 1 <= len(groups[-1].strip("=")) <= 4:
            return None
    cleaned = re.sub(r"[^A-Za-z2-7]", "", raw_data).upper()
    if len(cleaned) < 16 or len(cleaned) > 128:
        return None
    return cleaned

def generate_local_totp(secret: str):
    try:
        secret = re.sub(r"[^A-Za-z2-7]", "", secret).upper()
        if not secret:
            return None
        missing_padding = len(secret) % 8
        if missing_padding:
            secret += "=" * (8 - missing_padding)
        key = base64.b32decode(secret, casefold=True)
        counter = int(time.time()) // 30
        msg = struct.pack(">Q", counter)
        mac = hmac.new(key, msg, hashlib.sha1).digest()
        offset = mac[-1] & 0x0F
        code = (struct.unpack(">I", mac[offset:offset + 4])[0] & 0x7FFFFFFF) % 1000000
        return f"{code:06d}"
    except Exception:
        return None

def _decode_qr_sync(image_bytes: bytes):
    """Tries pyzbar (if installed) and then OpenCV. Returns the decoded text or None."""
    if not image_bytes:
        return None
    try:
        from PIL import Image
        from pyzbar.pyzbar import decode as zbar_decode
        img = Image.open(io.BytesIO(image_bytes))
        res = zbar_decode(img)
        if res:
            return res[0].data.decode("utf-8", errors="ignore")
    except Exception:
        pass
    try:
        import cv2
        import numpy as np
        arr = np.frombuffer(image_bytes, dtype=np.uint8)
        frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if frame is not None:
            detector = cv2.QRCodeDetector()
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            h, w = gray.shape[:2]
            candidates = [frame, gray]
            if max(h, w) < 1200:
                candidates.append(cv2.resize(gray, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC))
            if max(h, w) > 1600:
                candidates.append(cv2.resize(gray, None, fx=0.5, fy=0.5, interpolation=cv2.INTER_AREA))
            for cand in candidates:
                val, _pts, _ = detector.detectAndDecode(cand)
                if val:
                    return val
    except Exception:
        pass
    return None

async def decode_qr_bytes(image_bytes: bytes):
    return await asyncio.to_thread(_decode_qr_sync, image_bytes)

# ============================================
# NEW: APP PASSWORD VERIFICATION (SMTP)
# ============================================

def check_gmail_credentials(email: str, app_password: str) -> bool:
    # Remove any spaces that Google displays in generated app passwords (e.g., "abcd efgh ijkl mnop")
    cleaned_password = app_password.replace(" ", "")

    try:
        # Establish connection to Gmail's SMTP server
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=10) as server:
            server.ehlo()
            server.starttls()  # Upgrade connection to secure TLS
            server.ehlo()
            server.login(email, cleaned_password)

        print("Success: App password is valid.")
        return True

    except smtplib.SMTPAuthenticationError:
        print("Failed: Authentication error. The email or app password is incorrect.")
        return False
    except smtplib.SMTPConnectError:
        print("Error: Could not connect to Gmail's SMTP server.")
        return False
    except Exception as e:
        print(f"Error: {e}")
        return False

async def verify_app_password(email: str, app_password: str) -> bool:
    try:
        return await asyncio.wait_for(asyncio.to_thread(check_gmail_credentials, email, app_password), timeout=40.0)
    except Exception as e:
        print(f"App password verification error: {e}")
        return False

# ============================================
# NEW: SUBMISSION FLOW SCREENS (2FA -> CODE -> APP PASSWORD)
# ============================================

def password_already_registered_text() -> str:
    return (
        f'{E_NO} <b>Password Already Registered!</b>\n'
        '━━━━━━━━━━━━━━━━━━\n\n'
        f'{E_WARN} This password already registered in system, change the password and give another.\n\n'
        f'{E_KEY} Every Gmail you sell must use a <b>unique</b> password \u2014 '
        f'duplicates are never allowed.\n\n'
        f'✍️ <b>Send a different Password:</b>'
    )

def flow_cancel_button(flow: str) -> InlineKeyboardButton:
    if flow == 'task':
        return InlineKeyboardButton(text="Cancel Task", icon_custom_emoji_id=ICON_CANCEL, callback_data="user_cancel_task", style="danger")
    return InlineKeyboardButton(text="Cancel", icon_custom_emoji_id=ICON_CANCEL, callback_data="menu_back", style="danger")

def build_twofa_prompt(flow: str, error: str = None):
    text = (
        f'{E_OK} <b>Email Is Validated!</b>\n'
        f'{E_LOCK} <b>Now Complete 2FA</b>\n'
        '━━━━━━━━━━━━━━━━━━\n\n'
        f'{E_QR} Send your <b>Authenticator QR</b> or <b>2FA Secret Key</b>\n\n'
        f'{E_KEY} This is the secret key from your authenticator app.\n'
        '🔎 Looks like: <code>JBSWY3DPEHPK3PXP</code> or an <b>Authenticator QR</b> screenshot\n\n'
        '✍️ <b>Send the QR or Key:</b>'
    )
    if error:
        text = f'{E_NO} <b>{error}</b>\n\n' + text
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Couldn't Verify", icon_custom_emoji_id="5420323339723881652", callback_data="twofa_couldnt_verify", style="danger")],
        [flow_cancel_button(flow)]
    ])
    return text, kb

def build_otp_message(secret: str, flow: str):
    otp = generate_local_totp(secret)
    if not otp:
        return None, None
    left = get_remaining_seconds()
    text = (
        f'{E_LOCK} <b>2FA Authentication Code</b>\n'
        '━━━━━━━━━━━━━━━━━━\n\n'
        f'{E_OTP} <b>OTP:</b> <code>{otp}</code>\n'
        f'{E_TIME} <b>Expires in:</b> {left} seconds\n\n'
        '💡 Enter this code where Google asks for it, then press <b>Done</b>.\n'
        '🔄 Code expired? Press <b>Refresh Code</b>.'
    )
    kb = InlineKeyboardBuilder()
    _kb_btn(kb, "Copy OTP", ICON_COPY, copy_text=CopyTextButton(text=otp), style="primary")
    kb.button(**bk("🔄", "Refresh Code"), callback_data="tw_ref", style="primary")
    _kb_btn(kb, "Done", ICON_OK, callback_data="tw_done", style="success")
    kb.button(text="Couldn't Verify", icon_custom_emoji_id="5420323339723881652", callback_data="twofa_couldnt_verify", style="danger")
    kb.adjust(2, 1, 1)
    kb.row(flow_cancel_button(flow))
    return text, kb.as_markup()

def build_apppass_prompt(flow: str, error: str = None):
    text = (
        f'{E_SHIELD} <b>Enter App Password</b>\n'
        '━━━━━━━━━━━━━━━━━━\n\n'
        f'{E_KEY} Create an App Password in Google settings\n'
        '🌐 <code>myaccount.google.com/apppasswords</code>\n\n'
        f'{E_LOCK} Then send the <b>16-character</b> password here:'
    )
    if error:
        text = f'{E_NO} <b>{error}</b>\n\n' + text
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="Open App Passwords", url="https://myaccount.google.com/apppasswords", style="primary"))
    kb.row(InlineKeyboardButton(text="App Password Not Found", icon_custom_emoji_id="5420323339723881652", callback_data="apppass_not_found", style="danger"))
    kb.row(flow_cancel_button(flow))
    return text, kb.as_markup()

def build_submission_success_text(label: str, ref_id, amt_str: str, release_at) -> str:
    """Shared 'Submitted Successfully' message - used whether the submission went through the
    full 2FA + App Password flow, or skipped straight to the hold (2FA step turned OFF)."""
    return (
        f'{E_OK} <b>{label} #{ref_id} Submitted Successfully!</b>\n'
        '━━━━━━━━━━━━━━━━━━\n\n'
        f'{E_HOLD} <b>Status:</b> Goes into {HOLD_DAYS}-Day Hold\n'
        f'{E_MONEY} <b>Hold Balance:</b> +{amt_str}\n'
        f'{E_CAL} <b>Release Date:</b> {fmt_dt(release_at)} IST\n\n'
        f'ℹ️ Your reward stays in <b>Hold Balance</b> until the admin reviews and releases it.\n\n'
        f'{E_WARN} <b>Important:</b> Please make sure to <b>logout</b> of this account from your device!'
    )

async def _flow_edit(chat_id: int, message_id, text: str, kb) -> int:
    """Edits the flow message in place (so no stale messages are left). Falls back to a new message."""
    if message_id:
        try:
            await bot.edit_message_text(text=text, chat_id=chat_id, message_id=message_id, parse_mode=ParseMode.HTML, reply_markup=kb)
            return message_id
        except TelegramBadRequest as e:
            if "message is not modified" in str(e):
                return message_id
        except Exception:
            pass
    sent = await bot.send_message(chat_id, text, parse_mode=ParseMode.HTML, reply_markup=kb)
    return sent.message_id

async def _task_flow_active(user_id: int, task_id) -> bool:
    if not task_id:
        return False
    async with db_pool.acquire() as conn:
        status = await conn.fetchval(
            "SELECT t.status FROM task_assignments ta JOIN tasks t ON t.id = ta.task_id WHERE ta.task_id=$1 AND ta.user_id=$2",
            task_id, user_id
        )
    return status in ('assigned', 'verifying')

# ============================================
# NEW: HOLD SYSTEM CORE (database helpers)
# ============================================

def hold_kind_label(kind: str) -> str:
    return "Task Gmail" if kind == 'task' else "Sell Gmail"

def hold_creds_text(h: dict) -> str:
    """gmail / password / 2fakey / app password - one per line."""
    return "\n".join([
        str(h.get('email') or ''),
        str(h.get('gmail_password') or ''),
        str(h.get('twofa_key') or ''),
        str(h.get('app_password') or ''),
    ])

async def get_hold(hold_id: int):
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow('''
            SELECT h.*, s.claimed_by
            FROM holds h
            LEFT JOIN pending_sells s ON (h.kind = 'sell' AND s.id = h.ref_id)
            WHERE h.id = $1
        ''', hold_id)
    return dict(row) if row else None

def render_hold_card(h: dict, header: str = None) -> str:
    kind = h['kind']
    icon = E_UP if kind == 'task' else E_BOX
    title = header or f"{hold_kind_label(kind)} #{h['ref_id']}"
    uname = f"@{_esc(h['username'])}" if h.get('username') else "No username"
    status = h['status']
    status_lbl = {
        'pending_review': "🟡 Pending Review",
        'in_holder': "🟣 In Holder",
        'released': f"{E_GREEN} Released & Paid",
        'rejected': f"{E_RED} Rejected",
    }.get(status, status)

    lines = [
        f"{icon} <b>{title}</b>",
    ]
    if h.get('bulk_tag'):
        lines.append(f"🏷 <b>Bulk Tag:</b> <code>#{h['bulk_tag']}</code>")
    lines.append(f"{E_USER} <b>User:</b> {uname} (<code>{h['user_id']}</code>)")
    if kind == 'sell' and h.get('claimed_by'):
        lines.append(f"👷 <b>Claimed By Worker:</b> <code>{h['claimed_by']}</code>")
    lines += [
        "━━━━━━━━━━━━━━━━━━",
        f"{E_MAIL} <b>Gmail:</b> <code>{_esc(h.get('email'))}</code>",
        f"{E_KEY} <b>Password:</b> <code>{_esc(h.get('gmail_password'))}</code>",
        f"{E_LOCK} <b>2FA Key:</b> <code>{_esc(h.get('twofa_key'))}</code>",
        f"{E_SHIELD} <b>App Password:</b> <code>{_esc(h.get('app_password'))}</code>",
        "━━━━━━━━━━━━━━━━━━",
        f"{E_MONEY} <b>Amount:</b> ₹{float(h.get('amount') or 0):.2f}",
        f"{E_TIME} <b>Submitted:</b> {fmt_dt(h.get('created_at'))} IST",
    ]
    if h.get('release_at'):
        extra = f" ({fmt_remaining(h['release_at'])})" if status in ('pending_review', 'in_holder') else ""
        lines.append(f"{E_CAL} <b>Release Date:</b> {fmt_dt(h['release_at'])} IST{extra}")
    lines.append(f"📍 <b>Status:</b> {status_lbl}")
    if status == 'rejected' and h.get('reject_reason'):
        lines.append(f"💬 <b>Reason:</b> {_esc(h['reject_reason'])}")
    return "\n".join(lines)

def get_hold_card_keyboard(h: dict) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    creds = hold_creds_text(h)
    if len(creds) <= 256:
        _kb_btn(kb, "Copy Credentials", ICON_COPY, copy_text=CopyTextButton(text=creds), style="primary")
    else:
        _kb_btn(kb, "Copy Credentials", ICON_COPY, callback_data=f"hold_copy:{h['id']}", style="primary")

    status = h['status']
    if status == 'pending_review':
        kb.button(**bk("📥", "Send to Holder"), callback_data=f"hold_send:{h['id']}", style="success")
        _kb_btn(kb, "Reject", ICON_NO, callback_data=f"hold_rej:{h['id']}", style="danger")
        kb.adjust(1, 2)
    elif status == 'in_holder':
        kb.button(**bk("🔓", "Release"), callback_data=f"hold_rel:{h['id']}", style="success")
        _kb_btn(kb, "Reject", ICON_NO, callback_data=f"hold_rej:{h['id']}", style="danger")
        kb.adjust(1, 2)
    else:
        kb.adjust(1)
    return kb.as_markup()

async def refresh_hold_card_message(chat_id: int, message_id: int, hold_id: int):
    """Re-renders an admin hold card in place (after send / release / reject)."""
    h = await get_hold(hold_id)
    if not h:
        return
    try:
        await bot.edit_message_text(
            text=render_hold_card(h), chat_id=chat_id, message_id=message_id,
            parse_mode=ParseMode.HTML, reply_markup=get_hold_card_keyboard(h)
        )
    except Exception as e:
        if "message is not modified" not in str(e):
            print(f"Error refreshing hold card: {e}")

async def notify_admin_new_hold(hold_id: int):
    h = await get_hold(hold_id)
    if not h:
        return
    header = ("New Task Submission" if h['kind'] == 'task' else "New Gmail Sell Request") + f" #{h['ref_id']}"
    try:
        await bot.send_message(ADMIN_ID, render_hold_card(h, header=header), parse_mode=ParseMode.HTML, reply_markup=get_hold_card_keyboard(h))
    except Exception as e:
        print(f"Error notifying admin of new hold: {e}")

async def move_hold_to_holder(conn, hold_id: int):
    """pending_review -> in_holder. Returns the hold dict, or None if it was already processed."""
    async with conn.transaction():
        h = await conn.fetchrow(
            "UPDATE holds SET status='in_holder', holder_at=$2 WHERE id=$1 AND status='pending_review' RETURNING *",
            hold_id, datetime.utcnow()
        )
        if not h:
            return None
        h = dict(h)
        if h['kind'] == 'task':
            await conn.execute("DELETE FROM task_assignments WHERE task_id=$1", h['ref_id'])
            await conn.execute("UPDATE tasks SET status='in_holder' WHERE id=$1", h['ref_id'])
        else:
            await conn.execute("UPDATE pending_sells SET status='in_holder' WHERE id=$1", h['ref_id'])
        return h

async def release_hold(hold_id: int):
    """in_holder -> released. Moves the amount Hold Balance -> Balance. Returns dict or None."""
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            h = await conn.fetchrow(
                "UPDATE holds SET status='released', closed_at=$2 WHERE id=$1 AND status='in_holder' RETURNING *",
                hold_id, datetime.utcnow()
            )
            if not h:
                return None
            h = dict(h)
            uid = h['user_id']
            amt = float(h['amount'] or 0)
            ref_id = h['ref_id']
            email = h.get('email') or ''
            label = hold_kind_label(h['kind'])

            await ensure_user(uid, conn=conn)
            await conn.execute(
                "UPDATE users SET hold_balance = GREATEST(COALESCE(hold_balance, 0) - $1, 0), balance = balance + $1 WHERE user_id=$2",
                amt, uid
            )
            await conn.execute(
                "INSERT INTO transactions (user_id, type, amount, note, hold_id) VALUES ($1, 'hold_remove', $2, $3, $4)",
                uid, -amt, f"Released from hold - {label} {email} #{ref_id}", hold_id
            )
            if h['kind'] == 'task':
                await conn.execute("UPDATE tasks SET status='completed' WHERE id=$1", ref_id)
                await conn.execute(
                    "INSERT INTO transactions (user_id, type, amount, note, hold_id) VALUES ($1, 'task', $2, $3, $4)",
                    uid, amt, f"{email} #{ref_id}", hold_id
                )
            else:
                await conn.execute("UPDATE pending_sells SET status='approved' WHERE id=$1", ref_id)
                await conn.execute(
                    "INSERT INTO transactions (user_id, type, amount, note, hold_id) VALUES ($1, 'sell', $2, $3, $4)",
                    uid, amt, f"Gmail sell #{ref_id} approved", hold_id
                )

            h['ref_info'] = None
            referred_by = await conn.fetchval("SELECT referred_by FROM users WHERE user_id=$1", uid)
            ref_reward = round(amt * REFERRAL_COMMISSION_PCT / 100.0, 2)
            if REF_STATUS and referred_by and referred_by != uid and ref_reward > 0:
                await ensure_user(referred_by, conn=conn)
                await conn.execute(
                    "UPDATE users SET balance = balance + $1, referral_earnings = referral_earnings + $1 WHERE user_id=$2",
                    ref_reward, referred_by
                )
                await conn.execute(
                    "INSERT INTO transactions (user_id, type, amount, note) VALUES ($1, 'referral', $2, $3)",
                    referred_by, ref_reward, f"Referral reward from User #{uid}"
                )
                h['ref_info'] = (referred_by, ref_reward)
    invalidate_user_cache(uid)
    if h.get('ref_info'):
        invalidate_user_cache(h['ref_info'][0])
    return h

async def reject_hold(hold_id: int, reason: str):
    """pending_review / in_holder -> rejected. Removes the amount from Hold Balance. Returns dict or None."""
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            h = await conn.fetchrow(
                "UPDATE holds SET status='rejected', closed_at=$2, reject_reason=$3 "
                "WHERE id=$1 AND status IN ('pending_review', 'in_holder') RETURNING *",
                hold_id, datetime.utcnow(), reason
            )
            if not h:
                return None
            h = dict(h)
            uid = h['user_id']
            amt = float(h['amount'] or 0)
            ref_id = h['ref_id']
            email = h.get('email') or ''
            label = hold_kind_label(h['kind'])

            await conn.execute(
                "UPDATE users SET hold_balance = GREATEST(COALESCE(hold_balance, 0) - $1, 0) WHERE user_id=$2",
                amt, uid
            )
            await conn.execute(
                "INSERT INTO transactions (user_id, type, amount, note, hold_id) VALUES ($1, 'hold_remove', $2, $3, $4)",
                uid, -amt, f"Rejected - {label} {email} #{ref_id}: {reason}", hold_id
            )
            if h['kind'] == 'task':
                await conn.execute("DELETE FROM task_assignments WHERE task_id=$1", ref_id)
                await conn.execute("UPDATE tasks SET status='available' WHERE id=$1", ref_id)
            else:
                await conn.execute("UPDATE pending_sells SET status='declined' WHERE id=$1", ref_id)
    invalidate_user_cache(uid)
    return h

async def finalize_task_submission(user_id: int, username, task_id: int, email: str, gmail_password: str, twofa: str, app_pw: str):
    """Called after the app password is verified: task -> pending_review, hold row created, Hold Balance += reward."""
    release_at = datetime.utcnow() + timedelta(days=HOLD_DAYS)
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow('''
                UPDATE tasks SET status='pending_review'
                WHERE id=$1 AND status IN ('assigned', 'verifying')
                  AND EXISTS (SELECT 1 FROM task_assignments WHERE task_id=$1 AND user_id=$2)
                RETURNING reward, added_by
            ''', task_id, user_id)
            if not row:
                return None
            reward = float(row['reward'] or 0)
            await ensure_user(user_id, conn=conn)
            hold_id = await conn.fetchval('''
                INSERT INTO holds (kind, ref_id, user_id, username, email, gmail_password, twofa_key, app_password, amount, status, created_at, release_at)
                VALUES ('task', $1, $2, $3, $4, $5, $6, $7, $8, 'pending_review', $9, $10) RETURNING id
            ''', task_id, user_id, username, email, gmail_password, twofa, app_pw, reward, datetime.utcnow(), release_at)
            await conn.execute("UPDATE users SET hold_balance = COALESCE(hold_balance, 0) + $1 WHERE user_id=$2", reward, user_id)
            await conn.execute(
                "INSERT INTO transactions (user_id, type, amount, note, hold_id) VALUES ($1, 'hold_add', $2, $3, $4)",
                user_id, reward, f"Task Gmail {email} #{task_id} submitted - {HOLD_DAYS} day hold", hold_id
            )
    invalidate_user_cache(user_id)
    return {'hold_id': hold_id, 'amount': reward, 'release_at': release_at, 'added_by': row['added_by'], 'ref_id': task_id}

async def is_sell_password_used(password: str) -> bool:
    """Gmail-style uniqueness check: a password that has already been used for any
    Sell Gmail submission can never be registered again."""
    if not password:
        return False
    async with db_pool.acquire() as conn:
        existing = await conn.fetchval(
            "SELECT id FROM holds WHERE kind='sell' AND gmail_password = $1 LIMIT 1",
            password
        )
    return existing is not None

async def finalize_sell_submission(user_id: int, username, sell_username: str, sell_password: str, twofa: str, app_pw: str, bulk_tag: str = None):
    """Called after the app password is verified: sell row created (pending_review), hold row created, Hold Balance += rate.
    bulk_tag (optional): shared tag (e.g. 'BULK-H73J') stamped on every item submitted together via Bulk Sell,
    so admins can identify which pending requests belong to the same batch - purely cosmetic, no security skipped."""
    release_at = datetime.utcnow() + timedelta(days=HOLD_DAYS)
    rate = GMAIL_SELL_RATE
    details = f"Username: {sell_username}\nPassword: {sell_password}"
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            dup = await conn.fetchval("SELECT id FROM pending_sells WHERE LOWER(details) LIKE $1 LIMIT 1", f"%{sell_username.lower()}%")
            if dup:
                return 'dup'
            dup_pass = await conn.fetchval("SELECT id FROM holds WHERE kind='sell' AND gmail_password = $1 LIMIT 1", sell_password)
            if dup_pass:
                return 'dup_pass'
            sell_id = await conn.fetchval(
                "INSERT INTO pending_sells (user_id, details, amount, bulk_tag) VALUES ($1, $2, $3, $4) RETURNING id",
                user_id, details, rate, bulk_tag
            )
            await ensure_user(user_id, conn=conn)
            hold_id = await conn.fetchval('''
                INSERT INTO holds (kind, ref_id, user_id, username, email, gmail_password, twofa_key, app_password, amount, status, created_at, release_at, bulk_tag)
                VALUES ('sell', $1, $2, $3, $4, $5, $6, $7, $8, 'pending_review', $9, $10, $11) RETURNING id
            ''', sell_id, user_id, username, sell_username, sell_password, twofa, app_pw, rate, datetime.utcnow(), release_at, bulk_tag)
            await conn.execute("UPDATE users SET hold_balance = COALESCE(hold_balance, 0) + $1 WHERE user_id=$2", rate, user_id)
            note = f"Sell Gmail {sell_username} #{sell_id} submitted - {HOLD_DAYS} day hold"
            if bulk_tag:
                note += f" [#{bulk_tag}]"
            await conn.execute(
                "INSERT INTO transactions (user_id, type, amount, note, hold_id) VALUES ($1, 'hold_add', $2, $3, $4)",
                user_id, rate, note, hold_id
            )
    invalidate_user_cache(user_id)
    return {'hold_id': hold_id, 'amount': rate, 'release_at': release_at, 'ref_id': sell_id}

async def alert_workers_new_sell():
    """Real-time stock alert to authorized workers (same behaviour as before)."""
    if not WORKER_BOT_TOKEN:
        return
    try:
        w_bot = make_bot(WORKER_BOT_TOKEN)
        async with db_pool.acquire() as conn:
            active_workers = await conn.fetch(
                "SELECT worker_id FROM worker_permissions WHERE is_active = TRUE AND can_sell_gmail = TRUE AND is_deleted = FALSE"
            )
            current_stock = await conn.fetchval("SELECT COUNT(*) FROM pending_sells WHERE status = 'pending_review' AND claimed_by IS NULL") or 0

        w_msg = (
            f'{E_BOX} <b>New Gmail Sell Request Stock!</b>\n\n'
            f'<tg-emoji emoji-id="5244837092042750681">📊</tg-emoji> <b>Available Stock:</b> <code>{current_stock}</code>\n\n'
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

async def alert_worker_task_submission(added_by_worker, task_id: int, email: str, password: str):
    """Real-time alert to the worker who added the task (same behaviour as before, no screenshot)."""
    if not WORKER_BOT_TOKEN or not added_by_worker or str(added_by_worker) == str(ADMIN_ID):
        return
    try:
        w_bot = make_bot(WORKER_BOT_TOKEN)
        worker_kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="Approve", icon_custom_emoji_id=ICON_OK, callback_data=f"w_ta:{task_id}", style="success"),
            InlineKeyboardButton(text="Decline", icon_custom_emoji_id=ICON_NO, callback_data=f"w_td:{task_id}", style="danger")
        ]])
        worker_msg_text = (
            f'{E_UP} <b>New Task Submission #{task_id}</b>\n\n'
            f'{E_MAIL} <b>Email:</b>\n<code>{_esc(email)}</code>\n\n'
            f'{E_KEY} <b>Password:</b>\n<code>{_esc(password)}</code>'
        )
        await w_bot.send_message(added_by_worker, worker_msg_text, reply_markup=worker_kb, parse_mode=ParseMode.HTML)
        await w_bot.session.close()
    except Exception as err:
        print(f"Error sending worker real-time submission alert: {err}")

# ============================================
# AIOHTTP SERVER (health check + webhook fast-path)
# ============================================

async def health(request):
    return web.Response(text="Bot is running!")

# ============================================
# STATES
# ============================================

class UserState(StatesGroup):
    selling = State()
    selling_username = State()
    selling_password = State()
    bulk_selling_input = State()
    setting_upi = State()
    setting_usdt = State()
    setting_ultra = State()
    submitting_task = State()
    submitting_twofa = State()
    submitting_apppass = State()
    waiting_for_support = State()

class AdminState(StatesGroup):
    waiting_for_task_reject_reason = State()
    waiting_for_sell_reject_reason = State()
    waiting_for_channel_link = State()
    waiting_for_payout_channel = State()
    waiting_for_add_balance = State()
    waiting_for_cut_balance = State()
    waiting_for_check_balance = State()
    waiting_for_ban_user = State()
    waiting_for_unban_user = State()
    waiting_for_add_task = State()
    waiting_for_bulk_add_task = State()
    waiting_for_bulk_add_passwords = State()
    waiting_for_hold_reject_reason = State()
    waiting_for_remove_task = State()
    waiting_for_broadcast = State()
    waiting_for_broadcast_target = State()
    waiting_for_broadcast_buttons = State()
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
    waiting_for_emoji_id = State()
    waiting_for_ref_commission = State()
    waiting_for_ref_lb_rewards = State()
    waiting_for_ref_lb_end = State()
    waiting_for_app_pass_not_found = State()
    waiting_for_twofa_not_verified = State()

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
        ssl=False,
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
        await conn.execute("ALTER TABLE pending_sells ADD COLUMN IF NOT EXISTS bulk_tag TEXT DEFAULT NULL")

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

        # ---- NEW: hold system / specific task passwords ----
        await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS hold_balance DOUBLE PRECISION DEFAULT 0")
        await conn.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS fixed_password TEXT DEFAULT NULL")
        await conn.execute("ALTER TABLE dustbin_tasks ADD COLUMN IF NOT EXISTS fixed_password TEXT DEFAULT NULL")
        await conn.execute("ALTER TABLE transactions ADD COLUMN IF NOT EXISTS hold_id INTEGER DEFAULT NULL")
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS holds (
                id SERIAL PRIMARY KEY,
                kind TEXT NOT NULL,
                ref_id INTEGER NOT NULL,
                user_id BIGINT NOT NULL,
                username TEXT,
                email TEXT,
                gmail_password TEXT,
                twofa_key TEXT,
                app_password TEXT,
                amount DOUBLE PRECISION DEFAULT 0,
                status TEXT DEFAULT 'pending_review',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                release_at TIMESTAMP,
                holder_at TIMESTAMP,
                closed_at TIMESTAMP,
                reject_reason TEXT
            )
        ''')
        await conn.execute("ALTER TABLE holds ADD COLUMN IF NOT EXISTS bulk_tag TEXT DEFAULT NULL")
        await conn.execute("CREATE INDEX IF NOT EXISTS ix_holds_status_kind ON holds(status, kind)")
        await conn.execute("CREATE INDEX IF NOT EXISTS ix_holds_user ON holds(user_id)")
        # Speeds up the sell-password uniqueness check (is_sell_password_used)
        await conn.execute("CREATE INDEX IF NOT EXISTS ix_holds_sell_password ON holds(gmail_password) WHERE kind='sell'")
        await conn.execute("UPDATE users SET hold_balance = 0 WHERE hold_balance IS NULL")

def _load_media_json(raw: str) -> list:
    """Parses a JSON-encoded list of {"type": "photo"|"video", "file_id": str} from bot_settings."""
    if not raw:
        return []
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict) and item.get("file_id") and item.get("type") in ("photo", "video")]
    except Exception:
        pass
    return []

async def load_settings_and_cache():
    global BANNED_USERS_CACHE, MUST_JOIN_CHANNEL, PAYOUT_CHANNEL, BOT_USERNAME, BOT_STATUS, BOT_OFF_MESSAGE, REF_STATUS, ULTRA_STATUS, SINGLE_TASK_STATUS, SELL_GMAIL_STATUS, EMAILABLE_API_KEY, VALIDATOR_ENABLED, VALIDATOR_PROVIDER, ADMIN_ID
    global DEFAULT_TASK_RATE, GMAIL_SELL_RATE, MIN_WITHDRAWAL_AMT, DEFAULT_TASK_PASS, DEFAULT_TASK_PASS_STATUS, UPI_FEES, USDT_FEES, ULTRA_FEES, ULTRA_TOKEN, ULTRA_KEY
    global TWOFA_TASK_STATUS, TWOFA_SELL_STATUS
    global TASK_VIDEO_LINK, SELL_VIDEO_LINK, HOWTO_VIDEO_LINK
    global APP_PASS_NOT_FOUND_TEXT, APP_PASS_NOT_FOUND_MEDIA, TWOFA_NOT_VERIFIED_TEXT, TWOFA_NOT_VERIFIED_MEDIA
    global REFERRAL_COMMISSION_PCT, REF_LB_REWARDS, REF_LB_START, REF_LB_END

    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT user_id FROM banned_users")
        BANNED_USERS_CACHE = {r['user_id'] for r in rows}

        channel_val = await conn.fetchval("SELECT value FROM bot_settings WHERE key='must_join_channel'")
        MUST_JOIN_CHANNEL = channel_val if channel_val else None

        payout_channel_val = await conn.fetchval("SELECT value FROM bot_settings WHERE key='payout_channel'")
        if payout_channel_val and payout_channel_val != "off":
            # Numeric channel IDs (private channels, e.g. -1001234567890) are stored as text -
            # convert back to int so bot.send_message()/get_chat_member() receive the right type.
            PAYOUT_CHANNEL = int(payout_channel_val) if payout_channel_val.lstrip('-').isdigit() else payout_channel_val
        else:
            PAYOUT_CHANNEL = None

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

        twofa_task_val = await conn.fetchval("SELECT value FROM bot_settings WHERE key='twofa_task_status'")
        TWOFA_TASK_STATUS = (twofa_task_val != 'off')

        twofa_sell_val = await conn.fetchval("SELECT value FROM bot_settings WHERE key='twofa_sell_status'")
        TWOFA_SELL_STATUS = (twofa_sell_val != 'off')

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

        apnf_text = await conn.fetchval("SELECT value FROM bot_settings WHERE key='app_pass_not_found_text'")
        APP_PASS_NOT_FOUND_TEXT = apnf_text if apnf_text else None

        apnf_media = await conn.fetchval("SELECT value FROM bot_settings WHERE key='app_pass_not_found_media'")
        APP_PASS_NOT_FOUND_MEDIA = _load_media_json(apnf_media)

        tnv_text = await conn.fetchval("SELECT value FROM bot_settings WHERE key='twofa_not_verified_text'")
        TWOFA_NOT_VERIFIED_TEXT = tnv_text if tnv_text else None

        tnv_media = await conn.fetchval("SELECT value FROM bot_settings WHERE key='twofa_not_verified_media'")
        TWOFA_NOT_VERIFIED_MEDIA = _load_media_json(tnv_media)

        pct_val = await conn.fetchval("SELECT value FROM bot_settings WHERE key='ref_commission_pct'")
        if pct_val:
            try:
                REFERRAL_COMMISSION_PCT = float(pct_val)
            except ValueError:
                pass

        lb_rewards_val = await conn.fetchval("SELECT value FROM bot_settings WHERE key='ref_lb_rewards'")
        if lb_rewards_val is not None:
            try:
                REF_LB_REWARDS = parse_ref_rewards(lb_rewards_val)
            except ValueError:
                pass

        lb_start_val = await conn.fetchval("SELECT value FROM bot_settings WHERE key='ref_lb_start'")
        if lb_start_val:
            try:
                REF_LB_START = datetime.fromisoformat(lb_start_val)
            except ValueError:
                REF_LB_START = None

        lb_end_val = await conn.fetchval("SELECT value FROM bot_settings WHERE key='ref_lb_end'")
        if lb_end_val:
            try:
                REF_LB_END = datetime.fromisoformat(lb_end_val)
            except ValueError:
                REF_LB_END = None
        else:
            REF_LB_END = None

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
            "SELECT balance, COALESCE(hold_balance, 0) AS hold_balance, upi, usdt_address, ultra_number, notifications_enabled, currency, referred_by, referral_earnings FROM users WHERE user_id=$1",
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
    kb.button(text="Get Task", icon_custom_emoji_id="5807510737332998730", callback_data="menu_get_task")
    kb.button(text="Balance", icon_custom_emoji_id="5417924076503062111", callback_data="menu_balance")
    kb.button(text="Sell Gmail", icon_custom_emoji_id="5377548235709619284", callback_data="menu_sell_gmail")
    kb.button(text="History", icon_custom_emoji_id="5440410042773824003", callback_data="menu_history")
    kb.button(text="Referrals", icon_custom_emoji_id="5391292736647209211", callback_data="menu_referrals")
    kb.button(text="My Accounts", icon_custom_emoji_id="6300651782678781823", callback_data="menu_my_accounts")
    kb.button(text="Settings", icon_custom_emoji_id="5893161718179173515", callback_data="menu_settings")
    kb.button(text="Support", icon_custom_emoji_id="5471960722206366390", callback_data="menu_support")
    if HOWTO_VIDEO_LINK:
        kb.button(**bk("🎬", "How To Use Bot"), url=HOWTO_VIDEO_LINK)
        kb.adjust(2, 2, 2, 1, 2)
    else:
        kb.adjust(2, 2, 2, 1, 1)
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
    kb.button(**bk("🏆", "Referral Leaderboard"), callback_data="ref_leaderboard", style="success")
    kb.button(
        text="Back", icon_custom_emoji_id="5875082500023258804",
        callback_data="menu_back"
    )
    kb.adjust(2, 1, 1)
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
    kb.button(text="Holder", icon_custom_emoji_id="5472027899789843495", style="primary")

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

    kb.button(text="Bot Status: ON" if BOT_STATUS else "Bot Status: OFF",
              icon_custom_emoji_id=(pe_id("🟢") if BOT_STATUS else pe_id("🔴")) or None,
              style="danger" if BOT_STATUS else "success")

    kb.button(text="Ref Status: ON" if REF_STATUS else "Ref Status: OFF",
              icon_custom_emoji_id=(pe_id("🟢") if REF_STATUS else pe_id("🔴")) or None,
              style="success" if REF_STATUS else "danger")

    kb.button(text="Validator", icon_custom_emoji_id="5893161718179173515", style="primary")
    kb.button(text="Ultra Status: ON" if ULTRA_STATUS else "Ultra Status: OFF",
              icon_custom_emoji_id=(pe_id("🟢") if ULTRA_STATUS else pe_id("🔴")) or None,
              style="success" if ULTRA_STATUS else "danger")

    kb.button(text="Dustbin", icon_custom_emoji_id="5309832892262654231", style="danger")
    kb.button(text="Videos", icon_custom_emoji_id="5305265301917549162", style="primary")

    kb.button(text="Manage Workers", icon_custom_emoji_id="5264713049637409446", style="primary")
    kb.button(text="Transfer Admin", icon_custom_emoji_id="5217822164362739968", style="danger")

    kb.button(text="Referral Panel", icon_custom_emoji_id="5391292736647209211", style="success")
    kb.button(text="Payout Channel", icon_custom_emoji_id="5444856076954520455", style="primary")
    kb.button(text="Main Menu", icon_custom_emoji_id="5416041192905265756", style="primary")

    kb.adjust(3)
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

    pass_mode_text = f"5. Password Mode: {'Fixed (Default)' if DEFAULT_TASK_PASS_STATUS else 'Random'}"
    kb.button(
        text=pass_mode_text, icon_custom_emoji_id=pe_id("🔑") or None,
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

    single_task_btn_text = f"8. Single Tasks: {'ON' if SINGLE_TASK_STATUS else 'OFF'}"
    kb.button(
        text=single_task_btn_text, icon_custom_emoji_id=pe_id("✍️") or None,
        callback_data="admin_toggle_single_task",
        style="success" if SINGLE_TASK_STATUS else "danger"
    )

    sell_gmail_btn_text = f"9. Sell Gmail: {'ON' if SELL_GMAIL_STATUS else 'OFF'}"
    kb.button(
        text=sell_gmail_btn_text, icon_custom_emoji_id=pe_id("📨") or None,
        callback_data="admin_toggle_sell_gmail",
        style="success" if SELL_GMAIL_STATUS else "danger"
    )

    twofa_task_btn_text = f"10. Task 2FA: {'ON' if TWOFA_TASK_STATUS else 'OFF'}"
    kb.button(
        text=twofa_task_btn_text, icon_custom_emoji_id=pe_id("🔐") or None,
        callback_data="admin_toggle_twofa_task",
        style="success" if TWOFA_TASK_STATUS else "danger"
    )

    twofa_sell_btn_text = f"11. Sell 2FA: {'ON' if TWOFA_SELL_STATUS else 'OFF'}"
    kb.button(
        text=twofa_sell_btn_text, icon_custom_emoji_id=pe_id("🔐") or None,
        callback_data="admin_toggle_twofa_sell",
        style="success" if TWOFA_SELL_STATUS else "danger"
    )

    kb.adjust(2, 2, 2, 2, 1, 2)
    return kb.as_markup()

def get_validator_admin_inline_keyboard():
    kb = InlineKeyboardBuilder()
    status_toggle = bk("🔴", "Deactivate") if VALIDATOR_ENABLED else bk("🟢", "Activate")
    status_style = "danger" if VALIDATOR_ENABLED else "success"

    if VALIDATOR_PROVIDER == "netnit":
        kb.button(
            text="Change Key", icon_custom_emoji_id="6005570495603282482",
            callback_data="admin_validator_change_key",
            style="primary"
        )
    kb.button(
        **bk("🔄", "Change Provider"),
        callback_data="admin_validator_change_provider",
        style="primary"
    )
    kb.button(
        **status_toggle,
        callback_data="admin_validator_toggle_status",
        style=status_style
    )
    if VALIDATOR_PROVIDER == "netnit":
        kb.adjust(2, 1)
    else:
        kb.adjust(2)
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
            **bk("✔️", "Submit"),
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
            elif status_raw == 'in_holder':
                status_str = "🟣 In Holder"
            elif status_raw == 'verifying':
                status_str = "🔵 Verifying"
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
            WHERE t.status IN ('available', 'assigned', 'verifying')
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
            elif raw_type == 'hold_add':
                tx_type = "HOLD +"
            elif raw_type == 'hold_remove':
                tx_type = "HOLD -"
            else:
                tx_type = raw_type.upper()

            date_fmt = to_ist(tx['created_at']).strftime("%b %d, %Y %I:%M %p")
            note_str = f"\n📝 <i>{_esc(tx['note']) if raw_type in ('hold_add', 'hold_remove') else tx['note']}</i>" if tx['note'] else ""

            type_emoji = E_TIME if raw_type in ('hold_add', 'hold_remove') else (E_GREEN if amt >= 0 else E_RED)
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
        try:
            await event.answer(BOT_OFF_MESSAGE, parse_mode=ParseMode.HTML)
        except TelegramBadRequest:
            await event.answer(plain_text(BOT_OFF_MESSAGE))
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
            await event.answer(plain_text(BOT_OFF_MESSAGE), show_alert=True)
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

# ============================================
# DIRECT LINKS (DEEP LINKS): t.me/<bot>?start=<action>[_<referrer_id>]
# Examples:  ?start=sell   ?start=task   ?start=ref   ?start=sell_123456789
# ============================================

# link keyword -> internal action name
DEEPLINK_ALIASES = {
    "sell": "sell", "gmail": "sell", "sellgmail": "sell",
    "task": "task", "tasks": "task", "gettask": "task",
    "ref": "ref", "referral": "ref", "referrals": "ref", "invite": "ref",
    "lb": "leaderboard", "leaderboard": "leaderboard",
    "balance": "balance", "wallet": "balance",
    "history": "history",
    "accounts": "accounts", "myaccounts": "accounts",
    "settings": "settings",
    "support": "support",
    "menu": "menu", "home": "menu",
}

class _DeepLinkCall:
    """Minimal stand-in for a CallbackQuery so the existing menu handlers can be reused by a /start link."""
    def __init__(self, user, message):
        self.from_user = user
        self.message = message
        self.data = None

    async def answer(self, text=None, show_alert=False, **kwargs):
        # A real callback would show an alert popup; for a link we send it as a normal message instead.
        if text and show_alert:
            try:
                await self.message.answer(text)
            except Exception:
                pass
        return True

def parse_start_payload(payload: str):
    """Returns (referrer_id, deep_action). Supports '123456', 'sell', 'sell_123456'."""
    payload = (payload or "").strip()
    if not payload:
        return None, None
    if payload.isdigit():
        return int(payload), None
    action_raw, _, rest = payload.partition("_")
    action = DEEPLINK_ALIASES.get(action_raw.lower())
    if not action:
        return None, None
    referrer_id = int(rest) if rest.isdigit() else None
    return referrer_id, action

def build_direct_link(action: str, user_id: int = None) -> str:
    suffix = f"_{user_id}" if user_id else ""
    return f"https://t.me/{BOT_USERNAME}?start={action}{suffix}"

async def _dl_history(call, state: FSMContext):
    await state.clear()
    text, reply_markup = await render_transaction_history_page(call.from_user.id, page=1, is_admin=False)
    try:
        await call.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            await call.message.answer(text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
    await state.update_data(last_menu_msg_id=call.message.message_id)

async def _dl_menu(call, state: FSMContext):
    await state.clear()
    text = (
        '<tg-emoji emoji-id=\"5195448447062251797\">👋</tg-emoji> <b>Welcome back.</b>\n\n'
        'Choose an option from the menu below:'
    )
    try:
        await call.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=get_main_menu_keyboard())
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            await call.message.answer(text, parse_mode=ParseMode.HTML, reply_markup=get_main_menu_keyboard())
    await state.update_data(last_menu_msg_id=call.message.message_id)

def _deeplink_handler(action: str):
    # Resolved at call time because the target handlers are defined further down the file.
    return {
        "sell": cb_sell_gmail,
        "task": cb_get_task,
        "ref": cb_referrals,
        "leaderboard": cb_ref_leaderboard,
        "balance": cb_balance,
        "history": _dl_history,
        "accounts": cb_my_accounts,
        "settings": cb_settings,
        "support": cb_support_start,
        "menu": _dl_menu,
    }.get(action)

async def open_deep_link(action: str, message: Message, state: FSMContext) -> bool:
    handler = _deeplink_handler(action)
    if handler is None:
        return False
    opening = None
    try:
        opening = await message.answer("⏳ Opening...")
        fake_call = _DeepLinkCall(message.from_user, opening)
        await handler(fake_call, state)
        return True
    except Exception as e:
        print(f"Error opening deep link '{action}': {e}")
        if opening is not None:
            try:
                await opening.delete()
            except Exception:
                pass
        return False

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

        referrer_id, deep_action = (None, None)
        if command and command.args:
            referrer_id, deep_action = parse_start_payload(command.args)

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

        if deep_action:
            if is_new_user:
                # Greet new users first (no keyboard - the requested page opens right below)
                await message.answer(text, parse_mode=ParseMode.HTML)
            if await open_deep_link(deep_action, message, state):
                return

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

# ============================================
# REFERRAL SYSTEM: COMMISSION + LEADERBOARD
# ============================================

E_TROPHY = tg("6183525361637136222", "🏆")
E_USERS = tg("5391292736647209211", "👥")
E_CROWN = tg("5217822164362739968", "👑")
E_LINK = tg("5271604874419647061", "🔗")
E_CASH = tg("5039789890133296083", "💵")
E_BOLT = tg("5195033767969839232", "⚡")
E_STATS = tg("5244837092042750681", "📊")
E_TARGET = tg("5310278924616356636", "🎯")

# Ranks users by commission earned (type='referral') inside the current period.
_REF_LB_SQL = '''
SELECT user_id, earned, cnt, rnk FROM (
    SELECT user_id,
           SUM(amount) AS earned,
           COUNT(*) AS cnt,
           ROW_NUMBER() OVER (ORDER BY SUM(amount) DESC, COUNT(*) DESC, MIN(id) ASC) AS rnk
    FROM transactions
    WHERE type = 'referral'
      AND ($1::timestamp IS NULL OR created_at >= $1::timestamp)
      AND ($2::timestamp IS NULL OR created_at < $2::timestamp)
    GROUP BY user_id
) t
'''

def parse_ref_rewards(raw: str):
    out = []
    for tok in re.split(r"[\s,]+", (raw or "").strip()):
        if tok:
            out.append(float(tok))
    return out

def fmt_pct(p: float) -> str:
    return f"{p:g}"

def mask_user_id(uid: int) -> str:
    s = str(uid)
    if len(s) <= 5:
        return s[:1] + "•••"
    return f"{s[:3]}•••{s[-2:]}"

def ref_period_label() -> str:
    return f"since {fmt_dt(REF_LB_START)} IST" if REF_LB_START else "all time"

def ref_end_label() -> str:
    if not REF_LB_END:
        return "No end date set"
    if REF_LB_END <= datetime.utcnow():
        return f"{fmt_dt(REF_LB_END)} IST (round ended - prizes will be paid soon)"
    return f"{fmt_dt(REF_LB_END)} IST ({fmt_left(REF_LB_END)})"

def parse_ref_end(raw: str):
    """Admin input (India time) -> naive UTC datetime. Accepts '3d', '12h' or 'DD-MM-YYYY HH:MM' (24h) / 'DD-MM-YYYY'."""
    s_ = (raw or "").strip().lower()
    m = re.fullmatch(r"(\d+)\s*([dh])", s_)
    if m:
        n = int(m.group(1))
        return datetime.utcnow() + (timedelta(days=n) if m.group(2) == 'd' else timedelta(hours=n))
    for fmt in ("%d-%m-%Y %H:%M", "%d/%m/%Y %H:%M", "%d-%m-%Y %I:%M %p", "%d-%m-%Y", "%d/%m/%Y"):
        try:
            d = datetime.strptime((raw or "").strip().upper(), fmt)
        except ValueError:
            continue
        if fmt in ("%d-%m-%Y", "%d/%m/%Y"):
            d = d.replace(hour=23, minute=59)
        return d.replace(tzinfo=IST).astimezone(timezone.utc).replace(tzinfo=None)
    raise ValueError("bad date")

def ref_prize_for_rank(rank: int):
    if 1 <= rank <= len(REF_LB_REWARDS) and REF_LB_REWARDS[rank - 1] > 0:
        return REF_LB_REWARDS[rank - 1]
    return None

async def fetch_ref_leaderboard(limit: int = 10):
    async with db_pool.acquire() as conn:
        return await conn.fetch(_REF_LB_SQL + " WHERE rnk <= $3 ORDER BY rnk", REF_LB_START, REF_LB_END, limit)

async def fetch_ref_rank(user_id: int):
    async with db_pool.acquire() as conn:
        return await conn.fetchrow(_REF_LB_SQL + " WHERE user_id = $3", REF_LB_START, REF_LB_END, user_id)

async def _edit_or_send(call: CallbackQuery, text: str, kb):
    try:
        await call.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            await call.message.answer(text, parse_mode=ParseMode.HTML, reply_markup=kb)

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
        approved_ref_accounts = await conn.fetchval(
            "SELECT COUNT(*) FROM transactions WHERE user_id = $1 AND type = 'referral'", user_id
        ) or 0

    total_earnings = user_data['referral_earnings'] if user_data else 0.0
    formatted_earnings = format_currency(total_earnings, curr)
    invite_link = f"https://t.me/{BOT_USERNAME}?start={user_id}"

    pct = REFERRAL_COMMISSION_PCT
    ex_sell = format_currency(GMAIL_SELL_RATE * pct / 100.0, curr)
    ex_task = format_currency(DEFAULT_TASK_RATE * pct / 100.0, curr)

    my_rank = await fetch_ref_rank(user_id)
    if my_rank:
        rank_line = f"{E_TROPHY} <b>Your leaderboard rank:</b> #{my_rank['rnk']}\n"
    else:
        rank_line = f"{E_TROPHY} <b>Your leaderboard rank:</b> Not ranked yet\n"

    prize_lines = ""
    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    for i, amt in enumerate(REF_LB_REWARDS, start=1):
        if amt > 0:
            prize_lines += f"{medals.get(i, '🏅')} Rank {i}: <b>{format_currency(amt, curr)}</b>\n"
    prize_block = (
        f"\n{E_CROWN} <b>Leaderboard Prizes</b>\n{prize_lines}"
        f"{E_CAL} <b>Round ends:</b> {ref_end_label()}\n"
        f"<i>Top referrers win cash prizes - open the leaderboard below!</i>\n"
    ) if prize_lines else ""

    text = (
        f'{E_USERS} <b>My Referrals</b>\n'
        f'━━━━━━━━━━━━━━━━━━\n'
        f'{E_MONEY} <b>Total earnings:</b> {formatted_earnings}\n'
        f'{E_USERS} <b>Invited users:</b> {invited_users_count}\n'
        f'{E_OK} <b>Approved referral accounts:</b> {approved_ref_accounts}\n'
        f'{rank_line}'
        f'━━━━━━━━━━━━━━━━━━\n'
        f'{E_BOLT} <b>Earn {fmt_pct(pct)}% Lifetime Commission!</b>\n'
        f'Every time someone you invited gets a Gmail account accepted, you earn <b>{fmt_pct(pct)}%</b> of its reward - '
        f'for life. No limit, it never expires.\n\n'
        f'{E_CASH} <b>Example Earnings</b>\n'
        f'{E_TARGET} Sell Gmail accepted: <b>+{ex_sell}</b> for you\n'
        f'{E_TARGET} Task Gmail accepted: <b>+{ex_task}</b> for you\n'
        f'{prize_block}\n'
        f'{E_LINK} <b>Your invite link:</b>\n'
        f'<code>{invite_link}</code>'
    )

    await _edit_or_send(call, text, get_referral_inline_keyboard(user_id))
    await state.update_data(last_menu_msg_id=call.message.message_id)

async def render_ref_leaderboard(user_id: int):
    user_data = await get_user_data(user_id)
    curr = user_data['currency'] if user_data else "USD"

    rows = await fetch_ref_leaderboard(10)
    my_rank = await fetch_ref_rank(user_id)

    text = (
        f'{E_TROPHY} <b>Referral Leaderboard</b>\n'
        f'━━━━━━━━━━━━━━━━━━\n'
        f'{E_CAL} <b>Started:</b> {ref_period_label()}\n'
        f'{E_TIME} <b>Ends:</b> {ref_end_label()}\n'
        f'{E_MONEY} <b>Ranked by:</b> commission earned from referrals\n'
        f'━━━━━━━━━━━━━━━━━━\n\n'
    )

    if not rows:
        text += f'{E_EMPTY} <b>No referral earnings yet in this period.</b>\nBe the first to take the #1 spot! {E_ROCKET}\n'
    else:
        medals = {1: "🥇", 2: "🥈", 3: "🥉"}
        for r in rows:
            rank = int(r['rnk'])
            badge = medals.get(rank, f"<b>{rank}.</b>")
            you = f" ← <b>You</b>" if r['user_id'] == user_id else ""
            line = (
                f"{badge} <code>{mask_user_id(r['user_id'])}</code> - "
                f"<b>{format_currency(float(r['earned']), curr)}</b> ({int(r['cnt'])} acc){you}\n"
            )
            prize = ref_prize_for_rank(rank)
            if prize:
                line += f"      🎁 Prize: <b>{format_currency(prize, curr)}</b>\n"
            text += line

    text += "\n━━━━━━━━━━━━━━━━━━\n"
    if my_rank:
        text += (
            f'{E_USER} <b>Your rank:</b> #{int(my_rank["rnk"])} • '
            f'<b>{format_currency(float(my_rank["earned"]), curr)}</b> earned\n'
        )
    else:
        text += f'{E_USER} <b>You are not ranked yet</b> - invite friends and climb the board! {E_ROCKET}\n'

    prizes_total = len([a for a in REF_LB_REWARDS if a > 0])
    if prizes_total:
        text += f'{E_CROWN} <b>Prizes for the Top {prizes_total}</b> - paid by the admin at the end of each round.'

    kb = InlineKeyboardBuilder()
    kb.button(**bk("🔄", "Refresh"), callback_data="ref_leaderboard", style="primary")
    kb.button(text="Back", icon_custom_emoji_id="5875082500023258804", callback_data="menu_referrals")
    kb.adjust(2)
    return text, kb.as_markup()

@dp.callback_query(F.data == "ref_leaderboard")
async def cb_ref_leaderboard(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.clear()
    text, kb = await render_ref_leaderboard(call.from_user.id)
    await _edit_or_send(call, text, kb)
    await state.update_data(last_menu_msg_id=call.message.message_id)

# ---------- ADMIN: REFERRAL PANEL ----------

async def render_admin_ref_panel():
    rows = await fetch_ref_leaderboard(5)

    if REF_LB_REWARDS:
        reward_lines = "\n".join(
            f"  #{i}: ₹{amt:.2f}" for i, amt in enumerate(REF_LB_REWARDS, start=1)
        )
    else:
        reward_lines = "  <i>No prizes set</i>"

    if rows:
        top_lines = "\n".join(
            f"  {int(r['rnk'])}. <code>{r['user_id']}</code> - ₹{float(r['earned']):.2f} ({int(r['cnt'])} acc)"
            for r in rows
        )
    else:
        top_lines = "  <i>No referral earnings in this period yet</i>"

    status_line = f"{E_GREEN} ON" if REF_STATUS else f"{E_RED} OFF (commissions are silently disabled)"

    text = (
        f'{E_USERS} <b>Referral Panel</b>\n'
        f'━━━━━━━━━━━━━━━━━━\n'
        f'{E_BOLT} <b>Referral Status:</b> {status_line}\n'
        f'{E_MONEY} <b>Commission:</b> <code>{fmt_pct(REFERRAL_COMMISSION_PCT)}%</code>\n'
        f'{E_CAL} <b>Leaderboard Started:</b> {ref_period_label()}\n'
        f'{E_TIME} <b>Leaderboard Ends:</b> {ref_end_label()}\n\n'
        f'{E_TROPHY} <b>Leaderboard Prizes:</b>\n{reward_lines}\n\n'
        f'{E_STATS} <b>Current Top 5:</b>\n{top_lines}\n'
        f'━━━━━━━━━━━━━━━━━━\n'
        f'<i>Pay Rewards &amp; Reset credits the prizes to the winners and starts a new round.</i>'
    )

    kb = InlineKeyboardBuilder()
    kb.button(**bk("💰", "Change Commission"), callback_data="admin_ref_commission", style="primary")
    kb.button(**bk("🏆", "Leaderboard Rewards"), callback_data="admin_ref_rewards", style="primary")
    kb.button(**bk("⏰", "Set End Date & Time"), callback_data="admin_ref_end", style="primary")
    kb.button(**bk("🎉", "Pay Rewards & Reset"), callback_data="admin_ref_lb_pay", style="success")
    kb.button(**bk("🔄", "Refresh"), callback_data="admin_ref_refresh", style="primary")
    kb.button(text="Back", icon_custom_emoji_id="5875082500023258804", callback_data="back_admin_menu")
    kb.adjust(2, 1, 1, 2)
    return text, kb.as_markup()

@dp.message(F.text == "Referral Panel", StateFilter("*"))
async def admin_btn_referral_panel(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.clear()
    text, kb = await render_admin_ref_panel()
    await message.answer(text, parse_mode=ParseMode.HTML, reply_markup=kb)

@dp.callback_query(F.data == "admin_ref_refresh")
async def cb_admin_ref_refresh(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    await call.answer()
    await state.clear()
    text, kb = await render_admin_ref_panel()
    await _edit_or_send(call, text, kb)

@dp.callback_query(F.data == "admin_ref_commission")
async def cb_admin_ref_commission(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    await call.answer()
    await state.set_state(AdminState.waiting_for_ref_commission)
    await call.message.answer(
        f'{E_MONEY} <b>Current Referral Commission:</b> <code>{fmt_pct(REFERRAL_COMMISSION_PCT)}%</code>\n\n'
        f'Send the new commission percentage (0 - 100), e.g. <code>30</code>:',
        parse_mode=ParseMode.HTML
    )

@dp.message(AdminState.waiting_for_ref_commission, ~F.text.startswith("/"), ~F.text.in_(MENU_BUTTONS))
async def process_ref_commission_step(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    global REFERRAL_COMMISSION_PCT
    try:
        new_pct = float(message.text.strip().replace("%", ""))
        if new_pct < 0 or new_pct > 100:
            raise ValueError("range")
        REFERRAL_COMMISSION_PCT = new_pct
        async with db_pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO bot_settings (key, value) VALUES ('ref_commission_pct', $1) ON CONFLICT (key) DO UPDATE SET value = $1",
                str(new_pct)
            )
        await message.answer(
            f'{E_OK} <b>Referral commission updated to</b> <code>{fmt_pct(new_pct)}%</code>',
            parse_mode=ParseMode.HTML,
            reply_markup=get_admin_menu_keyboard()
        )
    except ValueError:
        await message.answer("❌ Invalid value. Send a number between 0 and 100.", reply_markup=get_admin_menu_keyboard())
    await state.clear()

@dp.callback_query(F.data == "admin_ref_rewards")
async def cb_admin_ref_rewards(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    await call.answer()
    await state.set_state(AdminState.waiting_for_ref_lb_rewards)
    current = ", ".join(f"₹{a:g}" for a in REF_LB_REWARDS) if REF_LB_REWARDS else "None"
    await call.message.answer(
        f'{E_TROPHY} <b>Leaderboard Rewards</b>\n\n'
        f'<b>Current:</b> {current}\n\n'
        f'Send the prize amounts in INR, one per line (rank 1 first, max 10 ranks):\n'
        f'<code>100\n50\n25</code>\n\n'
        f'<i>Send</i> <code>none</code> <i>to disable leaderboard prizes.</i>',
        parse_mode=ParseMode.HTML
    )

@dp.message(AdminState.waiting_for_ref_lb_rewards, ~F.text.startswith("/"), ~F.text.in_(MENU_BUTTONS))
async def process_ref_lb_rewards_step(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    global REF_LB_REWARDS
    raw = message.text.strip()
    try:
        if raw.lower() in ("none", "off", "0"):
            new_rewards = []
        else:
            new_rewards = parse_ref_rewards(raw)
            if not new_rewards or len(new_rewards) > 10 or any(a < 0 for a in new_rewards):
                raise ValueError("bad")
        REF_LB_REWARDS = new_rewards
        async with db_pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO bot_settings (key, value) VALUES ('ref_lb_rewards', $1) ON CONFLICT (key) DO UPDATE SET value = $1",
                ",".join(f"{a:g}" for a in new_rewards)
            )
        shown = "\n".join(f"#{i}: ₹{a:.2f}" for i, a in enumerate(new_rewards, start=1)) if new_rewards else "Disabled"
        await message.answer(
            f'{E_OK} <b>Leaderboard rewards updated!</b>\n\n{shown}',
            parse_mode=ParseMode.HTML,
            reply_markup=get_admin_menu_keyboard()
        )
    except ValueError:
        await message.answer(
            "❌ Invalid format. Send up to 10 numbers (one per line), e.g.\n100\n50\n25",
            reply_markup=get_admin_menu_keyboard()
        )
    await state.clear()

@dp.callback_query(F.data == "admin_ref_end")
async def cb_admin_ref_end(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    await call.answer()
    await state.set_state(AdminState.waiting_for_ref_lb_end)
    await call.message.answer(
        f'{E_TIME} <b>Leaderboard End Date &amp; Time (India time)</b>\n\n'
        f'<b>Current:</b> {ref_end_label()}\n'
        f'<b>Now (IST):</b> {now_ist().strftime("%d-%m-%Y %H:%M")}\n\n'
        f'Send when this round should end, in <b>India time</b>:\n'
        f'<code>25-12-2026 21:30</code>  (24-hour format)\n'
        f'<code>25-12-2026</code>  (ends 11:59 PM)\n'
        f'<code>3d</code> or <code>12h</code>  (from now)\n\n'
        f'<i>Send</i> <code>none</code> <i>to remove the end date.</i>\n'
        f'<i>When the time is reached, standings freeze and you get an alert to pay the winners.</i>',
        parse_mode=ParseMode.HTML
    )

@dp.message(AdminState.waiting_for_ref_lb_end, ~F.text.startswith("/"), ~F.text.in_(MENU_BUTTONS))
async def process_ref_lb_end_step(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    global REF_LB_END
    raw = message.text.strip()
    try:
        async with db_pool.acquire() as conn:
            if raw.lower() in ("none", "off", "clear", "remove"):
                REF_LB_END = None
                await conn.execute("DELETE FROM bot_settings WHERE key='ref_lb_end'")
                await message.answer(
                    f'{E_OK} <b>Leaderboard end date removed.</b> The round now runs until you pay & reset.',
                    parse_mode=ParseMode.HTML, reply_markup=get_admin_menu_keyboard()
                )
            else:
                new_end = parse_ref_end(raw)
                if new_end <= datetime.utcnow():
                    raise ValueError("past")
                REF_LB_END = new_end
                await conn.execute(
                    "INSERT INTO bot_settings (key, value) VALUES ('ref_lb_end', $1) ON CONFLICT (key) DO UPDATE SET value = $1",
                    new_end.isoformat()
                )
                await message.answer(
                    f'{E_OK} <b>Leaderboard end set:</b> {ref_end_label()}',
                    parse_mode=ParseMode.HTML, reply_markup=get_admin_menu_keyboard()
                )
    except ValueError:
        await message.answer(
            "❌ Invalid or past time. Use e.g. 25-12-2026 21:30 (India time), 3d, 12h, or none.",
            reply_markup=get_admin_menu_keyboard()
        )
    await state.clear()

async def pay_ref_leaderboard_rewards():
    """Credits the prizes to the current winners, then starts a new period.
    Returns a list of (user_id, rank, prize), [] if nobody qualifies, or None if a payout is already running."""
    global REF_LB_START, REF_LB_END, REF_LB_PAYING
    if REF_LB_PAYING:
        return None
    REF_LB_PAYING = True
    try:
        paid = []
        new_start = None
        async with db_pool.acquire() as conn:
            async with conn.transaction():
                rows = await conn.fetch(
                    _REF_LB_SQL + " WHERE rnk <= $3 ORDER BY rnk", REF_LB_START, REF_LB_END, len(REF_LB_REWARDS)
                )
                if not rows:
                    return []
                for r in rows:
                    rank = int(r['rnk'])
                    prize = ref_prize_for_rank(rank)
                    if not prize:
                        continue
                    uid = r['user_id']
                    await ensure_user(uid, conn=conn)
                    await conn.execute("UPDATE users SET balance = balance + $1 WHERE user_id=$2", prize, uid)
                    await conn.execute(
                        "INSERT INTO transactions (user_id, type, amount, note) VALUES ($1, 'ref_lb_reward', $2, $3)",
                        uid, prize, f"Referral Leaderboard Rank #{rank} prize"
                    )
                    paid.append((uid, rank, prize))
                # New round starts where the old one ended (so commissions earned after the end are not lost),
                # or right now if no end date was set. Stored in UTC.
                new_start = REF_LB_END if (REF_LB_END and REF_LB_END <= datetime.utcnow()) else datetime.utcnow()
                await conn.execute(
                    "INSERT INTO bot_settings (key, value) VALUES ('ref_lb_start', $1) ON CONFLICT (key) DO UPDATE SET value = $1",
                    new_start.isoformat()
                )
                await conn.execute("DELETE FROM bot_settings WHERE key='ref_lb_end'")
        REF_LB_START = new_start
        REF_LB_END = None
        for uid, _rank, _prize in paid:
            invalidate_user_cache(uid)
        return paid
    finally:
        REF_LB_PAYING = False

@dp.callback_query(F.data == "admin_ref_lb_pay")
async def cb_admin_ref_lb_pay(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    await call.answer()

    if not any(a > 0 for a in REF_LB_REWARDS):
        await _edit_or_send(call, f'{E_WARN} <b>No leaderboard prizes are set.</b>\n\nSet them first from <b>Leaderboard Rewards</b>.', get_back_inline_keyboard("admin_ref_refresh"))
        return

    rows = await fetch_ref_leaderboard(len(REF_LB_REWARDS))
    if not rows:
        await _edit_or_send(call, f'{E_EMPTY} <b>Nobody has earned referral commission in this period yet.</b>', get_back_inline_keyboard("admin_ref_refresh"))
        return

    lines = []
    total = 0.0
    for r in rows:
        rank = int(r['rnk'])
        prize = ref_prize_for_rank(rank)
        if prize:
            total += prize
            lines.append(f"#{rank} <code>{r['user_id']}</code> - earned ₹{float(r['earned']):.2f} → <b>prize ₹{prize:.2f}</b>")

    text = (
        f'{E_TROPHY} <b>Confirm Leaderboard Payout</b>\n\n'
        + "\n".join(lines) +
        f'\n\n{E_MONEY} <b>Total to credit:</b> ₹{total:.2f}\n\n'
        f'{E_WARN} Prizes are added to the winners\' balances and a <b>new round</b> starts (leaderboard resets).'
    )
    kb = InlineKeyboardBuilder()
    kb.button(text="Confirm & Pay", icon_custom_emoji_id="6217663806110175239", callback_data="admin_ref_lb_pay_confirm", style="success")
    kb.button(text="Cancel", icon_custom_emoji_id="5274099962655816924", callback_data="admin_ref_refresh", style="danger")
    kb.adjust(2)
    await _edit_or_send(call, text, kb.as_markup())

@dp.callback_query(F.data == "admin_ref_lb_pay_confirm")
async def cb_admin_ref_lb_pay_confirm(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    result = await pay_ref_leaderboard_rewards()
    if result is None:
        await call.answer("⏳ A payout is already in progress.", show_alert=True)
        return
    await call.answer()

    if not result:
        await _edit_or_send(call, f'{E_EMPTY} <b>Nothing to pay - no winners in this period.</b>', get_back_inline_keyboard("admin_ref_refresh"))
        return

    lines = [f"#{rank} <code>{uid}</code> - ₹{prize:.2f}" for uid, rank, prize in result]
    total = sum(p for _u, _r, p in result)
    await _edit_or_send(
        call,
        f'{E_OK} <b>Leaderboard rewards paid!</b>\n\n' + "\n".join(lines) +
        f'\n\n{E_MONEY} <b>Total credited:</b> ₹{total:.2f}\n{E_ROCKET} A new leaderboard round has started.',
        get_back_inline_keyboard("admin_ref_refresh")
    )

    async def notify_winner(uid, rank, prize):
        ud = await get_user_data(uid)
        amt_str = format_currency(prize, ud['currency'] if ud else "USD")
        await send_user_notification(
            uid,
            f'{E_TROPHY} <b>Referral Leaderboard Winner!</b>\n\n'
            f'{E_CROWN} You finished at <b>Rank #{rank}</b>\n'
            f'{E_MONEY} <b>+{amt_str}</b> has been added to your balance {E_PARTY}',
            parse_mode=ParseMode.HTML
        )

    for uid, rank, prize in result:
        asyncio.create_task(notify_winner(uid, rank, prize))

# ============================================
# MY ACCOUNTS PAGINATED SYSTEM
# ============================================

async def render_my_accounts_page(user_id: int, page: int = 1):
    items_per_page = 5

    async with db_pool.acquire() as conn:
        # Items that went through the new hold system (Task + Sell)
        holds = await conn.fetch('''
            SELECT id, kind, ref_id, email, amount, status, created_at, release_at, closed_at
            FROM holds
            WHERE user_id=$1
        ''', user_id)

        # Older sell requests (submitted before the hold system existed)
        sells = await conn.fetch('''
            SELECT s.id, s.details, s.status, s.created_at
            FROM pending_sells s
            WHERE s.user_id=$1
              AND NOT EXISTS (SELECT 1 FROM holds h WHERE h.kind = 'sell' AND h.ref_id = s.id)
        ''', user_id)

        # Tasks still in progress / older tasks waiting for review
        tasks = await conn.fetch('''
            SELECT t.id, t.details, t.status, ta.assigned_at as created_at
            FROM task_assignments ta
            JOIN tasks t ON ta.task_id = t.id
            WHERE ta.user_id=$1
              AND NOT EXISTS (
                  SELECT 1 FROM holds h
                  WHERE h.kind = 'task' AND h.ref_id = t.id AND h.user_id = ta.user_id
                    AND h.status IN ('pending_review', 'in_holder')
              )
        ''', user_id)

        # Older completed tasks (paid before the hold system existed)
        completed_tasks = await conn.fetch('''
            SELECT tr.id, tr.note as details, 'completed' as status, tr.created_at
            FROM transactions tr
            WHERE tr.user_id=$1 AND tr.type='task' AND tr.hold_id IS NULL
        ''', user_id)

    all_accounts = []

    for h in holds:
        email = h['email'] or ""
        if "@gmail.com" not in email.lower() and "@" not in email:
            email += "@gmail.com"
        status_raw = h['status']
        extra = ""
        if status_raw == 'pending_review':
            status_str = f"🟡 Waiting For Review ({HOLD_DAYS}-day hold)"
            extra = f"\n{E_CAL} <b>Release Date:</b> {fmt_dt(h['release_at'])} IST"
        elif status_raw == 'in_holder':
            status_str = f"🟣 In Hold - {fmt_remaining(h['release_at'])}"
            extra = f"\n{E_CAL} <b>Release Date:</b> {fmt_dt(h['release_at'])} IST"
        elif status_raw == 'released':
            status_str = f"{E_GREEN} Released & Paid"
            if h['closed_at']:
                extra = f"\n{E_CAL} <b>Released:</b> {fmt_dt(h['closed_at'])} IST"
        else:
            status_str = f"{E_RED} Declined / Rejected"
        all_accounts.append({
            'title': f"{email} #{h['ref_id']}",
            'type': 'Sell' if h['kind'] == 'sell' else 'Register',
            'status': status_str,
            'extra': extra,
            'date': h['created_at']
        })

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
            status_str = f"{E_GREEN} Approved & Paid"
        else:
            status_str = f"{E_RED} Declined / Rejected"

        all_accounts.append({
            'title': display_title,
            'type': 'Sell',
            'status': status_str,
            'extra': "",
            'date': s['created_at']
        })

    for t in tasks:
        task_id = t['id']
        try:
            parts = t['details'].split(" | ")
            email = parts[0].replace("Email: ", "").strip()
        except Exception:
            email = "Task"

        if "@gmail.com" not in email.lower() and "@" not in email:
            email += "@gmail.com"

        display_title = f"{email} #{task_id}"

        status_raw = t['status']
        if status_raw == 'pending_review':
            status_str = "🟡 Waiting For Review"
        elif status_raw in ('assigned', 'verifying'):
            status_str = "🔵 In Progress"
        elif status_raw == 'completed':
            status_str = f"{E_GREEN} Approved & Paid"
        else:
            status_str = f"{E_RED} Declined / Rejected"

        all_accounts.append({
            'title': display_title,
            'type': 'Register',
            'status': status_str,
            'extra': "",
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
            'status': f"{E_GREEN} Approved & Paid",
            'extra': "",
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
            '<tg-emoji emoji-id="5350387571199319521">🏷</tg-emoji>️ <b>My Accounts</b>\n\n'
            f"{E_EMPTY} You haven't submitted any Gmail accounts yet."
        )
    else:
        text = (
            '<tg-emoji emoji-id="5350387571199319521">🏷</tg-emoji>️ <b>My Accounts</b>\n'
            f'You have <b>{total_items}</b> submitted Gmail accounts.\n'
            f'Showing <b>{start_idx + 1}-{end_idx}</b> of <b>{total_items}</b>.\n\n'
        )

        for item in page_items:
            date_fmt = to_ist(item['date']).strftime("%b %d %I:%M %p")
            text += (
                f"<code>{_esc(item['title'])}</code>\n"
                f"<tg-emoji emoji-id=\"5237699328843200968\">📌</tg-emoji> <b>Type:</b> {item['type']}\n"
                f"{item['status']}\n"
                f"Created: {date_fmt}"
                f"{item['extra']}\n\n"
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
            elif task_status in ('assigned', 'verifying'):
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

        task = await conn.fetchrow("SELECT id, title, details, reward, fixed_password FROM tasks WHERE status='available' ORDER BY RANDOM() LIMIT 1")
        if not task:
            txt = '<tg-emoji emoji-id=\"5262831879731555779\">📭</tg-emoji> No tasks available right now.'
            try:
                await call.message.edit_text(txt, parse_mode=ParseMode.HTML, reply_markup=get_main_menu_keyboard())
            except TelegramBadRequest as e:
                if "message is not modified" not in str(e):
                    await call.message.answer(txt, parse_mode=ParseMode.HTML, reply_markup=get_main_menu_keyboard())
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

        fixed_pw = (task['fixed_password'] or '').strip()
        if fixed_pw:
            # Admin attached a specific password to this Gmail - every user gets the same one
            password = fixed_pw
        elif DEFAULT_TASK_PASS_STATUS:
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
    hold_bal = (user_data['hold_balance'] if user_data else 0.0) or 0.0

    ultra_line = f'\n<tg-emoji emoji-id=\"5195033767969839232\">⚡️</tg-emoji> <b>Ultra Gateway:</b> <code>{ultra}</code>' if ULTRA_STATUS else ""

    text = (
        f'<tg-emoji emoji-id=\"5445353829304387411\">💳</tg-emoji> <b>Balance</b>\n\n'
        f'💵 <b>Available:</b> {formatted_bal}\n'
        f'{E_HOLD} <b>On Hold:</b> {format_currency(hold_bal, curr)}\n'
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
    """Entry point for Sell Gmail - now a chooser between Single Sell and Bulk Sell."""
    if not SELL_GMAIL_STATUS:
        await call.answer("⚠️ Selling Gmail is currently disabled by Admin!", show_alert=True)
        return

    await call.answer()
    await state.clear()
    user_data = await get_user_data(call.from_user.id)
    rate_str = format_currency(GMAIL_SELL_RATE, user_data['currency'])
    txt = (
        f'<tg-emoji emoji-id=\"5350387571199319521\">🏷</tg-emoji>️ <b>Sell Price {rate_str}/Gmail</b>\n\n'
        f'📦 <b>Choose how you want to sell:</b>\n\n'
        f'• <b>Single Sell</b> — sell one Gmail, step by step.\n'
        f'• <b>Bulk Sell</b> — sell up to {BULK_SELL_MAX_ITEMS} Gmails at once in one message.'
    )
    sell_kb = InlineKeyboardBuilder()
    sell_kb.button(text="Single Sell", icon_custom_emoji_id="5377548235709619284", callback_data="sell_mode_single", style="primary")
    sell_kb.button(text="Bulk Sell", icon_custom_emoji_id="5472027899789843495", callback_data="sell_mode_bulk", style="success")
    if SELL_VIDEO_LINK:
        sell_kb.button(text="Tutorial Video", icon_custom_emoji_id="5282843764451195532", url=SELL_VIDEO_LINK)
        sell_kb.button(text="Back", icon_custom_emoji_id="5875082500023258804", callback_data="menu_back")
        sell_kb.adjust(2, 1, 1)
    else:
        sell_kb.button(text="Back", icon_custom_emoji_id="5875082500023258804", callback_data="menu_back")
        sell_kb.adjust(2, 1)
    sell_markup = sell_kb.as_markup()
    try:
        await call.message.edit_text(txt, parse_mode=ParseMode.HTML, reply_markup=sell_markup)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            await call.message.answer(txt, parse_mode=ParseMode.HTML, reply_markup=sell_markup)
    await state.update_data(last_menu_msg_id=call.message.message_id)

@dp.callback_query(F.data == "sell_mode_single")
async def cb_sell_mode_single(call: CallbackQuery, state: FSMContext):
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
        '🤑 <b>Step 1/4:</b> Please send the Gmail <b>Username</b> (e.g., <code>example@gmail.com</code>):'
    )
    sell_kb = InlineKeyboardBuilder()
    if SELL_VIDEO_LINK:
        sell_kb.button(text="Tutorial Video", icon_custom_emoji_id="5282843764451195532", url=SELL_VIDEO_LINK)
    sell_kb.button(text="Back", icon_custom_emoji_id="5875082500023258804", callback_data="menu_sell_gmail")
    sell_kb.adjust(1)
    sell_markup = sell_kb.as_markup()
    try:
        await call.message.edit_text(txt, parse_mode=ParseMode.HTML, reply_markup=sell_markup)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            await call.message.answer(txt, parse_mode=ParseMode.HTML, reply_markup=sell_markup)
    await state.update_data(last_menu_msg_id=call.message.message_id)

@dp.callback_query(F.data == "sell_mode_bulk")
async def cb_sell_mode_bulk(call: CallbackQuery, state: FSMContext):
    if not SELL_GMAIL_STATUS:
        await call.answer("⚠️ Selling Gmail is currently disabled by Admin!", show_alert=True)
        return

    await call.answer()
    await state.clear()
    await state.set_state(UserState.bulk_selling_input)
    user_data = await get_user_data(call.from_user.id)
    rate_str = format_currency(GMAIL_SELL_RATE, user_data['currency'])
    txt = (
        f'{E_BOX} <b>Bulk Gmail Sell</b>\n'
        '━━━━━━━━━━━━━━━━━━\n\n'
        f'💸 <b>Rate:</b> {rate_str} / Gmail\n\n'
        f'📋 Send <b>one Gmail per line</b>, in this exact format:\n'
        f'<code>gmail:password:2fa_key:app_password</code>\n\n'
        f'<b>Example:</b>\n'
        f'<code>demo1@gmail.com:Pass@123:JBSWY3DPEHPK3PXP:abcduvwxijklopqr\n'
        f'demo2@gmail.com:Pass@456:KRSXG5CTMVRXEZLU:mnopabcdqrstuvwx</code>\n\n'
        f'{E_WARN} Every account still goes through <b>full verification</b> — '
        f'Gmail existence check, 2FA key validation &amp; a live App Password login. '
        f'<b>No security step is skipped.</b>\n\n'
        f'📦 <b>Limit:</b> up to <b>{BULK_SELL_MAX_ITEMS}</b> Gmails per batch.\n\n'
        f'✍️ <b>Send your list now:</b>'
    )
    bulk_kb = InlineKeyboardBuilder()
    if SELL_VIDEO_LINK:
        bulk_kb.button(text="Tutorial Video", icon_custom_emoji_id="5282843764451195532", url=SELL_VIDEO_LINK)
    bulk_kb.button(text="Back", icon_custom_emoji_id="5875082500023258804", callback_data="menu_sell_gmail")
    bulk_kb.adjust(1)
    bulk_markup = bulk_kb.as_markup()
    try:
        await call.message.edit_text(txt, parse_mode=ParseMode.HTML, reply_markup=bulk_markup)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            await call.message.answer(txt, parse_mode=ParseMode.HTML, reply_markup=bulk_markup)
    await state.update_data(last_menu_msg_id=call.message.message_id)

@dp.message(UserState.bulk_selling_input, F.text, ~F.text.startswith("/"), ~F.text.in_(MENU_BUTTONS))
async def process_bulk_sell_input(message: Message, state: FSMContext):
    user_id = message.from_user.id
    if not SELL_GMAIL_STATUS:
        await message.answer("⚠️ Selling Gmail is currently disabled by Admin!", reply_markup=get_main_menu_keyboard())
        await state.clear()
        return

    if user_id in ACTIVE_SUBMISSIONS:
        await message.answer("⚠️ You already have a submission in progress. Please wait for it to finish.")
        return

    raw_lines = [ln.strip() for ln in (message.text or "").splitlines() if ln.strip()]
    if not raw_lines:
        await message.answer(
            f"{E_NO} Send at least one line in the format <code>gmail:password:2fa_key:app_password</code>",
            parse_mode=ParseMode.HTML
        )
        return

    if len(raw_lines) > BULK_SELL_MAX_ITEMS:
        await message.answer(
            f"{E_NO} <b>Too many accounts!</b> You sent <b>{len(raw_lines)}</b>, the max per batch is <b>{BULK_SELL_MAX_ITEMS}</b>.\n\n"
            f"Please split your list into smaller batches and send again.",
            parse_mode=ParseMode.HTML
        )
        return

    await cleanup_last_menu(message, state)
    await state.clear()

    # ---- Parse every line up front (format-only checks, no network calls yet) ----
    items = []
    for line in raw_lines:
        parts = line.split(":")
        if len(parts) != 4:
            items.append({"raw": line, "error": "Invalid format (expected gmail:password:2fa_key:app_password)"})
            continue

        email_raw, pw, tw, ap = (p.strip() for p in parts)
        if "@gmail.com" not in email_raw.lower() and "@" not in email_raw:
            email_raw = f"{email_raw}@gmail.com"
        if not email_raw.lower().endswith("@gmail.com"):
            items.append({"raw": line, "error": "Not a Gmail address"})
            continue
        if not pw:
            items.append({"raw": line, "error": "Missing password"})
            continue
        if not tw or not generate_local_totp(tw):
            items.append({"raw": line, "error": "Invalid 2FA secret key"})
            continue
        ap_clean = ap.replace(" ", "")
        if not re.fullmatch(r"[A-Za-z0-9]{16}", ap_clean):
            items.append({"raw": line, "error": "App password must be exactly 16 characters"})
            continue

        items.append({
            "raw": line, "error": None,
            "email": email_raw.lower(), "password": pw, "twofa": tw, "app_pw": ap_clean
        })

    total = len(items)
    bulk_tag = generate_bulk_tag()
    tg_username = message.from_user.username
    user_data = await get_user_data(user_id)
    currency = user_data['currency'] if user_data else "USD"

    ACTIVE_SUBMISSIONS.add(user_id)
    progress_msg = await message.answer(
        f"{E_ROCKET} <b>Bulk Gmail Sell Started</b>\n"
        '━━━━━━━━━━━━━━━━━━\n\n'
        f"{render_bulk_progress_bar(0, total)} 0%\n"
        f"{E_BOX} <b>Progress:</b> 0/{total}\n\n"
        f"🏷 <b>Batch Tag:</b> <code>#{bulk_tag}</code>",
        parse_mode=ParseMode.HTML
    )

    success_count = 0
    failed_results = []  # list of (email_or_raw, reason)

    try:
        for idx, item in enumerate(items, start=1):
            bar = render_bulk_progress_bar(idx - 1, total)
            pct = int(100 * (idx - 1) / total) if total else 0
            display_email = item.get("email") or item["raw"][:40]
            status_line = f"{E_NO} Skipping invalid line..." if item["error"] else f"{E_TIME} Verifying Gmail, 2FA &amp; App Password..."

            try:
                await progress_msg.edit_text(
                    f"{E_ROCKET} <b>Bulk Gmail Sell In Progress</b>\n"
                    '━━━━━━━━━━━━━━━━━━\n\n'
                    f"{bar} {pct}%\n"
                    f"{E_BOX} <b>Processing:</b> {idx}/{total}\n"
                    f"{E_MAIL} <b>Current Gmail:</b> <code>{_esc(display_email)}</code>\n"
                    f"{status_line}\n\n"
                    f"{E_OK} <b>Success:</b> {success_count}    {E_NO} <b>Failed:</b> {len(failed_results)}\n\n"
                    f"🏷 <b>Batch Tag:</b> <code>#{bulk_tag}</code>",
                    parse_mode=ParseMode.HTML
                )
            except Exception:
                pass

            if item["error"]:
                failed_results.append((display_email, item["error"]))
                continue

            email = item["email"]
            password = item["password"]
            twofa = item["twofa"]
            app_pw = item["app_pw"]

            # ---- FULL security, identical to the single-sell flow - nothing bypassed ----
            if await is_sell_password_used(password):
                failed_results.append((email, "Password already registered in system"))
                continue

            search_pattern = f"%{email}%"
            async with db_pool.acquire() as conn:
                existing_sell = await conn.fetchval("SELECT id FROM pending_sells WHERE LOWER(details) LIKE $1", search_pattern)
                existing_task = await conn.fetchval("SELECT id FROM tasks WHERE LOWER(title) LIKE $1 OR LOWER(details) LIKE $1", search_pattern)
            if existing_sell or existing_task:
                failed_results.append((email, "This Gmail already exists in the database"))
                continue

            is_valid = await is_gmail_registered(email)
            if not is_valid:
                failed_results.append((email, "Gmail account does not exist on Google"))
                continue

            ok = await verify_app_password(email, app_pw)
            if not ok:
                failed_results.append((email, "App Password verification failed"))
                continue

            res = await finalize_sell_submission(user_id, tg_username, email, password, twofa, app_pw, bulk_tag=bulk_tag)
            if res == 'dup':
                failed_results.append((email, "This Gmail already exists in the database"))
                continue
            if res == 'dup_pass':
                failed_results.append((email, "Password already registered in system"))
                continue

            success_count += 1
            asyncio.create_task(notify_admin_new_hold(res['hold_id']))

        if success_count:
            asyncio.create_task(alert_workers_new_sell())

        final_bar = render_bulk_progress_bar(total, total)
        amt_each = format_currency(GMAIL_SELL_RATE, currency)
        total_amt = format_currency(GMAIL_SELL_RATE * success_count, currency)

        summary_lines = [
            f"{E_PARTY} <b>Bulk Gmail Sell Completed!</b>",
            '━━━━━━━━━━━━━━━━━━',
            '',
            f"{final_bar} 100%",
            f"{E_BOX} <b>Processed:</b> {total}/{total}",
            f"{E_OK} <b>Success:</b> {success_count}    {E_NO} <b>Failed:</b> {len(failed_results)}",
            '',
            f"🏷 <b>Batch Tag:</b> <code>#{bulk_tag}</code>",
        ]
        if success_count:
            summary_lines.append(f"💰 <b>On Hold:</b> {total_amt} ({amt_each} × {success_count})")
        if failed_results:
            summary_lines.append('')
            summary_lines.append(f"{E_WARN} <b>Failed Item(s):</b>")
            for em, reason in failed_results[:20]:
                summary_lines.append(f"• <code>{_esc(em)}</code> — {_esc(reason)}")
            if len(failed_results) > 20:
                summary_lines.append(f"<i>...and {len(failed_results) - 20} more</i>")

        final_kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="Main Menu", icon_custom_emoji_id="5416041192905265756", callback_data="menu_back", style="primary")
        ]])
        try:
            await progress_msg.edit_text("\n".join(summary_lines), parse_mode=ParseMode.HTML, reply_markup=final_kb)
        except Exception:
            await message.answer("\n".join(summary_lines), parse_mode=ParseMode.HTML, reply_markup=get_main_menu_keyboard())
    finally:
        ACTIVE_SUBMISSIONS.discard(user_id)

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
        '<tg-emoji emoji-id=\"6005570495603282482\">🔑</tg-emoji> <b>Step 2/4:</b> Now send the <b>Password</b> for this Gmail account:',
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
    if not username:
        await state.clear()
        await message.answer("⚠️ Session expired. Please start again.", reply_markup=get_main_menu_keyboard())
        return

    if await is_sell_password_used(password):
        retry_msg = await message.answer(
            password_already_registered_text(),
            parse_mode=ParseMode.HTML,
            reply_markup=get_back_inline_keyboard()
        )
        await state.update_data(last_menu_msg_id=retry_msg.message_id)
        return

    if not TWOFA_SELL_STATUS:
        # 2FA step is OFF: Username -> Password -> submit immediately (no 2FA / App Password asked)
        user_id = message.from_user.id
        if user_id in ACTIVE_SUBMISSIONS:
            return
        ACTIVE_SUBMISSIONS.add(user_id)
        try:
            await state.clear()
            tg_username = message.from_user.username
            res = await finalize_sell_submission(user_id, tg_username, username, password, None, None)
            if res == 'dup':
                await message.answer(
                    f"{E_NO} <b>This email is already in the database. You cannot sell the same email twice.</b>",
                    parse_mode=ParseMode.HTML,
                    reply_markup=get_main_menu_keyboard()
                )
                return
            if res == 'dup_pass':
                await message.answer(
                    password_already_registered_text(),
                    parse_mode=ParseMode.HTML,
                    reply_markup=get_main_menu_keyboard()
                )
                return

            user_data = await get_user_data(user_id)
            amt_str = format_currency(res['amount'], user_data['currency'] if user_data else "USD")
            final_text = build_submission_success_text("Gmail Sell", res['ref_id'], amt_str, res['release_at'])
            sent_msg = await message.answer(final_text, parse_mode=ParseMode.HTML, reply_markup=get_main_menu_keyboard())
            await state.update_data(last_menu_msg_id=sent_msg.message_id)

            asyncio.create_task(notify_admin_new_hold(res['hold_id']))
            asyncio.create_task(alert_workers_new_sell())
        finally:
            ACTIVE_SUBMISSIONS.discard(user_id)
        return

    # Next: 2FA key / QR  ->  OTP code  ->  App Password  ->  submitted (goes into hold)
    await state.set_state(UserState.submitting_twofa)
    text, kb = build_twofa_prompt('sell')
    sent_msg = await message.answer(text, parse_mode=ParseMode.HTML, reply_markup=kb)
    await state.update_data(
        flow='sell', email=username, gmail_password=password, sell_password=password,
        flow_msg_id=sent_msg.message_id, last_menu_msg_id=sent_msg.message_id, twofa_tries=0
    )

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
            **bk("👁", "View Request"),
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
                **bk("💬", "Reply User"),
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
                **bk("💬", "Reply User"),
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
            f"<tg-emoji emoji-id=\"5274099962655816924\">❌</tg-emoji> Failed to send reply to User <code>{target_user_id}</code>.\n\nError: <code>{_esc(e)}</code>",
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
        # NOTE: inline button labels never parse HTML/entities - no parse_mode applies to
        # button text, so a literal <tg-emoji> tag shows up as raw text (this was the bug).
        # A button can only carry ONE premium custom emoji, via icon_custom_emoji_id, so
        # we keep the premium sell-permission emoji there, and show Bot Access (on/off)
        # via the button's color style instead of a second emoji tag.
        worker_display_name = w['name'] if w['name'] else f"Worker ({w['worker_id']})"
        btn_label = f"#{idx} {worker_display_name}"
        sell_icon_id = "5377548235709619284" if w['can_sell_gmail'] else "5240241223632954241"  # premium 📨 / 🚫
        access_style = "success" if w['is_active'] else "danger"  # green = Bot Access ON, red = OFF
        kb.button(text=btn_label, icon_custom_emoji_id=sell_icon_id, callback_data=f"adm_work_view:{w['worker_id']}", style=access_style)

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
    toggle_access = bk("🔴", "Turn Worker OFF") if w['is_active'] else bk("🟢", "Turn Worker ON")
    toggle_sell = bk("🚫", "Disable Sell Gmail") if w['can_sell_gmail'] else bk("📨", "Enable Sell Gmail")

    kb.button(**toggle_access, callback_data=f"adm_work_tog_access:{worker_id}")
    kb.button(**toggle_sell, callback_data=f"adm_work_tog_sell:{worker_id}")
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

@dp.message(F.text.in_({"Ref Status: OFF", "Ref Status: ON", "🔴 Ref Status: OFF", "🟢 Ref Status: ON"}), StateFilter("*"))
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

@dp.message(F.text.in_({"Ultra Status: OFF", "Ultra Status: ON", "🔴 Ultra Status: OFF", "🟢 Ultra Status: ON"}), StateFilter("*"))
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

@dp.message(F.text.in_({"Bot Status: OFF", "Bot Status: ON", "🔴 Bot Status: OFF", "🟢 Bot Status: ON"}), StateFilter("*"))
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
    BOT_OFF_MESSAGE = (message.html_text or message.text or '').strip()   # keeps premium emoji / formatting the admin typed
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

def build_validator_menu_text() -> str:
    status_str = "<tg-emoji emoji-id=\"5416081784641168838\">🟢</tg-emoji> <b>Active</b>" if VALIDATOR_ENABLED else "<tg-emoji emoji-id=\"5411225014148014586\">🔴</tg-emoji> <b>Deactivated</b>"
    provider_name = get_provider_name()

    key_line = ""
    if VALIDATOR_PROVIDER == "netnit":
        key_line = f"<tg-emoji emoji-id=\"6005570495603282482\">🔑</tg-emoji> <b>Current API Key:</b> <code>{EMAILABLE_API_KEY}</code>\n"

    return (
        f"<tg-emoji emoji-id=\"5893161718179173515\">⚙️</tg-emoji> <b>Gmail Validator Management</b>\n\n"
        f"<tg-emoji emoji-id=\"5447410659077661506\">🌐</tg-emoji> <b>Current Provider:</b> <code>{provider_name}</code>\n"
        f"{key_line}"
        f"<tg-emoji emoji-id=\"5237699328843200968\">📌</tg-emoji> <b>Validator Status:</b> {status_str}\n\n"
        f"Use the buttons below to configure the email validator:"
    )

@dp.message(F.text == "Validator", StateFilter("*"))
async def admin_btn_validator_menu(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.clear()
    await message.answer(build_validator_menu_text(), parse_mode=ParseMode.HTML, reply_markup=get_validator_admin_inline_keyboard())

@dp.callback_query(F.data == "admin_validator_toggle_status")
async def cb_admin_validator_toggle_status(call: CallbackQuery):
    await call.answer("Validator status updated!", show_alert=True)
    global VALIDATOR_ENABLED
    VALIDATOR_ENABLED = not VALIDATOR_ENABLED
    new_val = 'on' if VALIDATOR_ENABLED else 'off'

    async with db_pool.acquire() as conn:
        await conn.execute("INSERT INTO bot_settings (key, value) VALUES ('validator_enabled', $1) ON CONFLICT (key) DO UPDATE SET value = $1", new_val)

    try:
        await call.message.edit_text(build_validator_menu_text(), parse_mode=ParseMode.HTML, reply_markup=get_validator_admin_inline_keyboard())
    except Exception:
        pass

VALIDATOR_PROVIDER_CYCLE = ["netnit", "manual"]

@dp.callback_query(F.data == "admin_validator_change_provider")
async def cb_admin_validator_change_provider(call: CallbackQuery):
    global VALIDATOR_PROVIDER
    try:
        idx = VALIDATOR_PROVIDER_CYCLE.index(VALIDATOR_PROVIDER)
    except ValueError:
        idx = -1
    VALIDATOR_PROVIDER = VALIDATOR_PROVIDER_CYCLE[(idx + 1) % len(VALIDATOR_PROVIDER_CYCLE)]

    async with db_pool.acquire() as conn:
        await conn.execute("INSERT INTO bot_settings (key, value) VALUES ('validator_provider', $1) ON CONFLICT (key) DO UPDATE SET value = $1", VALIDATOR_PROVIDER)

    provider_name = get_provider_name()
    await call.answer(f"Switched provider to {provider_name}!", show_alert=True)

    try:
        await call.message.edit_text(build_validator_menu_text(), parse_mode=ParseMode.HTML, reply_markup=get_validator_admin_inline_keyboard())
    except Exception:
        pass

@dp.callback_query(F.data == "admin_validator_change_key")
async def cb_admin_validator_change_key(call: CallbackQuery, state: FSMContext):
    if VALIDATOR_PROVIDER != "netnit":
        await call.answer("⚠️ The API key only applies to the Third Party provider.", show_alert=True)
        return
    await call.answer()
    provider_name = get_provider_name()

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

    provider_name = get_provider_name()

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
        "<tg-emoji emoji-id=\"5472027899789843495\">📦</tg-emoji> <b>Bulk Task Addition</b> <i>(Step 1/2)</i>\n\n"
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

    total = len(usernames)
    await state.set_state(AdminState.waiting_for_bulk_add_passwords)
    await state.update_data(bulk_usernames=usernames)

    # Show the numbered list so the admin can match passwords line by line
    numbered = [_esc(f"{i}. {u}") for i, u in enumerate(usernames, start=1)]
    chunks = chunk_blocks(numbered, limit=3000, sep="\n")
    for idx, chunk in enumerate(chunks):
        head = f"{E_BOX} <b>{total} username(s) received</b> <i>(duplicates removed)</i>\n\n" if idx == 0 else ""
        await message.answer(f"{head}<code>{chunk}</code>", parse_mode=ParseMode.HTML)

    await message.answer(
        f"{E_KEY} <b>Bulk Task Addition</b> <i>(Step 2/2)</i>\n\n"
        f"Send <b>{total}</b> password(s), <b>one per line</b>, in the same order as the list above.\n\n"
        f"• Type <code>skip</code> on a line to use the Random / Fixed password mode for that Gmail only.\n"
        f"• Send just <code>SKIP</code> to skip passwords for <b>all</b> Gmails.\n\n"
        f"<i>Example:</i>\n"
        f"<code>password@123\n"
        f"sjdjdffi@34\n"
        f"skip\n"
        f"shiodhei@3u</code>\n\n"
        f"{E_WARN} A password you set here is attached to that Gmail permanently - every user who gets this task receives that same password.",
        parse_mode=ParseMode.HTML
    )

@dp.message(AdminState.waiting_for_bulk_add_passwords, F.text, ~F.text.startswith("/"), ~F.text.in_(MENU_BUTTONS))
async def process_bulk_add_passwords_step(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    data = await state.get_data()
    usernames = data.get('bulk_usernames') or []
    if not usernames:
        await state.clear()
        await message.answer("❌ Session expired. Please start Bulk Add again.", reply_markup=get_admin_menu_keyboard())
        return

    total_items = len(usernames)
    raw = message.text.strip()

    if raw.lower() == "skip":
        passwords = [None] * total_items
    else:
        pw_lines = [ln.strip() for ln in raw.replace("\r", "").split("\n") if ln.strip()]
        if len(pw_lines) != total_items:
            await message.answer(
                f"{E_NO} <b>You sent {len(pw_lines)} password line(s) but there are {total_items} username(s).</b>\n\n"
                f"Send exactly <b>{total_items}</b> line(s) (use <code>skip</code> on a line to skip it), or just <code>SKIP</code> to skip all.",
                parse_mode=ParseMode.HTML
            )
            return
        passwords = []
        bad_lines = []
        for line_no, ln in enumerate(pw_lines, start=1):
            if ln.lower() == "skip":
                passwords.append(None)
            elif " | " in ln:
                bad_lines.append(line_no)
                passwords.append(None)
            else:
                passwords.append(ln)
        if bad_lines:
            await message.answer(
                f"{E_NO} <b>Line(s) {', '.join(map(str, bad_lines))} contain the characters</b> <code> | </code> <b>which are not allowed in a password.</b>\n\nPlease fix them and send the passwords again.",
                parse_mode=ParseMode.HTML
            )
            return

    added_count = 0
    skipped_count = 0
    specific_count = 0

    status_msg = await message.answer(f"<tg-emoji emoji-id=\"5382194935057372936\">⏳</tg-emoji> <b>Adding tasks in bulk... 0/{total_items}</b>", parse_mode=ParseMode.HTML)

    async with db_pool.acquire() as conn:
        for idx, (email, specific_pw) in enumerate(zip(usernames, passwords), start=1):
            search_pattern = f"%{email}%"
            existing = await conn.fetchval(
                "SELECT id FROM tasks WHERE LOWER(title) LIKE $1 OR LOWER(details) LIKE $1 LIMIT 1",
                search_pattern
            )

            if not existing:
                shown_password = specific_pw if specific_pw else DEFAULT_TASK_PASS
                default_reward = DEFAULT_TASK_RATE
                title = f"Login to {email}"
                details = f"Email: {email} | Pass: {shown_password}"

                await conn.execute(
                    "INSERT INTO tasks (title, details, reward, fixed_password) VALUES ($1, $2, $3, $4)",
                    title, details, default_reward, specific_pw
                )
                added_count += 1
                if specific_pw:
                    specific_count += 1
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
        f"{E_KEY} <b>With Specific Password:</b> {specific_count}\n"
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

async def _send_hold_cards(call: CallbackQuery, rows, header_text: str):
    await call.message.answer(header_text, parse_mode=ParseMode.HTML)
    for h in rows:
        h = dict(h)
        await call.message.answer(render_hold_card(h), parse_mode=ParseMode.HTML, reply_markup=get_hold_card_keyboard(h))

@dp.callback_query(F.data == "admin_view_pending_sells")
async def cb_admin_view_pending_sells(call: CallbackQuery):
    await call.answer()
    if call.from_user.id != ADMIN_ID:
        return

    async with db_pool.acquire() as conn:
        hold_rows = await conn.fetch('''
            SELECT h.*, s.claimed_by
            FROM holds h
            LEFT JOIN pending_sells s ON s.id = h.ref_id
            WHERE h.kind = 'sell' AND h.status = 'pending_review'
            ORDER BY h.id ASC
        ''')
        legacy_rows = await conn.fetch('''
            SELECT s.id, s.user_id, s.details, s.amount, s.claimed_by
            FROM pending_sells s
            WHERE s.status = 'pending_review'
              AND NOT EXISTS (SELECT 1 FROM holds h WHERE h.kind = 'sell' AND h.ref_id = s.id)
            ORDER BY s.created_at ASC
        ''')

    if not hold_rows and not legacy_rows:
        empty_txt = f"{E_EMPTY} <b>No pending sell Gmail requests found!</b>"
        try:
            await call.message.edit_text(empty_txt, parse_mode=ParseMode.HTML, reply_markup=get_back_inline_keyboard("back_pending_reviews"))
        except TelegramBadRequest as e:
            if "message is not modified" not in str(e):
                await call.message.answer(empty_txt, parse_mode=ParseMode.HTML, reply_markup=get_back_inline_keyboard("back_pending_reviews"))
        return

    if hold_rows:
        await _send_hold_cards(call, hold_rows, f"{E_BOX} <b>Displaying {len(hold_rows)} pending Gmail sell request(s):</b>")

    # Requests submitted before the hold system existed keep the old Approve / Decline buttons
    if legacy_rows:
        await call.message.answer(f"{E_BOX} <b>Older sell request(s) (no hold): {len(legacy_rows)}</b>", parse_mode=ParseMode.HTML)
    for r in legacy_rows:
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
            formatted_details = f"{E_MAIL} <b>Username:</b> <code>{_esc(username)}</code>\n{E_KEY} <b>Password:</b> <code>{_esc(password)}</code>"
        except Exception:
            formatted_details = f"<code>{_esc(details)}</code>"

        claimed_str = f"\n👷 <b>Claimed By Worker:</b> <code>{claimed_by}</code>" if claimed_by else f"\n{E_BOX} <b>Status:</b> {E_GREEN} Unclaimed Stock"
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="Approve", icon_custom_emoji_id=ICON_OK, callback_data=f"sa:{sell_id}", style="success"),
            InlineKeyboardButton(text="Decline", icon_custom_emoji_id=ICON_NO, callback_data=f"sd:{sell_id}", style="danger")
        ]])
        await call.message.answer(
            f'{E_BOX} <b>Pending Gmail Sell Request #{sell_id}</b>\n\n'
            f'{E_USER} <b>User ID:</b> <code>{user_id}</code>\n'
            f'{E_MONEY} <b>Rate:</b> ₹{amount:.2f}'
            f'{claimed_str}\n\n'
            f'📝 <b>Details:</b>\n{formatted_details}',
            reply_markup=kb,
            parse_mode=ParseMode.HTML
        )

    if hold_rows:
        kb = InlineKeyboardBuilder()
        kb.button(**bk("📥", f"Copy All & Send All to Holder ({len(hold_rows)})"), callback_data="hold_sendall:sell", style="success")
        await call.message.answer(
            f"{E_HOLDER} <b>Copy all credentials & move every request above to the Holder in one tap.</b>",
            parse_mode=ParseMode.HTML, reply_markup=kb.as_markup()
        )

@dp.callback_query(F.data == "admin_view_pending_tasks")
async def cb_admin_view_pending_tasks(call: CallbackQuery):
    await call.answer()
    if call.from_user.id != ADMIN_ID:
        return

    async with db_pool.acquire() as conn:
        hold_rows = await conn.fetch('''
            SELECT h.*
            FROM holds h
            WHERE h.kind = 'task' AND h.status = 'pending_review'
            ORDER BY h.id ASC
        ''')
        legacy_rows = await conn.fetch('''
            SELECT t.id, t.title, t.details, t.reward, ta.user_id
            FROM tasks t
            JOIN task_assignments ta ON t.id = ta.task_id
            WHERE t.status = 'pending_review'
              AND NOT EXISTS (SELECT 1 FROM holds h WHERE h.kind = 'task' AND h.ref_id = t.id AND h.status = 'pending_review')
            ORDER BY ta.assigned_at ASC
        ''')

    if not hold_rows and not legacy_rows:
        empty_txt = f"{E_EMPTY} <b>No pending task submissions found!</b>"
        try:
            await call.message.edit_text(empty_txt, parse_mode=ParseMode.HTML, reply_markup=get_back_inline_keyboard("back_pending_reviews"))
        except TelegramBadRequest as e:
            if "message is not modified" not in str(e):
                await call.message.answer(empty_txt, parse_mode=ParseMode.HTML, reply_markup=get_back_inline_keyboard("back_pending_reviews"))
        return

    if hold_rows:
        await _send_hold_cards(call, hold_rows, f"{E_UP} <b>Displaying {len(hold_rows)} pending task submission(s):</b>")

    if legacy_rows:
        await call.message.answer(f"{E_UP} <b>Older task submission(s) (no hold): {len(legacy_rows)}</b>", parse_mode=ParseMode.HTML)
    for r in legacy_rows:
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
            InlineKeyboardButton(text='Approve', icon_custom_emoji_id=ICON_OK, callback_data=f'ta:{task_id}', style="success"),
            InlineKeyboardButton(text='Decline', icon_custom_emoji_id=ICON_NO, callback_data=f'td:{task_id}', style="danger")
        ]])
        await call.message.answer(
            f'{E_UP} <b>Pending Task Submission</b>\n\n'
            f'{E_USER} <b>User ID:</b> <code>{user_id}</code>\n'
            f'🆔 <b>Task #{task_id}</b>\n'
            f'{E_MAIL} <b>Email:</b> <code>{_esc(email)}</code>\n'
            f'{E_KEY} <b>Password:</b> <code>{_esc(password)}</code>\n'
            f'{E_MONEY} <b>Reward:</b> ₹{reward}',
            reply_markup=kb,
            parse_mode=ParseMode.HTML
        )

    if hold_rows:
        kb = InlineKeyboardBuilder()
        kb.button(**bk("📥", f"Copy All & Send All to Holder ({len(hold_rows)})"), callback_data="hold_sendall:task", style="success")
        await call.message.answer(
            f"{E_HOLDER} <b>Copy all credentials & move every submission above to the Holder in one tap.</b>",
            parse_mode=ParseMode.HTML, reply_markup=kb.as_markup()
        )

# ============================================
# NEW: HOLD / HOLDER ACTIONS (ADMIN)
# ============================================

@dp.callback_query(F.data.startswith("hold_copy:"))
async def cb_hold_copy_fallback(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        await call.answer()
        return
    await call.answer()
    h = await get_hold(int(call.data.split(":")[1]))
    if not h:
        return
    await call.message.answer(f"<code>{_esc(hold_creds_text(h))}</code>", parse_mode=ParseMode.HTML)

@dp.callback_query(F.data.startswith("hold_send:"))
async def cb_hold_send(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        await call.answer()
        return
    hold_id = int(call.data.split(":")[1])
    async with db_pool.acquire() as conn:
        h = await move_hold_to_holder(conn, hold_id)
    if not h:
        await call.answer("⚠️ This request is already processed!", show_alert=True)
        return
    await call.answer("📥 Sent to Holder")
    await refresh_hold_card_message(call.message.chat.id, call.message.message_id, hold_id)

@dp.callback_query(F.data.startswith("hold_sendall:"))
async def cb_hold_send_all(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        await call.answer()
        return
    kind = call.data.split(":")[1]
    if kind not in ('task', 'sell'):
        await call.answer()
        return
    await call.answer("Processing...")

    moved = []
    async with db_pool.acquire() as conn:
        ids = [r['id'] for r in await conn.fetch(
            "SELECT id FROM holds WHERE kind=$1 AND status='pending_review' ORDER BY id ASC", kind
        )]
        for hid in ids:
            h = await move_hold_to_holder(conn, hid)
            if h:
                moved.append(h)

    if not moved:
        try:
            await call.message.edit_text(f"{E_EMPTY} <b>Nothing left to send - everything was already processed.</b>", parse_mode=ParseMode.HTML, reply_markup=None)
        except Exception:
            pass
        return

    try:
        await call.message.edit_text(
            f"{E_OK} <b>{len(moved)} {hold_kind_label(kind)} request(s) moved to the Holder.</b>\n"
            f"{E_HOLDER} Credentials are posted below (gmail / password / 2fakey / app password).",
            parse_mode=ParseMode.HTML, reply_markup=None
        )
    except Exception:
        pass

    blocks = [_esc(hold_creds_text(h)) for h in moved]
    for chunk in chunk_blocks(blocks, limit=3500, sep="\n\n"):
        await call.message.answer(f"<code>{chunk}</code>", parse_mode=ParseMode.HTML)

@dp.callback_query(F.data.startswith("hold_rel:"))
async def cb_hold_release(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        await call.answer()
        return
    hold_id = int(call.data.split(":")[1])
    h = await release_hold(hold_id)
    if not h:
        await call.answer("⚠️ This request is already processed!", show_alert=True)
        return
    await call.answer("🔓 Released!")
    await refresh_hold_card_message(call.message.chat.id, call.message.message_id, hold_id)
    notify_hold_released(h)

def notify_hold_released(h: dict):
    """Fires the user (and referral) notifications for a hold that just moved in_holder -> released.
    Shared by the single Release button and the bulk /release command."""
    uid = h['user_id']
    label = hold_kind_label(h['kind'])
    ref_id = h['ref_id']
    amt = float(h['amount'] or 0)

    async def notify_user():
        ud = await get_user_data(uid)
        amt_str = format_currency(amt, ud['currency'] if ud else "USD")
        await send_user_notification(
            uid,
            f"{E_PARTY} <b>{label} #{ref_id} Accepted!</b>\n\n"
            f"{E_MONEY} <b>+{amt_str}</b> added to your Balance\n"
            f"{E_HOLD} <b>-{amt_str}</b> removed from your Hold Balance",
            parse_mode=ParseMode.HTML
        )
    asyncio.create_task(notify_user())

    if h.get('ref_info'):
        ref_uid, ref_reward = h['ref_info']
        async def notify_ref():
            rd = await get_user_data(ref_uid)
            ref_amt_str = format_currency(ref_reward, rd['currency'] if rd else "USD")
            kind_word = "task" if h['kind'] == 'task' else "sell"
            await send_user_notification(
                ref_uid,
                f'🎉 Your referral <code>{uid}</code> {kind_word} gmail got approved and <b>{ref_amt_str}</b> credited to your balance!',
                parse_mode=ParseMode.HTML
            )
        asyncio.create_task(notify_ref())

@dp.message(Command("release"), StateFilter("*"))
async def admin_cmd_release(message: Message, command: CommandObject):
    """/release 2  -> releases the oldest 2 items currently in the Holder.
    /release all -> releases every item currently in the Holder."""
    if message.from_user.id != ADMIN_ID:
        return

    args = (command.args or "").strip().lower()
    if not args:
        await message.answer(
            "<tg-emoji emoji-id=\"5420323339723881652\">⚠️</tg-emoji> <b>Usage:</b>\n"
            "<code>/release 2</code> - release the oldest 2 items in the Holder\n"
            "<code>/release all</code> - release everything in the Holder",
            parse_mode=ParseMode.HTML
        )
        return

    async with db_pool.acquire() as conn:
        holder_count = await conn.fetchval("SELECT COUNT(*) FROM holds WHERE status='in_holder'") or 0

    if holder_count == 0:
        await message.answer(f"{E_EMPTY} <b>The Holder is empty. Nothing to release.</b>", parse_mode=ParseMode.HTML)
        return

    if args == "all":
        requested = holder_count
    else:
        num_match = re.search(r'\d+', args)
        if not num_match:
            await message.answer(
                "❌ Invalid format. Use <code>/release 2</code> or <code>/release all</code>.",
                parse_mode=ParseMode.HTML
            )
            return
        requested = int(num_match.group(0))
        if requested <= 0:
            await message.answer("❌ Please send a number greater than 0.", parse_mode=ParseMode.HTML)
            return

    shortage_note = ""
    if requested > holder_count:
        shortage_note = (
            f"\n\n{E_WARN} <b>The Holder only has {holder_count} item(s).</b> "
            f"Released all {holder_count} instead of the requested {requested}."
        )
        requested = holder_count

    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id FROM holds WHERE status='in_holder' ORDER BY holder_at ASC NULLS LAST, id ASC LIMIT $1",
            requested
        )

    status_msg = await message.answer(f"{E_TIME} <b>Releasing {len(rows)} item(s)...</b>", parse_mode=ParseMode.HTML)

    released_count = 0
    total_amt = 0.0
    for r in rows:
        h = await release_hold(r['id'])
        if h:
            released_count += 1
            total_amt += float(h.get('amount') or 0)
            notify_hold_released(h)

    result_text = (
        f"{E_OK} <b>Release Complete!</b>\n\n"
        f"{E_HOLDER} <b>Released:</b> <code>{released_count}</code> item(s)\n"
        f"{E_MONEY} <b>Total Amount Released:</b> ₹{total_amt:.2f}"
        f"{shortage_note}"
    )
    try:
        await status_msg.edit_text(result_text, parse_mode=ParseMode.HTML)
    except Exception:
        await message.answer(result_text, parse_mode=ParseMode.HTML)

@dp.callback_query(F.data.startswith("hold_rej:"))
async def cb_hold_reject(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        await call.answer()
        return
    hold_id = int(call.data.split(":")[1])
    h = await get_hold(hold_id)
    if not h or h['status'] not in ('pending_review', 'in_holder'):
        await call.answer("⚠️ This request is already processed!", show_alert=True)
        return
    await call.answer()
    await state.set_state(AdminState.waiting_for_hold_reject_reason)
    await state.update_data(hold_id=hold_id, admin_msg_id=call.message.message_id, admin_chat_id=call.message.chat.id)
    label = hold_kind_label(h['kind'])
    ref_id = h['ref_id']
    await call.message.answer(
        f'{E_WARN} <b>Please reply with the reason for rejecting {label} #{ref_id}:</b>',
        parse_mode=ParseMode.HTML
    )

@dp.message(AdminState.waiting_for_hold_reject_reason, F.text, ~F.text.startswith("/"), ~F.text.in_(MENU_BUTTONS))
async def process_hold_reject_reason(message: Message, state: FSMContext):
    data = await state.get_data()
    hold_id = data.get('hold_id')
    admin_msg_id = data.get('admin_msg_id')
    admin_chat_id = data.get('admin_chat_id') or message.chat.id
    reason = message.text.strip()
    await state.clear()

    h = await reject_hold(hold_id, reason)
    if not h:
        await message.answer("⚠️ This request is already processed!")
        return

    if admin_msg_id:
        await refresh_hold_card_message(admin_chat_id, admin_msg_id, hold_id)

    uid = h['user_id']
    label = hold_kind_label(h['kind'])
    ref_id = h['ref_id']
    amt = float(h['amount'] or 0)

    async def notify_user():
        ud = await get_user_data(uid)
        amt_str = format_currency(amt, ud['currency'] if ud else "USD")
        pool_note = "\n\n🛡 The task has been returned to the pool." if h['kind'] == 'task' else ""
        await send_user_notification(
            uid,
            f"{E_WARN} <b>Your {label} #{ref_id} was rejected.</b>\n\n"
            f"💬 <b>Reason:</b> {_esc(reason)}\n\n"
            f"{E_HOLD} <b>-{amt_str}</b> removed from your Hold Balance{pool_note}",
            parse_mode=ParseMode.HTML
        )
    asyncio.create_task(notify_user())

    await message.answer(f'{E_OK} Rejection reason recorded and user notified.', parse_mode=ParseMode.HTML)

# ============================================
# NEW: HOLDER MENU (ADMIN)
# ============================================

async def render_holder_dashboard():
    async with db_pool.acquire() as conn:
        t_count = await conn.fetchval("SELECT COUNT(*) FROM holds WHERE kind='task' AND status='in_holder'") or 0
        s_count = await conn.fetchval("SELECT COUNT(*) FROM holds WHERE kind='sell' AND status='in_holder'") or 0
        total_amt = await conn.fetchval("SELECT COALESCE(SUM(amount), 0) FROM holds WHERE status='in_holder'") or 0.0
        due = await conn.fetchval("SELECT COUNT(*) FROM holds WHERE status='in_holder' AND release_at <= $1", datetime.utcnow()) or 0

    if t_count + s_count == 0:
        return f"{E_EMPTY} <b>The Holder is empty.</b>\n\nSend requests here from <b>Pending Reviews</b>.", None

    text = (
        f"{E_HOLDER} <b>Holder Dashboard</b>\n\n"
        f"{E_BOX} <b>Sell Gmail in Holder:</b> <code>{s_count}</code>\n"
        f"{E_UP} <b>Task Gmail in Holder:</b> <code>{t_count}</code>\n"
        f"{E_MONEY} <b>Total Amount Held:</b> ₹{float(total_amt):.2f}\n"
        f"{E_CAL} <b>{HOLD_DAYS}-day period completed:</b> <code>{due}</code>\n\n"
        f"ℹ️ <b>Quick release:</b> <code>/release 2</code> releases the oldest 2, <code>/release all</code> releases everything.\n\n"
        f"Click an option below to view the held accounts:"
    )
    kb = InlineKeyboardBuilder()
    _kb_btn(kb, "Sell Gmail", "5377548235709619284", callback_data="admin_view_holder_sell", style="primary")
    _kb_btn(kb, "Task Gmail", "5197269100878907942", callback_data="admin_view_holder_task", style="primary")
    kb.adjust(2)
    return text, kb.as_markup()

@dp.message(F.text == "Holder", StateFilter("*"))
async def admin_btn_holder(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.clear()
    text, kb = await render_holder_dashboard()
    await message.answer(text, parse_mode=ParseMode.HTML, reply_markup=kb or get_admin_menu_keyboard())

@dp.callback_query(F.data == "back_holder")
async def cb_back_holder(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        await call.answer()
        return
    await call.answer()
    await state.clear()
    text, kb = await render_holder_dashboard()
    try:
        await call.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=kb or get_back_inline_keyboard("back_admin_menu"))
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            await call.message.answer(text, parse_mode=ParseMode.HTML, reply_markup=kb or get_back_inline_keyboard("back_admin_menu"))

@dp.callback_query(F.data.in_({"admin_view_holder_sell", "admin_view_holder_task"}))
async def cb_admin_view_holder(call: CallbackQuery):
    await call.answer()
    if call.from_user.id != ADMIN_ID:
        return
    kind = 'sell' if call.data.endswith("sell") else 'task'

    async with db_pool.acquire() as conn:
        rows = await conn.fetch('''
            SELECT h.*, s.claimed_by
            FROM holds h
            LEFT JOIN pending_sells s ON (h.kind = 'sell' AND s.id = h.ref_id)
            WHERE h.kind = $1 AND h.status = 'in_holder'
            ORDER BY h.release_at ASC, h.id ASC
        ''', kind)

    if not rows:
        empty_txt = f"{E_EMPTY} <b>No {hold_kind_label(kind)} in the Holder!</b>"
        try:
            await call.message.edit_text(empty_txt, parse_mode=ParseMode.HTML, reply_markup=get_back_inline_keyboard("back_holder"))
        except TelegramBadRequest as e:
            if "message is not modified" not in str(e):
                await call.message.answer(empty_txt, parse_mode=ParseMode.HTML, reply_markup=get_back_inline_keyboard("back_holder"))
        return

    icon = E_BOX if kind == 'sell' else E_UP
    await _send_hold_cards(call, rows, f"{icon} <b>Displaying {len(rows)} {hold_kind_label(kind)} in the Holder:</b>")

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
            f'📅 <b>Date:</b> {to_ist(r["created_at"]).strftime("%Y-%m-%d %H:%M:%S") + " IST"}',
            reply_markup=kb.as_markup(),
            parse_mode=ParseMode.HTML
        )

@dp.message(F.text == "Chat", StateFilter("*"))
async def admin_btn_chat(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.set_state(AdminState.waiting_for_chat_user_id)
    await message.answer("💬 Send the numeric <b>User ID</b> you want to message:", parse_mode=ParseMode.HTML)

@dp.message(AdminState.waiting_for_chat_user_id, ~F.text.startswith("/"), ~F.text.in_(MENU_BUTTONS))
async def process_chat_user_id_step(message: Message, state: FSMContext):
    await cleanup_last_menu(message, state)
    try:
        target_user_id = int(message.text.strip())
        await state.update_data(chat_target_user_id=target_user_id)
        await state.set_state(AdminState.waiting_for_chat_message)
        await message.answer(f"✉️ <b>Now send the message you want to deliver to User <code>{target_user_id}</code>:</b>", parse_mode=ParseMode.HTML)
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
        await message.answer(f"<tg-emoji emoji-id=\"6217663806110175239\">✅</tg-emoji> <b>Message successfully sent to User <code>{target_user_id}</code>!</b>", parse_mode=ParseMode.HTML, reply_markup=get_admin_menu_keyboard())
    except Exception as e:
        await message.answer(f"<tg-emoji emoji-id=\"5274099962655816924\">❌</tg-emoji> Failed to send message to User <code>{target_user_id}</code>.\n\nError: <code>{_esc(e)}</code>", parse_mode=ParseMode.HTML, reply_markup=get_admin_menu_keyboard())

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
    await call.message.answer("<tg-emoji emoji-id=\"5870458774455587120\">👤</tg-emoji> Send the numeric <b>User ID</b> whose task you want to unassign:", parse_mode=ParseMode.HTML)

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
                WHERE ta.user_id=$1 AND t.status IN ('assigned', 'verifying')
            ''', target_id)
            if not assigned:
                await message.answer(f"<tg-emoji emoji-id=\"5262831879731555779\">📭</tg-emoji> User <code>{target_id}</code> does not have any active task assigned.", parse_mode=ParseMode.HTML, reply_markup=get_admin_menu_keyboard())
                await state.clear()
                return

            task_id = assigned['task_id']
            task_msg_id = assigned['message_id']

            async with conn.transaction():
                await conn.execute("DELETE FROM task_assignments WHERE task_id=$1", task_id)
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

        await message.answer(f"<tg-emoji emoji-id=\"6217663806110175239\">✅</tg-emoji> <b>Successfully unassigned Task #{task_id} from User <code>{target_id}</code>, removed task message, and returned it to pool.</b>", parse_mode=ParseMode.HTML, reply_markup=get_admin_menu_keyboard())

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
        assigned_count = await conn.fetchval("SELECT COUNT(*) FROM tasks WHERE status IN ('assigned', 'verifying')") or 0

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
        rows = await conn.fetch("SELECT id, title, details, reward, added_by, status, fixed_password FROM tasks WHERE status IN ('available', 'assigned', 'verifying')")

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
                    "INSERT INTO dustbin_tasks (title, details, reward, added_by, fixed_password) VALUES ($1, $2, $3, $4, $5)",
                    r['title'], r['details'], r['reward'], r['added_by'], r['fixed_password']
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

    removed_str = to_ist(row['removed_at']).strftime("%b %d, %Y %I:%M %p") if row['removed_at'] else "Unknown"

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
    kb.row(InlineKeyboardButton(text="Back", icon_custom_emoji_id="5875082500023258804", callback_data="back_dustbin_menu"))
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
        row = await conn.fetchrow("SELECT title, details, reward, added_by, fixed_password FROM dustbin_tasks WHERE id=$1", dustbin_id)
        if not row:
            await call.answer("⚠️ This item is no longer in the Dustbin.", show_alert=True)
            return

        async with conn.transaction():
            await conn.execute(
                "INSERT INTO tasks (title, details, reward, status, added_by, fixed_password) VALUES ($1, $2, $3, 'available', $4, $5)",
                row['title'], row['details'], row['reward'], row['added_by'], row['fixed_password']
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
            "UPDATE dustbin_tasks SET title=$1, details=$2, fixed_password=NULL WHERE id=$3",
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
    # NOTE: button labels can't render <tg-emoji> HTML (it showed up as raw text). A button carries ONE
    # premium emoji via icon_custom_emoji_id, so the section keeps its icon and the Set / Not Set state
    # is shown with the button colour (green = Set, red = Not Set) plus the label text.
    kb = InlineKeyboardBuilder()
    sections = (
        ("Tasks Video", TASK_VIDEO_LINK, "5197269100878907942", "video_set:tasks"),
        ("Sell Video", SELL_VIDEO_LINK, "5377548235709619284", "video_set:sell"),
        ("How To Use Bot Video", HOWTO_VIDEO_LINK, "5436113877181941026", "video_set:howto"),
    )
    for label, link, icon_id, cb in sections:
        kb.button(
            text=f"{label}: {'Set' if link else 'Not Set'}",
            icon_custom_emoji_id=icon_id,
            callback_data=cb,
            style="success" if link else "danger"
        )
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

# ============================================
# ADMIN TOOL: FIND PREMIUM EMOJI IDS  (/emojiid)
# ============================================

@dp.message(Command("emojiid"), StateFilter("*"))
async def admin_cmd_emojiid(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.clear()
    await state.set_state(AdminState.waiting_for_emoji_id)
    await message.answer(
        "<b>Premium Emoji ID Finder</b>\n\n"
        "Send (or forward) a message that contains premium emoji and I will reply with their ids, "
        "already formatted to paste into <code>PREMIUM_EMOJI</code> in the code.\n\n"
        "Send /cancel when you are done.",
        parse_mode=ParseMode.HTML
    )

@dp.message(AdminState.waiting_for_emoji_id, ~F.text.startswith("/"))
async def admin_emojiid_collect(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    content = message.text or message.caption or ""
    entities = message.entities or message.caption_entities or []
    found = []
    for ent in entities:
        if ent.type == "custom_emoji" and ent.custom_emoji_id:
            found.append((ent.extract_from(content), ent.custom_emoji_id))
    if not found:
        await message.answer("No premium emoji found in that message. Send a message containing premium emoji.")
        return
    seen, out_lines = set(), []
    for char, cid in found:
        if cid in seen:
            continue
        seen.add(cid)
        out_lines.append(f'    "{char}": "{cid}",')
    await message.answer(
        "<b>Found:</b>\n<pre>" + html.escape("\n".join(out_lines)) + "</pre>",
        parse_mode=ParseMode.HTML
    )

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
            'in_holder': '🟣',
            'verifying': '🔵',
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
            'in_holder': '🟣',
            'verifying': '🔵',
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
        dt_str = to_ist(row['assigned_at']).strftime("%b %d, %Y %I:%M %p")

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
        await message.answer(f"<tg-emoji emoji-id=\"6217663806110175239\">✅</tg-emoji> <b>Added ₹{amount:.2f} to User <code>{target_id}</code>'s balance.</b>", parse_mode=ParseMode.HTML, reply_markup=get_admin_menu_keyboard())

        asyncio.create_task(send_user_notification(
            target_id,
            f"<tg-emoji emoji-id=\"5417924076503062111\">💰</tg-emoji> <b>Admin credited your balance!</b>\n+₹{amount:.2f} added to your account.",
            parse_mode=ParseMode.HTML
        ))
    except Exception as e:
        await message.answer(f"<tg-emoji emoji-id=\"5274099962655816924\">❌</tg-emoji> Invalid format or error: <code>{_esc(e)}</code>", parse_mode=ParseMode.HTML, reply_markup=get_admin_menu_keyboard())
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
        await message.answer(f"<tg-emoji emoji-id=\"6217663806110175239\">✅</tg-emoji> <b>Deducted ₹{amount:.2f} from User <code>{target_id}</code>'s balance.</b>", parse_mode=ParseMode.HTML, reply_markup=get_admin_menu_keyboard())

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
        await message.answer(f"<tg-emoji emoji-id=\"5274099962655816924\">❌</tg-emoji> Invalid format or error: <code>{_esc(e)}</code>", parse_mode=ParseMode.HTML, reply_markup=get_admin_menu_keyboard())
    await state.clear()

@dp.message(F.text == "Check Balance", StateFilter("*"))
async def admin_btn_check_balance(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.set_state(AdminState.waiting_for_check_balance)
    await message.answer("🔎 Send the numeric User ID to check:", parse_mode=ParseMode.HTML)

@dp.message(AdminState.waiting_for_check_balance, ~F.text.startswith("/"), ~F.text.in_(MENU_BUTTONS))
async def process_check_balance_step(message: Message, state: FSMContext):
    await cleanup_last_menu(message, state)
    try:
        target_id = int(message.text.strip())
        user_data = await get_user_data(target_id)
        if not user_data:
            await message.answer(f"<tg-emoji emoji-id=\"5262831879731555779\">📭</tg-emoji> User <code>{target_id}</code> not found in database.", parse_mode=ParseMode.HTML, reply_markup=get_admin_menu_keyboard())
            await state.clear()
            return

        bal = user_data['balance']
        upi = user_data['upi']
        usdt = user_data['usdt_address']
        hold_bal = user_data['hold_balance'] or 0.0

        await message.answer(
            f"<tg-emoji emoji-id=\"5870458774455587120\">👤</tg-emoji> <b>User Information for <code>{target_id}</code>:</b>\n\n"
            f"• <b>Balance:</b> ₹{bal:.2f}\n"
            f"• <b>Hold Balance:</b> ₹{hold_bal:.2f}\n"
            f"• <b>UPI:</b> <code>{_esc(upi)}</code>\n"
            f"• <b>USDT BEP-20:</b> <code>{_esc(usdt)}</code>",
            parse_mode=ParseMode.HTML,
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

    text = "🏆 <b>Top 10 Balance Holders</b>\n\n"
    for idx, r in enumerate(rows, start=1):
        text += f"<b>{idx}.</b> User ID: <code>{r['user_id']}</code> — <b>₹{r['balance']:.2f}</b>\n"

    await message.answer(text, parse_mode=ParseMode.HTML, reply_markup=get_admin_menu_keyboard())

@dp.message(F.text == "Transactions", StateFilter("*"))
async def admin_btn_transactions(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.set_state(AdminState.waiting_for_user_transactions)
    await message.answer("<tg-emoji emoji-id=\"5445353829304387411\">💳</tg-emoji> Send the User ID to check their transaction history:", parse_mode=ParseMode.HTML)

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
        holder_count = await conn.fetchval("SELECT COUNT(*) FROM holds WHERE status='in_holder'")

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
        f"{E_HOLDER} <b>In Holder:</b> <code>{holder_count or 0}</code>\n"
        f"<tg-emoji emoji-id=\"5444856076954520455\">💸</tg-emoji> <b>Pending Withdrawals:</b> <code>{pending_withdrawals or 0}</code>"
    )
    await message.answer(text, parse_mode=ParseMode.HTML, reply_markup=get_admin_menu_keyboard())

@dp.message(F.text == "Ban User", StateFilter("*"))
async def admin_btn_ban_user(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.set_state(AdminState.waiting_for_ban_user)
    await message.answer("<tg-emoji emoji-id=\"5240241223632954241\">🚫</tg-emoji> Send the numeric User ID to ban:", parse_mode=ParseMode.HTML)

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
            await message.answer(f"<tg-emoji emoji-id=\"5420323339723881652\">⚠️</tg-emoji> User <code>{target_id}</code> is already banned.", parse_mode=ParseMode.HTML, reply_markup=get_admin_menu_keyboard())
            await state.clear()
            return

        async with db_pool.acquire() as conn:
            await conn.execute("INSERT INTO banned_users (user_id) VALUES ($1) ON CONFLICT DO NOTHING", target_id)

        BANNED_USERS_CACHE.add(target_id)
        await message.answer(f"<tg-emoji emoji-id=\"5240241223632954241\">🚫</tg-emoji> <b>User <code>{target_id}</code> has been banned!</b>", parse_mode=ParseMode.HTML, reply_markup=get_admin_menu_keyboard())
    except ValueError:
        await message.answer("❌ Invalid User ID.", reply_markup=get_admin_menu_keyboard())
    await state.clear()

@dp.message(F.text == "Unban User", StateFilter("*"))
async def admin_btn_unban_user(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.set_state(AdminState.waiting_for_unban_user)
    await message.answer("<tg-emoji emoji-id=\"6217663806110175239\">✅</tg-emoji> Send the numeric User ID to unban:", parse_mode=ParseMode.HTML)

@dp.message(AdminState.waiting_for_unban_user, ~F.text.startswith("/"), ~F.text.in_(MENU_BUTTONS))
async def process_unban_user_step(message: Message, state: FSMContext):
    await cleanup_last_menu(message, state)
    try:
        target_id = int(message.text.strip())

        async with db_pool.acquire() as conn:
            res = await conn.execute("DELETE FROM banned_users WHERE user_id=$1", target_id)

        if res == "DELETE 0" and target_id not in BANNED_USERS_CACHE:
            await message.answer(f"<tg-emoji emoji-id=\"5420323339723881652\">⚠️</tg-emoji> User <code>{target_id}</code> is not currently banned, so they cannot be unbanned.", parse_mode=ParseMode.HTML, reply_markup=get_admin_menu_keyboard())
            await state.clear()
            return

        BANNED_USERS_CACHE.discard(target_id)
        await message.answer(f"<tg-emoji emoji-id=\"6217663806110175239\">✅</tg-emoji> <b>User <code>{target_id}</code> has been unbanned!</b>", parse_mode=ParseMode.HTML, reply_markup=get_admin_menu_keyboard())
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
    await state.update_data(broadcast_btns=[])
    await state.set_state(AdminState.waiting_for_broadcast_buttons)
    text, markup = render_broadcast_btn_picker([])
    await message.answer(text, parse_mode=ParseMode.HTML, reply_markup=markup)

# ---------- Broadcast: attach direct-link buttons ----------

# (key, picker emoji, label, premium icon id, callback_data) - identical to the main menu buttons
BROADCAST_BTN_OPTIONS = [
    ("task", "📧", "Get Task", "5807510737332998730", "menu_get_task"),
    ("balance", "💰", "Balance", "5417924076503062111", "menu_balance"),
    ("sell", "🤑", "Sell Gmail", "5377548235709619284", "menu_sell_gmail"),
    ("history", "🧾", "History", "5440410042773824003", "menu_history"),
    ("ref", "👥", "Referrals", "5391292736647209211", "menu_referrals"),
    ("accounts", "🦆", "My Accounts", "6300651782678781823", "menu_my_accounts"),
    ("leaderboard", "🏆", "Referral Leaderboard", None, "ref_leaderboard"),
    ("settings", "⚙️", "Settings", "5893161718179173515", "menu_settings"),
    ("support", "🛠", "Support", "5471960722206366390", "menu_support"),
]

def build_broadcast_markup(selected):
    """Inline buttons under the broadcast - same look and same callbacks as the main menu. None when nothing is selected."""
    if not selected:
        return None
    chosen = [o for o in BROADCAST_BTN_OPTIONS if o[0] in selected]
    if not chosen:
        return None

    def _btn(o):
        if o[3] is None:
            # Leaderboard: same look as the button on the Referrals screen (green, premium icon when available)
            return InlineKeyboardButton(**bk(o[1], o[2]), callback_data=o[4], style="success")
        return InlineKeyboardButton(text=o[2], icon_custom_emoji_id=o[3], callback_data=o[4])

    # Same layout idea as the menu: pairs, then Leaderboard / Settings / Support each on their own row
    single_keys = ("leaderboard", "settings", "support")
    normal = [o for o in chosen if o[0] not in single_keys]
    kb = InlineKeyboardBuilder()
    for i in range(0, len(normal), 2):
        kb.row(*[_btn(o) for o in normal[i:i + 2]])
    for o in chosen:
        if o[0] in single_keys:
            kb.row(_btn(o))
    return kb.as_markup()

def render_broadcast_btn_picker(selected):
    kb = InlineKeyboardBuilder()
    for key, emoji, label, _icon, _cb in BROADCAST_BTN_OPTIONS:
        mark = "✅ " if key in selected else ""
        kb.button(text=f"{mark}{emoji} {label}", callback_data=f"bcbtn:t:{key}")
    kb.button(text="Continue ➜", callback_data="bcbtn:go", style="success")
    kb.button(text="Skip (no buttons)", callback_data="bcbtn:none")
    kb.adjust(2, 2, 2, 2, 1, 1, 1)
    text = (
        "🔗 <b>Attach Buttons</b>\n\n"
        "Pick the main-menu buttons to show under this broadcast. They look and work exactly like the bot's menu buttons.\n\n"
        f"Selected: <b>{len(selected)}</b>"
    )
    return text, kb.as_markup()

def get_broadcast_audience_keyboard():
    kb = InlineKeyboardBuilder()
    kb.button(text="24 Hours", icon_custom_emoji_id="5391032818111363540", callback_data="bcdur:24")
    kb.button(text="48 Hours", icon_custom_emoji_id="5391032818111363540", callback_data="bcdur:48")
    kb.button(text="72 Hours", icon_custom_emoji_id="5391032818111363540", callback_data="bcdur:72")
    kb.button(text="All Users", icon_custom_emoji_id="5391292736647209211", callback_data="bcdur:all")
    kb.adjust(1)
    return kb.as_markup()

@dp.callback_query(F.data.startswith("bcbtn:"), AdminState.waiting_for_broadcast_buttons)
async def cb_broadcast_buttons(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    await call.answer()
    data = await state.get_data()
    selected = list(data.get("broadcast_btns") or [])
    parts = call.data.split(":")
    action = parts[1] if len(parts) > 1 else ""

    if action == "t" and len(parts) > 2:
        key = parts[2]
        if key in {o[0] for o in BROADCAST_BTN_OPTIONS}:
            if key in selected:
                selected.remove(key)
            else:
                selected.append(key)
            await state.update_data(broadcast_btns=selected)
            text, markup = render_broadcast_btn_picker(selected)
            try:
                await call.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
            except Exception:
                pass
        return

    if action == "none":
        selected = []
        await state.update_data(broadcast_btns=[])
    elif action != "go":
        return

    await state.set_state(AdminState.waiting_for_broadcast_target)
    note = ""
    if selected and data.get("broadcast_is_forwarded"):
        note = "\n\n<i>Note: buttons can't be attached to a forwarded message, so it will be sent as a copy (without the 'Forwarded from' header).</i>"
    audience_text = (
        "<tg-emoji emoji-id=\"5332724926216428039\">📢</tg-emoji> <b>Select the target audience for this broadcast:</b>\n\n"
        "Choose which users (based on their last activity) should receive this message."
        f"{note}"
    )
    try:
        await call.message.edit_text(audience_text, parse_mode=ParseMode.HTML, reply_markup=get_broadcast_audience_keyboard())
    except Exception:
        await call.message.answer(audience_text, parse_mode=ParseMode.HTML, reply_markup=get_broadcast_audience_keyboard())

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
    broadcast_markup = build_broadcast_markup(data.get("broadcast_btns") or [])

    for idx, u in enumerate(users, start=1):
        target_id = u['user_id']
        try:
            if is_forwarded and not broadcast_markup:
                await bot.forward_message(
                    chat_id=target_id,
                    from_chat_id=from_chat_id,
                    message_id=message_id
                )
            else:
                await bot.copy_message(
                    chat_id=target_id,
                    from_chat_id=from_chat_id,
                    message_id=message_id,
                    reply_markup=broadcast_markup
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

def build_change_values_text() -> str:
    """Builds the 'Change System Rates & Limits' panel text (same output as the old repeated blocks)."""
    task_usd = DEFAULT_TASK_RATE / USD_TO_INR
    sell_usd = GMAIL_SELL_RATE / USD_TO_INR
    min_w_usd = MIN_WITHDRAWAL_AMT / USD_TO_INR

    return (
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
        f"{E_LOCK} <b>Task 2FA Step:</b> {'<tg-emoji emoji-id=\"5416081784641168838\">🟢</tg-emoji> ON' if TWOFA_TASK_STATUS else '<tg-emoji emoji-id=\"5411225014148014586\">🔴</tg-emoji> OFF (validate & submit instantly)'}\n"
        f"{E_LOCK} <b>Sell 2FA Step:</b> {'<tg-emoji emoji-id=\"5416081784641168838\">🟢</tg-emoji> ON' if TWOFA_SELL_STATUS else '<tg-emoji emoji-id=\"5411225014148014586\">🔴</tg-emoji> OFF (validate, password & submit)'}\n"
        f"━━━━━━━━━━━━━━━━━━\n\n"
        f"Select an option below to update:"
    )

@dp.message(F.text == "Change Values", StateFilter("*"))
async def admin_btn_change_values(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.clear()
    await message.answer(build_change_values_text(), parse_mode=ParseMode.HTML, reply_markup=get_change_values_inline_keyboard())

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

    try:
        await call.message.edit_text(build_change_values_text(), parse_mode=ParseMode.HTML, reply_markup=get_change_values_inline_keyboard())
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

    try:
        await call.message.edit_text(build_change_values_text(), parse_mode=ParseMode.HTML, reply_markup=get_change_values_inline_keyboard())
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

    try:
        await call.message.edit_text(build_change_values_text(), parse_mode=ParseMode.HTML, reply_markup=get_change_values_inline_keyboard())
    except Exception:
        pass

@dp.callback_query(F.data == "admin_toggle_twofa_task")
async def cb_admin_toggle_twofa_task(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    global TWOFA_TASK_STATUS
    TWOFA_TASK_STATUS = not TWOFA_TASK_STATUS
    new_val = 'on' if TWOFA_TASK_STATUS else 'off'

    async with db_pool.acquire() as conn:
        await conn.execute("INSERT INTO bot_settings (key, value) VALUES ('twofa_task_status', $1) ON CONFLICT (key) DO UPDATE SET value = $1", new_val)

    await call.answer(
        f"Task 2FA Step is now {'ON' if TWOFA_TASK_STATUS else 'OFF - Task Gmail will validate & submit instantly'}!",
        show_alert=True
    )

    try:
        await call.message.edit_text(build_change_values_text(), parse_mode=ParseMode.HTML, reply_markup=get_change_values_inline_keyboard())
    except Exception:
        pass

@dp.callback_query(F.data == "admin_toggle_twofa_sell")
async def cb_admin_toggle_twofa_sell(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    global TWOFA_SELL_STATUS
    TWOFA_SELL_STATUS = not TWOFA_SELL_STATUS
    new_val = 'on' if TWOFA_SELL_STATUS else 'off'

    async with db_pool.acquire() as conn:
        await conn.execute("INSERT INTO bot_settings (key, value) VALUES ('twofa_sell_status', $1) ON CONFLICT (key) DO UPDATE SET value = $1", new_val)

    await call.answer(
        f"Sell 2FA Step is now {'ON' if TWOFA_SELL_STATUS else 'OFF - Gmail, Password then instant submit'}!",
        show_alert=True
    )

    try:
        await call.message.edit_text(build_change_values_text(), parse_mode=ParseMode.HTML, reply_markup=get_change_values_inline_keyboard())
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
        await message.answer("❌ Invalid format. Please provide values line by line like: <code>upi 5</code>", parse_mode=ParseMode.HTML, reply_markup=get_admin_menu_keyboard())
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
    await message.answer("<tg-emoji emoji-id=\"5262529363710060188\">🗑</tg-emoji> Send the Task ID to remove (e.g. <code>3</code>):", parse_mode=ParseMode.HTML)

@dp.message(AdminState.waiting_for_remove_task, ~F.text.startswith("/"), ~F.text.in_(MENU_BUTTONS))
async def process_remove_task_step(message: Message, state: FSMContext):
    await cleanup_last_menu(message, state)
    try:
        task_id = int(message.text.strip())
        async with db_pool.acquire() as conn:
            active_hold = await conn.fetchval(
                "SELECT id FROM holds WHERE kind='task' AND ref_id=$1 AND status IN ('pending_review', 'in_holder')", task_id
            )
            if active_hold:
                await message.answer(
                    f"⚠️ Task <code>#{task_id}</code> is under review / in the Holder. Release or reject it first, then remove it.",
                    parse_mode=ParseMode.HTML, reply_markup=get_admin_menu_keyboard()
                )
                await state.clear()
                return
            async with conn.transaction():
                await conn.execute("DELETE FROM task_assignments WHERE task_id=$1", task_id)
                res = await conn.execute("DELETE FROM tasks WHERE id=$1", task_id)

        if res == "DELETE 0":
            await message.answer(f"<tg-emoji emoji-id=\"5262831879731555779\">📭</tg-emoji> Task <code>#{task_id}</code> not found.", parse_mode=ParseMode.HTML, reply_markup=get_admin_menu_keyboard())
        else:
            await message.answer(f"<tg-emoji emoji-id=\"6217663806110175239\">✅</tg-emoji> <b>Task <code>#{task_id}</code> removed successfully!</b>", parse_mode=ParseMode.HTML, reply_markup=get_admin_menu_keyboard())
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

@dp.message(Command("payoutchannel"), StateFilter("*"))
@dp.message(F.text == "Payout Channel", StateFilter("*"))
async def set_payout_channel_command(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.set_state(AdminState.waiting_for_payout_channel)
    current = PAYOUT_CHANNEL if PAYOUT_CHANNEL else "Disabled"
    await message.answer(
        f"<tg-emoji emoji-id=\"5444856076954520455\">💸</tg-emoji> <b>Payout Channel Settings</b>\n\n"
        f"Currently set to: <code>{current}</code>\n\n"
        f"<tg-emoji emoji-id=\"5420323339723881652\">⚠️</tg-emoji> <b>The bot must be an admin of this channel</b> so it can post withdrawal requests and let you Pay/Reject from there.\n\n"
        f"Send the channel username (e.g. <code>@MyPayoutChannel</code>), a link (e.g. <code>https://t.me/MyPayoutChannel</code>), or a private channel's numeric ID (e.g. <code>-1001234567890</code>).\n\n"
        f"Only the bot owner and users who are <b>admins/owner of that channel</b> will be able to press Pay/Reject there.\n\n"
        f"<i>Type <code>none</code> to stop mirroring withdrawal requests to a channel.</i>",
        parse_mode=ParseMode.HTML
    )

@dp.message(AdminState.waiting_for_payout_channel, ~F.text.startswith("/"), ~F.text.in_(MENU_BUTTONS))
async def process_payout_channel_step(message: Message, state: FSMContext):
    await cleanup_last_menu(message, state)
    global PAYOUT_CHANNEL
    val = message.text.strip()

    if val.lower() == "none":
        PAYOUT_CHANNEL = None
        db_val = "off"
        msg = "<tg-emoji emoji-id=\"6217663806110175239\">✅</tg-emoji> <b>Payout Channel disabled.</b> Withdrawal requests will no longer be mirrored to a channel."
    else:
        if val.lstrip('-').isdigit():
            # Numeric channel ID (private channels use e.g. -1001234567890)
            PAYOUT_CHANNEL = int(val)
            db_val = val
        else:
            if "/" in val:
                val = "@" + val.split("/")[-1].replace("@", "")
            elif not val.startswith("@"):
                val = "@" + val
            PAYOUT_CHANNEL = val
            db_val = val

        # Sanity check: make sure the bot can actually see/post in this channel before saving.
        try:
            await bot.get_chat(PAYOUT_CHANNEL)
        except Exception as e:
            await message.answer(
                f"<tg-emoji emoji-id=\"5274099962655816924\">❌</tg-emoji> <b>Couldn't access that channel:</b> <code>{e}</code>\n\n"
                f"Make sure the bot has been added to the channel as an <b>admin</b>, then try again.",
                parse_mode=ParseMode.HTML,
                reply_markup=get_admin_menu_keyboard()
            )
            await state.clear()
            return

        msg = f"<tg-emoji emoji-id=\"6217663806110175239\">✅</tg-emoji> <b>Payout Channel updated to:</b> <code>{val}</code>\n\nNew UPI/USDT withdrawal requests will now also be posted there for approval."

    async with db_pool.acquire() as conn:
        await conn.execute("INSERT INTO bot_settings (key, value) VALUES ('payout_channel', $1) ON CONFLICT (key) DO UPDATE SET value = $1", str(db_val))

    await message.answer(msg, parse_mode=ParseMode.HTML, reply_markup=get_admin_menu_keyboard())
    await state.clear()

# ============================================
# "NOT FOUND" / "COULDN'T VERIFY" HELP CONTENT (admin-configurable)
# Both support MULTIPLE photos/videos (an album) plus one shared caption.
# ============================================

NOT_FOUND_KIND_META = {
    "app": {
        "state": AdminState.waiting_for_app_pass_not_found,
        "label": "App Password Not Found",
        "text_key": "app_pass_not_found_text",
        "media_key": "app_pass_not_found_media",
    },
    "verify": {
        "state": AdminState.waiting_for_twofa_not_verified,
        "label": "Couldn't Verify (2FA)",
        "text_key": "twofa_not_verified_text",
        "media_key": "twofa_not_verified_media",
    },
}

def _get_not_found_content(kind: str):
    if kind == "app":
        return APP_PASS_NOT_FOUND_TEXT, APP_PASS_NOT_FOUND_MEDIA
    return TWOFA_NOT_VERIFIED_TEXT, TWOFA_NOT_VERIFIED_MEDIA

async def send_not_found_help(chat_id: int, text: str, media: list):
    """Sends the configured help content: a single photo/video, an album of several
    photos/videos (sharing one caption), or plain text — whichever was configured."""
    try:
        if media:
            items = media[:10]  # Telegram media group hard limit
            if len(items) == 1:
                item = items[0]
                if item["type"] == "video":
                    await bot.send_video(chat_id, video=item["file_id"], caption=text, parse_mode=ParseMode.HTML)
                else:
                    await bot.send_photo(chat_id, photo=item["file_id"], caption=text, parse_mode=ParseMode.HTML)
            else:
                group = []
                for i, item in enumerate(items):
                    caption = text if i == 0 else None
                    parse_mode = ParseMode.HTML if i == 0 else None
                    if item["type"] == "video":
                        group.append(InputMediaVideo(media=item["file_id"], caption=caption, parse_mode=parse_mode))
                    else:
                        group.append(InputMediaPhoto(media=item["file_id"], caption=caption, parse_mode=parse_mode))
                await bot.send_media_group(chat_id, media=group)
        elif text:
            await bot.send_message(chat_id, text, parse_mode=ParseMode.HTML)
    except Exception as e:
        print(f"send_not_found_help error: {e}")

@dp.message(Command("set"), StateFilter("*"))
async def admin_set_command_router(message: Message, command: CommandObject, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return

    arg = (command.args or "").strip().lower()
    meta = NOT_FOUND_KIND_META.get(arg)
    if not meta:
        await message.answer(
            f"{E_WARN} <b>Unknown /set option.</b>\n\n"
            f"Usage:\n"
            f"• <code>/set app</code> — configure the message, photos &amp; videos shown "
            f"when a user taps <b>\"App Password Not Found\"</b>.\n"
            f"• <code>/set verify</code> — configure the message, photos &amp; videos shown "
            f"when a user taps <b>\"Couldn't Verify\"</b> on the 2FA step.",
            parse_mode=ParseMode.HTML
        )
        return

    cur_text, cur_media = _get_not_found_content(arg)

    await state.set_state(meta["state"])
    await state.update_data(nf_kind=arg, nf_media=[], nf_caption=cur_text)

    if cur_text or cur_media:
        current_preview = f"\n\n<b>Current text:</b>\n{cur_text or '<i>(none, media only)</i>'}"
        if cur_media:
            current_preview += f"\n<b>Current media:</b> ✅ {len(cur_media)} item(s) set"
    else:
        current_preview = "\n\n<i>Nothing configured yet.</i>"

    await message.answer(
        f"{E_SHIELD} <b>{meta['label']} — Setup</b>\n"
        '━━━━━━━━━━━━━━━━━━\n\n'
        f"Send the content you want shown to users who tap <b>\"{meta['label']}\"</b>.\n\n"
        f"• Send one or more <b>photos/videos</b> — each one gets added to the album.\n"
        f"• Then send a <b>text message</b> to use it as the caption and finish.\n"
        f"• Or type <code>done</code> to finish with the media only (no caption).\n"
        f"• Send <code>none</code> at any time to clear it."
        f"{current_preview}",
        parse_mode=ParseMode.HTML
    )

@dp.message(
    StateFilter(AdminState.waiting_for_app_pass_not_found, AdminState.waiting_for_twofa_not_verified),
    F.photo | F.video | (F.text & ~F.text.startswith("/") & ~F.text.in_(MENU_BUTTONS))
)
async def process_not_found_setup(message: Message, state: FSMContext):
    global APP_PASS_NOT_FOUND_TEXT, APP_PASS_NOT_FOUND_MEDIA, TWOFA_NOT_VERIFIED_TEXT, TWOFA_NOT_VERIFIED_MEDIA

    data = await state.get_data()
    kind = data.get("nf_kind", "app")
    meta = NOT_FOUND_KIND_META[kind]
    media = list(data.get("nf_media", []))

    # Collecting another photo/video for the album
    if message.photo or message.video:
        if message.photo:
            media.append({"type": "photo", "file_id": message.photo[-1].file_id})
        else:
            media.append({"type": "video", "file_id": message.video.file_id})

        update_kwargs = {"nf_media": media}
        if message.caption:
            update_kwargs["nf_caption"] = message.caption.strip()
        await state.update_data(**update_kwargs)

        await message.answer(
            f"{E_OK} <b>Added!</b> {len(media)} item(s) in the album so far.\n\n"
            f"Send more photos/videos, send a <b>text caption</b> to finish, "
            f"type <code>done</code> to finish without a caption, or <code>none</code> to clear everything.",
            parse_mode=ParseMode.HTML
        )
        return

    # Finishing: text message
    raw = (message.text or "").strip()
    if raw.lower() == "none":
        new_text, new_media = None, []
    elif raw.lower() == "done":
        new_text, new_media = data.get("nf_caption"), media
    else:
        new_text, new_media = raw, media

    if kind == "app":
        APP_PASS_NOT_FOUND_TEXT = new_text
        APP_PASS_NOT_FOUND_MEDIA = new_media
    else:
        TWOFA_NOT_VERIFIED_TEXT = new_text
        TWOFA_NOT_VERIFIED_MEDIA = new_media

    async with db_pool.acquire() as conn:
        await conn.execute(
            f"INSERT INTO bot_settings (key, value) VALUES ('{meta['text_key']}', $1) ON CONFLICT (key) DO UPDATE SET value = $1",
            new_text or ""
        )
        await conn.execute(
            f"INSERT INTO bot_settings (key, value) VALUES ('{meta['media_key']}', $1) ON CONFLICT (key) DO UPDATE SET value = $1",
            json.dumps(new_media) if new_media else ""
        )

    await state.clear()

    if not new_text and not new_media:
        await message.answer(f"{E_OK} <b>{meta['label']} content cleared.</b>", parse_mode=ParseMode.HTML, reply_markup=get_admin_menu_keyboard())
        return

    await message.answer(f"{E_OK} <b>{meta['label']} content updated!</b>", parse_mode=ParseMode.HTML)
    await send_not_found_help(message.chat.id, new_text, new_media)
    await message.answer("Preview above ⬆️", reply_markup=get_admin_menu_keyboard())

@dp.callback_query(F.data == "apppass_not_found")
async def cb_apppass_not_found(call: CallbackQuery):
    await call.answer()
    if not APP_PASS_NOT_FOUND_TEXT and not APP_PASS_NOT_FOUND_MEDIA:
        default_text = (
            f"{E_WARN} <b>Can't find your App Password?</b>\n"
            '━━━━━━━━━━━━━━━━━━\n\n'
            f"1️⃣ Go to <code>myaccount.google.com/apppasswords</code>\n"
            f"2️⃣ Sign in with the Gmail account you're using\n"
            f"3️⃣ Type any name (e.g. <code>bot</code>) and tap <b>Create</b>\n"
            f"4️⃣ Copy the <b>16-character</b> password shown and send it here\n\n"
            f"ℹ️ If you don't see this option, make sure <b>2-Step Verification</b> is turned ON for that account first."
        )
        await send_not_found_help(call.from_user.id, default_text, [])
        return

    await send_not_found_help(call.from_user.id, APP_PASS_NOT_FOUND_TEXT, APP_PASS_NOT_FOUND_MEDIA)

@dp.callback_query(F.data == "twofa_couldnt_verify")
async def cb_twofa_couldnt_verify(call: CallbackQuery):
    await call.answer()
    if not TWOFA_NOT_VERIFIED_TEXT and not TWOFA_NOT_VERIFIED_MEDIA:
        default_text = (
            f"{E_WARN} <b>Couldn't Verify Your 2FA?</b>\n"
            '━━━━━━━━━━━━━━━━━━\n\n'
            f"1️⃣ Double-check the secret key / QR you sent matches your Authenticator app\n"
            f"2️⃣ Enter the OTP in Google <b>before</b> it expires\n"
            f"3️⃣ Press <b>Refresh Code</b> for a fresh OTP and try again\n"
            f"4️⃣ Make sure <b>2-Step Verification</b> is enabled on that Gmail account\n\n"
            f"ℹ️ Still stuck? Reach out to support for help."
        )
        await send_not_found_help(call.from_user.id, default_text, [])
        return

    await send_not_found_help(call.from_user.id, TWOFA_NOT_VERIFIED_TEXT, TWOFA_NOT_VERIFIED_MEDIA)

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

# ============================================
# PAYOUT CHANNEL (owner/admin-only withdrawal review channel)
# ============================================

async def is_payout_channel_authorized(user_id: int) -> bool:
    """Only the bot owner (ADMIN_ID) or a Telegram admin/owner of the payout channel
    itself may approve/reject a withdrawal from inside that channel."""
    if user_id == ADMIN_ID:
        return True
    if not PAYOUT_CHANNEL:
        return False
    try:
        member = await bot.get_chat_member(chat_id=PAYOUT_CHANNEL, user_id=user_id)
        return member.status in ('creator', 'administrator')
    except Exception:
        return False

def get_payout_channel_keyboard(withdraw_id: int):
    kb = InlineKeyboardBuilder()
    kb.button(**bk("💸", "Pay"), callback_data=f'pcw_pay:{withdraw_id}', style="success")
    kb.button(**bk("❌", "Reject"), callback_data=f'pcw_rej:{withdraw_id}', style="danger")
    kb.adjust(2)
    return kb.as_markup()

async def post_to_payout_channel(text: str, withdraw_id: int):
    """Mirrors a pending withdrawal request into the Payout Channel (if configured)
    with its own Pay/Reject buttons, using the same premium-emoji formatting as the
    owner's private admin notification."""
    if not PAYOUT_CHANNEL:
        return
    try:
        await bot.send_message(
            PAYOUT_CHANNEL,
            text,
            reply_markup=get_payout_channel_keyboard(withdraw_id),
            parse_mode=ParseMode.HTML
        )
    except Exception as e:
        print(f"Error posting to payout channel: {e}")

async def _mark_withdrawal_paid(withdrawal_id: int):
    """Shared DB logic for marking a withdrawal as paid - used by both the private
    admin-DM 'wp:' handler and the payout-channel 'pcw_pay:' handler, so both entry
    points stay perfectly in sync and race-safe (status='pending' guard)."""
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            w_data = await conn.fetchrow(
                "UPDATE withdrawals SET status='paid' WHERE id=$1 AND status='pending' RETURNING user_id, amount",
                withdrawal_id
            )
            if not w_data:
                return None
            user_id = w_data['user_id']
            payout_amount = w_data['amount']
            await conn.execute(
                "UPDATE transactions SET type='withdrawal', note=$1 WHERE user_id=$2 AND note LIKE $3",
                "Withdrawal paid", user_id, f"%Withdrawal #{withdrawal_id}%"
            )
    invalidate_user_cache(user_id)
    return {'user_id': user_id, 'payout_amount': payout_amount}

async def _mark_withdrawal_rejected(withdrawal_id: int):
    """Shared DB logic for rejecting + refunding a withdrawal - used by both the
    private admin-DM 'wr:' handler and the payout-channel 'pcw_rej:' handler."""
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            w_data = await conn.fetchrow(
                "UPDATE withdrawals SET status='rejected' WHERE id=$1 AND status='pending' RETURNING user_id, amount, method",
                withdrawal_id
            )
            if not w_data:
                return None
            user_id = w_data['user_id']
            payout_amount = w_data['amount']
            method = (w_data['method'] or 'UPI').lower()

            fee = UPI_FEES if 'upi' in method else (USDT_FEES if 'usdt' in method else ULTRA_FEES)
            refund_total = payout_amount + fee

            await conn.execute("UPDATE users SET balance = balance + $1 WHERE user_id=$2", refund_total, user_id)
            await conn.execute(
                "UPDATE transactions SET type='withdrawal_rejected', note = note || ' [REJECTED]' WHERE user_id=$1 AND note LIKE $2",
                user_id, f"%Withdrawal #{withdrawal_id}%"
            )
            await conn.execute(
                "INSERT INTO transactions (user_id, type, amount, note) VALUES ($1, $2, $3, $4)",
                user_id, "refund", refund_total, f"Refund for rejected withdrawal #{withdrawal_id}"
            )
    invalidate_user_cache(user_id)
    return {'user_id': user_id, 'refund_total': refund_total}

@dp.callback_query(F.data.startswith("pcw_pay:"))
async def payout_channel_approve(call: CallbackQuery):
    if not await is_payout_channel_authorized(call.from_user.id):
        await call.answer("❌ Only the owner or an admin of this channel can process withdrawals.", show_alert=True)
        return

    withdrawal_id = int(call.data.split(":")[1])
    result = await _mark_withdrawal_paid(withdrawal_id)
    if not result:
        await call.answer("⚠️ This request is already processed!", show_alert=True)
        return

    await call.answer("✅ Marked as paid.")
    user_id = result['user_id']
    payout_amount = result['payout_amount']
    processed_by = f"@{call.from_user.username}" if call.from_user.username else call.from_user.full_name
    processed_time = now_ist().strftime("%d %b %Y, %H:%M IST")

    await edit_admin_message(
        call,
        f"<tg-emoji emoji-id=\"6217663806110175239\">✅</tg-emoji> <b>PAID</b>\n"
        f"<tg-emoji emoji-id=\"5870458774455587120\">👤</tg-emoji> <b>Processed by:</b> {processed_by}\n"
        f"<tg-emoji emoji-id=\"5382194935057372936\">⏳</tg-emoji> <b>Time:</b> {processed_time}"
    )

    async def notify_user():
        user_data = await get_user_data(user_id)
        formatted_amt = format_currency(payout_amount, user_data['currency'])
        await send_user_notification(
            user_id,
            f"<tg-emoji emoji-id=\"5461151367559141950\">🎉</tg-emoji> Your withdrawal request of {formatted_amt} has been approved and paid!",
            parse_mode=ParseMode.HTML
        )

    asyncio.create_task(notify_user())

@dp.callback_query(F.data.startswith("pcw_rej:"))
async def payout_channel_reject(call: CallbackQuery):
    if not await is_payout_channel_authorized(call.from_user.id):
        await call.answer("❌ Only the owner or an admin of this channel can process withdrawals.", show_alert=True)
        return

    withdrawal_id = int(call.data.split(":")[1])
    result = await _mark_withdrawal_rejected(withdrawal_id)
    if not result:
        await call.answer("⚠️ This request is already processed!", show_alert=True)
        return

    await call.answer("⚠️ Rejected & refunded.")
    user_id = result['user_id']
    refund_total = result['refund_total']
    processed_by = f"@{call.from_user.username}" if call.from_user.username else call.from_user.full_name
    processed_time = now_ist().strftime("%d %b %Y, %H:%M IST")

    await edit_admin_message(
        call,
        f"<tg-emoji emoji-id=\"5420323339723881652\">⚠️</tg-emoji> <b>REJECTED (Balance Refunded)</b>\n"
        f"<tg-emoji emoji-id=\"5870458774455587120\">👤</tg-emoji> <b>Processed by:</b> {processed_by}\n"
        f"<tg-emoji emoji-id=\"5382194935057372936\">⏳</tg-emoji> <b>Time:</b> {processed_time}"
    )

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
        **bk("💸", "Pay"),
        callback_data=f'wp:{withdraw_id}',
        style="success"
    )
    kb.button(
        **bk("❌", "Reject"),
        callback_data=f'wr:{withdraw_id}',
        style="danger"
    )
    kb.adjust(2)

    withdraw_request_text = (
        f'<tg-emoji emoji-id=\"5417924076503062111\">💰</tg-emoji> <b>WITHDRAWAL REQUEST #{withdraw_id} (UPI)</b>\n\n'
        f'<tg-emoji emoji-id=\"5870458774455587120\">👤</tg-emoji> @{call.from_user.username}\n'
        f'🆔 <code>{user_id}</code>\n'
        f'<tg-emoji emoji-id=\"5417924076503062111\">💰</tg-emoji> Net Payout: ₹{payout_amount:.2f} (Fee Charged: ₹{UPI_FEES:.2f})\n'
        f'<tg-emoji emoji-id=\"6291696801636424911\">🏦</tg-emoji> UPI: <code>{upi}</code>'
    )

    await bot.send_message(
        ADMIN_ID,
        withdraw_request_text,
        reply_markup=kb.as_markup(),
        parse_mode=ParseMode.HTML
    )
    asyncio.create_task(post_to_payout_channel(withdraw_request_text, withdraw_id))

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
        **bk("💸", "Pay"),
        callback_data=f'wp:{withdraw_id}',
        style="success"
    )
    kb.button(
        **bk("❌", "Reject"),
        callback_data=f'wr:{withdraw_id}',
        style="danger"
    )
    kb.adjust(2)

    usdt_amount = payout_amount / USD_TO_INR
    withdraw_request_text = (
        f'<tg-emoji emoji-id=\"5417924076503062111\">💰</tg-emoji> <b>WITHDRAWAL REQUEST #{withdraw_id} (USDT BEP-20)</b>\n\n'
        f'<tg-emoji emoji-id=\"5870458774455587120\">👤</tg-emoji> @{call.from_user.username}\n'
        f'🆔 <code>{user_id}</code>\n'
        f'<tg-emoji emoji-id=\"5417924076503062111\">💰</tg-emoji> Net Payout: ₹{payout_amount:.2f} (~${usdt_amount:.2f} USDT) (Fee Charged: ₹{USDT_FEES:.2f})\n'
        f'<tg-emoji emoji-id=\"5197434882321567830\">🪙</tg-emoji> USDT BEP-20: <code>{usdt}</code>'
    )

    await bot.send_message(
        ADMIN_ID,
        withdraw_request_text,
        reply_markup=kb.as_markup(),
        parse_mode=ParseMode.HTML
    )
    asyncio.create_task(post_to_payout_channel(withdraw_request_text, withdraw_id))

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
    user_id = call.from_user.id
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow('''
            SELECT ta.task_id, ta.assigned_at, t.status, t.title, t.details, t.reward
            FROM task_assignments ta
            JOIN tasks t ON ta.task_id = t.id
            WHERE ta.user_id=$1 AND t.status IN ('assigned', 'verifying', 'pending_review')
            ORDER BY ta.assigned_at DESC LIMIT 1
        ''', user_id)

    if not row:
        await call.answer('❌ You do not have any active task.', show_alert=True)
        return
    if row['status'] == 'pending_review':
        await call.answer('⏳ You have already submitted this task.', show_alert=True)
        return
    if datetime.utcnow() - row['assigned_at'] > timedelta(minutes=30):
        await call.answer('⌛ This task has expired. Please get a new task.', show_alert=True)
        return

    await call.answer()
    task_id = row['task_id']
    try:
        parts = row['details'].split(" | ")
        email = parts[0].replace("Email: ", "").strip()
        password = parts[1].replace("Pass: ", "").strip()
    except Exception:
        email = row['title'].replace("Login to ", "").strip()
        password = DEFAULT_TASK_PASS

    is_valid = await is_gmail_registered(email, user_id=user_id)
    if not is_valid:
        await call.message.answer(
            f"{E_NO} <b>This Gmail account (<code>{_esc(email)}</code>) does not exist on Google!</b>\n\n"
            f"Please create <code>{_esc(email)}</code> first on Google, then press <b>Submit</b> again.",
            parse_mode=ParseMode.HTML
        )
        return

    if not TWOFA_TASK_STATUS:
        # 2FA step is OFF: Gmail validated above -> submit immediately (no 2FA / App Password asked)
        if user_id in ACTIVE_SUBMISSIONS:
            return
        ACTIVE_SUBMISSIONS.add(user_id)
        try:
            await state.clear()
            tg_username = call.from_user.username
            res = await finalize_task_submission(user_id, tg_username, task_id, email, password, None, None)
            if not res:
                await _flow_expired(call.message.chat.id, call.message.message_id, state)
                return

            user_data = await get_user_data(user_id)
            amt_str = format_currency(res['amount'], user_data['currency'] if user_data else "USD")
            final_text = build_submission_success_text("Task", res['ref_id'], amt_str, res['release_at'])
            msg_id = await _flow_edit(call.message.chat.id, call.message.message_id, final_text, get_main_menu_keyboard())
            await state.update_data(last_menu_msg_id=msg_id)

            asyncio.create_task(notify_admin_new_hold(res['hold_id']))
            asyncio.create_task(alert_worker_task_submission(res.get('added_by'), res['ref_id'], email, password))
        finally:
            ACTIVE_SUBMISSIONS.discard(user_id)
        return

    async with db_pool.acquire() as conn:
        if row['status'] == 'assigned':
            # Fresh 30 minutes for the 2FA + App Password steps (only granted once)
            await conn.execute("UPDATE tasks SET status='verifying' WHERE id=$1 AND status='assigned'", task_id)
            await conn.execute("UPDATE task_assignments SET assigned_at=$1 WHERE task_id=$2 AND user_id=$3", datetime.utcnow(), task_id, user_id)

    await state.clear()
    await state.set_state(UserState.submitting_twofa)
    text, kb = build_twofa_prompt('task')
    msg_id = await _flow_edit(call.message.chat.id, call.message.message_id, text, kb)
    await state.update_data(
        flow='task', task_id=task_id, email=email, gmail_password=password,
        flow_msg_id=msg_id, last_menu_msg_id=msg_id, twofa_tries=0
    )
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE task_assignments SET message_id=$1 WHERE task_id=$2", msg_id, task_id)

@dp.callback_query(F.data == "user_cancel_task")
async def inline_cancel_task(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.clear()
    user_id = call.from_user.id
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow('SELECT ta.task_id, t.status FROM task_assignments ta JOIN tasks t ON ta.task_id = t.id WHERE ta.user_id=$1 AND t.status IN (\'assigned\', \'verifying\', \'pending_review\') ORDER BY ta.assigned_at DESC LIMIT 1', user_id)
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

    txt = f'{E_OK} Task #{task_id} has been cancelled and returned to the pool.'
    try:
        await call.message.edit_text(txt, parse_mode=ParseMode.HTML, reply_markup=get_back_inline_keyboard())
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            await call.message.answer(txt, parse_mode=ParseMode.HTML, reply_markup=get_back_inline_keyboard())

async def _flow_expired(call_or_msg_chat_id: int, msg_id, state: FSMContext):
    await state.clear()
    txt = f'{E_WARN} <b>This task has expired and was returned to the pool.</b>\n\nPlease get a new task from the menu.'
    new_id = await _flow_edit(call_or_msg_chat_id, msg_id, txt, get_main_menu_keyboard())
    await state.update_data(last_menu_msg_id=new_id)

@dp.message(UserState.submitting_twofa, F.photo | F.document | (F.text & ~F.text.startswith("/") & ~F.text.in_(MENU_BUTTONS)))
async def process_twofa_input(message: Message, state: FSMContext):
    data = await state.get_data()
    flow = data.get('flow')
    if not flow:
        await state.clear()
        return
    user_id = message.from_user.id
    chat_id = message.chat.id
    flow_msg_id = data.get('flow_msg_id')

    if flow == 'task' and not await _task_flow_active(user_id, data.get('task_id')):
        try:
            await message.delete()
        except Exception:
            pass
        await _flow_expired(chat_id, flow_msg_id, state)
        return

    secret = None
    if message.text:
        secret = extract_secret(message.text)
    else:
        file_id = None
        if message.photo:
            file_id = message.photo[-1].file_id
        elif message.document and (message.document.mime_type or "").startswith("image/"):
            file_id = message.document.file_id
        if file_id:
            buf = io.BytesIO()
            try:
                await bot.download(file_id, destination=buf)
                qr_text = await decode_qr_bytes(buf.getvalue())
                if qr_text:
                    secret = extract_secret(qr_text)
            except Exception as e:
                print(f"QR download/decode error: {e}")

    # The message holds a secret: remove it from the chat right away
    try:
        await message.delete()
    except Exception:
        pass

    if not secret or not generate_local_totp(secret):
        tries = int(data.get('twofa_tries', 0)) + 1
        await state.update_data(twofa_tries=tries)
        text, kb = build_twofa_prompt(flow, error=f"No valid 2FA key or QR found (try {tries}). Send a clear QR screenshot or the secret key text.")
        new_id = await _flow_edit(chat_id, flow_msg_id, text, kb)
        await state.update_data(flow_msg_id=new_id, last_menu_msg_id=new_id)
        return

    await state.update_data(twofa_secret=secret)
    text, kb = build_otp_message(secret, flow)
    new_id = await _flow_edit(chat_id, flow_msg_id, text, kb)
    await state.update_data(flow_msg_id=new_id, last_menu_msg_id=new_id)
    if flow == 'task':
        async with db_pool.acquire() as conn:
            await conn.execute("UPDATE task_assignments SET message_id=$1 WHERE task_id=$2", new_id, data.get('task_id'))

@dp.callback_query(F.data == "tw_ref")
async def cb_twofa_refresh(call: CallbackQuery, state: FSMContext):
    try:
        data = await state.get_data()
        secret = data.get('twofa_secret')
        if not secret:
            await call.answer("⚠️ Session expired. Please start again.", show_alert=True)
            return
        text, kb = build_otp_message(secret, data.get('flow'))
        if not text:
            await call.answer("⚠️ Could not generate the code. Send the key again.", show_alert=True)
            return
        try:
            await call.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
            await call.answer(f"Refreshed! {get_remaining_seconds()}s left.")
        except Exception:
            await call.answer("Code is still fresh.")
    except Exception as e:
        print(f"[tw_ref] Error: {e}")
        traceback.print_exc()
        try:
            await call.answer("⚠️ Something went wrong. Please try again.", show_alert=True)
        except Exception:
            pass

@dp.callback_query(F.data == "tw_done")
async def cb_twofa_done(call: CallbackQuery, state: FSMContext):
    try:
        data = await state.get_data()
        flow = data.get('flow')
        if not data.get('twofa_secret') or not flow:
            await call.answer("⚠️ Session expired. Please start again.", show_alert=True)
            return
        user_id = call.from_user.id
        if flow == 'task' and not await _task_flow_active(user_id, data.get('task_id')):
            await call.answer()
            await _flow_expired(call.message.chat.id, call.message.message_id, state)
            return

        await call.answer()
        await state.set_state(UserState.submitting_apppass)
        text, kb = build_apppass_prompt(flow)
        new_id = await _flow_edit(call.message.chat.id, call.message.message_id, text, kb)
        await state.update_data(flow_msg_id=new_id, last_menu_msg_id=new_id)
    except Exception as e:
        print(f"[tw_done] Error: {e}")
        traceback.print_exc()
        try:
            await call.answer("⚠️ Something went wrong. Please press Cancel and try again.", show_alert=True)
        except Exception:
            pass

@dp.message(UserState.submitting_apppass, F.text, ~F.text.startswith("/"), ~F.text.in_(MENU_BUTTONS))
async def process_app_password(message: Message, state: FSMContext):
    user_id = message.from_user.id
    chat_id = message.chat.id
    data = await state.get_data()
    flow = data.get('flow')
    if not flow or not data.get('twofa_secret'):
        await state.clear()
        return

    try:
        await message.delete()
    except Exception:
        pass

    if user_id in ACTIVE_SUBMISSIONS:
        return

    flow_msg_id = data.get('flow_msg_id')
    app_pw = re.sub(r"\s+", "", message.text or "")
    email = data.get('email')

    if not re.fullmatch(r"[A-Za-z0-9]{16}", app_pw):
        text, kb = build_apppass_prompt(flow, error="App password must be exactly 16 characters. Try again.")
        new_id = await _flow_edit(chat_id, flow_msg_id, text, kb)
        await state.update_data(flow_msg_id=new_id, last_menu_msg_id=new_id)
        return

    if flow == 'task' and not await _task_flow_active(user_id, data.get('task_id')):
        await _flow_expired(chat_id, flow_msg_id, state)
        return

    ACTIVE_SUBMISSIONS.add(user_id)
    try:
        checking_kb = InlineKeyboardMarkup(inline_keyboard=[[flow_cancel_button(flow)]])
        flow_msg_id = await _flow_edit(
            chat_id, flow_msg_id,
            f'{E_TIME} <b>Verifying App Password...</b>\n\nPlease wait a few seconds.',
            checking_kb
        )
        ok = await verify_app_password(email, app_pw)
        if not ok:
            text, kb = build_apppass_prompt(
                flow,
                error="App password verification failed. Check the Gmail account & the 16-character password, then send it again."
            )
            new_id = await _flow_edit(chat_id, flow_msg_id, text, kb)
            await state.update_data(flow_msg_id=new_id, last_menu_msg_id=new_id)
            return

        twofa = data.get('twofa_secret')
        gmail_password = data.get('gmail_password')
        username = message.from_user.username

        if flow == 'task':
            res = await finalize_task_submission(user_id, username, data.get('task_id'), email, gmail_password, twofa, app_pw)
            if not res:
                await _flow_expired(chat_id, flow_msg_id, state)
                return
            label, ref_id = "Task", res['ref_id']
        else:
            res = await finalize_sell_submission(user_id, username, email, gmail_password, twofa, app_pw)
            if res == 'dup':
                await state.clear()
                new_id = await _flow_edit(
                    chat_id, flow_msg_id,
                    f"{E_NO} <b>This email is already in the database. You cannot sell the same email twice.</b>",
                    get_main_menu_keyboard()
                )
                await state.update_data(last_menu_msg_id=new_id)
                return
            if res == 'dup_pass':
                await state.clear()
                new_id = await _flow_edit(
                    chat_id, flow_msg_id,
                    password_already_registered_text(),
                    get_main_menu_keyboard()
                )
                await state.update_data(last_menu_msg_id=new_id)
                return
            label, ref_id = "Gmail Sell", res['ref_id']

        user_data = await get_user_data(user_id)
        amt_str = format_currency(res['amount'], user_data['currency'] if user_data else "USD")
        final_text = build_submission_success_text(label, ref_id, amt_str, res['release_at'])
        await state.clear()
        new_id = await _flow_edit(chat_id, flow_msg_id, final_text, get_main_menu_keyboard())
        await state.update_data(last_menu_msg_id=new_id)

        asyncio.create_task(notify_admin_new_hold(res['hold_id']))
        if flow == 'task':
            asyncio.create_task(alert_worker_task_submission(res.get('added_by'), res['ref_id'], email, gmail_password))
        else:
            asyncio.create_task(alert_workers_new_sell())
    finally:
        ACTIVE_SUBMISSIONS.discard(user_id)

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

        ref_reward = round(amount * REFERRAL_COMMISSION_PCT / 100.0, 2)
        if REF_STATUS and referred_by and referred_by != user_id and ref_reward > 0:
            await ensure_user(referred_by, conn=conn)
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
        await send_user_notification(user_id, f"<tg-emoji emoji-id=\"5461151367559141950\">🎉</tg-emoji> Your Gmail sell request #{sell_id} was approved!\n+{formatted_amt} added to your balance.", parse_mode=ParseMode.HTML)

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

        ref_reward = round(reward * REFERRAL_COMMISSION_PCT / 100.0, 2)
        if REF_STATUS and referred_by and referred_by != user_id and ref_reward > 0:
            await ensure_user(referred_by, conn=conn)
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
        await send_user_notification(user_id, f"<tg-emoji emoji-id=\"5461151367559141950\">🎉</tg-emoji> Task #{task_id} approved!\n+{formatted_reward} added to your balance.", parse_mode=ParseMode.HTML)

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
    result = await _mark_withdrawal_paid(withdrawal_id)
    if not result:
        await call.answer("⚠️ This request is already processed!", show_alert=True)
        return

    await call.answer()
    user_id = result['user_id']
    payout_amount = result['payout_amount']
    processed_by = f"@{call.from_user.username}" if call.from_user.username else call.from_user.full_name
    processed_time = now_ist().strftime("%d %b %Y, %H:%M IST")

    await edit_admin_message(
        call,
        f"<tg-emoji emoji-id=\"6217663806110175239\">✅</tg-emoji> <b>Withdrawal Paid</b>\n"
        f"<tg-emoji emoji-id=\"5870458774455587120\">👤</tg-emoji> <b>Processed by:</b> {processed_by}\n"
        f"<tg-emoji emoji-id=\"5382194935057372936\">⏳</tg-emoji> <b>Time:</b> {processed_time}"
    )

    async def notify_user():
        user_data = await get_user_data(user_id)
        formatted_amt = format_currency(payout_amount, user_data['currency'])
        await send_user_notification(
            user_id,
            f"<tg-emoji emoji-id=\"5461151367559141950\">🎉</tg-emoji> Your withdrawal request of {formatted_amt} has been approved and paid!",
            parse_mode=ParseMode.HTML
        )

    asyncio.create_task(notify_user())

@dp.callback_query(F.data.startswith("wr:"))
async def reject_withdraw(call: CallbackQuery):
    withdrawal_id = int(call.data.split(":")[1])
    result = await _mark_withdrawal_rejected(withdrawal_id)
    if not result:
        await call.answer("⚠️ This request is already processed!", show_alert=True)
        return

    await call.answer()
    user_id = result['user_id']
    refund_total = result['refund_total']
    processed_by = f"@{call.from_user.username}" if call.from_user.username else call.from_user.full_name
    processed_time = now_ist().strftime("%d %b %Y, %H:%M IST")

    await edit_admin_message(
        call,
        f"<tg-emoji emoji-id=\"5420323339723881652\">⚠️</tg-emoji> <b>Withdrawal Rejected (Balance Refunded)</b>\n"
        f"<tg-emoji emoji-id=\"5870458774455587120\">👤</tg-emoji> <b>Processed by:</b> {processed_by}\n"
        f"<tg-emoji emoji-id=\"5382194935057372936\">⏳</tg-emoji> <b>Time:</b> {processed_time}"
    )

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
                    WHERE t.status IN ('assigned', 'verifying')
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
                    WHERE t.status NOT IN ('completed', 'pending_review', 'in_holder')
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

async def ref_lb_end_watcher():
    """Alerts the admin once when the leaderboard round reaches its end time (standings are frozen from then)."""
    global REF_LB_NOTIFIED_FOR
    while True:
        try:
            end = REF_LB_END
            if end and datetime.utcnow() >= end and REF_LB_NOTIFIED_FOR != end:
                REF_LB_NOTIFIED_FOR = end
                rows = await fetch_ref_leaderboard(len(REF_LB_REWARDS) or 3)
                if rows:
                    lines = []
                    for r in rows:
                        rank = int(r['rnk'])
                        prize = ref_prize_for_rank(rank)
                        prize_txt = f" → prize ₹{prize:.2f}" if prize else ""
                        lines.append(f"#{rank} <code>{r['user_id']}</code> - ₹{float(r['earned']):.2f}{prize_txt}")
                    body = "\n".join(lines)
                else:
                    body = "<i>Nobody earned referral commission in this round.</i>"
                await bot.send_message(
                    ADMIN_ID,
                    f'{E_TROPHY} <b>Referral Leaderboard Round Ended</b>\n'
                    f'{E_TIME} {fmt_dt(end)} IST\n\n{body}\n\n'
                    f'Open <b>Referral Panel</b> → <b>Pay Rewards &amp; Reset</b> to pay the winners.',
                    parse_mode=ParseMode.HTML
                )
        except Exception as e:
            print(f"Error in leaderboard end watcher: {e}")
        await asyncio.sleep(60)

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
    asyncio.create_task(ref_lb_end_watcher())

    port = int(os.environ.get("PORT", 8080))

    if WEBHOOK_URL:
        # FAST PATH: Telegram pushes updates to us directly - removes the getUpdates round-trip
        app = web.Application()
        app.router.add_get('/', health)
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
