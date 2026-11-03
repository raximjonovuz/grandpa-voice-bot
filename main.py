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

from aiogram import Bot, Dispatcher, F
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
from dotenv import load_dotenv

import db
import cf_storage

# Log sozlamalari
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - [%(levelname)s] - %(name)s - %(message)s"
)
logger = logging.getLogger("SmartAIChatBot")

load_dotenv()
raw_token = os.getenv("BOT_TOKEN") or os.getenv("bottoken")
if not raw_token:
    raise ValueError("BOT_TOKEN topilmadi! Iltimos .env faylida BOT_TOKEN ni ko'rsating.")

BOT_TOKEN = raw_token.split("//")[0].split("#")[0].strip().strip('"').strip("'")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
GROQ_CHAT_KEY = os.getenv("GROQ_CHAT_KEY", "").strip() or GROQ_API_KEY
CHAT_MODEL = os.getenv("CHAT_MODEL", "openai/gpt-oss-120b").strip()

# Ikki rasmiy Admin profili
ADMIN_USERNAMES = ["o_raximjonov", "raximjonov_admin", "o_raximjonovuz"]

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
# admin_id -> {"action": "takeover_reply" | "set_vip_uid" | "set_vip_uname" | "set_custom_global", "target_id": int}
admin_action_state: dict[int, dict] = {}


def is_admin_user(username: Optional[str], user_id: int) -> bool:
    uname = (username or "").lower().strip("@").strip()
    return uname in ADMIN_USERNAMES


# --- Klaviaturalar ---

def get_main_keyboard() -> ReplyKeyboardMarkup:
    """Asosiy menyu: faqat AI Suhbat va Ovozni matn qilish."""
    kb = [
        [
            KeyboardButton(text="💬 AI Suhbat"),
            KeyboardButton(text="📝 Ovozni matn qilish (15s)")
        ]
    ]
    return ReplyKeyboardMarkup(
        keyboard=kb,
        resize_keyboard=True,
        input_field_placeholder="Bo'limni tanlang yoki xabar yozing..."
    )


