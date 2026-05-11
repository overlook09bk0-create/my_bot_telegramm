"""
handlers/user_handler.py — Обработка сообщений пользователей в ЛС.

Приоритет обработки:
  1.  Технический режим → стоп
  2.  Бан / мут → стоп
  3.  Анти-спам
  4.  Регистрация нового пользователя + досье
  5.  Нецензурная лексика (варны 1/3 → мут)
  6.  Угрозы / доксинг (авто-бан)
  7.  ★ Активная сессия Джарвиса → ИИ перехватывает ВСЁ (FAQ не срабатывает)
  8.  Прямое обращение "Джарвис, ..." → немедленный запуск ИИ
  9.  FAQ по ключевым словам
  10. Кнопки меню (🆘 Написать, 📜 Правила, 👤 Мой ID)
  11. Стандарт: тикет + таймер + пересылка в группу
"""

import json
import logging
import time
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

from aiogram import Router, Bot, F
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext

from states import AdminApplication

import database as db
import ai_timer
from config import ADMIN_GROUP_ID, STAFF_GROUP_ID, ARCHIVE_GROUP_ID
from keyboards.admin_kb import admin_message_kb, dossier_kb
from services.security import detect_threat, contains_bad_words, handle_bad_word

logger = logging.getLogger(__name__)
router = Router()

# ══════════════════════════════════════════════════════════════════════════════
# REPLY KEYBOARD — главное меню пользователя
# ══════════════════════════════════════════════════════════════════════════════

async def main_menu_kb(user_id: int | None = None) -> ReplyKeyboardMarkup:
    """
    Клавиатура главного меню.
    Динамически меняет кнопки в зависимости от статуса пользователя:
      - Анонимность: ВКЛ/ВЫКЛ  — по is_anon()
      - Джарвис: "🤖 Позвать" / "🛑 Остановить" — по активной сессии
    """
    anon_active   = await db.is_anon(user_id) if user_id else False
    anon_label    = "🔓 ВЫКЛЮЧИТЬ Анонимность" if anon_active else "🕶️ ВКЛЮЧИТЬ Анонимность"

    # Проверяем активна ли сессия Джарвиса прямо сейчас
    jarvis_active = False
    if user_id:
        session = await db.get_jarvis_session(user_id)
        jarvis_active = bool(session and session.get("state") == "jarvis_active")

    jarvis_label = "🛑 Остановить Джарвиса" if jarvis_active else "🤖 Позвать Джарвиса"

    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="🆘 Написать админу"),
                KeyboardButton(text="📜 Правила"),
            ],
            [
                KeyboardButton(text=jarvis_label),
                KeyboardButton(text="👤 Мой ID"),
            ],
            [
                KeyboardButton(text=anon_label),
            ],
            [
                KeyboardButton(text="👨‍💻 Стать администратором"),
            ],
        ],
        resize_keyboard=True,
        input_field_placeholder="Выберите действие или напишите сообщение...",
    )


# ══════════════════════════════════════════════════════════════════════════════
# FAQ — загружается при старте
# ══════════════════════════════════════════════════════════════════════════════

_FAQ: dict[str, str] = {}


def load_faq(path: str = "faq.json") -> None:
    global _FAQ
    try:
        faq_path = Path(path)
        if faq_path.exists():
            with faq_path.open(encoding="utf-8") as f:
                raw = json.load(f)
            _FAQ = {k.lower(): v for k, v in raw.items()}
            logger.info(f"[FAQ] Загружено {len(_FAQ)} записей")
        else:
            logger.warning(f"[FAQ] Файл {path} не найден — FAQ отключён")
    except Exception as e:
        logger.error(f"[FAQ] Ошибка загрузки: {e}")


load_faq()


# ── Русские месяцы для сообщения о блокировке Джарвиса ───────────────────────
_MONTHS_RU = {
    1: "января", 2: "февраля", 3: "марта", 4: "апреля",
    5: "мая", 6: "июня", 7: "июля", 8: "августа",
    9: "сентября", 10: "октября", 11: "ноября", 12: "декабря",
}


def _format_unlock_time(dt_utc) -> str:
    """UTC datetime → красивое локальное время: '13 марта в 14:30'"""
    local_dt = dt_utc.astimezone()
    return f"{local_dt.day} {_MONTHS_RU[local_dt.month]} в {local_dt.strftime('%H:%M')}"


