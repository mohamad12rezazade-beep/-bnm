
import asyncio
import os
import re
import logging
import asyncpg

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import (
    Message,
    CallbackQuery,
    KeyboardButton,
    ReplyKeyboardMarkup,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

logging.basicConfig(level=logging.INFO)

TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
OWNER_ID = 8935601841

CHANNELS = [
    ("@BET_1XZX", "https://t.me/BET_1XZX"),
    ("@GAP_BAZIN1", "https://t.me/GAP_BAZIN1"),
]

GIFTS = {
    "teddy": ("🧸 Teddy", 15),
    "heart": ("❤️ Heart", 15),
    "flower": ("🌹 Flower", 25),
    "gift": ("🎁 Gift", 25),
    "rocket": ("🚀 Rocket", 50),
    "cake": ("🎂 Cake", 50),
    "diamond": ("💎 Diamond", 100),
}

if not TOKEN or not DATABASE_URL:
    raise RuntimeError("BOT_TOKEN یا DATABASE_URL تنظیم نشده است.")

bot = Bot(
    TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)
dp = Dispatcher()

db = None
bot_username = None
waiting = {}


def phone_button():
    return ReplyKeyboardMarkup(
        keyboard=[[
            KeyboardButton(
                text="📱 تأیید شماره موبایل",
                request_contact=True
            )
        ]],
        resize_keyboard=True
    )


def menu():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🔗 لینک دعوت")],
            [KeyboardButton(text="🎁 خرید گیفت")],
            [KeyboardButton(text="⭐ موجودی استارز")]
        ],
        resize_keyboard=True
    )


async def setup_database():
    global db

    db = await asyncpg.create_pool(
        DATABASE_URL,
        min_size=1,
        max_size=5,
        ssl="require"
    )

    async with db.acquire() as c:
        await c.execute("""
        CREATE TABLE IF NOT EXISTS users(
            user_id BIGINT PRIMARY KEY,
            username TEXT,
            phone TEXT,
            credits INTEGER DEFAULT 0,
            referred_by BIGINT,
            referral_paid BOOLEAN DEFAULT FALSE
        );

        CREATE TABLE IF NOT EXISTS orders(
            id BIGSERIAL PRIMARY KEY,
            user_id BIGINT,
            gift_name TEXT,
            price INTEGER,
            recipient TEXT,
            status TEXT DEFAULT 'pending',
            created_at TIMESTAMPTZ DEFAULT NOW()
        );
        """)


async def save_user(user):
    async with db.acquire() as c:
        await c.execute("""
        INSERT INTO users(user_id, username)
        VALUES($1,$2)
        ON CONFLICT(user_id)
        DO UPDATE SET username=$2
        """, user.id, user.username)


async def get_user(user_id):
    async with db.acquire() as c:
        return await c.fetchrow(
            "SELECT * FROM users WHERE user_id=$1",
            user_id
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


async def is_member(user_id):
    for channel, _ in CHANNELS:
        try:
            member = await bot.get_chat_member(channel, user_id)
            if member.status in ("left", "kicked"):
                return False
        except Exception:
            logging.exception("Membership check failed: %s", channel)
            return False

    return True


async def join_message(message):
    buttons = [
        [InlineKeyboardButton(
            text=f"عضویت در {channel}",
            url=url
        )]
        for channel, url in CHANNELS
    ]

    buttons.append([
        InlineKeyboardButton(
            text="✅ بررسی عضویت",
            callback_data="check"
        )
    ])

    await message.answer(
        "برای استفاده از ربات در هر دو کانال عضو شو:",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=buttons
        )
    )


async def pay_referral(user_id):
    async with db.acquire() as c:
        async with c.transaction():
            user = await c.fetchrow("""
            SELECT referred_by, referral_paid
            FROM users
            WHERE user_id=$1
            FOR UPDATE
            """, user_id)

            if not user or not user["referred_by"] or user["referral_paid"]:
                return

            inviter = user["referred_by"]

            await c.execute(
                "UPDATE users SET credits=credits+2 WHERE user_id=$1",
                inviter
            )

            await c.execute(
                "UPDATE users SET referral_paid=TRUE WHERE user_id=$1",
                user_id
            )

    try:
        await bot.send_message(
            inviter,
            "🎉 دوستت ثبت‌نام کرد. ۲ اعتبار به حسابت اضافه شد."
        )
    except Exception:
        logging.exception("Referral notification failed")


async def access(user_id):
    user = await get_user(user_id)

    if not user or not user["phone"]:
        return "phone"

    if not await is_member(user_id):
        return "channels"

    await pay_referral(user_id)
    return "ok"


async def require_access(message):
    result = await access(message.from_user.id)

    if result == "phone":
        await message.answer(
            "ابتدا شماره موبایل خودت رو تأیید کن.",
            reply_markup=phone_button()
        )
        return False

    if result == "channels":
        await join_message(message)
        return False

    return True


