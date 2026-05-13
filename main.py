import asyncio
import logging
import os
from datetime import datetime
from typing import List, Optional

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    Update,
)
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    Integer,
    String,
    Text,
    select,
    func,
    update as sa_update,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

# ─────────────────────────────────────────────
# ⚙️  SOZLAMALAR
# ─────────────────────────────────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql+asyncpg://user:pass@localhost/dbname")

# Railway postgres:// → postgresql+asyncpg:// conversion
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+asyncpg://", 1)
elif DATABASE_URL.startswith("postgresql://") and "asyncpg" not in DATABASE_URL:
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)

# Super adminlar (Telegram ID lar)
SUPER_ADMINS: List[int] = [int(x) for x in os.getenv("SUPER_ADMINS", "123456789").split(",") if x.strip()]

# Majburiy obuna kanallari (boshlang'ich)
DEFAULT_CHANNELS: List[str] = [ch for ch in os.getenv("CHANNELS", "@mychannel").split(",") if ch.strip()]

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# 🗄️  DATABASE MODELLARI
# ─────────────────────────────────────────────
class Base(DeclarativeBase):
    pass


class UserModel(Base):
    __tablename__ = "users"
    id = Column(BigInteger, primary_key=True)          # Telegram user_id
    username = Column(String(64), nullable=True)
    full_name = Column(String(128), nullable=True)
    joined_at = Column(DateTime, default=datetime.utcnow)
    last_seen = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class MovieModel(Base):
    __tablename__ = "movies"
    id = Column(Integer, primary_key=True, autoincrement=True)
    code = Column(String(32), unique=True, nullable=False)   # Foydalanuvchi yuboriladigan kod
    chat_id = Column(BigInteger, nullable=False)              # Xabar joylashgan chat
    message_id = Column(BigInteger, nullable=False)           # Asl xabar ID
    title = Column(String(256), nullable=True)                # Ixtiyoriy nom
    views = Column(Integer, default=0)
    added_at = Column(DateTime, default=datetime.utcnow)


class AdminModel(Base):
    __tablename__ = "admins"
    id = Column(BigInteger, primary_key=True)   # Telegram user_id
    added_at = Column(DateTime, default=datetime.utcnow)


class ChannelModel(Base):
    __tablename__ = "channels"
    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String(64), unique=True, nullable=False)   # @channelusername


# ─────────────────────────────────────────────
# 🔌  DB ENGINE
# ─────────────────────────────────────────────
engine = create_async_engine(DATABASE_URL, echo=False)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    # Default kanallarni qo'shish
    async with AsyncSessionLocal() as s:
        for ch in DEFAULT_CHANNELS:
            ch = ch.strip()
            if not ch:
                continue
            existing = await s.get(ChannelModel, None)
            res = await s.execute(select(ChannelModel).where(ChannelModel.username == ch))
            if not res.scalar_one_or_none():
                s.add(ChannelModel(username=ch))
        await s.commit()


# ─────────────────────────────────────────────
# 🛠️  YORDAMCHI FUNKSIYALAR
# ─────────────────────────────────────────────
async def get_channels(session: AsyncSession) -> List[str]:
    res = await session.execute(select(ChannelModel))
    return [r.username for r in res.scalars().all()]


async def is_admin(session: AsyncSession, user_id: int) -> bool:
    if user_id in SUPER_ADMINS:
        return True
    adm = await session.get(AdminModel, user_id)
    return adm is not None


async def check_subscription(bot: Bot, user_id: int, channels: List[str]) -> List[str]:
    """Obuna bo'lmagan kanallar ro'yxatini qaytaradi."""
    not_subscribed = []
    for ch in channels:
        try:
            member = await bot.get_chat_member(ch, user_id)
            if member.status in ("left", "kicked", "restricted"):
                not_subscribed.append(ch)
        except Exception:
            not_subscribed.append(ch)
    return not_subscribed


async def register_user(session: AsyncSession, message: Message):
    user = message.from_user
    existing = await session.get(UserModel, user.id)
    if existing:
        existing.last_seen = datetime.utcnow()
        existing.username = user.username
        existing.full_name = user.full_name
    else:
        session.add(UserModel(
            id=user.id,
            username=user.username,
            full_name=user.full_name,
        ))
    await session.commit()


