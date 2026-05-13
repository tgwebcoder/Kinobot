"""
🎬 KINO BOT — Aiogram 3.x + PostgreSQL (Railway)
================================================
Mualllif tarkibi:
  - Majburiy obuna middleware
  - Kino qo'shish va nusxalash (copy_message)
  - Admin panel (statistika, reklama, kanallar, adminlar)
  - O'zbek tilli professional interfeys
"""

import asyncio
import logging
import os
from datetime import datetime
from typing import Any, Awaitable, Callable, Dict, Optional

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy import BigInteger, DateTime, Integer, String, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# ═══════════════════════════════════════════════════════════
#  ⚙️  CONFIG — Muhit o'zgaruvchilari
# ═══════════════════════════════════════════════════════════
BOT_TOKEN: str = os.environ["8709080386:AAGWNe1mOTIqGDBFwBFM-1SHlg8gmcVtBvs"]
DATABASE_URL: str = os.environ["DATABASE_URL"]
SUPER_ADMIN_ID: int = int(os.environ.get("8505118420", "0"))

# Railway asyncpg uchun URL ni to'g'irlash
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+asyncpg://", 1)
elif DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)

# ═══════════════════════════════════════════════════════════
#  🗄️  DATABASE MODELLARI
# ═══════════════════════════════════════════════════════════
class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    username: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    full_name: Mapped[str] = mapped_column(String(200), nullable=False)
    joined_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Movie(Base):
    __tablename__ = "movies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    message_id: Mapped[int] = mapped_column(Integer, nullable=False)
    views: Mapped[int] = mapped_column(Integer, default=0)
    added_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Channel(Base):
    __tablename__ = "channels"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    channel_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    channel_username: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    channel_name: Mapped[str] = mapped_column(String(200), nullable=False)


class Admin(Base):
    __tablename__ = "admins"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    added_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


