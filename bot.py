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
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message, CallbackQuery, ReplyKeyboardMarkup, KeyboardButton, Contact
)
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder

# ---------------- تنظیمات ----------------
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
router = Router()


# ---------------- دیتابیس ----------------
INIT_SQL = """
CREATE TABLE IF NOT EXISTS users (
    user_id BIGINT PRIMARY KEY,
    username TEXT,
    full_name TEXT,
    phone TEXT,
    balance INTEGER NOT NULL DEFAULT 0,
    referrer_id BIGINT,
    referrer_rewarded BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
ALTER TABLE users ADD COLUMN IF NOT EXISTS username TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS full_name TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS phone TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS balance INTEGER NOT NULL DEFAULT 0;
ALTER TABLE users ADD COLUMN IF NOT EXISTS referrer_id BIGINT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS referrer_rewarded BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE users ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

CREATE TABLE IF NOT EXISTS orders (
    order_id TEXT PRIMARY KEY,
    user_id BIGINT,
    gift_name TEXT,
    gift_price INTEGER,
    recipient TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    published BOOLEAN NOT NULL DEFAULT FALSE
);
ALTER TABLE orders ADD COLUMN IF NOT EXISTS user_id BIGINT;
ALTER TABLE orders ADD COLUMN IF NOT EXISTS gift_name TEXT;
ALTER TABLE orders ADD COLUMN IF NOT EXISTS gift_price INTEGER;
ALTER TABLE orders ADD COLUMN IF NOT EXISTS recipient TEXT;
ALTER TABLE orders ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'pending';
ALTER TABLE orders ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
ALTER TABLE orders ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
ALTER TABLE orders ADD COLUMN IF NOT EXISTS published BOOLEAN NOT NULL DEFAULT FALSE;

CREATE TABLE IF NOT EXISTS referrals (
    id BIGSERIAL PRIMARY KEY,
    referrer_id BIGINT,
    referred_id BIGINT,
    rewarded BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
ALTER TABLE referrals ADD COLUMN IF NOT EXISTS referrer_id BIGINT;
ALTER TABLE referrals ADD COLUMN IF NOT EXISTS referred_id BIGINT;
ALTER TABLE referrals ADD COLUMN IF NOT EXISTS rewarded BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE referrals ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

DELETE FROM referrals a USING referrals b
WHERE a.referred_id = b.referred_id
AND a.referred_id IS NOT NULL AND a.id > b.id;
CREATE UNIQUE INDEX IF NOT EXISTS referrals_referred_unique_idx
ON referrals(referred_id);

CREATE TABLE IF NOT EXISTS transactions (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT,
    amount INTEGER,
    type TEXT,
    description TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS user_id BIGINT;
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS amount INTEGER;
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS type TEXT;
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS description TEXT;
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

CREATE INDEX IF NOT EXISTS idx_users_referrer ON users(referrer_id);
CREATE INDEX IF NOT EXISTS idx_orders_user ON orders(user_id);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
CREATE INDEX IF NOT EXISTS idx_transactions_user ON transactions(user_id);

CREATE OR REPLACE FUNCTION create_order_atomic(
    p_order_id TEXT, p_user_id BIGINT, p_gift_name TEXT,
    p_gift_price INTEGER, p_recipient TEXT
) RETURNS BOOLEAN AS $$
DECLARE v_balance INTEGER;
BEGIN
    SELECT balance INTO v_balance FROM users
    WHERE user_id=p_user_id FOR UPDATE;
    IF NOT FOUND OR v_balance < p_gift_price THEN RETURN FALSE; END IF;

    UPDATE users SET balance=balance-p_gift_price WHERE user_id=p_user_id;
    INSERT INTO orders(order_id,user_id,gift_name,gift_price,recipient,status)
    VALUES(p_order_id,p_user_id,p_gift_name,p_gift_price,p_recipient,'pending');
    INSERT INTO transactions(user_id,amount,type,description)
    VALUES(p_user_id,-p_gift_price,'purchase','خرید ' || p_gift_name);
    RETURN TRUE;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION reject_order_atomic(p_order_id TEXT)
RETURNS BOOLEAN AS $$
DECLARE v_order orders%ROWTYPE;
BEGIN
    SELECT * INTO v_order FROM orders
    WHERE order_id=p_order_id AND status='pending' FOR UPDATE;
    IF NOT FOUND THEN RETURN FALSE; END IF;

    UPDATE orders SET status='rejected',updated_at=NOW()
    WHERE order_id=p_order_id;
    UPDATE users SET balance=balance+v_order.gift_price
    WHERE user_id=v_order.user_id;
    INSERT INTO transactions(user_id,amount,type,description)
    VALUES(v_order.user_id,v_order.gift_price,'refund',
           'بازگشت وجه سفارش ' || p_order_id);
    RETURN TRUE;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION reward_referrer_atomic(p_referred_id BIGINT)
RETURNS BOOLEAN AS $$
DECLARE v_referrer_id BIGINT;
BEGIN
    SELECT referrer_id INTO v_referrer_id FROM users
    WHERE user_id=p_referred_id AND referrer_rewarded=FALSE FOR UPDATE;
    IF NOT FOUND OR v_referrer_id IS NULL
       OR v_referrer_id=p_referred_id THEN RETURN FALSE; END IF;

    PERFORM 1 FROM users WHERE user_id=v_referrer_id FOR UPDATE;
    IF NOT FOUND THEN RETURN FALSE; END IF;

    UPDATE users SET balance=balance+2 WHERE user_id=v_referrer_id;
    UPDATE users SET referrer_rewarded=TRUE WHERE user_id=p_referred_id;
    INSERT INTO referrals(referrer_id,referred_id,rewarded)
    VALUES(v_referrer_id,p_referred_id,TRUE)
    ON CONFLICT(referred_id) DO UPDATE SET rewarded=TRUE;
    INSERT INTO transactions(user_id,amount,type,description)
    VALUES(v_referrer_id,2,'invite_reward',
           'پاداش دعوت کاربر ' || p_referred_id);
    RETURN TRUE;
END;
$$ LANGUAGE plpgsql;
"""