def sub_keyboard(channels: List[str]) -> InlineKeyboardMarkup:
    buttons = []
    for ch in channels:
        link = f"https://t.me/{ch.lstrip('@')}"
        buttons.append([InlineKeyboardButton(text=f"📢 {ch}", url=link)])
    buttons.append([InlineKeyboardButton(text="✅ Tekshirish", callback_data="check_sub")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


# ─────────────────────────────────────────────
# 📋  STATES
# ─────────────────────────────────────────────
class AddMovie(StatesGroup):
    waiting_code = State()
    waiting_video = State()


class Broadcast(StatesGroup):
    waiting_content = State()


class AddAdmin(StatesGroup):
    waiting_id = State()


class AddChannel(StatesGroup):
    waiting_username = State()


# ─────────────────────────────────────────────
# ⌨️  KLAVIATURALAR
# ─────────────────────────────────────────────
def main_admin_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🎬 Kino qo'shish", callback_data="admin_add_movie"),
            InlineKeyboardButton(text="📊 Statistika", callback_data="admin_stats"),
        ],
        [
            InlineKeyboardButton(text="📣 Reklama", callback_data="admin_broadcast"),
            InlineKeyboardButton(text="📢 Kanallar", callback_data="admin_channels"),
        ],
        [
            InlineKeyboardButton(text="👑 Adminlar", callback_data="admin_admins"),
        ],
    ])


def back_kb(data: str = "admin_panel") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Orqaga", callback_data=data)]
    ])


def cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Bekor qilish", callback_data="cancel_action")]
    ])


# ─────────────────────────────────────────────
# 🤖  ROUTER VA HANDLERLAR
# ─────────────────────────────────────────────
router = Router()


# ── /start ──
@router.message(CommandStart())
async def cmd_start(message: Message, bot: Bot):
    async with AsyncSessionLocal() as s:
        await register_user(s, message)
        channels = await get_channels(s)
        not_sub = await check_subscription(bot, message.from_user.id, channels)

    if not_sub:
        await message.answer(
            "🎬 <b>Kino Botga Xush Kelibsiz!</b>\n\n"
            "🔐 Botdan foydalanish uchun quyidagi kanallarga obuna bo'ling:\n\n"
            "⬇️ <i>Obuna bo'lgach «✅ Tekshirish» tugmasini bosing!</i>",
            reply_markup=sub_keyboard(not_sub),
            parse_mode="HTML",
        )
        return

    await message.answer(
        f"🎉 <b>Salom, {message.from_user.first_name}!</b>\n\n"
        "🎬 <b>Kino Botga Xush Kelibsiz!</b>\n\n"
        "📽️ Kino kodini yuboring va fil'mingizni tomosha qiling!\n\n"
        "💡 <i>Masalan: </i><code>001</code>",
        parse_mode="HTML",
    )


# ── Obuna tekshirish callback ──
@router.callback_query(F.data == "check_sub")
async def check_sub_callback(call: CallbackQuery, bot: Bot):
    async with AsyncSessionLocal() as s:
        channels = await get_channels(s)
        not_sub = await check_subscription(bot, call.from_user.id, channels)

    if not_sub:
        await call.answer("❌ Hali obuna bo'lmagan kanallar bor!", show_alert=True)
        await call.message.edit_reply_markup(reply_markup=sub_keyboard(not_sub))
        return

    await call.message.edit_text(
        f"✅ <b>Zo'r, {call.from_user.first_name}!</b>\n\n"
        "🎬 Endi kino kodini yuboring va tomosha qiling!\n\n"
        "💡 <i>Masalan: </i><code>001</code>",
        parse_mode="HTML",
    )


# ── /admin ──
@router.message(Command("admin"))
async def cmd_admin(message: Message):
    async with AsyncSessionLocal() as s:
        if not await is_admin(s, message.from_user.id):
            await message.answer("🚫 <b>Sizda admin huquqi yo'q!</b>", parse_mode="HTML")
            return

    await message.answer(
        "👑 <b>Admin Panel</b>\n\n"
        "🛠️ Quyidagi amallardan birini tanlang:",
        reply_markup=main_admin_kb(),
        parse_mode="HTML",
    )


# ── Admin panel callback ──
@router.callback_query(F.data == "admin_panel")
async def admin_panel_cb(call: CallbackQuery):
    async with AsyncSessionLocal() as s:
        if not await is_admin(s, call.from_user.id):
            await call.answer("🚫 Ruxsat yo'q!", show_alert=True)
            return

    await call.message.edit_text(
        "👑 <b>Admin Panel</b>\n\n"
        "🛠️ Quyidagi amallardan birini tanlang:",
        reply_markup=main_admin_kb(),
        parse_mode="HTML",
    )


