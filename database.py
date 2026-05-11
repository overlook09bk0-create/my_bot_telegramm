"""
database.py — Все операции с SQLite.

Таблицы:
  users           — пользователи (бан, мут, варны)
  threats         — угрозы / доксинг
  settings        — глобальные настройки (bot_active)
  admin_stats     — статистика действий администраторов
  jarvis_sessions — сессии Джарвиса (waiting_admin / jarvis_active / closed)
  jarvis_blocked  — суточная блокировка ИИ
  tickets         — система тикетов (инкрементальные номера заявок)
  anon_users      — пользователи с включённым анонимным режимом
"""

import aiosqlite
from datetime import datetime, timezone

DB_PATH = "bot_database.db"


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:

        # ── Пользователи ──────────────────────────────────────────────────────
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id    INTEGER PRIMARY KEY,
                username   TEXT,
                first_name TEXT,
                bio        TEXT,
                is_banned  INTEGER DEFAULT 0,
                ban_reason TEXT,
                mute_until DATETIME,
                warns      INTEGER DEFAULT 0
            )
        """)
        try:
            await db.execute("ALTER TABLE users ADD COLUMN warns INTEGER DEFAULT 0")
        except Exception:
            pass

        # ── Угрозы ────────────────────────────────────────────────────────────
        await db.execute("""
            CREATE TABLE IF NOT EXISTS threats (
                user_id      INTEGER,
                threat_type  TEXT,
                message_text TEXT,
                detected_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # ── Глобальные настройки ──────────────────────────────────────────────
        await db.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        await db.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES ('bot_active', '1')"
        )

        # ── Статистика администраторов ────────────────────────────────────────
        await db.execute("""
            CREATE TABLE IF NOT EXISTS admin_stats (
                admin_id   INTEGER PRIMARY KEY,
                admin_name TEXT,
                replies    INTEGER DEFAULT 0,
                bans       INTEGER DEFAULT 0,
                mutes      INTEGER DEFAULT 0,
                warns      INTEGER DEFAULT 0
            )
        """)

        # ── Сессии Джарвиса ───────────────────────────────────────────────────
        await db.execute("""
            CREATE TABLE IF NOT EXISTS jarvis_sessions (
                user_id           INTEGER PRIMARY KEY,
                state             TEXT    DEFAULT 'waiting_admin',
                created_at        TEXT    NOT NULL,
                jarvis_started_at TEXT,
                history           TEXT    DEFAULT '[]'
            )
        """)

        # ── Суточная блокировка ИИ ────────────────────────────────────────────
        await db.execute("""
            CREATE TABLE IF NOT EXISTS jarvis_blocked (
                user_id       INTEGER PRIMARY KEY,
                blocked_until TEXT NOT NULL
            )
        """)

        # ── Тикеты (система заявок) ───────────────────────────────────────────
        # ticket_id  — глобальный инкрементальный номер (AUTOINCREMENT)
        # user_id    — кто создал заявку
        # created_at — когда создана
        # status     — open / closed
        await db.execute("""
            CREATE TABLE IF NOT EXISTS tickets (
                ticket_id  INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    INTEGER NOT NULL,
                created_at TEXT    NOT NULL,
                status     TEXT    DEFAULT 'open'
            )
        """)

        # ── Анонимный режим ───────────────────────────────────────────────────
        # Если user_id есть в этой таблице — пользователь анонимен
        await db.execute("""
            CREATE TABLE IF NOT EXISTS anon_users (
                user_id INTEGER PRIMARY KEY
            )
        """)

        await db.commit()


# ══════════════════════════════════════════════════════════════════════════════
# ПОЛЬЗОВАТЕЛИ
# ══════════════════════════════════════════════════════════════════════════════

async def is_banned(user_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT is_banned FROM users WHERE user_id = ?", (user_id,))
        row = await cur.fetchone()
        return bool(row and row[0])


async def is_muted(user_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT mute_until FROM users WHERE user_id = ?", (user_id,))
        row = await cur.fetchone()
        if not row or not row[0]:
            return False
        try:
            mute_dt = datetime.fromisoformat(row[0])
            if mute_dt.tzinfo is None:
                mute_dt = mute_dt.replace(tzinfo=timezone.utc)
            return datetime.now(timezone.utc) < mute_dt
        except ValueError:
            return False


async def ban_user(user_id: int, reason: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))
        await db.execute(
            "UPDATE users SET is_banned = 1, ban_reason = ? WHERE user_id = ?",
            (reason, user_id),
        )
        await db.commit()


async def set_mute(user_id: int, until_iso: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))
        await db.execute(
            "UPDATE users SET mute_until = ? WHERE user_id = ?",
            (until_iso, user_id),
        )
        await db.commit()


