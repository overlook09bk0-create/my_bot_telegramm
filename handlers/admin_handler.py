"""
handlers/admin_handler.py — Управление ботом для администраторов.
Включает: бан/мут/разбан/размут (кнопки + команды), технический режим,
          статистика /stats, шаблоны /t1-/t3.
Команды в Группе Безопасности: /ban {id}, /mute {id} {время}.
"""

import re
from datetime import datetime, timezone, timedelta

from aiogram import Router, Bot, F
from aiogram.types import CallbackQuery, Message, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command

import database as db
import ai_timer
from config import ADMIN_GROUP_ID, STAFF_GROUP_ID

router = Router()

# ══════════════════════════════════════════════════════════════════════════════
# ШАБЛОНЫ ОТВЕТОВ
# ══════════════════════════════════════════════════════════════════════════════

TEMPLATES: dict[str, str] = {
    "t1": (
        "👋 <b>Добро пожаловать!</b>\n\n"
        "Благодарим за обращение. Мы рады приветствовать вас в нашем сообществе.\n\n"
        "📌 Ознакомьтесь с <b>правилами</b> — они помогут чувствовать себя комфортно.\n"
        "💬 Если возникнут вопросы — смело пишите, мы всегда на связи!\n\n"
        "С уважением, <b>Команда администраторов</b> ✨"
    ),
    "t2": (
        "📋 <b>Инструкция по работе с ботом:</b>\n\n"
        "1️⃣ Отправьте вопрос в личные сообщения боту\n"
        "2️⃣ Бот автоматически передаст его администратору\n"
        "3️⃣ Ожидайте ответа — обычно <b>до 2 часов</b> в рабочее время\n\n"
        "💡 <b>Быстрые ответы:</b> напишите ключевое слово:\n"
        "<i>правила / цена / как зайти / помощь / админ</i>\n\n"
        "📞 <b>Срочно?</b> Укажите «СРОЧНО» в начале сообщения."
    ),
    "t3": (
        "⚠️ <b>Предупреждение от администрации</b>\n\n"
        "Ваше поведение нарушает правила сообщества.\n\n"
        "🔸 Просим ознакомиться с правилами и соблюдать их.\n"
        "🔸 Повторное нарушение — <b>ограничение доступа</b>.\n\n"
        "Считаете это ошибкой — объясните ситуацию.\n\n"
        "<i>Администрация</i>"
    ),
}


# ══════════════════════════════════════════════════════════════════════════════
# УТИЛИТЫ
# ══════════════════════════════════════════════════════════════════════════════

def _parse_callback(data: str) -> tuple[str, int]:
    action, uid = data.rsplit(":", 1)
    return action, int(uid)


def _is_admin_chat(chat_id: int) -> bool:
    return chat_id in (ADMIN_GROUP_ID, STAFF_GROUP_ID)


def _user_label(u: dict) -> str:
    name = u.get("first_name") or "—"
    username = f"@{u['username']}" if u.get("username") else "без username"
    return f"{name} ({username}) | <code>{u['user_id']}</code>"


def _admin_label(admin) -> str:
    return admin.username or admin.full_name or str(admin.id)


async def _notify_security(bot: Bot, text: str) -> None:
    try:
        await bot.send_message(ADMIN_GROUP_ID, text, parse_mode="HTML")
    except Exception:
        pass


def _parse_mute_duration(arg: str) -> timedelta | None:
    """
    Парсит строку вида '10m', '2h', '1d' в timedelta.
    Возвращает None если формат неверный.
    """
    m = re.fullmatch(r"(\d+)([mhd])", arg.strip().lower())
    if not m:
        return None
    amount, unit = int(m.group(1)), m.group(2)
    return {"m": timedelta(minutes=amount),
            "h": timedelta(hours=amount),
            "d": timedelta(days=amount)}[unit]


# ══════════════════════════════════════════════════════════════════════════════
# ТЕХНИЧЕСКИЙ РЕЖИМ
# ══════════════════════════════════════════════════════════════════════════════

