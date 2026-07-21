FROM python:3.12-slim AS base

RUN groupadd -r appuser && useradd -r -g appuser -s /sbin/nologin appuser

# Knowledge base directory
RUN mkdir -p /data/knowledge-base && chown appuser:appuser /data/knowledge-base

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/

USER appuser
EXPOSE 8090

ENTRYPOINT ["python", "src/main.py"]