def _fmt_seconds(sec: int) -> str:
    """Секунды → строка 'X мин Y сек', например '3 мин 45 сек'."""
    m, s = divmod(max(0, sec), 60)
    if m == 0:
        return f"{s} сек"
    return f"{m} мин {s} сек" if s else f"{m} мин"


def _faq_answer(text: str) -> str | None:
    """
    Ищет ключевое слово в тексте.
    Для "джарвис": короткое обращение (≤15 символов) → FAQ,
    длинный вопрос → None (пусть отвечает ИИ).
    """
    lower = text.lower().strip()
    jarvis_keywords = {"джарвис", "jarvis"}

    for keyword, answer in _FAQ.items():
        if keyword not in lower:
            continue
        if keyword in jarvis_keywords:
            is_simple = len(text.strip()) <= 15 and "?" not in text
            if not is_simple:
                return None
        return answer
    return None


# ══════════════════════════════════════════════════════════════════════════════
# АНТИ-СПАМ
# ══════════════════════════════════════════════════════════════════════════════

_spam_tracker: dict[int, list[float]] = defaultdict(list)
SPAM_LIMIT    = 5
SPAM_WINDOW   = 10
SPAM_MUTE_MIN = 10


def _is_spamming(user_id: int) -> bool:
    now = time.monotonic()
    _spam_tracker[user_id] = [t for t in _spam_tracker[user_id] if now - t < SPAM_WINDOW]
    _spam_tracker[user_id].append(now)
    return len(_spam_tracker[user_id]) > SPAM_LIMIT


# ══════════════════════════════════════════════════════════════════════════════
# /start
# ══════════════════════════════════════════════════════════════════════════════

@router.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(
        "🛡 <b>Цитадель «Рыцарский Подвиг»</b>\n\n"
        "Приветствуем тебя, благородный странник! Ты вошел под своды нашей крепости. "
        "Если твой дух ослаб, а доспехи покрылись пылью дорожных невзгод — "
        "здесь ты найдешь отдых и верных братьев по оружию.\n\n"
        "📜 <b>КОДЕКС ЧЕСТИ ОРДЕНА:</b>\n"
        "— <b>Благочестие:</b> Скверна и 18+ помыслы караются изгнанием.\n"
        "— <b>Дисциплина:</b> Не нарушай покой залов спамом и лишним шумом.\n"
        "— <b>Обет молчания:</b> Личные тайны магистров не подлежат разглашению.\n\n"
        "⚔️ <b>ВЕРНЫЙ ОРУЖЕНОСЕЦ — ДЖАРВИС:</b>\n"
        "Если рыцари-администраторы сейчас заняты в сражении и не ответят тебе "
        "в течение 5 минут — не тревожься. Тебе поможет наш верный оруженосец Джарвис. "
        "Он наделен мудростью древних свитков и готов поддержать тебя в любую секунду.\n\n"
        "💡 <b>КАК ПРИЗВАТЬ ПОМОЩНИКА:</b>\n"
        "Чтобы заговорить напрямую с оруженосцем, начни своё послание с его имени.\n"
        "Пример: <i>«Джарвис, помоги мне справиться с этой бурей в душе...»</i>\n\n"
        "Соберись с духом, путник. Твой главный подвиг еще впереди! ✨",
        parse_mode="HTML",
        reply_markup=await main_menu_kb(message.from_user.id),
    )


# ══════════════════════════════════════════════════════════════════════════════
# /anon — включение/выключение анонимного режима
# ══════════════════════════════════════════════════════════════════════════════

async def _toggle_anon(message: Message) -> None:
    """Общая логика переключения анонимности — для /anon и кнопки."""
    user_id = message.from_user.id
    currently = await db.is_anon(user_id)
    new_state = not currently
    await db.set_anon(user_id, new_state)

    if new_state:
        text = "🕶️ <b>Анонимный режим ВКЛЮЧЕН.</b>\nТеперь админы не видят ваш профиль."
    else:
        text = "🔓 <b>Анонимный режим ВЫКЛЮЧЕН.</b>\nТеперь ваш профиль виден админам."

    await message.answer(
        text,
        parse_mode="HTML",
        reply_markup=await main_menu_kb(user_id),
    )


@router.message(Command("anon"))
async def cmd_anon(message: Message):
    if message.chat.type != "private":
        return
    await _toggle_anon(message)