def get_chat_keyboard() -> ReplyKeyboardMarkup:
    """AI suhbat paytida toza menyu tugmasi."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="⬅️ Asosiy menyu"),
                KeyboardButton(text="🔄 Suhbatni yangilash")
            ]
        ],
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


async def set_bot_commands(bot: Bot):
    commands = [
        BotCommand(command="start", description="Botni ishga tushirish"),
        BotCommand(command="help", description="Yordam va qo'llanma")
    ]
    await bot.set_my_commands(commands)


# --- Audio / Groq Whisper (Voice-to-Text) ---

async def convert_to_mp3(input_path: Path, output_mp3: Path) -> None:
    async with processing_semaphore:
        cmd = [
            "ffmpeg", "-y",
            "-i", str(input_path),
            "-ar", "16000", "-ac", "1", "-b:a", "32k",
            str(output_mp3)
        ]
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        await process.communicate()


def transcribe_with_groq_whisper(audio_file_path: Path) -> str:
    """Groq Whisper large-v3-turbo yordamida ko'p tilli matnga aylantirish."""
    url = "https://api.groq.com/openai/v1/audio/transcriptions"
    boundary = "----WebKitFormBoundaryGroqWhisper"

    with open(audio_file_path, "rb") as f:
        file_bytes = f.read()

    data_parts = [
        f"--{boundary}".encode(),
        b'Content-Disposition: form-data; name="model"\r\n',
        b"whisper-large-v3-turbo",
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
        with urllib.request.urlopen(req, timeout=20) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            return result.get("text", "").strip() or "Nutq aniqlanmadi."
    except Exception as e:
        logger.error(f"Groq Whisper xatosi: {e}")
        return "Ovozdagi nutqni aniqlashda xatolik yuz berdi."


# --- AI Xarakterlar va Prompt Motori ---

PERSONA_INSTRUCTIONS = {
    "kocha": (
        "USLUB: Toshkent va o'zbek yoshlarining samimiy 'ko'cha bola / jigar' uslubida gaplash. "
        "Yigitlarga 'jigar', 'brat', 'jo'ra', 'uka/aka'; qizlarga 'opa/singil' deb murojaat qil. "
        "Zamonaviy, jonli o'zbekcha iboralardan ('gap yo'q', 'bomba', 'tinchlikmi', 'qoyil') tabiiy foydalan. "
        "Har bir gapga avval mantiqiy javob berib, keyin o'rinli va samimiy hazil qo'sh."
    ),
    "faylasuf": (
        "USLUB: Dono faylasuf va hayotiy tajribaga ega ustozdek gaplash. "
        "Foydalanuvchining har bir gapidan chuqur ma'no top, hikmatli, o'ylantiradigan va aqlli fikrlar bildir. "
        "Ohanging bosiq, salobati baland va ma'noli bo'lsin."
    ),
    "sarkazm": (
        "USLUB: O'tkir tilli sarkazm va 'roast' ustasi. "
        "Foydalanuvchining gapiga aqlli qochirim, kulgili kinoya va yengil trolling bilan javob ber. "
        "Ammo mantiqni yo'qotma: avval u yozgan gapning ma'nosiga aniq javob qilib, ustidan chiroyli va kulgili hazil qil."
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
        f"Suhbatdoshing haqida ma'lumot: Ism-familiyasi: {name} | Yoshi: {age} | Username: @{uname}.\n"
    )
    if memory:
        base += f"Suhbatdosh haqida eslab qolingan faktlar: {memory}\n"

    # Agar Admindan ushbu foydalanuvchi uchun maxsus VIP muomala berilgan bo'lsa — u eng ustuvor!
    if vip_rule:
        base += (
            "\n⚠️ ADMIN (BOSS) TOMONIDAN USHBU SHAXS UCHUN MAXSUS QAT'IY BUYRUQ:\n"
            f"«{vip_rule}»\n"
            "Boshqa barcha hazil qoidalarini chetga surib, ushbu foydalanuvchi bilan AYNAN shu buyruq asosida gaplash! "
            "Agar Boss u bilan hazillashmaslikni va hurmat qilishni buyurgan bo'lsa, unga: "
            "'Siz bilan qaltis hazil qilolmayman, Boss bunga ruxsat bermagan, sizni qattiq hurmat qilamiz!' ruhida muloyim va hurmat bilan javob ber.\n"
        )
    else:
        base += f"\n{persona_text}\n"
        if custom_global:
            base += f"Qo'shimcha global qoida: {custom_global}\n"

    base += (
        "\nTIL VA MULOQOT QOIDALARI:\n"
        "1. Faqat TOZA, TABIIY O'ZBEK TILIDA (lotin alifbosida) gapir. "
        "So'zlarni buzib yozma ('xush kelibsening', 'senimdan', 'qilapsan' kabi g'aliz so'zlarni umuman ishlatma).\n"
        "2. Suhbatdoshning gapini hech qachon inkor qilma va ma'nosiz savol qaytarma. U nima yozgan bo'lsa, aynan o'sha mavzuga mantiqiy va jonli javob ber.\n"
        "3. Javobing qisqa va lo'nda bo'lsin (1-3 ta jumla). Uzun ma'ruza o'qima.\n"
        "4. Agar foydalanuvchi rus yoki ingliz tilida yozsa, o'sha tilda javob ber.\n\n"
        "TABIIY O'ZBEKCHA MULOQOT NAMUNALARI:\n"
        "- User: Kimsan, nimaga sansiraysan?\n"
        "  Javob: O'zimizning yaqin jigarimizsan-ku deb sansirab yubordim-da! Xafa bo'lma, xohlasang hozirdan 'Siz'ga o'tamiz 😎\n"
        "- User: Bugun bir fandan yiqildim\n"
        "  Javob: Eee, chatoq bo'libdi-ku! Qaysi fan edi o'zi, domla rosa qattiq oldimi deyman? 😅\n"
        "- User: Odamday gapir\n"
        "  Javob: Bo'ldi, gap yo'q, jiddiy to'lqinga o'tdik. Qanday mavzuda gaplashamiz?\n"
    )
    return base


def groq_chat(messages: list) -> str:
    url = "https://api.groq.com/openai/v1/chat/completions"
    models_to_try = [CHAT_MODEL, "openai/gpt-oss-120b", "qwen/qwen3.8-27b"]
    # Takrorlanishni olib tashlash
    unique_models = []
    for m in models_to_try:
        if m and m not in unique_models:
            unique_models.append(m)

    last_err = None
    for model_name in unique_models:
        payload = {
            "model": model_name,
            "messages": messages,
            "max_tokens": 220,
            "temperature": 0.65,
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

    # Agar xotira bo'sh bo'lsa, bazadagi oxirgi yozishmalardan yuklab olamiz (server qayta yonganda ham suhbat uzilmasligi uchun)
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
    """Suhbatni ham mahalliy bazaga, ham Cloudflare KV (72 soatlik xotira) ga yozish."""
    db.log_chat_message(user_id, sender, text)
    asyncio.create_task(asyncio.to_thread(cf_storage.append_kv_chat_log, user_id, sender, text))


async def notify_admins_new_user(user):
    """Yangi foydalanuvchi kirganda @O_raximjonov va @raximjonov_admin ga xabar berish."""
    admin_ids = db.get_admin_chat_ids(ADMIN_USERNAMES)
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


# --- Telegram Bot Handlerlari ---

@dp.message(CommandStart())
async def handle_start(message: Message):
    user = message.from_user
    is_new = db.add_or_update_user(user.id, user.username, user.first_name, user.last_name)

    if is_new:
        asyncio.create_task(notify_admins_new_user(user))

    user_info = db.get_user(user.id) or {}
    saved_name = user_info.get("custom_name")

    if is_admin_user(user.username, user.id):
        welcome = (
            f"Assalomu alaykum, hurmatli Admin ({user.first_name})! 👑\n\n"
            "Boshqaruv paneliga o'tish uchun: /admin\n\n"
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

    await message.answer(welcome, reply_markup=get_main_keyboard())


@dp.message(Command("help"))
async def handle_help(message: Message):
    user = message.from_user
    db.add_or_update_user(user.id, user.username, user.first_name, user.last_name)
    text = (
        "💡 Botdan foydalanish qo'llanmasi:\n\n"
        "1. 💬 AI Suhbat — Sizni eslab qoladigan, o'zbek tilida ravon va samimiy gaplashadigan AI suhbatdosh.\n"
        "2. 📝 Ovozni matn qilish (15s) — Istalgan tildagi (O'zbek, Rus, Ingliz, Turk va b.) ovozli xabarni bir zumda matnga aylantirib beradi.\n\n"
        "🔄 Agar ismingiz yoki yoshingizni qayta kiritmoqchi bo'lsangiz, suhbat ichida «🔄 Suhbatni yangilash» tugmasini bosing."
    )
    await message.answer(text, reply_markup=get_main_keyboard())


# --- ADMIN PANEL VA BOSHQARUV ---

@dp.message(Command("admin"))
async def handle_admin_cmd(message: Message):
    user = message.from_user
    db.add_or_update_user(user.id, user.username, user.first_name, user.last_name)

    if not is_admin_user(user.username, user.id):
        await message.answer("Noma'lum buyruq. Yordam uchun /help ni bosing.")
        return

    admin_action_state.pop(user.id, None)
    stats = db.get_stats()
    current_persona = db.get_setting("ai_persona", "kocha")
    custom_global = db.get_setting("custom_global_prompt", "") or "Yo'q"

    text = (
        "👑 Admin Boshqaruv Paneli\n\n"
        f"👥 Jami foydalanuvchilar: {stats['total_users']} ta\n"
        f"🆕 Bugun qo'shilganlar: {stats['today_new_users']} ta\n"
        f"🔥 Bugun faol bo'lganlar: {stats['today_active_users']} ta\n"
        f"⚡ Bugungi so'rovlar: {stats['today_requests']} ta (Jami: {stats['total_requests']} ta)\n\n"
        f"🎭 Joriy AI Rejimi: {current_persona.upper()}\n"
        f"📌 Qo'shimcha global qoida: {custom_global}"
    )
    await message.answer(text, reply_markup=get_admin_panel_keyboard())


@dp.callback_query(F.data == "adm_back")
async def cb_adm_back(callback: CallbackQuery):
    if not is_admin_user(callback.from_user.username, callback.from_user.id):
        return
    admin_action_state.pop(callback.from_user.id, None)
    stats = db.get_stats()
    current_persona = db.get_setting("ai_persona", "kocha")
    custom_global = db.get_setting("custom_global_prompt", "") or "Yo'q"

    text = (
        "👑 Admin Boshqaruv Paneli\n\n"
        f"👥 Jami foydalanuvchilar: {stats['total_users']} ta\n"
        f"🆕 Bugun qo'shilganlar: {stats['today_new_users']} ta\n"
        f"🔥 Bugun faol bo'lganlar: {stats['today_active_users']} ta\n"
        f"⚡ Bugungi so'rovlar: {stats['today_requests']} ta (Jami: {stats['total_requests']} ta)\n\n"
        f"🎭 Joriy AI Rejimi: {current_persona.upper()}\n"
        f"📌 Qo'shimcha global qoida: {custom_global}"
    )
    await callback.message.edit_text(text, reply_markup=get_admin_panel_keyboard())
    await callback.answer()


@dp.callback_query(F.data == "adm_stats")
async def cb_adm_stats(callback: CallbackQuery):
    if not is_admin_user(callback.from_user.username, callback.from_user.id):
        return
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
    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()


@dp.callback_query(F.data == "adm_personas")
async def cb_adm_personas(callback: CallbackQuery):
    if not is_admin_user(callback.from_user.username, callback.from_user.id):
        return
    await callback.message.edit_text(
        "🎭 Botning global suhbat rejimini (xarakterini) tanlang:\n\n"
        "Tanlangan rejim barcha oddiy foydalanuvchilar uchun darhol kuchga kiradi (VIP qoida berilgan shaxslardan tashqari):",
        reply_markup=get_persona_selection_keyboard()
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("set_persona:"))
async def cb_set_persona(callback: CallbackQuery):
    if not is_admin_user(callback.from_user.username, callback.from_user.id):
        return
    mode_key = callback.data.split(":", 1)[1]
    db.set_setting("ai_persona", mode_key)
    await callback.answer(f"✅ AI rejimi '{mode_key}' ga o'zgartirildi!")
    await callback.message.edit_text(
        "🎭 Botning global suhbat rejimini (xarakterini) tanlang:",
        reply_markup=get_persona_selection_keyboard()
    )


@dp.callback_query(F.data.startswith("adm_users:"))
async def cb_adm_users(callback: CallbackQuery):
    if not is_admin_user(callback.from_user.username, callback.from_user.id):
        return
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
    await callback.message.edit_text(
        "👥 Foydalanuvchilar ro'yxati (Oxirgi faollar):\n"
        "Foydalanuvchining ma'lumotlari va yozishmalarini ko'rish uchun ustiga bosing:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows)
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("adm_view_u:"))
async def cb_adm_view_user(callback: CallbackQuery):
    if not is_admin_user(callback.from_user.username, callback.from_user.id):
        return
    target_uid = int(callback.data.split(":", 1)[1])
    u = db.get_user(target_uid)
    if not u:
        await callback.answer("Foydalanuvchi topilmadi!", show_alert=True)
        return

    paused, rem_sec = db.is_ai_paused(target_uid)
    pause_status = f"⏸ AI Pauzada ({rem_sec // 60} daq {rem_sec % 60} soniya qoldi)" if paused else "▶️ AI Faol"
    uname = f"@{u['username']}" if u["username"] else "yo'q"
    vip_rule = u["vip_instruction"] or "Yo'q (umumiy rejimda)"

    # Suhbat tarixini olish (avval lokal, bo'sh bo'lsa Cloudflare KV dan)
    logs = db.get_local_chat_logs(target_uid, limit=15)
    if not logs:
        logs = await asyncio.to_thread(cf_storage.get_kv_chat_logs, target_uid)
        logs = logs[-15:]

    chat_lines = []
    for item in logs:
        s_icon = "👤 User" if item["sender"] == "user" else ("👑 Admin" if item["sender"] == "admin" else "🤖 AI")
        t_short = item["time"][11:16] if len(item["time"]) >= 16 else item["time"]
        chat_lines.append(f"[{t_short}] {s_icon}: {item['text']}")

    chat_history_str = "\n".join(chat_lines) if chat_lines else "Hozircha yozishmalar yo'q."
    if len(chat_history_str) > 2500:
        chat_history_str = "...\n" + chat_history_str[-2500:]

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
        [
            InlineKeyboardButton(text="✍️ Bot nomidan yozish (10m pauza)", callback_data=f"adm_takeover:{target_uid}"),
        ],
        [
            InlineKeyboardButton(text="👑 Maxsus muomala berish", callback_data=f"adm_vip_u:{target_uid}"),
            InlineKeyboardButton(text="🔄 Chatni yangilash", callback_data=f"adm_view_u:{target_uid}")
        ]
    ]
    if paused:
        kb_rows.append([InlineKeyboardButton(text="🤖 AI ni darhol yoqish", callback_data=f"adm_resume_ai:{target_uid}")])
    kb_rows.append([InlineKeyboardButton(text="⬅️ Foydalanuvchilar ro'yxatiga", callback_data="adm_users:0")])

    try:
        await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows))
    except Exception:
        await callback.message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows))
    await callback.answer()


