import asyncio
import json
import logging
import os
import re
import uuid
from collections import deque
import urllib.request
import urllib.error
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
load_dotenv()

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message,
    BotCommand,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery
)
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web

import db
import cf_storage

# Log sozlamalari
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - [%(levelname)s] - %(name)s - %(message)s"
)
logger = logging.getLogger("SmartAIChatBot")

raw_token = os.getenv("BOT_TOKEN") or os.getenv("bottoken")
if not raw_token:
    raise ValueError("BOT_TOKEN topilmadi! Iltimos .env faylida BOT_TOKEN ni ko'rsating.")

BOT_TOKEN = raw_token.split("//")[0].split("#")[0].strip().strip('"').strip("'")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
GROQ_CHAT_KEY = os.getenv("GROQ_CHAT_KEY", "").strip() or GROQ_API_KEY
CHAT_MODEL = os.getenv("CHAT_MODEL", "openai/gpt-oss-120b").strip()

# Rasmiy Admin profillari va ID lari
ADMIN_USERNAMES = ["o_raximjonov", "raximjonov_admin", "o_raximjonovuz", "zmax706"]
ADMIN_IDS = {966627404, 7567280485}

WEBHOOK_HOST = os.getenv("WEBHOOK_HOST", "https://grandpa-voice-bot.onrender.com").rstrip("/")
WEBHOOK_PATH = f"/webhook/{BOT_TOKEN}"
WEBHOOK_URL = f"{WEBHOOK_HOST}{WEBHOOK_PATH}"

BASE_DIR = Path(__file__).parent
TEMP_DIR = BASE_DIR / "temp"
TEMP_DIR.mkdir(parents=True, exist_ok=True)

MAX_VOICE_DURATION = 15  # Ovozni matnga aylantirish uchun maksimal 15 soniya
CONCURRENCY_LIMIT = 5
processing_semaphore = asyncio.Semaphore(CONCURRENCY_LIMIT)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

db.init_db()

# Suhbat xotirasi va Admin joriy amallari
HISTORY_LIMIT = 8
chat_history: dict[int, deque] = {}
cleanup_ids: dict[int, list] = {}

# Admin qaysi amalni bajarayotganini saqlash:
# admin_id -> {"action": "takeover_reply" | "set_vip_uid" | "set_vip_uname" | "set_vip_uname_rule" | "set_custom_global", ...}
admin_action_state: dict[int, dict] = {}

VIP_PRESETS = {
    "no_joke": (
        "Bu inson Bossning (Adminning) juda yaqin va hurmatli odami. "
        "U bilan umuman qaltis hazil qilma, ustidan kulma va sansirama! "
        "Faqat 'Siz' deb, juda muloyim va hurmat bilan gaplash. "
        "Agar u hazil so'rasa yoki sinamoqchi bo'lsa: 'Siz bilan qaltis hazil qilolmayman, Boss bunga ruxsat bermagan, sizni qattiq hurmat qilamiz!' deb javob ber."
    ),
    "best_friend": (
        "Bu inson Bossning eng yaqin qadrdon jigari va do'sti. "
        "U bilan juda samimiy, ko'tarinki kayfiyatda, xuddi eng yaqin do'stdek hurmat va mehr bilan gaplash."
    ),
    "serious_expert": (
        "Bu foydalanuvchi bilan faqat jiddiy, aqlli va aniq faktlar asosida gaplash. "
        "Hech qanday ko'cha slengi yoki ortiqcha hazil ishlatma."
    ),
    "hard_roast": (
        "Bu foydalanuvchi bilan o'tkir sarkazm, kulgili qochirim va kuchli hazil (roast) uslubida gaplash. "
        "Har bir gapiga topqirlik bilan kulgili javob qaytar."
    )
}


def is_admin_user(username: Optional[str], user_id: int) -> bool:
    if user_id in ADMIN_IDS:
        return True
    uname = (username or "").lower().strip("@").strip()
    return uname in ADMIN_USERNAMES


async def safe_edit_or_send(callback: CallbackQuery, text: str, reply_markup: Optional[InlineKeyboardMarkup] = None):
    """Tugma bosilganda xabarni xatosiz va tez yangilash ('message is not modified' xatosini yutish)."""
    try:
        await callback.message.edit_text(text, reply_markup=reply_markup)
    except TelegramBadRequest as e:
        if "message is not modified" in str(e).lower():
            return
        try:
            await callback.message.answer(text, reply_markup=reply_markup)
        except Exception as ex:
            logger.warning(f"safe_edit_or_send answer xatosi: {ex}")
    except Exception:
        try:
            await callback.message.answer(text, reply_markup=reply_markup)
        except Exception as ex:
            logger.warning(f"safe_edit_or_send fallback xatosi: {ex}")


# --- Klaviaturalar ---

def get_main_keyboard(is_admin: bool = False) -> ReplyKeyboardMarkup:
    """Asosiy menyu: AI Suhbat va Ovozni matn qilish (+ Admin uchun Admin Panel tugmasi)."""
    kb = [
        [
            KeyboardButton(text="💬 AI Suhbat"),
            KeyboardButton(text="📝 Ovozni matn qilish (15s)")
        ]
    ]
    if is_admin:
        kb.append([KeyboardButton(text="👑 Admin Panel")])
    return ReplyKeyboardMarkup(
        keyboard=kb,
        resize_keyboard=True,
        input_field_placeholder="Bo'limni tanlang yoki xabar yozing..."
    )


def get_chat_keyboard(is_admin: bool = False) -> ReplyKeyboardMarkup:
    """AI suhbat paytida menyu tugmalari."""
    kb = [
        [
            KeyboardButton(text="⬅️ Asosiy menyu"),
            KeyboardButton(text="🔄 Suhbatni yangilash")
        ]
    ]
    if is_admin:
        kb.append([KeyboardButton(text="👑 Admin Panel")])
    return ReplyKeyboardMarkup(
        keyboard=kb,
        resize_keyboard=True,
        input_field_placeholder="Xabar yozing yoki 15s ovoz yuboring..."
    )


def get_admin_panel_keyboard() -> InlineKeyboardMarkup:
    """Admin boshqaruv paneli inline tugmalari."""
    current_persona = db.get_setting("ai_persona", "kocha")
    persona_names = {
        "kocha": "🕶️ Ko'cha bola / Jigar",
        "faylasuf": "🧠 Faylasuf / Dono",
        "sarkazm": "🔥 Sarkazm / Roast",
        "dost": "🤝 Samimiy do'st",
        "rasmiy": "💼 Rasmiy / Ekspert"
    }
    p_label = persona_names.get(current_persona, current_persona)

    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"🎭 AI Rejimi: {p_label}", callback_data="adm_personas")],
        [
            InlineKeyboardButton(text="👥 Foydalanuvchilar va Chatlar", callback_data="adm_users:0"),
            InlineKeyboardButton(text="📊 Statistika", callback_data="adm_stats")
        ],
        [
            InlineKeyboardButton(text="👑 Link/@user ga VIP muomala", callback_data="adm_add_vip"),
            InlineKeyboardButton(text="📋 VIP ro'yxati", callback_data="adm_vip_list")
        ],
        [InlineKeyboardButton(text="✍️ Global AI yo'riqnoma yozish", callback_data="adm_custom_global")]
    ])


