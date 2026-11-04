import os
import re
from pathlib import Path
from typing import Optional
from dotenv import load_dotenv

load_dotenv()

raw_token = os.getenv("BOT_TOKEN") or os.getenv("bottoken")
if not raw_token:
    raise ValueError("BOT_TOKEN topilmadi! Iltimos .env faylida BOT_TOKEN ni ko'rsating.")

BOT_TOKEN = raw_token.split("//")[0].split("#")[0].strip().strip('"').strip("'")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
GROQ_CHAT_KEY = os.getenv("GROQ_CHAT_KEY", "").strip() or GROQ_API_KEY
CHAT_MODEL = os.getenv("CHAT_MODEL", "openai/gpt-oss-120b").strip()

# Rasmiy Admin profillari va ID lari
ADMIN_USERNAMES = ["o_raximjonov", "raximjonov_admin", "o_raximjonovuz"]
ADMIN_IDS = {966627404}

WEBHOOK_HOST = os.getenv("WEBHOOK_HOST", "https://grandpa-voice-bot.onrender.com").rstrip("/")
WEBHOOK_PATH = f"/webhook/{BOT_TOKEN}"
WEBHOOK_URL = f"{WEBHOOK_HOST}{WEBHOOK_PATH}"

BASE_DIR = Path(__file__).parent
TEMP_DIR = BASE_DIR / "temp"
TEMP_DIR.mkdir(parents=True, exist_ok=True)

MAX_VOICE_DURATION = 15  # Ovozni matnga aylantirish uchun maksimal 15 soniya
CONCURRENCY_LIMIT = 5
HISTORY_LIMIT = 4

# Maxsus Mehmon (Adizova Mohina)
SPECIAL_GUEST_ID = 7433386436
SPECIAL_GUEST_PRESET = (
    "Bu inson Bossning (Adminning) eng qadrli maxsus mehmoni, hurmatli qiz bola (Adizova Mohina). "
    "U bilan mutlaqo qaltis hazil qilma, ustidan aslo kulma va aslo sansirama! "
    "Faqat 'Siz' deb, juda muloyim, nozik, samimiy va hurmat bilan gaplash. "
    "Qiz bolaga xos muomala qil, ko'nglini olishga harakat qil, kuni qanday o'tayotganini, "
    "kayfiyatini so'ra, zeriktirma. So'zlarni aslo qo'shtirnoqqa olma, g'alati inglizcha memlar "
    "(wifi, FAQ, vlog kabi) ishlatma. Faqat iliq, yoqimli, madaniyatli va toza o'zbek tilida muloqot qil."
)

VIP_PRESETS = {
    "special_guest": SPECIAL_GUEST_PRESET,
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

PERSONA_INSTRUCTIONS = {
    "kocha": "Uslub: Yaqin o'zbek jigar/jo'ra. Erkin, samimiy va hozirjavob hazil bilan gapir.",
    "faylasuf": "Uslub: Dono faylasuf ustoz. Har gapdan chuqur hikmat topib, salobatli javob ber.",
    "sarkazm": "Uslub: O'tkir sarkazm va roast ustasi. Qochirim, kulgili kinoya va achchiq hazil bilan javob ber.",
    "dost": "Uslub: Mehribon va dardkash yaqin do'st. Dalda berib, iliq gapir.",
    "rasmiy": "Uslub: Rasmiy ekspert. Faqat 'Siz' deb, qisqa, aniq va jiddiy javob ber."
}

REFUSAL_PHRASES = (
    "i'm sorry", "i’m sorry", "i cannot", "i can't", "i can’t",
    "yordam bera olmayman", "javob bera olmayman", "davom ettira olmayman"
)


def is_admin_user(username: Optional[str], user_id: int) -> bool:
    if user_id in ADMIN_IDS:
        return True
    uname = (username or "").lower().strip("@").strip()
    return uname in ADMIN_USERNAMES