@dp.callback_query(F.data.startswith("adm_takeover:"))
async def cb_adm_takeover(callback: CallbackQuery):
    if not is_admin_user(callback.from_user.username, callback.from_user.id):
        return
    target_uid = int(callback.data.split(":", 1)[1])
    u = db.get_user(target_uid)
    name = (u["custom_name"] or u["first_name"]) if u else str(target_uid)

    # AI ni darhol 10 daqiqaga pauza qilamiz
    db.pause_ai_for_user(target_uid, minutes=10)
    admin_action_state[callback.from_user.id] = {
        "action": "takeover_reply",
        "target_id": target_uid
    }

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🤖 Suhbatdan chiqish va AI ni yoqish", callback_data=f"adm_resume_ai:{target_uid}")],
        [InlineKeyboardButton(text="⬅️ Orqaga", callback_data=f"adm_view_u:{target_uid}")]
    ])
    await callback.message.answer(
        f"🕵️‍♂️ Jonli suhbat rejimi (Live Takeover) yoqildi!\n"
        f"Foydalanuvchi: {name} (ID: {target_uid})\n\n"
        f"⏸ Ushbu foydalanuvchi uchun AI 10 daqiqaga jim bo'ldi.\n"
        f"Endi shu yerga nimani yozsangiz, foydalanuvchiga bot nomidan boradi va uning javoblari sizga keladi.\n\n"
        f"Xabaringizni yozing:",
        reply_markup=kb
    )
    await callback.answer("AI 10 daqiqaga to'xtatildi!")