async def upsert_user(
    user_id: int,
    username: str | None = None,
    first_name: str | None = None,
    last_name: str | None = None,
    bio: str | None = None,
) -> bool:
    """Создаёт или обновляет пользователя. Возвращает True если новый."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT 1 FROM users WHERE user_id = ?", (user_id,))
        exists = await cur.fetchone()
        if exists:
            fields, values = [], []
            if username is not None:
                fields.append("username = ?"); values.append(username)
            if first_name is not None:
                fields.append("first_name = ?"); values.append(first_name)
            if bio is not None:
                fields.append("bio = ?"); values.append(bio)
            if fields:
                values.append(user_id)
                await db.execute(
                    f"UPDATE users SET {', '.join(fields)} WHERE user_id = ?", values
                )
        else:
            await db.execute(
                "INSERT INTO users (user_id, username, first_name, bio) VALUES (?, ?, ?, ?)",
                (user_id, username, first_name, bio),
            )
        await db.commit()
        return not bool(exists)


async def log_threat(user_id: int, threat_type: str, message_text: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO threats (user_id, threat_type, message_text) VALUES (?, ?, ?)",
            (user_id, threat_type, message_text),
        )
        await db.commit()


# ══════════════════════════════════════════════════════════════════════════════
# ВАРНЫ
# ══════════════════════════════════════════════════════════════════════════════

async def add_warn(user_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))
        await db.execute("UPDATE users SET warns = warns + 1 WHERE user_id = ?", (user_id,))
        await db.commit()
        cur = await db.execute("SELECT warns FROM users WHERE user_id = ?", (user_id,))
        row = await cur.fetchone()
        return row[0] if row else 1


async def reset_warns(user_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET warns = 0 WHERE user_id = ?", (user_id,))
        await db.commit()


async def get_warns(user_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT warns FROM users WHERE user_id = ?", (user_id,))
        row = await cur.fetchone()
        return row[0] if row else 0


# ══════════════════════════════════════════════════════════════════════════════
# РАЗБАН / РАЗМУТ
# ══════════════════════════════════════════════════════════════════════════════

async def get_user(user_id: int) -> dict | None:
    """Возвращает данные пользователя (username, first_name и др.) или None."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT user_id, username, first_name FROM users WHERE user_id = ?",
            (user_id,),
        )
        row = await cur.fetchone()
        return dict(row) if row else None


async def get_muted_users() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        now_iso = datetime.now(timezone.utc).isoformat()
        cur = await db.execute(
            """
            SELECT user_id, username, first_name, mute_until
            FROM users
            WHERE mute_until IS NOT NULL AND mute_until > ?
            ORDER BY mute_until ASC
            """,
            (now_iso,),
        )
        return [dict(r) for r in await cur.fetchall()]


async def get_banned_users() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT user_id, username, first_name, ban_reason FROM users "
            "WHERE is_banned = 1 ORDER BY user_id ASC"
        )
        return [dict(r) for r in await cur.fetchall()]


async def unban_user(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET is_banned = 0, ban_reason = NULL, mute_until = NULL "
            "WHERE user_id = ?",
            (user_id,),
        )
        await db.commit()


async def unmute_user(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET mute_until = NULL WHERE user_id = ?", (user_id,))
        await db.commit()


# ══════════════════════════════════════════════════════════════════════════════
# СТАТУС БОТА
# ══════════════════════════════════════════════════════════════════════════════

async def set_ai_status(status: bool) -> None:
    value = "1" if status else "0"
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES ('ai_active', ?)",
            (value,),
        )
        await db.commit()


async def is_ai_active() -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT value FROM settings WHERE key = 'ai_active'")
        row = await cur.fetchone()
        return True if row is None else row[0] == "1"


async def set_bot_status(status: bool) -> None:
    value = "1" if status else "0"
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES ('bot_active', ?)",
            (value,),
        )
        await db.commit()


async def is_bot_active() -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT value FROM settings WHERE key = 'bot_active'")
        row = await cur.fetchone()
        return True if row is None else row[0] == "1"


# ══════════════════════════════════════════════════════════════════════════════
# СТАТИСТИКА АДМИНИСТРАТОРОВ
# ══════════════════════════════════════════════════════════════════════════════

