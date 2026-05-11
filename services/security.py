"""
services/security.py — Авто-детект доксинга, угроз и нецензурной лексики.
"""

import re
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from aiogram import Bot
from aiogram.types import Message

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# СПИСОК ЗАПРЕЩЁННЫХ СЛОВ (маты / оскорбления)
# Добавляйте слова в нижнем регистре. Проверка ведётся по подстроке,
# чтобы «покрывать» словоформы (блять → блядь и т.д.).
# ══════════════════════════════════════════════════════════════════════════════

BAD_WORDS: list[str] = [
    # Базовые маты (корни)
    "хуй", "хуя", "хуе", "хуи", "хуём", "нахуй", "похуй", "пиздец",
    "пизд", "пизда", "ёбан", "ебан", "ёб твою", "еб твою",
    "блять", "блядь", "бля",
    "мудак", "мудила", "мудозвон",
    "ёбнут", "ёбнул", "ёбнуть",
    "сука", "суки", "сучка",
    "залупа", "залупин",
    "ёб", "еб",
    "гандон", "гондон",
    "пиздить", "пиздит", "пиздил",
    "шлюха", "шлюхи",
    "долбоёб", "долбоеб",
    "идиот", "идиотка",   # оскорбления
    "дебил", "дебилка",
    "кретин", "кретинка",
    "урод", "уродина",
    "придурок", "придурки",
    "тупица", "тупой", "тупая",
    "козёл", "козел", "козлина",
    "скотина", "скот",
    "ублюдок", "ублюдки",
    "выблядок",
    "тварь", "твари",
]

# Компилируем единый паттерн для быстрой проверки
_BAD_WORDS_RE = re.compile(
    "|".join(re.escape(w) for w in BAD_WORDS),
    re.IGNORECASE | re.UNICODE,
)

MAX_WARNS = 3          # Количество варнов до мута
MUTE_HOURS = 24        # Длительность мута в часах


# ══════════════════════════════════════════════════════════════════════════════
# ДЕТЕКТ УГРОЗ / ДОКСИНГА
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class ThreatResult:
    detected: bool
    threat_type: str = ""
    description: str = ""


# Банковские карты (13–19 цифр с пробелами/тире)
_CARD_RE = re.compile(r"\b(?:\d[ \-]?){13,19}\b")

# Российские/СНГ номера телефонов
_PHONE_RE = re.compile(
    r"(?:\+7|8)[\s\-]?\(?\d{3}\)?[\s\-]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}"
)

# Адреса (ул./пр./д. + цифры)
_ADDRESS_RE = re.compile(
    r"(?:ул\.?|улица|пр\.?|проспект|пер\.?|переулок|д\.?|дом|кв\.?|квартира)"
    r"\s+[\w\d\"«»]+",
    re.IGNORECASE | re.UNICODE,
)

# Серия/номер паспорта РФ: 4 цифры пробел 6 цифр
_PASSPORT_RE = re.compile(r"\b\d{4}\s\d{6}\b")

# Угрожающие фразы
_THREAT_PHRASES = [
    "я тебя пробью", "пробью по базе", "слив инфы", "слить инфу",
    "твой адрес", "знаю где ты живёшь", "знаю где живёшь",
    "деанон", "деанонимизация", "найду тебя", "опубликую данные",
    "слить данные", "база данных на тебя", "пробить по паспорту",
    "ИНН", "СНИЛС", "пробью ИНН",
]

# ФИО-паттерн (три слова с заглавной, кириллица)
_FIO_RE = re.compile(r"[А-ЯЁ][а-яё]+\s+[А-ЯЁ][а-яё]+\s+[А-ЯЁ][а-яё]+")


