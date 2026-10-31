# 👴 Instagram Grandpa (Boboy) Ovoz Effekti Telegram Boti

Ushbu bot Telegram orqali yuborilgan ovozli xabarlarni (`.ogg` / Opus) qabul qilib, ularni **FFmpeg DSP filtri** orqali Instagramdagi mashhur **"Grandpa" (Boboy)** ovoz effektiga aylantiradi va foydalanuvchiga Telegram voice note shaklida qaytaradi.

---

## 🚀 Texnologiyalar to'plami
- **Python 3.11+**
- **aiogram 3.x** (Asinxron Telegram Bot framework)
- **FFmpeg** (Ovoz chastotasi, tremolo, vibrato va equalizer DSP filtrlari)
- **aiohttp** (Render yoki boshqa bulutli hostinglar uchun 24/7 bepul Health-check veb-serveri)
- **Docker & Docker Compose** (Konteynerlashtirish)

---

## 📁 Loyiha tarkibi
```text
Bot/
├── main.py              # Asosiy bot mantig'i va FFmpeg DSP pipeline
├── requirements.txt     # Python kutubxonalari
├── Dockerfile           # FFmpeg o'rnatilgan Docker container
├── docker-compose.yml   # Docker compose konfiguratsiyasi
├── render.yaml          # Render.com bepul servisi uchun 1-bosqichli konfiguratsiya
├── .env                 # Bot token (maxfiy)
├── .gitignore           # Git uchun istisnolar
└── README.md            # Qo'llanma
```

---

## 🛠 1. Lokal kompyuterda ishga tushirish (Windows / Linux / Mac)

### 1-qadam: FFmpeg o'rnatilganligini tekshirish
Terminalda tekshiring:
```bash
ffmpeg -version
```
Agar o'rnatilmagan bo'lsa:
- **Windows**: `winget install Gyan.FFmpeg` yoki [ffmpeg.org](https://ffmpeg.org/download.html) dan yuklab olib, PATH ga qo'shing.
- **Linux (Ubuntu/Debian)**: `sudo apt-get install -y ffmpeg`
- **macOS**: `brew install ffmpeg`

### 2-qadam: Kutubxonalarni o'rnatish
```bash
pip install -r requirements.txt
```

### 3-qadam: .env faylini sozlash
`.env` faylida o'zingizning bot tokeningizni saqlang:
```env
BOT_TOKEN="9036541933:AAHgj9J9zvOzy9DNB40KTQYxAfpbRSZnkOY"
```

### 4-qadam: Botni yurgizish
```bash
python main.py
```

---

## 🐳 2. Docker orqali ishga tushirish

```bash
docker compose up -d --build
```

---

## 🌐 3. Bepul Render.com serveriga joylash (O'zbekistonda ochiq va 100% bepul)

[Render.com](https://render.com) da botingizni bepul 24/7 ishlatishingiz mumkin:

1. **GitHub repozitoriy yarating**:
   ```bash
   git init
   git add .
   git commit -m "Grandpa Voice Bot"
   git remote add origin https://github.com/USERNAME/REPO_NAME.git
   git push -u origin main
   ```
   *(Eslatma: `.env` fayli `.gitignore`da bor, tokeningiz ochiq ketmaydi)*

2. **Render.com ga kiring**:
   - [render.com](https://render.com) saytiga kiring (GitHub orqali ro'yxatdan o'ting).
   - **"New +"** -> **"Web Service"** ni bosing.
   - O'zingizning GitHub repozitoriyingizni ulang.
   - **Environment** qismida: `Docker` ni tanlang.
   - **Instance Type**: `Free` ($0/month).
   - **Environment Variables** bo'limiga kiring:
     - Key: `BOT_TOKEN`
     - Value: `sizning_bot_tokeningiz`
   - **"Create Web Service"** tugmasini bosing!

Render avtomatik tarzda `Dockerfile`ni quradi, `ffmpeg`ni o'rnatadi va botni ishga tushiradi. `main.py` ichiga o'rnatilgan aiohttp health-check serveri Render tomonidan xizmat doimo faol (`Healthy`) deb tan olinishini ta'minlaydi.

---

## 🎛 Audio DSP filtri formulasi

Instagramning "Grandpa" effekti quyidagi FFmpeg filtrlari zanjiridan iborat:
```text
asetrate=48000*0.81,atempo=1.235,vibrato=f=5.8:d=0.38,tremolo=f=5.8:d=0.30,equalizer=f=1300:t=q:w=1.2:g=5,lowpass=f=3400
```
- **asetrate + atempo**: Ovoz tembrini pastlatadi (yoshini kattalashtiradi), lekin davomiylikni o'zgartirmaydi.
- **vibrato & tremolo**: Qariyalar nutqidagi tabiiy tovush titrashi (tremor) va nafas tebranishini hosil qiladi.
- **equalizer & lowpass**: Bo'g'iz rezonansini (1300Hz) kuchaytirib, yuqori shovqinli chastotalarni (3400Hz dan yuqori) qirqadi.
