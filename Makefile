PYTHON_BIN ?= python3.13
VENV := .venv
PYTHON := $(VENV)/bin/python
PIP := $(VENV)/bin/pip

.PHONY: help venv install init-config run run-bot run-web check smoke smoke-permissions smoke-captcha smoke-scheduler smoke-commands docker-build docker-up docker-down docker-logs clean

help:
	@echo "GroupGuard 常用命令："
	@echo "  make install       创建虚拟环境并安装依赖"
	@echo "  make init-config   若无 config.json，则从示例复制"
	@echo "  make run           同进程启动 Bot + Web"
	@echo "  make run-bot       仅启动 Telegram Bot"
	@echo "  make run-web       仅启动 Web 控制台"
	@echo "  make check         编译检查全部 Python 文件"
	@echo "  make smoke         运行 Web 面板冒烟测试"
	@echo "  make smoke-permissions 运行角色、群组与接口隔离回归测试"
	@echo "  make smoke-captcha 运行验证码刷新、三次失败移出回归测试"
	@echo "  make smoke-scheduler 运行验证超时移出回归测试"
	@echo "  make smoke-commands 运行命令菜单、学习分目录、功能门槛与自助命令测试"
	@echo "  make docker-build  构建 Docker 镜像"
	@echo "  make docker-up     启动 Docker Compose"
	@echo "  make docker-down   停止 Docker Compose"
	@echo "  make docker-logs   查看容器日志"
	@echo "  make clean         清理 __pycache__"

venv: $(PYTHON)

$(PYTHON):
	$(PYTHON_BIN) -m venv $(VENV)

install: venv
	$(PIP) install --upgrade pip
	$(PIP) install -r requirements.txt

init-config:
	@if [ ! -f config.json ]; then \
		cp config.example.json config.json; \
		echo "已创建 config.json，请填写 bot.token、web.password 等字段。"; \
	else \
		echo "config.json 已存在，未覆盖。"; \
	fi

run: $(PYTHON)
	$(PYTHON) -m app.main

run-bot: $(PYTHON)
	$(PYTHON) run_bot.py

run-web: $(PYTHON)
	$(PYTHON) run_web.py

check: $(PYTHON)
	$(PYTHON) -m compileall -q app run_bot.py run_web.py
	@echo "编译检查通过。"

smoke: $(PYTHON)
	$(PYTHON) scripts/smoke_web.py

smoke-permissions: $(PYTHON)
	$(PYTHON) scripts/smoke_permissions.py

smoke-captcha: $(PYTHON)
	$(PYTHON) scripts/smoke_captcha.py

smoke-rejoin: $(PYTHON)
	$(PYTHON) scripts/smoke_rejoin_mute.py

smoke-scheduler: $(PYTHON)
	$(PYTHON) scripts/smoke_scheduler.py

smoke-commands: $(PYTHON)
	$(PYTHON) scripts/smoke_commands.py

docker-build:
	docker compose build

docker-up:
	docker compose up -d --build

docker-down:
	docker compose down

docker-logs:
	docker compose logs -f --tail=200

clean:
	find . -path ./.venv -prune -o -type d -name __pycache__ -exec rm -rf {} +
	@echo "已清理 __pycache__。"
