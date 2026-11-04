import asyncio
import logging
import os
import re
import sys
import uuid
from datetime import datetime

import asyncpg
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message,
    CallbackQuery,
    ReplyKeyboardMarkup,
    KeyboardButton,
    Contact,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder

# ---------- تنظیمات ----------
BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
ADMIN_ID = 8935601841

CHANNELS = [
    {"username": "@BET_1XZX", "url": "https://t.me/BET_1XZX"},
    {"username": "@GAP_BAZIN1", "url": "https://t.me/GAP_BAZIN1"},
]
PUBLISH_CHANNEL = "@BET_1XZX"

GIFTS = {
    "teddy": {"name": "🧸 Teddy", "price": 15},
    "heart": {"name": "❤️ Heart", "price": 15},
    "flower": {"name": "🌹 Flower", "price": 25},
    "gift": {"name": "🎁 Gift", "price": 25},
    "rocket": {"name": "🚀 Rocket", "price": 50},
    "cake": {"name": "🎂 Cake", "price": 50},
    "diamond": {"name": "💎 Diamond", "price": 100},
}

BTN_INVITE = "🔗 لینک دعوت"
BTN_BUY = "🎁 خرید گیفت"
BTN_BALANCE = "⭐ موجودی استارز"
MENU_TEXTS = {BTN_INVITE, BTN_BUY, BTN_BALANCE}

