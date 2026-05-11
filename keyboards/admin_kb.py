"""
keyboards/admin_kb.py — Инлайн-клавиатуры для групп администраторов.
"""

from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton


def staff_message_kb(user_id: int) -> InlineKeyboardMarkup:
    """Кнопки под каждым сообщением в группе Персонала."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="🔇 Мут 1 час",
                callback_data=f"mute_1h:{user_id}"
            ),
            InlineKeyboardButton(
                text="🔇 Мут 7 дней",
                callback_data=f"mute_7d:{user_id}"
            ),
        ],
        [
            InlineKeyboardButton(
                text="🚫 Бан",
                callback_data=f"ban:{user_id}"
            ),
        ],
    ])


def admin_message_kb(user_id: int) -> InlineKeyboardMarkup:
    """Кнопки под сообщениями в группе Безопасности (расширенные)."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="🔇 Мут 1 час",
                callback_data=f"mute_1h:{user_id}"
            ),
            InlineKeyboardButton(
                text="🔇 Мут 7 дней",
                callback_data=f"mute_7d:{user_id}"
            ),
        ],
        [
            InlineKeyboardButton(
                text="🚫 Бан",
                callback_data=f"ban:{user_id}"
            ),
        ],
    ])


def dossier_kb(user_id: int) -> InlineKeyboardMarkup:
    """Кнопки под досье нового пользователя."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="🚫 Сразу забанить",
                callback_data=f"ban:{user_id}"
            ),
            InlineKeyboardButton(
                text="🔇 Мут 7 дней",
                callback_data=f"mute_7d:{user_id}"
            ),
        ],
    ])
