"""
Бот-предложка (aiogram 3 + SQLite).

Запуск:
    pip install -r requirements.txt
    BOT_TOKEN=... OWNER_IDS=123456789 python bot.py
"""
import asyncio
import html
import json
import logging
import math
import os
import re
from datetime import datetime

import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatMemberStatus, ChatType, ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command, CommandStart, Filter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    MessageOriginChannel,
    TelegramObject,
)

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

# ───────────────────────── конфиг ─────────────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
# ID владельцев (через запятую). Они всегда админы и их нельзя удалить.
OWNER_IDS = {int(x) for x in re.findall(r"\d+", os.getenv("OWNER_IDS", ""))}
DB_PATH = os.getenv("DB_PATH", "suggest.db")
PAGE = 8

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("suggest_bot")

TYPE_RU = {
    "text": "Текст", "photo": "Фото", "video": "Видео", "animation": "GIF",
    "audio": "Аудио", "voice": "Голосовое", "video_note": "Кружок",
    "document": "Файл", "sticker": "Стикер", "poll": "Опрос",
    "location": "Геопозиция", "venue": "Место", "contact": "Контакт",
    "dice": "Кубик", "album": "Альбом",
}
EMO = {"pending": "⏳", "approved": "✅", "rejected": "❌"}

db: aiosqlite.Connection


# ───────────────────────── база ─────────────────────────
SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
    user_id INTEGER PRIMARY KEY, username TEXT, full_name TEXT, joined_at TEXT);
CREATE TABLE IF NOT EXISTS admins(
    user_id INTEGER PRIMARY KEY, username TEXT, added_by INTEGER, added_at TEXT);
CREATE TABLE IF NOT EXISTS pending_admins(
    username TEXT PRIMARY KEY, added_by INTEGER, added_at TEXT);
CREATE TABLE IF NOT EXISTS channels(
    id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER UNIQUE,
    title TEXT, username TEXT, added_at TEXT);
CREATE TABLE IF NOT EXISTS suggestions(
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, chat_id INTEGER,
    message_ids TEXT, content_type TEXT, text TEXT,
    status TEXT DEFAULT 'pending', created_at TEXT,
    decided_by INTEGER, decided_at TEXT, note TEXT);
CREATE TABLE IF NOT EXISTS admin_msgs(
    id INTEGER PRIMARY KEY AUTOINCREMENT, suggestion_id INTEGER,
    admin_id INTEGER, control_msg_id INTEGER);