def get_persona_selection_keyboard() -> InlineKeyboardMarkup:
    current = db.get_setting("ai_persona", "kocha")
    modes = [
        ("kocha", "🕶️ Ko'cha bola / Jigar (Sleng va hazil)"),
        ("faylasuf", "🧠 Faylasuf / Dono ustoz (Chuqur ma'noli)"),
        ("sarkazm", "🔥 Sarkazm / Roast (O'tkir qochirim)"),
        ("dost", "🤝 Samimiy do'st (Dardkash va muloyim)"),
        ("rasmiy", "💼 Rasmiy / Ekspert (Jiddiy va aniq)")
    ]
    rows = []
    for key, title in modes:
        mark = "✅ " if key == current else ""
        rows.append([InlineKeyboardButton(text=f"{mark}{title}", callback_data=f"set_persona:{key}")])
    rows.append([InlineKeyboardButton(text="⬅️ Orqaga", callback_data="adm_back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def get_vip_presets_keyboard(target_ref: str, back_cb: str = "adm_back") -> InlineKeyboardMarkup:
    """target_ref: 'uid:12345' yoki 'uname:ali_01'"""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛑 Hazil qilma (Boss ruxsat bermagan, hurmat qil)", callback_data=f"vip_pre:no_joke:{target_ref}")],
        [InlineKeyboardButton(text="🤝 Eng yaqin jigar / VIP do'st", callback_data=f"vip_pre:best_friend:{target_ref}")],
        [InlineKeyboardButton(text="🧠 Faqat jiddiy va aqlli javob ber", callback_data=f"vip_pre:serious_expert:{target_ref}")],
        [InlineKeyboardButton(text="🔥 Qattiq hazil / Roast qil", callback_data=f"vip_pre:hard_roast:{target_ref}")],
        [InlineKeyboardButton(text="🗑 VIP qoidani o'chirish", callback_data=f"vip_pre:clear:{target_ref}")],
        [InlineKeyboardButton(text="⬅️ Orqaga", callback_data=back_cb)]
    ])


async def set_bot_commands(bot: Bot):
    commands = [
        BotCommand(command="start", description="Botni ishga tushirish"),
        BotCommand(command="admin", description="Admin boshqaruv paneli"),
        BotCommand(command="help", description="Yordam va qo'llanma")
    ]
    await bot.set_my_commands(commands)


# --- Audio / Groq Whisper (Aniq O'zbekcha Voice-to-Text) ---

async def convert_to_mp3(input_path: Path, output_mp3: Path) -> None:
    async with processing_semaphore:
        cmd = [
            "ffmpeg", "-y",
            "-i", str(input_path),
            "-ar", "16000", "-ac", "1", "-b:a", "64k",
            str(output_mp3)
        ]
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        await process.communicate()


def refine_stt_text(raw_text: str) -> str:
    """
    Whisper tomonidan qozoq/qirg'iz kirill alifbosida yoki xato fonetikada yozilgan
    o'zbekcha nutqni toza O'zbek lotin alifbosiga keltirish.
    """
    if not raw_text or raw_text in ("Nutq aniqlanmadi.", "Ovozdagi nutqni aniqlashda xatolik yuz berdi."):
        return raw_text

    sys_prompt = (
        "Sen professional o'zbek tilshunosi va nutqni matnga aylantirish (STT) tahrirchisisan.\n"
        "Whisper audio modeli o'zbek tilidagi ovozli xabarlarni ba'zan qozoq/qirg'iz kirill harflari bilan "
        "(masalan: 'мәтіннің хатау тәржемі қылып сан' -> 'Matnni xato tarjima qilyapsan', "
        "'Өйті құлдық' -> 'O'tdi qulliq') yoki inglizcha/turkcha fonetikada "
        "('Salam Hamma Gya' -> 'Salom hammaga') yozib yuboradi.\n\n"
        "QAT'IY VAZIFANG:\n"
        "1. Agar matn o'zbek tilida aytilgan bo'lib, kirill (qozoq/rus/o'zbek) harflarida yoki buzilgan fonetikada bo'lsa, "
        "uning asl fonetik ohangidan kelib chiqib, TOZA VA TO'G'RI O'ZBEK LOTIN ALIFBOSIGA o'gir.\n"
        "2. Agar matn haqiqatan ham toza rus yoki ingliz tilida aytilgan bo'lsa, o'sha tilda xatosiz qoldir.\n"
        "3. FAQAT tayyor to'g'rilangan matnning o'zini qaytar! Hech qanday izoh, qo'shtirnoq yoki tushuntirish yozma."
    )
    try:
        fixed = groq_chat(
            [
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": raw_text}
            ],
            temperature=0.1,
            max_tokens=300
        )
        return fixed.strip().strip('"').strip("«»") or raw_text
    except Exception as e:
        logger.warning(f"STT post-correction xatosi: {e}")
        return raw_text


def transcribe_with_groq_whisper(audio_file_path: Path) -> str:
    """Groq Whisper large-v3 + Uzbek kontekst + AI fonetik tahrirlash."""
    url = "https://api.groq.com/openai/v1/audio/transcriptions"
    boundary = "----WebKitFormBoundaryGroqWhisperUz"

    with open(audio_file_path, "rb") as f:
        file_bytes = f.read()

    whisper_prompt = (
        "Assalomu alaykum, salom hammaga! Yaxshimisiz, ishlar qalay? "
        "Bu o'zbek tilidagi ovozli xabar. Matnni xato tarjima qilmasdan, "
        "o'zbek lotin alifbosida aniq va to'g'ri yoz."
    )

    data_parts = [
        f"--{boundary}".encode(),
        b'Content-Disposition: form-data; name="model"\r\n',
        b"whisper-large-v3",
        f"--{boundary}".encode(),
        b'Content-Disposition: form-data; name="language"\r\n',
        b"uz",
        f"--{boundary}".encode(),
        b'Content-Disposition: form-data; name="prompt"\r\n',
        whisper_prompt.encode("utf-8"),
        f"--{boundary}".encode(),
        b'Content-Disposition: form-data; name="temperature"\r\n',
        b"0",
        f"--{boundary}".encode(),
        b'Content-Disposition: form-data; name="file"; filename="audio.mp3"',
        b"Content-Type: audio/mpeg\r\n",
        file_bytes,
        f"--{boundary}--\r\n".encode()
    ]
    body = b"\r\n".join(data_parts)

    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Authorization", f"Bearer {GROQ_API_KEY}")
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    req.add_header("User-Agent", "Mozilla/5.0")

    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            raw_text = result.get("text", "").strip()
            if not raw_text:
                return "Nutq aniqlanmadi."
            return refine_stt_text(raw_text)
    except Exception as e:
        logger.error(f"Groq Whisper xatosi: {e}")
        return "Ovozdagi nutqni aniqlashda xatolik yuz berdi."


# --- AI Xarakterlar va Prompt Motori ---

PERSONA_INSTRUCTIONS = {
    "kocha": (
        "USLUB: Samimiy, hazilkash va hozirjavob o'zbek yigiti (yaqin jigar/jo'ra) uslubida gaplash. "
        "Muloqotda erkin, jonli va tabiiy bo'l. Har bir gapga avval mantiqiy javob berib, keyin o'rinli hazil qo'sh."
    ),
    "faylasuf": (
        "USLUB: Dono faylasuf va hayotiy tajribaga ega ustozdek gaplash. "
        "Foydalanuvchining har bir gapidan chuqur ma'no top, hikmatli, o'ylantiradigan va aqlli fikrlar bildir."
    ),
    "sarkazm": (
        "USLUB: O'tkir tilli sarkazm va 'roast' ustasi. "
        "Foydalanuvchining gapiga aqlli qochirim, kulgili kinoya va yengil hazil bilan javob ber."
    ),
    "dost": (
        "USLUB: Eng yaqin, mehribon, dardkash va samimiy do'st. "
        "Foydalanuvchini diqqat bilan tingla, unga dalda ber, kayfiyatini ko'tar va iliq muomalada bo'l."
    ),
    "rasmiy": (
        "USLUB: Yuqori darajadagi rasmiy ekspert va maslahatchi. "
        "Foydalanuvchiga faqat 'Siz' deb murojaat qil, xushmuomala, aniq, tizimli va jiddiy javob ber."
    )
}


def build_system_prompt(user_data: dict) -> str:
    name = user_data.get("custom_name") or user_data.get("first_name") or "Do'stim"
    age = user_data.get("age") or "noma'lum"
    uname = user_data.get("username") or "yo'q"
    memory = user_data.get("memory_notes") or ""
    vip_rule = (user_data.get("vip_instruction") or "").strip()

    persona_key = db.get_setting("ai_persona", "kocha")
    custom_global = db.get_setting("custom_global_prompt", "").strip()
    persona_text = PERSONA_INSTRUCTIONS.get(persona_key, PERSONA_INSTRUCTIONS["kocha"])

    base = (
        "Sen Telegramdagi aqlli, tabiiy va ravon gaplashadigan o'zbek suhbatdoshsan.\n"
        "MUHIM TAQIQ: Hech qachon o'zingni 'ChatGPT', 'OpenAI' yoki 'til modeliman' deb aytma! "
        "Kimligingni so'rashsa: 'Men sening aqlli suhbatdoshing va yaqin do'stingman' deb javob ber.\n"
        f"Suhbatdoshing haqida ma'lumot: Ism-familiyasi: {name} | Yoshi: {age} | Username: @{uname}.\n"
    )
    if memory:
        base += f"Suhbatdosh haqida eslab qolingan faktlar: {memory}\n"

    if vip_rule:
        base += (
            "\n⚠️ ADMIN (BOSS) TOMONIDAN USHBU SHAXS UCHUN MAXSUS QAT'IY BUYRUQ:\n"
            f"«{vip_rule}»\n"
            "Boshqa barcha hazil qoidalarini chetga surib, ushbu foydalanuvchi bilan AYNAN shu buyruq asosida gaplash!\n"
        )
    else:
        base += f"\n{persona_text}\n"
        if custom_global:
            base += f"Qo'shimcha global qoida: {custom_global}\n"

    base += (
        "\nTIL VA MULOQOT QOIDALARI:\n"
        "1. Faqat TOZA, TABIIY O'ZBEK TILIDA (lotin alifbosida) gapir.\n"
        "2. Hech qachon so'zlarni qo'shtirnoq ichiga olib sun'iy yozma (masalan, \"bomba\", \"qoyil\", \"ko'cha\" deb qo'shtirnoqqa olma!).\n"
        "3. Suhbatdoshning gapini hech qachon inkor qilma va ma'nosiz savol qaytarma. U nima yozgan bo'lsa, aynan o'sha mavzuga mantiqiy va jonli javob ber.\n"
        "4. Javobing qisqa va lo'nda bo'lsin (1-3 ta jumla). Uzun ma'ruza o'qima.\n\n"
        "TABIIY O'ZBEKCHA MULOQOT NAMUNALARI:\n"
        "- User: Kim san?\n"
        "  Javob: O'zimizning yaqin suhbatdosh do'stingman! Har qanday mavzuda gaplashamiz, nima gaplar o'zi? 😊\n"
        "- User: Bugun bir fandan yiqildim\n"
        "  Javob: Eee, chatoq bo'libdi-ku! Qaysi fan edi o'zi, domla rosa qattiq oldimi deyman? 😅\n"
        "- User: Odamday gapir\n"
        "  Javob: Bo'ldi, jiddiy to'lqinga o'tdik. Qanday mavzuda suhbatlashamiz?\n"
    )
    return base


def groq_chat(messages: list, temperature: float = 0.6, max_tokens: int = 220) -> str:
    url = "https://api.groq.com/openai/v1/chat/completions"
    models_to_try = [CHAT_MODEL, "openai/gpt-oss-120b", "qwen/qwen3.8-27b"]
    unique_models = []
    for m in models_to_try:
        if m and m not in unique_models:
            unique_models.append(m)

    last_err = None
    for model_name in unique_models:
        payload = {
            "model": model_name,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {GROQ_CHAT_KEY}",
                "Content-Type": "application/json",
                "User-Agent": "Mozilla/5.0",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                text = data["choices"][0]["message"].get("content") or ""
                text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
                if text:
                    return text
        except Exception as e:
            last_err = e
            logger.warning(f"Model {model_name} xatosi: {e}")
            continue

    raise RuntimeError(f"Barcha AI modellar xato berdi: {last_err}")


def generate_ai_reply(user_id: int, user_message: str) -> str:
    user_data = db.get_user(user_id) or {}
    history = chat_history.setdefault(user_id, deque(maxlen=HISTORY_LIMIT))

    if not history:
        recent_logs = db.get_local_chat_logs(user_id, limit=6)
        for item in recent_logs:
            role = "user" if item["sender"] == "user" else "assistant"
            history.append({"role": role, "content": item["text"]})

    messages = [{"role": "system", "content": build_system_prompt(user_data)}]
    messages += list(history)
    messages.append({"role": "user", "content": user_message})

    try:
        reply = groq_chat(messages)
        history.append({"role": "user", "content": user_message})
        history.append({"role": "assistant", "content": reply})
        return reply
    except Exception as e:
        logger.error(f"AI Chat xatosi: {e}")
        return "Hozir xayolim bir joyga jamlanmay qoldi 😅 Yana bir marta yozib yubor!"


async def record_conversation(user_id: int, sender: str, text: str):
    """Suhbatni mahalliy bazaga darhol, Cloudflare KV (72 soatlik xotira) ga fonda yozish."""
    db.log_chat_message(user_id, sender, text)
    asyncio.create_task(asyncio.to_thread(cf_storage.append_kv_chat_log, user_id, sender, text))


async def notify_admins_new_user(user):
    """Yangi foydalanuvchi kirganda barcha adminlarga xabar berish."""
    admin_ids = db.get_admin_chat_ids(ADMIN_USERNAMES, ADMIN_IDS)
    if not admin_ids:
        return

    stats = db.get_stats()
    uname = f"@{user.username}" if user.username else "username yo'q"
    full_tg_name = f"{user.first_name or ''} {user.last_name or ''}".strip()

    text = (
        "🔔 Yangi foydalanuvchi qo'shildi!\n\n"
        f"👤 Ismi: {full_tg_name}\n"
        f"🔗 Profil: {uname}\n"
        f"🆔 ID: {user.id}\n\n"
        f"📅 Bugun qo'shilganlar: {stats['today_new_users']} ta\n"
        f"👥 Jami foydalanuvchilar: {stats['total_users']} ta"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="💬 Chatini o'qish", callback_data=f"adm_view_u:{user.id}"),
            InlineKeyboardButton(text="👑 Maxsus muomala", callback_data=f"adm_vip_u:{user.id}")
        ],
        [InlineKeyboardButton(text="✍️ Bot nomidan yozish", callback_data=f"adm_takeover:{user.id}")]
    ])

    for aid in admin_ids:
        if aid == user.id:
            continue
        try:
            await bot.send_message(aid, text, reply_markup=kb)
        except Exception as e:
            logger.warning(f"Adminga ({aid}) xabar yuborishda xato: {e}")


