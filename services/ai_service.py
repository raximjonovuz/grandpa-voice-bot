import asyncio
import json
import logging
import re
import time
import urllib.error
import urllib.request
from collections import deque

import db
import cf_storage
from config import (
    GROQ_CHAT_KEY,
    CHAT_MODEL,
    SMALL_MODEL,
    HISTORY_LIMIT,
    KV_CHAT_LOG,
    SPECIAL_GUEST_ID,
    SPECIAL_GUEST_PRESET,
    PERSONA_INSTRUCTIONS,
    REFUSAL_PHRASES,
)

logger = logging.getLogger("AIService")

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

# Suhbat xotirasi (operativ xotirada; bo'sh bo'lsa bazadan to'ldiriladi)
chat_history: dict[int, deque] = {}


class LLMUnavailable(Exception):
    """Hamma modellar tarmoq/server xatosi bilan javob bermadi."""


# Ishlamayotgan (o'chirilgan/noto'g'ri nomli) modellar: bir soat davomida qayta urinmaymiz
_dead_models: dict[str, float] = {}
DEAD_TTL = 3600


# ------------------------------------------------------------------ prompt

BASE_RULES = (
    "Sen Telegramdagi o'zbek suhbatdoshsan, ismi «Hazilkash». Odamdek, jonli va qisqa gaplashasan. "
    "Kimligingni so'rashsa, rostini ayt: AI do'st ekaningni.\n"
    "QOIDALAR:\n"
    "1) Avval foydalanuvchi nima degani va nima so'raganini TO'G'RI tushun va unga mantiqli javob ber; "
    "hazil faqat shundan keyin, yengil qo'shiladi. Uning gapini rad etma, bahslashma — 'ha, va...' usulida davom ettir.\n"
    "2) Tushunmasang to'qima: 'Nima demoqchisan, aniqroq yozchi' de.\n"
    "3) Javob 1-2 ta QISQA jumla (25 so'zdan oshmasin), 0-2 emoji. Ro'yxat, sarlavha, yulduzcha, qo'shtirnoq yo'q.\n"
    "4) Foydalanuvchi gapini va oldingi javobingni takrorlama. Har safar 'salom' yoki bir xil gap bilan boshlama. "
    "Har javobni savol bilan tugatish shart emas.\n"
    "5) Til: foydalanuvchi qaysi tilda/alifboda yozsa, shunda javob ber. O'zbekchasi toza bo'lsin: sen, sening, senga, seni, "
    "xush kelibsan, qalaysan, nima gap. Turkcha/qozoqcha so'zlarni ('senin', 'bilmem') aralashtirma. Inglizcha memlar (wifi, vlog) yo'q.\n"
    "6) Foydalanuvchi so'kinsa yoki qo'pol yozsa: xafa bo'lma, va'z o'qima, o'zing so'kinma — hazil bilan yumshat.\n"
    "7) Din, oila, millat, tashqi ko'rinish, kasallik, o'lim ustidan kulma. Odam xafa yoki jiddiy gapirsa — hazilni to'xtat, samimiy bo'l.\n"
    "8) Ismini har gapda aytma."
)


def _age_int(age: str):
    m = re.search(r"\d{1,2}", age or "")
    return int(m.group(0)) if m else None


def _address_hint(gender: str, age_i) -> str:
    """Jins va yoshga qarab murojaat va ohang (Python hal qiladi, LLM adashmasin)."""
    if age_i is None:
        tone = "Yoshi noma'lum: o'rtacha, samimiy ohang."
    elif age_i <= 15:
        tone = "Bola: oddiy so'zlar, o'yinqaroq, qo'pol sleng yo'q."
    elif age_i <= 17:
        tone = "O'smir: o'smirlarcha, o'yinqaroq sleng."
    elif age_i <= 25:
        tone = "Yosh: zamonaviy yoshlar slengi (qalesan, zo'r-ku, bomba)."
    elif age_i <= 39:
        tone = "Katta yosh: sleng kamroq, tabiiy va samimiy."
    else:
        tone = "Yoshi katta: hurmat bilan, sleng yo'q, 'siz' deyish mumkin."

    if gender == "E":
        words = "jo'ra, birodar, uka" if (age_i or 20) < 30 else "aka, birodar"
        who = f"Erkak. Murojaat: {words}."
    elif gender == "A":
        who = "Qiz/ayol. Murojaat: singil, dugona" + (", opa" if (age_i or 20) >= 30 else "") + ". Muloyimroq, qo'pol so'zlarsiz."
    else:
        who = "Jinsi noma'lum: 'do'stim' de, jins ko'rsatuvchi so'z ishlatma."
    return f"{who} {tone}"


