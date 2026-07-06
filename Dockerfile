FROM python:3.11-slim

# BIST saatli APScheduler icin timezone kritik
ENV TZ=Europe/Istanbul \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY src ./src
# Editable kurulum: PROJECT_ROOT (config.py) /app olarak cozulsun diye
RUN pip install --no-cache-dir -e .

COPY config.toml ./
RUN mkdir -p data logs

CMD ["python", "-m", "swing_tracker.main"]