"""


async def init_db():
    global db
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    await db.executescript(SCHEMA)
    await db.commit()


async def q_one(sql, args=()):
    async with db.execute(sql, args) as cur:
        return await cur.fetchone()


async def q_all(sql, args=()):
    async with db.execute(sql, args) as cur:
        return await cur.fetchall()


async def run(sql, args=()):
    cur = await db.execute(sql, args)
    await db.commit()
    return cur


# ───────────────────────── утилиты ─────────────────────────
def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def fmt_dt(s, short=False) -> str:
    if not s:
        return "—"
    try:
        d = datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return s
    return d.strftime("%d.%m %H:%M" if short else "%d.%m.%Y %H:%M")


def esc(s) -> str:
    return html.escape(str(s or ""))


def who(uid, username, full_name=None) -> str:
    name = f"@{esc(username)}" if username else esc(full_name or "без username")
    return f"{name} · <code>{uid}</code>"


def btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def kb(rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


def pager(prefix: str, page: int, pages: int):
    row = []
    if page > 0:
        row.append(btn("⬅️", f"{prefix}:{page - 1}"))
    row.append(btn(f"{page + 1}/{pages}", "adm:noop"))
    if page < pages - 1:
        row.append(btn("➡️", f"{prefix}:{page + 1}"))
    return row


async def is_admin(uid: int) -> bool:
    if uid in OWNER_IDS:
        return True
    return await q_one("SELECT 1 FROM admins WHERE user_id=?", (uid,)) is not None


async def admin_ids() -> list[int]:
    rows = await q_all("SELECT user_id FROM admins")
    return list(OWNER_IDS | {r["user_id"] for r in rows})


async def upsert_user(u):
    await db.execute(
        "INSERT INTO users(user_id,username,full_name,joined_at) VALUES(?,?,?,?) "
        "ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, full_name=excluded.full_name",
        (u.id, u.username, u.full_name, now()),
    )
    # если этого человека заранее добавили в админы по @username — активируем
    if u.username:
        low = u.username.lower()
        if await q_one("SELECT 1 FROM pending_admins WHERE lower(username)=?", (low,)):
            await db.execute(
                "INSERT OR IGNORE INTO admins(user_id,username,added_by,added_at) VALUES(?,?,?,?)",
                (u.id, u.username, None, now()),
            )
            await db.execute("DELETE FROM pending_admins WHERE lower(username)=?", (low,))
    await db.commit()


async def get_sug(sid: int):
    return await q_one(
        "SELECT s.*, u.username, u.full_name FROM suggestions s "
        "LEFT JOIN users u ON u.user_id=s.user_id WHERE s.id=?",
        (sid,),
    )


def render_info(s, extra: str = "") -> str:
    ids = json.loads(s["message_ids"])
    ctype = TYPE_RU.get(s["content_type"], s["content_type"])
    if s["content_type"] == "album":
        ctype += f" ({len(ids)})"
    text = s["text"] or ""
    out = (
        f"📨 <b>Предложка #{s['id']}</b>\n"
        f"👤 {who(s['user_id'], s['username'], s['full_name'])}\n"
        f"🗂 Тип: {ctype}\n"
        f"🕒 {fmt_dt(s['created_at'])}"
    )
    if text:
        out += f"\n\n📝 <i>Текст:</i>\n{esc(text[:700])}{'…' if len(text) > 700 else ''}"
    if extra:
        out += f"\n\n{extra}"
    return out


async def status_line(s) -> str:
    if s["status"] == "pending":
        return "⏳ Ожидает решения"
    adm = await q_one("SELECT username, full_name FROM users WHERE user_id=?", (s["decided_by"],))
    mod = who(s["decided_by"], adm["username"] if adm else None, adm["full_name"] if adm else None)
    head = "✅ Одобрено" if s["status"] == "approved" else "❌ Отклонено"
    line = f"{head} · {fmt_dt(s['decided_at'])}\n👮 {mod}"
    if s["note"]:
        line += "\n" + esc(s["note"])
    return line


async def show(c: CallbackQuery, text: str, markup=None):
    try:
        await c.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as e:
        if "not modified" not in str(e):
            await c.message.answer(text, reply_markup=markup)


async def notify_user(bot: Bot, uid: int, text: str):
    try:
        await bot.send_message(uid, text, reply_markup=kb([[btn("✍️ Написать ещё", "sug:write")]]))
    except TelegramAPIError:
        pass


# ───────────────────────── состояния и фильтры ─────────────────────────
class Suggest(StatesGroup):
    waiting = State()


class AdminS(StatesGroup):
    add_channel = State()
    add_admin = State()


class IsAdmin(Filter):
    async def __call__(self, event: TelegramObject) -> bool:
        user = getattr(event, "from_user", None)
        return bool(user) and await is_admin(user.id)


base = Router()
admin_router = Router()
user_router = Router()
base.message.filter(F.chat.type == ChatType.PRIVATE)
user_router.message.filter(F.chat.type == ChatType.PRIVATE)
admin_router.message.filter(IsAdmin())
admin_router.callback_query.filter(IsAdmin())


# ───────────────────────── команды ─────────────────────────
def start_kb(admin: bool) -> InlineKeyboardMarkup:
    rows = [[btn("✍️ Написать предложку", "sug:write")]]
    if admin:
        rows.append([btn("🛠 Админ-панель", "adm:home")])
    return kb(rows)


@base.message(CommandStart())
async def cmd_start(m: Message, state: FSMContext):
    await state.clear()
    await upsert_user(m.from_user)
    await m.answer(
        "👋 Привет! Здесь можно отправить свою предложку в канал.\n\n"
        "Нажми кнопку ниже и пришли что угодно: текст, фото, видео, "
        "голосовое, файл, опрос — модераторы всё посмотрят.",
        reply_markup=start_kb(await is_admin(m.from_user.id)),
    )


@base.message(Command("cancel"))
async def cmd_cancel(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("Отменено.", reply_markup=start_kb(await is_admin(m.from_user.id)))


@base.message(Command("admin"))
async def cmd_admin(m: Message, state: FSMContext):
    await state.clear()
    if not await is_admin(m.from_user.id):
        return await m.answer("⛔ Нет доступа.")
    await upsert_user(m.from_user)
    text, markup = await v_home()
    await m.answer(text, reply_markup=markup)


# ───────────────────────── пользователь: предложка ─────────────────────────
album_buf: dict[str, list[Message]] = {}


@user_router.callback_query(F.data == "sug:write")
async def sug_write(c: CallbackQuery, state: FSMContext):
    await upsert_user(c.from_user)
    await state.set_state(Suggest.waiting)
    await c.answer()
    await c.message.answer(
        "✍️ Отправь свою предложку одним сообщением.\n"
        "Подойдёт всё: текст, фото, видео, голосовое, файл, стикер, опрос. "
        "Альбом из нескольких фото/видео тоже можно.",
        reply_markup=kb([[btn("✖️ Отмена", "sug:cancel")]]),
    )


@user_router.callback_query(F.data == "sug:cancel")
async def sug_cancel(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await c.answer("Отменено")
    await c.message.answer("Хорошо, отменил.", reply_markup=start_kb(await is_admin(c.from_user.id)))


@user_router.message(Suggest.waiting)
async def sug_receive(m: Message, state: FSMContext, bot: Bot):
    if m.media_group_id:
        key = f"{m.chat.id}:{m.media_group_id}"
        if key in album_buf:
            album_buf[key].append(m)
            return
        album_buf[key] = [m]
        await asyncio.sleep(1.5)  # ждём остальные части альбома
        msgs = album_buf.pop(key)
        await state.clear()
        await submit(bot, msgs)
        return
    await state.clear()
    await submit(bot, [m])


def mod_kb(sid: int) -> InlineKeyboardMarkup:
    return kb([[btn("✅ Опубликовать", f"mod:ok:{sid}"), btn("❌ Отклонить", f"mod:no:{sid}")]])


async def submit(bot: Bot, msgs: list[Message]):
    msgs.sort(key=lambda x: x.message_id)
    first = msgs[0]
    await upsert_user(first.from_user)

    text = ""
    for x in msgs:
        if x.poll:
            text = x.poll.question
            break
        if x.text or x.caption:
            text = x.text or x.caption
            break
    ctype = "album" if len(msgs) > 1 else getattr(first.content_type, "value", first.content_type)
    ids = [x.message_id for x in msgs]

    cur = await run(
        "INSERT INTO suggestions(user_id,chat_id,message_ids,content_type,text,created_at) VALUES(?,?,?,?,?,?)",
        (first.from_user.id, first.chat.id, json.dumps(ids), ctype, text, now()),
    )
    sid = cur.lastrowid
    info = render_info(await get_sug(sid))

    delivered = 0
    for admin in await admin_ids():
        try:
            res = await bot.copy_messages(admin, first.chat.id, ids)
            ctrl = await bot.send_message(
                admin, info, reply_markup=mod_kb(sid), reply_to_message_id=res[0].message_id
            )
            await run(
                "INSERT INTO admin_msgs(suggestion_id,admin_id,control_msg_id) VALUES(?,?,?)",
                (sid, admin, ctrl.message_id),
            )
            delivered += 1
        except TelegramAPIError as e:
            log.warning("Не доставлено админу %s: %s", admin, e)

    if not delivered:
        await run("DELETE FROM suggestions WHERE id=?", (sid,))
        await bot.send_message(
            first.chat.id,
            "😕 Не получилось отправить предложку модераторам. Такой тип сообщения "
            "может не поддерживаться (например, викторина) — попробуй другое.",
            reply_markup=kb([[btn("✍️ Попробовать снова", "sug:write")]]),
        )
        return

    await bot.send_message(
        first.chat.id,
        "✅ Предложка отправлена на модерацию. Я сообщу, когда её рассмотрят.",
        reply_markup=kb([[btn("✍️ Написать ещё", "sug:write")]]),
    )


# ───────────────────────── модерация ─────────────────────────
async def close_admin_msgs(bot: Bot, sid: int):
    s = await get_sug(sid)
    text = render_info(s, await status_line(s))
    for r in await q_all("SELECT * FROM admin_msgs WHERE suggestion_id=?", (sid,)):
        try:
            await bot.edit_message_text(
                text, chat_id=r["admin_id"], message_id=r["control_msg_id"], reply_markup=None
            )
        except TelegramAPIError:
            pass


async def do_publish(c: CallbackQuery, bot: Bot, sid: int, chans: list):
    s = await get_sug(sid)
    claim = await run(
        "UPDATE suggestions SET status='approved', decided_by=?, decided_at=? WHERE id=? AND status='pending'",
        (c.from_user.id, now(), sid),
    )
    if claim.rowcount == 0:
        return await c.answer("Уже обработано другим модератором", show_alert=True)

    ids = json.loads(s["message_ids"])
    done, errors = [], []
    for ch in chans:
        try:
            await bot.copy_messages(ch["chat_id"], s["chat_id"], ids)
            done.append(ch["title"])
        except TelegramAPIError as e:
            errors.append(f"{ch['title']}: {e.message}")

    if not done:
        await run("UPDATE suggestions SET status='pending', decided_by=NULL, decided_at=NULL WHERE id=?", (sid,))
        return await c.answer(("Не удалось опубликовать:\n" + "\n".join(errors))[:190], show_alert=True)

    note = "Опубликовано в: " + ", ".join(done)
    if errors:
        note += "\n⚠️ Ошибки: " + "; ".join(errors)
    await run("UPDATE suggestions SET note=? WHERE id=?", (note, sid))
    await close_admin_msgs(bot, sid)
    await notify_user(bot, s["user_id"], "✅ Твоя предложка опубликована!")
    await c.answer("Опубликовано")


@admin_router.callback_query(F.data.startswith("mod:"))
async def mod_cb(c: CallbackQuery, bot: Bot):
    p = c.data.split(":")
    action, sid = p[1], int(p[2])
    s = await get_sug(sid)
    if not s:
        return await c.answer("Предложка не найдена", show_alert=True)
    if s["status"] != "pending":
        await c.answer("Уже обработано", show_alert=True)
        return await close_admin_msgs(bot, sid)

    if action == "back":
        await c.message.edit_reply_markup(reply_markup=mod_kb(sid))
        return await c.answer()

    if action == "no":
        claim = await run(
            "UPDATE suggestions SET status='rejected', decided_by=?, decided_at=? WHERE id=? AND status='pending'",
            (c.from_user.id, now(), sid),
        )
        if claim.rowcount == 0:
            return await c.answer("Уже обработано", show_alert=True)
        await close_admin_msgs(bot, sid)
        await notify_user(bot, s["user_id"], "❌ К сожалению, твою предложку отклонили.")
        return await c.answer("Отклонено")

    chans = await q_all("SELECT * FROM channels ORDER BY id")
    if action == "ok":
        if not chans:
            return await c.answer("Сначала подключи канал: /admin → Каналы", show_alert=True)
        if len(chans) == 1:
            return await do_publish(c, bot, sid, chans)
        rows = [[btn(f"📢 {ch['title'][:40]}", f"mod:pub:{sid}:{ch['id']}")] for ch in chans]
        rows.append([btn("📢 Во все каналы", f"mod:pub:{sid}:all")])
        rows.append([btn("⬅️ Назад", f"mod:back:{sid}")])
        await c.message.edit_reply_markup(reply_markup=kb(rows))
        return await c.answer("Выбери канал")

    if action == "pub":
        target = p[3]
        if target == "all":
            return await do_publish(c, bot, sid, chans)
        chosen = [ch for ch in chans if str(ch["id"]) == target]
        if not chosen:
            return await c.answer("Канал уже отключён", show_alert=True)
        return await do_publish(c, bot, sid, chosen)


# ───────────────────────── админ-панель: экраны ─────────────────────────
async def v_home():
    total = (await q_one("SELECT COUNT(*) c FROM suggestions"))["c"]
    pend = (await q_one("SELECT COUNT(*) c FROM suggestions WHERE status='pending'"))["c"]
    users = (await q_one("SELECT COUNT(*) c FROM users"))["c"]
    chans = (await q_one("SELECT COUNT(*) c FROM channels"))["c"]
    text = (
        "🛠 <b>Админ-панель</b>\n\n"
        f"📢 Каналов: {chans}\n👥 Пользователей: {users}\n"
        f"📨 Предложек: {total} (⏳ ждут: {pend})"
    )
    return text, kb([
        [btn("📢 Каналы", "adm:channels"), btn("👮 Админы", "adm:admins")],
        [btn("📚 История предложек", "adm:hist:0")],
        [btn("👥 Пользователи", "adm:users:0")],
    ])


async def v_channels():
    rows = await q_all("SELECT * FROM channels ORDER BY id")
    lines = [
        f"• {esc(r['title'])}" + (f" (@{esc(r['username'])})" if r["username"] else "")
        for r in rows
    ]
    text = "📢 <b>Подключённые каналы</b>\n\n" + ("\n".join(lines) or "Пока нет ни одного канала.")
    buttons = [[btn(f"🗑 {r['title'][:30]}", f"adm:chdel:{r['id']}")] for r in rows]
    buttons.append([btn("➕ Добавить канал", "adm:chadd")])
    buttons.append([btn("⬅️ Назад", "adm:home")])
    return text, kb(buttons)


async def v_admins():
    admins = await q_all("SELECT * FROM admins ORDER BY added_at")
    pend = await q_all("SELECT * FROM pending_admins ORDER BY added_at")
    lines = [f"👑 <code>{uid}</code> (владелец)" for uid in sorted(OWNER_IDS)]
    buttons = []
    for a in admins:
        if a["user_id"] in OWNER_IDS:
            continue
        lines.append(f"👮 {who(a['user_id'], a['username'])}")
        label = f"@{a['username']}" if a["username"] else str(a["user_id"])
        buttons.append([btn(f"🗑 {label}", f"adm:adel:{a['user_id']}")])
    for pnd in pend:
        lines.append(f"⏳ @{esc(pnd['username'])} — ждёт, пока нажмёт /start")
        buttons.append([btn(f"🗑 @{pnd['username']}", f"adm:pdel:{pnd['username']}")])
    buttons.append([btn("➕ Добавить админа", "adm:adadd")])
    buttons.append([btn("⬅️ Назад", "adm:home")])
    return "👮 <b>Администраторы</b>\n\n" + "\n".join(lines), kb(buttons)


async def v_history(page: int):
    total = (await q_one("SELECT COUNT(*) c FROM suggestions"))["c"]
    pages = max(1, math.ceil(total / PAGE))
    page = min(max(page, 0), pages - 1)
    rows = await q_all(
        "SELECT s.id, s.status, s.created_at, s.user_id, u.username FROM suggestions s "
        "LEFT JOIN users u ON u.user_id=s.user_id ORDER BY s.id DESC LIMIT ? OFFSET ?",
        (PAGE, page * PAGE),
    )
    text = f"📚 <b>История предложек</b> — всего {total}"
    if not rows:
        text += "\n\nПока пусто."
    buttons = []
    for r in rows:
        name = f"@{r['username']}" if r["username"] else str(r["user_id"])
        buttons.append([btn(f"{EMO[r['status']]} #{r['id']} · {name} · {fmt_dt(r['created_at'], True)}",
                            f"adm:sug:{r['id']}:h{page}")])
    buttons.append(pager("adm:hist", page, pages))
    buttons.append([btn("⬅️ Назад", "adm:home")])
    return text, kb(buttons)


async def v_sug(sid: int, ctx: str):
    s = await get_sug(sid)
    if not s:
        return "Предложка не найдена.", kb([[btn("⬅️ Назад", "adm:hist:0")]])
    if ctx.startswith("u"):
        back = btn("⬅️ Назад", f"adm:user:{ctx[1:]}")
    else:
        back = btn("⬅️ Назад", f"adm:hist:{ctx[1:] or 0}")
    text = render_info(s, await status_line(s))
    return text, kb([
        [btn("👁 Показать содержимое", f"adm:show:{sid}")],
        [btn("👤 Профиль пользователя", f"adm:user:{s['user_id']}")],
        [back],
    ])


async def v_users(page: int):
    total = (await q_one("SELECT COUNT(*) c FROM users"))["c"]
    pages = max(1, math.ceil(total / PAGE))
    page = min(max(page, 0), pages - 1)
    rows = await q_all(
        "SELECT u.*, (SELECT COUNT(*) FROM suggestions s WHERE s.user_id=u.user_id) cnt "
        "FROM users u ORDER BY u.joined_at DESC, u.user_id DESC LIMIT ? OFFSET ?",
        (PAGE, page * PAGE),
    )
    text = f"👥 <b>Пользователи</b> — всего {total}"
    if not rows:
        text += "\n\nПока никого."
    buttons = []
    for r in rows:
        name = f"@{r['username']}" if r["username"] else (r["full_name"] or str(r["user_id"]))
        buttons.append([btn(f"{name} · 📨{r['cnt']} · {fmt_dt(r['joined_at'], True)}", f"adm:user:{r['user_id']}")])
    buttons.append(pager("adm:users", page, pages))
    buttons.append([btn("⬅️ Назад", "adm:home")])
    return text, kb(buttons)


async def v_user(uid: int):
    u = await q_one("SELECT * FROM users WHERE user_id=?", (uid,))
    if not u:
        return "Пользователь не найден.", kb([[btn("⬅️ Назад", "adm:users:0")]])
    sugs = await q_all("SELECT * FROM suggestions WHERE user_id=? ORDER BY id DESC", (uid,))
    cnt = {k: sum(1 for s in sugs if s["status"] == k) for k in EMO}
    text = (
        f"👤 <b>{who(uid, u['username'], u['full_name'])}</b>\n"
        f"Имя: {esc(u['full_name'])}\n"
        f"📅 Вошёл: {fmt_dt(u['joined_at'])}\n"
        f"📨 Предложек: {len(sugs)} (✅ {cnt['approved']} · ❌ {cnt['rejected']} · ⏳ {cnt['pending']})"
    )
    buttons = []
    if sugs:
        text += "\n\n<b>Последние предложки:</b>"
    for s in sugs[:10]:
        preview = (s["text"] or "").replace("\n", " ")[:40] or f"[{TYPE_RU.get(s['content_type'], s['content_type'])}]"
        text += f"\n{EMO[s['status']]} #{s['id']} · {fmt_dt(s['created_at'], True)} — {esc(preview)}"
        buttons.append([btn(f"{EMO[s['status']]} #{s['id']} · {preview[:30]}", f"adm:sug:{s['id']}:u{uid}")])
    buttons.append([btn("⬅️ Назад", "adm:users:0")])
    return text, kb(buttons)


# ───────────────────────── админ-панель: действия ─────────────────────────
@admin_router.callback_query(F.data.startswith("adm:"))
async def adm_cb(c: CallbackQuery, state: FSMContext, bot: Bot):
    p = c.data.split(":")
    act = p[1]
    if act == "noop":
        return await c.answer()
    await state.clear()
    await c.answer()

    if act == "home":
        await show(c, *await v_home())
    elif act == "channels":
        await show(c, *await v_channels())
    elif act == "chadd":
        await state.set_state(AdminS.add_channel)
        await show(
            c,
            "➕ <b>Добавление канала</b>\n\n"
            "1. Добавь бота в канал админом с правом публикации.\n"
            "2. Перешли сюда любое сообщение из канала или отправь @username / ID канала.",
            kb([[btn("✖️ Отмена", "adm:channels")]]),
        )
    elif act == "chdel":
        await run("DELETE FROM channels WHERE id=?", (int(p[2]),))
        await show(c, *await v_channels())
    elif act == "admins":
        await show(c, *await v_admins())
    elif act == "adadd":
        await state.set_state(AdminS.add_admin)
        await show(
            c,
            "➕ <b>Добавление админа</b>\n\nОтправь @username или числовой ID пользователя.\n"
            "Пользователь должен нажать /start в этом боте, иначе предложки ему не придут.",
            kb([[btn("✖️ Отмена", "adm:admins")]]),
        )
    elif act == "adel":
        uid = int(p[2])
        if uid not in OWNER_IDS:
            await run("DELETE FROM admins WHERE user_id=?", (uid,))
        await show(c, *await v_admins())
    elif act == "pdel":
        await run("DELETE FROM pending_admins WHERE username=?", (p[2],))
        await show(c, *await v_admins())
    elif act == "hist":
        await show(c, *await v_history(int(p[2])))
    elif act == "users":
        await show(c, *await v_users(int(p[2])))
    elif act == "user":
        await show(c, *await v_user(int(p[2])))
    elif act == "sug":
        await show(c, *await v_sug(int(p[2]), p[3] if len(p) > 3 else "h0"))
    elif act == "show":
        s = await get_sug(int(p[2]))
        if not s:
            return
        try:
            await bot.copy_messages(c.message.chat.id, s["chat_id"], json.loads(s["message_ids"]))
        except TelegramAPIError:
            await c.message.answer("⚠️ Не удалось получить содержимое (возможно, пользователь удалил сообщение).")


@admin_router.message(AdminS.add_channel)
async def add_channel(m: Message, state: FSMContext, bot: Bot):
    ref = None
    fo = m.forward_origin
    if isinstance(fo, MessageOriginChannel):
        ref = fo.chat.id
    elif m.text:
        t = re.sub(r"^https?://t\.me/", "@", m.text.strip())
        if re.fullmatch(r"-?\d+", t):
            ref = int(t)
        elif re.fullmatch(r"@\w{4,}", t):
            ref = t
    if ref is None:
        return await m.answer("Не понял 🤔 Перешли сообщение из канала или отправь @username / ID.")

    try:
        chat = await bot.get_chat(ref)
    except TelegramAPIError:
        return await m.answer("Не нашёл такой канал. Проверь, что бот добавлен в него.")
    if chat.type != ChatType.CHANNEL:
        return await m.answer("Это не канал. Нужен именно канал.")

    try:
        me = await bot.get_chat_member(chat.id, bot.id)
    except TelegramAPIError:
        return await m.answer("Бот не состоит в этом канале. Добавь его админом и повтори.")
    can_post = me.status == ChatMemberStatus.CREATOR or (
        me.status == ChatMemberStatus.ADMINISTRATOR and getattr(me, "can_post_messages", False)
    )
    if not can_post:
        return await m.answer("У бота нет права публиковать сообщения в этом канале. Выдай право и повтори.")

    await run(
        "INSERT OR REPLACE INTO channels(chat_id,title,username,added_at) VALUES(?,?,?,?)",
        (chat.id, chat.title or str(chat.id), chat.username, now()),
    )
    await state.clear()
    text, markup = await v_channels()
    await m.answer(f"✅ Канал «{esc(chat.title)}» подключён.\n\n{text}", reply_markup=markup)


@admin_router.message(AdminS.add_admin)
async def add_admin(m: Message, state: FSMContext):
    t = (m.text or "").strip()
    note = ""
    if re.fullmatch(r"\d+", t):
        uid = int(t)
        u = await q_one("SELECT username FROM users WHERE user_id=?", (uid,))
        await run(
            "INSERT OR IGNORE INTO admins(user_id,username,added_by,added_at) VALUES(?,?,?,?)",
            (uid, u["username"] if u else None, m.from_user.id, now()),
        )
        if not u:
            note = "\n⚠️ Этот пользователь ещё не запускал бота — пусть нажмёт /start."
    elif re.fullmatch(r"@?\w{4,32}", t):
        uname = t.lstrip("@")
        u = await q_one("SELECT user_id, username FROM users WHERE lower(username)=?", (uname.lower(),))
        if u:
            await run(
                "INSERT OR IGNORE INTO admins(user_id,username,added_by,added_at) VALUES(?,?,?,?)",
                (u["user_id"], u["username"], m.from_user.id, now()),
            )
        else:
            await run(
                "INSERT OR IGNORE INTO pending_admins(username,added_by,added_at) VALUES(?,?,?)",
                (uname, m.from_user.id, now()),
            )
            note = f"\n⏳ @{esc(uname)} пока не в базе — станет админом, как только нажмёт /start."
    else:
        return await m.answer("Отправь @username или числовой ID.")

    await state.clear()
    text, markup = await v_admins()
    await m.answer(f"✅ Готово.{note}\n\n{text}", reply_markup=markup)


# ───────────────────────── заглушки ─────────────────────────
@user_router.callback_query(F.data.startswith(("adm:", "mod:")))
async def no_access(c: CallbackQuery):
    await c.answer("⛔ Нет доступа", show_alert=True)


@user_router.message()
async def fallback(m: Message):
    await m.answer("Нажми /start, чтобы отправить предложку.")


# ───────────────────────── запуск ─────────────────────────
async def main():
    if not BOT_TOKEN:
        raise SystemExit("Не задан BOT_TOKEN")
    if not OWNER_IDS:
        raise SystemExit("Не задан OWNER_IDS (ID хотя бы одного владельца)")
    await init_db()
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_routers(base, admin_router, user_router)
    await bot.delete_webhook(drop_pending_updates=False)
    log.info("Бот запущен")
    try:
        await dp.start_polling(bot)
    finally:
        await db.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
