import asyncio
import logging
import os
import shutil
import uuid
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, FSInputFile
from aiohttp import web
from dotenv import load_dotenv

# Log sozlamalari
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - [%(levelname)s] - %(name)s - %(message)s"
)
logger = logging.getLogger("GrandpaVoiceBot")

# .env yuklash va tokenni tozalab olish
load_dotenv()
raw_token = os.getenv("BOT_TOKEN") or os.getenv("bottoken")
if not raw_token:
    raise ValueError("BOT_TOKEN topilmadi! Iltimos .env faylida BOT_TOKEN ni ko'rsating.")

# Sharhlar (// yoki #) va qo'shtirnoqlardan tozalash
BOT_TOKEN = raw_token.split("//")[0].split("#")[0].strip().strip('"').strip("'")

# Papkalar va cheklovlar
BASE_DIR = Path(__file__).parent
TEMP_DIR = BASE_DIR / "temp"
TEMP_DIR.mkdir(parents=True, exist_ok=True)

MAX_VOICE_DURATION = 60  # soniya (1 daqiqa)
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB

# Server resursini tejash uchun bir vaqtning o'zida maksimal 3 ta ffmpeg jarayoni
CONCURRENCY_LIMIT = 3
processing_semaphore = asyncio.Semaphore(CONCURRENCY_LIMIT)

# Haqiqiy Instagram / CapCut "Grandpa" (Boboy) ovoz effekti DSP filtri:
# 1. aresample=48000: Barcha turdagi audio/ovozlarni 48kHz ga standartlashtirish
# 2. asetrate=48000*0.84,atempo=1.19: Qari odamning tabiiy tembriga o'tkazish (pitch down ~3 semiton)
# 3. vibrato=f=6.2:d=0.24 va tremolo=f=6.2:d=0.32: Qariyalarga xos fiziologik ovoz va nafas titrashi (senile vocal tremor)
# 4. highpass=f=180,lowpass=f=3800: Yoshlikka xos o'tkir yuqori va past shovqinlarni kesish
# 5. equalizer: 1150Hz dagi bo'g'iz rezonansini kuchaytirish va 320Hz ni yupqalashtirish
GRANDPA_AUDIO_FILTER = (
    "aresample=48000,"
    "asetrate=48000*0.84,"
    "atempo=1.19,"
    "vibrato=f=6.2:d=0.24,"
    "tremolo=f=6.2:d=0.32,"
    "highpass=f=180,"
    "lowpass=f=3800,"
    "equalizer=f=1150:t=q:w=1.4:g=4,"
    "equalizer=f=320:t=q:w=1.2:g=-2,"
    "volume=1.2"
)

# Aiogram bot va dispatcher
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


async def process_grandpa_voice(input_path: Path, output_path: Path) -> None:
    """
    FFmpeg yordamida audio faylni nozik Instagram 'Grandpa' ovoz effektiga o'tkazish.
    Asinxron subprocess orqali asosiy event loop bloklanmaydi.
    """
    async with processing_semaphore:
        cmd = [
            "ffmpeg",
            "-y",
            "-i", str(input_path),
            "-af", GRANDPA_AUDIO_FILTER,
            "-c:a", "libopus",
            "-b:a", "32k",
            "-vbr", "on",
            str(output_path)
        ]

        logger.info(f"FFmpeg ishga tushirilmoqda: {input_path.name} -> {output_path.name}")
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
    text = (
        "Assalomu alaykum! 👴🎤\n\n"
        "Men **Instagram Grandpa (Boboy)** ovoz effekti botiman!\n\n"
        "Menga ixtiyoriy **ovozli xabar (voice)** yuboring, men uni darhol "
        "titroq, xirildoq va qari boboy ovoziga o'tkazib qaytarib beraman.\n\n"
        "⚙️ *Cheklovlar:* Ovoz uzunligi maksimum 60 soniya."
    )
    await message.reply(text, parse_mode="Markdown")


@dp.message(Command("help"))
async def handle_help(message: Message):
    text = (
        "💡 **Botdan foydalanish bo'yicha qo'llanma:**\n\n"
        "1. Mikrofon tugmasini bosib Telegram ovozli xabari (voice) yozib yuboring.\n"
        "2. Bot bir necha soniyada Instagramdagi mashhur 'Grandpa' effektini qo'llaydi.\n"
        "3. Tayyor ovoz sizga qaytarib yuboriladi.\n\n"
        "Xatolik yoki takliflar bo'lsa, adminga murojaat qiling."
    )
    await message.reply(text, parse_mode="Markdown")


