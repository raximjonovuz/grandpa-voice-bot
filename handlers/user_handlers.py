import asyncio
import logging
import uuid
import re
from collections import deque
from typing import Dict, List

from aiogram import Router, F, Bot
from aiogram.types import (
    Message,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton
)
from aiogram.filters import CommandStart, Command

import db
from config import (
    is_admin_user,
    ADMIN_USERNAMES,
    ADMIN_IDS,
    MAX_VOICE_DURATION,
    TEMP_DIR,
    HISTORY_LIMIT,
    DAILY_LIMIT,
    MEMORY_EVERY
)
from services.ai_service import (
    generate_ai_reply,
    record_conversation,
    chat_history,
    groq_chat,
    update_memory_sync,
    infer_gender_sync
)
from services.audio_service import (
    convert_to_mp3,
    transcribe_with_groq_whisper,
    refine_stt_text
)
from handlers.admin_handlers import (
    admin_action_state,
    get_admin_panel_keyboard,
    get_vip_presets_keyboard
)

logger = logging.getLogger("UserHandlers")
user_router = Router()

cleanup_ids: Dict[int, List[int]] = {}
user_locks: Dict[int, asyncio.Lock] = {}   # bir foydalanuvchining xabarlari navbat bilan ishlansin


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
        input_field_placeholder="Xabaringizni yozing..."
    )


async def notify_admins_new_user(bot: Bot, user):
    """Yangi foydalanuvchi kirganda barcha adminlarga xabar berish."""
    admin_ids = db.get_admin_chat_ids(ADMIN_USERNAMES, ADMIN_IDS)
    if not admin_ids:
        return

    stats = db.get_stats()
    uname = f"@{user.username}" if user.username else "username yo'q"
    full_tg_name = f"{user.first_name or ''} {user.last_name or ''}".strip()

    text = (
        "🚀 Yangi foydalanuvchi qo'shildi!\n\n"
        f"• Telegram ismi: {full_tg_name}\n"
        f"• Username: {uname}\n"
        f"• User ID: {user.id}\n"
        f"• Bugun qo'shilganlar: {stats['today_new_users']} ta\n"
        f"• Jami a'zolar: {stats['total_users']} ta\n\n"
        "Ushbu foydalanuvchi bilan tezkor boshqaruv:"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="💬 Chatini o'qish", callback_data=f"adm_view_u:{user.id}"),
            InlineKeyboardButton(text="👑 Maxsus muomala", callback_data=f"adm_vip_u:{user.id}")
        ],
        [InlineKeyboardButton(text="✍️ Bot nomidan yozish", callback_data=f"adm_takeover:{user.id}")]
    ])
    for aid in admin_ids:
        try:
            await bot.send_message(aid, text, reply_markup=kb)
        except Exception as e:
            logger.warning(f"Admin {aid} ga bildirishnoma yuborilmadi: {e}")


# --- Komandalar va Menyu ---

@user_router.message(CommandStart())
async def handle_start(message: Message, bot: Bot):
    user = message.from_user
    admin_action_state.pop(user.id, None)
    is_adm = is_admin_user(user.username, user.id)
    is_new = db.add_or_update_user(user.id, user.username, user.first_name, user.last_name)

    if is_new:
        asyncio.create_task(notify_admins_new_user(bot, user))

    user_name = user.first_name or "Do'stim"
    text = (
        f"Assalomu alaykum, {user_name}! 👋\n\n"
        "Men ko'p funksiyali aqlli Telegram botman. Quyidagi imkoniyatlardan foydalanishingiz mumkin:\n\n"
        "💬 «AI Suhbat» — samimiy, dono va topqir AI suhbatdoshi bilan erkin suhbatlashish.\n"
        "📝 «Ovozni matn qilish» — 15 soniyagacha bo'lgan ovozli xabarlarni darhol aniq matnga aylantirish.\n\n"
        "Boshlash uchun pastdagi tugmalardan birini tanlang:"
    )
    if is_adm:
        text += (
            "\n\n👑 Siz rasmiy ADMINsiz! "
            "Pastdagi <👑 Admin Panel> tugmasini yoki /admin buyrug'ini bosib botni to'liq boshqarishingiz mumkin."
        )

    await message.answer(text, reply_markup=get_main_keyboard(is_adm))


@user_router.message(Command("help"))
async def handle_help(message: Message):
    user = message.from_user
    is_adm = is_admin_user(user.username, user.id)
    text = (
        "📖 Botdan foydalanish bo'yicha qo'llanma:\n\n"
        "1. 💬 «AI Suhbat» — tugmani bosing va xabar yozing. Bot siz bilan o'zbek tilida erkin suhbatlashadi.\n"
        "2. 📝 «Ovozni matn qilish» — istalgan vaqtda 15 soniyagacha ovozli xabar (voice) yuborsangiz, uni matnga aylantirib beradi.\n"
        "3. 🔄 «Suhbatni yangilash» — xotirani tozalab, suhbatni yangidan boshlash.\n\n"
        "🔒 Sifatni yaxshilash uchun suhbat qisqa muddat saqlanadi va admin ko'rishi mumkin. "
        "/forget — ma'lumotlaringizni butunlay o'chirish.\n"
    )
    await message.answer(text, reply_markup=get_main_keyboard(is_adm))


