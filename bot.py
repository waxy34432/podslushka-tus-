import asyncio
import logging
from datetime import datetime, timezone

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.exceptions import TelegramBadRequest

from config import BOT_TOKEN, OWNER_ID
import database as db

logging.basicConfig(level=logging.INFO)
dp = Dispatcher()
# Короткие состояния диалога для действий админ-панели.
admin_states: dict[int, str] = {}


def user_home():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✉️ Отправить предложку", callback_data="submit")]
    ])


def consent_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Согласен, продолжить", callback_data="consent")],
        [InlineKeyboardButton(text="↩️ Назад", callback_data="home")]
    ])


def admin_home():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📥 Предложки", callback_data="admin:queue")],
        [InlineKeyboardButton(text="📊 Статистика", callback_data="admin:stats"),
         InlineKeyboardButton(text="🔎 Поиск по ID", callback_data="admin:search")],
        [InlineKeyboardButton(text="🚫 Чёрный список", callback_data="admin:block_help")],
        [InlineKeyboardButton(text="⚙️ Настройки", callback_data="settings")]
    ])


def settings_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Каналы", callback_data="settings:channels")],
        [InlineKeyboardButton(text="👥 Администраторы", callback_data="settings:admins")],
        [InlineKeyboardButton(text="⬅️ В админ-панель", callback_data="admin:home")]
    ])


def channel_keyboard(channels):
    rows = []
    for channel in channels:
        mark = "⭐ " if channel["is_default"] else ""
        rows.append([InlineKeyboardButton(
            text=f"{mark}{channel['title']}",
            callback_data=f"channel:default:{channel['chat_id']}"
        )])
        rows.append([InlineKeyboardButton(
            text=f"🗑 Удалить: {channel['title']}",
            callback_data=f"channel:delete:{channel['chat_id']}"
        )])
    rows.append([InlineKeyboardButton(text="➕ Добавить канал", callback_data="channel:add")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="settings")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admins_keyboard(admins):
    rows = []
    for item in admins:
        rows.append([InlineKeyboardButton(
            text=f"❌ Удалить {item['user_id']}",
            callback_data=f"adminuser:remove:{item['user_id']}"
        )])
    rows.append([InlineKeyboardButton(text="➕ Добавить администратора", callback_data="adminuser:add")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="settings")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def moderation_keyboard(submission_id):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Одобрить и опубликовать", callback_data=f"moderate:publish:{submission_id}")],
        [InlineKeyboardButton(text="❌ Отклонить", callback_data=f"moderate:reject:{submission_id}")],
        [InlineKeyboardButton(text="📚 История отправителя", callback_data=f"moderate:history:{submission_id}")]
    ])


async def is_admin(user_id: int) -> bool:
    return user_id == OWNER_ID or await db.is_admin(user_id)


def welcome_text():
    return "Добро пожаловать в предложку!\n\nПодслушка тус по репосту 🖤\n\nНажми кнопку ниже, чтобы отправить."


async def send_admin_notice(bot: Bot, text: str, reply_markup=None):
    for admin_id in await db.get_admin_ids():
        try:
            await bot.send_message(admin_id, text, parse_mode="HTML", reply_markup=reply_markup)
        except Exception:
            logging.exception("Не удалось отправить уведомление администратору %s", admin_id)


@dp.message(CommandStart())
async def start(message: Message):
    await message.answer(welcome_text(), reply_markup=user_home())
    if await is_admin(message.from_user.id):
        await message.answer("Для открытия административной панели используй /admin.")


@dp.message(Command("admin"))
async def admin_command(message: Message):
    if not await is_admin(message.from_user.id):
        return
    await message.answer("🛠 Административная панель", reply_markup=admin_home())


@dp.callback_query(F.data == "home")
async def home_callback(call: CallbackQuery):
    await call.message.edit_text(welcome_text(), reply_markup=user_home())
    await call.answer()


@dp.callback_query(F.data == "submit")
async def submit_callback(call: CallbackQuery):
    if await db.is_blocked(call.from_user.id):
        await call.answer("Отправка предложек для этого аккаунта недоступна.", show_alert=True)
        return
    await call.message.edit_text(
        "Отправь сюда то что ты хотел бы видеть в канале",
        reply_markup=consent_keyboard()
    )
    await call.answer()


