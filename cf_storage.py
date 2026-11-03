import json
import logging
import os
import urllib.request
import urllib.error
from datetime import datetime
from typing import List, Dict, Any

logger = logging.getLogger("CloudflareKV")

CF_ACCOUNT_ID = os.getenv("CF_ACCOUNT_ID", "d69a2f6a87f30ea285e75252a28a2ff4").strip()
CF_API_TOKEN = os.getenv("CF_API_TOKEN", "").strip()
CF_KV_NAMESPACE_ID = os.getenv("CF_KV_NAMESPACE_ID", "fba10af05f7d435aaa747f816ed10895").strip()

# 3 kun = 72 soat = 259200 soniya
TTL_SECONDS = 259200
MAX_MESSAGES_PER_USER = 60


def _kv_url(key: str, with_ttl: bool = False) -> str:
    base = f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}/storage/kv/namespaces/{CF_KV_NAMESPACE_ID}/values/{key}"
    if with_ttl:
        return f"{base}?expiration_ttl={TTL_SECONDS}"
    return base


def get_kv_chat_logs(user_id: int) -> List[Dict[str, Any]]:
    """Cloudflare KV dan foydalanuvchining 72 soatlik suhbat tarixini olish."""
    if not CF_API_TOKEN or not CF_KV_NAMESPACE_ID:
        return []

    url = _kv_url(f"chat_{user_id}")
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {CF_API_TOKEN}",
            "User-Agent": "Mozilla/5.0"
        },
        method="GET"
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            raw = resp.read().decode("utf-8")
            data = json.loads(raw)
            if isinstance(data, list):
                return data
            return []
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return []
        logger.warning(f"CF KV read HTTP xatosi ({e.code}): {e}")
        return []
    except Exception as e:
        logger.warning(f"CF KV read xatosi: {e}")
        return []


def append_kv_chat_log(user_id: int, sender: str, text: str) -> None:
    """Cloudflare KV ga yangi xabarni qo'shish (72 soatlik TTL bilan)."""
    if not CF_API_TOKEN or not CF_KV_NAMESPACE_ID:
        return

    logs = get_kv_chat_logs(user_id)
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    logs.append({
        "sender": sender,
        "text": text,
        "time": now_str
    })
    # Oxirgi MAX_MESSAGES_PER_USER xabarni saqlaymiz
    if len(logs) > MAX_MESSAGES_PER_USER:
        logs = logs[-MAX_MESSAGES_PER_USER:]

    url = _kv_url(f"chat_{user_id}", with_ttl=True)
    body = json.dumps(logs, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {CF_API_TOKEN}",
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0"
        },
        method="PUT"
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            resp.read()
    except Exception as e:
        logger.warning(f"CF KV write xatosi: {e}")