async def _ensure_admin(db, admin_id: int, admin_name: str) -> None:
    await db.execute(
        "INSERT OR IGNORE INTO admin_stats (admin_id, admin_name) VALUES (?, ?)",
        (admin_id, admin_name),
    )
    await db.execute(
        "UPDATE admin_stats SET admin_name = ? WHERE admin_id = ?",
        (admin_name, admin_id),
    )


async def log_admin_action(admin_id: int, admin_name: str, action: str) -> None:
    # Явный словарь исключает опечатку "replys" вместо "replies"
    _action_to_column = {
        "reply": "replies",
        "ban":   "bans",
        "mute":  "mutes",
        "warn":  "warns",
    }
    column = _action_to_column.get(action)
    if column is None:
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await _ensure_admin(db, admin_id, admin_name)
        await db.execute(
            f"UPDATE admin_stats SET {column} = {column} + 1 WHERE admin_id = ?",
            (admin_id,),
        )
        await db.commit()


async def get_top_admins(limit: int = 3) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """
            SELECT admin_id, admin_name, replies, bans, mutes, warns,
                   (replies + bans + mutes + warns) AS total
            FROM admin_stats
            ORDER BY total DESC
            LIMIT ?
            """,
            (limit,),
        )
        return [dict(r) for r in await cur.fetchall()]


# ══════════════════════════════════════════════════════════════════════════════
# СЕССИИ ДЖАРВИСА
# ══════════════════════════════════════════════════════════════════════════════

async def create_jarvis_session(user_id: int) -> None:
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT OR REPLACE INTO jarvis_sessions
                (user_id, state, created_at, jarvis_started_at, history)
            VALUES (?, 'waiting_admin', ?, NULL, '[]')
            """,
            (user_id, now),
        )
        await db.commit()


async def get_jarvis_session(user_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM jarvis_sessions WHERE user_id = ?", (user_id,)
        )
        row = await cur.fetchone()
        return dict(row) if row else None


async def set_jarvis_state(user_id: int, state: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        if state == "jarvis_active":
            now = datetime.now(timezone.utc).isoformat()
            await db.execute(
                "UPDATE jarvis_sessions SET state = ?, jarvis_started_at = ? WHERE user_id = ?",
                (state, now, user_id),
            )
        else:
            await db.execute(
                "UPDATE jarvis_sessions SET state = ? WHERE user_id = ?",
                (state, user_id),
            )
        await db.commit()


async def update_jarvis_history(user_id: int, history_json: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE jarvis_sessions SET history = ? WHERE user_id = ?",
            (history_json, user_id),
        )
        await db.commit()


async def close_jarvis_session(user_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE jarvis_sessions SET state = 'closed' WHERE user_id = ?",
            (user_id,),
        )
        await db.commit()


async def block_jarvis_for_user(user_id: int, hours: int = 24) -> None:
    from datetime import timedelta
    until = (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO jarvis_blocked (user_id, blocked_until) VALUES (?, ?)",
            (user_id, until),
        )
        await db.commit()


async def is_jarvis_blocked(user_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT blocked_until FROM jarvis_blocked WHERE user_id = ?", (user_id,)
        )
        row = await cur.fetchone()
        if not row:
            return False
        try:
            until_dt = datetime.fromisoformat(row[0])
            if until_dt.tzinfo is None:
                until_dt = until_dt.replace(tzinfo=timezone.utc)
            return datetime.now(timezone.utc) < until_dt
        except ValueError:
            return False


async def get_jarvis_remaining_seconds(user_id: int) -> int:
    """
    Возвращает сколько секунд осталось до конца активной сессии Джарвиса.
    Считает от jarvis_started_at + 10 минут.
    Возвращает 0 если сессия не активна или время вышло.
    """
    from datetime import timedelta
    JARVIS_LIMIT = 10 * 60  # 10 минут в секундах
    session = await get_jarvis_session(user_id)
    if not session or session.get("state") != "jarvis_active":
        return 0
    started_str = session.get("jarvis_started_at")
    if not started_str:
        return 0
    try:
        started = datetime.fromisoformat(started_str)
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        elapsed = (datetime.now(timezone.utc) - started).total_seconds()
        remaining = JARVIS_LIMIT - elapsed
        return max(0, int(remaining))
    except ValueError:
        return 0


async def get_jarvis_blocked_until(user_id: int) -> datetime | None:
    """
    Возвращает datetime (UTC) до которого заблокирован Джарвис для пользователя.
    Возвращает None если блокировки нет или она истекла.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT blocked_until FROM jarvis_blocked WHERE user_id = ?", (user_id,)
        )
        row = await cur.fetchone()
        if not row:
            return None
        try:
            until_dt = datetime.fromisoformat(row[0])
            if until_dt.tzinfo is None:
                until_dt = until_dt.replace(tzinfo=timezone.utc)
            # Возвращаем только если блокировка ещё активна
            return until_dt if datetime.now(timezone.utc) < until_dt else None
        except ValueError:
            return None