# ── Kino qo'shish ──
@router.callback_query(F.data == "admin_add_movie")
async def add_movie_start(call: CallbackQuery, state: FSMContext):
    async with AsyncSessionLocal() as s:
        if not await is_admin(s, call.from_user.id):
            await call.answer("🚫 Ruxsat yo'q!", show_alert=True)
            return

    await call.message.edit_text(
        "🎬 <b>Yangi Kino Qo'shish</b>\n\n"
        "1️⃣ Avval kinoning <b>kodini</b> yuboring:\n\n"
        "💡 <i>Masalan: </i><code>001</code> <i>yoki</i> <code>avatar2</code>",
        reply_markup=cancel_kb(),
        parse_mode="HTML",
    )
    await state.set_state(AddMovie.waiting_code)


@router.message(AddMovie.waiting_code)
async def add_movie_code(message: Message, state: FSMContext):
    code = message.text.strip()
    async with AsyncSessionLocal() as s:
        res = await s.execute(select(MovieModel).where(MovieModel.code == code))
        if res.scalar_one_or_none():
            await message.answer(
                f"⚠️ <b>«{code}» kodi allaqachon mavjud!</b>\n\n"
                "Boshqa kod kiriting yoki /admin buyrug'i orqali qaytib chiqing.",
                parse_mode="HTML",
            )
            return

    await state.update_data(code=code)
    await message.answer(
        f"✅ Kod: <code>{code}</code>\n\n"
        "2️⃣ Endi <b>video faylni</b> yuboring:\n\n"
        "📤 <i>Video sifatida yuboring (hujjat sifatida emas!)</i>",
        reply_markup=cancel_kb(),
        parse_mode="HTML",
    )
    await state.set_state(AddMovie.waiting_video)