@dp.callback_query(F.data.startswith("adm_resume_ai:"))
async def cb_adm_resume_ai(callback: CallbackQuery):
    if not is_admin_user(callback.from_user.username, callback.from_user.id):
        return
    target_uid = int(callback.data.split(":", 1)[1])
    db.resume_ai_for_user(target_uid)
    admin_action_state.pop(callback.from_user.id, None)
    await callback.answer("🤖 AI ushbu foydalanuvchi uchun qayta yoqildi!", show_alert=True)
    await cb_adm_view_user(callback)


@dp.callback_query(F.data.startswith("adm_vip_u:"))
async def cb_adm_vip_user(callback: CallbackQuery):
    if not is_admin_user(callback.from_user.username, callback.from_user.id):
        return
    target_uid = int(callback.data.split(":", 1)[1])
    u = db.get_user(target_uid)
    name = (u["custom_name"] or u["first_name"]) if u else str(target_uid)

    admin_action_state[callback.from_user.id] = {
        "action": "set_vip_uid",
        "target_id": target_uid
    }
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗑 VIP qoidani o'chirish", callback_data=f"adm_clear_vip:{target_uid}")],
        [InlineKeyboardButton(text="⬅️ Bekor qilish", callback_data=f"adm_view_u:{target_uid}")]
    ])
    await callback.message.answer(
        f"👑 {name} uchun maxsus muomala yo'riqnomasini yozib yuboring.\n\n"
        f"Masalan:\n"
        f"«Bu kishi Bossning yaqin tanishi. U bilan umuman qaltis hazil qilma. 'Siz bilan hazil qilolmayman, Boss bunga ruxsat bermagan, sizni hurmat qilamiz' deb juda muloyim gaplash.»",
        reply_markup=kb
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("adm_clear_vip:"))
async def cb_adm_clear_vip(callback: CallbackQuery):
    if not is_admin_user(callback.from_user.username, callback.from_user.id):
        return
    target_uid = int(callback.data.split(":", 1)[1])
    db.set_vip_by_user_id(target_uid, "")
    admin_action_state.pop(callback.from_user.id, None)
    await callback.answer("✅ Maxsus qoida o'chirildi, umumiy rejimga qaytdi!", show_alert=True)
    await cb_adm_view_user(callback)


