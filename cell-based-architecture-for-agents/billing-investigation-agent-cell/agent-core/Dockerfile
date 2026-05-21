FROM python:3.12-slim AS base

RUN groupadd -r appuser && useradd -r -g appuser -s /sbin/nologin appuser

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/

USER appuser
EXPOSE 8080

ENTRYPOINT ["python", "-m", "src.main"]