class Database:
    def __init__(self):
        self.pool = None

    async def connect(self):
        if not DATABASE_URL:
            raise RuntimeError("DATABASE_URL تنظیم نشده است")
        self.pool = await asyncpg.create_pool(
            DATABASE_URL, min_size=1, max_size=5,
            command_timeout=60, ssl="require"
        )
        async with self.pool.acquire() as conn:
            await conn.execute(INIT_SQL)
        logger.info("دیتابیس آماده است")

    async def close(self):
        if self.pool:
            await self.pool.close()

    async def get_user(self, user_id):
        async with self.pool.acquire() as conn:
            return await conn.fetchrow(
                "SELECT * FROM users WHERE user_id=$1", user_id
            )

    async def get_user_by_phone(self, phone):
        async with self.pool.acquire() as conn:
            return await conn.fetchrow(
                "SELECT * FROM users WHERE phone=$1", phone
            )

    async def set_referrer(self, user_id, referrer_id):
        if user_id == referrer_id:
            return False
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                if not await conn.fetchval(
                    "SELECT EXISTS(SELECT 1 FROM users WHERE user_id=$1)",
                    referrer_id
                ):
                    return False
                await conn.execute("""
                    INSERT INTO users(user_id,referrer_id) VALUES($1,$2)
                    ON CONFLICT(user_id) DO UPDATE
                    SET referrer_id=COALESCE(users.referrer_id,EXCLUDED.referrer_id)
                """, user_id, referrer_id)
                actual = await conn.fetchval(
                    "SELECT referrer_id FROM users WHERE user_id=$1", user_id
                )
                if actual != referrer_id:
                    return False
                await conn.execute("""
                    INSERT INTO referrals(referrer_id,referred_id,rewarded)
                    VALUES($1,$2,FALSE)
                    ON CONFLICT(referred_id) DO NOTHING
                """, referrer_id, user_id)
                return True

    async def upsert_user(self, user_id, username, full_name, phone, referrer_id=None):
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    "SELECT referrer_id FROM users WHERE user_id=$1 FOR UPDATE",
                    user_id
                )
                if row:
                    await conn.execute("""
                        UPDATE users SET username=$1,full_name=$2,phone=$3,
                        referrer_id=COALESCE(referrer_id,$4)
                        WHERE user_id=$5
                    """, username, full_name, phone, referrer_id, user_id)
                    final_ref = row["referrer_id"] or referrer_id
                else:
                    final_ref = referrer_id if referrer_id != user_id else None
                    await conn.execute("""
                        INSERT INTO users(user_id,username,full_name,phone,referrer_id)
                        VALUES($1,$2,$3,$4,$5)
                    """, user_id, username, full_name, phone, final_ref)
                if final_ref and final_ref != user_id:
                    await conn.execute("""
                        INSERT INTO referrals(referrer_id,referred_id,rewarded)
                        VALUES($1,$2,FALSE) ON CONFLICT(referred_id) DO NOTHING
                    """, final_ref, user_id)

    async def try_reward_referrer(self, user_id):
        async with self.pool.acquire() as conn:
            return bool(await conn.fetchval(
                "SELECT reward_referrer_atomic($1)", user_id
            ))

    async def create_order(self, order_id, user_id, gift_key, price, recipient):
        async with self.pool.acquire() as conn:
            return bool(await conn.fetchval(
                "SELECT create_order_atomic($1,$2,$3,$4,$5)",
                order_id, user_id, GIFTS[gift_key]["name"], price, recipient
            ))

    async def reject_order(self, order_id):
        async with self.pool.acquire() as conn:
            return bool(await conn.fetchval(
                "SELECT reject_order_atomic($1)", order_id
            ))

    async def approve_order(self, order_id):
        async with self.pool.acquire() as conn:
            result = await conn.execute("""
                UPDATE orders SET status='approved',updated_at=NOW()
                WHERE order_id=$1 AND status='pending'
            """, order_id)
            return result == "UPDATE 1"

    async def get_order(self, order_id):
        async with self.pool.acquire() as conn:
            return await conn.fetchrow(
                "SELECT * FROM orders WHERE order_id=$1", order_id
            )

    async def mark_order_completed(self, order_id):
        async with self.pool.acquire() as conn:
            result = await conn.execute("""
                UPDATE orders SET status='completed',published=TRUE,updated_at=NOW()
                WHERE order_id=$1 AND status='approved' AND published=FALSE
            """, order_id)
            return result == "UPDATE 1"

    async def get_balance(self, user_id):
        async with self.pool.acquire() as conn:
            value = await conn.fetchval(
                "SELECT balance FROM users WHERE user_id=$1", user_id
            )
            return int(value or 0)

    # اصلاح اصلی: مقدار موجودی جدید را برمی‌گرداند و تراکنش را اتمیک ثبت می‌کند.
    async def add_balance(self, user_id: int, amount: int):
        if not isinstance(user_id, int) or user_id <= 0:
            raise ValueError("شناسه کاربر نامعتبر است")
        if not isinstance(amount, int) or amount == 0:
            raise ValueError("مبلغ تغییر موجودی نامعتبر است")
        max_int = 2_147_483_647
        if abs(amount) > max_int:
            raise ValueError("مبلغ بیش از محدوده مجاز است")

        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow("""
                    SELECT COALESCE(balance,0) AS balance
                    FROM users WHERE user_id=$1 FOR UPDATE
                """, user_id)
                if row is None:
                    return False

                old_balance = int(row["balance"])
                new_balance = old_balance + amount
                if new_balance < 0:
                    raise ValueError(
                        f"موجودی کافی نیست؛ موجودی فعلی {old_balance} است"
                    )
                if new_balance > max_int:
                    raise ValueError("موجودی از محدوده مجاز دیتابیس بیشتر می‌شود")

                actual = new_balance - old_balance
                await conn.execute(
                    "UPDATE users SET balance=$1 WHERE user_id=$2",
                    new_balance, user_id
                )
                await conn.execute("""
                    INSERT INTO transactions(user_id,amount,type,description)
                    VALUES($1,$2,'admin_adjust',$3)
                """, user_id, actual,
                    "افزایش موجودی توسط ادمین" if actual > 0
                    else "کاهش موجودی توسط ادمین")
                return new_balance

    async def get_transactions(self, user_id, limit=20):
        async with self.pool.acquire() as conn:
            return await conn.fetch("""
                SELECT amount,type,description,created_at FROM transactions
                WHERE user_id=$1 ORDER BY created_at DESC LIMIT $2
            """, user_id, limit)