logging.basicConfig(
    level=logging.INFO,
    stream=sys.stdout,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger(__name__)


# ---------- SQL ----------
INIT_SQL = """
CREATE TABLE IF NOT EXISTS users (user_id BIGINT PRIMARY KEY);
ALTER TABLE users ADD COLUMN IF NOT EXISTS username TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS full_name TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS phone TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS balance INTEGER NOT NULL DEFAULT 0;
ALTER TABLE users ADD COLUMN IF NOT EXISTS referrer_id BIGINT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS referrer_rewarded BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE users ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

DO $$
BEGIN
    BEGIN
        ALTER TABLE users ADD CONSTRAINT users_phone_unique UNIQUE (phone);
    EXCEPTION WHEN duplicate_table THEN NULL;
             WHEN duplicate_object THEN NULL;
    END;
END $$;

CREATE TABLE IF NOT EXISTS orders (order_id TEXT PRIMARY KEY);
ALTER TABLE orders ADD COLUMN IF NOT EXISTS user_id BIGINT;
ALTER TABLE orders ADD COLUMN IF NOT EXISTS gift_name TEXT;
ALTER TABLE orders ADD COLUMN IF NOT EXISTS gift_price INTEGER;
ALTER TABLE orders ADD COLUMN IF NOT EXISTS recipient TEXT;
ALTER TABLE orders ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'pending';
ALTER TABLE orders ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
ALTER TABLE orders ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
ALTER TABLE orders ADD COLUMN IF NOT EXISTS published BOOLEAN NOT NULL DEFAULT FALSE;

CREATE TABLE IF NOT EXISTS referrals (id BIGSERIAL PRIMARY KEY);
ALTER TABLE referrals ADD COLUMN IF NOT EXISTS referrer_id BIGINT;
ALTER TABLE referrals ADD COLUMN IF NOT EXISTS referred_id BIGINT;
ALTER TABLE referrals ADD COLUMN IF NOT EXISTS rewarded BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE referrals ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

DO $$
BEGIN
    BEGIN
        ALTER TABLE referrals ADD CONSTRAINT referrals_referred_unique UNIQUE (referred_id);
    EXCEPTION WHEN duplicate_table THEN NULL;
             WHEN duplicate_object THEN NULL;
    END;
END $$;

CREATE TABLE IF NOT EXISTS transactions (id BIGSERIAL PRIMARY KEY);
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS user_id BIGINT;
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS amount INTEGER;
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS type TEXT;
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS description TEXT;
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

CREATE INDEX IF NOT EXISTS idx_users_phone ON users(phone);
CREATE INDEX IF NOT EXISTS idx_users_referrer ON users(referrer_id);
CREATE INDEX IF NOT EXISTS idx_orders_user ON orders(user_id);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
CREATE INDEX IF NOT EXISTS idx_transactions_user ON transactions(user_id);

CREATE OR REPLACE FUNCTION create_order_atomic(
    p_order_id TEXT, p_user_id BIGINT, p_gift_name TEXT, p_gift_price INTEGER, p_recipient TEXT
) RETURNS BOOLEAN AS $$
DECLARE v_balance INTEGER;
BEGIN
    SELECT balance INTO v_balance FROM users WHERE user_id = p_user_id FOR UPDATE;
    IF v_balance IS NULL OR v_balance < p_gift_price THEN RETURN FALSE; END IF;
    UPDATE users SET balance = balance - p_gift_price WHERE user_id = p_user_id;
    INSERT INTO orders(order_id, user_id, gift_name, gift_price, recipient, status)
    VALUES (p_order_id, p_user_id, p_gift_name, p_gift_price, p_recipient, 'pending');
    INSERT INTO transactions(user_id, amount, type, description)
    VALUES (p_user_id, -p_gift_price, 'purchase', 'خرید ' || p_gift_name);
    RETURN TRUE;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION reject_order_atomic(p_order_id TEXT) RETURNS BOOLEAN AS $$
DECLARE v_order orders%ROWTYPE;
BEGIN
    SELECT * INTO v_order FROM orders WHERE order_id = p_order_id AND status = 'pending' FOR UPDATE;
    IF NOT FOUND THEN RETURN FALSE; END IF;
    UPDATE orders SET status = 'rejected', updated_at = NOW() WHERE order_id = p_order_id;
    UPDATE users SET balance = balance + v_order.gift_price WHERE user_id = v_order.user_id;
    INSERT INTO transactions(user_id, amount, type, description)
    VALUES (v_order.user_id, v_order.gift_price, 'refund', 'بازگشت وجه رد سفارش ' || p_order_id);
    RETURN TRUE;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION reward_referrer_atomic(p_referred_id BIGINT) RETURNS BOOLEAN AS $$
DECLARE v_referrer_id BIGINT;
BEGIN
    SELECT referrer_id INTO v_referrer_id FROM users
    WHERE user_id = p_referred_id AND referrer_rewarded = FALSE FOR UPDATE;
    IF v_referrer_id IS NULL THEN RETURN FALSE; END IF;
    UPDATE users SET referrer_rewarded = TRUE WHERE user_id = p_referred_id;
    UPDATE users SET balance = balance + 2 WHERE user_id = v_referrer_id;
    INSERT INTO referrals(referrer_id, referred_id, rewarded)
    VALUES (v_referrer_id, p_referred_id, TRUE)
    ON CONFLICT (referred_id) DO NOTHING;
    INSERT INTO transactions(user_id, amount, type, description)
    VALUES (v_referrer_id, 2, 'invite_reward', 'پاداش دعوت کاربر ' || p_referred_id);
    RETURN TRUE;
END;
$$ LANGUAGE plpgsql;
"""


class Database:
    def __init__(self):
        self.pool: asyncpg.Pool | None = None

    async def connect(self):
        if not DATABASE_URL:
            raise RuntimeError("DATABASE_URL تنظیم نشده است")
        self.pool = await asyncpg.create_pool(
            DATABASE_URL, min_size=1, max_size=5, command_timeout=60, ssl="require"
        )
        async with self.pool.acquire() as conn:
            await conn.execute("SELECT 1")
            logger.info("دیتابیس متصل شد، ساخت جدول‌ها...")
            await conn.execute(INIT_SQL)
            logger.info("✅ دیتابیس آماده است")

    async def close(self):
        if self.pool:
            await self.pool.close()

    async def get_user(self, user_id: int):
        async with self.pool.acquire() as conn:
            return await conn.fetchrow("SELECT * FROM users WHERE user_id = $1", user_id)

    async def get_user_by_phone(self, phone: str):
        async with self.pool.acquire() as conn:
            return await conn.fetchrow("SELECT * FROM users WHERE phone = $1", phone)

    async def upsert_user(self, user_id, username, full_name, phone, referrer_id=None):
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                existing = await conn.fetchrow("SELECT user_id, referrer_id FROM users WHERE user_id = $1", user_id)
                if existing:
                    current_ref = existing["referrer_id"]
                    final_ref = current_ref if current_ref else (referrer_id if referrer_id != user_id else None)
                    await conn.execute(
                        "UPDATE users SET username=$1, full_name=$2, phone=$3, referrer_id=$4 WHERE user_id=$5",
                        username, full_name, phone, final_ref, user_id,
                    )
                    if not current_ref and final_ref:
                        await conn.execute(
                            "INSERT INTO referrals(referrer_id, referred_id) VALUES($1,$2) ON CONFLICT (referred_id) DO NOTHING",
                            final_ref, user_id,
                        )
                else:
                    ref = referrer_id if referrer_id != user_id else None
                    await conn.execute(
                        "INSERT INTO users(user_id, username, full_name, phone, referrer_id) "
                        "VALUES($1,$2,$3,$4,$5)",
                        user_id, username, full_name, phone, ref,
                    )
                    if ref:
                        await conn.execute(
                            "INSERT INTO referrals(referrer_id, referred_id) VALUES($1,$2) ON CONFLICT (referred_id) DO NOTHING",
                            ref, user_id,
                        )

    async def try_reward_referrer(self, user_id: int) -> bool:
        async with self.pool.acquire() as conn:
            result = await conn.fetchval("SELECT reward_referrer_atomic($1)", user_id)
            return bool(result)

    async def set_referrer(self, user_id: int, referrer_id: int) -> bool:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow("SELECT referrer_id FROM users WHERE user_id=$1", user_id)
                if not row or row["referrer_id"]:
                    return False
                await conn.execute("UPDATE users SET referrer_id=$1 WHERE user_id=$2", referrer_id, user_id)
                await conn.execute(
                    "INSERT INTO referrals(referrer_id, referred_id) VALUES($1,$2) ON CONFLICT (referred_id) DO NOTHING",
                    referrer_id, user_id,
                )
                return True

    async def create_order(self, order_id, user_id, gift_key, price, recipient) -> bool:
        gift_name = GIFTS[gift_key]["name"]
        async with self.pool.acquire() as conn:
            result = await conn.fetchval(
                "SELECT create_order_atomic($1,$2,$3,$4,$5)",
                order_id, user_id, gift_name, price, recipient,
            )
            return bool(result)

    async def reject_order(self, order_id: str) -> bool:
        async with self.pool.acquire() as conn:
            result = await conn.fetchval("SELECT reject_order_atomic($1)", order_id)
            return bool(result)

    async def approve_order(self, order_id: str) -> bool:
        async with self.pool.acquire() as conn:
            result = await conn.execute(
                "UPDATE orders SET status='approved', updated_at=NOW() WHERE order_id=$1 AND status='pending'",
                order_id,
            )
            return result == "UPDATE 1"

    async def get_order(self, order_id: str):
        async with self.pool.acquire() as conn:
            return await conn.fetchrow("SELECT * FROM orders WHERE order_id = $1", order_id)

    async def mark_order_completed(self, order_id: str) -> bool:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    "SELECT * FROM orders WHERE order_id=$1 AND status='approved' AND published=FALSE FOR UPDATE",
                    order_id,
                )
                if not row:
                    return False
                await conn.execute(
                    "UPDATE orders SET status='completed', published=TRUE, updated_at=NOW() WHERE order_id=$1",
                    order_id,
                )
                return True

    async def get_balance(self, user_id: int) -> int:
        async with self.pool.acquire() as conn:
            val = await conn.fetchval("SELECT balance FROM users WHERE user_id=$1", user_id)
            return val or 0

    async def add_balance(self, user_id: int, amount: int) -> bool:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow("SELECT balance FROM users WHERE user_id=$1 FOR UPDATE", user_id)
                if not row:
                    return False
                new_balance = max(0, (row["balance"] or 0) + amount)
                await conn.execute("UPDATE users SET balance=$1 WHERE user_id=$2", new_balance, user_id)
                desc = "افزایش توسط ادمین" if amount > 0 else "کاهش توسط ادمین"
                await conn.execute(
                    "INSERT INTO transactions(user_id, amount, type, description) VALUES($1,$2,'admin_adjust',$3)",
                    user_id, amount, desc,
                )
                return True

    async def get_stats(self):
        async with self.pool.acquire() as conn:
            users_count = await conn.fetchval("SELECT COUNT(*) FROM users")
            orders_pending = await conn.fetchval("SELECT COUNT(*) FROM orders WHERE status='pending'")
            orders_completed = await conn.fetchval("SELECT COUNT(*) FROM orders WHERE status='completed'")
            return {
                "users": users_count or 0,
                "pending": orders_pending or 0,
                "completed": orders_completed or 0,
            }

    async def get_transactions(self, user_id: int, limit: int = 20):
        try:
            async with self.pool.acquire() as conn:
                return await conn.fetch(
                    "SELECT amount, type, description, created_at FROM transactions "
                    "WHERE user_id=$1 ORDER BY created_at DESC LIMIT $2",
                    user_id, limit,
                )
        except Exception:
            return []


