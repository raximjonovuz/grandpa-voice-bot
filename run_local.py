import asyncio
import logging
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from main import bot, dp, set_bot_commands, db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - [%(levelname)s] - %(name)s - %(message)s"
)
logger = logging.getLogger("LocalBotRunner")


async def run_local():
    logger.info("Bot lokal rejimda ishga tushmoqda...")
    db.init_db()
    
    # Webhookni o'chirib, to'g'ridan-to'g'ri polling boshlash
    await bot.delete_webhook(drop_pending_updates=True)
    await set_bot_commands(bot)
    
    bot_user = await bot.get_me()
    logger.info(f"✅ Barcha tizimlar tayyor! Bot: @{bot_user.username}")
    logger.info("Telegramda /start bosib test qilishingiz mumkin...")
    
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    try:
        asyncio.run(run_local())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot to'xtatildi.")
