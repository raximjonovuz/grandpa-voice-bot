import re
import logging
from typing import Optional, Dict, Any

from aiogram import Router, F, Bot
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton
)
from aiogram.filters import Command
from aiogram.exceptions import TelegramBadRequest

import db
from config import (
    is_admin_user,
    VIP_PRESETS,
    PERSONA_INSTRUCTIONS,
    ADMIN_USERNAMES,
    ADMIN_IDS
)
from services.ai_service import chat_history, record_conversation

logger = logging.getLogger("AdminHandlers")
admin_router = Router()

# admin_id -> {"action": "takeover_reply" | "set_vip_uid" | "set_vip_uname" | "set_vip_uname_rule" | "set_custom_global", ...}
admin_action_state: Dict[int, Dict[str, Any]] = {}


async def safe_edit_or_send(callback: CallbackQuery, text: str, reply_markup: Optional[InlineKeyboardMarkup] = None):
    """Tugma bosilganda xabarni xatosiz va tez yangilash."""
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


def get_admin_panel_keyboard() -> InlineKeyboardMarkup:
    """Admin panel asosiy inline tugmalari."""
    current_persona = db.get_setting("ai_persona", "kocha")
    p_label = current_persona.upper()
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
    """AI uchun suhbat xarakterlari tanlash klaviaturasi."""
    current = db.get_setting("ai_persona", "kocha")
    modes = [
        ("kocha", "Ko'cha (Jo'ra / Erkin hazil)"),
        ("sarkazm", "O'tkir Sarkazm va Roast"),
        ("dost", "Yaqin Do'st (Mehrli / Dardkash)"),
        ("rasmiy", "Rasmiy / Jiddiy ekspert"),
        ("faylasuf", "Dono Faylasuf")
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
        [InlineKeyboardButton(text="🌸 Maxsus Mehmon (Qiz bola / Nozik muomala)", callback_data=f"vip_pre:special_guest:{target_ref}")],
        [InlineKeyboardButton(text="🛑 Hazil qilma (Boss ruxsat bermagan, hurmat qil)", callback_data=f"vip_pre:no_joke:{target_ref}")],
        [InlineKeyboardButton(text="🤝 Eng yaqin jigar / VIP do'st", callback_data=f"vip_pre:best_friend:{target_ref}")],
        [InlineKeyboardButton(text="🧠 Faqat jiddiy va aqlli javob ber", callback_data=f"vip_pre:serious_expert:{target_ref}")],
        [InlineKeyboardButton(text="🔥 Qattiq hazil / Roast qil", callback_data=f"vip_pre:hard_roast:{target_ref}")],
        [InlineKeyboardButton(text="🗑 VIP qoidani o'chirish", callback_data=f"vip_pre:clear:{target_ref}")],
        [InlineKeyboardButton(text="⬅️ Orqaga", callback_data=back_cb)]
    ])


def _build_admin_dashboard_text() -> str:
    stats = db.get_stats()
    persona = db.get_setting("ai_persona", "kocha").upper()
    custom_global = db.get_setting("custom_global_prompt", "")
    global_note = f"\n📌 Global qo'shimcha: «{custom_global[:50]}...»" if custom_global else ""

    return (
        f"👑 Boshqaruv Markazi (Admin Panel):\n\n"
        f"• Faol AI rejimi: {persona}{global_note}\n"
        f"• Jami a'zolar: {stats['total_users']} ta\n"
        f"• Bugun yangi: {stats['today_new_users']} ta | Faol: {stats['today_active_users']} ta\n"
        f"• Jami so'rovlar: {stats['total_requests']} ta\n\n"
        f"Pastdagi tugmalar orqali kerakli bo'limni tanlang:"
    )


async def render_user_profile_view(callback: CallbackQuery, target_uid: int):
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


# --- Handlers ---

