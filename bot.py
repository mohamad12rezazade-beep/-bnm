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
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message,
    CallbackQuery,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardRemove,
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

logging.basicConfig(level=logging.INFO, stream=sys.stdout, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger(__name__)

# ---------- دیتابیس ----------
class Database:
    def __init__(self):
        self.pool: asyncpg.Pool | None = None

    async def connect(self):
        if not DATABASE_URL:
            raise RuntimeError("DATABASE_URL تنظیم نشده است")
        # GitHub Actions در محیط IPv4 نیاز به استفاده از پولینگ دارد، اگر خطا داد از اتصال مستقیم استفاده کنید
        self.pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5, command_timeout=30)
        async with self.pool.acquire() as conn:
            await conn.execute("SELECT 1")
        logger.info("دیتابیس متصل شد")

    async def close(self):
        if self.pool:
            await self.pool.close()

    async def get_user(self, user_id: int):
        async with self.pool.acquire() as conn:
            return await conn.fetchrow("SELECT * FROM users WHERE user_id = $1", user_id)

    async def get_user_by_phone(self, phone: str):
        async with self.pool.acquire() as conn:
            return await conn.fetchrow("SELECT * FROM users WHERE phone = $1", phone)

    async def upsert_user(self, user_id: int, username: str | None, full_name: str, phone: str, referrer_id: int | None = None):
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                existing = await conn.fetchrow("SELECT user_id FROM users WHERE user_id = $1", user_id)
                if existing:
                    await conn.execute(
                        "UPDATE users SET username=$1, full_name=$2, phone=$3 WHERE user_id=$4",
                        username, full_name, phone, user_id,
                    )
                else:
                    ref = referrer_id if referrer_id != user_id else None
                    await conn.execute(
                        "INSERT INTO users(user_id, username, full_name, phone, referrer_id) VALUES($1,$2,$3,$4,$5)",
                        user_id, username, full_name, phone, ref,
                    )
                    if ref:
                        await conn.execute(
                            "INSERT INTO referrals(referrer_id, referred_id) VALUES($1,$2) ON CONFLICT DO NOTHING",
                            ref, user_id,
                        )

    async def try_reward_referrer(self, user_id: int) -> bool:
        async with self.pool.acquire() as conn:
            result = await conn.fetchval("SELECT reward_referrer_atomic($1)", user_id)
            return bool(result)

    async def create_order(self, order_id: str, user_id: int, gift_key: str, price: int, recipient: str) -> bool:
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

    async def approve_order(self, order_id: str) -> bool:
        async with self.pool.acquire() as conn:
            result = await conn.execute(
                "UPDATE orders SET status='approved', updated_at=NOW() WHERE order_id=$1 AND status='pending'",
                order_id,
            )
            return result == "UPDATE 1"

    async def get_balance(self, user_id: int) -> int:
        async with self.pool.acquire() as conn:
            val = await conn.fetchval("SELECT balance FROM users WHERE user_id=$1", user_id)
            return val or 0

    async def get_transactions(self, user_id: int, limit: int = 20):
        async with self.pool.acquire() as conn:
            return await conn.fetch(
                "SELECT amount, type, description, created_at FROM transactions WHERE user_id=$1 ORDER BY created_at DESC LIMIT $2",
                user_id, limit,
            )

db = Database()

# ---------- وضعیت‌ها ----------
class BuyGift(StatesGroup):
    waiting_recipient = State()
    confirming = State()

# ---------- بررسی عضویت ----------
async def check_membership(bot: Bot, user_id: int) -> list[str]:
    missing = []
    for ch in CHANNELS:
        try:
            member = await bot.get_chat_member(chat_id=ch["username"], user_id=user_id)
            if member.status in ("left", "kicked"):
                missing.append(ch["username"])
        except Exception as e:
            logger.warning(f"بررسی عضویت کانال {ch['username']} ناموفق: {e}")
            missing.append(ch["username"])
    return missing

def channels_keyboard():
    kb = InlineKeyboardBuilder()
    for ch in CHANNELS:
        kb.button(text=f"عضویت در {ch['username']}", url=ch["url"])
    kb.button(text="✅ بررسی عضویت", callback_data="check_membership")
    kb.adjust(1)
    return kb.as_markup()

