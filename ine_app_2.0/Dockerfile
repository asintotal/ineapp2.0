FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-spa \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

RUN useradd -m app && mkdir -p /data && chown app /data
USER app
ENV DATA_DIR=/data PORT=8000 FLASK_APP=app.py
VOLUME /data
EXPOSE 8000

# 1 proceso (el limitador de intentos de login vive en memoria) y varios hilos
CMD ["sh", "-c", "gunicorn -w 1 --threads 4 -b 0.0.0.0:${PORT} --timeout 120 app:app"]