@router.message(F.text.in_({"🕶️ ВКЛЮЧИТЬ Анонимность", "🔓 ВЫКЛЮЧИТЬ Анонимность"}))
async def btn_anon(message: Message):
    """Кнопка анонимности — делает то же самое что /anon."""
    if message.chat.type != "private":
        return
    await _toggle_anon(message)


@router.message(F.text == "🛑 Остановить Джарвиса")
async def btn_stop_jarvis(message: Message, bot: Bot):
    """Досрочно завершает активную сессию Джарвиса по запросу пользователя."""
    if message.chat.type != "private":
        return

    user = message.from_user
    result = await ai_timer.stop_jarvis_early(user.id, bot)

    if result is None:
        # Сессии нет — кнопка осталась из прошлого состояния, просто обновляем меню
        await message.answer(
            "🤖 Активной сессии Джарвиса нет.",
            reply_markup=await main_menu_kb(user.id),
        )
        return

    used_str      = _fmt_seconds(result["used_sec"])
    remaining_str = _fmt_seconds(result["remaining_sec"])

    await message.answer(
        f"🛑 Вы остановили Джарвиса.\n\n"
        f"⏱ Использовано времени: <b>{used_str}</b>\n"
        f"⏳ Остаток <b>{remaining_str}</b> сохранён в системе.\n\n"
        f"<i>Следующий сеанс будет доступен через 24 часа.</i>",
        parse_mode="HTML",
        reply_markup=await main_menu_kb(user.id),
    )
    logger.info(f"[JARVIS] Досрочная остановка user_id={user.id}: "
                f"использовано {result['used_sec']}с, остаток {result['remaining_sec']}с")


# ══════════════════════════════════════════════════════════════════════════════
# ОСНОВНОЙ ОБРАБОТЧИК — ТОЛЬКО ЛИЧНЫЕ ЧАТЫ
# ══════════════════════════════════════════════════════════════════════════════

# ══════════════════════════════════════════════════════════════════════════════
# АНКЕТА В АДМИНИСТРАЦИЮ (FSM)
# ══════════════════════════════════════════════════════════════════════════════

_ANKET_TEXT = (
    "📋 <b>АНКЕТА ДЛЯ КАНДИДАТОВ В АДМИНИСТРАЦИЮ</b>\n\n"
    "Пожалуйста, внимательно ознакомьтесь с вопросами ниже, скопируйте их "
    "и отправьте ответы одним сообщением.\n\n"
    "<b>Основная информация:</b>\n\n"
    "1. Имя и ваш Telegram username\n"
    "2. Возраст\n"
    "3. Часовой пояс\n"
    "4. Сколько времени вы готовы уделять работе (в день / неделю)\n"
    "5. Есть ли у вас опыт в поддержке пользователей (онлайн или оффлайн)? Опишите\n"
    "6. Почему вы хотите стать администратором?\n"
    "7. Какие качества, по вашему мнению, наиболее важны для сотрудника поддержки?\n"
    "8. Как вы реагируете на агрессию или оскорбления со стороны пользователей?\n"
    "9. Чем бы вы хотели заниматься: поддержка, общение или совмещать всё?\n\n"
    "<b>Ситуационные вопросы (ответьте развёрнуто):</b>\n\n"
    "1. Пользователь переживает тяжёлую утрату (смерть близкого человека) и находится "
    "в отчаянии. Ваши действия?\n"
    "2. Пользователь сталкивается с травлей в школе, при этом не получает поддержки "
    "от родителей и не может сменить учебное заведение. Какие рекомендации вы дадите?\n\n"
    "Просим отвечать максимально подробно и обдуманно.\n\n"
    "<i>Важно: у вас должен быть установлен Telegram username для обратной связи.</i>"
)


