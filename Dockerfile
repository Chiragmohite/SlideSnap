FROM python:3.11-slim

# Install ffmpeg and SSL certs
RUN apt-get update && apt-get install -y \
    ffmpeg \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY . .

# Install Python dependencies
RUN pip install --no-cache-dir -r backend/requirements.txt

# Force latest yt-dlp after requirements install
RUN pip install --no-cache-dir --upgrade yt-dlp

EXPOSE 7860

CMD ["python", "backend/main.py"]