async def safe_delete(chat_id: int, ids: list):
    for mid in ids:
        try:
            await bot.delete_message(chat_id, mid)
        except Exception:
            pass


def _build_admin_dashboard_text() -> str:
    stats = db.get_stats()
    current_persona = db.get_setting("ai_persona", "kocha")
    custom_global = db.get_setting("custom_global_prompt", "") or "Yo'q"
    return (
        "👑 Admin Boshqaruv Paneli\n\n"
        f"👥 Jami foydalanuvchilar: {stats['total_users']} ta\n"
        f"🆕 Bugun qo'shilganlar: {stats['today_new_users']} ta\n"
        f"🔥 Bugun faol bo'lganlar: {stats['today_active_users']} ta\n"
        f"⚡ Bugungi so'rovlar: {stats['today_requests']} ta (Jami: {stats['total_requests']} ta)\n\n"
        f"🎭 Joriy AI Rejimi: {current_persona.upper()}\n"
        f"📌 Qo'shimcha global qoida: {custom_global}"
    )


# --- Telegram Bot Handlerlari ---

@dp.message(CommandStart())
async def handle_start(message: Message):
    user = message.from_user
    admin_action_state.pop(user.id, None)
    is_new = db.add_or_update_user(user.id, user.username, user.first_name, user.last_name)

    if is_new:
        asyncio.create_task(notify_admins_new_user(user))

    user_info = db.get_user(user.id) or {}
    saved_name = user_info.get("custom_name")
    is_adm = is_admin_user(user.username, user.id)

    if is_adm:
        welcome = (
            f"Assalomu alaykum, hurmatli Admin ({user.first_name})! 👑\n\n"
            "Pastdagi «👑 Admin Panel» tugmasini yoki /admin buyrug'ini bosib botni to'liq boshqarishingiz mumkin.\n\n"
            "Asosiy bo'limlar:\n"
            "• 💬 AI Suhbat — Aqlli AI bilan suhbat\n"
            "• 📝 Ovozni matn qilish (15s) — Ovozli xabarni matnga o'girish"
        )
    elif saved_name:
        welcome = (
            f"Qaytganingizdan xursandman, {saved_name}! 😊✨\n\n"
            "Quyidagi bo'limlardan birini tanlang yoki to'g'ridan-to'g'ri gaplashishni boshlang:"
        )
    else:
        welcome = (
            f"Assalomu alaykum, {user.first_name}! 🤖✨\n\n"
            "Men sizning aqlli suhbatdoshingiz va ovozni matnga aylantiruvchi yordamchingizman.\n\n"
            "Quyidagi bo'limlardan birini tanlang:"
        )

    await message.answer(welcome, reply_markup=get_main_keyboard(is_adm))


