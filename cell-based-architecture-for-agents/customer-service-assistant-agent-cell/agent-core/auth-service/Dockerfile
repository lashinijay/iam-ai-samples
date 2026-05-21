FROM python:3.12-slim AS base

RUN groupadd -r appuser && useradd -r -g appuser -s /sbin/nologin appuser

WORKDIR /app
COPY requirements.txt .
RUN pip install --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

COPY main.py .

USER appuser
EXPOSE 8081

ENTRYPOINT ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8081", "--log-level", "info"]