@admin_router.message(Command("admin"))
@admin_router.message(F.text == "👑 Admin Panel")
async def handle_admin_cmd(message: Message):
    user = message.from_user
    db.add_or_update_user(user.id, user.username, user.first_name, user.last_name)

    if not is_admin_user(user.username, user.id):
        await message.answer("⛔ Bu bo'lim faqat bot adminlari uchun.")
        return

    admin_action_state.pop(user.id, None)
    await message.answer(_build_admin_dashboard_text(), reply_markup=get_admin_panel_keyboard())


@admin_router.callback_query(F.data == "adm_back")
async def cb_adm_back(callback: CallbackQuery):
    if not is_admin_user(callback.from_user.username, callback.from_user.id):
        await callback.answer("⛔ Faqat admin uchun!", show_alert=True)
        return
    await callback.answer()
    admin_action_state.pop(callback.from_user.id, None)
    await safe_edit_or_send(callback, _build_admin_dashboard_text(), reply_markup=get_admin_panel_keyboard())


@admin_router.callback_query(F.data == "adm_stats")
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


@admin_router.callback_query(F.data == "adm_personas")
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


@admin_router.callback_query(F.data.startswith("set_persona:"))
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


@admin_router.callback_query(F.data.startswith("adm_users:"))
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


@admin_router.callback_query(F.data.startswith("adm_view_u:"))
async def cb_adm_view_user(callback: CallbackQuery):
    if not is_admin_user(callback.from_user.username, callback.from_user.id):
        await callback.answer("⛔ Faqat admin uchun!", show_alert=True)
        return
    await callback.answer()
    admin_action_state.pop(callback.from_user.id, None)
    target_uid = int(callback.data.split(":", 1)[1])
    await render_user_profile_view(callback, target_uid)


@admin_router.callback_query(F.data.startswith("adm_takeover:"))
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


@admin_router.callback_query(F.data.startswith("adm_resume_ai:"))
async def cb_adm_resume_ai(callback: CallbackQuery):
    if not is_admin_user(callback.from_user.username, callback.from_user.id):
        await callback.answer("⛔ Faqat admin uchun!", show_alert=True)
        return
    target_uid = int(callback.data.split(":", 1)[1])
    db.resume_ai_for_user(target_uid)
    admin_action_state.pop(callback.from_user.id, None)
    await callback.answer("🤖 AI ushbu foydalanuvchi uchun qayta yoqildi!", show_alert=True)
    await render_user_profile_view(callback, target_uid)


@admin_router.callback_query(F.data.startswith("adm_vip_u:"))
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


@admin_router.callback_query(F.data.startswith("vip_pre:"))
async def cb_vip_preset_apply(callback: CallbackQuery):
    if not is_admin_user(callback.from_user.username, callback.from_user.id):
        await callback.answer("⛔ Faqat admin uchun!", show_alert=True)
        return

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
        await render_user_profile_view(callback, target_uid)
    else:
        target_uname = ref_val
        db.set_vip_by_username(target_uname, rule_text)
        msg = f"🗑 @{target_uname} uchun VIP qoida o'chirildi!" if preset_key == "clear" else f"✅ @{target_uname} uchun VIP muomala saqlandi!"
        await callback.answer(msg, show_alert=True)
        await safe_edit_or_send(callback, f"{msg}\n\n{_build_admin_dashboard_text()}", reply_markup=get_admin_panel_keyboard())


@admin_router.callback_query(F.data == "adm_add_vip")
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


@admin_router.callback_query(F.data == "adm_vip_list")
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


@admin_router.callback_query(F.data == "adm_custom_global")
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


@admin_router.callback_query(F.data == "adm_clear_global")
async def cb_adm_clear_global(callback: CallbackQuery):
    if not is_admin_user(callback.from_user.username, callback.from_user.id):
        await callback.answer("⛔ Faqat admin uchun!", show_alert=True)
        return
    db.set_setting("custom_global_prompt", "")
    admin_action_state.pop(callback.from_user.id, None)
    await callback.answer("✅ Global qo'shimcha qoida tozalandi!", show_alert=True)
    await safe_edit_or_send(callback, _build_admin_dashboard_text(), reply_markup=get_admin_panel_keyboard())
