
import asyncio
import logging
import os
import re
from contextlib import asynccontextmanager

import asyncpg
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import (
    Message,
    CallbackQuery,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

OWNER_ID = 8935601841

CHANNELS = [
    ("@BET_1XZX", "https://t.me/BET_1XZX"),
    ("@GAP_BAZIN1", "https://t.me/GAP_BAZIN1"),
]

ORDER_CHANNEL = "@BET_1XZX"

GIFTS = {
    "teddy": ("🧸 خرس", 15),
    "heart": ("❤️ قلب", 15),
    "flower": ("🌹 گل", 25),
    "gift": ("🎁 هدیه", 25),
    "rocket": ("🚀 موشک", 50),
    "cake": ("🎂 کیک", 50),
    "diamond": ("💎 الماس", 100),
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

logger = logging.getLogger(__name__)

bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)

dp = Dispatcher()

db_pool = None
bot_username = None

# نگهداری انتخاب موقت هدیه تا دریافت گیرنده
waiting_for_recipient = {}


def phone_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(
                    text="📱 تأیید شماره موبایل",
                    request_contact=True,
                )
            ]
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def main_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🔗 لینک دعوت")],
            [KeyboardButton(text="🎁 خرید گیفت")],
            [KeyboardButton(text="⭐ موجودی استارز")],
        ],
        resize_keyboard=True,
    )


def normalize_phone(phone):
    phone = re.sub(r"[^\d+]", "", phone or "")

    if phone.startswith("0098"):
        phone = "+98" + phone[4:]
    elif phone.startswith("98"):
        phone = "+" + phone
    elif phone.startswith("09"):
        phone = "+98" + phone[1:]

    if re.fullmatch(r"\+989\d{9}", phone):
        return phone

    return None


async def init_database():
    global db_pool

    db_pool = await asyncpg.create_pool(
        DATABASE_URL,
        min_size=1,
        max_size=5,
        ssl="require",
    )

    async with db_pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                username TEXT,
                phone TEXT,
                credits INTEGER NOT NULL DEFAULT 0,
                referred_by BIGINT,
                referral_paid BOOLEAN NOT NULL DEFAULT FALSE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS orders (
                id BIGSERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                gift_code TEXT NOT NULL,
                gift_name TEXT NOT NULL,
                price INTEGER NOT NULL,
                recipient TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                completed_at TIMESTAMPTZ
            );

            CREATE INDEX IF NOT EXISTS orders_user_id_idx
            ON orders(user_id);
        """)


async def save_user(user):
    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO users (user_id, username)
            VALUES ($1, $2)
            ON CONFLICT (user_id)
            DO UPDATE SET username = EXCLUDED.username
            """,
            user.id,
            user.username,
        )


async def get_user(user_id):
    async with db_pool.acquire() as conn:
        return await conn.fetchrow(
            "SELECT * FROM users WHERE user_id = $1",
            user_id,
        )


async def is_member(user_id):
    for channel, _ in CHANNELS:
        try:
            member = await bot.get_chat_member(channel, user_id)

            if member.status in ("left", "kicked"):
                return False

        except (TelegramBadRequest, TelegramForbiddenError):
            logger.exception("Cannot check membership in %s", channel)
            return False

    return True


