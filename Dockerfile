FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update && \
    apt-get install -y --no-install-recommends git ca-certificates && \
    rm -rf /var/lib/apt/lists/*

COPY pyproject.toml uv.lock ./
RUN pip install --no-cache-dir uv && \
    uv sync --frozen --no-dev

COPY . .

ENV PORT=8080
EXPOSE 8080

CMD ["sh", "-c", ". .venv/bin/activate && uvicorn main:app --host 0.0.0.0 --port ${PORT}"]