@dp.message(CommandStart())
async def start(message: Message):
    await save_user(message.from_user)

    args = (message.text or "").split()

    if len(args) > 1 and args[1].startswith("ref_"):
        try:
            inviter = int(args[1][4:])
            if inviter != message.from_user.id:
                async with db.acquire() as c:
                    await c.execute("""
                    UPDATE users
                    SET referred_by=$1
                    WHERE user_id=$2
                    AND phone IS NULL
                    AND referred_by IS NULL
                    """, inviter, message.from_user.id)
        except ValueError:
            pass

    user = await get_user(message.from_user.id)

    if not user["phone"]:
        await message.answer(
            "سلام 👋\nبرای شروع شماره موبایلت رو تأیید کن.",
            reply_markup=phone_button()
        )
        return

    if await access(message.from_user.id) == "channels":
        await join_message(message)
        return

    await message.answer(
        "به ربات خرید گیفت خوش آمدی 🎁",
        reply_markup=menu()
    )


@dp.message(F.contact)
async def contact_handler(message: Message):
    contact = message.contact

    if contact.user_id != message.from_user.id:
        await message.answer(
            "❌ فقط شماره خودت قابل تأییده.",
            reply_markup=phone_button()
        )
        return

    phone = normalize_phone(contact.phone_number)

    if not phone:
        await message.answer(
            "❌ شماره موبایل ایران معتبر نیست.",
            reply_markup=phone_button()
        )
        return

    async with db.acquire() as c:
        await c.execute("""
        UPDATE users SET phone=$1 WHERE user_id=$2
        """, phone, message.from_user.id)

    if not await is_member(message.from_user.id):
        await join_message(message)
        return

    await pay_referral(message.from_user.id)

    await message.answer(
        "✅ شماره تأیید شد.",
        reply_markup=menu()
    )


@dp.callback_query(F.data == "check")
async def check_membership(callback: CallbackQuery):
    result = await access(callback.from_user.id)

    if result == "phone":
        await callback.message.answer(
            "اول شماره موبایلت رو تأیید کن.",
            reply_markup=phone_button()
        )
        await callback.answer()
        return

    if result == "channels":
        await callback.answer(
            "هنوز عضو هر دو کانال نیستی.",
            show_alert=True
        )
        return

    await callback.answer("تأیید شد.")
    await callback.message.answer(
        "✅ عضویت تأیید شد.",
        reply_markup=menu()
    )


@dp.message(F.text == "🔗 لینک دعوت")
async def referral(message: Message):
    if not await require_access(message):
        return

    link = f"https://t.me/{bot_username}?start=ref_{message.from_user.id}"

    await message.answer(
        "لینک دعوت اختصاصی تو:\n"
        f"<code>{link}</code>\n\n"
        "با ثبت‌نام هر دوست و تکمیل عضویت، ۲ اعتبار می‌گیری."
    )


@dp.message(F.text == "⭐ موجودی استارز")
async def balance(message: Message):
    if not await require_access(message):
        return

    user = await get_user(message.from_user.id)

    await message.answer(
        f"⭐ موجودی شما: <b>{user['credits']}</b> اعتبار"
    )


@dp.message(F.text == "🎁 خرید گیفت")
async def gifts(message: Message):
    if not await require_access(message):
        return

    buttons = []

    for code, (name, price) in GIFTS.items():
        buttons.append([
            InlineKeyboardButton(
                text=f"{name} | {price} اعتبار",
                callback_data=f"gift:{code}"
            )
        ])

    await message.answer(
        "گیفت مورد نظر رو انتخاب کن:",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=buttons
        )
    )


@dp.callback_query(F.data.startswith("gift:"))
async def choose_gift(callback: CallbackQuery):
    if await access(callback.from_user.id) != "ok":
        await callback.answer(
            "ابتدا شماره و عضویت کانال‌ها رو تأیید کن.",
            show_alert=True
        )
        return

    code = callback.data.split(":")[1]

    if code not in GIFTS:
        await callback.answer("گیفت نامعتبر است.")
        return

    waiting[callback.from_user.id] = code

    name, price = GIFTS[code]

    await callback.message.answer(
        f"🎁 {name}\nقیمت: {price} اعتبار\n\n"
        "آیدی گیرنده رو بفرست:\n"
        "@username یا شناسه عددی"
    )

    await callback.answer()