@dp.message(F.voice)
async def handle_voice(message: Message):
    voice = message.voice
    if not voice:
        return

    # Uzunlik tekshiruvi
    if voice.duration and voice.duration > MAX_VOICE_DURATION:
        await message.reply(
            f"⚠️ Ovozli xabar juda uzun ({voice.duration}s). "
            f"Iltimos, maksimum {MAX_VOICE_DURATION} soniyagacha bo'lgan ovoz yuboring."
        )
        return

    # Hajm tekshiruvi
    if voice.file_size and voice.file_size > MAX_FILE_SIZE:
        await message.reply(
            f"⚠️ Ovozli xabar hajmi juda katta ({voice.file_size // (1024 * 1024)} MB). "
            f"Iltimos, 10 MB dan kichik ovoz yuboring."
        )
        return

    # Vaqtinchalik holat xabari
    status_msg = await message.reply("👴 Ovoz boboy tiliga o'tkazilmoqda, biroz kuting...")

    task_id = uuid.uuid4().hex
    input_file = TEMP_DIR / f"in_{task_id}.ogg"
    output_file = TEMP_DIR / f"out_{task_id}.ogg"

    try:
        # Ovoz faylini yuklab olish
        file_info = await bot.get_file(voice.file_id)
        if not file_info.file_path:
            raise ValueError("Fayl yo'lini olib bo'lmadi.")

        await bot.download_file(file_info.file_path, destination=input_file)

        # FFmpeg bilan o'zgartirish
        await process_grandpa_voice(input_file, output_file)

        # Telegram ovozli xabar sifatida foydalanuvchiga qaytarish
        processed_voice = FSInputFile(str(output_file))
        await message.reply_voice(
            voice=processed_voice,
            caption="👴 Mana, boboy ovozida tayyor bo'ldi!"
        )

    except Exception as e:
        logger.exception(f"Ovozni qayta ishlashda xatolik: {e}")
        await message.reply(
            "❌ Kechirasiz, ovozni qayta ishlashda kutilmagan xatolik yuz berdi. "
            "Iltimos, qaytadan urinib ko'ring."
        )

    finally:
        # Vaqtinchalik fayllarni o'chirish (disk to'lib ketmasligi uchun)
        if input_file.exists():
            try:
                input_file.unlink()
            except OSError:
                pass

        if output_file.exists():
            try:
                output_file.unlink()
            except OSError:
                pass

        # Vaqtinchalik status xabarini o'chirish
        try:
            await status_msg.delete()
        except Exception:
            pass


@dp.message()
async def handle_other_messages(message: Message):
    """Ovozli xabardan boshqa har qanday xabar uchun eslatma."""
    await message.reply(
        "🎤 Iltimos, menga **ovozli xabar (voice note)** yuboring! "
        "Men uni Instagram 'Grandpa' effektiga aylantirib beraman.",
        parse_mode="Markdown"
    )


# --- Render / Cloud Web Service Health Check Server ---
async def handle_health_check(request: web.Request) -> web.Response:
    return web.json_response({
        "status": "ok",
        "service": "Grandpa Voice Bot",
        "aiogram": "running"
    })


async def start_health_check_server(port: int) -> web.AppRunner:
    app = web.Application()
    app.router.add_get("/", handle_health_check)
    app.router.add_get("/health", handle_health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"Health-check veb-serveri 0.0.0.0:{port} portida ishga tushdi.")
    return runner


async def cleanup_temp_dir():
    """Start va shutdown vaqtida qolib ketgan eski fayllarni tozalash."""
    if TEMP_DIR.exists():
        for item in TEMP_DIR.glob("*.ogg"):
            try:
                item.unlink()
            except OSError:
                pass


async def main():
    logger.info("Bot ishga tushmoqda...")
    await cleanup_temp_dir()

    # Faqat Render/Cloud serverda health-check veb-serverini ishga tushiramiz
    runner = None
    if os.getenv("RENDER"):
        port = int(os.getenv("PORT", "8080"))
        try:
            runner = await start_health_check_server(port)
        except Exception as e:
            logger.warning(f"Health-check veb-serveri xatosi: {e}")

    try:
        # Eski kutilayotgan yangilanishlarni tozalash va pollingni boshlash
        await bot.delete_webhook(drop_pending_updates=True)
        bot_user = await bot.get_me()
        logger.info(f"✅ Bot tayyor va ishga tushdi: @{bot_user.username}")
        logger.info("Endi Telegramda botga ovozli xabar yuborishingiz mumkin.")
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        logger.info("Bot to'xtatilmoqda...")
        if runner:
            await runner.cleanup()
        await bot.session.close()
        await cleanup_temp_dir()
        logger.info("Bot muvaffaqiyatli to'xtatildi.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Dastur foydalanuvchi tomonidan to'xtatildi.")