@router.message(F.text == "👨‍💻 Стать администратором")
async def btn_admin_apply(message: Message, state: FSMContext):
    """Кнопка подачи заявки — проверяем статус и username, показываем анкету."""
    if message.chat.type != "private":
        return

    user_id = message.from_user.id

    # Проверяем статус предыдущей заявки
    app = await db.get_application_status(user_id)
    if app:
        if app["status"] == "accepted":
            await message.answer(
                "✅ Вас уже приняли в администрацию!\n\n"
                "Ожидайте сообщения от администрации.",
            )
            return

        if app["status"] == "rejected":
            from datetime import timedelta
            cooldown_until = app["updated_at"] + timedelta(days=2)
            now = datetime.now(timezone.utc)
            if now < cooldown_until:
                # Считаем сколько осталось
                delta = cooldown_until - now
                hours_left = int(delta.total_seconds() // 3600)
                days_left  = hours_left // 24
                hrs_left   = hours_left % 24

                if days_left > 0:
                    time_str = f"{days_left} дн. {hrs_left} ч."
                else:
                    time_str = f"{hrs_left} ч."

                await message.answer(
                    "⏳ Вы уже подавали заявку, но вас не приняли.\n\n"
                    f"Повторная подача будет доступна через <b>{time_str}</b>.\n"
                    "Попробуйте обратиться позже!",
                    parse_mode="HTML",
                )
                return
            # Кулдаун истёк — сбрасываем статус, даём подать заново

    # Проверяем наличие username — без него админы не смогут связаться
    if not message.from_user.username:
        await message.answer(
            "⚠️ <b>У вас не установлен Telegram username.</b>\n\n"
            "Администраторы не смогут с вами связаться без него.\n\n"
            "Пожалуйста:\n"
            "1. Откройте <b>Настройки → Изменить профиль</b>\n"
            "2. Установите username\n"
            "3. Вернитесь и нажмите кнопку снова. 👨‍💻",
            parse_mode="HTML",
        )
        return

    await state.set_state(AdminApplication.waiting_for_answers)
    await message.answer(
        _ANKET_TEXT,
        parse_mode="HTML",
        reply_markup=ReplyKeyboardRemove(),
    )


@router.message(AdminApplication.waiting_for_answers)
async def receive_admin_application(message: Message, state: FSMContext, bot: Bot):
    """Получаем заполненную анкету и пересылаем в группу Безопасности."""
    user = message.from_user
    answers = message.text or message.caption or "[без текста]"

    # Сбрасываем состояние
    await state.clear()

    # Пересылаем в ADMIN_GROUP (группа Безопасности) с кнопками принять/отклонить
    report = (
        f"📩 <b>НОВАЯ ЗАЯВКА В АДМИНИСТРАЦИЮ</b>\n\n"
        f"👤 <b>Отправитель:</b> @{user.username or '—'} "
        f"(ID: <code>{user.id}</code>)\n\n"
        f"📝 <b>ТЕКСТ АНКЕТЫ:</b>\n"
        f"<blockquote>{answers[:3800]}</blockquote>"
    )
    apply_kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Принять", callback_data=f"apply_accept:{user.id}"),
        InlineKeyboardButton(text="❌ Отклонить", callback_data=f"apply_reject:{user.id}"),
    ]])
    try:
        await bot.send_message(ADMIN_GROUP_ID, report, parse_mode="HTML", reply_markup=apply_kb)
        logger.info(f"[APPLY] Заявка от user_id={user.id} отправлена в ADMIN_GROUP")
    except Exception as e:
        logger.error(f"[APPLY] Ошибка отправки заявки: {e}")

    # Подтверждение пользователю
    await message.answer(
        "✅ Ваша заявка принята! Администрация свяжется с вами, если вы нам подходите.",
        reply_markup=await main_menu_kb(user.id),
    )


