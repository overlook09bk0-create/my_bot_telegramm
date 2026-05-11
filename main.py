"""
main.py — Точка входа (обычная версия, без пранков).
"""

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

from config import BOT_TOKEN, STAFF_GROUP_ID, ADMIN_GROUP_ID
from database import init_db
from middlewares.archive import ArchiveMiddleware
from handlers import user_handler, admin_handler, reply_handler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)


async def main() -> None:
    await init_db()
    logger.info("База данных инициализирована.")

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=MemoryStorage())
    dp.message.middleware(ArchiveMiddleware())

    dp.include_router(admin_handler.router)
    dp.include_router(reply_handler.router)
    dp.include_router(user_handler.router)

    for gid, name in [(STAFF_GROUP_ID, "STAFF"), (ADMIN_GROUP_ID, "ADMIN")]:
        try:
            await bot.send_message(gid, "🤖 Бот запущен и готов к работе.")
            logger.info(f"Тестовое сообщение в {name} отправлено.")
        except Exception as e:
            logger.error(f"Не удалось отправить в {name}: {e}")

    logger.info("Бот запущен. Ожидание обновлений...")
    try:
        await dp.start_polling(bot, allowed_updates=["message", "callback_query"])
    finally:
        await bot.session.close()
        logger.info("Бот остановлен.")


if __name__ == "__main__":
    asyncio.run(main())