@dp.callback_query(F.data == "adm_add_vip")
async def cb_adm_add_vip_by_link(callback: CallbackQuery):
    if not is_admin_user(callback.from_user.username, callback.from_user.id):
        return
    admin_action_state[callback.from_user.id] = {"action": "set_vip_uname"}
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Bekor qilish", callback_data="adm_back")]
    ])
    await callback.message.edit_text(
        "👑 Foydalanuvchi linki yoki @username orqali maxsus muomala belgilash:\n\n"
        "Quyidagi formatda bitta xabar yuboring:\n"
        "`@username — qanday gaplashish kerakligi`\n"
        "yoki\n"
        "`https://t.me/username — qanday gaplashish kerakligi`\n\n"
        "Misol:\n"
        "`https://t.me/ali_01 — Bu mening do'stim, u bilan hazillashma, Boss ruxsat bermagan deb hurmat bilan gaplash`",
        reply_markup=kb
    )
    await callback.answer()


@dp.callback_query(F.data == "adm_vip_list")
async def cb_adm_vip_list(callback: CallbackQuery):
    if not is_admin_user(callback.from_user.username, callback.from_user.id):
        return
    rules = db.get_all_vip_rules()
    lines = []
    for idx, r in enumerate(rules, 1):
        lines.append(f"{idx}. @{r['username']} -> {r['instruction']}")

    text = "📋 Belgilangan VIP / Maxsus qoidalar ro'yxati:\n\n" + ("\n\n".join(lines) if lines else "Hozircha VIP qoidalar yo'q.")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Yangi VIP qo'shish", callback_data="adm_add_vip")],
        [InlineKeyboardButton(text="⬅️ Orqaga", callback_data="adm_back")]
    ])
    await callback.message.edit_text(text[:4000], reply_markup=kb)
    await callback.answer()


