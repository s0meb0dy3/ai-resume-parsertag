FROM python:3.11-slim

WORKDIR /app

# 系统依赖（PyMuPDF / pdfplumber 所需）
RUN apt-get update && apt-get install -y --no-install-recommends \
        libglib2.0-0 \
        libgl1 \
        libfreetype6 \
    && rm -rf /var/lib/apt/lists/*

# 安装 Python 依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 单文件服务
COPY app.py .

EXPOSE 21801

# 环境变量（可通过 docker run -e 覆盖）
ENV MODEL_SERVICE_URL=http://10.2.5.7:13001/v1
ENV MODEL_NAME=Qwen3.5-397B-A17B
ENV TEMPERATURE=0.01
ENV MAX_TOKENS=4096
ENV SERVICE_PORT=21801
ENV API_TIMEOUT=300
ENV MAX_RETRIES=2
ENV MAX_WORKERS=24
ENV LOG_DIR=/app/logs
ENV LOG_KEEP_DAYS=7
ENV LOG_MAX_MB=50

# 创建上传目录和日志目录
RUN mkdir -p uploads logs

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "21801"]
