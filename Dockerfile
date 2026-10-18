FROM python:3.12-slim
WORKDIR /app
# ffmpeg converts Telegram voice notes / videos to what Instagram accepts
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN useradd -m bot && mkdir -p /app/data && chown -R bot /app/data
USER bot
# Keep /app/data on a volume so the Instagram session survives redeploys.
VOLUME ["/app/data"]
CMD ["python", "main.py"]