@router.message()
async def handle_user_message(message: Message, bot: Bot):
    if message.chat.type != "private":
        return

    user     = message.from_user
    raw_text = message.text or message.caption or "[медиа без текста]"

    logger.info(f"[ЛС] user_id={user.id} @{user.username}: {raw_text[:70]!r}")

    # ── 1. Технический режим ──────────────────────────────────────────────────
    if not await db.is_bot_active():
        await message.answer(
            "🛠 Извините, сейчас проводятся технические работы. "
            "Пожалуйста, попробуйте позже!"
        )
        return

    # ── 2. Бан ────────────────────────────────────────────────────────────────
    if await db.is_banned(user.id):
        await message.answer("⛔ Вы заблокированы. Обратитесь к администрации.")
        return

    # ── 3. Мут ────────────────────────────────────────────────────────────────
    if await db.is_muted(user.id):
        await message.answer("🔇 Вы временно ограничены в отправке сообщений.")
        return

    # ── 4. Анти-спам ──────────────────────────────────────────────────────────
    if _is_spamming(user.id):
        until = datetime.now(timezone.utc) + timedelta(minutes=SPAM_MUTE_MIN)
        await db.set_mute(user.id, until.isoformat())
        _spam_tracker.pop(user.id, None)
        await message.answer("🚫 Слишком много сообщений. Отдохните 10 минут.")
        await bot.send_message(
            ADMIN_GROUP_ID,
            f"⚡ <b>АНТИ-СПАМ</b>\n"
            f"<a href='tg://user?id={user.id}'>{user.full_name}</a> "
            f"(<code>{user.id}</code>) замучен на 10 мин за флуд.",
            parse_mode="HTML",
        )
        return

    # ── 5. Регистрация нового пользователя ────────────────────────────────────
    is_new = await db.upsert_user(
        user_id=user.id,
        username=user.username,
        first_name=user.first_name,
        last_name=user.last_name,
    )
    if is_new:
        await _send_dossier(bot, user)

    # ── 6. Нецензурная лексика → варны (1/3 → мут) ───────────────────────────
    if contains_bad_words(raw_text):
        await handle_bad_word(bot, message, ADMIN_GROUP_ID)
        return

    # ── 7. Угрозы / доксинг → авто-бан ───────────────────────────────────────
    threat = detect_threat(raw_text)
    if threat.detected:
        await _handle_threat(bot, message, user, raw_text, threat)
        return

    # ── 8. ★ АКТИВНАЯ СЕССИЯ ДЖАРВИСА ────────────────────────────────────────
    # Джарвис перехватывает ВСЕ сообщения подряд — имя писать не нужно
    session = await db.get_jarvis_session(user.id)
    jarvis_active = session and session["state"] == "jarvis_active"

    if jarvis_active:
        print(f"[JARVIS] ⚡ БЛОК 8 — активная сессия user_id={user.id}, рапорт не нужен")
        logger.info(f"[JARVIS] Активная сессия user_id={user.id} → ИИ")
        handled = await ai_timer.on_jarvis_reply(user.id, raw_text, bot)
        if handled:
            return

    # ── 9. Прямое обращение "Джарвис, ..." (сессия НЕ активна) ──────────────
    if ai_timer.is_jarvis_mention(raw_text) and not jarvis_active:
        logger.info(f"[JARVIS] Прямое обращение user_id={user.id}: {raw_text[:60]!r}")

        # Проверяем не выключен ли ИИ администраторами
        if not await db.is_ai_active():
            await message.answer(
                "🔴 <b>Джарвис временно недоступен.</b>\n"
                "ИИ-ассистент отключён администраторами. "
                "Пожалуйста, напишите напрямую — живой оператор вам поможет.",
                parse_mode="HTML",
            )
            return

        if not session or session["state"] == "closed":
            await db.create_jarvis_session(user.id)
        # Владелец никогда не блокируется
        _is_owner = (user.username or "").lower() == "it_is_your_brother"
        if _is_owner or not await db.is_jarvis_blocked(user.id):
            await db.set_jarvis_state(user.id, "jarvis_active")
            ai_timer._start_jarvis_limit_timer(user.id, bot)

            print(f"[JARVIS] ✅ БЛОК 9 — отправляю рапорт в STAFF для user_id={user.id}")
            # Рапорт в STAFF_GROUP при ЛЮБОМ ручном вызове Джарвиса
            # (кнопка, прямое обращение — всё что пришло от пользователя, не по таймеру)
            username_str = f"@{user.username}" if user.username else "без username"
            try:
                await bot.send_message(
                    STAFF_GROUP_ID,
                    f"🤖 <b>ВНИМАНИЕ:</b> Пользователь "
                    f"<code>{user.id}</code> ({username_str}) вызвал Джарвиса вручную.\n"
                    f"Я беру управление на 10 минут.\n"
                    f"Прошу не вмешиваться, сер!",
                    parse_mode="HTML",
                )
                logger.info(f"[JARVIS] Рапорт о ручном вызове отправлен в STAFF_GROUP")
            except Exception as e:
                logger.error(f"[JARVIS] Ошибка рапорта в STAFF: {e}")

            handled = await ai_timer.on_jarvis_reply(user.id, raw_text, bot)
            if handled:
                return
        else:
            # Достаём точное время разблокировки из БД
            unlock_dt = await db.get_jarvis_blocked_until(user.id)
            if unlock_dt:
                unlock_str = _format_unlock_time(unlock_dt)
                blocked_text = (
                    f"🤖 Простите, сер, системы ещё не восстановились.\n"
                    f"Следующий сеанс возможен только в <b>{unlock_str}</b>."
                )
            else:
                blocked_text = "🤖 Джарвис временно недоступен. Попробуйте позже."
            await message.answer(blocked_text, parse_mode="HTML")
            return

    # ── 10. Кнопки меню ───────────────────────────────────────────────────────

    # 📜 Правила
    if raw_text == "📜 Правила":
        await message.answer(
            "📜 <b>Правила проекта «Ангелы Слез»:</b>\n\n"
            "— Запрещён 18+ контент.\n"
            "— Запрещён спам и флуд.\n"
            "— Запрещено выпрашивать личные данные админов.\n"
            "— Уважайте других участников.\n\n"
            "При нарушении — мут или бан без предупреждения.",
            parse_mode="HTML",
            reply_markup=await main_menu_kb(message.from_user.id),
        )
        return

    # 👤 Мой ID
    if raw_text == "👤 Мой ID":
        await message.answer(
            f"👤 Ваш Telegram ID: <code>{user.id}</code>\n"
            f"Имя: {user.full_name}\n"
            f"Username: @{user.username or '—'}",
            parse_mode="HTML",
            reply_markup=await main_menu_kb(user.id),
        )
        return

    # 🆘 Написать админу — просто продолжаем стандартный путь (пересылка)
    if raw_text == "🆘 Написать админу":
        await message.answer(
            "✍️ Напишите ваше сообщение, и мы передадим его администратору.",
            reply_markup=await main_menu_kb(user.id),
        )
        return

    # ── 11. FAQ по ключевым словам ────────────────────────────────────────────
    # Ключи в этом списке показываются только ОДИН РАЗ на пользователя
    _ONCE_KEYS = {"привет", "спасибо"}

    faq_reply = _faq_answer(raw_text)
    if faq_reply:
        # Определяем какой ключ сработал
        matched_key = None
        for kw in _FAQ:
            if kw in raw_text.lower():
                matched_key = kw
                break

        # Если ключ одноразовый — проверяем показывали ли уже
        if matched_key in _ONCE_KEYS:
            already_shown = await db.is_faq_shown(user.id, matched_key)
            if already_shown:
                # Уже видел — пропускаем FAQ, идём дальше (пересылка в группу)
                pass
            else:
                await db.mark_faq_shown(user.id, matched_key)
                await message.answer(faq_reply, parse_mode="HTML", reply_markup=await main_menu_kb(user.id))
                return
        else:
            await message.answer(faq_reply, parse_mode="HTML", reply_markup=await main_menu_kb(user.id))
            return

    # ── 12. Стандартный путь: тикет + таймер + пересылка ─────────────────────
    # Создаём тикет только если нет открытого
    ticket_id = await db.get_open_ticket(user.id)
    if ticket_id is None:
        ticket_id = await db.create_ticket(user.id)
        await message.answer(
            f"✅ Ваша заявка <b>№{ticket_id}</b> принята.\n"
            f"Ожидайте ответа оператора или Джарвиса (через 5 минут).",
            parse_mode="HTML",
            reply_markup=await main_menu_kb(user.id),
        )
    else:
        await message.answer(
            f"✅ Сообщение добавлено к заявке <b>№{ticket_id}</b>.",
            parse_mode="HTML",
        )

    await ai_timer.on_user_message(user.id, bot)
    await _forward_to_group(bot, STAFF_GROUP_ID, user, message, ticket_id)


