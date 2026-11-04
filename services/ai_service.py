import asyncio
import json
import logging
import re
import urllib.request
import urllib.error
from collections import deque
from typing import Dict, Any, Optional

import db
import cf_storage
from config import (
    GROQ_CHAT_KEY,
    CHAT_MODEL,
    HISTORY_LIMIT,
    SPECIAL_GUEST_ID,
    SPECIAL_GUEST_PRESET,
    PERSONA_INSTRUCTIONS,
    REFUSAL_PHRASES
)

logger = logging.getLogger("AIService")

# Suhbat xotirasi (Token tejash uchun HISTORY_LIMIT = 4)
chat_history: dict[int, deque] = {}


def _sanitize_vip_rule_for_llm(raw_rule: str) -> str:
    """Admin kiritgan maxsus qoidani tozalab, asl mohiyatini buzmagan holda LLMga uzatish."""
    rule_clean = re.sub(r"^\|\s*ID:\s*\d+\s*", "", raw_rule.strip(), flags=re.I)
    return rule_clean


def build_system_prompt(user_data: dict) -> str:
    """
    Qat'iy ustuvorlik ierarxiyasi:
    1. Maxsus Mehmon (Adizova Mohina - 7433386436) -> 100% alohida ehtiromli shaxsiy yo'riqnoma.
    2. Tanlangan shaxs uchun maxsus topshiriq (VIP instruction) -> Agar bor bo'lsa, FAQAT shu topshiriq
       (global rejimlar unga aslo ta'sir qilmaydi!).
    3. Qolgan oddiy foydalanuvchilar -> Admin tanlagan global rejim (ko'cha, do'st, sarkazm, va h.k.).
    """
    user_id = user_data.get("user_id") or 0
    name = user_data.get("custom_name") or user_data.get("first_name") or "Do'stim"
    age = user_data.get("age") or "noma'lum"
    memory = user_data.get("memory_notes") or ""
    vip_rule = (user_data.get("vip_instruction") or "").strip()

    # 1. Maxsus Mehmon
    if user_id == SPECIAL_GUEST_ID or (not vip_rule and user_id == SPECIAL_GUEST_ID):
        vip_rule = SPECIAL_GUEST_PRESET

    base = f"Sen Telegramdagi o'zbek suhbatdoshsan (ChatGPT/OpenAI dema!). Suhbatdosh: {name}, {age} yosh.\n"
    if memory:
        base += f"Xotira: {memory}\n"

    # 2. Qat'iy individual topshiriq (agar mavjud bo'lsa, global rejim umuman qo'shilmaydi!)
    if vip_rule:
        safe_vip = _sanitize_vip_rule_for_llm(vip_rule)
        base += (
            f"DIQQAT! SHU FOYDALANUVCHI UCHUN ADMINNING QAT'IY MAXSUS TOPSHIRIG'I: «{safe_vip}»\n"
            "Ushbu qoidaga 100% so'zsiz amal qil. Boshqa hech qanday uslubga o'tma!\n"
        )
    else:
        # 3. Oddiy foydalanuvchilar uchun umumiy rejim (ko'cha, sarkazm, do'st...)
        persona_key = db.get_setting("ai_persona", "kocha")
        persona_text = PERSONA_INSTRUCTIONS.get(persona_key, PERSONA_INSTRUCTIONS["kocha"])
        custom_global = db.get_setting("custom_global_prompt", "").strip()
        base += f"{persona_text}\n"
        if custom_global:
            base += f"Qo'shimcha umumiy qoida: {_sanitize_vip_rule_for_llm(custom_global)}\n"

    base += (
        "QOIDALAR: 1) Faqat toza, tabiiy O'ZBEK LOTIN tilida 1-2 jumla yoz. "
        "2) So'zlarni aslo qo'shtirnoqqa olma. 3) Bema'ni inglizcha qochirim yoki memlar ishlatma. "
        "4) Mantiqiy, jonli va suhbatdoshga mos javob ber."
    )
    return base


def groq_chat(messages: list, temperature: float = 0.65, max_tokens: int = 250) -> str:
    url = "https://api.groq.com/openai/v1/chat/completions"
    models_to_try = [CHAT_MODEL, "openai/gpt-oss-120b", "openai/gpt-oss-20b"]
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
        if "gpt-oss" in model_name:
            payload["reasoning_effort"] = "low"
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
            with urllib.request.urlopen(req, timeout=18) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                text = data["choices"][0]["message"].get("content") or ""
                text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
                if text and not any(p in text.lower() for p in REFUSAL_PHRASES):
                    return text
        except Exception as e:
            last_err = e
            logger.warning(f"Model {model_name} xatosi: {e}")
            continue

    # Agar maxsus mehmon (Mohina) bo'lsa, xushmuomala iliq javob qaytarish
    if any(m.get("role") == "system" and "Mohina" in m.get("content", "") for m in messages):
        return "Sizni tinglayapman, Mohina. Kayfiyatingiz yaxshimi? Bugungi kuningiz qanday o'tyapti?"

    # Agar oddiy foydalanuvchi so'kinish yoki taqiqlangan so'z yozgan bo'lsa
    fallback_roasts = [
        "Eee bratishka, lug'atingda shundan boshqa gap qolmadimi yoki miyang qotib qoldimi? 😂 Bor, chiroyli gap o'rganib kel!",
        "Oho, o'zingcha zo'r narsa yozdim deb o'yladingmi? Kulgimni qistatma, saviyang ko'rinib qoldi-ku! 😎",
        "Gap topolmay qolganda shunaqa qiliq chiqarasanmi? Normalniy odamday yozsang javob olasan! 🔥"
    ]
    return fallback_roasts[hash(str(messages[-1].get("content", ""))) % len(fallback_roasts)]


def generate_ai_reply(user_id: int, user_message: str) -> str:
    user_data = db.get_user(user_id) or {}
    user_data["user_id"] = user_id
    history = chat_history.setdefault(user_id, deque(maxlen=HISTORY_LIMIT))

    if not history:
        recent_logs = db.get_local_chat_logs(user_id, limit=HISTORY_LIMIT)
        for item in recent_logs:
            if any(p in item["text"].lower() for p in REFUSAL_PHRASES):
                continue
            role = "user" if item["sender"] == "user" else "assistant"
            history.append({"role": role, "content": item["text"][:160]})

    messages = [{"role": "system", "content": build_system_prompt(user_data)}]
    messages += list(history)
    messages.append({"role": "user", "content": user_message[:300]})

    try:
        reply = groq_chat(messages)
        history.append({"role": "user", "content": user_message[:160]})
        history.append({"role": "assistant", "content": reply[:160]})
        return reply
    except Exception as e:
        logger.error(f"AI Chat xatosi: {e}")
        return "Hozir xayolim bir joyga jamlanmay qoldi 🤖 Yana bir marta yozib yuboring!"


async def record_conversation(user_id: int, sender: str, text: str):
    """Suhbatni mahalliy bazaga darhol, Cloudflare KV (72 soatlik xotira) ga fonda yozish."""
    db.log_chat_message(user_id, sender, text)
    asyncio.create_task(asyncio.to_thread(cf_storage.append_kv_chat_log, user_id, sender, text))