@dp.message(F.text)
async def recipient(message: Message):
    user_id = message.from_user.id

    if user_id not in waiting:
        return

    if not await require_access(message):
        return

    target = message.text.strip()

    if not (
        re.fullmatch(r"@[A-Za-z0-9_]{5,32}", target)
        or re.fullmatch(r"\d{5,20}", target)
    ):
        await message.answer("آیدی گیرنده معتبر نیست.")
        return

    code = waiting[user_id]
    name, price = GIFTS[code]

    async with db.acquire() as c:
        async with c.transaction():
            user = await c.fetchrow(
                "SELECT credits FROM users WHERE user_id=$1 FOR UPDATE",
                user_id
            )

            if not user or user["credits"] < price:
                enough = False
                order_id = None
            else:
                enough = True

                await c.execute(
                    "UPDATE users SET credits=credits-$1 WHERE user_id=$2",
                    price, user_id
                )

                order_id = await c.fetchval("""
                INSERT INTO orders(user_id,gift_name,price,recipient)
                VALUES($1,$2,$3,$4)
                RETURNING id
                """, user_id, name, price, target)

    waiting.pop(user_id, None)

    if not enough:
        await message.answer("❌ اعتبار کافی نداری.")
        return

    await message.answer(
        f"✅ سفارش شماره {order_id} ثبت شد."
    )

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(
                text="✅ تأیید",
                callback_data=f"approve:{order_id}"
            ),
            InlineKeyboardButton(
                text="❌ رد",
                callback_data=f"reject:{order_id}"
            )
        ]]
    )

    try:
        await bot.send_message(
            OWNER_ID,
            f"سفارش جدید\n"
            f"شماره: {order_id}\n"
            f"کاربر: {user_id}\n"
            f"گیرنده: {target}\n"
            f"گیفت: {name}\n"
            f"قیمت: {price}",
            reply_markup=keyboard
        )
    except Exception:
        logging.exception("Owner notification failed")


@dp.callback_query(F.data.startswith("approve:"))
async def approve(callback: CallbackQuery):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("دسترسی غیرمجاز", show_alert=True)
        return

    order_id = int(callback.data.split(":")[1])

    async with db.acquire() as c:
        result = await c.execute("""
        UPDATE orders SET status='approved'
        WHERE id=$1 AND status='pending'
        """, order_id)

        order = await c.fetchrow(
            "SELECT * FROM orders WHERE id=$1", order_id
        )

    if result == "UPDATE 0":
        await callback.answer("سفارش قبلاً بررسی شده.")
        return

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(
                text="📦 ثبت انجام سفارش",
                callback_data=f"complete:{order_id}"
            )
        ]]
    )

    await callback.message.edit_reply_markup(reply_markup=keyboard)

    await bot.send_message(
        order["user_id"],
        f"✅ سفارش {order_id} تأیید شد."
    )

    await callback.answer("تأیید شد.")


@dp.callback_query(F.data.startswith("reject:"))
async def reject(callback: CallbackQuery):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("دسترسی غیرمجاز", show_alert=True)
        return

    order_id = int(callback.data.split(":")[1])

    async with db.acquire() as c:
        async with c.transaction():
            order = await c.fetchrow(
                "SELECT * FROM orders WHERE id=$1 FOR UPDATE",
                order_id
            )

            if not order or order["status"] != "pending":
                await callback.answer("قبلاً بررسی شده.")
                return

            await c.execute(
                "UPDATE orders SET status='rejected' WHERE id=$1",
                order_id
            )

            await c.execute(
                "UPDATE users SET credits=credits+$1 WHERE user_id=$2",
                order["price"], order["user_id"]
            )

    await callback.message.edit_text(
        callback.message.text + "\n\n❌ رد شد؛ اعتبار برگشت خورد."
    )

    await bot.send_message(
        order["user_id"],
        f"❌ سفارش {order_id} رد شد و اعتبار برگشت خورد."
    )

    await callback.answer("رد شد.")


@dp.callback_query(F.data.startswith("complete:"))
async def complete(callback: CallbackQuery):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("دسترسی غیرمجاز", show_alert=True)
        return

    order_id = int(callback.data.split(":")[1])

    async with db.acquire() as c:
        async with c.transaction():
            order = await c.fetchrow(
                "SELECT * FROM orders WHERE id=$1 FOR UPDATE",
                order_id
            )

            if not order or order["status"] != "approved":
                await callback.answer("سفارش قابل تکمیل نیست.")
                return

            await c.execute("""
            UPDATE orders
            SET status='completed'
            WHERE id=$1
            """, order_id)

    try:
        await bot.send_message(
            "@BET_1XZX",
            f"🎉 سفارش انجام شد\n"
            f"شماره: {order_id}\n"
            f"گیفت: {order['gift_name']}\n"
            f"گیرنده: {order['recipient']}\n"
            f"وضعیت: انجام شده"
        )
    except Exception:
        logging.exception("Order channel publication failed")
        await callback.answer(
            "ارسال به کانال ناموفق بود. دسترسی ربات را بررسی کن.",
            show_alert=True
        )
        return

    await callback.message.edit_text(
        callback.message.text + "\n\n🎉 سفارش انجام شد."
    )

    await bot.send_message(
        order["user_id"],
        f"🎉 سفارش {order_id} انجام شد."
    )

    await callback.answer("سفارش تکمیل شد.")


async def main():
    global bot_username

    await setup_database()

    # حذف webhook قبلی برای فعال شدن polling
    await bot.delete_webhook(drop_pending_updates=True)

    me = await bot.get_me()
    bot_username = me.username

    logging.info("BOT STARTED: @%s", bot_username)

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
