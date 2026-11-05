# Personal Agent — production image for Render / any container host
FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DATA_DIR=/app/data

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /app/data

COPY requirements.txt requirements.lock ./
RUN pip install --upgrade pip && pip install -r requirements.txt -c requirements.lock

COPY VERSION ./
COPY app ./app
COPY static ./static
COPY scripts ./scripts
# Read at runtime: crawl policy, intent exemplars
COPY config ./config
COPY intents ./intents

# Render (and most PaaS) inject PORT; default 8787 for local docker run
EXPOSE 8787
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8787}"]
