import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

import cf_storage

DB_PATH = Path(__file__).parent / "bot_database.db"


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=15.0)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn


def _async_cloud_backup():
    """Foydalanuvchilar, VIP qoidalar va sozlamalarni Cloudflare KV ga fonda saqlash."""
    def _worker():
        try:
            conn = _get_conn()
            cur = conn.cursor()
            cur.execute("""
                SELECT user_id, username, first_name, last_name, mode, chat_step,
                       user_age, user_custom_name, memory_notes, vip_instruction,
                       admin_paused_until, joined_at, last_active
                FROM users
            """)
            users_data = [
                {
                    "user_id": r[0], "username": r[1], "first_name": r[2], "last_name": r[3],
                    "mode": r[4], "chat_step": r[5], "user_age": r[6], "user_custom_name": r[7],
                    "memory_notes": r[8], "vip_instruction": r[9], "admin_paused_until": r[10],
                    "joined_at": r[11], "last_active": r[12]
                }
                for r in cur.fetchall()
            ]
            cur.execute("SELECT username, instruction, created_at FROM vip_rules")
            vips_data = [{"username": r[0], "instruction": r[1], "created_at": r[2]} for r in cur.fetchall()]
            cur.execute("SELECT key, value FROM settings")
            settings_data = {r[0]: r[1] for r in cur.fetchall()}
            conn.close()

            cf_storage.kv_put_json("bot_state_v1", {
                "users": users_data,
                "vip_rules": vips_data,
                "settings": settings_data,
                "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            }, with_ttl=False)
        except Exception:
            pass

    threading.Thread(target=_worker, daemon=True).start()


def restore_from_cloud_if_needed():
    """Server qayta ishga tushganda Cloudflare KV dan foydalanuvchilar va sozlamalarni tiklash."""
    try:
        state = cf_storage.kv_get_json("bot_state_v1")
        if not isinstance(state, dict):
            return
        conn = _get_conn()
        cur = conn.cursor()

        # Settings tiklash
        for k, v in (state.get("settings") or {}).items():
            cur.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (k, v))

        # VIP rules tiklash
        for vr in (state.get("vip_rules") or []):
            cur.execute(
                "INSERT OR REPLACE INTO vip_rules (username, instruction, created_at) VALUES (?, ?, ?)",
                (vr.get("username"), vr.get("instruction"), vr.get("created_at"))
            )

        # Users tiklash (agar mahalliy bazada yo'q bo'lsa)
        for u in (state.get("users") or []):
            cur.execute("SELECT user_id FROM users WHERE user_id = ?", (u["user_id"],))
            if not cur.fetchone():
                cur.execute("""
                    INSERT INTO users (
                        user_id, username, first_name, last_name, mode, chat_step,
                        user_age, user_custom_name, memory_notes, vip_instruction,
                        admin_paused_until, joined_at, last_active
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    u.get("user_id"), u.get("username"), u.get("first_name"), u.get("last_name"),
                    u.get("mode", "chat"), u.get("chat_step", "none"), u.get("user_age"),
                    u.get("user_custom_name"), u.get("memory_notes", ""), u.get("vip_instruction", ""),
                    u.get("admin_paused_until", 0), u.get("joined_at"), u.get("last_active")
                ))

        conn.commit()
        conn.close()
    except Exception:
        pass


def init_db():
    conn = _get_conn()
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            last_name TEXT,
            mode TEXT DEFAULT 'chat',
            chat_step TEXT DEFAULT 'none',
            user_age TEXT,
            user_custom_name TEXT,
            memory_notes TEXT DEFAULT '',
            vip_instruction TEXT DEFAULT '',
            admin_paused_until INTEGER DEFAULT 0,
            joined_at TEXT,
            last_active TEXT
        )
    """)

    new_cols = [
        ("mode", "TEXT DEFAULT 'chat'"),
        ("chat_step", "TEXT DEFAULT 'none'"),
        ("user_age", "TEXT"),
        ("user_custom_name", "TEXT"),
        ("memory_notes", "TEXT DEFAULT ''"),
        ("vip_instruction", "TEXT DEFAULT ''"),
        ("admin_paused_until", "INTEGER DEFAULT 0"),
    ]
    for col_name, col_def in new_cols:
        try:
            cursor.execute(f"ALTER TABLE users ADD COLUMN {col_name} {col_def}")
        except sqlite3.OperationalError:
            pass

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS vip_rules (
            username TEXT PRIMARY KEY,
            instruction TEXT,
            created_at TEXT
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS chat_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            sender TEXT,
            message TEXT,
            created_at TEXT,
            timestamp INTEGER
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            request_type TEXT,
            created_at TEXT
        )
    """)

    cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('ai_persona', 'kocha')")
    cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('custom_global_prompt', '')")

    # Test user 123456 ni tozalash
    cursor.execute("DELETE FROM users WHERE user_id IN (123456, 999)")

    conn.commit()
    conn.close()

    restore_from_cloud_if_needed()
    _async_cloud_backup()


def add_or_update_user(
    user_id: int,
    username: Optional[str],
    first_name: Optional[str],
    last_name: Optional[str]
) -> bool:
    conn = _get_conn()
    cursor = conn.cursor()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    clean_uname = (username or "").lower().strip("@").strip()

    pre_vip = ""
    if clean_uname:
        cursor.execute("SELECT instruction FROM vip_rules WHERE username = ?", (clean_uname,))
        vip_row = cursor.fetchone()
        if vip_row and vip_row[0]:
            pre_vip = vip_row[0]

    cursor.execute("SELECT user_id, vip_instruction FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()

    is_new = False
    if row:
        existing_vip = row[1] or ""
        final_vip = existing_vip or pre_vip
        cursor.execute("""
            UPDATE users 
            SET username = ?, first_name = ?, last_name = ?, last_active = ?, vip_instruction = ?
            WHERE user_id = ?
        """, (username, first_name, last_name, now_str, final_vip, user_id))
    else:
        is_new = True
        cursor.execute("""
            INSERT INTO users (
                user_id, username, first_name, last_name, mode, chat_step,
                vip_instruction, joined_at, last_active
            )
            VALUES (?, ?, ?, ?, 'chat', 'none', ?, ?, ?)
        """, (user_id, username, first_name, last_name, pre_vip, now_str, now_str))

    conn.commit()
    conn.close()
    if is_new:
        _async_cloud_backup()
    return is_new


def get_user(user_id: int) -> Optional[Dict[str, Any]]:
    conn = _get_conn()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT user_id, username, first_name, last_name, mode, chat_step,
               user_custom_name, user_age, memory_notes, vip_instruction,
               admin_paused_until, joined_at, last_active
        FROM users WHERE user_id = ?
    """, (user_id,))
    r = cursor.fetchone()
    conn.close()
    if not r:
        return None
    return {
        "user_id": r[0],
        "username": r[1] or "",
        "first_name": r[2] or "",
        "last_name": r[3] or "",
        "mode": r[4] or "chat",
        "chat_step": r[5] or "none",
        "custom_name": r[6] or "",
        "age": r[7] or "",
        "memory_notes": r[8] or "",
        "vip_instruction": r[9] or "",
        "admin_paused_until": r[10] or 0,
        "joined_at": r[11] or "",
        "last_active": r[12] or ""
    }