@user_router.message(Command("forget"))
async def handle_forget(message: Message):
    user = message.from_user
    admin_action_state.pop(user.id, None)
    chat_history.pop(user.id, None)
    db.forget_user(user.id)
    is_adm = is_admin_user(user.username, user.id)
    await message.answer(
        "🗑 Ism, yosh, xotira va suhbat tarixingiz o'chirildi. Xohlasangiz «💬 AI Suhbat» orqali qaytadan tanishamiz.",
        reply_markup=get_main_keyboard(is_adm)
    )


@user_router.message(F.text == "⬅️ Asosiy menyu")
async def back_to_main_menu(message: Message):
    user = message.from_user
    admin_action_state.pop(user.id, None)
    db.set_user_mode(user.id, "chat")
    is_adm = is_admin_user(user.username, user.id)
    await message.answer("Asosiy menyu:", reply_markup=get_main_keyboard(is_adm))


@user_router.message(F.text == "🔄 Suhbatni yangilash")
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


@user_router.message(F.text == "📝 Ovozni matn qilish (15s)")
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


@user_router.message(F.text == "💬 AI Suhbat")
async def select_ai_chat_mode(message: Message, bot: Bot):
    user = message.from_user
    admin_action_state.pop(user.id, None)
    is_new = db.add_or_update_user(user.id, user.username, user.first_name, user.last_name)
    if is_new:
        asyncio.create_task(notify_admins_new_user(bot, user))

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


# --- Ovozli xabarlar ---

@user_router.message(F.voice)
async def handle_voice_message(message: Message, bot: Bot):
    voice = message.voice
    if not voice:
        return

    user = message.from_user
    is_new = db.add_or_update_user(user.id, user.username, user.first_name, user.last_name)
    if is_new:
        asyncio.create_task(notify_admins_new_user(bot, user))

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
        raw_transcription = await asyncio.to_thread(transcribe_with_groq_whisper, mp3_file)
        transcription = refine_stt_text(raw_transcription, groq_chat)
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


# --- Matnli xabarlar va AI suhbat ---