@dp.message(Command("help"))
async def handle_help(message: Message):
    user = message.from_user
    admin_action_state.pop(user.id, None)
    db.add_or_update_user(user.id, user.username, user.first_name, user.last_name)
    is_adm = is_admin_user(user.username, user.id)
    text = (
        "💡 Botdan foydalanish qo'llanmasi:\n\n"
        "1. 💬 AI Suhbat — Sizni eslab qoladigan, o'zbek tilida ravon va samimiy gaplashadigan AI suhbatdosh.\n"
        "2. 📝 Ovozni matn qilish (15s) — O'zbek, Rus, Ingliz va boshqa tillardagi ovozli xabarni aniq matnga aylantirib beradi.\n\n"
        "🔄 Agar ismingiz yoki yoshingizni qayta kiritmoqchi bo'lsangiz, suhbat ichida «🔄 Suhbatni yangilash» tugmasini bosing."
    )
    await message.answer(text, reply_markup=get_main_keyboard(is_adm))


# --- ADMIN PANEL VA BOSHQARUV ---

@dp.message(Command("admin"))
@dp.message(F.text == "👑 Admin Panel")
async def handle_admin_cmd(message: Message):
    user = message.from_user
    db.add_or_update_user(user.id, user.username, user.first_name, user.last_name)

    if not is_admin_user(user.username, user.id):
        await message.answer("⛔ Bu bo'lim faqat bot adminlari uchun.")
        return

    admin_action_state.pop(user.id, None)
    await message.answer(_build_admin_dashboard_text(), reply_markup=get_admin_panel_keyboard())


@dp.callback_query(F.data == "adm_back")
async def cb_adm_back(callback: CallbackQuery):
    if not is_admin_user(callback.from_user.username, callback.from_user.id):
        await callback.answer("⛔ Faqat admin uchun!", show_alert=True)
        return
    await callback.answer()
    admin_action_state.pop(callback.from_user.id, None)
    await safe_edit_or_send(callback, _build_admin_dashboard_text(), reply_markup=get_admin_panel_keyboard())


@dp.callback_query(F.data == "adm_stats")
async def cb_adm_stats(callback: CallbackQuery):
    if not is_admin_user(callback.from_user.username, callback.from_user.id):
        await callback.answer("⛔ Faqat admin uchun!", show_alert=True)
        return
    await callback.answer()
    stats = db.get_stats()
    recent = db.get_recent_users(10)
    u_lines = []
    for idx, u in enumerate(recent, 1):
        uname = f"@{u['username']}" if u["username"] else "yo'q"
        cname = u["custom_name"] or u["first_name"]
        u_lines.append(f"{idx}. {cname} ({uname}) — {u['last_active']}")

    text = (
        "📊 Batafsil Statistika:\n\n"
        f"• Jami foydalanuvchilar: {stats['total_users']} ta\n"
        f"• Bugun yangi kirganlar: {stats['today_new_users']} ta\n"
        f"• Bugun faol bo'lganlar: {stats['today_active_users']} ta\n"
        f"• Bugungi so'rovlar: {stats['today_requests']} ta\n"
        f"• Jami so'rovlar: {stats['total_requests']} ta\n\n"
        "🕒 Oxirgi faol 10 ta foydalanuvchi:\n" + ("\n".join(u_lines) or "Hozircha yo'q")
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👥 Foydalanuvchilar chatini ochish", callback_data="adm_users:0")],
        [InlineKeyboardButton(text="⬅️ Orqaga", callback_data="adm_back")]
    ])
    await safe_edit_or_send(callback, text, reply_markup=kb)