db = Database()


# ---------------- وضعیت‌ها ----------------
class BuyGift(StatesGroup):
    waiting_recipient = State()
    confirming = State()


class AdminStates(StatesGroup):
    waiting_user_id = State()
    waiting_amount = State()


# ---------------- کیبوردها ----------------
def main_menu_keyboard():
    kb = ReplyKeyboardBuilder()
    kb.button(text=BTN_INVITE)
    kb.button(text=BTN_BUY)
    kb.button(text=BTN_BALANCE)
    kb.adjust(2, 1)
    return kb.as_markup(resize_keyboard=True)


def phone_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(
            text="📱 تأیید شماره موبایل", request_contact=True
        )]],
        resize_keyboard=True,
        one_time_keyboard=True
    )


def gift_keyboard():
    kb = InlineKeyboardBuilder()
    for key, gift in GIFTS.items():
        kb.button(text=f"{gift['name']} — {gift['price']} استارز",
                  callback_data=f"gift:{key}")
    kb.adjust(1)
    return kb.as_markup()


def order_admin_keyboard(order_id):
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ تأیید سفارش", callback_data=f"approve:{order_id}")
    kb.button(text="❌ رد سفارش", callback_data=f"reject:{order_id}")
    kb.adjust(2)
    return kb.as_markup()