def set_user_mode(user_id: int, mode: str):
    conn = _get_conn()
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET mode = ? WHERE user_id = ?", (mode, user_id))
    conn.commit()
    conn.close()


def get_user_mode(user_id: int) -> str:
    conn = _get_conn()
    cursor = conn.cursor()
    cursor.execute("SELECT mode FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    if row and row[0]:
        return row[0]
    return "chat"


def set_chat_state(
    user_id: int,
    step: str,
    custom_name: Optional[str] = None,
    age: Optional[str] = None
):
    conn = _get_conn()
    cursor = conn.cursor()
    if custom_name is not None and age is not None:
        cursor.execute(
            "UPDATE users SET chat_step = ?, user_custom_name = ?, user_age = ? WHERE user_id = ?",
            (step, custom_name, age, user_id)
        )
    elif custom_name is not None:
        cursor.execute(
            "UPDATE users SET chat_step = ?, user_custom_name = ? WHERE user_id = ?",
            (step, custom_name, user_id)
        )
    elif age is not None:
        cursor.execute(
            "UPDATE users SET chat_step = ?, user_age = ? WHERE user_id = ?",
            (step, age, user_id)
        )
    else:
        cursor.execute("UPDATE users SET chat_step = ? WHERE user_id = ?", (step, user_id))
    conn.commit()
    conn.close()
    _async_cloud_backup()


def get_chat_state(user_id: int) -> Dict[str, Any]:
    conn = _get_conn()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT chat_step, user_custom_name, user_age, memory_notes, vip_instruction FROM users WHERE user_id = ?",
        (user_id,)
    )
    row = cursor.fetchone()
    conn.close()
    if row:
        return {
            "step": row[0] or "none",
            "name": row[1] or "",
            "age": row[2] or "",
            "memory": row[3] or "",
            "vip": row[4] or ""
        }
    return {"step": "none", "name": "", "age": "", "memory": "", "vip": ""}


