import asyncio
import logging
import os
import uuid
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message, 
    FSInputFile, 
    BotCommand, 
    ReplyKeyboardMarkup, 
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery
)
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
logger = logging.getLogger("VoiceBot")

load_dotenv()
raw_token = os.getenv("BOT_TOKEN") or os.getenv("bottoken")
if not raw_token:
    raise ValueError("BOT_TOKEN topilmadi! Iltimos .env faylida BOT_TOKEN ni ko'rsating.")

BOT_TOKEN = raw_token.split("//")[0].split("#")[0].strip().strip('"').strip("'")

# Admin tekshiruvi uchun
ADMIN_USERNAMES = ["o_raximjonov", "o_raximjonovuz"]

# Majburiy obuna kanali
REQUIRED_CHANNEL = "@NovaTechLab"
CHANNEL_URL = "https://t.me/NovaTechLab"

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


async def check_channel_subscription(user_id: int) -> bool:
    """Foydalanuvchi kanalga a'zo bo'lganmi yoki yo'qligini tekshirish."""
    try:
        member = await bot.get_chat_member(chat_id=REQUIRED_CHANNEL, user_id=user_id)
        # creator, administrator, member statuslari ruxsat berilgan
        return member.status in ["creator", "administrator", "member"]
    except Exception as e:
        logger.error(f"Obuna tekshirishda xatolik: {e}")
        return True  # Xatolik bo'lsa foydalanuvchini bloklab qo'ymaslik uchun


def get_subscribe_keyboard() -> InlineKeyboardMarkup:
    """Kanalga a'zo bo'lish uchun inline tugmalar."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Kanalga a'zo bo'lish", url=CHANNEL_URL)],
        [InlineKeyboardButton(text="✅ A'zo bo'ldim (Tekshirish)", callback_data="check_sub")]
    ])


def get_main_keyboard() -> ReplyKeyboardMarkup:
    """Pastda doimiy turadigan bo'limlar menyusi."""
    kb = [
        [
            KeyboardButton(text="🔁 Teskari ovoz"),
            KeyboardButton(text="📝 Matnga aylantirish")
        ]
    ]
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)


async def set_bot_commands(bot: Bot):
    commands = [
        BotCommand(command="start", description="Botni ishga tushirish va rejim tanlash"),
        BotCommand(command="help", description="Yordam va qo'llanma"),
        BotCommand(command="admin", description="Admin paneli (statistika)")
    ]
    await bot.set_my_commands(commands)


async def process_reverse_voice(input_path: Path, output_path: Path) -> None:
    """Ovoz parametrlarini o'zgartirmasdan sof teskarisiga o'girish."""
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
    """WAV fayldan nutqni matnga aylantirish."""
    recognizer = sr.Recognizer()
    with sr.AudioFile(str(wav_path)) as source:
        audio_data = recognizer.record(source)

    for lang in ["uz-UZ", "ru-RU", "en-US"]:
        try:
            text = recognizer.recognize_google(audio_data, language=lang)
            if text:
                return text
        except (sr.UnknownValueError, sr.RequestError):
            continue

    return "Nutqni aniqlab bo'lmadi (ovoz juda past yoki noaniq)."


@dp.callback_query(F.data == "check_sub")
async def handle_check_subscription(callback: CallbackQuery):
    user = callback.from_user
    db.add_or_update_user(user.id, user.username, user.first_name, user.last_name)
    is_sub = await check_channel_subscription(user.id)

    if is_sub:
        await callback.answer("✅ Obuna tasdiqlandi!")
        try:
            await callback.message.delete()
        except Exception:
            pass
        await callback.message.answer(
            f"Xush kelibsiz, {user.first_name}! 🎙️✨\n\n"
            "Pastdagi bo'limlardan birini tanlang va ovozli xabar yuboring:\n"
            "1. 🔁 **Teskari ovoz**\n"
            "2. 📝 **Matnga aylantirish**",
            reply_markup=get_main_keyboard(),
            parse_mode="Markdown"
        )
    else:
        await callback.answer("❌ Hali kanalga a'zo bo'lmadingiz. Iltimos, obuna bo'ling!", show_alert=True)