def _script_hint(user_text: str) -> str:
    letters = [c for c in user_text if c.isalpha()]
    if not letters:
        return ""
    cyr = sum(1 for c in letters if re.match(r"[а-яёўқғҳА-ЯЁЎҚҒҲ]", c))
    if cyr / len(letters) > 0.5:
        return "\nFoydalanuvchi KIRILL alifbosida yozdi: javobni ham kirillda yoz (o'zbekcha bo'lsa o'zbek kirillida, ruscha bo'lsa ruscha)."
    return ""


def _clean_rule(raw_rule: str) -> str:
    return re.sub(r"^\|\s*ID:\s*\d+\s*", "", raw_rule.strip(), flags=re.I)


def build_system_prompt(user_data: dict, last_user_text: str = "") -> str:
    """
    Ustuvorlik:
    1. Maxsus mehmon (SPECIAL_GUEST_ID) — alohida iltifotli yo'riqnoma.
    2. Admin bergan shaxsiy topshiriq (VIP) — faqat shu qo'llanadi, global rejim qo'shilmaydi.
    3. Qolganlar — admin tanlagan global rejim + jins/yoshga mos murojaat.
    """
    user_id = user_data.get("user_id") or 0
    name = user_data.get("custom_name") or user_data.get("first_name") or "Do'stim"
    age = user_data.get("age") or ""
    gender = user_data.get("gender") or ""
    memory = (user_data.get("memory_notes") or "").strip()
    vip_rule = (user_data.get("vip_instruction") or "").strip()
    username = user_data.get("username") or ""

    if user_id == SPECIAL_GUEST_ID:
        vip_rule = SPECIAL_GUEST_PRESET

    prompt = BASE_RULES + "\n\n"
    prompt += f"SUHBATDOSH: ism={name}; yosh={age or 'nomalum'}"
    if username:
        prompt += f"; username=@{username}"
    prompt += "\n"
    if memory:
        prompt += f"ESLAB QOLGANLARING (kerak bo'lsagina ishlat, ko'rsatib yurma): {memory}\n"

    if vip_rule:
        prompt += (
            f"\nDIQQAT! SHU ODAM UCHUN ADMINNING QAT'IY TOPSHIRIG'I: «{_clean_rule(vip_rule)}»\n"
            "Bu topshiriq yuqoridagi uslub qoidalaridan ustun. Unga so'zsiz amal qil, boshqa uslubga o'tma.\n"
        )
    else:
        persona_key = db.get_setting("ai_persona", "kocha")
        prompt += "\n" + PERSONA_INSTRUCTIONS.get(persona_key, PERSONA_INSTRUCTIONS["kocha"]) + "\n"
        prompt += _address_hint(gender, _age_int(age)) + "\n"
        extra = (db.get_setting("custom_global_prompt", "") or "").strip()
        if extra:
            prompt += f"Admin qo'shimchasi: {_clean_rule(extra)}\n"

    prompt += _script_hint(last_user_text)
    return prompt


# ------------------------------------------------------------------ javobni tozalash

_WORD_FIXES = [
    (r"\bsenin\b", "sening"),
    (r"\bsenden\b", "sendan"),
    (r"\bxush kelibsening\b", "xush kelibsan"),
    (r"\bxush kelibsen\b", "xush kelibsan"),
    (r"\bbilmem\b", "bilmayman"),
]


def _keep_case(repl: str):
    def f(m):
        w = m.group(0)
        return repl.capitalize() if w[:1].isupper() else repl
    return f


def tidy(text: str, truncated: bool = False) -> str:
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S)
    text = re.sub(r"[*`]+|__|^#+\s*", "", text, flags=re.M)
    text = re.sub(r"[«»“”\"]", "", text)
    text = re.sub(r"\n{2,}", "\n", text).strip()
    for pat, repl in _WORD_FIXES:
        text = re.sub(pat, _keep_case(repl), text, flags=re.I)
    if truncated:
        # token tugab, gap yarimda uzilgan bo'lsa — oxirgi tugagan jumlagacha kesamiz
        m = list(re.finditer(r"[.!?…]|[\U0001F300-\U0001FAFF]", text))
        if m and m[-1].end() > len(text) * 0.4:
            text = text[:m[-1].end()].strip()
    return text


# ------------------------------------------------------------------ Groq