# Async engine va session factory
engine = create_async_engine(DATABASE_URL, echo=False, pool_pre_ping=True)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def init_db() -> None:
    """Jadvallarni yaratish va super adminni ro'yxatdan o'tkazish."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    if SUPER_ADMIN_ID:
        async with async_session() as session:
            exists = await session.scalar(
                select(Admin).where(Admin.user_id == SUPER_ADMIN_ID)
            )
            if not exists:
                session.add(Admin(user_id=SUPER_ADMIN_ID))
                await session.commit()


# ═══════════════════════════════════════════════════════════
#  🎛️  FSM HOLATLARI
# ═══════════════════════════════════════════════════════════
class AddMovieStates(StatesGroup):
    waiting_code = State()
    waiting_video = State()


class BroadcastStates(StatesGroup):
    choosing_type = State()
    waiting_content = State()


class AddChannelStates(StatesGroup):
    waiting_channel = State()


class RemoveChannelStates(StatesGroup):
    waiting_id = State()


class AddAdminStates(StatesGroup):
    waiting_user_id = State()


class SearchMovieStates(StatesGroup):
    waiting_code = State()


# ═══════════════════════════════════════════════════════════
#  🎹  KLAVIATURALAR
# ═══════════════════════════════════════════════════════════
def main_menu_kb(is_admin: bool = False) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="🎬 Kino qidirish", callback_data="search_movie")],
        [InlineKeyboardButton(text="ℹ️ Bot haqida", callback_data="about_bot")],
    ]
    if is_admin:
        rows.append(
            [InlineKeyboardButton(text="⚙️ Admin Panel", callback_data="admin_panel")]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_panel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🎬 Kino qo'shish", callback_data="add_movie")],
            [InlineKeyboardButton(text="📊 Statistika", callback_data="stats")],
            [InlineKeyboardButton(text="📢 Reklama", callback_data="broadcast")],
            [InlineKeyboardButton(text="📡 Kanallar", callback_data="manage_channels")],
            [InlineKeyboardButton(text="👑 Admin qo'shish", callback_data="add_admin")],
            [InlineKeyboardButton(text="🏠 Bosh menyu", callback_data="main_menu")],
        ]
    )


def channels_panel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="➕ Kanal qo'shish", callback_data="add_channel")],
            [InlineKeyboardButton(text="🗑 Kanal o'chirish", callback_data="remove_channel")],
            [InlineKeyboardButton(text="📋 Ro'yxatni ko'rish", callback_data="list_channels")],
            [InlineKeyboardButton(text="🔙 Admin panel", callback_data="admin_panel")],
        ]
    )


def broadcast_type_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📝 Matn", callback_data="bc_text"),
                InlineKeyboardButton(text="🖼 Rasm", callback_data="bc_photo"),
            ],
            [InlineKeyboardButton(text="📨 Forward", callback_data="bc_forward")],
            [InlineKeyboardButton(text="❌ Bekor qilish", callback_data="cancel")],
        ]
    )


def cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="❌ Bekor qilish", callback_data="cancel")]
        ]
    )


def back_admin_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔙 Admin panel", callback_data="admin_panel")]
        ]
    )


def subscribe_kb(channels: list) -> InlineKeyboardMarkup:
    rows = []
    for ch in channels:
        username = ch.channel_username
        if username:
            url = f"https://t.me/{username.lstrip('@')}"
            rows.append(
                [InlineKeyboardButton(text=f"📢 {ch.channel_name}", url=url)]
            )
    rows.append(
        [InlineKeyboardButton(text="✅ Obuna bo'ldim!", callback_data="check_sub")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ═══════════════════════════════════════════════════════════
#  🛠️  YORDAMCHI FUNKSIYALAR
# ═══════════════════════════════════════════════════════════
async def check_is_admin(user_id: int) -> bool:
    async with async_session() as session:
        result = await session.scalar(
            select(Admin).where(Admin.user_id == user_id)
        )
        return result is not None


async def get_all_channels() -> list:
    async with async_session() as session:
        rows = await session.scalars(select(Channel))
        return list(rows.all())


async def check_subscription(bot: Bot, user_id: int) -> list:
    """Foydalanuvchi obuna bo'lmagan kanallarni qaytaradi."""
    channels = await get_all_channels()
    not_subbed = []
    for ch in channels:
        try:
            member = await bot.get_chat_member(ch.channel_id, user_id)
            if member.status in ("left", "kicked", "banned"):
                not_subbed.append(ch)
        except Exception:
            not_subbed.append(ch)
    return not_subbed


async def register_user(user_id: int, username: Optional[str], full_name: str) -> None:
    async with async_session() as session:
        exists = await session.scalar(
            select(User).where(User.user_id == user_id)
        )
        if not exists:
            session.add(
                User(user_id=user_id, username=username, full_name=full_name)
            )
            await session.commit()


# ═══════════════════════════════════════════════════════════
#  🔒  MAJBURIY OBUNA MIDDLEWARE
# ═══════════════════════════════════════════════════════════
ALLOWED_CALLBACKS = {"check_sub"}
ALLOWED_COMMANDS = {"/start"}


class SubscriptionMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[Any, Dict[str, Any]], Awaitable[Any]],
        event: Any,
        data: Dict[str, Any],
    ) -> Any:
        bot: Bot = data["bot"]

        # Foydalanuvchi ID ni aniqlash
        if isinstance(event, Message):
            user = event.from_user
            # /start har doim o'tadi
            if event.text and event.text.split()[0] in ALLOWED_COMMANDS:
                return await handler(event, data)
        elif isinstance(event, CallbackQuery):
            user = event.from_user
            # Obuna tekshirish tugmasi o'tadi
            if event.data in ALLOWED_CALLBACKS:
                return await handler(event, data)
        else:
            return await handler(event, data)

        if user is None:
            return await handler(event, data)

        # Adminlar tekshirilmaydi
        if await check_is_admin(user.id):
            return await handler(event, data)

        # Obunani tekshirish
        not_subbed = await check_subscription(bot, user.id)
        if not_subbed:
            sub_text = (
                "🔐 <b>Botdan foydalanish uchun quyidagi\n"
                "kanallarga obuna bo'lishingiz shart!</b>\n\n"
                "📌 Barcha kanallarga obuna bo'lgach,\n"
                "<b>✅ Obuna bo'ldim!</b> tugmasini bosing."
            )
            if isinstance(event, Message):
                await event.answer(
                    sub_text,
                    reply_markup=subscribe_kb(not_subbed),
                    parse_mode="HTML",
                )
            elif isinstance(event, CallbackQuery):
                await event.message.answer(
                    sub_text,
                    reply_markup=subscribe_kb(not_subbed),
                    parse_mode="HTML",
                )
                await event.answer()
            return  # Handlergacha bormaslik

        return await handler(event, data)


# ═══════════════════════════════════════════════════════════
#  🗺️  ROUTER
# ═══════════════════════════════════════════════════════════
router = Router()


# ═══════════════════════════════════════════════════════════
#  👤  FOYDALANUVCHI HANDLERLARI
# ═══════════════════════════════════════════════════════════
@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    user = message.from_user
    await register_user(user.id, user.username, user.full_name)

    admin = await check_is_admin(user.id)
    not_subbed = await check_subscription(message.bot, user.id)

    if not_subbed and not admin:
        await message.answer(
            "🔐 <b>Botdan foydalanish uchun quyidagi\n"
            "kanallarga obuna bo'lishingiz shart!</b>\n\n"
            "📌 Barcha kanallarga obuna bo'lgach,\n"
            "<b>✅ Obuna bo'ldim!</b> tugmasini bosing.",
            reply_markup=subscribe_kb(not_subbed),
            parse_mode="HTML",
        )
        return

    await message.answer(
        f"🎬 <b>Xush kelibsiz, {user.first_name}!</b>\n\n"
        "🍿 Men — <b>Kino Bot</b>!\n"
        "📥 Sevimli kinolaringizni kod orqali topishingiz mumkin.\n\n"
        "🔢 Kino kodini yuboring va tomosha qiling! 🚀",
        reply_markup=main_menu_kb(is_admin=admin),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "check_sub")
async def check_sub_callback(callback: CallbackQuery, state: FSMContext) -> None:
    user = callback.from_user
    not_subbed = await check_subscription(callback.bot, user.id)

    if not_subbed:
        await callback.answer(
            "❌ Siz hali barcha kanallarga obuna bo'lmagansiz!",
            show_alert=True,
        )
        return

    await callback.message.delete()
    await register_user(user.id, user.username, user.full_name)
    admin = await check_is_admin(user.id)

    await callback.message.answer(
        f"✅ <b>Tabriklaymiz, {user.first_name}!</b>\n\n"
        "🎉 Barcha kanallarga obuna bo'ldingiz!\n"
        "🎬 Endi botdan to'liq foydalaning.\n\n"
        "🔢 Kino kodini yuboring va rohat qiling! 🍿",
        reply_markup=main_menu_kb(is_admin=admin),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "main_menu")
async def cb_main_menu(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    admin = await check_is_admin(callback.from_user.id)
    await callback.message.edit_text(
        "🏠 <b>Bosh Menyu</b>\n\n"
        "🎬 Kino kodini yuboring yoki tugmalardan foydalaning!",
        reply_markup=main_menu_kb(is_admin=admin),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "about_bot")
async def cb_about(callback: CallbackQuery) -> None:
    async with async_session() as session:
        user_count = await session.scalar(select(func.count(User.id))) or 0
        movie_count = await session.scalar(select(func.count(Movie.id))) or 0

    await callback.message.edit_text(
        "🎬 <b>Kino Bot haqida</b>\n\n"
        f"👥 Foydalanuvchilar: <b>{user_count:,}</b> ta\n"
        f"🎥 Kinolar bazasi: <b>{movie_count:,}</b> ta\n\n"
        "🔢 Kino kodini yuboring — darhol tomosha qiling!\n"
        "💡 Botni do'stlaringizga ulashing! 🚀",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🔙 Orqaga", callback_data="main_menu")]
            ]
        ),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "search_movie")
