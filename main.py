import asyncio
import logging
import os
import uuid
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, FSInputFile, BotCommand, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web
from dotenv import load_dotenv
import speech_recognition as sr

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

# Admin tekshiruvi uchun
ADMIN_USERNAMES = ["o_raximjonov", "o_raximjonovuz"]

WEBHOOK_HOST = os.getenv("WEBHOOK_HOST", "https://grandpa-voice-bot.onrender.com").rstrip("/")
WEBHOOK_PATH = f"/webhook/{BOT_TOKEN}"
WEBHOOK_URL = f"{WEBHOOK_HOST}{WEBHOOK_PATH}"

BASE_DIR = Path(__file__).parent
TEMP_DIR = BASE_DIR / "temp"
TEMP_DIR.mkdir(parents=True, exist_ok=True)

MAX_VOICE_DURATION = 120  # 2 daqiqa
MAX_FILE_SIZE = 20 * 1024 * 1024  # 20 MB

CONCURRENCY_LIMIT = 5
processing_semaphore = asyncio.Semaphore(CONCURRENCY_LIMIT)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# Bazani ishga tushirish
db.init_db()


def is_admin_user(username: str, user_id: int) -> bool:
    uname = (username or "").lower().strip("@").strip()
    return any(a in uname for a in ["o_raximjonov", "raximjonov"]) or uname in ADMIN_USERNAMES


async def set_bot_commands(bot: Bot):
    commands = [
        BotCommand(command="start", description="Botni ishga tushirish"),
        BotCommand(command="help", description="Yordam va qo'llanma"),
        BotCommand(command="admin", description="Admin paneli (statistika)")
    ]
    await bot.set_my_commands(commands)


async def process_reverse_voice(input_path: Path, output_path: Path) -> None:
    """Ovozni tozalash va teskarisiga o'girish."""
    async with processing_semaphore:
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
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        await process.communicate()


async def convert_to_wav(input_path: Path, output_wav: Path) -> None:
    """Speech recognition uchun 16kHz mono WAV ga o'tkazish."""
    cmd = [
        "ffmpeg",
        "-y",
        "-i", str(input_path),
        "-ar", "16000",
        "-ac", "1",
        str(output_wav)
    ]
    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    await process.communicate()


def recognize_speech_from_wav(wav_path: Path) -> str:
    """WAV fayldan nutqni matnga aylantirish (uz-UZ, ru-RU fallback)."""
    recognizer = sr.Recognizer()
    with sr.AudioFile(str(wav_path)) as source:
        audio_data = recognizer.record(source)

    # 1-navbatda o'zbek tili, tushunmasa rus/ingliz
    for lang in ["uz-UZ", "ru-RU", "en-US"]:
        try:
            text = recognizer.recognize_google(audio_data, language=lang)
            if text:
                return text
        except (sr.UnknownValueError, sr.RequestError):
            continue

    return "Nutqni aniqlab bo'lmadi (ovoz juda past yoki noaniq)."


@dp.message(CommandStart())
async def handle_start(message: Message):
    user = message.from_user
    db.add_or_update_user(user.id, user.username, user.first_name, user.last_name)
    bot_info = await bot.get_me()
    username = bot_info.username or "bot"

    if is_admin_user(user.username, user.id):
        text = (
            f"Assalomu alaykum, hurmatli Admin ({user.first_name})! 👑\n\n"
            "Siz bot boshqaruvchisi sifatida tanildingiz.\n\n"
            "Mavjud buyruqlar:\n"
            "• /admin — Bot statistikasi\n"
            "• /help — Bot imkoniyatlari\n\n"
            f"@{username}"
        )
    else:
        text = (
            f"Assalomu alaykum, {user.first_name}! 🎙️✨\n\n"
            "Menga ixtiyoriy **ovozli xabar (voice)** yuboring!\n\n"
            "Mavjud xizmatlar:\n"
            "1. 🔁 **Ovozni teskari qilish** (Reverse Voice)\n"
            "2. 📝 **Ovozni matnga aylantirish** (Voice to Text)\n\n"
            f"@{username}"
        )
    await message.reply(text)


@dp.message(Command("help"))
async def handle_help(message: Message):
    user = message.from_user
    db.add_or_update_user(user.id, user.username, user.first_name, user.last_name)
    bot_info = await bot.get_me()
    username = bot_info.username or "bot"
    text = (
        "💡 Botdan foydalanish qo'llanmasi:\n\n"
        "1. Menga ovozli xabar (voice) yuboring.\n"
        "2. Bot sizga 2 ta tugma chiqarib beradi:\n"
        "   • 🔁 Teskari ovoz — ovozni teskarisiga o'giradi\n"
        "   • 📝 Matnga aylantirish — ovozdagi gaplarni matn qilib beradi\n"
        "3. Kerakli tugmani tanlang va natijani oling!\n\n"
        f"@{username}"
    )
    await message.reply(text)


