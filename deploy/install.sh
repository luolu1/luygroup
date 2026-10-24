#!/usr/bin/env bash
set -Eeuo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODE="docker-compose"
SERVICE_USER="groupguard"

usage() {
  cat <<'EOF'
用法：
  sudo bash deploy/install.sh [docker-compose|docker|systemd]

默认使用 docker-compose。脚本不会覆盖现有 config.json 或 data/。
EOF
}

log() { printf '[groupguard] %s\n' "$*"; }
die() { printf '[groupguard] 错误：%s\n' "$*" >&2; exit 1; }

[[ $# -le 1 ]] || { usage; exit 2; }
MODE="${1:-$MODE}"
case "$MODE" in
  docker-compose|docker|systemd) ;;
  -h|--help) usage; exit 0 ;;
  *) usage; exit 2 ;;
esac

[[ "$(id -u)" -eq 0 ]] || die "请使用 root 或 sudo 运行。"
command -v python3 >/dev/null || die "缺少 python3。"

prepare_files() {
  mkdir -p "$APP_DIR/data"
  if [[ ! -f "$APP_DIR/config.json" ]]; then
    cp "$APP_DIR/config.example.json" "$APP_DIR/config.json"
    log "已创建 config.json，请先填写 bot.token、admin_ids 和 web.secret_key。"
  else
    log "保留现有 config.json。"
  fi
}

install_compose() {
  command -v docker >/dev/null || die "缺少 Docker。"
  docker compose version >/dev/null 2>&1 || die "缺少 Docker Compose 插件。"
  prepare_files
  docker compose -f "$APP_DIR/docker-compose.yml" up -d --build
  log "Docker Compose 已启动。查看状态：docker compose ps"
}

install_docker() {
  command -v docker >/dev/null || die "缺少 Docker。"
  prepare_files
  docker build -t groupguard:latest "$APP_DIR"
  docker network inspect groupguard >/dev/null 2>&1 || docker network create groupguard >/dev/null
  docker rm -f groupguard-web groupguard-bot >/dev/null 2>&1 || true
  docker run -d --name groupguard-web --network groupguard \
    -e GROUPGUARD_CONFIG=/app/config.json -e TZ="${TZ:-Asia/Shanghai}" \
    -v "$APP_DIR/config.json:/app/config.json:ro" \
    -v "$APP_DIR/data:/app/data" -p "${WEB_PORT:-8090}:8090" \
    --restart unless-stopped groupguard:latest python run_web.py >/dev/null
  docker run -d --name groupguard-bot --network groupguard \
    -e GROUPGUARD_CONFIG=/app/config.json -e TZ="${TZ:-Asia/Shanghai}" \
    -v "$APP_DIR/config.json:/app/config.json:ro" \
    -v "$APP_DIR/data:/app/data" --restart unless-stopped \
    groupguard:latest python run_bot.py >/dev/null
  log "Docker 容器已启动：groupguard-web、groupguard-bot。"
}

install_systemd() {
  command -v systemctl >/dev/null || die "当前系统没有 systemd。"
  id "$SERVICE_USER" >/dev/null 2>&1 || useradd --system --home-dir "$APP_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
  if [[ ! -x "$APP_DIR/.venv/bin/python" ]]; then
    python3 -m venv "$APP_DIR/.venv"
    "$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt"
  fi
  prepare_files
  chown -R "$SERVICE_USER:$SERVICE_USER" "$APP_DIR/data"
  install -m 0644 "$APP_DIR/deploy/groupguard-web.service" /etc/systemd/system/groupguard-web.service
  install -m 0644 "$APP_DIR/deploy/groupguard-bot.service" /etc/systemd/system/groupguard-bot.service
  systemctl daemon-reload
  systemctl enable --now groupguard-web.service groupguard-bot.service
  log "systemd 服务已启动：groupguard-web、groupguard-bot。"
}

case "$MODE" in
  docker-compose) install_compose ;;
  docker) install_docker ;;
  systemd) install_systemd ;;
esac

log "完成。请检查 config.json 配置，并运行：curl http://127.0.0.1:8090/healthz"