async def show_join_message(message):
    buttons = []

    for index, (_, url) in enumerate(CHANNELS, start=1):
        buttons.append([
            InlineKeyboardButton(
                text=f"عضویت در کانال {index}",
                url=url,
            )
        ])

    buttons.append([
        InlineKeyboardButton(
            text="✅ بررسی عضویت",
            callback_data="check_membership",
        )
    ])

    await message.answer(
        "برای استفاده از ربات، ابتدا در هر دو کانال زیر عضو شو و بعد روی بررسی عضویت بزن.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )


async def reward_referral(user_id):
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            user = await conn.fetchrow(
                """
                SELECT referred_by, referral_paid
                FROM users
                WHERE user_id = $1
                FOR UPDATE
                """,
                user_id,
            )

            if not user:
                return

            inviter_id = user["referred_by"]

            if not inviter_id or user["referral_paid"]:
                return

            await conn.execute(
                """
                UPDATE users
                SET credits = credits + 2
                WHERE user_id = $1
                """,
                inviter_id,
            )

            await conn.execute(
                """
                UPDATE users
                SET referral_paid = TRUE
                WHERE user_id = $1
                """,
                user_id,
            )

    try:
        await bot.send_message(
            inviter_id,
            "🎉 دوستت شماره موبایلش رو تأیید کرد و عضو هر دو کانال شد.\n"
            "⭐ ۲ اعتبار به حسابت اضافه شد.",
        )
    except Exception:
        logger.exception("Could not notify inviter")


async def check_access(user_id):
    user = await get_user(user_id)

    if not user or not user["phone"]:
        return "phone"

    if not await is_member(user_id):
        return "channels"

    await reward_referral(user_id)
    return "ok"


async def send_main_menu(message):
    await message.answer(
        "سلام! 👋\nبه ربات خرید گیفت خوش اومدی.",
        reply_markup=main_keyboard(),
    )


@dp.message(CommandStart())
async def start_handler(message: Message):
    await save_user(message.from_user)

    parts = (message.text or "").split(maxsplit=1)

    if len(parts) == 2 and parts[1].startswith("ref_"):
        try:
            inviter_id = int(parts[1][4:])

            if inviter_id != message.from_user.id:
                async with db_pool.acquire() as conn:
                    await conn.execute(
                        """
                        UPDATE users
                        SET referred_by = $1
                        WHERE user_id = $2
                          AND phone IS NULL
                          AND referred_by IS NULL
                        """,
                        inviter_id,
                        message.from_user.id,
                    )
        except ValueError:
            pass

    user = await get_user(message.from_user.id)

    if not user or not user["phone"]:
        await message.answer(
            "برای شروع، شماره موبایل خودت رو با دکمه زیر تأیید کن.",
            reply_markup=phone_keyboard(),
        )
        return

    access = await check_access(message.from_user.id)

    if access == "channels":
        await show_join_message(message)
        return

    await send_main_menu(message)


@dp.message(F.contact)
async def contact_handler(message: Message):
    contact = message.contact

    if contact.user_id != message.from_user.id:
        await message.answer(
            "❌ فقط شماره موبایل خودت قابل تأییده.",
            reply_markup=phone_keyboard(),
        )
        return

    phone = normalize_phone(contact.phone_number)

    if not phone:
        await message.answer(
            "❌ شماره معتبر نیست. فقط شماره موبایل ایران با پیش‌شماره +98 پذیرفته می‌شه.",
            reply_markup=phone_keyboard(),
        )
        return

    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE users
            SET phone = $1,
                username = $2
            WHERE user_id = $3
            """,
            phone,
            message.from_user.username,
            message.from_user.id,
        )

    if not await is_member(message.from_user.id):
        await show_join_message(message)
        return

    await reward_referral(message.from_user.id)
    await send_main_menu(message)


@dp.callback_query(F.data == "check_membership")
async def membership_callback(callback: CallbackQuery):
    access = await check_access(callback.from_user.id)

    if access == "phone":
        await callback.message.answer(
            "اول شماره موبایلت رو تأیید کن.",
            reply_markup=phone_keyboard(),
        )
        await callback.answer()
        return

    if access == "channels":
        await callback.answer(
            "هنوز عضو هر دو کانال نشدی.",
            show_alert=True,
        )
        return

    await callback.answer("عضویت تأیید شد.")

    await callback.message.answer(
        "✅ عضویتت تأیید شد.",
        reply_markup=main_keyboard(),
    )


async def require_access(message):
    access = await check_access(message.from_user.id)

    if access == "phone":
        await message.answer(
            "اول شماره موبایلت رو تأیید کن.",
            reply_markup=phone_keyboard(),
        )
        return False

    if access == "channels":
        await show_join_message(message)
        return False

    return True


@dp.message(F.text == "🔗 لینک دعوت")
async def referral_handler(message: Message):
    if not await require_access(message):
        return

    link = f"https://t.me/{bot_username}?start=ref_{message.from_user.id}"

    await message.answer(
        "🔗 لینک اختصاصی دعوت تو:\n"
        f"<code>{link}</code>\n\n"
        "با دعوت هر نفر، بعد از تأیید شماره و عضویت در هر دو کانال، "
        "۲ اعتبار به حسابت اضافه می‌شه."
    )


@dp.message(F.text == "⭐ موجودی استارز")
async def balance_handler(message: Message):
    if not await require_access(message):
        return

    user = await get_user(message.from_user.id)

    await message.answer(
        f"⭐ موجودی اعتبار شما: <b>{user['credits']}</b>"
    )


@dp.message(F.text == "🎁 خرید گیفت")
async def gifts_handler(message: Message):
    if not await require_access(message):
        return

    buttons = []

    for code, (name, price) in GIFTS.items():
        buttons.append([
            InlineKeyboardButton(
                text=f"{name} — {price} اعتبار",
                callback_data=f"gift:{code}",
            )
        ])

    await message.answer(
        "🎁 گیفت مورد نظرت رو انتخاب کن:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )


@dp.callback_query(F.data.startswith("gift:"))
async def select_gift(callback: CallbackQuery):
    access = await check_access(callback.from_user.id)

    if access != "ok":
        await callback.answer(
            "ابتدا شماره و عضویت کانال‌ها رو تأیید کن.",
            show_alert=True,
        )
        return

    code = callback.data.split(":", 1)[1]

    if code not in GIFTS:
        await callback.answer("گیفت نامعتبره.", show_alert=True)
        return

    waiting_for_recipient[callback.from_user.id] = code

    gift_name, price = GIFTS[code]

    await callback.message.answer(
        f"انتخاب شما: {gift_name}\n"
        f"قیمت: {price} اعتبار\n\n"
        "حالا آیدی گیرنده رو بفرست.\n"
        "مثال: <code>@username</code> یا شناسه عددی تلگرام"
    )

    await callback.answer()


def valid_recipient(value):
    value = value.strip()

    if re.fullmatch(r"@[A-Za-z0-9_]{5,32}", value):
        return value

    if re.fullmatch(r"[0-9]{5,20}", value):
        return value

    return None


@dp.message(F.text)
async def recipient_handler(message: Message):
    user_id = message.from_user.id

    if user_id not in waiting_for_recipient:
        return

    if not await require_access(message):
        return

    recipient = valid_recipient(message.text)

    if not recipient:
        await message.answer(
            "❌ آیدی معتبر نیست.\n"
            "لطفاً مثل <code>@username</code> یا شناسه عددی بفرست."
        )
        return

    code = waiting_for_recipient[user_id]
    gift_name, price = GIFTS[code]

    async with db_pool.acquire() as conn:
        async with conn.transaction():
            user = await conn.fetchrow(
                """
                SELECT credits
                FROM users
                WHERE user_id = $1
                FOR UPDATE
                """,
                user_id,
            )

            if not user or user["credits"] < price:
                enough = False
            else:
                enough = True

                await conn.execute(
                    """
                    UPDATE users
                    SET credits = credits - $1
                    WHERE user_id = $2
                    """,
                    price,
                    user_id,
                )

                order_id = await conn.fetchval(
                    """
                    INSERT INTO orders
                    (user_id, gift_code, gift_name, price, recipient)
                    VALUES ($1, $2, $3, $4, $5)
                    RETURNING id
                    """,
                    user_id,
                    code,
                    gift_name,
                    price,
                    recipient,
                )

    if not enough:
        await message.answer(
            "❌ اعتبارت کافی نیست. ابتدا از بخش لینک دعوت اعتبار جمع کن."
        )
        waiting_for_recipient.pop(user_id, None)
        return

    waiting_for_recipient.pop(user_id, None)

    await message.answer(
        f"✅ سفارش شماره <b>{order_id}</b> ثبت شد.\n"
        f"🎁 گیفت: {gift_name}\n"
        f"👤 گیرنده: <code>{recipient}</code>\n"
        f"⭐ هزینه: {price} اعتبار\n\n"
        "سفارش برای بررسی به مدیریت ارسال شد."
    )

    owner_keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ تأیید سفارش",
                    callback_data=f"approve:{order_id}",
                ),
                InlineKeyboardButton(
                    text="❌ رد سفارش",
                    callback_data=f"reject:{order_id}",
                ),
            ]
        ]
    )

    try:
        await bot.send_message(
            OWNER_ID,
            f"🛒 <b>سفارش جدید</b>\n\n"
            f"شماره سفارش: <b>{order_id}</b>\n"
            f"شناسه خریدار: <code>{user_id}</code>\n"
            f"نام کاربری خریدار: @{message.from_user.username or 'ندارد'}\n"
            f"گیفت: {gift_name}\n"
            f"گیرنده: <code>{recipient}</code>\n"
            f"هزینه: {price} اعتبار",
            reply_markup=owner_keyboard,
        )
    except Exception:
        logger.exception("Could not send order to owner")


@dp.callback_query(F.data.startswith("approve:"))
async def approve_order(callback: CallbackQuery):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("اجازه دسترسی نداری.", show_alert=True)
        return

    order_id = int(callback.data.split(":")[1])

    async with db_pool.acquire() as conn:
        result = await conn.execute(
            """
            UPDATE orders
            SET status = 'approved'
            WHERE id = $1 AND status = 'pending'
            """,
            order_id,
        )

        order = await conn.fetchrow(
            "SELECT * FROM orders WHERE id = $1",
            order_id,
        )

    if result == "UPDATE 0":
        await callback.answer("این سفارش قبلاً تعیین تکلیف شده.", show_alert=True)
        return

    await callback.message.edit_text(
        callback.message.text + "\n\n✅ سفارش تأیید شد.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="📦 ثبت انجام سفارش",
                        callback_data=f"complete:{order_id}",
                    )
                ]
            ]
        ),
    )

    try:
        await bot.send_message(
            order["user_id"],
            f"✅ سفارش شماره {order_id} تأیید شد و در حال انجامه.",
        )
    except Exception:
        logger.exception("Could not notify customer")

    await callback.answer("سفارش تأیید شد.")


@dp.callback_query(F.data.startswith("reject:"))
async def reject_order(callback: CallbackQuery):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("اجازه دسترسی نداری.", show_alert=True)
        return

    order_id = int(callback.data.split(":")[1])

    async with db_pool.acquire() as conn:
        async with conn.transaction():
            order = await conn.fetchrow(
                """
                SELECT * FROM orders
                WHERE id = $1
                FOR UPDATE
                """,
                order_id,
            )

            if not order or order["status"] != "pending":
                await callback.answer(
                    "این سفارش قبلاً تعیین تکلیف شده.",
                    show_alert=True,
                )
                return

            await conn.execute(
                """
                UPDATE orders
                SET status = 'rejected'
                WHERE id = $1
                """,
                order_id,
            )

            await conn.execute(
                """
                UPDATE users
                SET credits = credits + $1
                WHERE user_id = $2
                """,
                order["price"],
                order["user_id"],
            )

    await callback.message.edit_text(
        callback.message.text + "\n\n❌ سفارش رد شد و اعتبار برگشت داده شد."
    )

    try:
        await bot.send_message(
            order["user_id"],
            f"❌ سفارش شماره {order_id} رد شد.\n"
            f"⭐ {order['price']} اعتبار به حسابت برگشت.",
        )
    except Exception:
        logger.exception("Could not notify customer")

    await callback.answer("سفارش رد شد و اعتبار برگشت داده شد.")


@dp.callback_query(F.data.startswith("complete:"))
async def complete_order(callback: CallbackQuery):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("اجازه دسترسی نداری.", show_alert=True)
        return

    order_id = int(callback.data.split(":")[1])

    async with db_pool.acquire() as conn:
        async with conn.transaction():
            order = await conn.fetchrow(
                """
                SELECT * FROM orders
                WHERE id = $1
                FOR UPDATE
                """,
                order_id,
            )

            if not order or order["status"] != "approved":
                await callback.answer(
                    "این سفارش قابل تکمیل نیست.",
                    show_alert=True,
                )
                return

            await conn.execute(
                """
                UPDATE orders
                SET status = 'completed',
                    completed_at = NOW()
                WHERE id = $1
                """,
                order_id,
            )

    try:
        await bot.send_message(
            ORDER_CHANNEL,
            f"🎉 <b>سفارش انجام شد</b>\n\n"
            f"شماره سفارش: <b>{order_id}</b>\n"
            f"🎁 گیفت: {order['gift_name']}\n"
            f"👤 گیرنده: <code>{order['recipient']}</code>\n"
            f"📌 وضعیت: <b>انجام شده</b>",
        )
    except Exception:
        logger.exception("Could not publish completed order")

        await callback.answer(
            "سفارش تکمیل شد، اما ارسال به کانال ناموفق بود. دسترسی ربات به کانال را بررسی کن.",
            show_alert=True,
        )
        return

    await callback.message.edit_text(
        callback.message.text + "\n\n🎉 سفارش انجام شد و در کانال منتشر شد."
    )

    try:
        await bot.send_message(
            order["user_id"],
            f"🎉 سفارش شماره {order_id} انجام شد.",
        )
    except Exception:
        logger.exception("Could not notify customer")

    await callback.answer("سفارش تکمیل و در کانال منتشر شد.")


async def main():
    if not BOT_TOKEN:
        raise RuntimeError("Secret با نام BOT_TOKEN تنظیم نشده است.")

    if not DATABASE_URL:
        raise RuntimeError("Secret با نام DATABASE_URL تنظیم نشده است.")

    global bot_username

    await init_database()

    me = await bot.get_me()
    bot_username = me.username

    logger.info("Bot started: @%s", bot_username)

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
