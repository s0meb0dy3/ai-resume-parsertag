FROM 10.2.5.7:6080/docker.io/python:3.11-slim-bookworm

WORKDIR /app

# 系统时区固定为北京时间，不依赖运行时 TZ 环境变量
ENV TZ=Asia/Shanghai

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .
COPY config.py .
COPY langfuse_client.py .
COPY logger_manager.py .
COPY log_config.yml .
COPY parser.py .
COPY postprocess.py .
COPY pdf_utils.py .
COPY file_utils.py .
COPY prompts.py .
COPY tasks.py .
COPY api/ ./api/
COPY tag/ ./tag/
COPY schemas/ ./schemas/

RUN mkdir -p uploads logs results

EXPOSE 21804

# 业务配置（MODEL_SERVICE_URL / API_KEY 等）通过 --env-file .env 在运行时注入，
# 此处只保留容器必要的系统默认值，避免敏感信息烘焙进镜像层
ENV UPLOAD_DIR=/app/uploads \
    RESULT_DIR=/app/results \
    LOG_DIR=/app/logs \
    SERVICE_PORT=21804

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD sh -c "python -c \"import urllib.request; urllib.request.urlopen('http://localhost:${SERVICE_PORT}/health')\"" || exit 1

CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${SERVICE_PORT:-21804}"]