# ══════════════════════════════════════════════════════════════════════════════
# ПРЯМАЯ СВЯЗЬ АДМИНА С ПОЛЬЗОВАТЕЛЕМ (/link)
# ══════════════════════════════════════════════════════════════════════════════

async def set_link(admin_id: int, admin_name: str, user_id: int) -> None:
    """Создаёт прямую связь admin_id → user_id."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS admin_links (
                admin_id   INTEGER PRIMARY KEY,
                admin_name TEXT    NOT NULL,
                user_id    INTEGER NOT NULL,
                created_at TEXT    NOT NULL
            )
        """)
        await db.execute(
            "INSERT OR REPLACE INTO admin_links (admin_id, admin_name, user_id, created_at) "
            "VALUES (?, ?, ?, ?)",
            (admin_id, admin_name, user_id, datetime.now(timezone.utc).isoformat()),
        )
        await db.commit()


async def remove_link(admin_id: int) -> None:
    """Удаляет прямую связь для этого админа."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM admin_links WHERE admin_id = ?", (admin_id,)
        )
        await db.commit()


async def get_link_by_admin(admin_id: int) -> dict | None:
    """Возвращает {admin_id, admin_name, user_id} или None."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS admin_links (
                admin_id   INTEGER PRIMARY KEY,
                admin_name TEXT    NOT NULL,
                user_id    INTEGER NOT NULL,
                created_at TEXT    NOT NULL
            )
        """)
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM admin_links WHERE admin_id = ?", (admin_id,)
        )
        row = await cur.fetchone()
        return dict(row) if row else None


async def get_link_by_user(user_id: int) -> dict | None:
    """Возвращает {admin_id, admin_name, user_id} если кто-то уже слинкован с этим юзером."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS admin_links (
                admin_id   INTEGER PRIMARY KEY,
                admin_name TEXT    NOT NULL,
                user_id    INTEGER NOT NULL,
                created_at TEXT    NOT NULL
            )
        """)
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM admin_links WHERE user_id = ?", (user_id,)
        )
        row = await cur.fetchone()
        return dict(row) if row else None


# ══════════════════════════════════════════════════════════════════════════════
# ОДНОРАЗОВЫЕ FAQ-ОТВЕТЫ (показываются только 1 раз)
# ══════════════════════════════════════════════════════════════════════════════

async def is_faq_shown(user_id: int, key: str) -> bool:
    """Возвращает True если этот FAQ-ответ уже показывался пользователю."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS faq_shown (
                user_id INTEGER NOT NULL,
                key     TEXT    NOT NULL,
                PRIMARY KEY (user_id, key)
            )
        """)
        cur = await db.execute(
            "SELECT 1 FROM faq_shown WHERE user_id = ? AND key = ?",
            (user_id, key),
        )
        return (await cur.fetchone()) is not None


async def mark_faq_shown(user_id: int, key: str) -> None:
    """Отмечает что FAQ-ответ уже был показан пользователю."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS faq_shown (
                user_id INTEGER NOT NULL,
                key     TEXT    NOT NULL,
                PRIMARY KEY (user_id, key)
            )
        """)
        await db.execute(
            "INSERT OR IGNORE INTO faq_shown (user_id, key) VALUES (?, ?)",
            (user_id, key),
        )
        await db.commit()


# ══════════════════════════════════════════════════════════════════════════════
# ЛИМИТ СООБЩЕНИЙ К ДЖАРВИСУ В STAFF_GROUP
# ══════════════════════════════════════════════════════════════════════════════
# 10 сообщений на пользователя, перезарядка через 2 часа

STAFF_JARVIS_LIMIT = 10
STAFF_JARVIS_COOLDOWN_HOURS = 2


async def get_staff_jarvis_usage(user_id: int) -> dict:
    """
    Возвращает {'used': N, 'reset_at': datetime | None}.
    reset_at — время когда счётчик сбросится (через 2 часа после первого сообщения).
    """
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS staff_jarvis_usage (
                user_id   INTEGER PRIMARY KEY,
                used      INTEGER DEFAULT 0,
                reset_at  TEXT    NOT NULL
            )
        """)
        cur = await db.execute(
            "SELECT used, reset_at FROM staff_jarvis_usage WHERE user_id = ?",
            (user_id,),
        )
        row = await cur.fetchone()
        if not row:
            return {"used": 0, "reset_at": None}

        reset_at = datetime.fromisoformat(row[1])
        if reset_at.tzinfo is None:
            reset_at = reset_at.replace(tzinfo=timezone.utc)

        # Если время сброса прошло — автоматически сбрасываем
        if datetime.now(timezone.utc) >= reset_at:
            await db.execute(
                "DELETE FROM staff_jarvis_usage WHERE user_id = ?", (user_id,)
            )
            await db.commit()
            return {"used": 0, "reset_at": None}

        return {"used": row[0], "reset_at": reset_at}


