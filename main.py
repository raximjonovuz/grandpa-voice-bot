import asyncio
import json
import logging
import os
import uuid
import urllib.request
import urllib.error
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
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()

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

MAX_VOICE_DURATION = 15  # Matnga aylantirish uchun aniq 15 soniya
MAX_REVERSE_DURATION = 60

CONCURRENCY_LIMIT = 5
processing_semaphore = asyncio.Semaphore(CONCURRENCY_LIMIT)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

db.init_db()


def is_admin_user(username: str, user_id: int) -> bool:
    uname = (username or "").lower().strip("@").strip()
    return any(a in uname for a in ["o_raximjonov", "raximjonov"]) or uname in ADMIN_USERNAMES


async def check_channel_subscription(user_id: int) -> bool:
    """Foydalanuvchi kanalga a'zo bo'lganmi yoki yo'qligini qat'iy tekshirish."""
    try:
        member = await bot.get_chat_member(chat_id=REQUIRED_CHANNEL, user_id=user_id)
        return member.status in ["creator", "administrator", "member"]
    except Exception as e:
        logger.warning(f"Obuna tekshirish: user {user_id} a'zo emas ({e})")
        return False


def get_subscribe_keyboard() -> InlineKeyboardMarkup:
    """Kanalga a'zo bo'lish tugmalari."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Kanalga a'zo bo'lish", url=CHANNEL_URL)],
        [InlineKeyboardButton(text="✅ A'zo bo'ldim (Tekshirish)", callback_data="check_sub")]
    ])


def get_main_keyboard() -> ReplyKeyboardMarkup:
    """4 ta asosiy bo'lim menyusi."""
    kb = [
        [
            KeyboardButton(text="🔁 Teskari ovoz"),
            KeyboardButton(text="🧹 Shovqindan tozalash")
        ],
        [
            KeyboardButton(text="📝 Matnga aylantirish (15s)"),
            KeyboardButton(text="🎭 Hazilkash AI")
        ]
    ]
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)


async def set_bot_commands(bot: Bot):
    commands = [
        BotCommand(command="start", description="Botni ishga tushirish"),
        BotCommand(command="help", description="Yordam va qo'llanma")
    ]
    await bot.set_my_commands(commands)


async def process_reverse_voice(input_path: Path, output_path: Path) -> None:
    """Toza teskari ovoz."""
    async with processing_semaphore:
        cmd = [
            "ffmpeg", "-y",
            "-i", str(input_path),
            "-af", "areverse",
            "-c:a", "libopus", "-b:a", "64k",
            str(output_path)
        ]
        process = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        await process.communicate()


async def process_denoise_voice(input_path: Path, output_path: Path) -> None:
    """Ovozdagi shovqinni chuqur tozalash (AI Denoise filtri)."""
    async with processing_semaphore:
        # afftdn (FFT Denoise) + highpass (pastki g'uvillashlarni kesish) + lowpass (chiyillashni kesish) + acompressor
        audio_filter = "highpass=f=80,lowpass=f=8000,afftdn=nr=18:nf=-30,acompressor=threshold=-18dB:ratio=3"
        cmd = [
            "ffmpeg", "-y",
            "-i", str(input_path),
            "-af", audio_filter,
            "-c:a", "libopus", "-b:a", "64k",
            str(output_path)
        ]
        process = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        await process.communicate()


async def convert_to_mp3(input_path: Path, output_mp3: Path) -> None:
    """Groq Whisper uchun audio format."""
    cmd = [
        "ffmpeg", "-y",
        "-i", str(input_path),
        "-ar", "16000", "-ac", "1", "-b:a", "32k",
        str(output_mp3)
    ]
    process = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    await process.communicate()