db = Database()

class BuyGift(StatesGroup):
    waiting_recipient = State()
    confirming = State()

# ⚠️ فقط ۱ state برای ادمین
class AdminFlow(StatesGroup):
    waiting_user_id = State()
    waiting_amount = State()

def main_menu_keyboard():
    kb = ReplyKeyboardBuilder()
    kb.button(text=BTN_INVITE)
    kb.button(text=BTN_BUY)
    kb.button(text=BTN_BALANCE)
    kb.adjust(2, 1)
    return kb.as_markup(resize_keyboard=True)

def phone_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📱 تأیید شماره موبایل", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )

def gift_keyboard():
    kb = InlineKeyboardBuilder()
    for key, info in GIFTS.items():
        kb.button(text=f"{info['name']} — {info['price']} استارز", callback_data=f"gift:{key}")
    kb.adjust(1)
    return kb.as_markup()

def order_admin_keyboard(order_id: str):
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ تأیید سفارش", callback_data=f"approve:{order_id}")
    kb.button(text="❌ رد سفارش", callback_data=f"reject:{order_id}")
    kb.adjust(2)
    return kb.as_markup()

def order_complete_keyboard(order_id: str):
    kb = InlineKeyboardBuilder()
    kb.button(text="📦 ثبت انجام سفارش", callback_data=f"complete:{order_id}")
    return kb.as_markup()

