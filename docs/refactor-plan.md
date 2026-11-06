# luygroup 整体重构计划（阶段 3，计划稿）

> 目标：在**不改变业务行为**的前提下，逐步降低复杂度、提升可维护性与回归安全性。  
> 约束：当前工作树存在大量未提交改动，本计划**不直接改代码**；后续实施应在干净分支/小批次 PR 中进行，并按模块回归。

## 1. 现状基线（2026-11-06）

- 质量门禁（当前环境）：`python -m compileall -q app run_bot.py run_web.py` 通过；对已变更 Python 文件的 `ruff format` / `ruff check` 已定向修复并通过。
- 冒烟验证：`scripts/smoke_web.py` 通过（本次 UI + 定向格式修复后）。
- 结构风险（纯 LOC 粗查，>250 行模块/脚本较多）：
  - `app/web/pages.py` (~2929)
  - `app/bot/handlers/games.py` (~2003)
  - `app/bot/handlers/admin.py` (~1190)
  - `app/bot/handlers/join.py` (~1004)
  - `app/bot/handlers/commands.py` (~919)
  - `app/services/ai.py` (~752)
  - `app/services/scheduler.py` (~399，已做过局部优化)
  - 大型 smoke 脚本：`scripts/smoke_games_race.py`、`scripts/smoke_web.py` 等（>1000）

## 2. 重构原则

1. **行为不变优先**：每一步都应有可运行的验证（lint/check + 相关 smoke/回归）。
2. **小步拆分**：按职责拆文件/函数，不做跨模块“大爆炸”重写。
3. **避开并行改动冲突**：优先处理稳定/低频改动模块；与当前未提交改动重叠的模块（如 `web/pages.py`、`handlers/games.py`、`web/payment_*`）最后进入，或在合并当前工作后再拆。
4. **边界清晰**：路由/handler 只做输入解析与调用编排；业务逻辑下沉到 `services`/`repo`/专用 helper；模板渲染与数据组装分离。
5. **测试先行于结构变更**：对高风险路径先补/固定 smoke 场景，再做机械拆分。

## 3. 分阶段实施计划

### Phase A — 低风险整理（1-2 批）
- **A1. 格式与一致性（按批次）**：在干净分支对全仓 `ruff format` 分批处理，避免与功能改动混提。优先 `scripts/` 和低变动 `app/services/*`。
- **A2. 公共小工具收敛**：检查重复的表单解析、分页、权限判定、响应构造；仅在有明确重复时提取，不新增抽象层。
- **回归**：`make check`/`compileall`、`ruff check`、受影响 smoke（如 `smoke_permissions`、`smoke_web`、`smoke_scheduler_queries`）。

### Phase B — Web 层拆分（需在 `pages.py`/payment 改动稳定后）
- 将 `app/web/pages.py` 按领域拆分（示例，具体以现有路由为准）：
  - `app/web/routes/dashboard.py`
  - `app/web/routes/groups.py`
  - `app/web/routes/ai.py`
  - `app/web/routes/users.py`
  - `app/web/routes/reports_blacklist.py`
  - `app/web/routes/payments.py`（与 `payment_admin/callbacks/forms` 对齐）
  - `app/web/context.py`（`_render`/模板上下文/flash）
  - `app/web/deps.py`（鉴权与通用依赖）
- 保持路由路径、模板名称、表单字段与权限行为不变；`pages.py` 可暂作兼容聚合入口再逐步移除。
- **回归**：`scripts/smoke_web.py`、`smoke_permissions.py`、`smoke_payments.py`、`smoke_commands.py`（如涉及）。

### Phase C — Bot Handlers 拆分（高风险，后置）
- `handlers/games.py`：按游戏/竞拍/红包/抽奖等子域拆模块，共享校验与资金/库存操作复用 `services`。
- `handlers/admin.py` / `join.py` / `commands.py`：按功能组拆分，保留命令与回调契约。
- 先增加/巩固并发与幂等 smoke（已有 `smoke_games_race.py`、`smoke_economy_claims.py`、`smoke_scheduler_idempotency.py`），再机械移动代码。
- **回归**：对应 handler smoke + `smoke_commands.py`、`smoke_permissions.py`、`smoke_captcha.py`/`smoke_rejoin_mute.py`（按影响面选择）。

### Phase D — Services 与数据层
- `services/ai.py`：拆分 provider 调用、配额/学习/事件记录、提示与解析（保持现有外部行为与配置）。
- `db.py` / `models.py`：仅在必要时做小范围整理（避免 ORM 迁移风险）；若调整模型需配套迁移/兼容策略。
- **回归**：`smoke_ai_client.py`、`smoke_entitlement_queries.py`、相关 scheduler/smoke。

### Phase E — 测试脚本可维护性（可选，低优先）
- 大型 smoke 脚本可按场景拆分 helper/fixture，但要保持单脚本可运行与现有 `Makefile` 入口兼容。
- 避免在功能开发同一批次重排测试结构。

## 4. 风险与控制

| 风险 | 控制措施 |
|---|---|
| 路由/回调契约变化 | 保持 URL、表单字段、模板变量、重定向行为；改后跑 `smoke_web`/权限/支付 smoke |
| 并发资金/库存回归 | 先跑 `smoke_games_race`、`smoke_economy_claims`、`smoke_scheduler_idempotency` |
| 与当前未提交改动冲突 | 重构前 rebase/合并当前工作；按模块小 PR；不在 dirty tree 做结构性搬迁 |
| 全库格式化噪声 | 分批、独立提交、与行为改动分离 |

## 5. 建议的下一步（实施顺序）

1. 合并/稳定当前功能与 Web UI 改动，确保工作树干净。
2. 从 **Phase B 的 `pages.py` 拆分准备**开始：先梳理路由清单与模板依赖（只读），再做兼容性拆分。
3. 每个 PR 限定 1 个领域模块 + 对应 smoke 结果。