def transcribe_with_groq_whisper(audio_file_path: Path) -> str:
    """Groq Whisper large v3 turbo yordamida o'zbek, rus, ingliz, turk va boshqa tillarda matn qilish."""
    url = "https://api.groq.com/openai/v1/audio/transcriptions"
    boundary = "----WebKitFormBoundaryGroqWhisper"
    
    with open(audio_file_path, "rb") as f:
        file_bytes = f.read()

    data_parts = []
    # model parametri
    data_parts.append(f"--{boundary}".encode())
    data_parts.append(b'Content-Disposition: form-data; name="model"\r\n')
    data_parts.append(b"whisper-large-v3-turbo")

    # file parametri
    data_parts.append(f"--{boundary}".encode())
    data_parts.append(b'Content-Disposition: form-data; name="file"; filename="audio.mp3"')
    data_parts.append(b"Content-Type: audio/mpeg\r\n")
    data_parts.append(file_bytes)

    data_parts.append(f"--{boundary}--\r\n".encode())
    body = b"\r\n".join(data_parts)

    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Authorization", f"Bearer {GROQ_API_KEY}")
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    req.add_header("User-Agent", "Mozilla/5.0")

    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            result = json.loads(resp.read().decode())
            return result.get("text", "").strip() or "Nutq aniqlanmadi."
    except Exception as e:
        logger.error(f"Groq Whisper xatosi: {e}")
        return "Ovozdagi nutqni aniqlashda xatolik bo'ldi."


def generate_roast_chat(user_name: str, user_age: str, user_message: str) -> str:
    """Llama-3.1-8b-instant yordamida yengil hazil va kulgili javob berish."""
    url = "https://api.groq.com/openai/v1/chat/completions"
    
    system_prompt = (
        "Sen Telegram botidagi kulgili, o'tkir tilli, hazilkash va biroz 'trolling' qiluvchi do'stsan. "
        "Foydalanuvchi bilan o'zbek tilida gaplashasan. "
        f"Foydalanuvchining ismi: {user_name}, yoshi: {user_age}. "
        "Uning yoshi, ismi yoki yozgan gapi ustidan xafagarchiliksiz, yengil, samimiy va kulgili qilib hazil qil (roast). "
        "Javobing 2-3 ta qisqa jumladan oshmasin, kulgili va ixcham bo'lsin. Emojilardan foydalan."
    )

    payload = {
        "model": "llama-3.1-8b-instant",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message}
        ],
        "max_tokens": 150,
        "temperature": 0.8
    }

    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {GROQ_API_KEY}",
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0"
        },
        method="POST"
    )

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
            return data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        logger.error(f"Groq Chat xatosi: {e}")
        return f"Hey {user_name}, hazil qilmoqchi edim-u, aqlim yetmay qoldi 😂"


# --- Telegram Bot Handlerlari ---

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
            "Pastdagi bo'limlardan birini tanlang:\n"
            "1. 🔁 Teskari ovoz\n"
            "2. 🧹 Shovqindan tozalash\n"
            "3. 📝 Matnga aylantirish (15s)\n"
            "4. 🎭 Hazilkash AI\n\n"
            "📢 Homiy: @NovaTechLab",
            reply_markup=get_main_keyboard()
        )
    else:
        await callback.answer("❌ Hali kanalga a'zo bo'lmadingiz. Iltimos, obuna bo'ling!", show_alert=True)


@dp.message(CommandStart())
async def handle_start(message: Message):
    user = message.from_user
    db.add_or_update_user(user.id, user.username, user.first_name, user.last_name)
    bot_info = await bot.get_me()
    username = bot_info.username or "bot"

    # Hamma uchun kanal obunasini tekshirish
    is_sub = await check_channel_subscription(user.id)
    if not is_sub:
        await message.reply(
            "👋 Assalomu alaykum!\n\n"
            "Botdan to'liq foydalanish uchun rasmiy kanalimizga a'zo bo'ling:\n"
            "👉 @NovaTechLab\n\n"
            "Obuna bo'lgach, «✅ A'zo bo'ldim» tugmasini bosing:",
            reply_markup=get_subscribe_keyboard()
        )
        return

    text = (
        f"Assalomu alaykum, {user.first_name}! 🎙️✨\n\n"
        "Quyidagi bo'limlardan birini tanlang:\n\n"
        "1. 🔁 Teskari ovoz — ovozni teskarisiga o'giradi\n"
        "2. 🧹 Shovqindan tozalash — ovozdagi shovqinlarni tozalaydi\n"
        "3. 📝 Matnga aylantirish — 15s ovozni matn qilib beradi\n"
        "4. 🎭 Hazilkash AI — siz bilan hazillashadigan AI chat\n\n"
        "📢 Homiy kanal: @NovaTechLab\n"
        f"@{username}"
    )
    await message.reply(text, reply_markup=get_main_keyboard())