# ══════════════════════════════════════════════════════════════════════════════
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ══════════════════════════════════════════════════════════════════════════════

async def _send_dossier(bot: Bot, user) -> None:
    photos = await bot.get_user_profile_photos(user.id, limit=1)
    bio_text = "—"
    try:
        chat_info = await bot.get_chat(user.id)
        if chat_info.bio:
            bio_text = chat_info.bio
        await db.upsert_user(
            user_id=user.id, username=user.username,
            first_name=user.first_name, bio=chat_info.bio,
        )
    except Exception:
        pass

    dossier_text = (
        "🆕 <b>НОВЫЙ ПОЛЬЗОВАТЕЛЬ — ДОСЬЕ</b>\n\n"
        f"👤 <b>Имя:</b> {user.full_name}\n"
        f"🔗 <b>Username:</b> @{user.username or '—'}\n"
        f"🆔 <b>ID:</b> <code>{user.id}</code>\n"
        f"📝 <b>Bio:</b> {bio_text}\n"
        f"🕐 <b>Первое обращение:</b> {datetime.now(timezone.utc).strftime('%d.%m.%Y %H:%M')} UTC"
    )

    if photos.total_count > 0:
        photo = photos.photos[0][-1]
        await bot.send_photo(
            ADMIN_GROUP_ID, photo=photo.file_id,
            caption=dossier_text, parse_mode="HTML",
            reply_markup=dossier_kb(user.id),
        )
    else:
        await bot.send_message(
            ADMIN_GROUP_ID, dossier_text,
            parse_mode="HTML", reply_markup=dossier_kb(user.id),
        )


