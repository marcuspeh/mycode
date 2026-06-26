FROM python:3.13-slim

WORKDIR /app

RUN pip install --no-cache-dir \
    fastapi>=0.115.0 \
    "uvicorn[standard]>=0.30.0" \
    httpx>=0.27.0 \
    pydantic>=2.8.0 \
    pydantic-settings>=2.4.0 \
    structlog>=24.4.0 \
    pyyaml>=6.0.0

COPY app/ ./app/
COPY config/ ./config/

EXPOSE 3566

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "3566"]
