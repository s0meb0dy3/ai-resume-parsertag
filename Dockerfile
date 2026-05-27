FROM python:3.11-slim-bookworm

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .
COPY tag/ ./tag/

RUN mkdir -p uploads logs results

EXPOSE 21804

ENV MODEL_SERVICE_URL=http://10.2.5.7:13001/v1
ENV MODEL_NAME=Qwen3.5-397B-A17B
ENV TEMPERATURE=0.01
ENV MAX_TOKENS=4096
ENV SERVICE_PORT=21804
ENV API_TIMEOUT=300
ENV MAX_RETRIES=2
ENV MAX_WORKERS=24
ENV LOG_DIR=/app/logs
ENV LOG_KEEP_DAYS=7
ENV LOG_MAX_MB=50

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "21804"]