@dp.callback_query(F.data == "consent")
async def consent_callback(call: CallbackQuery):
    await db.grant_consent(call.from_user.id)
    await call.message.edit_text("Отправь одним сообщением текст, фото, видео, документ или голосовое сообщение.")
    await call.answer("Можно отправлять.")


@dp.callback_query(F.data == "admin:home")
async def admin_home_callback(call: CallbackQuery):
    if not await is_admin(call.from_user.id):
        await call.answer("Нет доступа", show_alert=True)
        return
    await call.message.edit_text("🛠 Административная панель", reply_markup=admin_home())
    await call.answer()


@dp.callback_query(F.data.startswith("admin:"))
async def admin_actions(call: CallbackQuery):
    if not await is_admin(call.from_user.id):
        await call.answer("Нет доступа", show_alert=True)
        return
    action = call.data.split(":", 1)[1]
    if action == "stats":
        s = await db.get_stats()
        await call.message.answer(
            f"📊 <b>Статистика</b>\nВсего: {s['total']}\nОжидают: {s['pending']}\n"
            f"Опубликовано: {s['published']}\nОтклонено: {s['rejected']}",
            parse_mode="HTML"
        )
    elif action == "queue":
        items = await db.get_pending(10)
        if not items:
            await call.message.answer("Новых предложек нет.")
        for item in items:
            await call.message.answer(
                f"📩 <b>Предложка №{item['id']}</b>\n"
                f"Отправитель: {item['full_name']}\n"
                f"Username: @{item['username'] or 'не указан'}\n"
                f"Telegram ID: <code>{item['user_id']}</code>\n"
                f"Дата: {item['created_at']}",
                parse_mode="HTML", reply_markup=moderation_keyboard(item["id"])
            )
    elif action == "search":
        admin_states[call.from_user.id] = "search_sender"
        await call.message.answer("Отправь Telegram ID пользователя отдельным сообщением.")
    elif action == "block_help":
        await call.message.answer("Команды:\n/block TELEGRAM_ID — заблокировать\n/unblock TELEGRAM_ID — разблокировать")
    await call.answer()


@dp.callback_query(F.data == "settings")
async def settings_callback(call: CallbackQuery):
    if not await is_admin(call.from_user.id):
        await call.answer("Нет доступа", show_alert=True)
        return
    await call.message.edit_text("⚙️ Настройки бота", reply_markup=settings_keyboard())
    await call.answer()


@dp.callback_query(F.data == "settings:channels")
async def channels_callback(call: CallbackQuery):
    if not await is_admin(call.from_user.id):
        await call.answer("Нет доступа", show_alert=True)
        return
    channels = await db.get_channels()
    if not channels:
        text = "Пока не добавлено ни одного канала.\n\nСначала добавь бота администратором канала с правом публикации, затем нажми «Добавить канал»."
    else:
        text = "📢 Подключённые каналы\n\nНажми на канал, чтобы сделать его основным. ⭐ — основной канал."
    await call.message.edit_text(text, reply_markup=channel_keyboard(channels))
    await call.answer()


@dp.callback_query(F.data == "channel:add")
async def add_channel_start(call: CallbackQuery):
    if call.from_user.id != OWNER_ID:
        await call.answer("Добавлять каналы может только владелец.", show_alert=True)
        return
    admin_states[call.from_user.id] = "add_channel"
    await call.message.answer(
        "Отправь @username канала или его числовой ID.\n"
        "Важно: предварительно добавь этого бота в канал администратором с правом публикации."
    )
    await call.answer()


@dp.callback_query(F.data.startswith("channel:default:"))
async def set_default_channel(call: CallbackQuery):
    if call.from_user.id != OWNER_ID:
        await call.answer("Только владелец может менять каналы.", show_alert=True)
        return
    chat_id = int(call.data.rsplit(":", 1)[1])
    await db.set_default_channel(chat_id)
    await call.answer("Основной канал обновлён.")
    channels = await db.get_channels()
    await call.message.edit_reply_markup(reply_markup=channel_keyboard(channels))