# ---------- کیبوردها ----------
def main_menu_keyboard():
    kb = ReplyKeyboardBuilder()
    kb.button(text="🔗 لینک دعوت")
    kb.button(text="🎁 خرید گیفت")
    kb.button(text="⭐ موجودی استارز")
    kb.adjust(2, 1)
    return kb.as_markup(resize_keyboard=True)

def phone_keyboard():
    kb = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📱 تأیید شماره موبایل", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )
    return kb

def gift_keyboard():
    kb = InlineKeyboardBuilder()
    for key, info in GIFTS.items():
        kb.button(text=f"{info['name']} — {info['price']} اعتبار", callback_data=f"gift:{key}")
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

# ---------- روتر ----------
router = Router()

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext, bot: Bot):
    await state.clear()
    user_id = message.from_user.id

    # بررسی اینکه آیا از طریق لینک دعوت وارد شده است
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
    if not user:
        # کاربر جدید، درخواست شماره
        await state.update_data(referrer_id=referrer_id)
        await message.answer(
            "برای استفاده از ربات، لطفاً شماره موبایل خود را با دکمه زیر تأیید کنید:",
            reply_markup=phone_keyboard(),
        )
        return

    # کاربر موجود، بررسی عضویت
    missing = await check_membership(bot, user_id)
    if missing:
        await message.answer(
            f"برای استفاده از ربات، ابتدا در کانال‌های زیر عضو شوید:\n" + "\n".join(missing),
            reply_markup=channels_keyboard(),
        )
        return

    await message.answer("منوی اصلی:", reply_markup=main_menu_keyboard())

@router.message(F.contact)
async def handle_contact(message: Message, state: FSMContext, bot: Bot):
    contact: Contact = message.contact
    user_id = message.from_user.id

    # بررسی اینکه مخاطب متعلق به خود فرستنده باشد
    if contact.user_id != user_id:
        await message.answer("❌ شما فقط می‌توانید شماره موبایل خودتان را ارسال کنید.")
        return

    phone = contact.phone_number
    # نرمال‌سازی شماره ایران
    phone = phone.replace(" ", "").replace("-", "")
    if phone.startswith("0"):
        phone = "+98" + phone[1:]
    elif phone.startswith("98") and not phone.startswith("+"):
        phone = "+" + phone
    elif not phone.startswith("+"):
        phone = "+98" + phone

    if not re.fullmatch(r"\+989\d{9}", phone):
        await message.answer("❌ فرمت شماره معتبر نیست. لطفاً از دکمه اشتراک‌گذاری مخاطب استفاده کنید.")
        return

    # بررسی اینکه شماره قبلاً توسط کاربر دیگری ثبت نشده باشد
    existing = await db.get_user_by_phone(phone)
    if existing and existing["user_id"] != user_id:
        await message.answer("❌ این شماره قبلاً توسط حساب دیگری ثبت شده است.")
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

    # بررسی عضویت
    missing = await check_membership(bot, user_id)
    if missing:
        await message.answer(
            f"✅ شماره تأیید شد.\n\nبرای ادامه، در کانال‌های زیر عضو شوید:",
            reply_markup=channels_keyboard(),
        )
    else:
        # عضویت کامل، تلاش برای پاداش دعوت
        await db.try_reward_referrer(user_id)
        await message.answer("✅ ثبت‌نام کامل شد. به منوی اصلی خوش آمدید.", reply_markup=main_menu_keyboard())
    await state.clear()

@router.callback_query(F.data == "check_membership")
async def cb_check_membership(call: CallbackQuery, bot: Bot):
    missing = await check_membership(bot, call.from_user.id)
    if missing:
        await call.answer("هنوز در همه کانال‌ها عضو نشده‌اید.", show_alert=True)
        return

    # عضو کامل شده، تلاش برای پاداش دعوت
    await db.try_reward_referrer(call.from_user.id)
    await call.message.delete()
    await call.message.answer("✅ عضویت تأیید شد. به منوی اصلی خوش آمدید.", reply_markup=main_menu_keyboard())
    await call.answer()