def update_user_memory(user_id: int, new_note: str):
    conn = _get_conn()
    cursor = conn.cursor()
    cursor.execute("SELECT memory_notes FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    current = (row[0] or "").strip() if row else ""
    if new_note and new_note not in current:
        updated = (current + "; " + new_note).strip("; ")
        if len(updated) > 500:
            updated = updated[-500:]
        cursor.execute("UPDATE users SET memory_notes = ? WHERE user_id = ?", (updated, user_id))
        conn.commit()
    conn.close()
    _async_cloud_backup()


# --- VIP / Maxsus muomala funksiyalari ---

def set_vip_by_user_id(user_id: int, instruction: str):
    conn = _get_conn()
    cursor = conn.cursor()
    cursor.execute("SELECT username FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    uname = (row[0] or "").lower().strip("@").strip() if row else ""

    cursor.execute("UPDATE users SET vip_instruction = ? WHERE user_id = ?", (instruction.strip(), user_id))
    if uname:
        if instruction.strip():
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            cursor.execute(
                "INSERT OR REPLACE INTO vip_rules (username, instruction, created_at) VALUES (?, ?, ?)",
                (uname, instruction.strip(), now_str)
            )
        else:
            cursor.execute("DELETE FROM vip_rules WHERE username = ?", (uname,))

    conn.commit()
    conn.close()
    _async_cloud_backup()


def set_vip_by_username(username: str, instruction: str) -> Optional[int]:
    clean_uname = username.lower().strip("@").strip()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = _get_conn()
    cursor = conn.cursor()

    if instruction.strip():
        cursor.execute(
            "INSERT OR REPLACE INTO vip_rules (username, instruction, created_at) VALUES (?, ?, ?)",
            (clean_uname, instruction.strip(), now_str)
        )
    else:
        cursor.execute("DELETE FROM vip_rules WHERE username = ?", (clean_uname,))

    cursor.execute("SELECT user_id FROM users WHERE LOWER(username) = ?", (clean_uname,))
    row = cursor.fetchone()
    found_uid = None
    if row:
        found_uid = row[0]
        cursor.execute("UPDATE users SET vip_instruction = ? WHERE user_id = ?", (instruction.strip(), found_uid))

    conn.commit()
    conn.close()
    _async_cloud_backup()
    return found_uid


def get_all_vip_rules() -> List[Dict[str, Any]]:
    conn = _get_conn()
    cursor = conn.cursor()
    cursor.execute("SELECT username, instruction, created_at FROM vip_rules ORDER BY created_at DESC")
    rows = cursor.fetchall()
    conn.close()
    return [{"username": r[0], "instruction": r[1], "created_at": r[2]} for r in rows]


# --- Live Takeover (Admin 10 daqiqa suhbatga aralashishi) ---

def pause_ai_for_user(user_id: int, minutes: int = 10):
    until_ts = int(time.time()) + (minutes * 60)
    conn = _get_conn()
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET admin_paused_until = ? WHERE user_id = ?", (until_ts, user_id))
    conn.commit()
    conn.close()


def resume_ai_for_user(user_id: int):
    conn = _get_conn()
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET admin_paused_until = 0 WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()


def is_ai_paused(user_id: int) -> Tuple[bool, int]:
    conn = _get_conn()
    cursor = conn.cursor()
    cursor.execute("SELECT admin_paused_until FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    if not row or not row[0]:
        return False, 0
    now_ts = int(time.time())
    if row[0] > now_ts:
        return True, row[0] - now_ts
    return False, 0


# --- Global AI Sozlamalari ---

def set_setting(key: str, value: str):
    conn = _get_conn()
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
    conn.commit()
    conn.close()
    _async_cloud_backup()


def get_setting(key: str, default: str = "") -> str:
    conn = _get_conn()
    cursor = conn.cursor()
    cursor.execute("SELECT value FROM settings WHERE key = ?", (key,))
    row = cursor.fetchone()
    conn.close()
    if row and row[0] is not None:
        return row[0]
    return default


# --- Suhbat Tarixi (72 soat / 3 kun) ---

def log_chat_message(user_id: int, sender: str, message_text: str):
    now_ts = int(time.time())
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cutoff_ts = now_ts - 259200  # 72 soat

    conn = _get_conn()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM chat_logs WHERE timestamp < ?", (cutoff_ts,))
    cursor.execute("""
        INSERT INTO chat_logs (user_id, sender, message, created_at, timestamp)
        VALUES (?, ?, ?, ?, ?)
    """, (user_id, sender, message_text, now_str, now_ts))
    conn.commit()
    conn.close()


def get_local_chat_logs(user_id: int, limit: int = 30) -> List[Dict[str, Any]]:
    now_ts = int(time.time())
    cutoff_ts = now_ts - 259200
    conn = _get_conn()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT sender, message, created_at 
        FROM chat_logs 
        WHERE user_id = ? AND timestamp >= ?
        ORDER BY id DESC 
        LIMIT ?
    """, (user_id, cutoff_ts, limit))
    rows = cursor.fetchall()
    conn.close()
    rows.reverse()
    return [{"sender": r[0], "text": r[1], "time": r[2]} for r in rows]


# --- Statistika ---

def log_request(user_id: int, request_type: str = "ai_chat"):
    conn = _get_conn()
    cursor = conn.cursor()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cursor.execute("""
        INSERT INTO requests (user_id, request_type, created_at)
        VALUES (?, ?, ?)
    """, (user_id, request_type, now_str))
    conn.commit()
    conn.close()


def get_stats() -> Dict[str, Any]:
    conn = _get_conn()
    cursor = conn.cursor()
    today_str = datetime.now().strftime("%Y-%m-%d")

    cursor.execute("SELECT COUNT(*) FROM users")
    total_users = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM users WHERE joined_at LIKE ?", (f"{today_str}%",))
    today_new_users = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM requests")
    total_requests = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM requests WHERE created_at LIKE ?", (f"{today_str}%",))
    today_requests = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(DISTINCT user_id) FROM requests WHERE created_at LIKE ?", (f"{today_str}%",))
    today_active_users = cursor.fetchone()[0]

    conn.close()
    return {
        "total_users": total_users,
        "today_new_users": today_new_users,
        "total_requests": total_requests,
        "today_requests": today_requests,
        "today_active_users": today_active_users
    }


def get_recent_users(limit: int = 20) -> List[Dict[str, Any]]:
    conn = _get_conn()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT user_id, username, first_name, last_name, user_custom_name, user_age,
               vip_instruction, joined_at, last_active 
        FROM users 
        ORDER BY last_active DESC 
        LIMIT ?
    """, (limit,))
    rows = cursor.fetchall()
    conn.close()

    result = []
    for r in rows:
        result.append({
            "user_id": r[0],
            "username": r[1] or "",
            "first_name": r[2] or "",
            "last_name": r[3] or "",
            "custom_name": r[4] or "",
            "age": r[5] or "",
            "vip_instruction": r[6] or "",
            "joined_at": r[7] or "",
            "last_active": r[8] or ""
        })
    return result


def get_admin_chat_ids(admin_usernames: List[str], admin_ids: Optional[set] = None) -> List[int]:
    conn = _get_conn()
    cursor = conn.cursor()
    placeholders = ",".join(["?"] * len(admin_usernames))
    cursor.execute(
        f"SELECT user_id FROM users WHERE LOWER(username) IN ({placeholders})",
        [u.lower().strip("@") for u in admin_usernames]
    )
    rows = cursor.fetchall()
    conn.close()
    ids = {r[0] for r in rows}
    if admin_ids:
        ids.update(admin_ids)
    return list(ids)
