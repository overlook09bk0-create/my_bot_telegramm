"""
ai_timer.py — Менеджер таймеров Джарвиса.

Схема работы:
  1. Пользователь пишет → создаётся сессия 'waiting_admin' + 5-мин таймер
  2. Живой админ ответил → таймер отменяется, сессия закрывается
  3. 5 мин прошло без ответа → Джарвис включается ('jarvis_active') + 10-мин лимит
  4. 10 мин диалога → прощание, ИИ заблокирован для этого юзера на 24 ч

Обращение к Джарвису НАПРЯМУЮ из STAFF_GROUP_ID:
  Если в группе персонала упоминают "джарвис" — Джарвис отвечает в ту же группу.
"""

import asyncio
import json
import logging
import re
from datetime import datetime, timezone, timedelta

import database as db
from ai_service import ask_jarvis
from config import STAFF_GROUP_ID

logger = logging.getLogger(__name__)

# ── Настройки таймеров ────────────────────────────────────────────────────────
ADMIN_WAIT_SECONDS   = 5 * 60    # 5 минут ожидания живого админа
JARVIS_LIMIT_SECONDS = 10 * 60   # 10 минут диалога с Джарвисом

# Владелец — без лимитов и с особым приветствием
OWNER_USERNAME = "it_is_your_brother"

# ── Ключевые слова для обращения к Джарвису (регистронезависимо) ──────────────
# Совпадение: "джарвис", "Джарвис,", "ДЖАРВИС!", "jarvis" и т.д.
_JARVIS_PATTERN = re.compile(r"дж[аa]рв[иi]с|jarvis", re.IGNORECASE)

# ── Активные таймеры ──────────────────────────────────────────────────────────
_admin_wait_tasks:   dict[int, asyncio.Task] = {}
_jarvis_limit_tasks: dict[int, asyncio.Task] = {}


# ══════════════════════════════════════════════════════════════════════════════
# ПРОВЕРКА: ОБРАЩЕНИЕ К ДЖАРВИСУ
# ══════════════════════════════════════════════════════════════════════════════

# Русские названия месяцев для красивого формата даты
_MONTHS_RU = {
    1: "января", 2: "февраля", 3: "марта", 4: "апреля",
    5: "мая", 6: "июня", 7: "июля", 8: "августа",
    9: "сентября", 10: "октября", 11: "ноября", 12: "декабря",
}


def _format_unlock_dt(dt_utc: datetime) -> str:
    """
    Форматирует время разблокировки в читаемый вид по LOCAL времени сервера.
    Пример: "13 марта в 14:30"
    dt_utc — datetime в UTC (с tzinfo).
    """
    # Конвертируем UTC → локальное время ноутбука/сервера
    local_dt = dt_utc.astimezone()
    month_name = _MONTHS_RU[local_dt.month]
    return f"{local_dt.day} {month_name} в {local_dt.strftime('%H:%M')}"


def is_jarvis_mention(text: str) -> bool:
    """
    Возвращает True если в тексте есть обращение к Джарвису.
    Работает независимо от регистра, запятых, знаков препинания.
    Примеры: "джарвис", "Джарвис,", "эй, ДЖАРВИС!", "Jarvis помоги"
    """
    return bool(_JARVIS_PATTERN.search(text))


# ══════════════════════════════════════════════════════════════════════════════
# ОБРАЩЕНИЕ К ДЖАРВИСУ ИЗ STAFF_GROUP
# ══════════════════════════════════════════════════════════════════════════════