def order_complete_keyboard(order_id):
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
    kb.button(text="➕ افزایش موجودی", callback_data="admin_add")
    kb.button(text="➖ کاهش موجودی", callback_data="admin_sub")
    kb.button(text="📊 آمار ربات", callback_data="admin_stats")
    kb.button(text="🔙 بستن پنل", callback_data="main_menu")
    kb.adjust(1)
    return kb.as_markup()


# ---------------- عضویت و رفرال ----------------
async def check_membership(bot, user_id):
    missing = []
    for ch in CHANNELS:
        try:
            member = await bot.get_chat_member(ch["username"], user_id)
            status = str(member.status)
            if status in ("left", "kicked"):
                missing.append(ch["username"])
            elif status == "restricted" and not getattr(member, "is_member", False):
                missing.append(ch["username"])
        except Exception:
            logger.exception("بررسی عضویت کانال %s ناموفق بود", ch["username"])
            missing.append(ch["username"])
    return missing


async def process_referral(bot, user_id):
    user = await db.get_user(user_id)
    if not user or not user["phone"] or not user["referrer_id"]:
        return
    if user["referrer_rewarded"] or await check_membership(bot, user_id):
        return
    if await db.try_reward_referrer(user_id):
        try:
            await bot.send_message(
                user["referrer_id"],
                "🎉 دوست شما با لینک دعوت وارد شد؛ ۲ استارز به موجودی شما اضافه شد!"
            )
        except Exception:
            logger.exception("ارسال پیام پاداش معرف ناموفق بود")


