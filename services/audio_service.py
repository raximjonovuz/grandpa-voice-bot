import asyncio
import logging
import re
import urllib.request
import urllib.error
from pathlib import Path
from config import GROQ_API_KEY, CONCURRENCY_LIMIT
from grandpa_voice import process_grandpa_voice

logger = logging.getLogger("AudioService")
processing_semaphore = asyncio.Semaphore(CONCURRENCY_LIMIT)


async def convert_to_mp3(input_path: Path, output_mp3: Path) -> None:
    async with processing_semaphore:
        cmd = [
            "ffmpeg", "-y",
            "-i", str(input_path),
            "-ar", "16000", "-ac", "1", "-b:a", "64k",
            str(output_mp3)
        ]
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        await process.communicate()


def refine_stt_text(raw_text: str, chat_func) -> str:
    """
    TOKEN TEJASH: Faqat Whisper kirill/qozoq harflarida yozib yuborgan taqdirdagina
    AI orqali toza O'zbek lotin alifbosiga o'giradi. Agar matn allaqachon lotin bo'lsa,
    AI chaqirilmaydi (0 token sarfi!).
    """
    if not raw_text or raw_text in ("Nutq aniqlanmadi.", "Ovozdagi nutqni aniqlashda xatolik yuz berdi."):
        return raw_text

    has_cyrillic = bool(re.search(r"[а-яА-ЯёЁқңәөұүіғһҚҢӘӨҰҮІҒҺ]", raw_text))
    if not has_cyrillic and "Gya" not in raw_text:
        return raw_text

    sys_prompt = (
        "STT tahrirchi: Qozoq/kirill yoki xato fonetikada yozilgan o'zbekcha nutqni "
        "toza O'ZBEK LOTIN alifbosiga o'gir ('мәтіннің хатау тәржемі қылып сан'->'Matnni xato tarjima qilyapsan'). "
        "Haqiqiy rus/ingliz gap bo'lsa o'z holicha qoldir. Faqat tayyor matnni qaytar, izoh yozma."
    )
    try:
        fixed = chat_func(
            [
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": raw_text}
            ],
            temperature=0.1,
            max_tokens=150
        )
        return fixed.strip().strip('"').strip("«»") or raw_text
    except Exception as e:
        logger.warning(f"STT post-correction xatosi: {e}")
        return raw_text


def transcribe_with_groq_whisper(audio_file_path: Path) -> str:
    """Groq Whisper large-v3 + Uzbek kontekst + tejamkor fonetik tahrirlash."""
    url = "https://api.groq.com/openai/v1/audio/transcriptions"
    boundary = "----WebKitFormBoundaryGroqWhisperUz"

    with open(audio_file_path, "rb") as f:
        file_bytes = f.read()

    whisper_prompt = (
        "Assalomu alaykum, salom hammaga! Bu o'zbek tilidagi ovozli xabar. "
        "O'zbek lotin alifbosida aniq va to'g'ri yoz."
    )

    data_parts = [
        f"--{boundary}".encode(),
        b'Content-Disposition: form-data; name="model"\r\n',
        b"whisper-large-v3",
        f"--{boundary}".encode(),
        b'Content-Disposition: form-data; name="language"\r\n',
        b"uz",
        f"--{boundary}".encode(),
        b'Content-Disposition: form-data; name="prompt"\r\n',
        whisper_prompt.encode("utf-8"),
        f"--{boundary}".encode(),
        b'Content-Disposition: form-data; name="temperature"\r\n',
        b"0",
        f"--{boundary}".encode(),
        b'Content-Disposition: form-data; name="file"; filename="audio.mp3"',
        b"Content-Type: audio/mpeg\r\n",
        file_bytes,
        f"--{boundary}--\r\n".encode()
    ]
    body = b"\r\n".join(data_parts)

    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Authorization", f"Bearer {GROQ_API_KEY}")
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    req.add_header("User-Agent", "Mozilla/5.0")

    try:
        with urllib.request.urlopen(req, timeout=14) as resp:
            import json
            data = json.loads(resp.read().decode("utf-8"))
            return data.get("text", "").strip() or "Nutq aniqlanmadi."
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        logger.error(f"Groq Whisper HTTP xatosi ({e.code}): {err_body}")
        return "Ovozdagi nutqni aniqlashda xatolik yuz berdi."
    except Exception as e:
        logger.error(f"Groq Whisper xatosi: {e}")
        return "Ovozdagi nutqni aniqlashda xatolik yuz berdi."