@router.message(AddMovie.waiting_video, F.video | F.document)
async def add_movie_video(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    code = data["code"]

    async with AsyncSessionLocal() as s:
        movie = MovieModel(
            code=code,
            chat_id=message.chat.id,
            message_id=message.message_id,
            title=code,
        )
        s.add(movie)
        await s.commit()

    await state.clear()
    await message.answer(
        f"🎉 <b>Kino muvaffaqiyatli qo'shildi!</b>\n\n"
        f"🔑 Kod: <code>{code}</code>\n"
        f"📨 Foydalanuvchilar shu kodni yuborsalar kino yetadi!\n\n"
        "👑 /admin — Panel",
        parse_mode="HTML",
    )


# ── Statistika ──
@router.callback_query(F.data == "admin_stats")
async def admin_stats(call: CallbackQuery):
    async with AsyncSessionLocal() as s:
        if not await is_admin(s, call.from_user.id):
            await call.answer("🚫 Ruxsat yo'q!", show_alert=True)
            return

        user_count = await s.scalar(select(func.count()).select_from(UserModel))
        movie_count = await s.scalar(select(func.count()).select_from(MovieModel))
        top_res = await s.execute(
            select(MovieModel).order_by(MovieModel.views.desc()).limit(10)
        )
        top_movies = top_res.scalars().all()

    top_text = ""
    for i, m in enumerate(top_movies, 1):
        top_text += f"  {i}. <code>{m.code}</code> — 👁 {m.views} marta\n"

    if not top_text:
        top_text = "  <i>Hali kino ko'rilmagan</i>\n"

    await call.message.edit_text(
        "📊 <b>Bot Statistikasi</b>\n\n"
        f"👥 Jami foydalanuvchilar: <b>{user_count}</b>\n"
        f"🎬 Jami kinolar: <b>{movie_count}</b>\n\n"
        "🏆 <b>Eng ko'p ko'rilgan 10 ta kino:</b>\n"
        f"{top_text}",
        reply_markup=back_kb(),
        parse_mode="HTML",
    )


# ── Reklama ──
@router.callback_query(F.data == "admin_broadcast")
async def admin_broadcast_start(call: CallbackQuery, state: FSMContext):
    async with AsyncSessionLocal() as s:
        if not await is_admin(s, call.from_user.id):
            await call.answer("🚫 Ruxsat yo'q!", show_alert=True)
            return

    await call.message.edit_text(
        "📣 <b>Reklama Yuborish</b>\n\n"
        "Yubormoqchi bo'lgan xabaringizni kiriting:\n"
        "📝 Matn, 🖼 rasm yoki 📨 Forward qilingan xabar bo'lishi mumkin.\n\n"
        "⚠️ <i>Xabar barcha foydalanuvchilarga jo'natiladi!</i>",
        reply_markup=cancel_kb(),
        parse_mode="HTML",
    )
    await state.set_state(Broadcast.waiting_content)


@router.message(Broadcast.waiting_content)
async def do_broadcast(message: Message, state: FSMContext, bot: Bot):
    await state.clear()
    async with AsyncSessionLocal() as s:
        res = await s.execute(select(UserModel))
        users = res.scalars().all()

    sent, failed = 0, 0
    for user in users:
        try:
            await message.copy_to(user.id)
            sent += 1
        except (TelegramForbiddenError, TelegramBadRequest):
            failed += 1
        await asyncio.sleep(0.05)

    await message.answer(
        f"📣 <b>Reklama yakunlandi!</b>\n\n"
        f"✅ Muvaffaqiyatli: <b>{sent}</b>\n"
        f"❌ Xatolik: <b>{failed}</b>",
        parse_mode="HTML",
        reply_markup=back_kb(),
    )


# ── Kanallar boshqaruvi ──
@router.callback_query(F.data == "admin_channels")
async def admin_channels(call: CallbackQuery):
    async with AsyncSessionLocal() as s:
        if not await is_admin(s, call.from_user.id):
            await call.answer("🚫 Ruxsat yo'q!", show_alert=True)
            return
        channels = await get_channels(s)

    ch_text = "\n".join([f"  • {ch}" for ch in channels]) if channels else "  <i>Hali kanal yo'q</i>"

    buttons = []
    for ch in channels:
        buttons.append([InlineKeyboardButton(
            text=f"🗑 {ch} o'chirish",
            callback_data=f"del_channel:{ch}"
        )])
    buttons.append([InlineKeyboardButton(text="➕ Kanal qo'shish", callback_data="add_channel")])
    buttons.append([InlineKeyboardButton(text="🔙 Orqaga", callback_data="admin_panel")])

    await call.message.edit_text(
        "📢 <b>Majburiy Obuna Kanallar</b>\n\n"
        f"{ch_text}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "add_channel")
async def add_channel_start(call: CallbackQuery, state: FSMContext):
    await call.message.edit_text(
        "📢 <b>Yangi kanal qo'shish</b>\n\n"
        "Kanal username'ini yuboring:\n"
        "💡 <i>Masalan: </i><code>@mychannel</code>",
        reply_markup=cancel_kb(),
        parse_mode="HTML",
    )
    await state.set_state(AddChannel.waiting_username)


@router.message(AddChannel.waiting_username)
async def do_add_channel(message: Message, state: FSMContext):
    username = message.text.strip()
    if not username.startswith("@"):
        username = "@" + username

    async with AsyncSessionLocal() as s:
        res = await s.execute(select(ChannelModel).where(ChannelModel.username == username))
        if res.scalar_one_or_none():
            await message.answer(f"⚠️ <b>{username}</b> allaqachon ro'yxatda!", parse_mode="HTML")
        else:
            s.add(ChannelModel(username=username))
            await s.commit()
            await message.answer(
                f"✅ <b>{username}</b> muvaffaqiyatli qo'shildi!",
                parse_mode="HTML",
                reply_markup=back_kb("admin_channels"),
            )
    await state.clear()


@router.callback_query(F.data.startswith("del_channel:"))
async def del_channel(call: CallbackQuery):
    ch = call.data.split(":", 1)[1]
    async with AsyncSessionLocal() as s:
        res = await s.execute(select(ChannelModel).where(ChannelModel.username == ch))
        obj = res.scalar_one_or_none()
        if obj:
            await s.delete(obj)
            await s.commit()
    await call.answer(f"🗑 {ch} o'chirildi!", show_alert=True)
    # Yangilangan ro'yxatni ko'rsatish
    await admin_channels(call)


# ── Adminlar boshqaruvi ──
@router.callback_query(F.data == "admin_admins")
async def admin_admins(call: CallbackQuery):
    async with AsyncSessionLocal() as s:
        if not await is_admin(s, call.from_user.id):
            await call.answer("🚫 Ruxsat yo'q!", show_alert=True)
            return
        res = await s.execute(select(AdminModel))
        admins = res.scalars().all()

    adm_text = "\n".join([f"  • <code>{a.id}</code>" for a in admins]) or "  <i>Hali qo'shimcha admin yo'q</i>"
    super_text = "\n".join([f"  • <code>{sid}</code>" for sid in SUPER_ADMINS])

    buttons = []
    for adm in admins:
        buttons.append([InlineKeyboardButton(
            text=f"🗑 {adm.id} o'chirish",
            callback_data=f"del_admin:{adm.id}"
        )])
    buttons.append([InlineKeyboardButton(text="➕ Admin qo'shish", callback_data="add_admin")])
    buttons.append([InlineKeyboardButton(text="🔙 Orqaga", callback_data="admin_panel")])

    await call.message.edit_text(
        "👑 <b>Adminlar Boshqaruvi</b>\n\n"
        f"⭐ Super adminlar:\n{super_text}\n\n"
        f"👮 Qo'shimcha adminlar:\n{adm_text}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "add_admin")
async def add_admin_start(call: CallbackQuery, state: FSMContext):
    if call.from_user.id not in SUPER_ADMINS:
        await call.answer("🚫 Faqat super admin qo'sha oladi!", show_alert=True)
        return
    await call.message.edit_text(
        "👑 <b>Yangi Admin Qo'shish</b>\n\n"
        "Admin Telegram ID sini yuboring:\n"
        "💡 <i>Masalan: </i><code>123456789</code>",
        reply_markup=cancel_kb(),
        parse_mode="HTML",
    )
    await state.set_state(AddAdmin.waiting_id)


@router.message(AddAdmin.waiting_id)
async def do_add_admin(message: Message, state: FSMContext):
    try:
        new_id = int(message.text.strip())
    except ValueError:
        await message.answer("❌ <b>Noto'g'ri ID!</b> Faqat raqam kiriting.", parse_mode="HTML")
        return

    async with AsyncSessionLocal() as s:
        existing = await s.get(AdminModel, new_id)
        if existing:
            await message.answer(f"⚠️ <code>{new_id}</code> allaqachon admin!", parse_mode="HTML")
        else:
            s.add(AdminModel(id=new_id))
            await s.commit()
            await message.answer(
                f"✅ <code>{new_id}</code> admin sifatida qo'shildi!",
                parse_mode="HTML",
                reply_markup=back_kb("admin_admins"),
            )
    await state.clear()


@router.callback_query(F.data.startswith("del_admin:"))
async def del_admin(call: CallbackQuery):
    if call.from_user.id not in SUPER_ADMINS:
        await call.answer("🚫 Faqat super admin o'chira oladi!", show_alert=True)
        return
    uid = int(call.data.split(":", 1)[1])
    async with AsyncSessionLocal() as s:
        obj = await s.get(AdminModel, uid)
        if obj:
            await s.delete(obj)
            await s.commit()
    await call.answer(f"🗑 {uid} adminlikdan olib tashlandi!", show_alert=True)
    await admin_admins(call)


# ── Bekor qilish ──
@router.callback_query(F.data == "cancel_action")
async def cancel_action(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.edit_text(
        "❌ <b>Amal bekor qilindi.</b>",
        reply_markup=back_kb(),
        parse_mode="HTML",
    )


# ── Kino kodi ──
@router.message(F.text & ~F.text.startswith("/"))
async def handle_movie_code(message: Message, bot: Bot):
    code = message.text.strip()

    async with AsyncSessionLocal() as s:
        await register_user(s, message)
        channels = await get_channels(s)
        not_sub = await check_subscription(bot, message.from_user.id, channels)

        if not_sub:
            await message.answer(
                "🔐 <b>Avval kanallarga obuna bo'ling!</b>",
                reply_markup=sub_keyboard(not_sub),
                parse_mode="HTML",
            )
            return

        res = await s.execute(select(MovieModel).where(MovieModel.code == code))
        movie = res.scalar_one_or_none()

        if not movie:
            await message.answer(
                f"🔍 <b>«{code}» kodli kino topilmadi!</b>\n\n"
                "💡 <i>Kodni to'g'ri kiritganingizni tekshiring.</i>",
                parse_mode="HTML",
            )
            return

        # Ko'rishlar sonini oshirish
        await s.execute(
            sa_update(MovieModel).where(MovieModel.code == code).values(views=MovieModel.views + 1)
        )
        await s.commit()

    # Kinoni caption va forward yozuvisiz nusxalash
    try:
        await bot.copy_message(
            chat_id=message.chat.id,
            from_chat_id=movie.chat_id,
            message_id=movie.message_id,
            caption=None,
        )
    except TelegramBadRequest as e:
        log.error(f"copy_message xatosi: {e}")
        await message.answer(
            "⚠️ <b>Kino yuborishda xatolik yuz berdi!</b>\n"
            "Iltimos, keyinroq urinib ko'ring.",
            parse_mode="HTML",
        )


# ─────────────────────────────────────────────
# 🚀  ASOSIY FUNKSIYA
# ─────────────────────────────────────────────
async def main():
    await init_db()
    log.info("✅ Baza tayyor!")

    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    log.info("🚀 Bot ishga tushmoqda...")
    await dp.start_polling(bot, allowed_updates=Update.all_types())


if __name__ == "__main__":
    asyncio.run(main())