async def cb_search_movie(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(SearchMovieStates.waiting_code)
    await callback.message.edit_text(
        "🔍 <b>Kino qidirish</b>\n\n"
        "🔢 Kino kodini yuboring:\n"
        "💡 Masalan: <code>1234</code> yoki <code>KN001</code>",
        reply_markup=cancel_kb(),
        parse_mode="HTML",
    )


@router.message(SearchMovieStates.waiting_code)
async def handle_code_state(message: Message, state: FSMContext) -> None:
    await state.clear()
    await _send_movie(message, message.text.strip())


@router.message(F.text & ~F.text.startswith("/"))
async def handle_text(message: Message, state: FSMContext) -> None:
    current = await state.get_state()
    if current:
        return  # Holatga tegishli handler qabul qiladi
    await _send_movie(message, message.text.strip())


async def _send_movie(message: Message, code: str) -> None:
    """Kino kodiga qarab videoni nusxalab yuborish."""
    async with async_session() as session:
        movie = await session.scalar(select(Movie).where(Movie.code == code))

    if not movie:
        await message.answer(
            f"❌ <b>«{code}» kodi bo'yicha kino topilmadi!</b>\n\n"
            "🔢 Kodni to'g'ri kiritdingizmi?\n"
            "💬 Muammo bo'lsa, admin bilan bog'laning.",
            parse_mode="HTML",
        )
        return

    try:
        await message.bot.copy_message(
            chat_id=message.chat.id,
            from_chat_id=movie.chat_id,
            message_id=movie.message_id,
            caption=None,  # Hech qanday matn yoki reklama chiqmasin
        )
        # Ko'rishlar sonini oshirish
        async with async_session() as session:
            await session.execute(
                update(Movie)
                .where(Movie.code == code)
                .values(views=Movie.views + 1)
            )
            await session.commit()

    except TelegramBadRequest as e:
        logging.error(f"copy_message xatosi [{code}]: {e}")
        await message.answer(
            "⚠️ <b>Kino yuborishda texnik xatolik!</b>\n\n"
            "🔧 Iltimos, keyinroq qayta urinib ko'ring.",
            parse_mode="HTML",
        )


# ═══════════════════════════════════════════════════════════
#  ⚙️  ADMIN PANEL HANDLERLARI
# ═══════════════════════════════════════════════════════════
async def _require_admin(callback: CallbackQuery) -> bool:
    if not await check_is_admin(callback.from_user.id):
        await callback.answer("⛔ Sizda bu amal uchun ruxsat yo'q!", show_alert=True)
        return False
    return True


@router.callback_query(F.data == "admin_panel")
async def cb_admin_panel(callback: CallbackQuery, state: FSMContext) -> None:
    if not await _require_admin(callback):
        return
    await state.clear()
    await callback.message.edit_text(
        "⚙️ <b>Admin Panel</b>\n\n"
        "🎛 Quyidagi amallardan birini tanlang:",
        reply_markup=admin_panel_kb(),
        parse_mode="HTML",
    )


# ───────────────────────────────────────────────────────────
#  🎬 KINO QO'SHISH
# ───────────────────────────────────────────────────────────
@router.callback_query(F.data == "add_movie")
async def cb_add_movie(callback: CallbackQuery, state: FSMContext) -> None:
    if not await _require_admin(callback):
        return
    await state.set_state(AddMovieStates.waiting_code)
    await callback.message.edit_text(
        "🎬 <b>Yangi Kino Qo'shish</b>\n\n"
        "1️⃣ Avval kino <b>kodini</b> yuboring:\n\n"
        "💡 Masalan: <code>1234</code> yoki <code>KN001</code>\n"
        "⚠️ Kod noyob bo'lishi kerak!",
        reply_markup=cancel_kb(),
        parse_mode="HTML",
    )


@router.message(AddMovieStates.waiting_code)
async def add_movie_code(message: Message, state: FSMContext) -> None:
    code = message.text.strip()

    async with async_session() as session:
        existing = await session.scalar(select(Movie).where(Movie.code == code))

    if existing:
        await message.answer(
            f"⚠️ <b>«{code}» kodi allaqachon mavjud!</b>\n\n"
            "🔢 Boshqa noyob kod kiriting:",
            reply_markup=cancel_kb(),
            parse_mode="HTML",
        )
        return

    await state.update_data(code=code)
    await state.set_state(AddMovieStates.waiting_video)
    await message.answer(
        f"✅ <b>Kod qabul qilindi:</b> <code>{code}</code>\n\n"
        "2️⃣ Endi <b>video faylni</b> yuboring:\n"
        "🎥 (Video, GIF yoki hujjat shaklida bo'lishi mumkin)",
        reply_markup=cancel_kb(),
        parse_mode="HTML",
    )


@router.message(AddMovieStates.waiting_video)
async def add_movie_video(message: Message, state: FSMContext) -> None:
    if not (message.video or message.document or message.animation):
        await message.answer(
            "⚠️ <b>Iltimos, video fayl yuboring!</b>\n"
            "📝 Matn emas, aniq video kerak.",
            reply_markup=cancel_kb(),
            parse_mode="HTML",
        )
        return

    data = await state.get_data()
    code = data["code"]

    async with async_session() as session:
        movie = Movie(
            code=code,
            chat_id=message.chat.id,
            message_id=message.message_id,
        )
        session.add(movie)
        await session.commit()

    await state.clear()
    await message.answer(
        f"🎉 <b>Kino muvaffaqiyatli qo'shildi!</b>\n\n"
        f"📎 Kod: <code>{code}</code>\n"
        f"🎬 Saqlandi: chat_id={message.chat.id}, msg_id={message.message_id}\n\n"
        "👥 Endi foydalanuvchilar bu kinoni kod orqali tomosha qilishadi!",
        reply_markup=back_admin_kb(),
        parse_mode="HTML",
    )


# ───────────────────────────────────────────────────────────
#  📊 STATISTIKA
# ───────────────────────────────────────────────────────────
@router.callback_query(F.data == "stats")
async def cb_stats(callback: CallbackQuery) -> None:
    if not await _require_admin(callback):
        return

    async with async_session() as session:
        user_count = await session.scalar(select(func.count(User.id))) or 0
        movie_count = await session.scalar(select(func.count(Movie.id))) or 0
        total_views = await session.scalar(select(func.sum(Movie.views))) or 0
        top_movies = list(
            (
                await session.scalars(
                    select(Movie).order_by(Movie.views.desc()).limit(10)
                )
            ).all()
        )

    medals = ["🥇", "🥈", "🥉"]
    top_text = ""
    for i, mv in enumerate(top_movies, 1):
        icon = medals[i - 1] if i <= 3 else f"<b>{i}.</b>"
        top_text += f"{icon} <code>{mv.code}</code> — <b>{mv.views:,}</b> 👁\n"

    await callback.message.edit_text(
        "📊 <b>Bot Statistikasi</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"👥 Foydalanuvchilar: <b>{user_count:,}</b>\n"
        f"🎬 Kinolar: <b>{movie_count:,}</b>\n"
        f"👁 Jami ko'rishlar: <b>{total_views:,}</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🏆 <b>Top-10 eng ko'p ko'rilgan:</b>\n\n"
        f"{top_text or '❌ Hali ko\'rishlar yo\'q'}",
        reply_markup=back_admin_kb(),
        parse_mode="HTML",
    )


# ───────────────────────────────────────────────────────────
#  📢 REKLAMA TARQATISH
# ───────────────────────────────────────────────────────────
@router.callback_query(F.data == "broadcast")
async def cb_broadcast(callback: CallbackQuery, state: FSMContext) -> None:
    if not await _require_admin(callback):
        return
    await state.set_state(BroadcastStates.choosing_type)
    await callback.message.edit_text(
        "📢 <b>Reklama Tarqatish</b>\n\n"
        "📌 Qaysi turdagi reklama yubormoqchisiz?",
        reply_markup=broadcast_type_kb(),
        parse_mode="HTML",
    )


@router.callback_query(F.data.in_({"bc_text", "bc_photo", "bc_forward"}))
async def cb_broadcast_type(callback: CallbackQuery, state: FSMContext) -> None:
    type_map = {"bc_text": "text", "bc_photo": "photo", "bc_forward": "forward"}
    hints = {
        "text": "📝 Matn xabarini yuboring:",
        "photo": "🖼 Rasm (caption bilan yoki yalang'och) yuboring:",
        "forward": "📨 Forward qilmoqchi bo'lgan xabarni yuboring:",
    }
    bc_type = type_map[callback.data]
    await state.update_data(bc_type=bc_type)
    await state.set_state(BroadcastStates.waiting_content)
    await callback.message.edit_text(
        f"📢 <b>Reklama ({bc_type.upper()})</b>\n\n"
        f"{hints[bc_type]}\n\n"
        "⚠️ <i>Bu xabar BARCHA foydalanuvchilarga yuboriladi!</i>",
        reply_markup=cancel_kb(),
        parse_mode="HTML",
    )


@router.message(BroadcastStates.waiting_content)
async def broadcast_do_send(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    bc_type = data.get("bc_type", "text")
    await state.clear()

    async with async_session() as session:
        users = list((await session.scalars(select(User))).all())

    total = len(users)
    success = 0
    fail = 0

    status_msg = await message.answer(
        f"⏳ <b>Reklama tarqatilmoqda...</b>\n"
        f"👥 Jami: <b>{total:,}</b> foydalanuvchi",
        parse_mode="HTML",
    )

    for user in users:
        try:
            if bc_type == "text" and message.text:
                await message.bot.send_message(
                    user.user_id, message.text, parse_mode="HTML"
                )
            elif bc_type == "photo" and message.photo:
                await message.bot.send_photo(
                    user.user_id,
                    message.photo[-1].file_id,
                    caption=message.caption,
                )
            elif bc_type == "forward":
                await message.bot.forward_message(
                    user.user_id, message.chat.id, message.message_id
                )
            success += 1
        except (TelegramForbiddenError, TelegramBadRequest):
            fail += 1
        except Exception as e:
            logging.error(f"Broadcast xatosi [{user.user_id}]: {e}")
            fail += 1

        await asyncio.sleep(0.05)  # Telegram rate limit

    await status_msg.edit_text(
        "✅ <b>Reklama tarqatildi!</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"✉️ Muvaffaqiyatli: <b>{success:,}</b>\n"
        f"❌ Xatolik: <b>{fail:,}</b>",
        reply_markup=back_admin_kb(),
        parse_mode="HTML",
    )


# ───────────────────────────────────────────────────────────
#  📡 KANALLARNI BOSHQARISH
# ───────────────────────────────────────────────────────────
@router.callback_query(F.data == "manage_channels")
async def cb_manage_channels(callback: CallbackQuery) -> None:
    if not await _require_admin(callback):
        return
    channels = await get_all_channels()
    count_text = f"📋 Hozirda <b>{len(channels)}</b> ta majburiy kanal mavjud."
    await callback.message.edit_text(
        f"📡 <b>Kanallarni Boshqarish</b>\n\n{count_text}\n\n"
        "Quyidagi amallardan birini tanlang:",
        reply_markup=channels_panel_kb(),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "list_channels")
async def cb_list_channels(callback: CallbackQuery) -> None:
    channels = await get_all_channels()
    if not channels:
        await callback.answer("📋 Kanallar ro'yxati hozircha bo'sh!", show_alert=True)
        return

    text = "📋 <b>Majburiy Kanallar:</b>\n\n"
    for i, ch in enumerate(channels, 1):
        username = f"@{ch.channel_username}" if ch.channel_username else "—"
        text += (
            f"<b>{i}. {ch.channel_name}</b>\n"
            f"   🆔 <code>{ch.channel_id}</code>\n"
            f"   🔗 {username}\n\n"
        )

    await callback.message.edit_text(
        text, reply_markup=channels_panel_kb(), parse_mode="HTML"
    )


@router.callback_query(F.data == "add_channel")
async def cb_add_channel(callback: CallbackQuery, state: FSMContext) -> None:
    if not await _require_admin(callback):
        return
    await state.set_state(AddChannelStates.waiting_channel)
    await callback.message.edit_text(
        "📡 <b>Yangi Kanal Qo'shish</b>\n\n"
        "✅ Avval botni kanalga <b>admin</b> qiling!\n\n"
        "Keyin kanal <b>username</b> yoki <b>ID</b>ini yuboring:\n"
        "💡 Masalan: <code>@mychannel</code> yoki <code>-1001234567890</code>",
        reply_markup=cancel_kb(),
        parse_mode="HTML",
    )


@router.message(AddChannelStates.waiting_channel)
async def add_channel_process(message: Message, state: FSMContext) -> None:
    channel_input = message.text.strip()
    try:
        chat = await message.bot.get_chat(channel_input)
    except Exception:
        await message.answer(
            "❌ <b>Kanal topilmadi!</b>\n\n"
            "⚠️ Botni kanalga admin qilganingizni tekshiring.\n"
            "🔄 Qaytadan urinib ko'ring:",
            reply_markup=cancel_kb(),
            parse_mode="HTML",
        )
        return

    async with async_session() as session:
        existing = await session.scalar(
            select(Channel).where(Channel.channel_id == chat.id)
        )
        if existing:
            await message.answer(
                "⚠️ <b>Bu kanal allaqachon qo'shilgan!</b>",
                reply_markup=back_admin_kb(),
                parse_mode="HTML",
            )
            await state.clear()
            return

        session.add(
            Channel(
                channel_id=chat.id,
                channel_username=chat.username,
                channel_name=chat.title or "Nomsiz kanal",
            )
        )
        await session.commit()

    await state.clear()
    await message.answer(
        f"✅ <b>Kanal muvaffaqiyatli qo'shildi!</b>\n\n"
        f"📢 Nomi: <b>{chat.title}</b>\n"
        f"🆔 ID: <code>{chat.id}</code>",
        reply_markup=back_admin_kb(),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "remove_channel")
async def cb_remove_channel(callback: CallbackQuery, state: FSMContext) -> None:
    if not await _require_admin(callback):
        return
    channels = await get_all_channels()
    if not channels:
        await callback.answer("❌ O'chirish uchun kanal yo'q!", show_alert=True)
        return

    text = "🗑 <b>Kanal O'chirish</b>\n\nO'chirmoqchi bo'lgan kanalning ID sini yuboring:\n\n"
    for ch in channels:
        text += f"📢 <b>{ch.channel_name}</b> → <code>{ch.channel_id}</code>\n"

    await state.set_state(RemoveChannelStates.waiting_id)
    await callback.message.edit_text(text, reply_markup=cancel_kb(), parse_mode="HTML")


@router.message(RemoveChannelStates.waiting_id)
async def remove_channel_process(message: Message, state: FSMContext) -> None:
    try:
        channel_id = int(message.text.strip())
    except ValueError:
        await message.answer(
            "⚠️ <b>Iltimos, to'g'ri kanal ID kiriting!</b>",
            reply_markup=cancel_kb(),
            parse_mode="HTML",
        )
        return

    async with async_session() as session:
        channel = await session.scalar(
            select(Channel).where(Channel.channel_id == channel_id)
        )
        if not channel:
            await message.answer(
                f"❌ <b>{channel_id} ID li kanal topilmadi!</b>",
                reply_markup=cancel_kb(),
                parse_mode="HTML",
            )
            return

        ch_name = channel.channel_name
        await session.delete(channel)
        await session.commit()

    await state.clear()
    await message.answer(
        f"✅ <b>Kanal o'chirildi!</b>\n\n"
        f"📢 {ch_name} (<code>{channel_id}</code>)",
        reply_markup=back_admin_kb(),
        parse_mode="HTML",
    )


# ───────────────────────────────────────────────────────────
#  👑 ADMIN QO'SHISH (faqat SUPER_ADMIN)
# ───────────────────────────────────────────────────────────
@router.callback_query(F.data == "add_admin")
async def cb_add_admin(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.from_user.id != SUPER_ADMIN_ID:
        await callback.answer(
            "⛔ Faqat Super Admin yangi admin qo'sha oladi!", show_alert=True
        )
        return
    await state.set_state(AddAdminStates.waiting_user_id)
    await callback.message.edit_text(
        "👑 <b>Yangi Admin Qo'shish</b>\n\n"
        "Admin qilmoqchi bo'lgan foydalanuvchining\n"
        "<b>Telegram ID</b>ini yuboring:\n\n"
        "💡 ID ni bilish uchun @userinfobot dan foydalaning",
        reply_markup=cancel_kb(),
        parse_mode="HTML",
    )


@router.message(AddAdminStates.waiting_user_id)
async def add_admin_process(message: Message, state: FSMContext) -> None:
    try:
        new_id = int(message.text.strip())
    except ValueError:
        await message.answer(
            "⚠️ <b>Iltimos, to'g'ri Telegram ID kiriting!</b>",
            reply_markup=cancel_kb(),
            parse_mode="HTML",
        )
        return

    async with async_session() as session:
        existing = await session.scalar(
            select(Admin).where(Admin.user_id == new_id)
        )
        if existing:
            await message.answer(
                "⚠️ <b>Bu foydalanuvchi allaqachon admin!</b>",
                reply_markup=back_admin_kb(),
                parse_mode="HTML",
            )
            await state.clear()
            return

        session.add(Admin(user_id=new_id))
        await session.commit()

    await state.clear()
    await message.answer(
        f"✅ <b>Yangi admin qo'shildi!</b>\n"
        f"👑 ID: <code>{new_id}</code>",
        reply_markup=back_admin_kb(),
        parse_mode="HTML",
    )


# ───────────────────────────────────────────────────────────
#  ❌ BEKOR QILISH
# ───────────────────────────────────────────────────────────
@router.callback_query(F.data == "cancel")
async def cb_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    admin = await check_is_admin(callback.from_user.id)
    await callback.message.edit_text(
        "❌ <b>Amal bekor qilindi.</b>",
        reply_markup=admin_panel_kb() if admin else main_menu_kb(),
        parse_mode="HTML",
    )


# ═══════════════════════════════════════════════════════════
#  🚀  MAIN — Botni ishga tushirish
# ═══════════════════════════════════════════════════════════
async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    logging.info("🗄  Baza yaratilmoqda...")
    await init_db()
    logging.info("✅ Baza tayyor!")

    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())

    # Middleware ni Message va CallbackQuery uchun ro'yxatdan o'tkazish
    dp.message.outer_middleware(SubscriptionMiddleware())
    dp.callback_query.outer_middleware(SubscriptionMiddleware())

    dp.include_router(router)

    logging.info("🎬 Kino Bot ishga tushdi!")
    await dp.start_polling(bot, skip_updates=True)


if __name__ == "__main__":
    asyncio.run(main())