@dp.message(CommandStart())
async def handle_start(message: Message):
    user = message.from_user
    db.add_or_update_user(user.id, user.username, user.first_name, user.last_name)
    bot_info = await bot.get_me()
    username = bot_info.username or "bot"

    # Admin bo'lsa obunani tekshirmasdan to'g'ridan-to'g'ri o'tkazish
    if not is_admin_user(user.username, user.id):
        is_sub = await check_channel_subscription(user.id)
        if not is_sub:
            await message.reply(
                "👋 **Assalomu alaykum!**\n\n"
                "Botdan to'liq foydalanish uchun rasmiy kanalimizga a'zo bo'ling:\n"
                "👉 @NovaTechLab\n\n"
                "Obuna bo'lgach, **«✅ A'zo bo'ldim»** tugmasini bosing:",
                reply_markup=get_subscribe_keyboard(),
                parse_mode="Markdown"
            )
            return

    if is_admin_user(user.username, user.id):
        text = (
            f"Assalomu alaykum, hurmatli Admin ({user.first_name})! 👑\n\n"
            "Qanday amal bajaramiz? Quyidagi bo'limlardan birini tanlang:\n\n"
            "1. 🔁 **Teskari ovoz**\n"
            "2. 📝 **Matnga aylantirish**\n\n"
            f"@{username}"
        )
    else:
        text = (
            f"Assalomu alaykum, {user.first_name}! 🎙️✨\n\n"
            "Ovoz bilan qanday ishlaymiz? Avval quyidagi bo'limlardan birini tanlang:\n\n"
            "1. 🔁 **Teskari ovoz**\n"
            "2. 📝 **Matnga aylantirish**\n\n"
            "Kerakli bo'limni bosing, so'ng ovozli xabar (voice) yuboring!\n\n"
            f"@{username}"
        )
    await message.reply(text, reply_markup=get_main_keyboard(), parse_mode="Markdown")


@dp.message(F.text == "🔁 Teskari ovoz")
async def set_reverse_mode(message: Message):
    user = message.from_user
    if not is_admin_user(user.username, user.id):
        is_sub = await check_channel_subscription(user.id)
        if not is_sub:
            await message.reply(
                "⚠️ Botdan foydalanish uchun kanalimizga obuna bo'ling!",
                reply_markup=get_subscribe_keyboard()
            )
            return

    db.set_user_mode(user.id, "reverse")
    await message.reply(
        "✅ **\"Teskari ovoz\"** rejimi tanlandi!\n\n"
        "Endi menga ovozli xabar (voice) yuboring. Men uni teskarisiga o'girib beraman.",
        reply_markup=get_main_keyboard(),
        parse_mode="Markdown"
    )


@dp.message(F.text == "📝 Matnga aylantirish")
async def set_text_mode(message: Message):
    user = message.from_user
    if not is_admin_user(user.username, user.id):
        is_sub = await check_channel_subscription(user.id)
        if not is_sub:
            await message.reply(
                "⚠️ Botdan foydalanish uchun kanalimizga obuna bo'ling!",
                reply_markup=get_subscribe_keyboard()
            )
            return

    db.set_user_mode(user.id, "text")
    await message.reply(
        "✅ **\"Matnga aylantirish\"** rejimi tanlandi!\n\n"
        "Endi menga ovozli xabar (voice) yuboring. Men undagi gaplarni matn qilib yozib beraman.",
        reply_markup=get_main_keyboard(),
        parse_mode="Markdown"
    )


@dp.message(Command("help"))
async def handle_help(message: Message):
    user = message.from_user
    db.add_or_update_user(user.id, user.username, user.first_name, user.last_name)
    bot_info = await bot.get_me()
    username = bot_info.username or "bot"
    text = (
        "💡 Botdan foydalanish qo'llanmasi:\n\n"
        "1. Pastdagi tugmalardan kerakli bo'limni tanlang:\n"
        "   • 🔁 Teskari ovoz\n"
        "   • 📝 Matnga aylantirish\n"
        "2. So'ng mikrofonni bosib ovozli xabar (voice) yuboring.\n"
        "3. Bot tanlangan bo'lim bo'yicha darhol javob qaytaradi!\n\n"
        f"@{username}"
    )
    await message.reply(text, reply_markup=get_main_keyboard())


