"""
middlewares/archive.py — Middleware: архивирует ВСЕ входящие сообщения.

Логика двойной отправки при анонимном режиме (/anon):
  • STAFF_GROUP_ID  — получает анонимную версию (без имени/username)
                      → обрабатывается в user_handler._forward_to_group()
  • ARCHIVE_GROUP_ID — получает ПОЛНЫЕ данные пользователя (Имя, Username, ID)
                      → обрабатывается ЗДЕСЬ, в middleware, всегда, без исключений

Middleware срабатывает ДО хендлеров, поэтому архив получает сообщение
независимо от того, что делает user_handler.
"""

import logging
from typing import Callable, Any, Awaitable

from aiogram import BaseMiddleware
from aiogram.types import Message, TelegramObject

from config import ARCHIVE_GROUP_ID

logger = logging.getLogger(__name__)


class ArchiveMiddleware(BaseMiddleware):
    """
    Перехватывает все сообщения из личных чатов и пересылает в ARCHIVE_GROUP_ID
    с ПОЛНЫМИ данными пользователя — имя, username, ID.

    Анонимный режим (/anon) НЕ влияет на архив:
    администрация всегда видит кто именно написал.
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if isinstance(event, Message) and event.chat.type == "private":
            bot = data.get("bot")
            if bot:
                try:
                    user = event.from_user

                    # Полный заголовок — всегда с именем и username,
                    # даже если у пользователя включён /anon
                    header = (
                        f"📥 <b>АРХИВ</b> | "
                        f"<a href='tg://user?id={user.id}'>{user.full_name}</a> "
                        f"(@{user.username or '—'}) | "
                        f"<code>{user.id}</code>"
                    )

                    await bot.send_message(
                        ARCHIVE_GROUP_ID, header, parse_mode="HTML"
                    )
                    await event.forward(ARCHIVE_GROUP_ID)

                    logger.debug(f"[ARCHIVE] Сохранено от user_id={user.id}")

                except Exception as e:
                    logger.error(f"[ARCHIVE] Ошибка пересылки: {e}")

        return await handler(event, data)
