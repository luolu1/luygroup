# 优化周期结果

## 范围

本周期针对 Web 安全边界、订阅权限、数据库迁移、游戏结算、关键词处理、安装脚本与可重复验证做定向修复。目标是修正已识别行为并验证边界，不是全面重写或性能基准。

## 八项修复

1. 共享 AI provider 请求使用已绑定的接口地址和凭据，避免密钥发往其他地址。
2. provider 编辑先校验输入，再修改记录或写事件；拒绝的编辑不产生副作用。
3. 开通限期订阅不再顺带赋予永久 `subscriber` 角色，到期后按有效权限判断是否可用。
4. 旧版控制台迁移保留两个额度列及其值。
5. 红包发布失败回滚整个事务，不再留下会被过期任务再次退款的开放红包。
6. 抽奖和竞拍先提交结算，再尽力发送通知；网络调用不占用数据库写锁。通知不是恰好一次投递。
7. 自动关键词报名检查绑定账户的游戏权限，保留未绑定群的既有行为；未命中时只发起一次 SELECT。
8. systemd 单元使用实际项目路径并支持空格；不能安全表示的特殊字符在安装副作用前拒绝，Docker 模式不变。

## 后续迁移归属修正

- SQLite 的 `ai_provider_names_match_labels_v1` 先按标签改写事件、再检查供应商名称冲突，可能把事件写到另一个供应商名下。修正为先读取原始供应商快照，仅对非空、唯一且不占用任何原始名称的标签建立映射；事件和供应商名称按同一映射逐条更新，不会级联。
- 未增加迁移标记，保留 v1/v2/v3 已有标记的兼容性。若 v1 已执行并造成归属丢失，数据库不再保有可可靠恢复的信息，本修正不会猜测或宣称恢复历史归属。
- 在 offline harness 合成 SQLite 中，修复前 `smoke_db_migrations` 失败：`('occupied', False, 1, [(1, 'beta', 'source-1'), (2, 'beta', 'source-2'), (99, 'unknown', 'orphan')])`；修复后同一 smoke 通过。矩阵覆盖被原始名称占用、共享标签、链、环、无冲突重命名、空标签、各 v1/v2/v3 标记已存在时的行为、两种插入顺序及重复升级；原有两个账户额度（`71/13` 与 `NULL/NULL`）检查也通过。
- 验证命令：`.venv/bin/python -B scripts/run_offline_smokes.py --only smoke_db_migrations --output-dir /tmp/opencode/provider-migration-red`（预期红，退出码 1）；`.venv/bin/python -B scripts/run_offline_smokes.py --only smoke_db_migrations --output-dir /tmp/opencode/provider-migration-green-confirm`（绿，退出码 0）；`.venv/bin/python -m ruff check app/db.py scripts/smoke_db_migrations.py`（通过）。LSP diagnostics 无法运行：basedpyright 未安装且此前被拒绝安装。
- 修正后 `make verify OFFLINE_ARGS='--jobs 2 --output-dir /tmp/opencode/provider-migration-verify'` 退出码 0，22 个 smoke 全通过，guard 与安装沙箱检查通过；`.venv/bin/basedpyright app/db.py scripts/smoke_db_migrations.py` 仍有原有种子逻辑 `existing.append(provider)` 的 `Sequence[AIProvider].append` 错误及 114 个警告（含新增 SQL 快照周围的未知类型警告），本周期不宣称类型全绿。

## 验证结果

- `make verify OFFLINE_ARGS='--jobs 2 --output-dir /tmp/opencode/luygroup-verify-final'`：退出码为 0，22 个受控 smoke 全通过。独立的并发第二次完整运行（`--jobs 1`）同样 22 项通过。
- 新增验证入口覆盖 `compileall`（应用与脚本）、Ruff、22 个受控 smoke、guard 回归和安装脚本沙箱检查。测试使用合成配置、真实的临时 SQLite 和模拟外部响应，并清理本次运行拥有的临时资源。Python 审计 guard 不是操作系统沙箱。
- 保留原 dispatcher 68 项与 recovery 14 项检查。新增 Web boundary 24 个用例、结算 10 个用例、红包发布 3 个用例通过；独立的 3 项 Web 修复前后对照也全部从失败变为通过。
- 两次连续迁移均通过：两个测试账户分别使用配额值 `71/13` 和 `NULL/NULL`，升级后完整 ORM 读取正常，配额、余额及必要索引保持不变。
- `run_web.py` 回环 HTTP QA 对合成配置完成 10 个请求并正常释放数据库资源。Uvicorn 在 shutdown 后重新抛出 SIGTERM（退出码 -15）；独立清理标记确认 engine 与 session factory 已释放，guard violations 为空。此前提醒任务集成测试因严格要求父目录而失败，修正为只接受当前 guard 分配的临时父目录，同时保留直接根目录检查，之后完整 22 项验证通过。
- Full basedpyright：优化前 74 个文件，1451 errors、4489 warnings；优化后 83 个文件，1448 errors、4618 warnings。新增 9 个 Python 文件均为 0 errors。新增回归代码仍有警告，包括私有、旧版及上游类型问题；整体类型检查不视为通过，警告也不能一概称为既有问题。CLI 检查已运行；LSP 不可用，未安装。

以上是行为与查询/锁属性验证，不是延迟或吞吐量基准，未测量性能提升百分比。

## 证据