def back_keyboard():
    kb = InlineKeyboardBuilder()
    kb.button(text="🔙 بازگشت به منو", callback_data="main_menu")
    return kb.as_markup()

def channels_keyboard():
    kb = InlineKeyboardBuilder()
    for ch in CHANNELS:
        kb.button(text=f"عضویت در {ch['username']}", url=ch["url"])
    kb.button(text="✅ بررسی عضویت", callback_data="check_membership")
    kb.adjust(1)
    return kb.as_markup()

def admin_panel_keyboard():
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ افزایش استارز کاربر", callback_data="admin_add_balance")
    kb.button(text="➖ کاهش استارز کاربر", callback_data="admin_sub_balance")
    kb.button(text="📊 آمار ربات", callback_data="admin_stats")
    kb.button(text="🔙 بستن پنل", callback_data="main_menu")
    kb.adjust(1)
    return kb.as_markup()

async def check_membership(bot: Bot, user_id: int) -> list[str]:
    missing = []
    for ch in CHANNELS:
        try:
            member = await bot.get_chat_member(chat_id=ch["username"], user_id=user_id)
            if member.status in ("left", "kicked"):
                missing.append(ch["username"])
        except Exception as e:
            logger.warning(f"بررسی عضویت {ch['username']} ناموفق: {e}")
            missing.append(ch["username"])
    return missing

async def process_referral(bot: Bot, user_id: int):
    user = await db.get_user(user_id)
    if not user or not user["phone"] or not user["referrer_id"] or user["referrer_rewarded"]:
        return
    missing = await check_membership(bot, user_id)
    if missing:
        return
    rewarded = await db.try_reward_referrer(user_id)
    if rewarded:
        try:
            await bot.send_message(
                user["referrer_id"],
                "🎉 یک دوست با لینک دعوت شما وارد شد و پاداش ۲ استارز به حساب شما اضافه شد!"
            )
            logger.info(f"✅ پاداش رفرال برای {user['referrer_id']} ثبت شد")
        except Exception as e:
            logger.error(f"خطا در ارسال پیام به معرف {user['referrer_id']}: {e}")


router = Router()

# ============================================================
# ۱) /admin
# ============================================================
@router.message(Command("admin"))
async def cmd_admin(message: Message, state: FSMContext):
    logger.info(f"🔔 /admin از {message.from_user.id}")
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔ دسترسی ندارید")
        return
    await state.clear()
    await message.answer(
        "👨‍💻 **پنل مدیریت**\n\nیک گزینه را انتخاب کنید:",
        reply_markup=admin_panel_keyboard(),
        parse_mode="Markdown",
    )

# ============================================================
# ۲) ادمین: آیدی کاربر (بالاترین اولویت)
# ============================================================
@router.message(AdminFlow.waiting_user_id)
async def admin_get_user_id(message: Message, state: FSMContext):
    logger.info(f"📝 آیدی خام از {message.from_user.id}: '{message.text}'")
    if message.from_user.id != ADMIN_ID:
        return
    text = (message.text or "").strip()
    try:
        target_id = int(text)
    except (ValueError, TypeError):
        await message.answer("❌ آیدی نامعتبر است. فقط عدد بفرستید.")
        return
    target = await db.get_user(target_id)
    if not target:
        await message.answer(f"❌ کاربری با آیدی {target_id} یافت نشد.")
        return
    await state.update_data(target_id=target_id)
    await state.set_state(AdminFlow.waiting_amount)
    await message.answer(
        f"👤 کاربر: {target['full_name'] or '-'}\n"
        f"💰 موجودی فعلی: {target['balance']} استارز\n\n"
        f"مقدار استارز را وارد کنید (فقط عدد):",
    )