@user_router.message(F.text)
async def handle_text_message(message: Message, bot: Bot):
    user = message.from_user
    text = message.text.strip()
    is_adm = is_admin_user(user.username, user.id)
    is_new = db.add_or_update_user(user.id, user.username, user.first_name, user.last_name)
    if is_new:
        asyncio.create_task(notify_admins_new_user(bot, user))

    # 1. Agar ADMIN bo'lsa va admin amali (Takeover / VIP / Global qoida) rejimida bo'lsa:
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
            m_only = re.match(r"^(?:https?://t\.me/|@)?([a-zA-Z0-9_]{3,32})(?:\s*\|\s*ID:\s*\d+)?/?$", text, flags=re.I)
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

            m_both = re.match(r"^(?:https?://t\.me/|@)?([a-zA-Z0-9_]{3,32})(?:\s*\|\s*ID:\s*\d+)?\s+(?:[-—:]\s*)?(.+)$", text, flags=re.S | re.I)
            if m_both:
                target_uname = m_both.group(1)
                rule_text = re.sub(r"^\|\s*ID:\s*\d+\s*", "", m_both.group(2).strip(), flags=re.I)
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

            await message.reply("⚠️ Username formati tushunarsiz bo'ldi. Iltimos, `@username` yoki `https://t.me/username` yuboring:")
            return

        elif action == "set_vip_uname_rule":
            target_uname = adm_state["target_uname"]
            found_uid = db.set_vip_by_username(target_uname, text)
            admin_action_state.pop(user.id, None)
            status_note = "Foydalanuvchi bazada topildi va yangilandi!" if found_uid else "Foydalanuvchi botga kirishi bilan ushbu qoida avtomatik ishlaydi!"
            await message.reply(
                f"✅ @{target_uname} uchun maxsus muomala qoidasi saqlandi!\n"
                f"📌 Qoida: «{text}»\n"
                f"ℹ️ {status_note}",
                reply_markup=get_admin_panel_keyboard()
            )
            return

        elif action == "set_custom_global":
            db.set_setting("custom_global_prompt", text)
            admin_action_state.pop(user.id, None)
            await message.reply(
                f"✅ Global qo'shimcha qoida saqlandi!\n📌 Qoida: «{text}»",
                reply_markup=get_admin_panel_keyboard()
            )
            return

    # 2. Foydalanuvchi Tanishuv (onboarding) bosqichlari:
    chat_st = db.get_chat_state(user.id)
    step = chat_st.get("step")

    # Profil to'liq bo'lmasa (masalan, menyuni bosmasdan yozgan bo'lsa) — avval tanishamiz
    if step not in ("ask_name", "ask_age") and not (chat_st.get("name") and chat_st.get("age")):
        db.set_chat_state(user.id, "ask_name")
        sent = await message.answer(
            "Qalesan! 😎 Avval tanishib olaylik. Ismingni ayt?",
            reply_markup=get_chat_keyboard(is_adm)
        )
        cleanup_ids[user.id] = [message.message_id, sent.message_id]
        return

    if step == "ask_name":
        clean_name = text.split("\n")[0].strip()[:40]
        if len(clean_name) < 2 or not re.search(r"[^\W\d_]", clean_name) or clean_name.startswith("/"):
            sent = await message.answer("Ismingni harflar bilan yoz, masalan: Ozodbek 🙂", reply_markup=get_chat_keyboard(is_adm))
            cleanup_ids.setdefault(user.id, []).extend([message.message_id, sent.message_id])
            return
        db.set_chat_state(user.id, "ask_age", custom_name=clean_name)
        sent = await message.answer(
            f"Tanishganimdan xursandman, {clean_name}! 😊 Yoshing nechida?",
            reply_markup=get_chat_keyboard(is_adm)
        )
        cleanup_ids.setdefault(user.id, []).extend([message.message_id, sent.message_id])
        return

    if step == "ask_age":
        m_age = re.search(r"\d{1,3}", text)
        age_val = int(m_age.group(0)) if m_age else 0
        if not (5 <= age_val <= 99):
            sent = await message.answer("Yoshingni raqam bilan yoz, masalan: 19 🙂", reply_markup=get_chat_keyboard(is_adm))
            cleanup_ids.setdefault(user.id, []).extend([message.message_id, sent.message_id])
            return

        cname = chat_st.get("name") or user.first_name or "Do'stim"
        db.set_chat_state(user.id, "chatting", age=str(age_val))
        chat_history.pop(user.id, None)

        # Tanishuv savol-javoblarini chatdan o'chiramiz: faqat AI salomi qoladi
        old_ids = cleanup_ids.pop(user.id, []) + [message.message_id]
        for mid in old_ids:
            try:
                await bot.delete_message(message.chat.id, mid)
            except Exception:
                pass

        await bot.send_chat_action(message.chat.id, "typing")
        gender = await asyncio.to_thread(infer_gender_sync, cname, user.first_name or "")
        if gender:
            db.set_user_gender(user.id, gender)
        greet = await asyncio.to_thread(
            generate_ai_reply, user.id,
            f"Salom, men {cname}man. Tanishganimizga qisqa, mos salom yoz.", False
        )
        await record_conversation(user.id, "bot", greet)
        await message.answer(greet, reply_markup=get_chat_keyboard(is_adm))
        return

    # 3. AI Suhbat / Jonli chat:
    lock = user_locks.setdefault(user.id, asyncio.Lock())
    async with lock:
        if not is_adm and db.count_today_user_msgs(user.id) >= DAILY_LIMIT:
            await message.answer("Bugungi suhbat limiti tugadi 😅 Ertaga davom etamiz!", reply_markup=get_chat_keyboard(is_adm))
            return

        text = text[:1000]
        await record_conversation(user.id, "user", text)

        # Agar ushbu user uchun admin AI ni to'xtatgan bo'lsa (Takeover):
        paused, rem_sec = db.is_ai_paused(user.id)
        if paused:
            admin_ids = db.get_admin_chat_ids(ADMIN_USERNAMES, ADMIN_IDS)
            u_info = db.get_user(user.id) or {}
            uname = f"@{u_info.get('username')}" if u_info.get('username') else "yo'q"
            cname = u_info.get("custom_name") or u_info.get("first_name") or str(user.id)

            adm_notify = (
                f"📩 Jonli xabar: {cname} ({uname} | ID: {user.id})\n"
                f"💬 «{text}»\n"
                f"⏱ AI pauzada ({rem_sec}s qoldi)."
            )
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✍️ Javob qaytarish", callback_data=f"adm_takeover:{user.id}")],
                [InlineKeyboardButton(text="🤖 AI ni yoqish", callback_data=f"adm_resume_ai:{user.id}")]
            ])
            for aid in admin_ids:
                try:
                    await bot.send_message(aid, adm_notify, reply_markup=kb)
                except Exception:
                    pass
            return

        # AI javobini generatsiya qilish
        db.log_request(user.id, "ai_chat")
        await bot.send_chat_action(message.chat.id, "typing")
        reply = await asyncio.to_thread(generate_ai_reply, user.id, text)
        await record_conversation(user.id, "bot", reply)
        await message.answer(reply, reply_markup=get_chat_keyboard(is_adm))

        # Xotirani vaqti-vaqti bilan yangilash (arzon model, fonda)
        if db.count_user_msgs_total(user.id) % MEMORY_EVERY == 0:
            asyncio.create_task(asyncio.to_thread(update_memory_sync, user.id))
