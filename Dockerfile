FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install dependencies first so the layer is cached across code changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Application code. config.json and data/ are mounted at runtime.
COPY app ./app
COPY run_bot.py run_web.py ./

RUN mkdir -p /app/data

EXPOSE 8090

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8090/healthz', timeout=3)"

# Combined bot + web process. Override with `python run_web.py` or
# `python run_bot.py` when running split services.
CMD ["python", "-m", "app.main"]
