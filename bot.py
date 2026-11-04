import os
import re
import asyncio
import logging
import asyncpg

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message,
    KeyboardButton,
    ReplyKeyboardMarkup,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv

# ---------------- CONFIG ----------------

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

OWNER_ID = 8935601841
ORDER_CHANNEL = "@BET_1XZX"

CHANNELS = [
    "@BET_1XZX",
    "@GAP_BAZIN1",
]

GIFTS = {
    "🧸 Teddy": 15,
    "❤️ Heart": 15,
    "🌹 Flower": 25,
    "🎁 Gift": 25,
    "🚀 Rocket": 50,
    "🎂 Cake": 50,
    "💎 Diamond": 100,
}

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

pool = None

# نگهداری گیفت انتخاب‌شده تا دریافت آیدی گیرنده
pending_gifts = {}


# ---------------- DATABASE ----------------

async def init_db():
    global pool

    pool = await asyncpg.create_pool(DATABASE_URL)

    async with pool.acquire() as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                phone TEXT,
                balance INTEGER NOT NULL DEFAULT 0,
                referrer BIGINT
            );

            CREATE TABLE IF NOT EXISTS referrals (
                invited BIGINT PRIMARY KEY,
                referrer BIGINT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS orders (
                id BIGSERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                gift TEXT NOT NULL,
                cost INTEGER NOT NULL,
                recipient TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TIMESTAMPTZ DEFAULT NOW(),
                channel_message_id BIGINT
            );
        """)


# ---------------- KEYBOARDS ----------------

def phone_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(
                    text="📱 تأیید شماره موبایل",
                    request_contact=True
                )
            ]
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def main_menu():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🔗 لینک دعوت")],
            [KeyboardButton(text="🎁 خرید گیفت")],
            [KeyboardButton(text="⭐ موجودی استارز")],
        ],
        resize_keyboard=True,
    )


# ---------------- PHONE VALIDATION ----------------

def normalize_phone(phone):
    phone = phone.strip()
    phone = re.sub(r"[\s\-()]", "", phone)

    if phone.startswith("0098"):
        phone = "+98" + phone[4:]
    elif phone.startswith("98"):
        phone = "+" + phone
    elif phone.startswith("09"):
        phone = "+98" + phone[1:]

    if re.fullmatch(r"\+989\d{9}", phone):
        return phone

    return None


# ---------------- CHANNEL MEMBERSHIP ----------------

async def check_channels(user_id):
    for channel in CHANNELS:
        try:
            member = await bot.get_chat_member(
                chat_id=channel,
                user_id=user_id,
            )

            if member.status not in ("creator", "administrator", "member"):
                return False

        except Exception:
            logging.exception("Channel membership check failed: %s", channel)
            return False

    return True


async def show_channel_message(message):
    await message.answer(
        "برای استفاده از ربات ابتدا عضو هر دو کانال شو:\n\n"
        "1. https://t.me/BET_1XZX\n"
        "2. https://t.me/GAP_BAZIN1\n\n"
        "بعد از عضویت دوباره دستور /start رو بفرست."
    )


# ---------------- REFERRAL ----------------

async def reward_referral(user_id):
    async with pool.acquire() as db:
        async with db.transaction():

            user = await db.fetchrow(
                """
                SELECT phone, referrer
                FROM users
                WHERE user_id=$1
                FOR UPDATE
                """,
                user_id,
            )

            if not user or not user["phone"] or not user["referrer"]:
                return

            referrer = user["referrer"]

            if referrer == user_id:
                return

            inserted = await db.fetchval(
                """
                INSERT INTO referrals(invited, referrer)
                VALUES($1, $2)
                ON CONFLICT(invited) DO NOTHING
                RETURNING invited
                """,
                user_id,
                referrer,
            )

            if inserted:
                await db.execute(
                    """
                    UPDATE users
                    SET balance = balance + 2
                    WHERE user_id=$1
                    """,
                    referrer,
                )

                try:
                    await bot.send_message(
                        referrer,
                        "🎉 یک دعوت موفق داشتی!\n"
                        "⭐ ۲ استارز به موجودی تو اضافه شد."
                    )
                except Exception:
                    pass


# ---------------- ACCESS CHECK ----------------

async def ensure_access(message):
    user_id = message.from_user.id

    async with pool.acquire() as db:
        user = await db.fetchrow(
            "SELECT phone FROM users WHERE user_id=$1",
            user_id,
        )

    if not user or not user["phone"]:
        await message.answer(
            "برای شروع، شماره موبایلت رو با دکمه زیر تأیید کن.",
            reply_markup=phone_keyboard(),
        )
        return False

    if not await check_channels(user_id):
        await show_channel_message(message)
        return False

    await reward_referral(user_id)
    return True


# ---------------- START ----------------

@dp.message(CommandStart())
async def start(message: Message):
    parts = message.text.split()
    referrer = None

    if len(parts) > 1:
        try:
            possible_referrer = int(parts[1])

            if possible_referrer != message.from_user.id:
                referrer = possible_referrer

        except ValueError:
            pass

    async with pool.acquire() as db:
        await db.execute(
            """
            INSERT INTO users(user_id, referrer)
            VALUES($1, $2)
            ON CONFLICT(user_id) DO NOTHING
            """,
            message.from_user.id,
            referrer,
        )

        user = await db.fetchrow(
            "SELECT phone FROM users WHERE user_id=$1",
            message.from_user.id,
        )

    if not user["phone"]:
        await message.answer(
            "سلام ❤️\n\n"
            "برای استفاده از ربات، شماره موبایل خودت رو تأیید کن.\n"
            "روی دکمه زیر بزن:",
            reply_markup=phone_keyboard(),
        )
        return

    if not await check_channels(message.from_user.id):
        await show_channel_message(message)
        return

    await reward_referral(message.from_user.id)

    await message.answer(
        "🎉 خوش اومدی!\nاز منوی زیر استفاده کن.",
        reply_markup=main_menu(),
    )


# ---------------- CONTACT VERIFICATION ----------------

@dp.message(F.contact)
async def get_contact(message: Message):

    contact = message.contact

    if not contact or contact.user_id != message.from_user.id:
        await message.answer(
            "❌ لطفاً شماره موبایل خودت رو با دکمه ارسال کن.",
            reply_markup=phone_keyboard(),
        )
        return

    phone = normalize_phone(contact.phone_number)

    if not phone:
        await message.answer(
            "❌ فقط شماره موبایل ایران با پیش‌شماره +98 پذیرفته می‌شود.",
            reply_markup=phone_keyboard(),
        )
        return

    async with pool.acquire() as db:
        await db.execute(
            """
            UPDATE users
            SET phone=$1
            WHERE user_id=$2
            """,
            phone,
            message.from_user.id,
        )

    if not await check_channels(message.from_user.id):
        await message.answer(
            "✅ شماره موبایل تأیید شد.\n\n"
            "حالا عضو دو کانال زیر شو و دوباره /start رو بزن:\n\n"
            "https://t.me/BET_1XZX\n"
            "https://t.me/GAP_BAZIN1",
            reply_markup=main_menu(),
        )
        return

    await reward_referral(message.from_user.id)

    await message.answer(
        "✅ شماره موبایل با موفقیت تأیید شد!\n"
        "🎉 خوش اومدی.",
        reply_markup=main_menu(),
    )


# ---------------- REFERRAL LINK ----------------

@dp.message(F.text == "🔗 لینک دعوت")
async def referral_link(message: Message):

    if not await ensure_access(message):
        return

    me = await bot.get_me()

    link = f"https://t.me/{me.username}?start={message.from_user.id}"

    await message.answer(
        "🔗 لینک دعوت اختصاصی تو:\n\n"
        f"{link}\n\n"
        "به ازای هر دعوت موفق، ۲ استارز دریافت می‌کنی."
    )


# ---------------- BALANCE ----------------

@dp.message(F.text == "⭐ موجودی استارز")
async def balance(message: Message):

    if not await ensure_access(message):
        return

    async with pool.acquire() as db:
        amount = await db.fetchval(
            "SELECT balance FROM users WHERE user_id=$1",
            message.from_user.id,
        )

    await message.answer(
        f"⭐ موجودی استارز شما: {amount} "
    )


# ---------------- GIFT MENU ----------------

@dp.message(F.text == "🎁 خرید گیفت")
async def gift_menu(message: Message):

    if not await ensure_access(message):
        return

    builder = InlineKeyboardBuilder()

    for gift, cost in GIFTS.items():
        builder.button(
            text=f"{gift} | {cost} ⭐",
            callback_data=f"gift:{gift}",
        )

    builder.adjust(2)

    await message.answer(
        "🎁 گیفت موردنظرت رو انتخاب کن:",
        reply_markup=builder.as_markup(),
    )


# ---------------- SELECT GIFT ----------------

@dp.callback_query(F.data.startswith("gift:"))
async def select_gift(callback: CallbackQuery):

    user_id = callback.from_user.id

    async with pool.acquire() as db:
        user = await db.fetchrow(
            "SELECT phone FROM users WHERE user_id=$1",
            user_id,
        )

    if not user or not user["phone"]:
        await callback.answer(
            "ابتدا شماره موبایلت رو تأیید کن.",
            show_alert=True,
        )
        return

    if not await check_channels(user_id):
        await callback.answer(
            "ابتدا عضو کانال‌های موردنیاز شو.",
            show_alert=True,
        )
        return

    gift = callback.data.split(":", 1)[1]

    if gift not in GIFTS:
        await callback.answer("گیفت نامعتبر است.", show_alert=True)
        return

    pending_gifts[user_id] = gift

    await callback.message.answer(
        f"🎁 گیفت انتخابی: {gift}\n\n"
        "حالا آیدی گیرنده رو بفرست.\n"
        "مثال: @username یا شناسه عددی تلگرام"
    )

    await callback.answer()


# ---------------- CREATE ORDER ----------------

@dp.message(F.text)
async def recipient_handler(message: Message):

    user_id = message.from_user.id

    if user_id not in pending_gifts:
        return

    if not await ensure_access(message):
        return

    recipient = message.text.strip()

    if not (
        re.fullmatch(r"@[A-Za-z0-9_]{5,32}", recipient)
        or recipient.isdigit()
    ):
        await message.answer(
            "❌ آیدی گیرنده معتبر نیست.\n"
            "مثال: @username یا شناسه عددی تلگرام"
        )
        return

    gift = pending_gifts[user_id]
    cost = GIFTS[gift]

    try:
        async with pool.acquire() as db:
            async with db.transaction():

                balance = await db.fetchval(
                    """
                    SELECT balance
                    FROM users
                    WHERE user_id=$1
                    FOR UPDATE
                    """,
                    user_id,
                )

                if balance is None or balance < cost:
                    await message.answer(
                        "❌ موجودی شما برای این گیفت کافی نیست."
                    )
                    return

                await db.execute(
                    """
                    UPDATE users
                    SET balance=balance-$1
                    WHERE user_id=$2
                    """,
                    cost,
                    user_id,
                )

                order_id = await db.fetchval(
                    """
                    INSERT INTO orders(
                        user_id, gift, cost, recipient
                    )
                    VALUES($1, $2, $3, $4)
                    RETURNING id
                    """,
                    user_id,
                    gift,
                    cost,
                    recipient,
                )

        pending_gifts.pop(user_id, None)

    except Exception:
        logging.exception("Order creation failed")
        await message.answer("❌ خطا در ثبت سفارش. دوباره تلاش کن.")
        return

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ تأیید انجام سفارش",
                    callback_data=f"approve:{order_id}",
                ),
                InlineKeyboardButton(
                    text="❌ رد سفارش",
                    callback_data=f"reject:{order_id}",
                ),
            ]
        ]
    )

    order_text = (
        f"🛍 سفارش جدید\n\n"
        f"🔢 کد سفارش: {order_id}\n"
        f"🎁 گیفت: {gift}\n"
        f"⭐ هزینه: {cost}\n"
        f"👤 گیرنده: {recipient}\n"
        f"🆔 شناسه خریدار: {user_id}\n\n"
        f"📌 وضعیت: در انتظار تأیید مالک"
    )

    try:
        await bot.send_message(
            OWNER_ID,
            order_text,
            reply_markup=keyboard,
        )
    except Exception:
        logging.exception("Could not notify owner")

    await message.answer(
        f"✅ سفارش شما ثبت شد.\n"
        f"🔢 کد سفارش: {order_id}\n\n"
        "بعد از تأیید مالک، سفارش انجام‌شده اعلام می‌شود.",
        reply_markup=main_menu(),
    )


# ---------------- OWNER APPROVE / REJECT ----------------

@dp.callback_query(
    F.data.startswith(("approve:", "reject:"))
)
async def order_decision(callback: CallbackQuery):

    if callback.from_user.id != OWNER_ID:
        await callback.answer(
            "⛔ فقط مالک ربات اجازه این کار را دارد.",
            show_alert=True,
        )
        return

    action, order_id_text = callback.data.split(":", 1)

    try:
        order_id = int(order_id_text)
    except ValueError:
        await callback.answer("کد سفارش نامعتبر است.")
        return

    async with pool.acquire() as db:
        async with db.transaction():

            order = await db.fetchrow(
                """
                SELECT *
                FROM orders
                WHERE id=$1
                FOR UPDATE
                """,
                order_id,
            )

            if not order:
                await callback.answer("سفارش پیدا نشد.")
                return

            if order["status"] != "pending":
                await callback.answer(
                    "این سفارش قبلاً تعیین تکلیف شده است.",
                    show_alert=True,
                )
                return

            if action == "approve":
                status = "approved"
            else:
                status = "rejected"

                await db.execute(
                    """
                    UPDATE users
                    SET balance=balance+$1
                    WHERE user_id=$2
                    """,
                    order["cost"],
                    order["user_id"],
                )

            await db.execute(
                """
                UPDATE orders
                SET status=$1
                WHERE id=$2
                """,
                status,
                order_id,
            )

    if action == "approve":

        result = "✅ وضعیت: انجام شده"

        # پیام سفارش فقط پس از تأیید مالک در کانال منتشر می‌شود.
        channel_text = (
            f"🎁 سفارش گیفت انجام شد\n\n"
            f"🔢 کد سفارش: {order_id}\n"
            f"🎁 گیفت: {order['gift']}\n"
            f"👤 گیرنده: {order['recipient']}\n"
            f"⭐ هزینه: {order['cost']} استارز\n\n"
            f"✅ وضعیت: انجام شده"
        )

        try:
            sent = await bot.send_message(
                ORDER_CHANNEL,
                channel_text,
            )

            async with pool.acquire() as db:
                await db.execute(
                    """
                    UPDATE orders
                    SET channel_message_id=$1
                    WHERE id=$2
                    """,
                    sent.message_id,
                    order_id,
                )

        except Exception:
            logging.exception("Could not send approved order to channel")

        try:
            await bot.send_message(
                order["user_id"],
                f"✅ سفارش شماره {order_id} تأیید شد و انجام شده است."
            )
        except Exception:
            pass

    else:

        result = "❌ سفارش رد شد؛ اعتبار به کاربر برگشت داده شد."

        try:
            await bot.send_message(
                order["user_id"],
                f"❌ سفارش شماره {order_id} رد شد.\n"
                f"⭐ {order['cost']} استارز به موجودی شما برگشت داده شد."
            )
        except Exception:
            pass

    await callback.message.edit_text(
        callback.message.text + "\n\n" + result,
        reply_markup=None,
    )

    await callback.answer("تصمیم ثبت شد.")


# ---------------- RUN BOT ----------------

async def main():

    if not BOT_TOKEN or not DATABASE_URL:
        raise RuntimeError(
            "BOT_TOKEN و DATABASE_URL باید در فایل .env تنظیم شوند."
        )

    await init_db()

    logging.info("Bot is running...")

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