@dp.callback_query(F.data == "adm_custom_global")
async def cb_adm_custom_global(callback: CallbackQuery):
    if not is_admin_user(callback.from_user.username, callback.from_user.id):
        return
    admin_action_state[callback.from_user.id] = {"action": "set_custom_global"}
    current = db.get_setting("custom_global_prompt", "") or "Yo'q"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗑 Tozalash (O'chirish)", callback_data="adm_clear_global")],
        [InlineKeyboardButton(text="⬅️ Bekor qilish", callback_data="adm_back")]
    ])
    await callback.message.edit_text(
        f"✍️ Global AI uchun qo'shimcha yo'riqnoma yozing.\n"
        f"Hozirgi qoida: {current}\n\n"
        f"Bu yerga yozgan qoidangiz barcha foydalanuvchilar bilan suhbatda AI ga qo'shimcha buyruq sifatida beriladi:",
        reply_markup=kb
    )
    await callback.answer()


@dp.callback_query(F.data == "adm_clear_global")
async def cb_adm_clear_global(callback: CallbackQuery):
    if not is_admin_user(callback.from_user.username, callback.from_user.id):
        return
    db.set_setting("custom_global_prompt", "")
    admin_action_state.pop(callback.from_user.id, None)
    await callback.answer("✅ Global qo'shimcha qoida tozalandi!", show_alert=True)
    await cb_adm_back(callback)


# --- FOYDALANUVCHI BO'LIMLARI VA TANISHUV ---

@dp.message(F.text == "⬅️ Asosiy menyu")
async def back_to_main_menu(message: Message):
    user = message.from_user
    db.set_user_mode(user.id, "chat")
    await message.answer("Asosiy menyu:", reply_markup=get_main_keyboard())


@dp.message(F.text == "🔄 Suhbatni yangilash")
async def reset_user_chat_profile(message: Message):
    user = message.from_user
    chat_history.pop(user.id, None)
    db.set_user_mode(user.id, "chat")
    db.set_chat_state(user.id, "ask_name", custom_name="", age="")
    sent = await message.answer(
        "🔄 Suhbat va tanishuv ma'lumotlari yangilandi!\n\n"
        "Ism va familiyangizni kiriting:",
        reply_markup=get_chat_keyboard()
    )
    cleanup_ids[user.id] = [message.message_id, sent.message_id]


@dp.message(F.text == "📝 Ovozni matn qilish (15s)")
async def select_voice_to_text_mode(message: Message):
    user = message.from_user
    db.add_or_update_user(user.id, user.username, user.first_name, user.last_name)
    db.set_user_mode(user.id, "voice_to_text")
    await message.answer(
        "📝 «Ovozni matn qilish» bo'limi tanlandi!\n\n"
        "Menga 15 soniyagacha bo'lgan ovozli xabar (voice) yuboring. "
        "O'zbek, Rus, Ingliz, Turk va boshqa tillardagi nutqni darhol matnga aylantirib beraman!",
        reply_markup=get_main_keyboard()
    )