@router.message(F.text == "🔗 لینک دعوت")
async def show_invite_link(message: Message, bot: Bot):
    user_id = message.from_user.id
    user = await db.get_user(user_id)
    if not user:
        await message.answer("لطفاً ابتدا /start را ارسال کنید.")
        return

    me = await bot.get_me()
    link = f"https://t.me/{me.username}?start=ref_{user_id}"
    await message.answer(
        f"لینک دعوت اختصاصی شما:\n\n`{link}`\n\n"
        "با دعوت از دوستان خود، پس از تأیید شماره و عضویت آن‌ها، ۲ اعتبار دریافت می‌کنید.",
        parse_mode="Markdown",
    )

@router.message(F.text == "⭐ موجودی استارز")
async def show_balance(message: Message):
    user_id = message.from_user.id
    balance = await db.get_balance(user_id)
    txs = await db.get_transactions(user_id)

    text = f"⭐ موجودی فعلی شما: **{balance}** اعتبار\n\n"
    if txs:
        text += "📋 تاریخچه تراکنش‌ها:\n"
        for tx in txs:
            sign = "+" if tx["amount"] > 0 else ""
            text += f"• {sign}{tx['amount']} — {tx['description'] or tx['type']}\n"
    else:
        text += "هنوز تراکنشی ثبت نشده است."

    await message.answer(text, parse_mode="Markdown")

@router.message(F.text == "🎁 خرید گیفت")
async def show_gifts(message: Message):
    await message.answer("یک گیفت انتخاب کنید:", reply_markup=gift_keyboard())

@router.callback_query(F.data.startswith("gift:"))
async def cb_select_gift(call: CallbackQuery, state: FSMContext):
    key = call.data.split(":")[1]
    gift = GIFTS.get(key)
    if not gift:
        await call.answer("گیفت نامعتبر", show_alert=True)
        return

    balance = await db.get_balance(call.from_user.id)
    if balance < gift["price"]:
        await call.answer(f"موجودی کافی نیست. موجودی شما: {balance} اعتبار", show_alert=True)
        return

    await state.update_data(gift_key=key, gift_price=gift["price"], gift_name=gift["name"])
    await state.set_state(BuyGift.waiting_recipient)
    await call.message.edit_text(
        f"شما {gift['name']} را به قیمت {gift['price']} اعتبار انتخاب کردید.\n\n"
        "لطفاً آیدی عددی یا یوزرنیم گیرنده را ارسال کنید:",
        reply_markup=back_keyboard(),
    )
    await call.answer()

@router.message(BuyGift.waiting_recipient)
async def receive_recipient(message: Message, state: FSMContext):
    recipient = message.text.strip()
    if not recipient:
        await message.answer("لطفاً یک آیدی معتبر ارسال کنید.")
        return

    data = await state.get_data()
    gift_name = data["gift_name"]
    gift_price = data["gift_price"]

    await state.update_data(recipient=recipient)
    await state.set_state(BuyGift.confirming)

    kb = InlineKeyboardBuilder()
    kb.button(text="✅ تأیید نهایی خرید", callback_data="confirm_purchase")
    kb.button(text="🔙 بازگشت", callback_data="main_menu")
    kb.adjust(1)

    await message.answer(
        f"📋 پیش‌فاکتور:\n"
        f"گیفت: {gift_name}\n"
        f"قیمت: {gift_price} اعتبار\n"
        f"گیرنده: {recipient}\n\n"
        f"آیا تأیید می‌کنید؟",
        reply_markup=kb.as_markup(),
    )

