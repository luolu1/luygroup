PYTHON_BIN ?= python3.13
VENV := .venv
PYTHON := $(VENV)/bin/python
PIP := $(VENV)/bin/pip

.PHONY: help venv install init-config run run-bot run-web check lint format smoke smoke-permissions smoke-entitlement-queries smoke-captcha smoke-rejoin smoke-scheduler smoke-scheduler-queries smoke-scheduler-idempotency smoke-bot-recovery smoke-subscription-purchase smoke-payments smoke-commands smoke-games-race smoke-economy-claims docker-build docker-up docker-down docker-logs clean

help:
	@echo "luygroup 常用命令："
	@echo "  make install       创建虚拟环境并安装依赖"
	@echo "  make init-config   若无 config.json，则从示例复制"
	@echo "  make run           同进程启动 Bot + Web"
	@echo "  make run-bot       仅启动 Telegram Bot"
	@echo "  make run-web       仅启动 Web 控制台"
	@echo "  make check         编译检查全部 Python 文件"
	@echo "  make lint          Ruff 检查生产代码与测试脚本"
	@echo "  make format        Ruff 格式化 Python 文件"
	@echo "  make smoke         运行 Web 面板冒烟测试"
	@echo "  make smoke-permissions 运行角色、群组与接口隔离回归测试"
	@echo "  make smoke-entitlement-queries 运行权限缓存语义与查询预算测试"
	@echo "  make smoke-captcha 运行验证码刷新、三次失败移出回归测试"
	@echo "  make smoke-scheduler 运行验证超时移出回归测试"
	@echo "  make smoke-scheduler-queries 运行调度器批量查询与清理回归测试"
	@echo "  make smoke-scheduler-idempotency 运行抽奖/竞拍/红包重复结算回归测试"
	@echo "  make smoke-bot-recovery 运行 Bot 断线恢复生命周期回归测试"
	@echo "  make smoke-subscription-purchase 运行订阅购买/续费与余额扣款回归测试"
	@echo "  make smoke-payments 运行易支付下单、验签、回调幂等与充值回归测试"
	@echo "  make smoke-commands 运行命令菜单、学习分目录、功能门槛与自助命令测试"
	@echo "  make smoke-games-race 运行竞拍/红包并发资金安全回归测试"
	@echo "  make smoke-economy-claims 运行签到/商城/抽奖并发资金与库存回归测试"
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

lint: $(PYTHON)
	$(PYTHON) -m ruff check app run_bot.py run_web.py scripts

format: $(PYTHON)
	$(PYTHON) -m ruff format app run_bot.py run_web.py scripts

smoke: $(PYTHON)
	$(PYTHON) scripts/smoke_web.py

smoke-permissions: $(PYTHON)
	$(PYTHON) scripts/smoke_permissions.py

smoke-entitlement-queries: $(PYTHON)
	$(PYTHON) scripts/smoke_entitlement_queries.py

smoke-captcha: $(PYTHON)
	$(PYTHON) scripts/smoke_captcha.py

smoke-rejoin: $(PYTHON)
	$(PYTHON) scripts/smoke_rejoin_mute.py

smoke-scheduler: $(PYTHON)
	$(PYTHON) scripts/smoke_scheduler.py

smoke-scheduler-queries: $(PYTHON)
	$(PYTHON) scripts/smoke_scheduler_queries.py

smoke-scheduler-idempotency: $(PYTHON)
	$(PYTHON) scripts/smoke_scheduler_idempotency.py

smoke-bot-recovery: $(PYTHON)
	$(PYTHON) scripts/smoke_bot_recovery.py

smoke-subscription-purchase: $(PYTHON)
	$(PYTHON) scripts/smoke_subscription_purchase.py

smoke-payments: $(PYTHON)
	$(PYTHON) scripts/smoke_payments.py

smoke-commands: $(PYTHON)
	$(PYTHON) scripts/smoke_commands.py

smoke-games-race: $(PYTHON)
	$(PYTHON) scripts/smoke_games_race.py

smoke-economy-claims: $(PYTHON)
	$(PYTHON) scripts/smoke_economy_claims.py

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