@router.message(Command("turnoffbot"))
async def cmd_turn_off_bot(message: Message, bot: Bot):
    if message.chat.id != ADMIN_GROUP_ID:
        return
    await db.set_bot_status(False)
    await message.answer(
        "🔴 Системы переведены в режим ожидания, сер. Техническое обслуживание начато."
    )
    await bot.send_message(
        STAFF_GROUP_ID,
        "⚠️ <b>Внимание:</b> Бот временно отключён администрацией.",
        parse_mode="HTML",
    )


@router.message(Command("turnonbot"))
async def cmd_turn_on_bot(message: Message, bot: Bot):
    if message.chat.id != ADMIN_GROUP_ID:
        return
    await db.set_bot_status(True)
    await message.answer(
        "🟢 Системы онлайн, сер. Все протоколы безопасности активны."
    )


# ══════════════════════════════════════════════════════════════════════════════
# КОМАНДА /ban {user_id} — текстовый бан без кнопки
# ══════════════════════════════════════════════════════════════════════════════

@router.message(Command("ban"))
async def cmd_ban(message: Message, bot: Bot):
    if message.chat.id != ADMIN_GROUP_ID:
        return

    args = (message.text or "").split()
    if len(args) < 2 or not args[1].lstrip("-").isdigit():
        await message.answer("⚠️ Использование: <code>/ban {user_id}</code>", parse_mode="HTML")
        return

    user_id = int(args[1])
    admin = message.from_user
    admin_label = _admin_label(admin)
    reason = " ".join(args[2:]) or f"Ручной бан от @{admin_label}"

    await db.ban_user(user_id, reason)
    await db.log_admin_action(admin.id, admin_label, "ban")
    await ai_timer.on_admin_replied(user_id)  # закрываем сессию Джарвиса

    try:
        await bot.send_message(
            user_id,
            "⛔ Вы были заблокированы администратором. "
            "Дальнейшие обращения не будут обработаны."
        )
    except Exception:
        pass

    await message.answer(f"✅ Пользователь <code>{user_id}</code> заблокирован.", parse_mode="HTML")
    await _notify_security(bot,
        f"🚫 Объект <code>{user_id}</code> получил <b>БАН</b> от @{admin_label}"
    )


# ══════════════════════════════════════════════════════════════════════════════
# КОМАНДА /mute {user_id} {duration} — текстовый мут без кнопки
# Форматы duration: 10m, 2h, 1d
# ══════════════════════════════════════════════════════════════════════════════

@router.message(Command("mute"))
async def cmd_mute(message: Message, bot: Bot):
    if message.chat.id != ADMIN_GROUP_ID:
        return

    args = (message.text or "").split()
    if len(args) < 3:
        await message.answer(
            "⚠️ Использование: <code>/mute {user_id} {10m|2h|1d}</code>",
            parse_mode="HTML"
        )
        return

    if not args[1].lstrip("-").isdigit():
        await message.answer("⚠️ Неверный user_id.", parse_mode="HTML")
        return

    user_id = int(args[1])
    delta = _parse_mute_duration(args[2])
    if delta is None:
        await message.answer(
            "⚠️ Неверный формат времени. Используйте: <code>10m</code>, <code>2h</code>, <code>1d</code>",
            parse_mode="HTML"
        )
        return

    admin = message.from_user
    admin_label = _admin_label(admin)
    until = datetime.now(timezone.utc) + delta
    await db.set_mute(user_id, until.isoformat())
    await db.log_admin_action(admin.id, admin_label, "mute")

    try:
        await bot.send_message(user_id, f"🔇 Вы ограничены в отправке сообщений до {until.strftime('%d.%m.%Y %H:%M')} UTC.")
    except Exception:
        pass

    await message.answer(
        f"✅ Мут для <code>{user_id}</code> до {until.strftime('%d.%m.%Y %H:%M')} UTC.",
        parse_mode="HTML"
    )
    await _notify_security(bot,
        f"🔇 Объект <code>{user_id}</code> получил <b>МУТ</b> ({args[2]}) от @{admin_label}"
    )


# ══════════════════════════════════════════════════════════════════════════════
# /stats — ТОП-3 активных администраторов
# ══════════════════════════════════════════════════════════════════════════════

