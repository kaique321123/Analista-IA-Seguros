FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0 \
    PORT=8000

COPY pyproject.toml README.md ./
COPY src ./src
COPY data/generate_data.py ./data/generate_data.py
COPY semantic_layer ./semantic_layer
COPY web ./web

RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir . \
    && python data/generate_data.py

EXPOSE 8000

CMD ["sh", "-c", "uvicorn analista_ia.web_server:app --host 0.0.0.0 --port ${PORT:-8000}"]
