import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, List, Optional

DB_PATH = Path(__file__).parent / "bot_database.db"


def init_db():
    """Baza va jadvallarni yaratish."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # Foydalanuvchilar jadvali
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            last_name TEXT,
            joined_at TEXT,
            last_active TEXT
        )
    """)

    # So'rovlar (ovoz qayta ishlash) statistikasi
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            request_type TEXT,
            created_at TEXT
        )
    """)

    conn.commit()
    conn.close()


def add_or_update_user(user_id: int, username: Optional[str], first_name: Optional[str], last_name: Optional[str]):
    """Foydalanuvchi ma'lumotlarini saqlash yoki yangilash."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    cursor.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()

    if row:
        cursor.execute("""
            UPDATE users 
            SET username = ?, first_name = ?, last_name = ?, last_active = ?
            WHERE user_id = ?
        """, (username, first_name, last_name, now_str, user_id))
    else:
        cursor.execute("""
            INSERT INTO users (user_id, username, first_name, last_name, joined_at, last_active)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (user_id, username, first_name, last_name, now_str, now_str))

    conn.commit()
    conn.close()


def log_request(user_id: int, request_type: str = "voice_reverse"):
    """Bajarilgan so'rovni qayd qilish."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    cursor.execute("""
        INSERT INTO requests (user_id, request_type, created_at)
        VALUES (?, ?, ?)
    """, (user_id, request_type, now_str))

    conn.commit()
    conn.close()


def get_stats() -> Dict[str, Any]:
    """Admin uchun to'liq statistika."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    today_str = datetime.now().strftime("%Y-%m-%d")

    cursor.execute("SELECT COUNT(*) FROM users")
    total_users = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM requests")
    total_requests = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM requests WHERE created_at LIKE ?", (f"{today_str}%",))
    today_requests = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(DISTINCT user_id) FROM requests WHERE created_at LIKE ?", (f"{today_str}%",))
    today_active_users = cursor.fetchone()[0]

    conn.close()
    return {
        "total_users": total_users,
        "total_requests": total_requests,
        "today_requests": today_requests,
        "today_active_users": today_active_users
    }


def get_recent_users(limit: int = 20) -> List[Dict[str, Any]]:
    """Oxirgi kirgan yoki faol foydalanuvchilar ro'yxati."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute("""
        SELECT user_id, username, first_name, last_name, joined_at, last_active 
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
            "username": r[1],
            "first_name": r[2],
            "last_name": r[3],
            "joined_at": r[4],
            "last_active": r[5]
        })
    return result