async def handle_staff_jarvis_mention(
    text: str,
    bot,
    chat_id: int,
    user_id: int = 0,
    username: str = "",
    reply_to_message_id: int | None = None,
) -> None:
    """
    Вызывается из reply_handler когда в STAFF_GROUP упоминают Джарвиса.
    Джарвис отвечает прямо в группу персонала.
    Лимит: 10 сообщений на пользователя, перезарядка через 2 часа.
    """
    print(f"[JARVIS] ✅ Вижу обращение к Джарвису в группе {chat_id}: {text[:80]!r}")
    logger.info(f"[JARVIS] Обращение в STAFF_GROUP от user_id={user_id}: {text[:80]!r}")

    # Проверяем не выключен ли ИИ администраторами
    if not await db.is_ai_active():
        try:
            await bot.send_message(
                chat_id,
                "🔴 <b>Джарвис отключён.</b> ИИ-ассистент выключен администраторами.",
                parse_mode="HTML",
                reply_to_message_id=reply_to_message_id,
            )
        except Exception:
            pass
        return

    user_tag = f"@{username}" if username else f"id{user_id}"

    # ── Проверяем лимит ───────────────────────────────────────────────────────
    usage = await db.get_staff_jarvis_usage(user_id)
    if usage["used"] >= db.STAFF_JARVIS_LIMIT:
        reset_at = usage["reset_at"]
        local_reset = reset_at.astimezone()
        reset_str = local_reset.strftime("%d.%m в %H:%M")
        try:
            await bot.send_message(
                chat_id,
                f"⛔ {user_tag} (<code>{user_id}</code>) вы уже истратили все свои "
                f"сообщения (<b>{db.STAFF_JARVIS_LIMIT}/{db.STAFF_JARVIS_LIMIT}</b>).\n"
                f"Ожидайте перезагрузку Джарвиса на вас: <b>{reset_str}</b>.",
                parse_mode="HTML",
                reply_to_message_id=reply_to_message_id,
            )
        except Exception as e:
            logger.error(f"[JARVIS] Ошибка отправки лимита: {e}")
        return

    # ── Отвечаем ─────────────────────────────────────────────────────────────
    history = [{"role": "user", "content": text}]
    reply_text = await ask_jarvis(history)

    updated = await db.increment_staff_jarvis_usage(user_id)
    used = updated["used"]
    limit = db.STAFF_JARVIS_LIMIT

    try:
        await bot.send_message(
            chat_id,
            f"🤖 {reply_text}",
            reply_to_message_id=reply_to_message_id,
        )
        logger.info(f"[JARVIS] Ответ отправлен в группу {chat_id}")
    except Exception as e:
        logger.error(f"[JARVIS] Ошибка отправки в группу: {e}")

    # ── Счётчик после ответа ─────────────────────────────────────────────────
    if used >= limit:
        reset_at = updated["reset_at"]
        local_reset = reset_at.astimezone()
        reset_str = local_reset.strftime("%d.%m в %H:%M")
        counter_text = (
            f"⚠️ {user_tag} (<code>{user_id}</code>) вы истратили все "
            f"<b>{used}/{limit}</b> сообщений.\n"
            f"Перезагрузка Джарвиса на вас: <b>{reset_str}</b>."
        )
    else:
        counter_text = (
            f"💬 Вы истратили <b>{used}/{limit}</b> сообщений "
            f"{user_tag} (<code>{user_id}</code>)."
        )
    try:
        await bot.send_message(chat_id, counter_text, parse_mode="HTML")
    except Exception as e:
        logger.error(f"[JARVIS] Ошибка отправки счётчика: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# ПУБЛИЧНЫЙ ИНТЕРФЕЙС (ЛС-СЕССИИ)
# ══════════════════════════════════════════════════════════════════════════════

async def on_user_message(user_id: int, bot) -> None:
    """
    Вызывается из user_handler при каждом сообщении пользователя в ЛС.
    Создаёт сессию и запускает 5-минутный таймер ожидания живого админа.
    """
    session = await db.get_jarvis_session(user_id)
    if session is None or session["state"] == "closed":
        await db.create_jarvis_session(user_id)
        _start_admin_wait_timer(user_id, bot)
        logger.info(f"[JARVIS] Новая сессия для user_id={user_id}")


async def on_admin_replied(user_id: int) -> None:
    """
    Вызывается из reply_handler когда живой администратор ответил пользователю.
    Отменяет таймер — Джарвис НЕ включается.
    """
    _cancel_admin_wait(user_id)
    _cancel_jarvis_limit(user_id)
    await db.close_jarvis_session(user_id)
    logger.info(f"[JARVIS] Живой админ ответил {user_id} — таймер отменён")


async def stop_jarvis_early(user_id: int, bot) -> dict:
    """
    Досрочно завершает активную сессию Джарвиса по инициативе пользователя.

    Возвращает dict:
      used_sec     — сколько секунд пользователь провёл в диалоге
      remaining_sec — сколько секунд НЕ использовано (из 10 минут)
    Возвращает None если сессия не была активна.
    """
    JARVIS_LIMIT = JARVIS_LIMIT_SECONDS  # 10 * 60

    session = await db.get_jarvis_session(user_id)
    if not session or session.get("state") != "jarvis_active":
        return None

    # Считаем использованное и оставшееся время
    started_str = session.get("jarvis_started_at")
    used_sec = 0
    if started_str:
        try:
            started = datetime.fromisoformat(started_str)
            if started.tzinfo is None:
                started = started.replace(tzinfo=timezone.utc)
            used_sec = int((datetime.now(timezone.utc) - started).total_seconds())
            used_sec = min(used_sec, JARVIS_LIMIT)
        except ValueError:
            pass

    remaining_sec = max(0, JARVIS_LIMIT - used_sec)

    # Завершаем сессию и блокируем ИИ
    await db.close_jarvis_session(user_id)
    await db.block_jarvis_for_user(user_id, hours=24)
    _cancel_jarvis_limit(user_id)

    # Получаем данные пользователя для рапорта
    user_data = await db.get_user(user_id)
    username_str = (
        f"@{user_data['username']}" if user_data and user_data.get("username")
        else "без username"
    )

    # Рапорт в STAFF_GROUP — диалог свободен
    try:
        await bot.send_message(
            STAFF_GROUP_ID,
            f"🛑 <b>ВНИМАНИЕ:</b> Пользователь "
            f"<code>{user_id}</code> ({username_str}) досрочно завершил сеанс связи.\n"
            f"Связь свободна — вы снова можете отвечать, сер!",
            parse_mode="HTML",
        )
        logger.info(f"[JARVIS] Досрочная остановка {user_id}, рапорт отправлен в STAFF")
    except Exception as e:
        logger.error(f"[JARVIS] Ошибка рапорта при досрочной остановке: {e}")

    return {"used_sec": used_sec, "remaining_sec": remaining_sec}


async def on_jarvis_reply(user_id: int, user_text: str, bot) -> bool:
    """
    Если сессия Джарвиса активна — отвечает пользователю через g4f.
    Возвращает True если сообщение обработано (не нужно пересылать в группу).
    """
    session = await db.get_jarvis_session(user_id)
    if not session or session["state"] != "jarvis_active":
        return False

    # Проверяем не истёк ли 10-минутный лимит (владелец освобождён от лимита)
    user_data_check = await db.get_user(user_id)
    is_owner_check = (
        user_data_check and
        user_data_check.get("username", "").lower() == OWNER_USERNAME.lower()
    )
    if not is_owner_check and session.get("jarvis_started_at"):
        started = datetime.fromisoformat(session["jarvis_started_at"])
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        elapsed = (datetime.now(timezone.utc) - started).total_seconds()
        if elapsed >= JARVIS_LIMIT_SECONDS:
            await _jarvis_farewell(user_id, bot)
            return True

    # Загружаем историю, добавляем новое сообщение
    history = json.loads(session.get("history") or "[]")
    history.append({"role": "user", "content": user_text})

    # Особое приветствие для владельца при первом обращении
    user_data = await db.get_user(user_id)
    is_owner = (
        user_data and
        user_data.get("username", "").lower() == OWNER_USERNAME.lower()
    )
    if is_owner and len(history) == 1:
        reply_text = (
            "Добрый день, сер. Все системы активированы. "
            "Что бы вы хотели добавить или проверить во мне?"
        )
    else:
        # Запрос к g4f (никогда не бросает — есть fallback)
        reply_text = await ask_jarvis(history)

    history.append({"role": "assistant", "content": reply_text})
    if len(history) > 20:
        history = history[-20:]

    await db.update_jarvis_history(user_id, json.dumps(history, ensure_ascii=False))

    # Обновляем клавиатуру вместе с ответом — кнопка "Остановить Джарвиса" появится сразу
    try:
        from handlers.user_handler import main_menu_kb
        kb = await main_menu_kb(user_id)
    except Exception:
        kb = None

    try:
        await bot.send_message(user_id, f"🤖 {reply_text}", reply_markup=kb)
    except Exception as e:
        logger.error(f"[JARVIS] Ошибка отправки ответа юзеру: {e}")

    return True


# ══════════════════════════════════════════════════════════════════════════════
# ТАЙМЕРЫ
# ══════════════════════════════════════════════════════════════════════════════

def _start_admin_wait_timer(user_id: int, bot) -> None:
    _cancel_admin_wait(user_id)
    task = asyncio.create_task(_admin_wait_coro(user_id, bot))
    _admin_wait_tasks[user_id] = task
    logger.info(f"[JARVIS] ⏱ 5-мин таймер запущен для user_id={user_id}")


def _start_jarvis_limit_timer(user_id: int, bot) -> None:
    _cancel_jarvis_limit(user_id)
    task = asyncio.create_task(_jarvis_limit_coro(user_id, bot))
    _jarvis_limit_tasks[user_id] = task
    logger.info(f"[JARVIS] ⏱ 10-мин лимит запущен для user_id={user_id}")


def _cancel_admin_wait(user_id: int) -> None:
    task = _admin_wait_tasks.pop(user_id, None)
    if task and not task.done():
        task.cancel()


def _cancel_jarvis_limit(user_id: int) -> None:
    task = _jarvis_limit_tasks.pop(user_id, None)
    if task and not task.done():
        task.cancel()


async def _admin_wait_coro(user_id: int, bot) -> None:
    """Ждёт 5 минут. Если живой админ не ответил — включает Джарвиса."""
    try:
        await asyncio.sleep(ADMIN_WAIT_SECONDS)
    except asyncio.CancelledError:
        return

    session = await db.get_jarvis_session(user_id)
    if not session or session["state"] != "waiting_admin":
        return

    if await db.is_jarvis_blocked(user_id):
        logger.info(f"[JARVIS] ИИ заблокирован для {user_id} — не включаем")
        return

    await db.set_jarvis_state(user_id, "jarvis_active")
    _start_jarvis_limit_timer(user_id, bot)

    # Получаем данные пользователя для уведомления в STAFF_GROUP
    user_data = await db.get_user(user_id)
    username_str = f"@{user_data['username']}" if user_data and user_data.get("username") else "без username"

    greeting = (
        "🤖 <b>Привет! Я Джарвис</b> — виртуальный ассистент.\n\n"
        "Администраторы сейчас заняты, но я готов помочь вам.\n"
        "Задайте ваш вопрос — постараюсь ответить.\n\n"
        "<i>⏱ Диалог доступен в течение 10 минут.</i>"
    )
    # Обновляем клавиатуру — кнопка меняется на "🛑 Остановить Джарвиса"
    try:
        from handlers.user_handler import main_menu_kb
        kb = await main_menu_kb(user_id)
    except Exception:
        kb = None

    try:
        await bot.send_message(user_id, greeting, parse_mode="HTML", reply_markup=kb)
        print(f"[JARVIS] ✅ Приветствие отправлено → user_id={user_id}")
        logger.info(f"[JARVIS] Приветствие отправлено → {user_id}")
    except Exception as e:
        logger.error(f"[JARVIS] Ошибка отправки приветствия: {e}")

    # Уведомление в STAFF_GROUP — сообщаем администраторам что Джарвис взял диалог
    try:
        await bot.send_message(
            STAFF_GROUP_ID,
            f"🤖 <b>ВНИМАНИЕ:</b> Все протоколы переведены на меня.\n"
            f"Я беру на себя общение с пользователем "
            f"<code>{user_id}</code> ({username_str}) "
            f"на следующие 10 минут.\n"
            f"Прошу не вмешиваться в диалог, сер!",
            parse_mode="HTML",
        )
        logger.info(f"[JARVIS] Уведомление о старте отправлено в STAFF_GROUP")
    except Exception as e:
        logger.error(f"[JARVIS] Ошибка уведомления STAFF о старте: {e}")


async def _jarvis_limit_coro(user_id: int, bot) -> None:
    """Ждёт 10 минут, затем завершает сессию."""
    try:
        await asyncio.sleep(JARVIS_LIMIT_SECONDS)
    except asyncio.CancelledError:
        return
    await _jarvis_farewell(user_id, bot)


async def _jarvis_farewell(user_id: int, bot) -> None:
    """Завершает сессию, блокирует ИИ на 24 ч, возвращает главное меню."""
    session = await db.get_jarvis_session(user_id)
    if not session or session["state"] != "jarvis_active":
        return

    await db.close_jarvis_session(user_id)
    await db.block_jarvis_for_user(user_id, hours=24)
    _cancel_jarvis_limit(user_id)

    # Рассчитываем точное время разблокировки из БД
    unlock_dt = await db.get_jarvis_blocked_until(user_id)
    if unlock_dt:
        unlock_str = _format_unlock_dt(unlock_dt)
    else:
        # Страховка: считаем вручную если БД не вернула значение
        unlock_str = _format_unlock_dt(datetime.now(timezone.utc) + timedelta(hours=24))

    # Получаем данные пользователя для уведомления в STAFF_GROUP
    user_data = await db.get_user(user_id)
    username_str = f"@{user_data['username']}" if user_data and user_data.get("username") else "без username"

    farewell = (
        f"🤖 Мои системы требуют калибровки, сер.\n\n"
        f"Я буду доступен для вас снова:\n"
        f"<b>{unlock_str}</b> по местному времени.\n\n"
        f"Живой администратор свяжется с вами при первой возможности.\n"
        f"<i>До связи!</i>"
    )
    try:
        await bot.send_message(user_id, farewell, parse_mode="HTML")
        logger.info(f"[JARVIS] Сессия закрыта для {user_id}, разблокировка: {unlock_str}")
    except Exception as e:
        logger.error(f"[JARVIS] Ошибка отправки прощания: {e}")

    # Уведомление в STAFF_GROUP — сообщаем что сеанс завершён и диалог свободен
    try:
        await bot.send_message(
            STAFF_GROUP_ID,
            f"✅ <b>ОТЧЁТ:</b> Сеанс с пользователем "
            f"<code>{user_id}</code> ({username_str}) окончен.\n"
            f"Связь свободна — вы снова можете отвечать пользователю, сер!\n"
            f"Мои системы уходят на калибровку до <b>{unlock_str}</b>.",
            parse_mode="HTML",
        )
        logger.info(f"[JARVIS] Уведомление о завершении отправлено в STAFF_GROUP")
    except Exception as e:
        logger.error(f"[JARVIS] Ошибка уведомления STAFF о завершении: {e}")

    # Возвращаем главное меню после завершения диалога
    try:
        from handlers.user_handler import send_main_menu
        await send_main_menu(bot, user_id)
    except Exception as e:
        logger.error(f"[JARVIS] Ошибка отправки меню: {e}")