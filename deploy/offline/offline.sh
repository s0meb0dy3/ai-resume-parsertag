#!/bin/sh
set -eu

BASE_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
COMPOSE_FILE="$BASE_DIR/compose.yml"
ENV_FILE="$BASE_DIR/.env"
IMAGE_DIR="$BASE_DIR/images"

compose() {
  docker compose --project-directory "$BASE_DIR" --env-file "$ENV_FILE" -f "$COMPOSE_FILE" "$@"
}

prepare() {
  command -v docker >/dev/null 2>&1 || {
    echo "错误：未安装 Docker。" >&2
    exit 1
  }
  docker compose version >/dev/null 2>&1 || {
    echo "错误：未安装 Docker Compose v2。" >&2
    exit 1
  }
  docker info >/dev/null 2>&1 || {
    echo "错误：Docker daemon 不可用。" >&2
    exit 1
  }
  [ -f "$ENV_FILE" ] || cp "$BASE_DIR/.env.example" "$ENV_FILE"
  mkdir -p "$BASE_DIR/data/logs" "$BASE_DIR/data/uploads" "$BASE_DIR/data/results" "$BASE_DIR/data/schemas"
}

load_images() {
  found=0
  for image_tar in "$IMAGE_DIR"/*.tar; do
    [ -f "$image_tar" ] || continue
    found=1
    echo "导入镜像：$(basename "$image_tar")"
    docker load -i "$image_tar"
  done
  [ "$found" -eq 1 ] || {
    echo "错误：$IMAGE_DIR 中没有 .tar 镜像包。" >&2
    exit 1
  }
}

wait_healthy() {
  container_name=$(awk -F= '/^CONTAINER_NAME=/{print $2}' "$ENV_FILE" | tail -n 1)
  container_name=${container_name:-ai-resume-parsertag}
  timeout=${HEALTH_TIMEOUT:-180}
  elapsed=0
  while [ "$elapsed" -lt "$timeout" ]; do
    status=$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$container_name" 2>/dev/null || true)
    case "$status" in
      healthy)
        host_port=$(awk -F= '/^HOST_PORT=/{print $2}' "$ENV_FILE" | tail -n 1)
        host_port=${host_port:-21804}
        echo "服务健康：http://localhost:$host_port/health"
        return 0
        ;;
      unhealthy|exited|dead)
        echo "错误：容器状态为 $status" >&2
        compose logs --tail=100
        return 1
        ;;
    esac
    sleep 3
    elapsed=$((elapsed + 3))
  done
  echo "错误：等待健康检查超时（${timeout}s）。" >&2
  compose ps
  compose logs --tail=100
  return 1
}

prepare

case "${1:-deploy}" in
  load)
    load_images
    ;;
  start)
    compose up -d --no-build --pull never
    wait_healthy
    ;;
  deploy)
    load_images
    compose up -d --no-build --pull never
    wait_healthy
    ;;
  stop)
    compose stop
    ;;
  restart)
    compose restart
    wait_healthy
    ;;
  down)
    compose down
    ;;
  status)
    compose ps
    ;;
  logs)
    compose logs -f --tail=200
    ;;
  health)
    wait_healthy
    ;;
  *)
    echo "用法：$0 {deploy|load|start|stop|restart|down|status|logs|health}" >&2
    exit 2
    ;;
esac
