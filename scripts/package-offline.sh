#!/bin/sh
set -eu

ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
OFFLINE_DIR="$ROOT_DIR/localmaterial/offline"
IMAGE_NAME=${IMAGE_NAME:-ai-resume-parsertag}
IMAGE_TAG=${IMAGE_TAG:-v3.1.1-20260805}
IMAGE_REF="$IMAGE_NAME:$IMAGE_TAG"
PLATFORM=${PLATFORM:-linux/amd64}
ARCHIVE_NAME=${ARCHIVE_NAME:-ai-resume-parsertag-offline-amd64.tar.gz}
CREATE_ARCHIVE=${CREATE_ARCHIVE:-1}

require_command() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "错误：缺少命令 $1" >&2
    exit 1
  }
}

require_command docker

if ! docker info >/dev/null 2>&1; then
  echo "错误：Docker daemon 不可用，请先启动 Docker Desktop 或 Docker Engine。" >&2
  exit 1
fi

echo "[1/5] 构建 $PLATFORM 镜像：$IMAGE_REF"
docker build --platform "$PLATFORM" -t "$IMAGE_REF" "$ROOT_DIR"

echo "[2/5] 准备离线输出目录：$OFFLINE_DIR"
mkdir -p "$OFFLINE_DIR/images" "$OFFLINE_DIR/data/logs" "$OFFLINE_DIR/data/uploads" "$OFFLINE_DIR/data/results" "$OFFLINE_DIR/data/schemas"
rm -f "$OFFLINE_DIR/images/"*.tar "$OFFLINE_DIR/$ARCHIVE_NAME"

echo "[3/5] 导出镜像包"
docker image save -o "$OFFLINE_DIR/images/$IMAGE_NAME-$IMAGE_TAG-amd64.tar" "$IMAGE_REF"

echo "[4/5] 复制部署配置与数据模板"
cp "$ROOT_DIR/deploy/offline/compose.yml" "$OFFLINE_DIR/compose.yml"
cp "$ROOT_DIR/deploy/offline/.env.example" "$OFFLINE_DIR/.env.example"
cp "$ROOT_DIR/deploy/offline/offline.sh" "$OFFLINE_DIR/offline.sh"
cp "$ROOT_DIR/deploy/offline/README.md" "$OFFLINE_DIR/README.md"
for schema_file in "$ROOT_DIR/schemas/"*.json; do
  schema_name=$(basename "$schema_file")
  if [ "$schema_name" = "tag_config_runtime.json" ] && [ -f "$OFFLINE_DIR/data/schemas/$schema_name" ]; then
    echo "保留运行时标签配置：data/schemas/$schema_name"
    continue
  fi
  cp "$schema_file" "$OFFLINE_DIR/data/schemas/$schema_name"
done
printf '%s\n' "$IMAGE_REF" > "$OFFLINE_DIR/images/image-ref.txt"
touch "$OFFLINE_DIR/data/logs/.gitkeep" "$OFFLINE_DIR/data/uploads/.gitkeep" "$OFFLINE_DIR/data/results/.gitkeep"
chmod +x "$OFFLINE_DIR/offline.sh"

if [ ! -f "$OFFLINE_DIR/.env" ]; then
  cp "$OFFLINE_DIR/.env.example" "$OFFLINE_DIR/.env"
fi

if [ "$CREATE_ARCHIVE" = "1" ]; then
  echo "[5/5] 生成整体传输压缩包"
  require_command tar
  tar -C "$OFFLINE_DIR" -czf "$OFFLINE_DIR/$ARCHIVE_NAME" \
    --exclude="./$ARCHIVE_NAME" .
  echo "压缩包：$OFFLINE_DIR/$ARCHIVE_NAME"
else
  echo "[5/5] 已跳过整体压缩（CREATE_ARCHIVE=$CREATE_ARCHIVE）"
fi

echo "离线包生成完成：$OFFLINE_DIR"
echo "本地验证：cd '$OFFLINE_DIR' && ./offline.sh deploy"
