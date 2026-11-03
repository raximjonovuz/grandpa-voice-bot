import json
import logging
import os
import urllib.request
import urllib.error
from datetime import datetime
from typing import List, Dict, Any, Optional
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("CloudflareKV")

# 3 kun = 72 soat = 259200 soniya
TTL_SECONDS = 259200
MAX_MESSAGES_PER_USER = 60


def _get_cf_config():
    account_id = os.getenv("CF_ACCOUNT_ID", "d69a2f6a87f30ea285e75252a28a2ff4").strip()
    api_token = os.getenv("CF_API_TOKEN", "").strip()
    namespace_id = os.getenv("CF_KV_NAMESPACE_ID", "fba10af05f7d435aaa747f816ed10895").strip()
    return account_id, api_token, namespace_id


def _kv_url(key: str, with_ttl: bool = False) -> str:
    account_id, _, namespace_id = _get_cf_config()
    base = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/storage/kv/namespaces/{namespace_id}/values/{key}"
    if with_ttl:
        return f"{base}?expiration_ttl={TTL_SECONDS}"
    return base


def kv_get_json(key: str) -> Optional[Any]:
    _, api_token, namespace_id = _get_cf_config()
    if not api_token or not namespace_id:
        return None

    url = _kv_url(key, with_ttl=False)
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {api_token}",
            "User-Agent": "Mozilla/5.0"
        },
        method="GET"
    )
    try:
        with urllib.request.urlopen(req, timeout=4) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        logger.warning(f"CF KV read HTTP xatosi ({key}, {e.code}): {e}")
        return None
    except Exception as e:
        logger.warning(f"CF KV read xatosi ({key}): {e}")
        return None


def kv_put_json(key: str, data: Any, with_ttl: bool = False) -> bool:
    _, api_token, namespace_id = _get_cf_config()
    if not api_token or not namespace_id:
        return False

    url = _kv_url(key, with_ttl=with_ttl)
    body = json.dumps(data, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {api_token}",
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0"
        },
        method="PUT"
    )
    try:
        with urllib.request.urlopen(req, timeout=4) as resp:
            resp.read()
        return True
    except Exception as e:
        logger.warning(f"CF KV write xatosi ({key}): {e}")
        return False


def get_kv_chat_logs(user_id: int) -> List[Dict[str, Any]]:
    """Cloudflare KV dan foydalanuvchining 72 soatlik suhbat tarixini olish."""
    data = kv_get_json(f"chat_{user_id}")
    if isinstance(data, list):
        return data
    return []


def append_kv_chat_log(user_id: int, sender: str, text: str) -> None:
    """Cloudflare KV ga yangi xabarni qo'shish (72 soatlik TTL bilan)."""
    logs = get_kv_chat_logs(user_id)
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    logs.append({
        "sender": sender,
        "text": text,
        "time": now_str
    })
    if len(logs) > MAX_MESSAGES_PER_USER:
        logs = logs[-MAX_MESSAGES_PER_USER:]
    kv_put_json(f"chat_{user_id}", logs, with_ttl=True)