@dp.message(F.text == "🔁 Teskari ovoz")
async def set_reverse_mode(message: Message):
    user = message.from_user
    if not await check_channel_subscription(user.id):
        await message.reply("⚠️ Kanalimizga a'zo bo'ling!", reply_markup=get_subscribe_keyboard())
        return
    db.set_user_mode(user.id, "reverse")
    await message.reply("✅ «Teskari ovoz» rejimi tanlandi! Menga ovozli xabar (voice) yuboring.", reply_markup=get_main_keyboard())


@dp.message(F.text == "🧹 Shovqindan tozalash")
async def set_denoise_mode(message: Message):
    user = message.from_user
    if not await check_channel_subscription(user.id):
        await message.reply("⚠️ Kanalimizga a'zo bo'ling!", reply_markup=get_subscribe_keyboard())
        return
    db.set_user_mode(user.id, "denoise")
    await message.reply("✅ «Shovqindan tozalash» rejimi tanlandi! Ovozli xabar yuboring, fon shovqinini tozalab beraman.", reply_markup=get_main_keyboard())


@dp.message(F.text == "📝 Matnga aylantirish (15s)")
async def set_text_mode(message: Message):
    user = message.from_user
    if not await check_channel_subscription(user.id):
        await message.reply("⚠️ Kanalimizga a'zo bo'ling!", reply_markup=get_subscribe_keyboard())
        return
    db.set_user_mode(user.id, "text")
    await message.reply("✅ «Matnga aylantirish (15s)» rejimi tanlandi!\nMaksimal 15 soniyagacha ovoz yuboring. O'zbek, Rus, Ingliz, Turk va boshqa tillarni taniydi.", reply_markup=get_main_keyboard())


@dp.message(F.text == "🎭 Hazilkash AI")
async def start_roast_chat(message: Message):
    user = message.from_user
    if not await check_channel_subscription(user.id):
        await message.reply("⚠️ Kanalimizga a'zo bo'ling!", reply_markup=get_subscribe_keyboard())
        return
    db.set_user_mode(user.id, "roast")
    db.set_chat_state(user.id, "ask_name")
    await message.reply("🎭 Hazilkash AI suhbatiga xush kelibsiz!\n\nKeling, oldin tanishib olaylik. Ismingiz nima?", reply_markup=get_main_keyboard())


@dp.message(Command("help"))
async def handle_help(message: Message):
    bot_info = await bot.get_me()
    username = bot_info.username or "bot"
    text = (
        "💡 Botdan foydalanish:\n\n"
        "1. 🔁 Teskari ovoz: Ovozni teskarisiga o'giradi\n"
        "2. 🧹 Shovqindan tozalash: G'uvillash va shovqinlarni o'chiradi\n"
        "3. 📝 Matnga aylantirish: 15s gacha ovozlarni barcha tillarda matn qiladi\n"
        "4. 🎭 Hazilkash AI: Ism va yoshingiz bo'yicha hazil qiladi\n\n"
        "📢 Kanal: @NovaTechLab\n"
        f"@{username}"
    )
    await message.reply(text, reply_markup=get_main_keyboard())


@dp.message(Command("admin"))
async def handle_admin(message: Message):
    user = message.from_user
    if not is_admin_user(user.username, user.id):
        await message.reply("Noma'lum buyruq. Yordam uchun /help ni bosing.", reply_markup=get_main_keyboard())
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