def groq_chat(messages: list, temperature: float = 0.65, max_tokens: int = 250,
              models: list | None = None, fallback: bool = True, clean: bool = True) -> str:
    """
    Modellar zanjiri bo'ylab urinadi.
    - Model rad javobi (refusal) bersa: '__REFUSED__' (fallback=True) yoki xato.
    - Hamma modellar tarmoq/server xatosi bersa: LLMUnavailable (haqoratli javob EMAS).
    """
    candidates = models or [CHAT_MODEL, "openai/gpt-oss-120b", "openai/gpt-oss-20b", "llama-3.3-70b-versatile"]
    chain = []
    for m in candidates:
        if m and m not in chain and _dead_models.get(m, 0) < time.time():
            chain.append(m)

    refused = False
    for model_name in chain:
        payload = {
            "model": model_name,
            "messages": messages,
            "max_tokens": max(max_tokens, 400) if "gpt-oss" in model_name else max_tokens,
            "temperature": temperature,
        }
        if "gpt-oss" in model_name:
            payload["reasoning_effort"] = "low"
        req = urllib.request.Request(
            GROQ_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {GROQ_CHAT_KEY}",
                "Content-Type": "application/json",
                "User-Agent": "Mozilla/5.0",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=18) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            choice = data["choices"][0]
            raw_text = choice["message"].get("content") or ""
            # JSON kabi xom natijalarda (clean=False) qo'shtirnoqlarni o'chirmaymiz
            text = tidy(raw_text, truncated=choice.get("finish_reason") == "length") if clean else raw_text.strip()
            if not text:
                continue
            if any(p in text.lower() for p in REFUSAL_PHRASES):
                refused = True
                continue
            return text
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", errors="replace")[:300]
            except Exception:
                pass
            logger.warning(f"Model {model_name} HTTP {e.code}: {body}")
            if e.code in (400, 404) or "decommission" in body or "model_not_found" in body:
                _dead_models[model_name] = time.time() + DEAD_TTL
        except Exception as e:
            logger.warning(f"Model {model_name} xatosi: {e}")

    if refused and fallback:
        return "__REFUSED__"
    raise LLMUnavailable("Hamma modellar javob bermadi")


ROASTS = [
    "Eee jo'ra, lug'atingda shundan boshqa gap qolmadimi? 😂 Bor, chiroyli gap o'rganib kel!",
    "Oho, o'zingcha zo'r narsa yozdim deb o'yladingmi? 😎 Yaxshisi, boshqa gap ayt-chi!",
    "Gap topolmay qolganda shunaqa qilasanmi? 😅 Odamday yozsang javob olasan!",
]


def _fallback_reply(user_data: dict, kind: str, seed_text: str) -> str:
    """kind: 'refused' (model rad etdi) yoki 'down' (servis ishlamayapti)."""
    is_vip = bool((user_data.get("vip_instruction") or "").strip()) or user_data.get("user_id") == SPECIAL_GUEST_ID
    if kind == "down":
        if is_vip:
            return "Kechirasiz, hozir bog'lanish biroz sekin. Bir ozdan so'ng yana yozing 🙏"
        return "Hozir miyam bir sekin ishladi 😅 Bir daqiqadan keyin yana yozchi!"
    if is_vip:
        return "Bu haqda boshqacha gaplashaylik, mayli 🙂 Boshqa nima gap?"
    return ROASTS[sum(map(ord, seed_text)) % len(ROASTS)]


# ------------------------------------------------------------------ javob generatsiyasi

def _load_history(user_id: int) -> deque:
    history = chat_history.setdefault(user_id, deque(maxlen=HISTORY_LIMIT))
    if not history:
        for item in db.get_local_chat_logs(user_id, limit=HISTORY_LIMIT):
            if any(p in item["text"].lower() for p in REFUSAL_PHRASES):
                continue
            role = "user" if item["sender"] == "user" else "assistant"
            history.append({"role": role, "content": item["text"][:300]})
    return history


def generate_ai_reply(user_id: int, user_message: str, remember_user_msg: bool = True) -> str:
    """remember_user_msg=False: tizim ko'rsatmasi (masalan, tanishuv salomi) — tarixga yozilmaydi."""
    user_data = db.get_user(user_id) or {}
    user_data["user_id"] = user_id
    history = _load_history(user_id)

    # Foydalanuvchi xabari bazaga allaqachon yozilgan bo'lsa va tarixga tushgan bo'lsa, ikki marta yubormaymiz.
    hist_list = list(history)
    if hist_list and hist_list[-1]["role"] == "user" and hist_list[-1]["content"] == user_message[:300]:
        hist_list.pop()

    messages = [{"role": "system", "content": build_system_prompt(user_data, user_message)}]
    messages += hist_list
    messages.append({"role": "user", "content": user_message[:300]})

    try:
        reply = groq_chat(messages)
    except LLMUnavailable:
        return _fallback_reply(user_data, "down", user_message)

    if reply == "__REFUSED__":
        return _fallback_reply(user_data, "refused", user_message)

    if remember_user_msg:
        history.append({"role": "user", "content": user_message[:300]})
    history.append({"role": "assistant", "content": reply[:300]})
    return reply