@dp.message(Command("admin"))
async def handle_admin(message: Message):
    user = message.from_user
    db.add_or_update_user(user.id, user.username, user.first_name, user.last_name)

    if not is_admin_user(user.username, user.id):
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


# Ovoz qabul qilish va inline tugmalar bilan tanlov berish
@dp.message(F.voice)
async def handle_voice(message: Message):
    voice = message.voice
    if not voice:
        return

    user = message.from_user
    db.add_or_update_user(user.id, user.username, user.first_name, user.last_name)

    if voice.duration and voice.duration > MAX_VOICE_DURATION:
        await message.reply(f"⚠️ Ovoz juda uzun. Maksimal {MAX_VOICE_DURATION} soniya.")
        return

    # Inline tugmalar: 1. Teskari ovoz  2. Matnga aylantirish
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🔁 Teskari ovoz", callback_data=f"rev:{voice.file_id}"),
            InlineKeyboardButton(text="📝 Matnga aylantirish", callback_data=f"txt:{voice.file_id}")
        ]
    ])

    await message.reply(
        "🎧 Ovoz qabul qilindi! Qanday amal bajarilsin?",
        reply_markup=keyboard
    )


# Tugma bosilganda ishlovchi callback
@dp.callback_query(F.data.startswith("rev:") | F.data.startswith("txt:"))
async def handle_voice_action(callback: CallbackQuery):
    action, file_id = callback.data.split(":", 1)
    user = callback.from_user
    bot_info = await bot.get_me()
    bot_tag = f"@{bot_info.username}" if bot_info.username else ""

    task_id = uuid.uuid4().hex
    input_file = TEMP_DIR / f"in_{task_id}.ogg"

    try:
        await callback.answer("Bajarilmoqda...")
        file_info = await bot.get_file(file_id)
        if not file_info.file_path:
            await callback.message.reply("Faylni yuklab bo'lmadi.")
            return

        await bot.download_file(file_info.file_path, destination=input_file)

        if action == "rev":
            # 1. Teskari ovoz
            output_file = TEMP_DIR / f"rev_{task_id}.ogg"
            await process_reverse_voice(input_file, output_file)
            db.log_request(user.id, "voice_reverse")

            processed_voice = FSInputFile(str(output_file))
            await callback.message.reply_voice(
                voice=processed_voice,
                caption=bot_tag
            )
            if output_file.exists():
                output_file.unlink()

        elif action == "txt":
            # 2. Voice to text (ovozni matnga aylantirish)
            wav_file = TEMP_DIR / f"speech_{task_id}.wav"
            await convert_to_wav(input_file, wav_file)
            recognized_text = await asyncio.to_thread(recognize_speech_from_wav, wav_file)
            db.log_request(user.id, "voice_to_text")

            await callback.message.reply(
                f"📝 **Ovozdagi matn:**\n\n\"{recognized_text}\"\n\n{bot_tag}"
            )
            if wav_file.exists():
                wav_file.unlink()

    except Exception as e:
        logger.exception(f"Callback xatolik: {e}")
        await callback.message.reply("❌ Xatolik yuz berdi. Qaytadan urinib ko'ring.")

    finally:
        if input_file.exists():
            try:
                input_file.unlink()
            except OSError:
                pass


@dp.message(F.text)
async def handle_text(message: Message):
    user = message.from_user
    db.add_or_update_user(user.id, user.username, user.first_name, user.last_name)
    text_lower = message.text.lower()

    if is_admin_user(user.username, user.id):
        stats = db.get_stats()
        if "nechta odam" in text_lower or "odam soni" in text_lower:
            await message.reply(f"📊 Jami foydalanuvchilar: {stats['total_users']} ta.\nBugun faol: {stats['today_active_users']} ta.")
            return
        if "bugun" in text_lower and "so'rov" in text_lower:
            await message.reply(f"📅 Bugungi so'rovlar: {stats['today_requests']} ta.")
            return
        if "kimlar kirdi" in text_lower or "kim kirdi" in text_lower:
            recent = db.get_recent_users(15)
            users_list = "\n".join([f"• {u['first_name']} (@{u['username'] or 'yoʻq'}) - {u['last_active']}" for u in recent])
            await message.reply(f"📋 Oxirgi kirganlar:\n\n{users_list}")
            return

    bot_info = await bot.get_me()
    bot_tag = f"@{bot_info.username}" if bot_info.username else ""
    await message.reply(f"Iltimos, ovozli xabar (voice) yuboring!\n\n{bot_tag}")


async def on_startup(bot: Bot) -> None:
    logger.info("Bot ishga tushmoqda...")
    await set_bot_commands(bot)
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
        "service": "Reverse & Voice-to-Text Bot",
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