- 主验证与逐脚本日志：`/tmp/opencode/luygroup-verify-final/run-252inm0v/summary.json`
- 并发第二次验证：`/tmp/opencode/luygroup-concurrent-final/run-_l70mib3/summary.json`
- Web 回环 HTTP QA 与资源释放：`/tmp/opencode/luygroup-http-qa-final/summary.json`、`/tmp/opencode/luygroup-http-qa-final/shutdown.json`
- Web boundary 修正后结果：`/tmp/opencode/luygroup-web-boundaries-parent-after-2.json`
- 结算修复前后：`/tmp/opencode/luygroup-settlement-delivery-red.log`、`/tmp/opencode/luygroup-settlement-delivery-green.log`
- 其他 guard 负向用例记录：`/tmp/opencode/offline-guard-regressions/run-fr49iayk/`。其中标记为 FAILED 或 TIMEOUT 的负向场景是预期 guard 控制，不代表验证套件失败。
- 迁移归属修正红灯：`/tmp/opencode/provider-migration-red/run-kqxe6496/smoke_db_migrations.log`；绿灯：`/tmp/opencode/provider-migration-green-confirm/run-jodkfxmz/smoke_db_migrations.log`；修正后的完整验证：`/tmp/opencode/provider-migration-verify/run-r1xu87b0/summary.json`。

## 未覆盖事项与状态

- 已有的永久 `subscriber` 角色不会自动降级；历史角色含义不明确的账号需另行审核。
- 通知保持 best-effort，没有引入持久 outbox 或自动重试，不保证恰好一次投递。未做全项目无缺陷、性能百分比或类型全绿的结论。
- 本周期未升级依赖，未做广泛 ORM 转换或路由/handler 重写；未提交代码。此前记录的“未生产重建、重启或迁移”是部署前状态，现已按用户授权完成下述发布。
- **最终 gate review：APPROVE。** 按本次部署交接的 Oracle 最终复审结论，迁移使用原始快照映射安全，36 项归属矩阵检查通过；不是对历史已损坏归属的恢复承诺。
- **生产环境：已部署，Web 与 Bot 已顺序重建启动。**

## 生产部署记录（2026-11-10 UTC）

- 部署前再次检查 `app/db.py`、`scripts/smoke_db_migrations.py`、Compose、Dockerfile 和 `.dockerignore`。代码内置于镜像，真实 `config.json` 与 `data/` 不进入构建上下文；运行时配置只读挂载，数据目录继续使用原宿主机目录。
- 亲自执行 `make verify OFFLINE_ARGS='--jobs 2 --output-dir /tmp/opencode/luygroup-predeploy-final'`。首次调用输出显示不完整，因此再次完整保存 gate 输出；第二次明确退出码 0，编译、Ruff、22 个 smoke、guard 与安装沙箱全部通过。日志：`/tmp/opencode/luygroup-predeploy-final-gate.log`；结果：`/tmp/opencode/luygroup-predeploy-final/run-okuc54ik/summary.json`。guard 的 FAILED/TIMEOUT 行是预期负向用例，不是 gate 失败。
- 构建前保留回滚标签 `groupguard:rollback-20261110T140046Z`，对应原镜像 `sha256:1210d45a252f14a6595b95916f0734022d21006a3bcaa58054e10d62dc73226e`。在原 Web/Bot 仍运行时执行 `docker compose build`，最终 `groupguard:latest` 为 `sha256:4084fb6a5d6c46bf6c89e150d0e9ebf8c511dc2d52143d7fcb655c441e3d4e41`（两个服务使用同一最终镜像）。依赖安装层命中缓存。
- 仅停止 `groupguard-bot` 和 `groupguard-web`，确认两者退出后归档完整 `data` 和 `config.json`。备份：`/opt/luygroup-backup-20261110T140046Z/luygroup-data-config.tar.gz`，310192 字节；目录 root:root 700，归档和校验文件 root:root 600。`tar -tzf` 静默验证可读，SHA-256：`cc9256ecf98958270bea162e29c52e7b4e3dc1eb4c95d9bfcac7abf5e48412e5`；同目录 `.tar.gz.sha256` 保存校验。未输出配置或数据库内容，未执行恢复写入。
- `docker compose up -d --no-deps --no-build groupguard-web` 于 `2026-11-10T14:05:52.344080409Z` 启动 Web；等待 Docker health 为 healthy，回环 `http://127.0.0.1:8090/healthz` 与 `/login` 均为 HTTP 200 后，才对 Bot 执行同样的 scoped up，Bot 启动于 `2026-11-10T14:06:34.724089313Z`，避免两个启动迁移并行。
- `2026-11-10T14:06:53Z` 检查：两容器均 running、重启计数均 0、镜像均为上述新 ID；Web healthy。按 Compose service 标签及容器名分别枚举，均仅有一个 `groupguard-bot`，且 running。启动后日志仅做脱敏计数：Web 有一次 application startup complete，Bot 有 start/run polling 两条启动标记；两者 error/traceback/exception/critical/failed、数据库错误、轮询冲突标记均为 0。
- 回滚策略：若后续验证失败，先把上述 rollback 标签重新标记为 `groupguard:latest`，仅对这两个服务按 Web 先、Bot 后顺序使用 `--no-build --no-deps` 重建；先停止 Bot。数据库备份不可盲目覆盖运行中的数据库，数据恢复需另行评估新写入和迁移兼容性。本次没有触发回滚，也没有操作其他容器。
- 运行检查范围为启动、健康端点、未认证登录页及短窗口轮询日志；未使用真实用户登录码、未发送测试群消息，也未验证公网反向代理或长期稳定性。全项目类型检查仍非全绿，历史归属损坏仍不宣称恢复。现有工作区修改全部保留，未提交代码。
