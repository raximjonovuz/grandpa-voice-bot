import asyncio
import logging
import os
import uuid
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, FSInputFile
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web
from dotenv import load_dotenv

# Log sozlamalari
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - [%(levelname)s] - %(name)s - %(message)s"
)
logger = logging.getLogger("ReverseVoiceBot")

load_dotenv()
raw_token = os.getenv("BOT_TOKEN") or os.getenv("bottoken")
if not raw_token:
    raise ValueError("BOT_TOKEN topilmadi! Iltimos .env faylida BOT_TOKEN ni ko'rsating.")

BOT_TOKEN = raw_token.split("//")[0].split("#")[0].strip().strip('"').strip("'")

# Webhook sozlamalari
WEBHOOK_HOST = os.getenv("WEBHOOK_HOST", "https://grandpa-voice-bot.onrender.com").rstrip("/")
WEBHOOK_PATH = f"/webhook/{BOT_TOKEN}"
WEBHOOK_URL = f"{WEBHOOK_HOST}{WEBHOOK_PATH}"

BASE_DIR = Path(__file__).parent
TEMP_DIR = BASE_DIR / "temp"
TEMP_DIR.mkdir(parents=True, exist_ok=True)

MAX_VOICE_DURATION = 120  # soniya (2 daqiqa)
MAX_FILE_SIZE = 20 * 1024 * 1024  # 20 MB

CONCURRENCY_LIMIT = 5
processing_semaphore = asyncio.Semaphore(CONCURRENCY_LIMIT)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


async def process_reverse_voice(input_path: Path, output_path: Path) -> None:
    """
    Ovozni teskarisiga (areverse) o'girish.
    Boshqa hech qanday parametr o'zgartirilmaydi.
    """
    async with processing_semaphore:
        cmd = [
            "ffmpeg",
            "-y",
            "-i", str(input_path),
            "-af", "areverse",
            "-c:a", "libopus",
            "-b:a", "64k",
            str(output_path)
        ]

        logger.info(f"FFmpeg areverse: {input_path.name} -> {output_path.name}")
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        _, stderr = await process.communicate()

        if process.returncode != 0:
            err_msg = stderr.decode(errors="replace")
            logger.error(f"FFmpeg xatosi (kod {process.returncode}):\n{err_msg}")
            raise RuntimeError(f"FFmpeg xatolik bilan yakunlandi (kod {process.returncode})")


@dp.message(CommandStart())
async def handle_start(message: Message):
    bot_info = await bot.get_me()
    username = bot_info.username or "bot"
    text = (
        "Assalomu alaykum! 🔁🎤\n\n"
        "Menga ovozli xabar (voice) yuboring, men uni teskarisiga o'girib beraman.\n\n"
        f"@{username}"
    )
    await message.reply(text)


@dp.message(Command("help"))
async def handle_help(message: Message):
    bot_info = await bot.get_me()
    username = bot_info.username or "bot"
    await message.reply(f"Menga ovozli xabar yuboring, uni teskarisiga o'girib beraman.\n\n@{username}")


@dp.message(F.voice)
async def handle_voice(message: Message):
    voice = message.voice
    if not voice:
        return

    if voice.duration and voice.duration > MAX_VOICE_DURATION:
        await message.reply(f"⚠️ Ovoz juda uzun. Maksimal {MAX_VOICE_DURATION} soniya.")
        return

    if voice.file_size and voice.file_size > MAX_FILE_SIZE:
        await message.reply("⚠️ Ovoz hajmi juda katta.")
        return

    task_id = uuid.uuid4().hex
    input_file = TEMP_DIR / f"in_{task_id}.ogg"
    output_file = TEMP_DIR / f"out_{task_id}.ogg"

    try:
        file_info = await bot.get_file(voice.file_id)
        if not file_info.file_path:
            raise ValueError("Fayl yo'lini olib bo'lmadi.")

        await bot.download_file(file_info.file_path, destination=input_file)
        await process_reverse_voice(input_file, output_file)

        bot_info = await bot.get_me()
        bot_tag = f"@{bot_info.username}" if bot_info.username else ""

        processed_voice = FSInputFile(str(output_file))
        await message.reply_voice(
            voice=processed_voice,
            caption=bot_tag
        )

    except Exception as e:
        logger.exception(f"Ovozni qayta ishlashda xatolik: {e}")
        await message.reply("❌ Xatolik yuz berdi. Qaytadan urinib ko'ring.")

    finally:
        for f in (input_file, output_file):
            if f.exists():
                try:
                    f.unlink()
                except OSError:
                    pass


@dp.message()
async def handle_other_messages(message: Message):
    bot_info = await bot.get_me()
    bot_tag = f"@{bot_info.username}" if bot_info.username else ""
    await message.reply(f"Iltimos, ovozli xabar (voice) yuboring!\n\n{bot_tag}")


async def on_startup(bot: Bot) -> None:
    logger.info(f"Webhook sozlanmoqda: {WEBHOOK_URL}")
    await bot.set_webhook(
        url=WEBHOOK_URL,
        drop_pending_updates=True,
        allowed_updates=dp.resolve_used_update_types()
    )
    bot_user = await bot.get_me()
    logger.info(f"✅ Webhook o'rnatildi! Bot: @{bot_user.username}")


async def on_shutdown(bot: Bot) -> None:
    logger.info("Bot to'xtatilmoqda...")
    await bot.session.close()


async def handle_health_check(request: web.Request) -> web.Response:
    return web.json_response({
        "status": "ok",
        "service": "Reverse Voice Bot",
        "mode": "webhook"
    })


def main():
    # Render'da yoki lokalda port
    port = int(os.getenv("PORT", "8080"))

    # Webhook serverini yaratish
    app = web.Application()
    app.router.add_get("/", handle_health_check)
    app.router.add_get("/health", handle_health_check)

    # Startup & Shutdown hodisalari
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)

    # Aiogram webhook handler'ini aiohttp'ga ulash
    webhook_requests_handler = SimpleRequestHandler(
        dispatcher=dp,
        bot=bot
    )
    webhook_requests_handler.register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)

    logger.info(f"Server 0.0.0.0:{port} portida Webhook rejimida ishga tushmoqda...")
    web.run_app(app, host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
