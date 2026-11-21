FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ALLOW_SYNTHETIC_FALLBACK=false \
    MARKET_CACHE_TTL_SEC=45

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py agents.py backtester.py crisis_advisor.py db.py europe_bridge.py leak_xray.py market_data.py telegram_bot.py cli_demo.py ./
COPY static ./static

RUN useradd --create-home --shell /bin/bash appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
