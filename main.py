import logging
import os
from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.types import BotCommand
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

import db
from config import (
    BOT_TOKEN,
    WEBHOOK_URL,
    WEBHOOK_HOST,
    WEBHOOK_PATH,
    WEBHOOK_SECRET
)
from handlers.admin_handlers import admin_router
from handlers.user_handlers import user_router

# Log sozlamalari
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - [%(levelname)s] - %(name)s - %(message)s"
)
logger = logging.getLogger("SmartAIChatBot")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# Routerlarni ro'yxatdan o'tkazish
dp.include_router(admin_router)
dp.include_router(user_router)


async def set_bot_commands(bot_instance: Bot):
    commands = [
        BotCommand(command="start", description="Botni ishga tushirish"),
        BotCommand(command="admin", description="Admin boshqaruv paneli"),
        BotCommand(command="forget", description="Ma'lumotlarimni o'chirish"),
        BotCommand(command="help", description="Yordam va qo'llanma")
    ]
    await bot_instance.set_my_commands(commands)


async def on_startup(bot_instance: Bot) -> None:
    logger.info("Bot serverda (webhook rejimida) ishga tushmoqda...")
    db.init_db()
    await bot_instance.set_webhook(
        url=WEBHOOK_URL,
        drop_pending_updates=True,
        allowed_updates=dp.resolve_used_update_types(),
        secret_token=WEBHOOK_SECRET
    )
    await set_bot_commands(bot_instance)
    bot_user = await bot_instance.get_me()
    logger.info(f"✅ Bot va Webhook tayyor: @{bot_user.username} -> {WEBHOOK_HOST}")


async def on_shutdown(bot_instance: Bot) -> None:
    logger.info("Bot to'xtatilmoqda...")
    await bot_instance.session.close()


async def handle_health_check(request: web.Request) -> web.Response:
    return web.json_response({
        "status": "ok",
        "service": "Smart AI Chat & Voice-to-Text Bot",
        "mode": "webhook"
    })


def main():
    port = int(os.getenv("PORT", "8080"))
    app = web.Application()
    app.router.add_get("/", handle_health_check)
    app.router.add_get("/health", handle_health_check)

    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)

    webhook_requests_handler = SimpleRequestHandler(
        dispatcher=dp,
        bot=bot,
        secret_token=WEBHOOK_SECRET
    )
    webhook_requests_handler.register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)

    logger.info(f"Server 0.0.0.0:{port} portida ishga tushmoqda...")
    web.run_app(app, host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
