#!/bin/bash
# 在 Linux/WSL 的部署目录中初始化面板，无需宿主机 Python。
set -euo pipefail
DEPLOY_DIR="$(cd "$(dirname "$0")/.." && pwd -P)"
PANEL_IMAGE="${THERESA_PANEL_IMAGE:-heai/theresa-webcontroller:latest}"
command -v docker >/dev/null || { echo "需要安装 Docker 和 Compose 插件" >&2; exit 1; }
docker compose version >/dev/null
docker run --rm -it --entrypoint python \
    -v "$DEPLOY_DIR:$DEPLOY_DIR" \
    -v /var/run/docker.sock:/var/run/docker.sock \
    -w "$DEPLOY_DIR" "$PANEL_IMAGE" \
    -m web.deployment --init --root "$DEPLOY_DIR" "$@"