async def _forward_to_group(
    bot: Bot,
    group_id: int,
    user,
    message: Message,
    ticket_id: int | None = None,
) -> None:
    """
    Пересылает сообщение в STAFF_GROUP (ПЗ-группу).

    Схема двойной отправки при анонимном режиме (/anon):
      • STAFF_GROUP_ID   → анонимно: только ID + "⚠️ Анонимное обращение"
      • ARCHIVE_GROUP_ID → всегда полные данные (через ArchiveMiddleware,
                           срабатывает автоматически до этого хендлера)
    """
    anon = await db.is_anon(user.id)
    ticket_tag = f" | 🎫 №{ticket_id}" if ticket_id else ""

    if anon:
        header = (
            f"⚠️ <b>Анонимное обращение</b>{ticket_tag}\n"
            f"🆔 ID: <code>{user.id}</code>"
        )
    else:
        header = (
            f"✉️ <a href='tg://user?id={user.id}'>{user.full_name}</a> "
            f"(@{user.username or '—'}) | {user.id}{ticket_tag}"
        )

    try:
        await bot.send_message(group_id, header, parse_mode="HTML")

        if anon:
            # copy_message НЕ показывает плашку "Переслано от..." — анонимность сохранена
            sent = await bot.copy_message(
                chat_id=group_id,
                from_chat_id=message.chat.id,
                message_id=message.message_id,
            )
        else:
            # forward сохраняет плашку с именем — для открытых обращений
            sent = await message.forward(group_id)

        # Сохраняем связку message_id → user_id для /report и reply на forward
        if sent:
            await db.save_forwarded_message(group_id, sent.message_id, user.id)
            # Также сохраняем для reply с цитатой во время /link:
            # group_message_id пересланного сообщения → оригинальный message_id пользователя
            await db.save_bot_message_id(
                group_id, sent.message_id, user.id, message.message_id
            )

        logger.info(f"[FORWARD] → группа {group_id} | anon={anon} | метод={'copy' if anon else 'forward'}")
    except Exception as e:
        logger.error(f"[FORWARD] Ошибка: {e}")


async def _handle_threat(bot: Bot, message: Message, user, text: str, threat) -> None:
    await db.log_threat(user.id, threat.threat_type, text)
    await db.ban_user(user.id, f"Авто-бан: {threat.description}")
    await message.answer(
        "⛔ Ваше сообщение нарушает правила безопасности. Вы заблокированы.",
        reply_markup=ReplyKeyboardRemove(),
    )
    report = (
        "🚨 <b>УГРОЗА — АВТО-БАН</b>\n\n"
        f"🔍 <b>Тип:</b> {threat.description}\n"
        f"👤 <a href='tg://user?id={user.id}'>{user.full_name}</a> "
        f"| <code>{user.id}</code>\n"
        f"💬 <code>{text[:300]}</code>"
    )
    for gid in (ADMIN_GROUP_ID, STAFF_GROUP_ID):
        try:
            await bot.send_message(
                gid, report, parse_mode="HTML",
                reply_markup=admin_message_kb(user.id),
            )
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════════════════════
# ПУБЛИЧНАЯ ФУНКЦИЯ — вызывается из ai_timer при завершении сессии Джарвиса
# ══════════════════════════════════════════════════════════════════════════════

async def send_main_menu(bot: Bot, user_id: int) -> None:
    """Отправляет главное меню после завершения диалога с Джарвисом."""
    try:
        await bot.send_message(
            user_id,
            "Если остались вопросы — я здесь. ✨",
            reply_markup=await main_menu_kb(user_id),
        )
    except Exception:
        pass