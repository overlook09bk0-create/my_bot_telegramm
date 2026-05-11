"""
handlers/reply_handler.py — Обработка сообщений в группах ADMIN и STAFF.

Функции:
  1. /report (reply на сообщение) → репорт в группу Безопасности с кнопками мут/бан
  2. Reply от живого администратора → пересылает ответ пользователю в ЛС
  3. Обращение к Джарвису ("джарвис" в тексте) → Джарвис отвечает в группу
"""

import logging
import re

from aiogram import Router, Bot, F
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command

import database as db
import ai_timer
from config import ADMIN_GROUP_ID, STAFF_GROUP_ID

logger = logging.getLogger(__name__)
router = Router()


# ══════════════════════════════════════════════════════════════════════════════
# ИЗВЛЕЧЕНИЕ USER_ID
# ══════════════════════════════════════════════════════════════════════════════

def _extract_user_id(msg: Message) -> int | None:
    """
    Извлекает user_id из заголовочного сообщения бота.
    Паттерны в порядке приоритета:

      1. "... | 123456789"        — стандартный заголовок открытого обращения
      2. "ID: 123456789"          — анонимный заголовок (copy_message)
      3. "Действия для 123456789" — кнопки досье
    """
    if not msg:
        return None
    text = msg.text or msg.caption or ""

    m = re.search(r"\|\s*(\d{5,12})(?:\s|$)", text)
    if m:
        return int(m.group(1))

    m = re.search(r"ID[:\s]+(\d{5,12})", text, re.IGNORECASE)
    if m:
        return int(m.group(1))

    m = re.search(r"Действия для\s+(\d{5,12})", text)
    if m:
        return int(m.group(1))

    return None


async def _resolve_user_id(replied: Message, chat_id: int) -> int | None:
    """
    Полное извлечение user_id из reply — все источники по приоритету:

      1. forward_origin.sender_user.id — если профиль открыт
      2. forward_from.id               — старый API Telegram
      3. Парсинг текста заголовка бота (| ID или ID:)
      4. БД forwarded_messages         — по message_id пересланного сообщения
         (работает когда профиль скрыт и forward без данных отправителя)
    """
    if not replied:
        return None

    # Источник 1: forward_origin (новый API)
    if replied.forward_origin:
        try:
            uid = replied.forward_origin.sender_user.id
            if uid:
                return uid
        except AttributeError:
            pass

    # Источник 2: forward_from (старый API)
    if getattr(replied, "forward_from", None):
        return replied.forward_from.id

    # Источник 3: парсим текст заголовка
    uid = _extract_user_id(replied)
    if uid:
        return uid

    # Источник 4: ищем по message_id в БД
    uid = await db.get_user_id_by_message(chat_id, replied.message_id)
    if uid:
        return uid

    # Источник 5: если это reply на заголовок — ищем в родительском сообщении
    if replied.reply_to_message:
        uid = _extract_user_id(replied.reply_to_message)
        if uid:
            return uid
        uid = await db.get_user_id_by_message(chat_id, replied.reply_to_message.message_id)
        if uid:
            return uid

    return None