# ------------------------------------------------------------------ xotira va jins

def update_memory_sync(user_id: int) -> None:
    """Foydalanuvchi aytgan MUHIM faktlarni (shahar, o'qish/ish, qiziqish...) qisqa ro'yxatda saqlaydi."""
    user = db.get_user(user_id)
    if not user:
        return
    logs = db.get_local_chat_logs(user_id, limit=14)
    lines = [l["text"][:200] for l in logs if l["sender"] == "user"][-7:]
    if not lines:
        return

    sys_prompt = (
        "Sen xotira yordamchisisan. Quyida foydalanuvchi haqida hozirgi faktlar va uning oxirgi xabarlari bor. "
        "Foydalanuvchi O'ZI aytgan yangi muhim faktlarni qo'shib (familiya, shahar, o'qish/ish, qiziqish, do'st/oila ismi, "
        "orzu, muammo, yoqtirgani/yoqtirmagani), eskirganini yangisi bilan almashtirib, yangilangan ro'yxatni qaytar. "
        "Ism va yosh kerak emas. Taxmin qilma. Ko'pi bilan 10 ta, har biri 10 so'zdan oshmasin. "
        'Faqat JSON: {"facts": ["...", "..."]}'
    )
    user_prompt = f"Hozirgi faktlar: {user.get('memory_notes') or 'yoq'}\nXabarlar:\n" + "\n".join(f"- {x}" for x in lines)
    try:
        raw = groq_chat(
            [{"role": "system", "content": sys_prompt}, {"role": "user", "content": user_prompt}],
            temperature=0.1, max_tokens=300,
            models=[SMALL_MODEL, "openai/gpt-oss-20b"], fallback=False, clean=False,
        )
        m = re.search(r"\{.*\}", raw, flags=re.S)
        if not m:
            return
        facts = json.loads(m.group(0)).get("facts", [])
        facts = [str(f).strip() for f in facts if isinstance(f, str) and 2 < len(f.strip()) <= 90][:10]
        if facts:
            db.set_user_memory(user_id, "; ".join(facts))
    except Exception as e:
        logger.warning(f"Xotirani yangilash xatosi: {e}")


def _heuristic_gender(name: str) -> str:
    n = (name or "").strip().split(" ")[0].lower()
    if not n:
        return ""
    if n.endswith(("lla", "bek", "dor", "ullo", "iddin", "jon")):
        return "E"
    if n.endswith(("a", "oy", "gul", "noz", "niso", "bonu", "xon")):
        return "A"
    return "E" if n.endswith(("r", "n", "m", "d", "l", "k", "z", "v", "b", "t", "s")) else ""


def infer_gender_sync(name: str, tg_first_name: str = "") -> str:
    """Ismdan jinsni aniqlaydi: 'E' (erkak), 'A' (ayol) yoki ''. Bir marta, juda arzon."""
    try:
        raw = groq_chat(
            [
                {"role": "system", "content": "O'zbek ismi erkakmi yoki ayolmi? Faqat bitta belgi qaytar: E (erkak), A (ayol), ? (noma'lum)."},
                {"role": "user", "content": f"Ism: {name}. Telegram ismi: {tg_first_name}"},
            ],
            temperature=0, max_tokens=5,
            models=[SMALL_MODEL, "openai/gpt-oss-20b"], fallback=False,
        )
        ch = raw.strip().upper()[:1]
        if ch in ("E", "A"):
            return ch
    except Exception as e:
        logger.warning(f"Jinsni aniqlash xatosi: {e}")
    return _heuristic_gender(name)


async def record_conversation(user_id: int, sender: str, text: str):
    """Suhbatni bazaga yozadi. Cloudflare KV ga har xabarni yozish faqat KV_CHAT_LOG=1 bo'lsa."""
    db.log_chat_message(user_id, sender, text)
    if KV_CHAT_LOG:
        asyncio.create_task(asyncio.to_thread(cf_storage.append_kv_chat_log, user_id, sender, text))