@router.callback_query(F.data == "confirm_purchase", BuyGift.confirming)
async def cb_confirm_purchase(call: CallbackQuery, state: FSMContext, bot: Bot):
    data = await state.get_data()
    gift_key = data["gift_key"]
    gift_price = data["gift_price"]
    gift_name = data["gift_name"]
    recipient = data["recipient"]
    user_id = call.from_user.id

    order_id = uuid.uuid4().hex[:12].upper()
    success = await db.create_order(order_id, user_id, gift_key, gift_price, recipient)

    if not success:
        await call.message.edit_text("❌ موجودی ناکافی است یا خطایی رخ داد. لطفاً دوباره تلاش کنید.")
        await state.clear()
        await call.answer()
        return

    # اطلاع به ادمین
    user = await db.get_user(user_id)
    admin_text = (
        f"🛒 **سفارش جدید**\n\n"
        f"شناسه: `{order_id}`\n"
        f"خریدار: {user['full_name']} (`{user_id}`)\n"
        f"گیرنده: `{recipient}`\n"
        f"گیفت: {gift_name}\n"
        f"قیمت: {gift_price}\n"
        f"تاریخ: {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    )
    try:
        await bot.send_message(ADMIN_ID, admin_text, parse_mode="Markdown", reply_markup=order_admin_keyboard(order_id))
    except Exception as e:
        logger.error(f"ارسال به ادمین ناموفق: {e}")

    await call.message.edit_text(
        f"✅ سفارش شما ثبت شد.\nشناسه: `{order_id}`\n\nپس از تأیید و انجام، نتیجه اطلاع‌رسانی می‌شود.",
        parse_mode="Markdown",
    )
    await state.clear()
    await call.answer()

@router.callback_query(F.data.startswith("approve:"))
async def cb_approve_order(call: CallbackQuery, bot: Bot):
    if call.from_user.id != ADMIN_ID:
        await call.answer("⛔ شما مجاز به انجام این عملیات نیستید.", show_alert=True)
        return

    order_id = call.data.split(":")[1]
    ok = await db.approve_order(order_id)
    if not ok:
        await call.answer("سفارش قبلاً پردازش شده یا وجود ندارد.", show_alert=True)
        return

    order = await db.get_order(order_id)
    await call.message.edit_text(
        call.message.text + "\n\n✅ **تأیید شد** — پس از ارسال دستی گیفت، دکمه زیر را بزنید.",
        parse_mode="Markdown",
        reply_markup=order_complete_keyboard(order_id),
    )
    await call.answer("تأیید شد")

@router.callback_query(F.data.startswith("reject:"))
async def cb_reject_order(call: CallbackQuery, bot: Bot):
    if call.from_user.id != ADMIN_ID:
        await call.answer("⛔ شما مجاز به انجام این عملیات نیستید.", show_alert=True)
        return

    order_id = call.data.split(":")[1]
    ok = await db.reject_order(order_id)
    if not ok:
        await call.answer("سفارش قبلاً پردازش شده یا وجود ندارد.", show_alert=True)
        return

    order = await db.get_order(order_id)
    try:
        await bot.send_message(
            order["user_id"],
            f"❌ سفارش `{order_id}` رد شد. {order['gift_price']} اعتبار به موجودی شما بازگشت.",
            parse_mode="Markdown",
        )
    except Exception:
        pass

    await call.message.edit_text(call.message.text + "\n\n❌ **رد شد** — وجه بازگشت داده شد.", parse_mode="Markdown")
    await call.answer("رد شد")

@router.callback_query(F.data.startswith("complete:"))
async def cb_complete_order(call: CallbackQuery, bot: Bot):
    if call.from_user.id != ADMIN_ID:
        await call.answer("⛔ شما مجاز به انجام این عملیات نیستید.", show_alert=True)
        return

    order_id = call.data.split(":")[1]
    ok = await db.mark_order_completed(order_id)
    if not ok:
        await call.answer("سفارش تأیید نشده یا قبلاً منتشر شده است.", show_alert=True)
        return

    order = await db.get_order(order_id)
    user = await db.get_user(order["user_id"])

    # انتشار در کانال
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
        logger.error(f"انتشار در کانال ناموفق: {e}")

    try:
        await bot.send_message(
            order["user_id"],
            f"🎉 سفارش `{order_id}` ({order['gift_name']}) انجام و در کانال منتشر شد.",
            parse_mode="Markdown",
        )
    except Exception:
        pass

    await call.message.edit_text(call.message.text + "\n\n📦 **انجام شد و منتشر شد**", parse_mode="Markdown")
    await call.answer("ثبت شد")

@router.callback_query(F.data == "main_menu")
async def cb_main_menu(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.delete()
    await call.message.answer("منوی اصلی:", reply_markup=main_menu_keyboard())
    await call.answer()

# ---------- main ----------
async def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN تنظیم نشده است")

    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    # بررسی اتصال
    me = await bot.get_me()
    logger.info(f"ربات متصل شد: @{me.username}")

    await db.connect()

    # حذف Webhook باقیمانده
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