# ============================================================
# ۳) ادمین: مقدار
# ============================================================
@router.message(AdminFlow.waiting_amount)
async def admin_get_amount(message: Message, state: FSMContext, bot: Bot):
    logger.info(f"💵 مقدار خام از {message.from_user.id}: '{message.text}'")
    if message.from_user.id != ADMIN_ID:
        return
    text = (message.text or "").strip()
    try:
        amount = int(text)
        if amount <= 0:
            raise ValueError
    except (ValueError, TypeError):
        await message.answer("❌ مقدار باید یه عدد مثبت باشه.")
        return
    data = await state.get_data()
    op = data.get("admin_op")
    target_id = data.get("target_id")
    if not target_id:
        await state.clear()
        await message.answer("خطا: آیدی گم شد. دوباره /admin بزنید.")
        return

    signed = amount if op == "admin_add_balance" else -amount
    ok = await db.add_balance(target_id, signed)
    if not ok:
        await message.answer("❌ کاربر پیدا نشد یا خطا رخ داد.")
        await state.clear()
        return

    new_balance = await db.get_balance(target_id)
    action = "افزایش" if signed > 0 else "کاهش"
    await message.answer(
        f"✅ **{action} انجام شد**\n\n"
        f"کاربر: `{target_id}`\n"
        f"مقدار: {signed:+d} استارز\n"
        f"موجودی جدید: {new_balance} استارز",
        parse_mode="Markdown",
        reply_markup=admin_panel_keyboard(),
    )

    try:
        await bot.send_message(
            target_id,
            f"🎁 موجودی شما تغییر کرد!\n"
            f"مقدار: {signed:+d} استارز\n"
            f"موجودی جدید شما: {new_balance} استارز"
        )
    except Exception:
        pass

    await state.clear()

# ============================================================
# ۴) ادمین: callback ها
# ============================================================
@router.callback_query(F.data == "admin_stats")
async def cb_admin_stats(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        await call.answer("⛔ دسترسی ندارید", show_alert=True)
        return
    stats = await db.get_stats()
    text = (
        f"📊 **آمار ربات**\n\n"
        f"👥 تعداد کاربران: {stats['users']}\n"
        f"⏳ سفارش‌های در انتظار: {stats['pending']}\n"
        f"✅ سفارش‌های تکمیل شده: {stats['completed']}\n"
    )
    try:
        await call.message.edit_text(text, parse_mode="Markdown", reply_markup=admin_panel_keyboard())
    except Exception:
        await call.message.answer(text, parse_mode="Markdown", reply_markup=admin_panel_keyboard())
    await call.answer()

@router.callback_query(F.data.in_({"admin_add_balance", "admin_sub_balance"}))
async def cb_admin_change_balance(call: CallbackQuery, state: FSMContext):
    logger.info(f"🔧 دکمه ادمین {call.data} از {call.from_user.id}")
    if call.from_user.id != ADMIN_ID:
        await call.answer("⛔ دسترسی ندارید", show_alert=True)
        return
    op = call.data
    await state.update_data(admin_op=op)
    await state.set_state(AdminFlow.waiting_user_id)
    action = "افزایش" if op == "admin_add_balance" else "کاهش"
    try:
        await call.message.edit_text(
            f"✍️ **{action} استارز**\n\nآیدی عددی کاربر مورد نظر را ارسال کنید:",
            parse_mode="Markdown",
        )
    except Exception:
        await call.message.answer(
            f"✍️ **{action} استارز**\n\nآیدی عددی کاربر مورد نظر را ارسال کنید:",
            parse_mode="Markdown",
        )
    await call.answer("حالت فعال شد")

# ============================================================
# ۵) استارت
# ============================================================
@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext, bot: Bot):
    await state.clear()
    user_id = message.from_user.id

    args = message.text.split(maxsplit=1)
    referrer_id = None
    if len(args) > 1 and args[1].startswith("ref_"):
        try:
            referrer_id = int(args[1][4:])
            if referrer_id == user_id:
                referrer_id = None
        except ValueError:
            pass

    user = await db.get_user(user_id)
    
    if not user or not user["phone"]:
        await state.update_data(referrer_id=referrer_id)
        await message.answer(
            "برای استفاده از ربات، ابتدا باید شماره موبایل خود را تأیید کنید.\n"
            "لطفاً روی دکمه زیر بزنید:",
            reply_markup=phone_keyboard(),
        )
        return

    if referrer_id and not user["referrer_id"]:
        await db.set_referrer(user_id, referrer_id)
        logger.info(f"🔗 معرف {referrer_id} برای کاربر {user_id} ثبت شد")

    missing = await check_membership(bot, user_id)
    if missing:
        await message.answer(
            "برای استفاده از ربات، ابتدا در کانال‌های زیر عضو شوید:\n" + "\n".join(missing),
            reply_markup=channels_keyboard(),
        )
        return

    await process_referral(bot, user_id)
    await message.answer("منوی اصلی:", reply_markup=main_menu_keyboard())

