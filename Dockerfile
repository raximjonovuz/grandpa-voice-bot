FROM python:3.11-slim

# Tizim paketlarini yangilash va FFmpeg o'rnatish
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg && \
    rm -rf /var/lib/apt/lists/*

# Ishchi papka yaratish
WORKDIR /app

# Bog'liqliklarni o'rnatish
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Kodlarni nusxalash
COPY . .

# Render va konteyner porti
EXPOSE 8080
ENV PORT=8080

# Botni ishga tushirish
CMD ["python", "main.py"]
