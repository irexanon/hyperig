FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN useradd -m bot && mkdir -p /app/data && chown -R bot /app/data
USER bot
# Keep /app/data on a volume so users' Instagram sessions survive redeploys.
VOLUME ["/app/data"]
CMD ["python", "main.py"]