@router.message(Command("stats"))
async def cmd_stats(message: Message):
    if message.chat.id != ADMIN_GROUP_ID:
        return

    top = await db.get_top_admins(limit=3)
    if not top:
        await message.answer("📊 Статистика пока пуста.")
        return

    medals = ["🥇", "🥈", "🥉"]
    lines = ["📊 <b>ТОП-3 активных администраторов</b>\n"]
    for i, a in enumerate(top):
        medal = medals[i] if i < len(medals) else f"{i+1}."
        name = a.get("admin_name") or f"ID {a['admin_id']}"
        lines.append(
            f"{medal} <b>{name}</b>\n"
            f"   💬 {a['replies']} отв. | 🚫 {a['bans']} банов | "
            f"🔇 {a['mutes']} мутов | ⚠️ {a['warns']} варнов\n"
            f"   Всего: <b>{a['total']}</b>"
        )
    await message.answer("\n".join(lines), parse_mode="HTML")


# ══════════════════════════════════════════════════════════════════════════════
# ШАБЛОНЫ: /t1, /t2, /t3
# ══════════════════════════════════════════════════════════════════════════════

@router.message(Command("t1"))
async def cmd_t1(message: Message):
    if _is_admin_chat(message.chat.id):
        await message.answer(TEMPLATES["t1"], parse_mode="HTML")

@router.message(Command("t2"))
async def cmd_t2(message: Message):
    if _is_admin_chat(message.chat.id):
        await message.answer(TEMPLATES["t2"], parse_mode="HTML")

@router.message(Command("t3"))
async def cmd_t3(message: Message):
    if _is_admin_chat(message.chat.id):
        await message.answer(TEMPLATES["t3"], parse_mode="HTML")


# ══════════════════════════════════════════════════════════════════════════════
# CALLBACK: бан (кнопка)
# ══════════════════════════════════════════════════════════════════════════════

@router.callback_query(F.data.startswith("ban:"))
async def cb_ban(call: CallbackQuery, bot: Bot):
    if not _is_admin_chat(call.message.chat.id):
        return await call.answer("⛔ Нет доступа.")

    _, user_id = _parse_callback(call.data)
    admin = call.from_user
    admin_label = _admin_label(admin)

    await db.ban_user(user_id, f"Ручной бан от @{admin_label}")
    await db.log_admin_action(admin.id, admin_label, "ban")
    await ai_timer.on_admin_replied(user_id)

    try:
        await bot.send_message(
            user_id,
            "⛔ Вы были заблокированы администратором. "
            "Дальнейшие обращения не будут обработаны."
        )
    except Exception:
        pass

    await call.message.edit_reply_markup(reply_markup=None)
    await call.answer(f"✅ {user_id} заблокирован.")
    await _notify_security(bot,
        f"🚫 Объект <code>{user_id}</code> получил <b>БАН</b> от @{admin_label}"
    )


# ══════════════════════════════════════════════════════════════════════════════
# CALLBACK: мут 1 час (кнопка)
# ══════════════════════════════════════════════════════════════════════════════

@router.callback_query(F.data.startswith("mute_1h:"))
async def cb_mute_1h(call: CallbackQuery, bot: Bot):
    if not _is_admin_chat(call.message.chat.id):
        return await call.answer("⛔ Нет доступа.")

    _, user_id = _parse_callback(call.data)
    admin = call.from_user
    admin_label = _admin_label(admin)
    until = datetime.now(timezone.utc) + timedelta(hours=1)

    await db.set_mute(user_id, until.isoformat())
    await db.log_admin_action(admin.id, admin_label, "mute")

    try:
        await bot.send_message(user_id, "🔇 Вы ограничены в отправке сообщений на 1 час.")
    except Exception:
        pass

    await call.message.edit_reply_markup(reply_markup=None)
    await call.answer(f"✅ Мут 1ч для {user_id}.")
    await _notify_security(bot,
        f"🔇 Объект <code>{user_id}</code> получил <b>МУТ 1 ЧАС</b> от @{admin_label}"
    )


# ══════════════════════════════════════════════════════════════════════════════
# CALLBACK: мут 7 дней (кнопка)
# ══════════════════════════════════════════════════════════════════════════════