@dp.callback_query(F.data.startswith("channel:delete:"))
async def delete_channel_callback(call: CallbackQuery):
    if call.from_user.id != OWNER_ID:
        await call.answer("Только владелец может удалять каналы.", show_alert=True)
        return
    chat_id = int(call.data.rsplit(":", 1)[1])
    await db.delete_channel(chat_id)
    await call.answer("Канал удалён.")
    channels = await db.get_channels()
    await call.message.edit_reply_markup(reply_markup=channel_keyboard(channels))


@dp.callback_query(F.data == "settings:admins")
async def admins_callback(call: CallbackQuery):
    if call.from_user.id != OWNER_ID:
        await call.answer("Управлять администраторами может только владелец.", show_alert=True)
        return
    admins = await db.get_admins()
    await call.message.edit_text(
        "👥 Администраторы\n\nВладелец: <code>{}</code>\n\nДополнительные администраторы:".format(OWNER_ID),
        parse_mode="HTML", reply_markup=admins_keyboard(admins)
    )
    await call.answer()


@dp.callback_query(F.data == "adminuser:add")
async def add_admin_start(call: CallbackQuery):
    if call.from_user.id != OWNER_ID:
        await call.answer("Только владелец может добавлять администраторов.", show_alert=True)
        return
    admin_states[call.from_user.id] = "add_admin"
    await call.message.answer("Отправь Telegram ID нового администратора (только число).")
    await call.answer()


@dp.callback_query(F.data.startswith("adminuser:remove:"))
async def remove_admin_callback(call: CallbackQuery):
    if call.from_user.id != OWNER_ID:
        await call.answer("Только владелец может удалять администраторов.", show_alert=True)
        return
    uid = int(call.data.rsplit(":", 1)[1])
    await db.remove_admin(uid)
    await call.answer("Администратор удалён.")
    admins = await db.get_admins()
    await call.message.edit_reply_markup(reply_markup=admins_keyboard(admins))


@dp.callback_query(F.data.startswith("moderate:"))
async def moderate_callback(call: CallbackQuery, bot: Bot):
    if not await is_admin(call.from_user.id):
        await call.answer("Нет доступа", show_alert=True)
        return
    _, action, raw_id = call.data.split(":")
    sid = int(raw_id)
    item = await db.get_submission(sid)
    if not item:
        await call.answer("Предложка не найдена.", show_alert=True)
        return
    if action == "history":
        rows = await db.sender_history(item["user_id"])
        lines = [f"№{r['id']} — {r['created_at']} — {r['status']}" for r in rows]
        await call.message.answer(
            f"История отправителя <code>{item['user_id']}</code>:\n" + ("\n".join(lines) or "Истории пока нет."),
            parse_mode="HTML"
        )
        await call.answer()
        return
    if item["status"] != "pending":
        await call.answer("Эта предложка уже обработана.", show_alert=True)
        return
    if action == "publish":
        channel = await db.get_default_channel()
        if not channel:
            await call.answer("Сначала владелец должен добавить канал в настройках.", show_alert=True)
            return
        try:
            await bot.copy_message(channel["chat_id"], item["chat_id"], item["message_id"])
            await db.set_submission_status(sid, "published", call.from_user.id)
            await call.message.edit_reply_markup(reply_markup=None)
            await call.answer("Опубликовано!")
        except TelegramBadRequest:
            logging.exception("Не удалось опубликовать")
            await call.answer("Не удалось опубликовать. Проверь права бота в канале.", show_alert=True)
    elif action == "reject":
        await db.set_submission_status(sid, "rejected", call.from_user.id)
        await call.message.edit_reply_markup(reply_markup=None)
        await call.answer("Отклонено.")


@dp.message(Command("block"))
async def block_command(message: Message):
    if not await is_admin(message.from_user.id):
        return
    try:
        uid = int(message.text.split(maxsplit=1)[1])
        await db.set_blocked(uid, True)
        await message.answer(f"Пользователь {uid} добавлен в чёрный список.")
    except Exception:
        await message.answer("Формат: /block TELEGRAM_ID")


@dp.message(Command("unblock"))
async def unblock_command(message: Message):
    if not await is_admin(message.from_user.id):
        return
    try:
        uid = int(message.text.split(maxsplit=1)[1])
        await db.set_blocked(uid, False)
        await message.answer(f"Пользователь {uid} разблокирован.")
    except Exception:
        await message.answer("Формат: /unblock TELEGRAM_ID")