async def increment_staff_jarvis_usage(user_id: int) -> dict:
    """
    Увеличивает счётчик на 1.
    При первом сообщении — устанавливает reset_at = сейчас + 2 часа.
    Возвращает актуальный {'used': N, 'reset_at': datetime}.
    """
    from datetime import timedelta
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS staff_jarvis_usage (
                user_id   INTEGER PRIMARY KEY,
                used      INTEGER DEFAULT 0,
                reset_at  TEXT    NOT NULL
            )
        """)
        cur = await db.execute(
            "SELECT used, reset_at FROM staff_jarvis_usage WHERE user_id = ?",
            (user_id,),
        )
        row = await cur.fetchone()

        now = datetime.now(timezone.utc)

        if not row:
            # Первое сообщение — создаём запись
            reset_at = now + timedelta(hours=STAFF_JARVIS_COOLDOWN_HOURS)
            await db.execute(
                "INSERT INTO staff_jarvis_usage (user_id, used, reset_at) VALUES (?, 1, ?)",
                (user_id, reset_at.isoformat()),
            )
            await db.commit()
            return {"used": 1, "reset_at": reset_at}

        reset_at = datetime.fromisoformat(row[1])
        if reset_at.tzinfo is None:
            reset_at = reset_at.replace(tzinfo=timezone.utc)

        # Если кулдаун истёк — сбрасываем и начинаем заново
        if now >= reset_at:
            reset_at = now + timedelta(hours=STAFF_JARVIS_COOLDOWN_HOURS)
            await db.execute(
                "INSERT OR REPLACE INTO staff_jarvis_usage (user_id, used, reset_at) VALUES (?, 1, ?)",
                (user_id, reset_at.isoformat()),
            )
            await db.commit()
            return {"used": 1, "reset_at": reset_at}

        # Обычный инкремент
        new_used = row[0] + 1
        await db.execute(
            "UPDATE staff_jarvis_usage SET used = ? WHERE user_id = ?",
            (new_used, user_id),
        )
        await db.commit()
        return {"used": new_used, "reset_at": reset_at}


# ══════════════════════════════════════════════════════════════════════════════
# СТАТУСЫ ЗАЯВОК В АДМИНИСТРАЦИЮ
# ══════════════════════════════════════════════════════════════════════════════
# status: "pending" | "accepted" | "rejected"

async def set_application_status(user_id: int, status: str) -> None:
    """Сохраняет статус заявки пользователя."""
    from datetime import timedelta
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS applications (
                user_id     INTEGER PRIMARY KEY,
                status      TEXT    NOT NULL,
                updated_at  TEXT    NOT NULL
            )
        """)
        await db.execute(
            "INSERT OR REPLACE INTO applications (user_id, status, updated_at) VALUES (?, ?, ?)",
            (user_id, status, datetime.now(timezone.utc).isoformat()),
        )
        await db.commit()


async def get_application_status(user_id: int) -> dict | None:
    """
    Возвращает {'status': ..., 'updated_at': datetime} или None.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS applications (
                user_id     INTEGER PRIMARY KEY,
                status      TEXT    NOT NULL,
                updated_at  TEXT    NOT NULL
            )
        """)
        cur = await db.execute(
            "SELECT status, updated_at FROM applications WHERE user_id = ?",
            (user_id,),
        )
        row = await cur.fetchone()
        if not row:
            return None
        updated = datetime.fromisoformat(row[1])
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=timezone.utc)
        return {"status": row[0], "updated_at": updated}


