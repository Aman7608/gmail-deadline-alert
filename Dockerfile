FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HEADLESS=1 \
    TZ=Asia/Kolkata

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Run as a normal user, not root. uid 1000 matches the first user on most Linux machines.
RUN useradd --uid 1000 --create-home appuser \
    && mkdir -p /app/data /app/logs \
    && chown -R appuser:appuser /app
USER appuser

CMD ["python", "-m", "app.main"]
