"""
"Chol ovozi" effekti (ovoz.mp4 dagi ovozga moslashtirilgan).

Ishlatish:
    # 1) Oddiy ishlatish (kodda)
    await process_grandpa_voice(Path("in.ogg"), Path("out.ogg"))

    # 2) Ovoz.mp4 ga aniq moslash uchun kalibrlash (bir marta):
    python grandpa_voice.py calibrate dry.wav ovoz.mp4
    -> chiqqan EQ_FILTER qatorini pastdagi EQ_FILTER ga qo'ying.

"dry.wav" - effektsiz, oddiy ovozli yozuv (xuddi shu odam, effektdan oldingi).
"""
import asyncio
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np

PITCH = 0.84  # 0.84 ~ -3 yarim ton (F0: ~120 Hz -> ~100 Hz)

BASE_FILTER = ",".join([
    "aresample=48000",
    f"asetrate=48000*{PITCH}",     # tovush balandligini tushirish
    "aresample=48000",             # sample rate ni qaytarish
    f"atempo={1 / PITCH:.5f}",     # tezlikni asliga qaytarish
    "vibrato=f=6.5:d=0.15",        # yengil titroq (kuchli tremolo olib tashlandi)
    "highpass=f=70",               # F0 (~100 Hz) saqlansin, faqat shovqin kesiladi
    "lowpass=f=2800:p=2",          # yuqori chastotalar yumshoq kesiladi
    "bass=g=2:f=200",              # past chastotalarni biroz kuchaytirish
    "equalizer=f=1100:t=q:w=1.0:g=-3",
])

# calibrate natijasini shu yerga qo'ying (bo'sh bo'lsa ishlatilmaydi)
EQ_FILTER = ""


def build_filter() -> str:
    parts = [BASE_FILTER]
    if EQ_FILTER:
        parts.append(EQ_FILTER)
    parts.append("alimiter=limit=0.95")
    return ",".join(parts)


async def process_grandpa_voice(input_path: Path, output_path: Path) -> None:
    cmd = [
        "ffmpeg", "-y",
        "-i", str(input_path),
        "-vn",
        "-af", build_filter(),
        "-c:a", "libopus",
        "-b:a", "32k",
        "-vbr", "on",
        str(output_path),
    ]
    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await process.communicate()
    if process.returncode != 0:
        raise RuntimeError(f"FFmpeg xatosi: {stderr.decode(errors='replace')}")


# ---------------------------------------------------------------- kalibrlash

CENTERS = [100, 125, 160, 200, 250, 315, 400, 500, 630, 800, 1000,
           1250, 1600, 2000, 2500, 3150, 4000]


def _to_wav16k(src: Path, dst: Path, af: str | None = None) -> None:
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(src), "-vn"]
    if af:
        cmd += ["-af", af]
    cmd += ["-ac", "1", "-ar", "16000", str(dst)]
    subprocess.run(cmd, check=True)


def _ltas(path: Path) -> np.ndarray:
    """Faol (ovozli) kadrlar bo'yicha o'rtacha spektr, 1/3-oktava, dB."""
    import wave
    with wave.open(str(path), "rb") as wf:
        sr = wf.getframerate()
        n = wf.getnframes()
        data = wf.readframes(n)
        x = np.frombuffer(data, dtype=np.int16).astype(np.float64) / 32768.0

    n, h = 2048, 512
    win = np.hanning(n)
    frames = [x[i:i + n] * win for i in range(0, len(x) - n, h)]
    spec = np.array([np.abs(np.fft.rfft(f)) ** 2 for f in frames])
    e = spec.sum(1)
    keep = e > e.max() * 10 ** (-25 / 10)  # jim joylar hisobga olinmaydi
    p = spec[keep].mean(0)
    f = np.fft.rfftfreq(n, 1 / sr)
    out = []
    for c in CENTERS:
        m = (f >= c / 2 ** (1 / 6)) & (f < c * 2 ** (1 / 6))
        out.append(10 * np.log10(p[m].mean() + 1e-20))
    out = np.array(out)
    return out - out[:8].mean()  # 100-500 Hz ga nisbatan


def calibrate(dry: Path, target: Path) -> None:
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        tgt = td / "t.wav"
        _to_wav16k(target, tgt)
        t_ltas = _ltas(tgt)

        cur = td / "c.wav"
        _to_wav16k(dry, cur, BASE_FILTER)
        diff = t_ltas - _ltas(cur)
        diff = np.clip(diff, -12, 12)
        diff = np.convolve(np.pad(diff, 1, mode="edge"), [0.25, 0.5, 0.25], "valid")

        eq = ",".join(
            f"equalizer=f={c}:t=o:w=0.33:g={g:.1f}"
            for c, g in zip(CENTERS, diff) if abs(g) >= 1.0
        )
        # tekshirish
        chk = td / "k.wav"
        _to_wav16k(dry, chk, BASE_FILTER + ("," + eq if eq else ""))
        resid = t_ltas - _ltas(chk)

        print("Chastota  Maqsad   Hozirgi  Tuzatish  Qoldiq (dB)")
        before = t_ltas - _ltas(cur)
        for c, tl, b, d, r in zip(CENTERS, t_ltas, before, diff, resid):
            print(f"{c:6d} Hz {tl:7.1f} {tl - b:8.1f} {d:9.1f} {r:8.1f}")
        print(f"\nO'rtacha xato: {np.abs(before).mean():.1f} dB -> {np.abs(resid).mean():.1f} dB")
        print("\nEQ_FILTER = " + repr(eq))


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "calibrate":
        calibrate(Path(sys.argv[2]), Path(sys.argv[3]))
    elif len(sys.argv) == 3:
        asyncio.run(process_grandpa_voice(Path(sys.argv[1]), Path(sys.argv[2])))
    else:
        print(__doc__)
