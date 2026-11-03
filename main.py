import asyncio
import logging
import os
import uuid
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, FSInputFile, BotCommand
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web
from dotenv import load_dotenv

import db

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

# Admin username (kichik harflarda solishtirish uchun)
ADMIN_USERNAMES = ["o_raximjonov", "o_raximjonovuz"]

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

# Bazani ishga tushirish
db.init_db()


def is_admin(message: Message) -> bool:
    """Foydalanuvchi admin ekanligini tekshirish."""
    username = (message.from_user.username or "").lower().strip("@").strip()
    # Foydalanuvchi nomi yoki ID orqali tekshirish
    return any(admin in username for admin in ["o_raximjonov", "raximjonov"]) or username in ADMIN_USERNAMES


async def set_bot_commands(bot: Bot):
    """Telegram menyusidagi buyruqlar (/) ni o'rnatish."""
    commands = [
        BotCommand(command="start", description="Botni ishga tushirish"),
        BotCommand(command="help", description="Yordam va qo'llanma"),
        BotCommand(command="admin", description="Admin paneli (statistika)")
    ]
    await bot.set_my_commands(commands)


async def process_reverse_voice(input_path: Path, output_path: Path) -> None:
    """
    Ovozni teskarisiga (areverse) o'girish va shovqindan tozalash (afftdn filtri bilan).
    """
    async with processing_semaphore:
        # afftdn=nr=10:nf=-25 (shovqinni yumshoq tozalash) + areverse (teskarisiga o'girish)
        audio_filter = "afftdn=nr=10:nf=-25,areverse"
        cmd = [
            "ffmpeg",
            "-y",
            "-i", str(input_path),
            "-af", audio_filter,
            "-c:a", "libopus",
            "-b:a", "64k",
            str(output_path)
        ]

        logger.info(f"FFmpeg areverse+denoise: {input_path.name} -> {output_path.name}")
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
    user = message.from_user
    db.add_or_update_user(user.id, user.username, user.first_name, user.last_name)
    bot_info = await bot.get_me()
    username = bot_info.username or "bot"

    if is_admin(message):
        text = (
            f"Assalomu alaykum, hurmatli Admin ({user.first_name})! 👑\n\n"
            "Siz botning boshqaruvchisi sifatida tanildingiz.\n\n"
            "📊 Statistikani ko'rish uchun: /admin\n"
            "🎤 Ovoz yuborsangiz — tozalab, teskarisiga o'girib beraman.\n\n"
            f"@{username}"
        )
    else:
        text = (
            f"Assalomu alaykum, {user.first_name}! 🔁🎤\n\n"
            "Menga ixtiyoriy **ovozli xabar (voice)** yuboring, "
            "men uning shovqinini tozalab, **teskarisiga o'girib** qaytarib beraman!\n\n"
            f"@{username}"
        )
    await message.reply(text)


@dp.message(Command("help"))
async def handle_help(message: Message):
    try:
        user = message.from_user
        db.add_or_update_user(user.id, user.username, user.first_name, user.last_name)
        bot_info = await bot.get_me()
        username = bot_info.username or "bot"
        text = (
            "💡 Botdan foydalanish:\n\n"
            "1. Mikrofonni bosib ovozli xabar (voice) yuboring.\n"
            "2. Bot ovozdagi shovqinlarni tozalaydi va teskarisiga o'giradi.\n"
            "3. Tayyor audio sizga darhol qaytariladi.\n\n"
            f"@{username}"
        )
        await message.reply(text)
    except Exception as e:
        logger.exception(f"/help xatolik: {e}")
        await message.reply("Yordam: Menga ovozli xabar yuboring, uni teskarisiga o'girib beraman.")