@dp.message(Command("admin"))
async def handle_admin(message: Message):
    user = message.from_user
    db.add_or_update_user(user.id, user.username, user.first_name, user.last_name)

    if not is_admin_user(user.username, user.id):
        uname = f"@{user.username}" if user.username else "username yo'q"
        await message.reply(
            f"Kechirasiz, ushbu buyruq faqat bot admini uchun!\n"
            f"Sizning profilingiz: {uname} (ID: {user.id})",
            reply_markup=get_main_keyboard()
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
    await message.reply(msg, reply_markup=get_main_keyboard())


# Ovoz qabul qilish va tanlangan rejim bo'yicha ishlash
@dp.message(F.voice)
async def handle_voice(message: Message):
    voice = message.voice
    if not voice:
        return

    user = message.from_user
    db.add_or_update_user(user.id, user.username, user.first_name, user.last_name)

    # Obuna tekshiruvi (faqat oddiy userlar uchun)
    if not is_admin_user(user.username, user.id):
        is_sub = await check_channel_subscription(user.id)
        if not is_sub:
            await message.reply(
                "⚠️ Kechirasiz, botdan foydalanish uchun kanalimizga a'zo bo'lishingiz kerak!",
                reply_markup=get_subscribe_keyboard()
            )
            return

    if voice.duration and voice.duration > MAX_VOICE_DURATION:
        await message.reply(f"⚠️ Ovoz juda uzun. Maksimal {MAX_VOICE_DURATION} soniya.")
        return

    mode = db.get_user_mode(user.id)
    task_id = uuid.uuid4().hex
    input_file = TEMP_DIR / f"in_{task_id}.ogg"

    try:
        file_info = await bot.get_file(voice.file_id)
        if not file_info.file_path:
            await message.reply("Faylni yuklab bo'lmadi.")
            return

        await bot.download_file(file_info.file_path, destination=input_file)
        bot_info = await bot.get_me()
        bot_tag = f"@{bot_info.username}" if bot_info.username else ""

        if mode == "text":
            wav_file = TEMP_DIR / f"speech_{task_id}.wav"
            await convert_to_wav(input_file, wav_file)
            recognized_text = await asyncio.to_thread(recognize_speech_from_wav, wav_file)
            db.log_request(user.id, "voice_to_text")

            clean_text = recognized_text.replace("`", "'")
            response_text = (
                "📝 **Ovozdagi matn:**\n\n"
                f"`{clean_text}`\n\n"
                "👆 *Matn ustiga bosing, avtomatik nusxalanadi!*"
            )

            await message.reply(
                response_text,
                parse_mode="Markdown",
                reply_markup=get_main_keyboard()
            )
            if wav_file.exists():
                wav_file.unlink()

        else:
            output_file = TEMP_DIR / f"rev_{task_id}.ogg"
            await process_reverse_voice(input_file, output_file)
            db.log_request(user.id, "voice_reverse")

            processed_voice = FSInputFile(str(output_file))
            await message.reply_voice(
                voice=processed_voice,
                caption=bot_tag,
                reply_markup=get_main_keyboard()
            )
            if output_file.exists():
                output_file.unlink()

    except Exception as e:
        logger.exception(f"Ovoz ishlash xatosi: {e}")
        await message.reply("❌ Xatolik yuz berdi. Qaytadan urinib ko'ring.", reply_markup=get_main_keyboard())

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
    await message.reply(
        f"Iltimos, pastdagi bo'limlardan birini tanlang va ovozli xabar (voice) yuboring!\n\n{bot_tag}",
        reply_markup=get_main_keyboard()
    )


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
        "service": "Voice Bot with Channel Subscription",
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
