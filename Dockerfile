FROM 10.2.5.7:6080/docker.io/python:3.11-slim-bookworm

WORKDIR /app

ENV TZ=Asia/Shanghai

COPY requirements.txt .
RUN pip install --no-cache-dir --timeout 600 --retries 5 -r requirements.txt

COPY app.py .
COPY config.py .
COPY api/ ./api/
COPY core/ ./core/
COPY logger/ ./logger/
COPY utils/ ./utils/
COPY tag/ ./tag/
COPY schemas/ ./schemas/

RUN mkdir -p uploads logs results

EXPOSE 21804

ENV UPLOAD_DIR=/app/uploads \
    RESULT_DIR=/app/results \
    LOG_DIR=/app/logs \
    SERVICE_PORT=21804

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:21804/health')" || exit 1

CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${SERVICE_PORT:-21804}"]