@dp.message(Command("admin"))
async def handle_admin(message: Message):
    try:
        user = message.from_user
        db.add_or_update_user(user.id, user.username, user.first_name, user.last_name)

        logger.info(f"Admin tekshiruvi: id={user.id}, username={user.username}, is_admin={is_admin(message)}")

        if not is_admin(message):
            uname = f"@{user.username}" if user.username else "username yo'q"
            await message.reply(
                f"Kechirasiz, ushbu buyruq faqat bot admini uchun!\n"
                f"Sizning profilingiz: {uname} (ID: {user.id})"
            )
            return

        stats = db.get_stats()
        recent = db.get_recent_users(10)

        users_text = ""
        for idx, u in enumerate(recent, 1):
            uname = f"@{u['username']}" if u['username'] else "No username"
            users_text += f"{idx}. {u['first_name']} ({uname}) — {u['last_active']}\n"

        msg = (
            "👑 Admin boshqaruv paneli\n\n"
            f"👥 Jami foydalanuvchilar: {stats['total_users']} ta\n"
            f"⚡ Jami so'rovlar soni: {stats['total_requests']} ta\n"
            f"📅 Bugungi so'rovlar: {stats['today_requests']} ta\n"
            f"🔥 Bugungi faol odamlar: {stats['today_active_users']} ta\n\n"
            f"📋 Oxirgi kirgan foydalanuvchilar:\n{users_text}"
        )
        await message.reply(msg)
    except Exception as e:
        logger.exception(f"/admin xatolik: {e}")
        await message.reply(f"Admin xatoligi: {e}")


# Matn orqali admin savollariga javob berish (masalan: "nechta odam kirdi?", "kimlar kirdi?")
@dp.message(F.text)
async def handle_text(message: Message):
    user = message.from_user
    db.add_or_update_user(user.id, user.username, user.first_name, user.last_name)
    text_lower = message.text.lower()

    if is_admin(message):
        stats = db.get_stats()

        if "nechta odam" in text_lower or "odam soni" in text_lower or "foydalanuvchi" in text_lower:
            await message.reply(
                f"Hurmatli Admin! 📊\n"
                f"Botga jami **{stats['total_users']}** ta odam kirgan.\n"
                f"Bugun faol bo'lganlar: **{stats['today_active_users']}** ta.",
                parse_mode="Markdown"
            )
            return

        if "bugun" in text_lower and ("so'rov" in text_lower or "sorov" in text_lower or "nechta" in text_lower):
            await message.reply(
                f"Bugungi statistika: 📅\n"
                f"Bugun jami **{stats['today_requests']}** ta ovozli so'rov qayta ishlandi.",
                parse_mode="Markdown"
            )
            return

        if "kimlar kirdi" in text_lower or "kim kirdi" in text_lower or "ro'yxat" in text_lower:
            recent = db.get_recent_users(15)
            users_list = "\n".join([
                f"• {u['first_name']} (@{u['username'] if u['username'] else 'yoʻq'}) - {u['last_active']}"
                for u in recent
            ])
            await message.reply(f"📋 Oxirgi kirganlar ro'yxati:\n\n{users_list}")
            return

    bot_info = await bot.get_me()
    bot_tag = f"@{bot_info.username}" if bot_info.username else ""
    await message.reply(f"Iltimos, ovozli xabar (voice) yuboring!\n\n{bot_tag}")


@dp.message(F.voice)
async def handle_voice(message: Message):
    voice = message.voice
    if not voice:
        return

    user = message.from_user
    db.add_or_update_user(user.id, user.username, user.first_name, user.last_name)
    db.log_request(user.id, "voice_reverse_denoise")

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


async def on_startup(bot: Bot) -> None:
    logger.info("Bot ishga tushmoqda...")
    await set_bot_commands(bot)
    logger.info(f"Webhook sozlanmoqda: {WEBHOOK_URL}")
    await bot.set_webhook(
        url=WEBHOOK_URL,
        drop_pending_updates=True,
        allowed_updates=dp.resolve_used_update_types()
    )
    bot_user = await bot.get_me()
    logger.info(f"✅ Webhook va buyruqlar o'rnatildi! Bot: @{bot_user.username}")


async def on_shutdown(bot: Bot) -> None:
    logger.info("Bot to'xtatilmoqda...")
    await bot.session.close()


async def handle_health_check(request: web.Request) -> web.Response:
    return web.json_response({
        "status": "ok",
        "service": "Reverse Voice Bot with Admin & Denoise",
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
        bot=bot
    )
    webhook_requests_handler.register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)

    logger.info(f"Server 0.0.0.0:{port} portida ishga tushmoqda...")
    web.run_app(app, host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