# ══════════════════════════════════════════════════════════════════════════════
# СВЯЗКА СООБЩЕНИЙ (для /report и reply на forward)
# ══════════════════════════════════════════════════════════════════════════════
# Хранит: chat_id + message_id пересланного сообщения → user_id
# Позволяет найти user_id когда админ делает reply на forward без заголовка

async def save_forwarded_message(chat_id: int, message_id: int, user_id: int) -> None:
    """Сохраняет связку переслан­ного сообщения с user_id."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS forwarded_messages (
                chat_id    INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                user_id    INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (chat_id, message_id)
            )
        """)
        await db.execute(
            "INSERT OR REPLACE INTO forwarded_messages (chat_id, message_id, user_id, created_at) "
            "VALUES (?, ?, ?, ?)",
            (chat_id, message_id, user_id, datetime.now(timezone.utc).isoformat()),
        )
        await db.commit()


async def get_user_id_by_message(chat_id: int, message_id: int) -> int | None:
    """Возвращает user_id по chat_id и message_id пересланного сообщения."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT user_id FROM forwarded_messages WHERE chat_id = ? AND message_id = ?",
            (chat_id, message_id),
        )
        row = await cur.fetchone()
        return row[0] if row else None


async def save_bot_message_id(
    group_chat_id: int, group_message_id: int,
    user_id: int, bot_message_id: int
) -> None:
    """
    Сохраняет связку: сообщение в группе → message_id который получил пользователь в боте.
    Используется для reply с цитатой во время /link.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS bot_messages (
                group_chat_id    INTEGER NOT NULL,
                group_message_id INTEGER NOT NULL,
                user_id          INTEGER NOT NULL,
                bot_message_id   INTEGER NOT NULL,
                PRIMARY KEY (group_chat_id, group_message_id)
            )
        """)
        await db.execute(
            "INSERT OR REPLACE INTO bot_messages VALUES (?, ?, ?, ?)",
            (group_chat_id, group_message_id, user_id, bot_message_id),
        )
        await db.commit()


async def get_bot_message_id(
    group_chat_id: int, group_message_id: int, user_id: int
) -> int | None:
    """Возвращает message_id в боте для данного сообщения группы."""
    async with aiosqlite.connect(DB_PATH) as db:
        try:
            cur = await db.execute(
                "SELECT bot_message_id FROM bot_messages "
                "WHERE group_chat_id = ? AND group_message_id = ? AND user_id = ?",
                (group_chat_id, group_message_id, user_id),
            )
            row = await cur.fetchone()
            return row[0] if row else None
        except Exception:
            return None


# ══════════════════════════════════════════════════════════════════════════════
# ТИКЕТЫ
# ══════════════════════════════════════════════════════════════════════════════

async def create_ticket(user_id: int) -> int:
    """
    Создаёт новый тикет для пользователя.
    Возвращает инкрементальный номер заявки (ticket_id).
    """
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO tickets (user_id, created_at, status) VALUES (?, ?, 'open')",
            (user_id, now),
        )
        await db.commit()
        return cur.lastrowid


async def close_ticket(user_id: int) -> None:
    """Закрывает последний открытый тикет пользователя."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            UPDATE tickets SET status = 'closed'
            WHERE ticket_id = (
                SELECT ticket_id FROM tickets
                WHERE user_id = ? AND status = 'open'
                ORDER BY ticket_id DESC LIMIT 1
            )
            """,
            (user_id,),
        )
        await db.commit()


async def get_open_ticket(user_id: int) -> int | None:
    """Возвращает номер открытого тикета пользователя или None."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT ticket_id FROM tickets WHERE user_id = ? AND status = 'open' "
            "ORDER BY ticket_id DESC LIMIT 1",
            (user_id,),
        )
        row = await cur.fetchone()
        return row[0] if row else None


# ══════════════════════════════════════════════════════════════════════════════
# АНОНИМНЫЙ РЕЖИМ
# ══════════════════════════════════════════════════════════════════════════════

async def set_anon(user_id: int, enabled: bool) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        if enabled:
            await db.execute(
                "INSERT OR IGNORE INTO anon_users (user_id) VALUES (?)", (user_id,)
            )
        else:
            await db.execute("DELETE FROM anon_users WHERE user_id = ?", (user_id,))
        await db.commit()


async def is_anon(user_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT 1 FROM anon_users WHERE user_id = ?", (user_id,)
        )
        return (await cur.fetchone()) is not None