# Ovozli xabarlar
@dp.message(F.voice)
async def handle_voice(message: Message):
    voice = message.voice
    if not voice:
        return

    user = message.from_user
    db.add_or_update_user(user.id, user.username, user.first_name, user.last_name)

    if not await check_channel_subscription(user.id):
        await message.reply("⚠️ Kechirasiz, botdan foydalanish uchun kanalimizga a'zo bo'ling!", reply_markup=get_subscribe_keyboard())
        return

    mode = db.get_user_mode(user.id)
    bot_info = await bot.get_me()
    bot_tag = f"@{bot_info.username}" if bot_info.username else ""

    # 15 soniya tekshiruvi (matnga aylantirish uchun)
    if mode == "text" and voice.duration and voice.duration > MAX_VOICE_DURATION:
        await message.reply(
            f"⚠️ Ovoz juda uzun ({voice.duration} soniya)!\n"
            f"Matnga aylantirish uchun maksimal {MAX_VOICE_DURATION} soniyagacha bo'lgan ovoz yuboring.",
            reply_markup=get_main_keyboard()
        )
        return

    task_id = uuid.uuid4().hex
    input_file = TEMP_DIR / f"in_{task_id}.ogg"

    try:
        file_info = await bot.get_file(voice.file_id)
        if not file_info.file_path:
            await message.reply("Faylni yuklab bo'lmadi.")
            return

        await bot.download_file(file_info.file_path, destination=input_file)

        if mode == "text":
            # 2. Voice to text (Groq Whisper)
            mp3_file = TEMP_DIR / f"whisper_{task_id}.mp3"
            await convert_to_mp3(input_file, mp3_file)
            transcription = await asyncio.to_thread(transcribe_with_groq_whisper, mp3_file)
            db.log_request(user.id, "voice_to_text")

            clean_text = transcription.replace("`", "'")
            response_text = (
                "📝 **Ovozdagi matn:**\n\n"
                f"`{clean_text}`\n\n"
                "👆 *Matn ustiga bosing, avtomatik nusxalanadi!*\n\n"
                "📢 Homiy kanal: @NovaTechLab"
            )
            await message.reply(response_text, parse_mode="Markdown", reply_markup=get_main_keyboard())
            if mp3_file.exists():
                mp3_file.unlink()

        elif mode == "denoise":
            # 1. Shovqindan tozalash
            output_file = TEMP_DIR / f"denoise_{task_id}.ogg"
            await process_denoise_voice(input_file, output_file)
            db.log_request(user.id, "voice_denoise")

            processed_voice = FSInputFile(str(output_file))
            await message.reply_voice(
                voice=processed_voice,
                caption=f"🧹 Shovqindan tozalandi!\n{bot_tag}",
                reply_markup=get_main_keyboard()
            )
            if output_file.exists():
                output_file.unlink()

        else:
            # Default: Teskari ovoz
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


# Matnli xabarlar va Hazilkash AI suhbati
@dp.message(F.text)
async def handle_text(message: Message):
    user = message.from_user
    db.add_or_update_user(user.id, user.username, user.first_name, user.last_name)

    if not await check_channel_subscription(user.id):
        await message.reply("⚠️ Botdan foydalanish uchun kanalimizga obuna bo'ling!", reply_markup=get_subscribe_keyboard())
        return

    state = db.get_chat_state(user.id)
    step = state["step"]

    # Hazilkash AI intervyusi
    if step == "ask_name":
        custom_name = message.text.strip()
        db.set_chat_state(user.id, "ask_age", custom_name=custom_name)
        await message.reply(f"Xursandman, {custom_name}! 😉\nEndi yoshingizni ayting, nechiga kirdingiz?", reply_markup=get_main_keyboard())
        return

    elif step == "ask_age":
        age = message.text.strip()
        custom_name = state["name"] or user.first_name
        db.set_chat_state(user.id, "chatting", age=age)
        first_roast = await asyncio.to_thread(generate_roast_chat, custom_name, age, f"Salom, men {custom_name}man, yoshim {age}da. Meni bir hazil qilib kutib olchi!")
        await message.reply(
            f"Zo'r! Demak {custom_name}, {age} yosh.\n\n"
            f"{first_roast}\n\n"
            "Endi istalgan narsangizni yozing, hazil bilan javob qaytaraman! 😆",
            reply_markup=get_main_keyboard()
        )
        return

    elif step == "chatting":
        custom_name = state["name"] or user.first_name
        age = state["age"] or "noma'lum"
        roast_reply = await asyncio.to_thread(generate_roast_chat, custom_name, age, message.text)
        db.log_request(user.id, "roast_chat")
        await message.reply(roast_reply, reply_markup=get_main_keyboard())
        return

    # Admin savollari
    if is_admin_user(user.username, user.id):
        text_lower = message.text.lower()
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
        f"Iltimos, pastdagi 4 ta bo'limdan birini tanlang!\n\n{bot_tag}",
        reply_markup=get_main_keyboard()
    )


async def on_startup(bot: Bot) -> None:
    logger.info("Bot ishga tushmoqda...")
    await set_bot_commands(bot)
    bot_user = await bot.get_me()
    logger.info(f"✅ Bot ishga tushdi: @{bot_user.username}")


async def on_shutdown(bot: Bot) -> None:
    logger.info("Bot to'xtatilmoqda...")
    await bot.session.close()


async def handle_health_check(request: web.Request) -> web.Response:
    return web.json_response({
        "status": "ok",
        "service": "Voice & AI Roast Bot",
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
