FROM python:3.12-slim

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOST=0.0.0.0 PORT=8000 DB_PATH=/datos/pagos.db

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# La base SQLite vive en un volumen para no perderla al redeplegar.
VOLUME ["/datos"]
EXPOSE 8000
CMD ["python", "-m", "app"]