def detect_threat(text: str) -> ThreatResult:
    """Проверяет текст на признаки доксинга. Возвращает ThreatResult."""

    if _CARD_RE.search(text):
        return ThreatResult(True, "bank_card", "Номер банковской карты")

    if _PASSPORT_RE.search(text):
        return ThreatResult(True, "passport", "Серия/номер паспорта")

    if _PHONE_RE.search(text) and _FIO_RE.search(text):
        return ThreatResult(True, "dox_phone_fio", "Телефон + ФИО (возможный докс)")

    if _ADDRESS_RE.search(text) and len(text) > 30:
        return ThreatResult(True, "address", "Домашний адрес")

    lower = text.lower()
    for phrase in _THREAT_PHRASES:
        if phrase in lower:
            return ThreatResult(True, "threat_phrase", f"Угрожающая фраза: «{phrase}»")

    return ThreatResult(False)


def contains_bad_words(text: str) -> bool:
    """Возвращает True, если текст содержит слово из BAD_WORDS."""
    return bool(_BAD_WORDS_RE.search(text))


# ══════════════════════════════════════════════════════════════════════════════
# ОБРАБОТЧИК НАРУШЕНИЙ (ВАРНЫ → МУТ)
# ══════════════════════════════════════════════════════════════════════════════

async def handle_bad_word(
    bot: Bot,
    message: Message,
    admin_group_id: int,
) -> None:
    """
    Вызывается, когда в сообщении обнаружено запрещённое слово.

    Логика:
      • add_warn → получаем текущий счётчик n
      • n < MAX_WARNS  →  предупреждение пользователю "n/3"
      • n == MAX_WARNS →  мут на MUTE_HOURS часов + отчёт в admin_group_id
                          + reset_warns после мута
    """
    # Импорт здесь, чтобы избежать циклических зависимостей
    import database as db

    user = message.from_user
    warn_count = await db.add_warn(user.id)

    logger.info(
        f"[BAD_WORD] user_id={user.id} warn={warn_count}/{MAX_WARNS} | "
        f"text={message.text[:60]!r}"
    )

    if warn_count < MAX_WARNS:
        # ── Предупреждение ────────────────────────────────────────────────────
        await message.answer(
            f"⚠️ Вам выдано предупреждение <b>{warn_count}/{MAX_WARNS}</b> "
            f"за нарушение правил чата.\n"
            f"При достижении {MAX_WARNS} предупреждений вы будете замучены на "
            f"{MUTE_HOURS} часов.",
            parse_mode="HTML",
        )

    else:
        # ── Мут ───────────────────────────────────────────────────────────────
        mute_until = datetime.now(timezone.utc) + timedelta(hours=MUTE_HOURS)
        mute_until_iso = mute_until.isoformat()

        await db.set_mute(user.id, mute_until_iso)

        # Сообщаем пользователю
        await message.answer(
            f"🔇 Вы замучены на <b>{MUTE_HOURS} часов</b> за "
            f"<b>{MAX_WARNS}/{MAX_WARNS}</b> предупреждений.\n"
            f"Мут снимется: <b>{mute_until.strftime('%d.%m.%Y %H:%M')} UTC</b>",
            parse_mode="HTML",
        )

        # Отчёт администраторам
        report = (
            "🔇 <b>АВТО-МУТ — НАРУШЕНИЕ ПРАВИЛ</b>\n\n"
            f"👤 <b>Пользователь:</b> "
            f"<a href='tg://user?id={user.id}'>{user.full_name}</a>\n"
            f"🆔 <b>ID:</b> <code>{user.id}</code>\n"
            f"🔗 <b>Username:</b> @{user.username or '—'}\n"
            f"⚠️ <b>Причина:</b> {MAX_WARNS}/{MAX_WARNS} предупреждений "
            f"(нецензурная лексика)\n"
            f"⏰ <b>Мут до:</b> {mute_until.strftime('%d.%m.%Y %H:%M')} UTC\n"
            f"💬 <b>Последнее сообщение:</b>\n"
            f"<code>{(message.text or '')[:300]}</code>"
        )

        try:
            await bot.send_message(
                admin_group_id,
                report,
                parse_mode="HTML",
            )
        except Exception as e:
            logger.error(f"[BAD_WORD] Не удалось отправить отчёт в admin_group: {e}")

        # Обнуляем варны после мута
        await db.reset_warns(user.id)
        logger.info(f"[BAD_WORD] Варны пользователя {user.id} сброшены после мута.")