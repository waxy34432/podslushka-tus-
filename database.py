import aiosqlite
from config import DB_PATH

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""CREATE TABLE IF NOT EXISTS submissions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL, username TEXT, full_name TEXT,
            chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
            created_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
            moderated_by INTEGER
        )""")
        await db.execute("CREATE TABLE IF NOT EXISTS blocked (user_id INTEGER PRIMARY KEY)")
        await db.execute("CREATE TABLE IF NOT EXISTS consents (user_id INTEGER PRIMARY KEY, accepted_at TEXT NOT NULL)")
        await db.execute("CREATE TABLE IF NOT EXISTS admins (user_id INTEGER PRIMARY KEY, added_at TEXT DEFAULT CURRENT_TIMESTAMP)")
        await db.execute("""CREATE TABLE IF NOT EXISTS channels (
            chat_id INTEGER PRIMARY KEY, title TEXT NOT NULL, username TEXT,
            is_default INTEGER NOT NULL DEFAULT 0
        )""")
        await db.commit()

async def add_submission(message):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO submissions(user_id,username,full_name,chat_id,message_id,created_at) VALUES(?,?,?,?,?,?)",
            (message.from_user.id, message.from_user.username, message.from_user.full_name,
             message.chat.id, message.message_id, message.date.isoformat())
        )
        await db.commit()
        return cur.lastrowid

async def get_submission(sid):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM submissions WHERE id=?", (sid,))
        row = await cur.fetchone()
        return dict(row) if row else None

async def set_submission_status(sid, status, admin_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE submissions SET status=?, moderated_by=? WHERE id=?", (status, admin_id, sid))
        await db.commit()

async def get_stats():
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT COUNT(*), SUM(status='pending'), SUM(status='published'), SUM(status='rejected') FROM submissions")
        r = await cur.fetchone()
        return dict(zip(("total","pending","published","rejected"), (r[0] or 0,r[1] or 0,r[2] or 0,r[3] or 0)))

async def get_pending(limit=10):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM submissions WHERE status='pending' ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(row) for row in await cur.fetchall()]

async def sender_history(uid):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM submissions WHERE user_id=? ORDER BY id DESC LIMIT 30", (uid,))
        return [dict(row) for row in await cur.fetchall()]

async def is_blocked(uid):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT 1 FROM blocked WHERE user_id=?", (uid,))
        return await cur.fetchone() is not None

async def set_blocked(uid, blocked):
    async with aiosqlite.connect(DB_PATH) as db:
        if blocked:
            await db.execute("INSERT OR IGNORE INTO blocked(user_id) VALUES(?)", (uid,))
        else:
            await db.execute("DELETE FROM blocked WHERE user_id=?", (uid,))
        await db.commit()

async def has_consent(uid):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT 1 FROM consents WHERE user_id=?", (uid,))
        return await cur.fetchone() is not None

async def grant_consent(uid):
    from datetime import datetime, timezone
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR REPLACE INTO consents(user_id,accepted_at) VALUES(?,?)",
                         (uid, datetime.now(timezone.utc).isoformat()))
        await db.commit()

async def is_admin(uid):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT 1 FROM admins WHERE user_id=?", (uid,))
        return await cur.fetchone() is not None

async def get_admin_ids():
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT user_id FROM admins")
        return [row[0] for row in await cur.fetchall()]

async def get_admins():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT user_id FROM admins ORDER BY user_id")
        return [dict(row) for row in await cur.fetchall()]

async def add_admin(uid):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO admins(user_id) VALUES(?)", (uid,))
        await db.commit()

async def remove_admin(uid):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM admins WHERE user_id=?", (uid,))
        await db.commit()

async def get_channels():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM channels ORDER BY is_default DESC, title")
        return [dict(row) for row in await cur.fetchall()]

async def add_channel(chat_id, title, username):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT COUNT(*) FROM channels")
        count = (await cur.fetchone())[0]
        await db.execute(
            "INSERT OR REPLACE INTO channels(chat_id,title,username,is_default) VALUES(?,?,?,?)",
            (chat_id, title, username, 1 if count == 0 else 0)
        )
        await db.commit()

async def set_default_channel(chat_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE channels SET is_default=0")
        await db.execute("UPDATE channels SET is_default=1 WHERE chat_id=?", (chat_id,))
        await db.commit()

async def delete_channel(chat_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM channels WHERE chat_id=?", (chat_id,))
        cur = await db.execute("SELECT chat_id FROM channels ORDER BY title LIMIT 1")
        row = await cur.fetchone()
        if row:
            await db.execute("UPDATE channels SET is_default=1 WHERE chat_id=?", (row[0],))
        await db.commit()

async def get_default_channel():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM channels WHERE is_default=1 LIMIT 1")
        row = await cur.fetchone()
        return dict(row) if row else None