@router.callback_query(F.data.startswith("mute_7d:"))
async def cb_mute_7d(call: CallbackQuery, bot: Bot):
    if not _is_admin_chat(call.message.chat.id):
        return await call.answer("⛔ Нет доступа.")

    _, user_id = _parse_callback(call.data)
    admin = call.from_user
    admin_label = _admin_label(admin)
    until = datetime.now(timezone.utc) + timedelta(days=7)

    await db.set_mute(user_id, until.isoformat())
    await db.log_admin_action(admin.id, admin_label, "mute")

    try:
        await bot.send_message(user_id, "🔇 Вы ограничены в отправке сообщений на 7 дней.")
    except Exception:
        pass

    await call.message.edit_reply_markup(reply_markup=None)
    await call.answer(f"✅ Мут 7д для {user_id}.")
    await _notify_security(bot,
        f"🔇 Объект <code>{user_id}</code> получил <b>МУТ 7 ДНЕЙ</b> от @{admin_label}"
    )


# ══════════════════════════════════════════════════════════════════════════════
# /razmut — список замученных
# ══════════════════════════════════════════════════════════════════════════════

@router.message(Command("razmut"))
async def cmd_razmut(message: Message):
    if message.chat.id != ADMIN_GROUP_ID:
        return

    muted = await db.get_muted_users()
    if not muted:
        await message.answer("✅ Замученных пользователей нет.")
        return

    lines = ["🔇 <b>Замученные пользователи:</b>\n"]
    buttons = []
    for u in muted:
        try:
            dt = datetime.fromisoformat(u.get("mute_until", ""))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            until_str = dt.strftime("%d.%m.%Y %H:%M UTC")
        except Exception:
            until_str = "неизвестно"
        lines.append(f"• {_user_label(u)}\n  До: {until_str}")
        buttons.append([InlineKeyboardButton(
            text=f"🔊 Размутить {u['user_id']}",
            callback_data=f"unmute:{u['user_id']}",
        )])

    await message.answer(
        "\n".join(lines), parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )


# ══════════════════════════════════════════════════════════════════════════════
# /razban — список забаненных
# ══════════════════════════════════════════════════════════════════════════════

@router.message(Command("razban"))
async def cmd_razban(message: Message):
    if message.chat.id != ADMIN_GROUP_ID:
        return

    banned = await db.get_banned_users()
    if not banned:
        await message.answer("✅ Забаненных пользователей нет.")
        return

    lines = ["🚫 <b>Забаненные пользователи:</b>\n"]
    buttons = []
    for u in banned:
        reason = u.get("ban_reason") or "причина не указана"
        lines.append(f"• {_user_label(u)}\n  Причина: {reason}")
        buttons.append([InlineKeyboardButton(
            text=f"✅ Разбанить {u['user_id']}",
            callback_data=f"unban:{u['user_id']}",
        )])

    await message.answer(
        "\n".join(lines), parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )


# ══════════════════════════════════════════════════════════════════════════════
# CALLBACK: разбан
# ══════════════════════════════════════════════════════════════════════════════

@router.callback_query(F.data.startswith("unban:"))
async def cb_unban(call: CallbackQuery, bot: Bot):
    if not _is_admin_chat(call.message.chat.id):
        return await call.answer("⛔ Нет доступа.")

    _, user_id = _parse_callback(call.data)
    admin = call.from_user

    await db.unban_user(user_id)
    try:
        await bot.send_message(user_id, "✅ Вы разблокированы. Можете снова обращаться к нам.")
    except Exception:
        pass
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    await call.answer(f"✅ {user_id} разбанен.")
    await _notify_security(bot,
        f"✅ <b>РАЗБАН</b>: объект <code>{user_id}</code> — @{_admin_label(admin)}"
    )


# ══════════════════════════════════════════════════════════════════════════════
# CALLBACK: размут
# ══════════════════════════════════════════════════════════════════════════════

@router.callback_query(F.data.startswith("unmute:"))
async def cb_unmute(call: CallbackQuery, bot: Bot):
    if not _is_admin_chat(call.message.chat.id):
        return await call.answer("⛔ Нет доступа.")

    _, user_id = _parse_callback(call.data)
    admin = call.from_user

    await db.unmute_user(user_id)
    try:
        await bot.send_message(user_id, "🔊 Ограничение на отправку сообщений снято.")
    except Exception:
        pass
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    await call.answer(f"✅ Мут снят с {user_id}.")
    await _notify_security(bot,
        f"🔊 <b>РАЗМУТ</b>: объект <code>{user_id}</code> — @{_admin_label(admin)}"
    )