@dp.message(F.chat.type == "private")
async def private_message_handler(message: Message, bot: Bot):
    uid = message.from_user.id

    # Сначала обрабатываем ожидаемый ввод владельца/администратора.
    state = admin_states.get(uid)
    if state and uid == OWNER_ID:
        value = (message.text or "").strip()
        if state == "add_admin":
            if not value.isdigit():
                await message.answer("Нужен числовой Telegram ID. Попробуй ещё раз.")
                return
            new_id = int(value)
            if new_id == OWNER_ID:
                await message.answer("Владелец уже имеет полный доступ.")
            else:
                await db.add_admin(new_id)
                await message.answer(f"Администратор {new_id} добавлен.")
                try:
                    await bot.send_message(new_id, "Тебе выдан доступ администратора. Напиши /admin для открытия панели.")
                except Exception:
                    await message.answer("Пользователь добавлен, но бот не смог написать ему первым. Пусть он сначала запустит бота командой /start.")
            admin_states.pop(uid, None)
            return
        if state == "add_channel":
            if not value:
                await message.answer("Отправь @username или числовой ID канала.")
                return
            try:
                chat = await bot.get_chat(value)
                me = await bot.get_me()
                member = await bot.get_chat_member(chat.id, me.id)
                if member.status not in ("administrator", "creator"):
                    await message.answer("Бот не является администратором этого канала. Добавь его администратором и повтори.")
                    return
                if getattr(member, "can_post_messages", True) is False:
                    await message.answer("У бота нет права публиковать сообщения. Выдай это право и повтори.")
                    return
                await db.add_channel(chat.id, chat.title or str(chat.id), getattr(chat, "username", None))
                await message.answer(f"Канал добавлен: <b>{chat.title}</b> (<code>{chat.id}</code>).", parse_mode="HTML")
            except Exception as exc:
                logging.exception("Ошибка добавления канала")
                await message.answer("Не удалось добавить канал. Проверь username/ID, права бота и попробуй ещё раз.")
                return
            admin_states.pop(uid, None)
            return
        if state == "search_sender":
            if not value.isdigit():
                await message.answer("Нужен числовой Telegram ID.")
                return
            rows = await db.sender_history(int(value))
            if not rows:
                await message.answer("Обращений от этого ID не найдено.")
            else:
                lines = [f"№{r['id']} — {r['created_at']} — {r['status']}" for r in rows]
                await message.answer("История отправителя:\n" + "\n".join(lines))
            admin_states.pop(uid, None)
            return

    # Администраторы не отправляют обычные сообщения в очередь предложек.
    if await is_admin(uid):
        return
    if await db.is_blocked(uid):
        await message.answer("Отправка предложек для этого аккаунта недоступна.")
        return
    if not await db.has_consent(uid):
        await message.answer(
            "Сначала нажми «Отправить предложку» и подтверди ознакомление с условиями обработки данных.",
            reply_markup=user_home()
        )
        return

    sid = await db.add_submission(message)
    await message.answer(f"Предложка №{sid} отправлена на модерацию. Спасибо!")
    admin_text = (
        f"📩 <b>Новая предложка №{sid}</b>\n\n"
        f"<b>Отправитель:</b> {message.from_user.full_name}\n"
        f"<b>Username:</b> @{message.from_user.username or 'не указан'}\n"
        f"<b>Telegram ID:</b> <code>{uid}</code>\n"
        f"<b>Дата:</b> {message.date.astimezone(timezone.utc):%d.%m.%Y %H:%M} UTC"
    )
    await send_admin_notice(bot, admin_text)
    for admin_id in await db.get_admin_ids():
        try:
            await bot.copy_message(admin_id, message.chat.id, message.message_id, reply_markup=None)
            # Attach moderation buttons to a separate card with sender details.
            # Buttons are sent on the details card to keep the media/text copy intact.
            await bot.send_message(admin_id, f"Действия для предложки №{sid}:", reply_markup=moderation_keyboard(sid))
        except Exception:
            logging.exception("Не удалось доставить копию предложки администратору %s", admin_id)


async def main():
    if not BOT_TOKEN:
        raise RuntimeError("Не задан BOT_TOKEN")
    if not OWNER_ID:
        raise RuntimeError("Не задан OWNER_ID")
    await db.init_db()
    bot = Bot(BOT_TOKEN)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