@dp.callback_query(F.data == "adm_personas")
async def cb_adm_personas(callback: CallbackQuery):
    if not is_admin_user(callback.from_user.username, callback.from_user.id):
        await callback.answer("⛔ Faqat admin uchun!", show_alert=True)
        return
    await callback.answer()
    await safe_edit_or_send(
        callback,
        "🎭 Botning global suhbat rejimini (xarakterini) tanlang:\n\n"
        "Tanlangan rejim barcha oddiy foydalanuvchilar uchun darhol kuchga kiradi (VIP qoida berilgan shaxslardan tashqari):",
        reply_markup=get_persona_selection_keyboard()
    )


@dp.callback_query(F.data.startswith("set_persona:"))
async def cb_set_persona(callback: CallbackQuery):
    if not is_admin_user(callback.from_user.username, callback.from_user.id):
        await callback.answer("⛔ Faqat admin uchun!", show_alert=True)
        return
    mode_key = callback.data.split(":", 1)[1]
    db.set_setting("ai_persona", mode_key)
    await callback.answer(f"✅ AI rejimi '{mode_key.upper()}' ga o'zgartirildi!")
    await safe_edit_or_send(
        callback,
        f"🎭 Global suhbat rejimi yangilandi ({mode_key.upper()})!\nBoshqa rejimni tanlash uchun bosing:",
        reply_markup=get_persona_selection_keyboard()
    )


@dp.callback_query(F.data.startswith("adm_users:"))
async def cb_adm_users(callback: CallbackQuery):
    if not is_admin_user(callback.from_user.username, callback.from_user.id):
        await callback.answer("⛔ Faqat admin uchun!", show_alert=True)
        return
    await callback.answer()
    admin_action_state.pop(callback.from_user.id, None)
    users = db.get_recent_users(20)
    rows = []
    for u in users:
        name_display = u["custom_name"] or u["first_name"] or "Foydalanuvchi"
        age_display = f" ({u['age']}y)" if u["age"] else ""
        uname_display = f" @{u['username']}" if u["username"] else ""
        vip_badge = "👑 " if u["vip_instruction"] else ""
        btn_text = f"{vip_badge}{name_display}{age_display}{uname_display}"
        rows.append([InlineKeyboardButton(text=btn_text[:60], callback_data=f"adm_view_u:{u['user_id']}")])

    rows.append([InlineKeyboardButton(text="⬅️ Orqaga", callback_data="adm_back")])
    await safe_edit_or_send(
        callback,
        "👥 Foydalanuvchilar ro'yxati (Oxirgi faollar):\n"
        "Foydalanuvchining ma'lumotlari, 72 soatlik yozishmalari va boshqaruv tugmalari uchun ustiga bosing:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows)
    )


async def _render_user_profile_view(callback: CallbackQuery, target_uid: int):
    u = db.get_user(target_uid)
    if not u:
        await safe_edit_or_send(
            callback,
            "⚠️ Foydalanuvchi topilmadi.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Orqaga", callback_data="adm_users:0")]])
        )
        return

    paused, rem_sec = db.is_ai_paused(target_uid)
    pause_status = f"⏸ AI Pauzada ({rem_sec // 60} daq {rem_sec % 60}s qoldi)" if paused else "▶️ AI Faol"
    uname = f"@{u['username']}" if u["username"] else "yo'q"
    vip_rule = u["vip_instruction"] or "Yo'q (umumiy rejimda)"

    # 1ms ichida lokal bazadan o'qiymiz (tugma qotib qolmasligi uchun)
    logs = db.get_local_chat_logs(target_uid, limit=15)

    chat_lines = []
    for item in logs:
        s_icon = "👤 User" if item["sender"] == "user" else ("👑 Admin" if item["sender"] == "admin" else "🤖 AI")
        t_short = item["time"][11:16] if len(item["time"]) >= 16 else item["time"]
        chat_lines.append(f"[{t_short}] {s_icon}: {item['text']}")

    chat_history_str = "\n".join(chat_lines) if chat_lines else "Hozircha yozishmalar yo'q."
    if len(chat_history_str) > 2400:
        chat_history_str = "...\n" + chat_history_str[-2400:]

    text = (
        f"👤 Foydalanuvchi profili:\n"
        f"• Ism-familiyasi (tanishuv): {u['custom_name'] or 'Kiritmagan'}\n"
        f"• Telegram ismi: {u['first_name']} {u['last_name']}\n"
        f"• Username: {uname} | ID: {u['user_id']}\n"
        f"• Yoshi: {u['age'] or 'Kiritmagan'}\n"
        f"• Kirgan vaqti: {u['joined_at']}\n"
        f"• Oxirgi faollik: {u['last_active']}\n"
        f"• Holat: {pause_status}\n"
        f"• 👑 Maxsus muomala: {vip_rule}\n\n"
        f"💬 Oxirgi yozishmalar (72 soatlik xotira):\n"
        f"────────────────────\n"
        f"{chat_history_str}"
    )

    kb_rows = [
        [InlineKeyboardButton(text="✍️ Bot nomidan yozish (10m pauza)", callback_data=f"adm_takeover:{target_uid}")],
        [
            InlineKeyboardButton(text="👑 Maxsus muomala berish", callback_data=f"adm_vip_u:{target_uid}"),
            InlineKeyboardButton(text="🔄 Chatni yangilash", callback_data=f"adm_view_u:{target_uid}")
        ]
    ]
    if paused:
        kb_rows.append([InlineKeyboardButton(text="🤖 AI ni darhol yoqish", callback_data=f"adm_resume_ai:{target_uid}")])
    kb_rows.append([InlineKeyboardButton(text="⬅️ Foydalanuvchilar ro'yxatiga", callback_data="adm_users:0")])

    await safe_edit_or_send(callback, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows))


@dp.callback_query(F.data.startswith("adm_view_u:"))
async def cb_adm_view_user(callback: CallbackQuery):
    if not is_admin_user(callback.from_user.username, callback.from_user.id):
        await callback.answer("⛔ Faqat admin uchun!", show_alert=True)
        return
    await callback.answer()
    admin_action_state.pop(callback.from_user.id, None)
    target_uid = int(callback.data.split(":", 1)[1])
    await _render_user_profile_view(callback, target_uid)


@dp.callback_query(F.data.startswith("adm_takeover:"))
async def cb_adm_takeover(callback: CallbackQuery):
    if not is_admin_user(callback.from_user.username, callback.from_user.id):
        await callback.answer("⛔ Faqat admin uchun!", show_alert=True)
        return
    target_uid = int(callback.data.split(":", 1)[1])
    u = db.get_user(target_uid)
    name = (u["custom_name"] or u["first_name"]) if u else str(target_uid)

    db.pause_ai_for_user(target_uid, minutes=10)
    admin_action_state[callback.from_user.id] = {
        "action": "takeover_reply",
        "target_id": target_uid
    }
    await callback.answer("⏸ AI 10 daqiqaga to'xtatildi!")

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🤖 Suhbatdan chiqish va AI ni yoqish", callback_data=f"adm_resume_ai:{target_uid}")],
        [InlineKeyboardButton(text="⬅️ Foydalanuvchi profiliga qaytish", callback_data=f"adm_view_u:{target_uid}")]
    ])
    await safe_edit_or_send(
        callback,
        f"🕵️‍♂️ Jonli suhbat rejimi (Live Takeover) yoqildi!\n"
        f"👤 Foydalanuvchi: {name} (ID: {target_uid})\n\n"
        f"⏸ Ushbu foydalanuvchi uchun AI 10 daqiqaga jim bo'ldi.\n"
        f"Endi shu yerga nimani yozsangiz, foydalanuvchiga bot nomidan boradi va uning javoblari to'g'ridan-to'g'ri sizga keladi.\n\n"
        f"✍️ Xabaringizni yozib yuboring:",
        reply_markup=kb
    )