def _report_kb(user_id: int) -> InlineKeyboardMarkup:
    """Кнопки мут/бан для репорта в группе Безопасности."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="🔇 Мут 1 час",
                callback_data=f"mute_1h:{user_id}",
            ),
            InlineKeyboardButton(
                text="🔇 Мут 7 дней",
                callback_data=f"mute_7d:{user_id}",
            ),
        ],
        [
            InlineKeyboardButton(
                text="🚫 Забанить",
                callback_data=f"ban:{user_id}",
            ),
        ],
    ])


# ══════════════════════════════════════════════════════════════════════════════
# /report — репорт на пользователя в группу Безопасности
# ══════════════════════════════════════════════════════════════════════════════

@router.message(Command("report"), F.chat.id.in_({ADMIN_GROUP_ID, STAFF_GROUP_ID}))
async def cmd_report(message: Message, bot: Bot):
    """
    Использование: reply на сообщение пользователя + /report [причина]

    Отправляет в ADMIN_GROUP_ID репорт:
      - кто жалуется
      - ID и username нарушителя
      - само сообщение пользователя
      - кнопки мут / бан
    """
    if message.from_user and message.from_user.is_bot:
        return

    if not message.reply_to_message:
        await message.reply(
            "⚠️ Используйте /report как <b>ответ</b> на сообщение пользователя.\n"
            "Например: ответьте на сообщение и напишите <code>/report спам</code>",
            parse_mode="HTML",
        )
        return

    replied = message.reply_to_message

    # Ищем user_id — все источники включая БД
    user_id = await _resolve_user_id(replied, message.chat.id)

    if not user_id:
        await message.reply(
            "⚠️ Не удалось определить пользователя.\n"
            "Сделайте reply прямо на сообщение пользователя (не на заголовок бота)."
        )
        return

    # Получаем данные пользователя из БД
    user_data = await db.get_user(user_id)
    username_str = f"@{user_data['username']}" if user_data and user_data.get("username") else "без username"
    name_str = user_data.get("first_name") or "—" if user_data else "—"

    # Причина из аргументов команды
    args = (message.text or "").split(maxsplit=1)
    reason = args[1].strip() if len(args) > 1 else "не указана"

    # Репортующий админ
    reporter = message.from_user
    reporter_str = f"@{reporter.username}" if reporter.username else reporter.full_name

    # Текст репортуемого сообщения
    reported_text = (
        replied.text or replied.caption or "[медиа без текста]"
    )[:1000]

    report_text = (
        f"🚨 <b>РЕПОРТ НА ПОЛЬЗОВАТЕЛЯ</b>\n\n"
        f"👤 <b>Нарушитель:</b> {name_str} {username_str}\n"
        f"🆔 <b>ID:</b> <code>{user_id}</code>\n\n"
        f"📝 <b>Сообщение:</b>\n"
        f"<blockquote>{reported_text}</blockquote>\n\n"
        f"⚠️ <b>Причина:</b> {reason}\n"
        f"👮 <b>Репорт от:</b> {reporter_str}"
    )

    try:
        await bot.send_message(
            ADMIN_GROUP_ID,
            report_text,
            parse_mode="HTML",
            reply_markup=_report_kb(user_id),
        )
        await message.reply("✅ Репорт отправлен в группу Безопасности.")
        logger.info(f"[REPORT] user_id={user_id} репортован {reporter_str}, причина: {reason}")
    except Exception as e:
        logger.error(f"[REPORT] Ошибка отправки: {e}")
        await message.reply(f"❌ Не удалось отправить репорт: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# ГЛАВНЫЙ ОБРАБОТЧИК ГРУПП
# ══════════════════════════════════════════════════════════════════════════════

# ══════════════════════════════════════════════════════════════════════════════
# /link {id} и /unlink — прямая связь админа с пользователем
# ══════════════════════════════════════════════════════════════════════════════

@router.message(Command("link"), F.chat.id == STAFF_GROUP_ID)
async def cmd_link(message: Message, bot: Bot):
    """
    /link 123456789 — админ устанавливает прямую связь с пользователем.
    Все сообщения этого админа в группе автоматически летят пользователю.
    """
    if message.from_user.is_bot:
        return

    args = (message.text or "").split()
    if len(args) < 2 or not args[1].isdigit():
        await message.reply(
            "⚠️ Укажите ID пользователя.\n"
            "Пример: <code>/link 123456789</code>",
            parse_mode="HTML",
        )
        return

    target_user_id = int(args[1])
    admin = message.from_user
    admin_name = f"@{admin.username}" if admin.username else admin.full_name

    # Проверяем не слинкован ли уже кто-то с этим пользователем
    existing = await db.get_link_by_user(target_user_id)
    if existing and existing["admin_id"] != admin.id:
        other_admin = existing["admin_name"]
        await message.reply(
            f"🚫 Пользователь <code>{target_user_id}</code> уже занят — "
            f"с ним общается {other_admin}.",
            parse_mode="HTML",
        )
        return

    # Проверяем не заблокирован ли пользователь
    if await db.is_banned(target_user_id):
        await message.reply(f"⛔ Пользователь <code>{target_user_id}</code> заблокирован.", parse_mode="HTML")
        return

    await db.set_link(admin.id, admin_name, target_user_id)

    await message.reply(
        f"🔗 <b>Прямая связь установлена!</b>\n\n"
        f"Теперь все ваши сообщения в этой группе автоматически "
        f"отправляются пользователю <code>{target_user_id}</code>.\n"
        f"Для завершения напишите <code>/unlink</code>.",
        parse_mode="HTML",
    )

    # Уведомляем пользователя
    try:
        await bot.send_message(
            target_user_id,
            "👤 <b>Администратор подключился к диалогу.</b>\n"
            "Теперь вы общаетесь напрямую с оператором.",
            parse_mode="HTML",
        )
    except Exception:
        pass

    logger.info(f"[LINK] {admin_name} → user_id={target_user_id}")


@router.message(Command("unlink"), F.chat.id == STAFF_GROUP_ID)
async def cmd_unlink(message: Message, bot: Bot):
    """Завершает прямую связь с пользователем."""
    if message.from_user.is_bot:
        return

    admin = message.from_user
    link = await db.get_link_by_admin(admin.id)

    if not link:
        await message.reply("⚠️ У вас нет активной прямой связи.")
        return

    user_id = link["user_id"]
    await db.remove_link(admin.id)

    admin_name = f"@{admin.username}" if admin.username else admin.full_name
    await message.reply(
        f"🔓 <b>Прямая связь завершена.</b>\n"
        f"Пользователь <code>{user_id}</code> отключён.",
        parse_mode="HTML",
    )

    # Уведомляем пользователя
    try:
        await bot.send_message(
            user_id,
            "👤 Оператор завершил прямой диалог. "
            "Если остались вопросы — просто напишите сообщение.",
        )
    except Exception:
        pass

    logger.info(f"[UNLINK] {admin_name} отключился от user_id={user_id}")


@router.message(F.chat.id == STAFF_GROUP_ID)
async def handle_group_message(message: Message, bot: Bot):
    """
    Обрабатывает ВСЕ сообщения в группах Безопасности и Персонала.
      - Если упоминается "джарвис" — Джарвис отвечает в группу
      - Если это reply от живого человека — пересылает ответ пользователю в ЛС
    """
    if message.from_user and message.from_user.is_bot:
        return

    text = message.text or message.caption or ""

    # ── БЛОК 1: Обращение к Джарвису ─────────────────────────────────────────
    if text and ai_timer.is_jarvis_mention(text):
        print(
            f"[JARVIS] ✅ Обращение к Джарвису | "
            f"chat_id={message.chat.id} | "
            f"@{message.from_user.username or message.from_user.id} | "
            f"{text[:80]!r}"
        )
        logger.info(f"[JARVIS] Обращение в группе {message.chat.id}: {text[:80]!r}")

        if not await db.is_bot_active():
            await message.reply(
                "⚙️ Системы на техническом обслуживании, сер. "
                "Джарвис временно недоступен."
            )
            return

        await ai_timer.handle_staff_jarvis_mention(
            text=text,
            bot=bot,
            chat_id=message.chat.id,
            user_id=message.from_user.id,
            username=message.from_user.username or "",
            reply_to_message_id=message.message_id,
        )
        return

    # ── БЛОК 1.5: Прямая связь (/link) — сообщение летит без reply ──────────
    if not message.from_user.is_bot:
        link = await db.get_link_by_admin(message.from_user.id)
        if link:
            user_id = link["user_id"]
            if await db.is_banned(user_id):
                await message.reply("⛔ Пользователь заблокирован.")
                return
            try:
                # Если админ делает reply — ищем message_id оригинала в боте для цитаты
                reply_msg_id = None
                if message.reply_to_message:
                    reply_msg_id = await db.get_user_id_by_message(
                        message.chat.id, message.reply_to_message.message_id
                    )
                    # get_user_id_by_message возвращает user_id, нам нужен именно message_id
                    # который бот отправил пользователю — ищем в forwarded_messages
                    reply_msg_id = await db.get_bot_message_id(
                        message.chat.id, message.reply_to_message.message_id, user_id
                    )

                await _send_reply_to_user(bot, user_id, message, reply_to_msg_id=reply_msg_id)
                await message.reply("✅ Доставлено.")
                admin = message.from_user
                await db.log_admin_action(admin.id, admin.username or admin.full_name, "reply")
            except Exception as e:
                await message.reply(f"❌ Ошибка: {e}")
            return

        # Проверяем не слинкован ли кто-то с пользователем которому хочет ответить
        # (через обычный reply) — блокируем если другой админ уже в прямой связи
        if message.reply_to_message:
            replied_uid = await _resolve_user_id(message.reply_to_message, message.chat.id)
            if replied_uid:
                busy_link = await db.get_link_by_user(replied_uid)
                if busy_link and busy_link["admin_id"] != message.from_user.id:
                    await message.reply(
                        f"🚫 Пользователь <code>{replied_uid}</code> сейчас занят — "
                        f"с ним общается {busy_link['admin_name']} напрямую.\n"
                        f"Дождитесь команды <code>/unlink</code>.",
                        parse_mode="HTML",
                    )
                    return

    # ── БЛОК 2: Reply живого администратора → пересылка пользователю ─────────
    if not message.reply_to_message:
        return

    if not await db.is_bot_active():
        await message.reply(
            "⚙️ Ошибка, сер. Система на обслуживании. Ответы временно недоступны."
        )
        return

    replied = message.reply_to_message

    # Ищем user_id — все источники включая БД
    user_id = await _resolve_user_id(replied, message.chat.id)

    if not user_id:
        # Молча игнорируем — это обычный reply между участниками группы
        logger.debug(
            f"[REPLY] user_id не найден (обычный reply) | "
            f"chat={message.chat.id} | "
            f"replied_text={repr((replied.text or '')[:100])}"
        )
        return

    if await db.is_banned(user_id):
        await message.reply("⛔ Пользователь заблокирован, сообщение не отправлено.")
        return

    # ── Блокировка: проверяем не активен ли сейчас Джарвис для этого юзера ───
    remaining = await db.get_jarvis_remaining_seconds(user_id)
    if remaining > 0:
        remaining_min = max(1, round(remaining / 60))
        await message.reply(
            f"🚫 <b>ОШИБКА:</b> Сер, этот пользователь сейчас общается с Джарвисом.\n"
            f"Пожалуйста, дождитесь окончания таймера "
            f"(через <b>~{remaining_min} мин</b>), "
            f"прежде чем брать диалог на себя!",
            parse_mode="HTML",
        )
        logger.info(f"[REPLY] Заблокирован ответ админа → user_id={user_id}, осталось {remaining}с")
        return

    # ── Отправка ответа пользователю ─────────────────────────────────────────
    try:
        await _send_reply_to_user(bot, user_id, message)
        await message.reply("✅ Сообщение доставлено.")
        logger.info(f"[REPLY] Ответ доставлен → user_id={user_id}")

        admin = message.from_user
        admin_name = admin.username or admin.full_name or str(admin.id)
        await db.log_admin_action(admin.id, admin_name, "reply")
        await ai_timer.on_admin_replied(user_id)

    except Exception as e:
        logger.error(f"[REPLY] Ошибка → user_id={user_id}: {e}")
        await message.reply(f"❌ Не удалось отправить: {e}")


async def _send_reply_to_user(
    bot: Bot,
    user_id: int,
    message: Message,
    reply_to_msg_id: int | None = None,
) -> None:
    """
    Отправляет ответ администратора пользователю в ЛС.
    Если reply_to_msg_id указан — делает цитату (reply) на конкретное сообщение.
    Поддерживает текст, фото, видео, документы, голосовые, стикеры.
    """
    caption = message.caption or ""
    text    = message.text or ""
    kwargs  = {"reply_to_message_id": reply_to_msg_id} if reply_to_msg_id else {}

    if message.photo:
        await bot.send_photo(
            user_id, photo=message.photo[-1].file_id,
            caption=f"💬 {caption}" if caption else None,
            **kwargs,
        )
    elif message.video:
        await bot.send_video(
            user_id, video=message.video.file_id,
            caption=f"💬 {caption}" if caption else None,
            **kwargs,
        )
    elif message.document:
        await bot.send_document(
            user_id, document=message.document.file_id,
            caption=f"💬 {caption}" if caption else None,
            **kwargs,
        )
    elif message.voice:
        await bot.send_voice(user_id, voice=message.voice.file_id, **kwargs)
    elif message.audio:
        await bot.send_audio(user_id, audio=message.audio.file_id, **kwargs)
    elif message.sticker:
        await bot.send_sticker(user_id, sticker=message.sticker.file_id, **kwargs)
    elif message.video_note:
        await bot.send_video_note(user_id, video_note=message.video_note.file_id, **kwargs)
    elif text:
        await bot.send_message(user_id, f"💬 {text}", **kwargs)
    else:
        await bot.copy_message(
            chat_id=user_id,
            from_chat_id=message.chat.id,
            message_id=message.message_id,
            **kwargs,
        )