# ══════════════════════════════════════════════════════════════════════════════
# CALLBACK: заявка в администрацию — принять / отклонить
# ══════════════════════════════════════════════════════════════════════════════

@router.callback_query(F.data.startswith("apply_accept:"))
async def cb_apply_accept(call: CallbackQuery, bot: Bot):
    if call.message.chat.id != ADMIN_GROUP_ID:
        return await call.answer("⛔ Нет доступа.")

    _, user_id = _parse_callback(call.data)
    admin = call.from_user

    await db.set_application_status(user_id, "accepted")
    try:
        await bot.send_message(
            user_id,
            "✅ <b>Поздравляем!</b>\n\n"
            "Вас приняли в администрацию. Ожидайте — администратор напишет вам "
            "в ближайшее время с инструкциями.",
            parse_mode="HTML",
        )
    except Exception:
        pass

    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    await call.answer("✅ Заявка принята.")
    await call.message.reply(
        f"✅ Заявка <b>принята</b> администратором @{_admin_label(admin)}.\n"
        f"Уведомление отправлено пользователю <code>{user_id}</code>.",
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("apply_reject:"))
async def cb_apply_reject(call: CallbackQuery, bot: Bot):
    if call.message.chat.id != ADMIN_GROUP_ID:
        return await call.answer("⛔ Нет доступа.")

    _, user_id = _parse_callback(call.data)
    admin = call.from_user

    await db.set_application_status(user_id, "rejected")
    try:
        await bot.send_message(
            user_id,
            "😔 К сожалению, на этот раз вы нам не подошли.\n\n"
            "Не расстраивайтесь — возможно, в будущем мы снова откроем набор. "
            "Спасибо за интерес к нашей команде!",
        )
    except Exception:
        pass

    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    await call.answer("❌ Заявка отклонена.")
    await call.message.reply(
        f"❌ Заявка <b>отклонена</b> администратором @{_admin_label(admin)}.\n"
        f"Уведомление отправлено пользователю <code>{user_id}</code>.",
        parse_mode="HTML",
    )


# ══════════════════════════════════════════════════════════════════════════════
# /turnOffAi и /turnOnAi — управление Джарвисом
# ══════════════════════════════════════════════════════════════════════════════

@router.message(Command("turnOffAi"))
async def cmd_turn_off_ai(message: Message, bot: Bot):
    if not _is_admin_chat(message.chat.id):
        return

    if not await db.is_ai_active():
        await message.reply("⚠️ Джарвис уже выключен.")
        return

    await db.set_ai_status(False)
    admin = message.from_user
    admin_str = f"@{admin.username}" if admin.username else admin.full_name

    notice = (
        f"🔴 <b>Джарвис отключён.</b>\n"
        f"ИИ-ассистент выключен администратором {admin_str}.\n"
        f"Пользователи не смогут вызвать Джарвиса до повторного включения."
    )
    for gid in (ADMIN_GROUP_ID, STAFF_GROUP_ID):
        try:
            await bot.send_message(gid, notice, parse_mode="HTML")
        except Exception:
            pass
    logger.info(f"[AI] Джарвис выключен администратором {admin_str}")


@router.message(Command("turnOnAi"))
async def cmd_turn_on_ai(message: Message, bot: Bot):
    if not _is_admin_chat(message.chat.id):
        return

    if await db.is_ai_active():
        await message.reply("⚠️ Джарвис уже включён.")
        return

    await db.set_ai_status(True)
    admin = message.from_user
    admin_str = f"@{admin.username}" if admin.username else admin.full_name

    notice = (
        f"🟢 <b>Джарвис включён.</b>\n"
        f"ИИ-ассистент активирован администратором {admin_str}.\n"
        f"Пользователи снова могут вызывать Джарвиса."
    )
    for gid in (ADMIN_GROUP_ID, STAFF_GROUP_ID):
        try:
            await bot.send_message(gid, notice, parse_mode="HTML")
        except Exception:
            pass
    logger.info(f"[AI] Джарвис включён администратором {admin_str}")