# ============================================================
# ۶) دریافت شماره
# ============================================================
@router.message(F.contact)
async def handle_contact(message: Message, state: FSMContext, bot: Bot):
    user_id = message.from_user.id
    contact: Contact = message.contact

    if contact.user_id != user_id:
        await message.answer("❌ شما فقط می‌توانید شماره موبایل خودتان را ارسال کنید.")
        return

    phone = contact.phone_number.replace(" ", "").replace("-", "")
    if phone.startswith("0"):
        phone = "+98" + phone[1:]
    elif phone.startswith("98") and not phone.startswith("+"):
        phone = "+" + phone
    elif not phone.startswith("+"):
        phone = "+98" + phone

    if not re.fullmatch(r"\+989\d{9}", phone):
        await message.answer("❌ فرمت شماره معتبر نیست.")
        return

    existing = await db.get_user_by_phone(phone)
    if existing and existing["user_id"] != user_id:
        await message.answer("❌ این شماره قبلاً ثبت شده است.")
        return

    data = await state.get_data()
    referrer_id = data.get("referrer_id")

    await db.upsert_user(
        user_id=user_id,
        username=message.from_user.username,
        full_name=message.from_user.full_name,
        phone=phone,
        referrer_id=referrer_id,
    )

    missing = await check_membership(bot, user_id)
    if missing:
        await message.answer(
            "✅ شماره شما تأیید شد.\n\nبرای ادامه، در کانال‌ها عضو شوید:",
            reply_markup=channels_keyboard(),
        )
    else:
        await process_referral(bot, user_id)
        await message.answer(
            "✅ شماره شما تأیید شد و در کانال‌ها عضو هستید.\nبه منوی اصلی خوش آمدید:",
            reply_markup=main_menu_keyboard(),
        )
    await state.clear()

# ============================================================
# ۷) بررسی عضویت
# ============================================================
@router.callback_query(F.data == "check_membership")
async def cb_check_membership(call: CallbackQuery, bot: Bot):
    user = await db.get_user(call.from_user.id)
    if not user or not user["phone"]:
        await call.answer("ابتدا شماره موبایل خود را تأیید کنید.", show_alert=True)
        return

    missing = await check_membership(bot, call.from_user.id)
    if missing:
        await call.answer("هنوز در همه کانال‌ها عضو نشده‌اید.", show_alert=True)
        return

    await process_referral(bot, call.from_user.id)

    try:
        await call.message.delete()
    except Exception:
        pass
    await call.message.answer("✅ عضویت تأیید شد. به منوی اصلی خوش آمدید.", reply_markup=main_menu_keyboard())
    await call.answer()