@dp.callback_query(F.data.startswith("adm_resume_ai:"))
async def cb_adm_resume_ai(callback: CallbackQuery):
    if not is_admin_user(callback.from_user.username, callback.from_user.id):
        await callback.answer("⛔ Faqat admin uchun!", show_alert=True)
        return
    target_uid = int(callback.data.split(":", 1)[1])
    db.resume_ai_for_user(target_uid)
    admin_action_state.pop(callback.from_user.id, None)
    await callback.answer("🤖 AI ushbu foydalanuvchi uchun qayta yoqildi!", show_alert=True)
    await _render_user_profile_view(callback, target_uid)


@dp.callback_query(F.data.startswith("adm_vip_u:"))
async def cb_adm_vip_user(callback: CallbackQuery):
    if not is_admin_user(callback.from_user.username, callback.from_user.id):
        await callback.answer("⛔ Faqat admin uchun!", show_alert=True)
        return
    await callback.answer()
    target_uid = int(callback.data.split(":", 1)[1])
    u = db.get_user(target_uid)
    name = (u["custom_name"] or u["first_name"]) if u else str(target_uid)

    admin_action_state[callback.from_user.id] = {
        "action": "set_vip_uid",
        "target_id": target_uid
    }
    await safe_edit_or_send(
        callback,
        f"👑 {name} uchun maxsus muomala turini tugmadan tanlang yoki o'zingiz xohlagan yo'riqnomani yozib yuboring:",
        reply_markup=get_vip_presets_keyboard(f"uid:{target_uid}", back_cb=f"adm_view_u:{target_uid}")
    )


@dp.callback_query(F.data.startswith("vip_pre:"))
async def cb_vip_preset_apply(callback: CallbackQuery):
    if not is_admin_user(callback.from_user.username, callback.from_user.id):
        await callback.answer("⛔ Faqat admin uchun!", show_alert=True)
        return

    # Format: vip_pre:<preset_key>:<uid|uname>:<val>
    parts = callback.data.split(":", 3)
    if len(parts) < 4:
        await callback.answer("Xato ma'lumot")
        return

    preset_key, ref_type, ref_val = parts[1], parts[2], parts[3]
    rule_text = "" if preset_key == "clear" else VIP_PRESETS.get(preset_key, "")

    admin_action_state.pop(callback.from_user.id, None)

    if ref_type == "uid":
        target_uid = int(ref_val)
        db.set_vip_by_user_id(target_uid, rule_text)
        msg = "🗑 VIP qoida o'chirildi!" if preset_key == "clear" else "✅ Maxsus VIP muomala o'rnatildi!"
        await callback.answer(msg, show_alert=True)
        await _render_user_profile_view(callback, target_uid)
    else:
        target_uname = ref_val
        db.set_vip_by_username(target_uname, rule_text)
        msg = f"🗑 @{target_uname} uchun VIP qoida o'chirildi!" if preset_key == "clear" else f"✅ @{target_uname} uchun VIP muomala saqlandi!"
        await callback.answer(msg, show_alert=True)
        await safe_edit_or_send(callback, f"{msg}\n\n{_build_admin_dashboard_text()}", reply_markup=get_admin_panel_keyboard())


@dp.callback_query(F.data == "adm_add_vip")
async def cb_adm_add_vip_by_link(callback: CallbackQuery):
    if not is_admin_user(callback.from_user.username, callback.from_user.id):
        await callback.answer("⛔ Faqat admin uchun!", show_alert=True)
        return
    await callback.answer()
    admin_action_state[callback.from_user.id] = {"action": "set_vip_uname"}
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Bekor qilish", callback_data="adm_back")]
    ])
    await safe_edit_or_send(
        callback,
        "👑 Foydalanuvchi linki yoki @username orqali maxsus muomala belgilash:\n\n"
        "Menga foydalanuvchining @username yoki Telegram linkini yuboring:\n"
        "• Masalan: `@ali_01` yoki `https://t.me/ali_01`\n"
        "• Yoki birdaniga qoidasi bilan: `@ali_01 — Boss ruxsat bermagan deb hurmat qil`",
        reply_markup=kb
    )


@dp.callback_query(F.data == "adm_vip_list")
async def cb_adm_vip_list(callback: CallbackQuery):
    if not is_admin_user(callback.from_user.username, callback.from_user.id):
        await callback.answer("⛔ Faqat admin uchun!", show_alert=True)
        return
    await callback.answer()
    rules = db.get_all_vip_rules()
    lines = []
    for idx, r in enumerate(rules, 1):
        lines.append(f"{idx}. @{r['username']} -> {r['instruction']}")

    text = "📋 Belgilangan VIP / Maxsus qoidalar ro'yxati:\n\n" + ("\n\n".join(lines) if lines else "Hozircha VIP qoidalar yo'q.")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Yangi VIP qo'shish", callback_data="adm_add_vip")],
        [InlineKeyboardButton(text="⬅️ Orqaga", callback_data="adm_back")]
    ])
    await safe_edit_or_send(callback, text[:4000], reply_markup=kb)


@dp.callback_query(F.data == "adm_custom_global")
async def cb_adm_custom_global(callback: CallbackQuery):
    if not is_admin_user(callback.from_user.username, callback.from_user.id):
        await callback.answer("⛔ Faqat admin uchun!", show_alert=True)
        return
    await callback.answer()
    admin_action_state[callback.from_user.id] = {"action": "set_custom_global"}
    current = db.get_setting("custom_global_prompt", "") or "Yo'q"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗑 Tozalash (O'chirish)", callback_data="adm_clear_global")],
        [InlineKeyboardButton(text="⬅️ Bekor qilish", callback_data="adm_back")]
    ])
    await safe_edit_or_send(
        callback,
        f"✍️ Global AI uchun qo'shimcha yo'riqnoma yozing.\n"
        f"Hozirgi qoida: {current}\n\n"
        f"Bu yerga yozgan qoidangiz barcha foydalanuvchilar bilan suhbatda AI ga qo'shimcha buyruq sifatida beriladi:",
        reply_markup=kb
    )


@dp.callback_query(F.data == "adm_clear_global")
async def cb_adm_clear_global(callback: CallbackQuery):
    if not is_admin_user(callback.from_user.username, callback.from_user.id):
        await callback.answer("⛔ Faqat admin uchun!", show_alert=True)
        return
    db.set_setting("custom_global_prompt", "")
    admin_action_state.pop(callback.from_user.id, None)
    await callback.answer("✅ Global qo'shimcha qoida tozalandi!", show_alert=True)
    await safe_edit_or_send(callback, _build_admin_dashboard_text(), reply_markup=get_admin_panel_keyboard())


# --- FOYDALANUVCHI BO'LIMLARI VA TANISHUV ---

