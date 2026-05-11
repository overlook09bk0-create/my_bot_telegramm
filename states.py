"""
states.py — FSM-состояния бота.

Используется для многошаговых диалогов с пользователем.
Подключается через MemoryStorage в main.py.
"""

from aiogram.fsm.state import State, StatesGroup


class AdminApplication(StatesGroup):
    """Состояния для подачи заявки в администрацию."""
    waiting_for_answers = State()   # ожидаем текст анкеты от пользователя
