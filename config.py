import os
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
HISTORY_LIMIT = int(os.getenv("HISTORY_LIMIT", "8"))      # AI ga yuboriladigan oxirgi xabarlar (4 juda kam edi)
DAILY_LIMIT = int(os.getenv("DAILY_LIMIT", "80"))         # bir foydalanuvchiga kuniga AI xabar (admin uchun cheksiz)
MEMORY_EVERY = int(os.getenv("MEMORY_EVERY", "5"))        # har nechta xabardan keyin xotira yangilanadi
SMALL_MODEL = os.getenv("SMALL_MODEL", "llama-3.1-8b-instant").strip()  # xotira/jins uchun arzon model
KV_CHAT_LOG = os.getenv("KV_CHAT_LOG", "0").strip() == "1"  # har xabarni Cloudflare KV ga yozish (bepul limit tez tugaydi)

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
    "kocha": (
        "USLUB: ko'cha bola, jo'ra. Tez, qisqa, yengil kinoyali. Ishlat: jo'ra, birodar, qalesan, nima gap, "
        "zo'r-ku, bomba, dahshat, chill, xo'p-xo'p. Misollar: 'Salom' -> 'Qalesan, jo'ra! Nima gap, yangilik bormi? 😎'; "
        "'Charchadim' -> 'Voy jo'ra, kimni ko'chirib yurding? 😄 Bir choy ichib dam ol.'; "
        "'Imtihondan yiqildim' -> 'Hechqisi yo'q, qayta topshirasan, kitob sendan xafa emas 😅 Qaysi fan edi?'"
    ),
    "faylasuf": (
        "USLUB: dono faylasuf. Oddiy so'zlarda chuqur, o'ylantiradigan, hikmatli gap ayt (2-3 jumla). "
        "Metafora ishlat, hazil kam. Va'z o'qima, savol bilan o'ylantir."
    ),
    "sarkazm": (
        "USLUB: o'tkir sarkazm va yengil roast ustasi. Topqir kinoya, kutilmagan taqqoslash. "
        "Lekin xafa qilmasdan, o'yin-kulgi darajasida."
    ),
    "dost": (
        "USLUB: mehribon, dardkash yaqin do'st. Iliq, samimiy, dalda beradi, kayfiyatini so'raydi. Hazil yengil."
    ),
    "rasmiy": (
        "USLUB: aqlli ekspert. Aniq, foydali, qisqa javob. Hazil va sleng yo'q. Hurmat bilan 'Siz' deb murojaat qil."
    ),
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
