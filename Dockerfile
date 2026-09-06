FROM python:3.12-slim

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV HOST=0.0.0.0
ENV PORT=8080
ENV DATABASE_PATH=/var/lib/wsp/data/wsp.db

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN mkdir -p /var/lib/wsp/data/backups /var/lib/wsp/data/transcripts /var/lib/wsp/data/logs

VOLUME ["/var/lib/wsp/data"]

CMD ["python", "main.py"]