@router.callback_query(F.data == "main_menu")
async def cb_main_menu(call: CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        await call.message.delete()
    except Exception:
        pass
    await call.message.answer("منوی اصلی:", reply_markup=main_menu_keyboard())
    await call.answer()

# ============================================================
# ۸) منوهای کاربر (فقط اگه تو حالت ادمین نیست)
# ============================================================
@router.message(F.text == BTN_INVITE)
async def show_invite_link(message: Message, bot: Bot, state: FSMContext):
    current = await state.get_state()
    if current and current.startswith("AdminFlow"):
        return  # ادمین تو حالته، این پیام نادیده بگیر
    await state.clear()
    user_id = message.from_user.id
    user = await db.get_user(user_id)
    if not user:
        await message.answer("لطفاً ابتدا /start را ارسال کنید.")
        return

    me = await bot.get_me()
    link = f"https://t.me/{me.username}?start=ref_{user_id}"
    await message.answer(
        f"لینک دعوت اختصاصی شما:\n\n`{link}`\n\n"
        "با دعوت از دوستان خود، پس از تأیید شماره و عضویت آن‌ها، ۲ استارز دریافت می‌کنید.",
        parse_mode="Markdown",
    )

@router.message(F.text == BTN_BUY)
async def show_gifts(message: Message, state: FSMContext):
    current = await state.get_state()
    if current and current.startswith("AdminFlow"):
        return
    await state.clear()
    await message.answer("یک گیفت انتخاب کنید:", reply_markup=gift_keyboard())

@router.message(F.text == BTN_BALANCE)
async def show_balance(message: Message, state: FSMContext):
    current = await state.get_state()
    if current and current.startswith("AdminFlow"):
        return
    await state.clear()
    try:
        user_id = message.from_user.id
        balance = await db.get_balance(user_id)
        txs = await db.get_transactions(user_id)

        text = f"⭐ موجودی فعلی شما: **{balance}** استارز\n\n"
        if txs:
            text += "📋 تاریخچه تراکنش‌ها:\n"
            for tx in txs:
                sign = "+" if tx["amount"] > 0 else ""
                desc = tx["description"] or tx["type"]
                text += f"• {sign}{tx['amount']} — {desc}\n"
        else:
            text += "هنوز تراکنشی ثبت نشده است."

        await message.answer(text, parse_mode="Markdown")
    except Exception as e:
        logger.exception(f"خطا در show_balance: {e}")
        await message.answer("خطا در دریافت موجودی. لطفاً دوباره تلاش کنید.")

# ============================================================
# ۹) گیفت‌ها
# ============================================================
@router.callback_query(F.data.startswith("gift:"))
async def cb_select_gift(call: CallbackQuery, state: FSMContext, bot: Bot):
    try:
        key = call.data.split(":", 1)[1]
        gift = GIFTS.get(key)
        if not gift:
            await call.answer("گیفت نامعتبر", show_alert=True)
            return

        balance = await db.get_balance(call.from_user.id)
        if balance < gift["price"]:
            await call.answer(f"موجودی کافی نیست. موجودی شما: {balance} استارز", show_alert=True)
            return

        await state.update_data(gift_key=key, gift_price=gift["price"], gift_name=gift["name"])
        await state.set_state(BuyGift.waiting_recipient)

        text = (
            f"شما {gift['name']} را به قیمت {gift['price']} استارز انتخاب کردید.\n\n"
            "لطفاً آیدی عددی یا یوزرنیم گیرنده را ارسال کنید:"
        )
        try:
            await call.message.edit_text(text, reply_markup=back_keyboard())
        except Exception:
            await call.message.answer(text, reply_markup=back_keyboard())

        await call.answer()
    except Exception as e:
        logger.exception(f"خطا در cb_select_gift: {e}")
        await call.answer("خطا رخ داد", show_alert=True)

@router.callback_query(F.data == "confirm_purchase", BuyGift.confirming)
async def cb_confirm_purchase(call: CallbackQuery, state: FSMContext, bot: Bot):
    try:
        data = await state.get_data()
        gift_key = data.get("gift_key")
        gift_price = data.get("gift_price")
        gift_name = data.get("gift_name")
        recipient = data.get("recipient")
        user_id = call.from_user.id

        if not all([gift_key, gift_price, gift_name, recipient]):
            await state.clear()
            await call.message.edit_text("خطا: اطلاعات ناقص است.")
            await call.answer()
            return

        order_id = uuid.uuid4().hex[:12].upper()
        success = await db.create_order(order_id, user_id, gift_key, gift_price, recipient)

        if not success:
            await call.message.edit_text("❌ موجودی ناکافی است.")
            await state.clear()
            await call.answer()
            return

        user = await db.get_user(user_id)
        admin_text = (
            f"🛒 **سفارش جدید**\n\n"
            f"شناسه: `{order_id}`\n"
            f"خریدار: {user['full_name']} (`{user_id}`)\n"
            f"گیرنده: `{recipient}`\n"
            f"گیفت: {gift_name}\n"
            f"قیمت: {gift_price} استارز\n"
            f"تاریخ: {datetime.now().strftime('%Y-%m-%d %H:%M')}"
        )
        try:
            await bot.send_message(ADMIN_ID, admin_text, parse_mode="Markdown", reply_markup=order_admin_keyboard(order_id))
        except Exception as e:
            logger.error(f"ارسال به ادمین ناموفق: {e}")

        await call.message.edit_text(
            f"✅ سفارش شما ثبت شد.\nشناسه: `{order_id}`",
            parse_mode="Markdown",
        )
        await state.clear()
        await call.answer()
    except Exception as e:
        logger.exception(f"خطا در cb_confirm_purchase: {e}")
        await call.answer("خطا رخ داد", show_alert=True)

@router.message(BuyGift.waiting_recipient)
async def receive_recipient(message: Message, state: FSMContext, bot: Bot):
    if message.text in MENU_TEXTS:
        await state.clear()
        if message.text == BTN_BUY:
            await show_gifts(message, state)
        elif message.text == BTN_BALANCE:
            await show_balance(message, state)
        elif message.text == BTN_INVITE:
            await show_invite_link(message, bot, state)
        return

    recipient = (message.text or "").strip()
    if not recipient:
        await message.answer("لطفاً یک آیدی معتبر ارسال کنید.")
        return

    data = await state.get_data()
    gift_name = data.get("gift_name")
    gift_price = data.get("gift_price")
    gift_key = data.get("gift_key")

    if not all([gift_name, gift_price, gift_key]):
        await state.clear()
        await message.answer("خطا: اطلاعات گیفت پیدا نشد.")
        return

    await state.update_data(recipient=recipient)
    await state.set_state(BuyGift.confirming)

    kb = InlineKeyboardBuilder()
    kb.button(text="✅ تأیید نهایی خرید", callback_data="confirm_purchase")
    kb.button(text="🔙 بازگشت", callback_data="main_menu")
    kb.adjust(1)

    await message.answer(
        f"📋 پیش‌فاکتور:\n"
        f"گیفت: {gift_name}\n"
        f"قیمت: {gift_price} استارز\n"
        f"گیرنده: {recipient}\n\n"
        f"آیا تأیید می‌کنید؟",
        reply_markup=kb.as_markup(),
    )

# ============================================================
# ۱۰) تأیید/رد/انجام سفارش
# ============================================================
@router.callback_query(F.data.startswith("approve:"))
async def cb_approve_order(call: CallbackQuery, bot: Bot):
    if call.from_user.id != ADMIN_ID:
        await call.answer("⛔", show_alert=True)
        return
    try:
        order_id = call.data.split(":", 1)[1]
        ok = await db.approve_order(order_id)
        if not ok:
            await call.answer("سفارش قبلاً پردازش شده.", show_alert=True)
            return
        await call.message.edit_text(
            call.message.text + "\n\n✅ **تأیید شد** — پس از ارسال دستی، دکمه زیر را بزنید.",
            parse_mode="Markdown",
            reply_markup=order_complete_keyboard(order_id),
        )
        await call.answer("تأیید شد")
    except Exception as e:
        logger.exception(f"خطا: {e}")
        await call.answer("خطا", show_alert=True)

@router.callback_query(F.data.startswith("reject:"))
async def cb_reject_order(call: CallbackQuery, bot: Bot):
    if call.from_user.id != ADMIN_ID:
        await call.answer("⛔", show_alert=True)
        return
    try:
        order_id = call.data.split(":", 1)[1]
        ok = await db.reject_order(order_id)
        if not ok:
            await call.answer("سفارش قبلاً پردازش شده.", show_alert=True)
            return
        order = await db.get_order(order_id)
        try:
            await bot.send_message(
                order["user_id"],
                f"❌ سفارش `{order_id}` رد شد. {order['gift_price']} استارز بازگشت.",
                parse_mode="Markdown",
            )
        except Exception:
            pass
        await call.message.edit_text(call.message.text + "\n\n❌ **رد شد**", parse_mode="Markdown")
        await call.answer("رد شد")
    except Exception as e:
        logger.exception(f"خطا: {e}")
        await call.answer("خطا", show_alert=True)

@router.callback_query(F.data.startswith("complete:"))
async def cb_complete_order(call: CallbackQuery, bot: Bot):
    if call.from_user.id != ADMIN_ID:
        await call.answer("⛔", show_alert=True)
        return
    try:
        order_id = call.data.split(":", 1)[1]
        ok = await db.mark_order_completed(order_id)
        if not ok:
            await call.answer("سفارش تأیید نشده یا قبلاً منتشر شده.", show_alert=True)
            return

        order = await db.get_order(order_id)
        user = await db.get_user(order["user_id"])

        publish_text = (
            f"✅ **سفارش انجام شد**\n\n"
            f"شناسه: `{order_id}`\n"
            f"خریدار: {user['full_name']}\n"
            f"گیفت: {order['gift_name']}\n"
            f"گیرنده: `{order['recipient']}`\n"
            f"تاریخ: {datetime.now().strftime('%Y-%m-%d %H:%M')}"
        )
        try:
            await bot.send_message(PUBLISH_CHANNEL, publish_text, parse_mode="Markdown")
        except Exception as e:
            logger.error(f"انتشار ناموفق: {e}")

        try:
            await bot.send_message(
                order["user_id"],
                f"🎉 سفارش `{order_id}` انجام و منتشر شد.",
                parse_mode="Markdown",
            )
        except Exception:
            pass

        await call.message.edit_text(call.message.text + "\n\n📦 **انجام شد و منتشر شد**", parse_mode="Markdown")
        await call.answer("ثبت شد")
    except Exception as e:
        logger.exception(f"خطا: {e}")
        await call.answer("خطا", show_alert=True)


async def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN تنظیم نشده است")
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL تنظیم نشده است")

    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    me = await bot.get_me()
    logger.info(f"ربات متصل شد: @{me.username}")
    logger.info(f"ADMIN_ID = {ADMIN_ID}")

    await db.connect()
    await bot.delete_webhook(drop_pending_updates=True)

    try:
        await dp.start_polling(bot)
    finally:
        await db.close()
        await bot.session.close()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("ربات متوقف شد")
    except Exception as e:
        logger.exception(f"خطای مهلک: {e}")
        sys.exit(1)