@dp.message(F.text == "⬅️ Asosiy menyu")
async def back_to_main_menu(message: Message):
    user = message.from_user
    admin_action_state.pop(user.id, None)
    db.set_user_mode(user.id, "chat")
    is_adm = is_admin_user(user.username, user.id)
    await message.answer("Asosiy menyu:", reply_markup=get_main_keyboard(is_adm))


@dp.message(F.text == "🔄 Suhbatni yangilash")
async def reset_user_chat_profile(message: Message):
    user = message.from_user
    admin_action_state.pop(user.id, None)
    chat_history.pop(user.id, None)
    db.set_user_mode(user.id, "chat")
    db.set_chat_state(user.id, "ask_name", custom_name="", age="")
    is_adm = is_admin_user(user.username, user.id)
    sent = await message.answer(
        "🔄 Suhbat va tanishuv ma'lumotlari yangilandi!\n\n"
        "Ism va familiyangizni kiriting:",
        reply_markup=get_chat_keyboard(is_adm)
    )
    cleanup_ids[user.id] = [message.message_id, sent.message_id]


@dp.message(F.text == "📝 Ovozni matn qilish (15s)")
async def select_voice_to_text_mode(message: Message):
    user = message.from_user
    admin_action_state.pop(user.id, None)
    db.add_or_update_user(user.id, user.username, user.first_name, user.last_name)
    db.set_user_mode(user.id, "voice_to_text")
    is_adm = is_admin_user(user.username, user.id)
    await message.answer(
        "📝 «Ovozni matn qilish» bo'limi tanlandi!\n\n"
        "Menga 15 soniyagacha bo'lgan ovozli xabar (voice) yuboring. "
        "O'zbek, Rus, Ingliz va boshqa tillardagi nutqni darhol aniq matnga aylantirib beraman!",
        reply_markup=get_main_keyboard(is_adm)
    )


@dp.message(F.text == "💬 AI Suhbat")
async def select_ai_chat_mode(message: Message):
    user = message.from_user
    admin_action_state.pop(user.id, None)
    is_new = db.add_or_update_user(user.id, user.username, user.first_name, user.last_name)
    if is_new:
        asyncio.create_task(notify_admins_new_user(user))

    db.set_user_mode(user.id, "chat")
    state = db.get_chat_state(user.id)
    is_adm = is_admin_user(user.username, user.id)

    if state["name"] and state["age"]:
        db.set_chat_state(user.id, "chatting")
        await message.answer(
            f"💬 Qani {state['name']}, suhbatdamiz! Nima yangiliklar bor, yozaver 😊",
            reply_markup=get_chat_keyboard(is_adm)
        )
    else:
        db.set_chat_state(user.id, "ask_name")
        sent = await message.answer(
            "💬 AI Suhbatga xush kelibsiz!\n\n"
            "Keling, avval tanishib olaylik. Ism va familiyangiz nima?",
            reply_markup=get_chat_keyboard(is_adm)
        )
        cleanup_ids[user.id] = [message.message_id, sent.message_id]


# --- OVOZLI XABARLAR (Ovozni Matnga Aylantirish) ---

@dp.message(F.voice)
async def handle_voice_message(message: Message):
    voice = message.voice
    if not voice:
        return

    user = message.from_user
    is_new = db.add_or_update_user(user.id, user.username, user.first_name, user.last_name)
    if is_new:
        asyncio.create_task(notify_admins_new_user(user))

    if voice.duration and voice.duration > MAX_VOICE_DURATION:
        await message.reply(
            f"⚠️ Ovozli xabar juda uzun ({voice.duration} soniya)!\n"
            f"Iltimos, maksimal {MAX_VOICE_DURATION} soniyagacha bo'lgan ovoz yuboring."
        )
        return

    task_id = uuid.uuid4().hex
    input_file = TEMP_DIR / f"in_{task_id}.ogg"
    mp3_file = TEMP_DIR / f"whisper_{task_id}.mp3"

    try:
        file_info = await bot.get_file(voice.file_id)
        if not file_info.file_path:
            await message.reply("Faylni yuklab bo'lmadi.")
            return

        await bot.download_file(file_info.file_path, destination=input_file)
        await convert_to_mp3(input_file, mp3_file)
        transcription = await asyncio.to_thread(transcribe_with_groq_whisper, mp3_file)
        db.log_request(user.id, "voice_to_text")

        clean_text = transcription.replace("`", "'")
        response_text = (
            "📝 Ovozdagi matn:\n\n"
            f"`{clean_text}`\n\n"
            "👆 Matn ustiga bosing, avtomatik nusxalanadi!"
        )
        await message.reply(response_text, parse_mode="Markdown")

    except Exception as e:
        logger.exception(f"Ovozni matnga o'girish xatosi: {e}")
        await message.reply("❌ Ovozni o'qishda xatolik yuz berdi. Qaytadan urinib ko'ring.")
    finally:
        for f in (input_file, mp3_file):
            if f.exists():
                try:
                    f.unlink()
                except OSError:
                    pass


# --- MATNLI XABARLAR, ADMIN BUYRUQLARI VA JONLI SUHBAT ---