@dp.message(F.text == "💬 AI Suhbat")
async def select_ai_chat_mode(message: Message):
    user = message.from_user
    is_new = db.add_or_update_user(user.id, user.username, user.first_name, user.last_name)
    if is_new:
        asyncio.create_task(notify_admins_new_user(user))

    db.set_user_mode(user.id, "chat")
    state = db.get_chat_state(user.id)

    # Agar avval ism va yoshini kiritgan bo'lsa — qayta so'ramasdan darhol davom etadi!
    if state["name"] and state["age"]:
        db.set_chat_state(user.id, "chatting")
        await message.answer(
            f"💬 Qani {state['name']}, suhbatdamiz! Nima yangiliklar bor, yozaver 😊",
            reply_markup=get_chat_keyboard()
        )
    else:
        db.set_chat_state(user.id, "ask_name")
        sent = await message.answer(
            "💬 AI Suhbatga xush kelibsiz!\n\n"
            "Keling, avval tanishib olaylik. Ism va familiyangiz nima?",
            reply_markup=get_chat_keyboard()
        )
        cleanup_ids[user.id] = [message.message_id, sent.message_id]


# --- OVOZLI XABARLAR (Faqat Ovozni Matnga Aylantirish) ---

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
    is_new = db.add_or_update_user(user.id, user.username, user.first_name, user.last_name)
    if is_new:
        asyncio.create_task(notify_admins_new_user(user))

    # 1. Agar yozayotgan odam ADMIN bo'lsa va maxsus amal (Takeover / VIP kiritish) rejimida bo'lsa:
    if is_admin_user(user.username, user.id) and user.id in admin_action_state:
        adm_state = admin_action_state[user.id]
        action = adm_state.get("action")

        if action == "takeover_reply":
            target_uid = adm_state["target_id"]
            # AI pauzasini yana 10 daqiqaga yangilaymiz (admin yozishda davom etayotgani uchun)
            db.pause_ai_for_user(target_uid, minutes=10)
            try:
                await bot.send_message(target_uid, text)
                await record_conversation(target_uid, "admin", text)
                # AI kontekstiga ham qo'shib qo'yamiz, 10 daqiqadan keyin AI bilsin
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
            await message.reply(f"✅ Maxsus muomala qoidasi saqlandi!\nQoida: «{text}»", reply_markup=kb)
            return

        elif action == "set_vip_uname":
            # Format: @username — qoida yoki https://t.me/username - qoida
            m = re.match(r"^(?:https?://t\.me/|@)?([a-zA-Z0-9_]{3,32})\s*[-—:]?\s*(.+)$", text, flags=re.S)
            if not m:
                await message.reply(
                    "⚠️ Format noto'g'ri! Iltimos, bunday yozing:\n"
                    "`@username — qanday gaplashish kerakligi`",
                    parse_mode="Markdown"
                )
                return
            target_uname, rule_text = m.group(1), m.group(2).strip()
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

        # Tanishuvdagi oraliq xabarlarni tozalash
        ids_to_del = cleanup_ids.pop(user.id, []) + [message.message_id]
        asyncio.create_task(safe_delete(message.chat.id, ids_to_del))

        await bot.send_chat_action(message.chat.id, "typing")
        intro_prompt = f"Salom! Mening ismim {custom_name}, yoshim {age} da. Meni o'z uslubingda qisqa va samimiy kutib ol!"
        first_reply = await asyncio.to_thread(generate_ai_reply, user.id, intro_prompt)
        db.log_request(user.id, "ai_chat")
        await record_conversation(user.id, "ai", first_reply)

        await message.answer(first_reply, reply_markup=get_chat_keyboard())
        return

    # 3. Agar foydalanuvchi hali tanishmagan bo'lsa — tanishuvni boshlaymiz
    if not state["name"] or not state["age"]:
        db.set_user_mode(user.id, "chat")
        db.set_chat_state(user.id, "ask_name")
        sent = await message.answer(
            "Suhbatni boshlashdan oldin yaqindan tanishib olaylik 😊\n"
            "Ism va familiyangiz nima?",
            reply_markup=get_chat_keyboard()
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
        # AI jim turadi! Xabarni darhol Adminlarga yetkazamiz
        admin_ids = db.get_admin_chat_ids(ADMIN_USERNAMES)
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
    await message.answer(reply, reply_markup=get_chat_keyboard())


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