# ---------------- پنل ادمین ----------------
@router.message(Command("admin"))
async def cmd_admin(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔ دسترسی ندارید")
        return
    await state.clear()
    await message.answer(
        "👨‍💻 <b>پنل مدیریت</b>\nیک گزینه را انتخاب کنید:",
        reply_markup=admin_panel_keyboard()
    )


@router.callback_query(F.data.in_({"admin_add", "admin_sub"}))
async def cb_admin_change_balance(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        await call.answer("⛔ دسترسی ندارید", show_alert=True)
        return
    await state.clear()
    await state.update_data(admin_op=call.data)
    await state.set_state(AdminStates.waiting_user_id)
    action = "افزایش" if call.data == "admin_add" else "کاهش"
    text = f"✍️ <b>{action} موجودی</b>\nآیدی عددی کاربر را بفرستید:"
    try:
        await call.message.edit_text(text)
    except Exception:
        await call.message.answer(text)
    await call.answer()


@router.callback_query(F.data == "admin_stats")
async def cb_admin_stats(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        await call.answer("⛔ دسترسی ندارید", show_alert=True)
        return
    try:
        async with db.pool.acquire() as conn:
            users = await conn.fetchval("SELECT COUNT(*) FROM users")
            pending = await conn.fetchval(
                "SELECT COUNT(*) FROM orders WHERE status='pending'")
            completed = await conn.fetchval(
                "SELECT COUNT(*) FROM orders WHERE status='completed'")
        text = (
            "📊 <b>آمار ربات</b>\n\n"
            f"👥 کاربران: {users}\n"
            f"⏳ سفارش‌های در انتظار: {pending}\n"
            f"✅ سفارش‌های تکمیل‌شده: {completed}"
        )
        try:
            await call.message.edit_text(text, reply_markup=admin_panel_keyboard())
        except Exception:
            await call.message.answer(text, reply_markup=admin_panel_keyboard())
    except Exception:
        logger.exception("خطای آمار")
        await call.message.answer("❌ خطا در دریافت آمار")
    await call.answer()


@router.message(AdminStates.waiting_user_id)
async def admin_get_user_id(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    raw_id = (message.text or "").strip()
    if not raw_id.isdigit():
        await message.answer("❌ آیدی نامعتبر است؛ فقط عدد مثبت بفرستید.")
        return
    target_id = int(raw_id)
    if target_id <= 0 or target_id > 9_223_372_036_854_775_807:
        await message.answer("❌ آیدی خارج از محدوده مجاز است.")
        return
    try:
        target = await db.get_user(target_id)
    except Exception:
        logger.exception("خطا در خواندن کاربر: user_id=%s", target_id)
        await message.answer("❌ خطا در ارتباط با دیتابیس هنگام پیدا کردن کاربر.")
        return
    if target is None:
        await message.answer(
            "❌ این کاربر هنوز در ربات ثبت نشده است.\n"
            "کاربر باید حداقل یک‌بار ربات را /start کرده باشد."
        )
        return
    data = await state.get_data()
    if data.get("admin_op") not in ("admin_add", "admin_sub"):
        await state.clear()
        await message.answer("❌ عملیات ادمین منقضی شده؛ دوباره /admin را بزنید.")
        return
    await state.update_data(target_id=target_id)
    await state.set_state(AdminStates.waiting_amount)
    await message.answer(
        f"👤 کاربر: {target['full_name'] or '-'}\n"
        f"🆔 آیدی: {target_id}\n"
        f"⭐ موجودی فعلی: {target['balance'] or 0}\n\n"
        "مبلغ را به صورت عدد مثبت ارسال کنید:"
    )


@router.message(AdminStates.waiting_amount)
async def admin_get_amount(message: Message, state: FSMContext, bot: Bot):
    if message.from_user.id != ADMIN_ID:
        return
    raw = (message.text or "").strip().replace(",", "").replace("٬", "")
    if not raw.isdigit():
        await message.answer("❌ مبلغ باید عدد صحیح و مثبت باشد.")
        return
    amount = int(raw)
    if amount <= 0 or amount > 2_147_483_647:
        await message.answer("❌ مبلغ باید بین ۱ تا ۲٬۱۴۷٬۴۸۳٬۶۴۷ باشد.")
        return
    data = await state.get_data()
    target_id = data.get("target_id")
    operation = data.get("admin_op")
    if not isinstance(target_id, int) or operation not in ("admin_add", "admin_sub"):
        await state.clear()
        await message.answer("❌ اطلاعات عملیات ناقص است؛ دوباره /admin بزنید.")
        return
    signed = amount if operation == "admin_add" else -amount
    try:
        new_balance = await db.add_balance(target_id, signed)
        if new_balance is False:
            await message.answer("❌ کاربر در دیتابیس پیدا نشد.")
            return
        action = "افزایش" if signed > 0 else "کاهش"
        await message.answer(
            f"✅ <b>{action} موجودی با موفقیت انجام شد</b>\n\n"
            f"کاربر: <code>{target_id}</code>\n"
            f"تغییر: {signed:+,} استارز\n"
            f"موجودی جدید: <b>{new_balance:,}</b> استارز",
            reply_markup=admin_panel_keyboard()
        )
        try:
            await bot.send_message(
                target_id,
                "🎁 موجودی شما توسط مدیریت تغییر کرد.\n"
                f"تغییر: {signed:+,} استارز\n"
                f"موجودی جدید: {new_balance:,} استارز"
            )
        except Exception:
            logger.exception(
                "موجودی تغییر کرد اما اعلان ارسال نشد: user_id=%s", target_id
            )
    except ValueError as exc:
        await message.answer(f"❌ {exc}")
    except Exception:
        logger.exception(
            "خطای دیتابیس در تغییر موجودی: admin=%s target=%s amount=%s",
            message.from_user.id, target_id, signed
        )
        await message.answer(
            "❌ عملیات دیتابیس ناموفق بود.\n"
            "جزئیات کامل خطا در GitHub Actions ثبت شده است."
        )
    finally:
        await state.clear()


# ---------------- شروع ----------------
@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext, bot: Bot):
    await state.clear()
    user_id = message.from_user.id
    args = (message.text or "").split(maxsplit=1)
    if len(args) > 1 and args[1].startswith("ref_"):
        try:
            referrer_id = int(args[1][4:])
            if referrer_id > 0 and referrer_id != user_id:
                saved = await db.set_referrer(user_id, referrer_id)
                logger.info("Referral user=%s referrer=%s saved=%s",
                            user_id, referrer_id, saved)
        except ValueError:
            pass
        except Exception:
            logger.exception("خطای ثبت معرف")

    user = await db.get_user(user_id)
    if not user or not user["phone"]:
        await message.answer(
            "برای استفاده از ربات ابتدا شماره موبایل خود را تأیید کنید:",
            reply_markup=phone_keyboard()
        )
        return
    missing = await check_membership(bot, user_id)
    if missing:
        await message.answer(
            "ابتدا در کانال‌های زیر عضو شوید:\n" + "\n".join(missing),
            reply_markup=channels_keyboard()
        )
        return
    await process_referral(bot, user_id)
    await message.answer("منوی اصلی:", reply_markup=main_menu_keyboard())


# ---------------- ثبت شماره ----------------
@router.message(F.contact)
async def handle_contact(message: Message, state: FSMContext, bot: Bot):
    contact: Contact = message.contact
    user_id = message.from_user.id
    if contact.user_id != user_id:
        await message.answer("❌ فقط شماره موبایل خودتان را ارسال کنید.")
        return
    phone = re.sub(r"[ ()-]", "", contact.phone_number)
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
    user = await db.get_user(user_id)
    referrer_id = user["referrer_id"] if user else None
    try:
        await db.upsert_user(
            user_id, message.from_user.username,
            message.from_user.full_name, phone, referrer_id
        )
    except asyncpg.UniqueViolationError:
        await message.answer("❌ این شماره قبلاً ثبت شده است.")
        return
    except Exception:
        logger.exception("خطا در ثبت شماره")
        await message.answer("❌ خطا در ثبت شماره؛ دوباره تلاش کنید.")
        return
    missing = await check_membership(bot, user_id)
    if missing:
        await message.answer(
            "✅ شماره تأیید شد.\nبرای ادامه عضو کانال‌ها شوید:",
            reply_markup=channels_keyboard()
        )
    else:
        await process_referral(bot, user_id)
        await message.answer(
            "✅ شماره و عضویت تأیید شد. خوش آمدید!",
            reply_markup=main_menu_keyboard()
        )
    await state.clear()


# ---------------- بررسی عضویت و منو ----------------
@router.callback_query(F.data == "check_membership")
async def cb_check_membership(call: CallbackQuery, bot: Bot):
    user = await db.get_user(call.from_user.id)
    if not user or not user["phone"]:
        await call.answer("ابتدا شماره موبایل را تأیید کنید.", show_alert=True)
        return
    missing = await check_membership(bot, call.from_user.id)
    if missing:
        await call.answer("هنوز عضو همه کانال‌ها نشده‌اید.", show_alert=True)
        return
    await process_referral(bot, call.from_user.id)
    try:
        await call.message.delete()
    except Exception:
        pass
    await call.message.answer(
        "✅ عضویت تأیید شد. منوی اصلی:",
        reply_markup=main_menu_keyboard()
    )
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


async def admin_busy(state: FSMContext):
    current = await state.get_state()
    return bool(current and current.startswith("AdminStates"))


@router.message(F.text == BTN_INVITE)
async def show_invite_link(message: Message, bot: Bot, state: FSMContext):
    if await admin_busy(state):
        return
    await state.clear()
    user = await db.get_user(message.from_user.id)
    if not user or not user["phone"]:
        await message.answer("ابتدا /start را بزنید و شماره خود را تأیید کنید.")
        return
    me = await bot.get_me()
    link = f"https://t.me/{me.username}?start=ref_{message.from_user.id}"
    await message.answer(
        f"🔗 لینک دعوت شما:\n\n<code>{link}</code>\n\n"
        "پس از تأیید شماره و عضویت دوستتان در کانال‌ها، "
        "۲ استارز پاداش می‌گیرید."
    )


@router.message(F.text == BTN_BUY)
async def show_gifts(message: Message, state: FSMContext):
    if await admin_busy(state):
        return
    await state.clear()
    user = await db.get_user(message.from_user.id)
    if not user or not user["phone"]:
        await message.answer("ابتدا شماره موبایل خود را تأیید کنید.")
        return
    await message.answer("🎁 یک گیفت انتخاب کنید:", reply_markup=gift_keyboard())


@router.message(F.text == BTN_BALANCE)
async def show_balance(message: Message, state: FSMContext):
    if await admin_busy(state):
        return
    await state.clear()
    try:
        user_id = message.from_user.id
        balance = await db.get_balance(user_id)
        txs = await db.get_transactions(user_id)
        text = f"⭐ موجودی شما: <b>{balance}</b> استارز\n\n"
        if txs:
            text += "📋 تاریخچه تراکنش‌ها:\n"
            for tx in txs:
                amount = tx["amount"] or 0
                sign = "+" if amount > 0 else ""
                text += f"• {sign}{amount} — {tx['description'] or tx['type']}\n"
        else:
            text += "هنوز تراکنشی ثبت نشده است."
        await message.answer(text)
    except Exception:
        logger.exception("خطا در نمایش موجودی")
        await message.answer("❌ خطا در دریافت موجودی.")


# ---------------- خرید گیفت ----------------
@router.callback_query(F.data.startswith("gift:"))
async def cb_select_gift(call: CallbackQuery, state: FSMContext):
    key = call.data.split(":", 1)[1]
    gift = GIFTS.get(key)
    if not gift:
        await call.answer("گیفت نامعتبر است.", show_alert=True)
        return
    user = await db.get_user(call.from_user.id)
    if not user or not user["phone"]:
        await call.answer("ابتدا شماره موبایل را تأیید کنید.", show_alert=True)
        return
    balance = await db.get_balance(call.from_user.id)
    if balance < gift["price"]:
        await call.answer(
            f"موجودی کافی نیست؛ موجودی شما {balance} استارز است.",
            show_alert=True
        )
        return
    await state.update_data(
        gift_key=key, gift_price=gift["price"], gift_name=gift["name"]
    )
    await state.set_state(BuyGift.waiting_recipient)
    text = (
        f"{gift['name']} با قیمت {gift['price']} استارز انتخاب شد.\n\n"
        "آیدی عددی یا یوزرنیم گیرنده را ارسال کنید:"
    )
    try:
        await call.message.edit_text(text, reply_markup=back_keyboard())
    except Exception:
        await call.message.answer(text, reply_markup=back_keyboard())
    await call.answer()


@router.message(BuyGift.waiting_recipient)
async def receive_recipient(message: Message, state: FSMContext, bot: Bot):
    if message.text in MENU_TEXTS:
        await state.clear()
        if message.text == BTN_BUY:
            await show_gifts(message, state)
        elif message.text == BTN_BALANCE:
            await show_balance(message, state)
        else:
            await show_invite_link(message, bot, state)
        return
    recipient = (message.text or "").strip()
    if not recipient or len(recipient) > 100:
        await message.answer("❌ لطفاً یک آیدی معتبر ارسال کنید.")
        return
    data = await state.get_data()
    if not all(data.get(k) for k in ("gift_key", "gift_name", "gift_price")):
        await state.clear()
        await message.answer("❌ اطلاعات گیفت پیدا نشد. دوباره شروع کنید.")
        return
    await state.update_data(recipient=recipient)
    await state.set_state(BuyGift.confirming)
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ تأیید نهایی خرید", callback_data="confirm_purchase")
    kb.button(text="🔙 بازگشت", callback_data="main_menu")
    kb.adjust(1)
    await message.answer(
        "📋 <b>پیش‌فاکتور</b>\n\n"
        f"گیفت: {data['gift_name']}\n"
        f"قیمت: {data['gift_price']} استارز\n"
        f"گیرنده: <code>{recipient}</code>\n\n"
        "خرید را تأیید می‌کنید؟",
        reply_markup=kb.as_markup()
    )


@router.callback_query(F.data == "confirm_purchase", BuyGift.confirming)
async def cb_confirm_purchase(call: CallbackQuery, state: FSMContext, bot: Bot):
    data = await state.get_data()
    gift_key = data.get("gift_key")
    price = data.get("gift_price")
    name = data.get("gift_name")
    recipient = data.get("recipient")
    if not all((gift_key, price, name, recipient)):
        await state.clear()
        await call.message.edit_text("❌ اطلاعات خرید ناقص است.")
        await call.answer()
        return
    order_id = uuid.uuid4().hex[:12].upper()
    try:
        success = await db.create_order(
            order_id, call.from_user.id, gift_key, price, recipient
        )
        if not success:
            await call.message.edit_text("❌ موجودی کافی نیست.")
            await state.clear()
            await call.answer()
            return
        user = await db.get_user(call.from_user.id)
        full_name = (
            user["full_name"] if user and user["full_name"]
            else call.from_user.full_name
        )
        admin_text = (
            "🛒 <b>سفارش جدید</b>\n\n"
            f"شناسه: <code>{order_id}</code>\n"
            f"خریدار: {full_name} (<code>{call.from_user.id}</code>)\n"
            f"گیرنده: <code>{recipient}</code>\n"
            f"گیفت: {name}\n"
            f"قیمت: {price} استارز\n"
            f"تاریخ: {datetime.now().strftime('%Y-%m-%d %H:%M')}"
        )
        try:
            await bot.send_message(
                ADMIN_ID, admin_text,
                reply_markup=order_admin_keyboard(order_id)
            )
        except Exception:
            logger.exception("ارسال سفارش به ادمین ناموفق بود")
        await call.message.edit_text(
            f"✅ سفارش ثبت شد.\nشناسه: <code>{order_id}</code>"
        )
        await state.clear()
        await call.answer()
    except Exception:
        logger.exception("خطا در ثبت سفارش")
        await call.answer("خطا در ثبت سفارش.", show_alert=True)


# ---------------- تأیید / رد سفارش ----------------
@router.callback_query(F.data.startswith("approve:"))
async def cb_approve_order(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        await call.answer("⛔ دسترسی ندارید", show_alert=True)
        return
    order_id = call.data.split(":", 1)[1]
    try:
        if not await db.approve_order(order_id):
            await call.answer("سفارش قبلاً پردازش شده است.", show_alert=True)
            return
        await call.message.edit_text(
            (call.message.text or "") +
            "\n\n✅ تأیید شد؛ پس از ارسال دستی، دکمه ثبت انجام را بزنید.",
            reply_markup=order_complete_keyboard(order_id)
        )
        await call.answer("سفارش تأیید شد.")
    except Exception:
        logger.exception("خطای تأیید سفارش")
        await call.answer("خطا در تأیید سفارش.", show_alert=True)


@router.callback_query(F.data.startswith("reject:"))
async def cb_reject_order(call: CallbackQuery, bot: Bot):
    if call.from_user.id != ADMIN_ID:
        await call.answer("⛔ دسترسی ندارید", show_alert=True)
        return
    order_id = call.data.split(":", 1)[1]
    try:
        if not await db.reject_order(order_id):
            await call.answer("سفارش قبلاً پردازش شده است.", show_alert=True)
            return
        order = await db.get_order(order_id)
        try:
            await bot.send_message(
                order["user_id"],
                f"❌ سفارش <code>{order_id}</code> رد شد.\n"
                f"{order['gift_price']} استارز به حساب شما برگشت."
            )
        except Exception:
            logger.exception("ارسال پیام رد سفارش ناموفق بود")
        await call.message.edit_text((call.message.text or "") + "\n\n❌ رد شد.")
        await call.answer("سفارش رد شد.")
    except Exception:
        logger.exception("خطای رد سفارش")
        await call.answer("خطا در رد سفارش.", show_alert=True)


# ---------------- تکمیل و انتشار ----------------
@router.callback_query(F.data.startswith("complete:"))
async def cb_complete_order(call: CallbackQuery, bot: Bot):
    if call.from_user.id != ADMIN_ID:
        await call.answer("⛔ دسترسی ندارید", show_alert=True)
        return
    order_id = call.data.split(":", 1)[1]
    try:
        order = await db.get_order(order_id)
        if not order or order["status"] != "approved" or order["published"]:
            await call.answer("سفارش آماده تکمیل نیست.", show_alert=True)
            return
        user = await db.get_user(order["user_id"])
        full_name = (
            user["full_name"] if user and user["full_name"]
            else str(order["user_id"])
        )
        publish_text = (
            "✅ <b>سفارش انجام شد</b>\n\n"
            f"شناسه: <code>{order_id}</code>\n"
            f"خریدار: {full_name}\n"
            f"گیفت: {order['gift_name']}\n"
            f"گیرنده: <code>{order['recipient']}</code>\n"
            f"تاریخ: {datetime.now().strftime('%Y-%m-%d %H:%M')}"
        )
        await bot.send_message(PUBLISH_CHANNEL, publish_text)
        if not await db.mark_order_completed(order_id):
            await call.answer("وضعیت سفارش تغییر کرده است.", show_alert=True)
            return
        try:
            await bot.send_message(
                order["user_id"],
                f"🎉 سفارش <code>{order_id}</code> انجام شد و در کانال منتشر شد."
            )
        except Exception:
            logger.exception("ارسال اعلان تکمیل سفارش ناموفق بود")
        await call.message.edit_text(
            (call.message.text or "") + "\n\n📦 انجام و منتشر شد."
        )
        await call.answer("سفارش تکمیل شد.")
    except Exception:
        logger.exception("خطای تکمیل سفارش")
        await call.answer("خطا در تکمیل یا انتشار سفارش.", show_alert=True)


# ---------------- اجرای ربات ----------------
async def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN تنظیم نشده است")
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL تنظیم نشده است")

    await db.connect()
    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML)
    )
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    try:
        me = await bot.get_me()
        logger.info("ربات فعال شد: @%s", me.username)
        logger.info("ADMIN_ID=%s", ADMIN_ID)
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot)
    finally:
        await db.close()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("ربات متوقف شد")
    except Exception:
        logger.exception("خطای مهلک ربات")
        sys.exit(1)