@dp.message(F.text)
async def handle_text_message(message: Message):
    user = message.from_user
    text = message.text.strip()
    is_adm = is_admin_user(user.username, user.id)
    is_new = db.add_or_update_user(user.id, user.username, user.first_name, user.last_name)
    if is_new:
        asyncio.create_task(notify_admins_new_user(user))

    # 1. Agar yozayotgan odam ADMIN bo'lsa va maxsus amal (Takeover / VIP kiritish) rejimida bo'lsa:
    if is_adm and user.id in admin_action_state:
        adm_state = admin_action_state[user.id]
        action = adm_state.get("action")

        if action == "takeover_reply":
            target_uid = adm_state["target_id"]
            db.pause_ai_for_user(target_uid, minutes=10)
            try:
                await bot.send_message(target_uid, text)
                await record_conversation(target_uid, "admin", text)
                hist = chat_history.setdefault(target_uid, deque(maxlen=HISTORY_LIMIT))
                hist.append({"role": "assistant", "content": text})

                kb = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="🤖 Suhbatdan chiqish va AI ni yoqish", callback_data=f"adm_resume_ai:{target_uid}")],
                    [InlineKeyboardButton(text="👤 Profil va chatni ko'rish", callback_data=f"adm_view_u:{target_uid}")]
                ])
                await message.reply("✅ Xabaringiz foydalanuvchiga bot nomidan yuborildi! (AI 10 daqiqa jim)", reply_markup=kb)
            except Exception as e:
                await message.reply(f"❌ Xabar yuborishda xato: {e}")
            return

        elif action == "set_vip_uid":
            target_uid = adm_state["target_id"]
            db.set_vip_by_user_id(target_uid, text)
            admin_action_state.pop(user.id, None)
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="👤 Foydalanuvchi profiliga qaytish", callback_data=f"adm_view_u:{target_uid}")]
            ])
            await message.reply(f"✅ Maxsus muomala qoidasi saqlandi!\n📌 Qoida: «{text}»", reply_markup=kb)
            return

        elif action == "set_vip_uname":
            # 1) Agar faqat @username yoki https://t.me/username yuborgan bo'lsa -> 2-qadamga (tayyor tugmalar + matn) o'tkazamiz!
            m_only = re.match(r"^(?:https?://t\.me/|@)?([a-zA-Z0-9_]{3,32})/?$", text)
            if m_only:
                target_uname = m_only.group(1)
                admin_action_state[user.id] = {
                    "action": "set_vip_uname_rule",
                    "target_uname": target_uname
                }
                await message.reply(
                    f"👑 @{target_uname} qabul qilindi!\n\n"
                    f"Endi u bilan qanday muomala qilishni pastdagi tayyor tugmalardan tanlang yoki o'zingiz matn yozib yuboring:",
                    reply_markup=get_vip_presets_keyboard(f"uname:{target_uname}", back_cb="adm_back")
                )
                return

            # 2) Agar birdaniga "@username — qoida" ko'rinishida yuborgan bo'lsa:
            m_both = re.match(r"^(?:https?://t\.me/|@)?([a-zA-Z0-9_]{3,32})\s+(?:[-—:]\s*)?(.+)$", text, flags=re.S)
            if m_both:
                target_uname, rule_text = m_both.group(1), m_both.group(2).strip()
                found_uid = db.set_vip_by_username(target_uname, rule_text)
                admin_action_state.pop(user.id, None)
                status_note = "Foydalanuvchi bazada topildi va darhol yangilandi!" if found_uid else "Foydalanuvchi botga kirishi bilan ushbu qoida avtomatik ishlaydi!"
                await message.reply(
                    f"✅ @{target_uname} uchun maxsus muomala saqlandi!\n"
                    f"📌 Qoida: «{rule_text}»\n"
                    f"ℹ️ {status_note}",
                    reply_markup=get_admin_panel_keyboard()
                )
                return

            await message.reply(
                "⚠️ Username formati tushunarsiz bo'ldi. Iltimos, `@username` yoki `https://t.me/username` yuboring:"
            )
            return

        elif action == "set_vip_uname_rule":
            target_uname = adm_state["target_uname"]
            found_uid = db.set_vip_by_username(target_uname, text)
            admin_action_state.pop(user.id, None)
            status_note = "Foydalanuvchi bazada topildi va darhol yangilandi!" if found_uid else "Foydalanuvchi botga kirishi bilan ushbu qoida avtomatik ishlaydi!"
            await message.reply(
                f"✅ @{target_uname} uchun maxsus muomala saqlandi!\n"
                f"📌 Qoida: «{text}»\n"
                f"ℹ️ {status_note}",
                reply_markup=get_admin_panel_keyboard()
            )
            return

        elif action == "set_custom_global":
            db.set_setting("custom_global_prompt", text)
            admin_action_state.pop(user.id, None)
            await message.reply(
                f"✅ Global AI yo'riqnomasi saqlandi:\n«{text}»",
                reply_markup=get_admin_panel_keyboard()
            )
            return

    # 2. Tanishuv bosqichlari (Alohida-alohida: avval Ism-familiya, keyin Yosh)
    state = db.get_chat_state(user.id)
    step = state["step"]

    if step == "ask_name":
        clean_name = " ".join(text.split()[:4]).strip()
        if len(clean_name) < 2 or clean_name.isdigit():
            await message.answer("Iltimos, ism-familiyangizni to'g'ri yozib yuboring (masalan: Ozodbek Raximjonov):")
            return

        db.set_chat_state(user.id, "ask_age", custom_name=clean_name)
        sent = await message.answer(
            f"Tanishganimdan xursandman, {clean_name}! 🤝\n"
            f"Endi yoshingizni kiriting (masalan: 19):"
        )
        cleanup_ids.setdefault(user.id, []).extend([message.message_id, sent.message_id])
        return

    elif step == "ask_age":
        m = re.search(r"\d{1,2}", text)
        if not m:
            await message.answer("Iltimos, yoshingizni raqam bilan yozing (masalan: 19):")
            return

        age = m.group(0)
        custom_name = state["name"] or user.first_name
        db.set_chat_state(user.id, "chatting", custom_name=custom_name, age=age)
        chat_history.pop(user.id, None)

        ids_to_del = cleanup_ids.pop(user.id, []) + [message.message_id]
        asyncio.create_task(safe_delete(message.chat.id, ids_to_del))

        await bot.send_chat_action(message.chat.id, "typing")
        intro_prompt = f"Salom! Mening ismim {custom_name}, yoshim {age} da. Meni o'z uslubingda qisqa va samimiy kutib ol!"
        first_reply = await asyncio.to_thread(generate_ai_reply, user.id, intro_prompt)
        db.log_request(user.id, "ai_chat")
        await record_conversation(user.id, "ai", first_reply)

        await message.answer(first_reply, reply_markup=get_chat_keyboard(is_adm))
        return

    # 3. Agar foydalanuvchi hali tanishmagan bo'lsa — tanishuvni boshlaymiz
    if not state["name"] or not state["age"]:
        db.set_user_mode(user.id, "chat")
        db.set_chat_state(user.id, "ask_name")
        sent = await message.answer(
            "Suhbatni boshlashdan oldin yaqindan tanishib olaylik 😊\n"
            "Ism va familiyangiz nima?",
            reply_markup=get_chat_keyboard(is_adm)
        )
        cleanup_ids[user.id] = [sent.message_id]
        return

    # 4. Suhbat davomida foydalanuvchi o'zi haqida muhim ma'lumot aytsa, eslab qolish
    if any(k in text.lower() for k in ["o'qiyman", "ishlayman", "yashayman", "qiziqaman", "kasbim", "universitet", "maktab"]):
        db.update_user_memory(user.id, text[:120])

    # 5. Suhbatni logga yozish (72 soatlik xotira)
    await record_conversation(user.id, "user", text)
    db.log_request(user.id, "ai_chat")

    # 6. LIVE TAKEOVER TEKSHIRUVI: Agar Admin ushbu suhbatga aralashgan bo'lsa (10 daqiqa ichida):
    paused, rem_sec = db.is_ai_paused(user.id)
    if paused:
        admin_ids = db.get_admin_chat_ids(ADMIN_USERNAMES, ADMIN_IDS)
        uname = f"@{user.username}" if user.username else "yo'q"
        alert_text = (
            f"📩 Live Chat ({state['name']} | {uname}):\n\n"
            f"«{text}»\n\n"
            f"⏱ AI hali {rem_sec // 60} daq {rem_sec % 60}s jim turadi."
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✍️ Javob yozish", callback_data=f"adm_takeover:{user.id}")],
            [InlineKeyboardButton(text="🤖 AI ni darhol yoqish", callback_data=f"adm_resume_ai:{user.id}")]
        ])
        for aid in admin_ids:
            if aid != user.id:
                try:
                    await bot.send_message(aid, alert_text, reply_markup=kb)
                except Exception:
                    pass
        return

    # 7. AI javobini yaratish va yuborish
    await bot.send_chat_action(message.chat.id, "typing")
    reply = await asyncio.to_thread(generate_ai_reply, user.id, text)
    await record_conversation(user.id, "ai", reply)
    await message.answer(reply, reply_markup=get_chat_keyboard(is_adm))


# --- Server va Webhook ---

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
        bot=bot
    )
    webhook_requests_handler.register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)

    logger.info(f"Server 0.0.0.0:{port} portida ishga tushmoqda...")
    web.run_app(app, host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
