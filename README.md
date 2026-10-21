# 群卫 GroupGuard

Telegram 群组管理机器人 + Web 控制台。集封禁禁言、反广告、进群验证、警告系统、跨群联动、全局黑名单、AI 助理、积分商城与互动玩法于一体。

- 机器人：aiogram 3（长轮询）
- 控制台：FastAPI + Jinja2 服务端渲染
- 数据库：SQLite（SQLAlchemy 2 async，WAL 模式）
- 界面与文档：中文

---

## 目录

- [功能一览](#功能一览)
- [技术栈](#技术栈)
- [目录结构](#目录结构)
- [快速开始](#快速开始)
- [配置文件](#配置文件)
- [机器人权限](#机器人权限)
- [命令列表](#命令列表)
- [AI 功能](#ai-功能)
- [积分体系与玩法](#积分体系与玩法)
- [Web 控制台](#web-控制台)
- [部署](#部署)
- [安全建议](#安全建议)
- [已知限制](#已知限制)
- [常见问题](#常见问题)

---

## 功能一览

### 群组安全

- 封禁 / 解封 / 移出 / 禁言 / 解除禁言
- 被封禁或移出的成员再次入群后自动保持禁言，直到管理员明确解除禁言
- 警告与警告记录，达到阈值自动禁言、踢出或封禁；记录显示处置时间、处置方式与可筛选的记录 ID，AI 误杀可直接复核并写入学习数据
- 删除违规消息（回复消息后执行）
- 反广告：内置正则、群组关键词（普通文本 / 正则）、允许域名、最大长度、转发消息拦截
- 关键词回复：命中关键词后发送预设回复，适合自动答疑；回复动作不会删除原消息
- 成员处置：在控制台「成员」与「反广告与警告」页直接禁言、解禁、封禁、解封或踢出；清除警告在「成员」页操作
- 反刷屏：时间窗口内消息频率限制，可配置触发动作
- 白名单 / 免检名单；Telegram 群管理员与机器人全局管理员自动免疫
- 管理日志：命令操作、验证、举报、抽奖、竞拍等事件记录
- 必关频道：进群时必须关注的频道，支持多个
- 进群验证：图片验证码（点击对应按钮）、答错立即换图、累计答错 3 次移出、超时踢出、必须设置用户名、邀请链接核销
- 群组自动登记：机器人被拉入群组或收到消息时自动创建群组记录；也可在控制台手动添加
- 新人欢迎语：验证通过或直接进群后自动发送，支持 `{name}`、`{group}`、`{id}` 占位符
- 全局黑名单：进群自动拦截并封禁，全机器人共享
- 跨群封禁联动：共享联动群 / 自动匹配所有已开启跨群封禁的群组
- 举报与通报：成员回复消息举报，可转发给指定会话或其他订阅群

### AI 能力

- AI 广告检测：在本地规则未命中时抽样送审，降低漏判
- AI 管理助理：解释群规、整理信息、起草公告
- AI 闲聊：群内 @机器人 或 `/ask` 对话
- 三种能力可分别开关
- OpenAI 兼容接口，支持多个接口按优先级排序、失败自动切换备用
- 供应商模板：选择 OpenAI、DeepSeek、OpenRouter、硅基流动、Moonshot、智谱、Groq、Together、Mistral、Ollama 或自定义接口，自动填入地址并拉取供应商模型列表
- 接口列表模式：可随时切换接口的供应商、模型、优先级与启用状态
- AI 学习数据：成功的广告判断、助理回答与闲聊按群组写入本地 JSONL，重复内容优先命中学习数据，节省 token；不会自动过期，存储目录可在控制台修改
- AI 误杀复核：在「警告」页查看 AI 判定记录与原文，标记「误杀并学习」后该消息会写入学习数据并按正常消息处理
- AI 接口添加页：接口列表页通过「添加供应商与模型」按钮进入独立添加页，避免设置页过长
- 每群每日 AI 请求额度与广告检测样本额度
- AI 调用记录、命中率、延迟与供应商统计

### 本地广告检测逻辑

本地检测不依赖 AI，顺序如下：

1. 只检查群组 / 超级群组中的文本或图片说明；纯图片、贴纸、文件不会做文字检测。
2. 跳过机器人、匿名管理员、`bot.admin_ids` 全局管理员、Telegram 群管理员和白名单成员。
3. 如果开启反刷屏，先检查时间窗口内的消息频率。
4. 如果开启反广告：
   - 先匹配群组自定义关键词（普通文本 / 正则，可设置删除、警告、禁言、踢出、封禁或关键词回复）；
   - 再匹配内置规则：外链、Telegram 链接、手机号、诱导私聊、博彩、币圈、色情、黑产、批发/代发、批量订单、自动下单、会员/账号售卖、中文商品交易、低价促销、合租/拼车、频道推广等；
   - 再检查允许域名、最大消息长度、是否禁止转发频道消息；
   - 最后检查短时间重复发送相同内容。
5. 命中后按规则动作处理：内置规则、刷屏、转发等默认先删除消息；关键词的删除/警告/禁言/踢出/封禁动作会删除原消息，关键词回复动作只发送预设回复，不删除原消息；警告达到阈值后按群设置禁言、踢出或封禁。
6. 只有本地规则没有命中时，才会在开启 `ai_ad_enabled` 且存在可用 AI 接口的情况下抽样送 AI 判断。没有配置或启用 AI 接口时，AI 检测不会生效，但本地规则仍然有效。

### 积分与互动

- 发言积分、冷却时间、每日签到
- 积分商城：商品、库存、积分价格、自定义奖励文案
- 积分红包：拼手气红包，过期自动退回
- 抽奖：参与费、定时开奖、自动公布中奖者
- 竞拍：起拍价、加价幅度、出价自动冻结、被超过自动退款
- 管理员可在控制台手动增减成员积分

### Web 控制台

- 总览：群组、成员、消息、AI、举报等核心指标
- 群组列表与详情，14 个设置标签页
- AI 接口管理、连通性测试、用量与事件
- 全局黑名单管理
- 举报记录查看与删除
- 群组统计、AI 调用与命中分析
- 协助管理员与细粒度权限
- 全部页面中文，移动端自适应

---

## 技术栈

| 组件 | 版本 / 说明 |
| --- | --- |
| Python | 3.13 |
| aiogram | 3.31 |
| FastAPI | 0.146 |
| Starlette | 1.9 |
| SQLAlchemy | 2.x async |
| 数据库 | SQLite + aiosqlite |
| 模板 | Jinja2 |
| HTTP 客户端 | httpx |

---

## 目录结构

```text
groupguard/
├── app/
│   ├── main.py                 # 同进程启动：Bot + Web
│   ├── config.py               # 配置加载与路径解析
│   ├── db.py                   # 引擎、会话、建表、AI 接口种子
│   ├── models.py               # 全部数据表
│   ├── bot/
│   │   ├── main.py             # Dispatcher、路由、Bot 启动
│   │   ├── captcha.py          # 进群验证图片验证码生成
│   │   ├── helpers.py          # 权限、目标解析、封禁/禁言等
│   │   ├── keyboards.py        # 按钮与命令面板
│   │   └── handlers/
│   │       ├── commands.py     # 通用命令、帮助、统计
│   │       ├── admin.py        # 管理命令与设置
│   │       ├── moderation.py   # 实时反广告、刷屏、AI 检测
│   │       ├── join.py         # 进群验证、欢迎、邀请
│   │       ├── report.py       # 举报与通报
│   │       ├── games.py        # 积分、商城、红包、抽奖、竞拍
│   │       └── ai_chat.py      # /ask 与 AI 对话
│   ├── services/
│   │   ├── repo.py             # 数据访问辅助
│   │   ├── detect.py           # 本地广告检测与频率限制
│   │   ├── ai.py               # OpenAI 兼容调用、供应商模板与备用切换
│   │   ├── ai_learning.py      # AI 学习数据（按用途/群组 JSONL，长期保存）
│   │   ├── quota.py            # 每群每日 AI 额度
│   │   └── scheduler.py        # 验证超时、开奖、竞拍结算、红包退款
│   └── web/
│       ├── main.py             # FastAPI 应用、会话、静态资源
│       ├── auth.py             # 面板登录与鉴权
│       ├── pages.py            # 全部页面路由
│       ├── utils.py            # 模板过滤器
│       ├── templates/          # Jinja2 模板
│       └── static/             # CSS / JS
├── config.example.json         # 配置模板（占位符）
├── config.json                 # 实际配置（已被 .gitignore 忽略）
├── data/                       # SQLite 数据库
├── deploy/                     # systemd / Nginx 示例
├── scripts/smoke_web.py        # Web 面板冒烟测试
├── scripts/smoke_rejoin_mute.py # 再次入群自动禁言回归测试
├── scripts/smoke_scheduler.py  # 验证超时移出回归测试
├── Dockerfile
├── docker-compose.yml
├── Makefile
├── run_bot.py                  # 只启动 Bot
├── run_web.py                  # 只启动 Web
└── requirements.txt
```

---

## 快速开始

### 1. 环境要求

- Python 3.13（推荐）
- 能访问 Telegram Bot API 的网络环境
- 一个从 [@BotFather](https://t.me/BotFather) 获取的 Bot Token
- 你的 Telegram 数字 ID（可先私聊机器人发送 `/id`）

### 2. 安装依赖

```bash
cd /opt/groupguard
python3.13 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

也可以直接使用 Makefile：

```bash
make install
```

### 3. 准备配置

```bash
cp config.example.json config.json
```

编辑 `config.json`，至少填写：

- `bot.token`：Bot Token
- `bot.admin_ids`：机器人全局管理员 Telegram ID 列表
- `web.secret_key`：会话签名密钥，必须改成随机长字符串
- `web.password`：控制台登录密码
- `web.base_url`：控制台实际访问地址（例如 `https://gg.example.com`）

生成随机密钥：

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

### 4. 启动

同进程启动（推荐，Bot 与控制台一起运行）：

```bash
python -m app.main
```

或：

```bash
make run
```

只启动其中一个：

```bash
python run_bot.py    # 仅 Telegram Bot
python run_web.py    # 仅 Web 控制台
```

启动后：

- 控制台：`http://服务器地址:8090`
- 健康检查：`http://服务器地址:8090/healthz`
- 登录方式：`bot.admin_ids` 中的 Telegram ID + `web.password`

### 5. 首次使用

1. 把机器人拉进群，设为管理员。
2. 授予权限：删除消息、封禁用户、邀请用户（进群验证需要）。
3. 在群里发送 `/id` 获取群 ID；机器人进群或收到消息后会自动登记，也可在控制台「群组」页手动添加。
4. 打开群组详情，按标签页配置反广告、警告、验证、AI、积分等。
5. 发送 `/help` 查看命令面板。

---

## 配置文件

默认读取项目根目录的 `config.json`。可通过环境变量指定其他路径：

```bash
GROUPGUARD_CONFIG=/etc/groupguard/config.json python -m app.main
```

若相对路径不存在，程序会尝试从项目根目录查找，因此从其他目录启动也能正常工作。SQLite 相对路径以配置文件所在目录为基准。

### `bot`

| 字段 | 说明 |
| --- | --- |
| `token` | Telegram Bot Token，必填 |
| `admin_ids` | 全局管理员 Telegram 数字 ID 数组。拥有全部权限，可登录控制台、管理全局黑名单 |
| `username` | 机器人用户名（可选，留空自动获取） |
| `proxy` | HTTP/SOCKS 代理，例如 `http://127.0.0.1:7890`；留空不使用 |

### `web`

| 字段 | 说明 |
| --- | --- |
| `host` | 监听地址，容器内通常为 `0.0.0.0` |
| `port` | 监听端口，默认 `8090` |
| `secret_key` | 会话签名密钥，必须随机且保密 |
| `password` | 控制台登录密码 |
| `base_url` | 控制台对外地址。以 `https://` 开头时会自动给会话 Cookie 加 `Secure` |
| `session_max_age` | 登录会话有效期（秒），默认 86400 |

### `database`

| 字段 | 说明 |
| --- | --- |
| `url` | SQLAlchemy 异步数据库 URL，默认 `sqlite+aiosqlite:///./data/groupguard.db` |

SQLite 已自动启用 WAL、`busy_timeout=5000` 与外键约束。单机场景无需额外数据库服务。

### `defaults`

新建群组时采用的默认值。之后可在控制台按群修改，不会影响已有群组。

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `warn_limit` | 3 | 警告阈值，`0` 表示不自动处罚 |
| `warn_action` | mute | 达到阈值后的动作：`mute` / `kick` / `ban` |
| `mute_minutes` | 60 | 默认禁言时长 |
| `flood_limit` | 8 | 时间窗口内最大消息数 |
| `flood_window` | 10 | 刷屏统计窗口（秒） |
| `flood_action` | mute | 刷屏触发动作 |
| `ai_daily_quota` | 200 | 每群每日 AI 请求额度，`0` 为不限 |
| `sample_daily_quota` | 500 | 每群每日 AI 广告检测样本额度，`0` 为不限 |
| `points_per_message` | 1 | 每条消息获得的积分 |
| `points_cooldown` | 60 | 发言积分冷却（秒） |
| `daily_bonus` | 5 | 每日签到奖励 |

### `ai`

| 字段 | 说明 |
| --- | --- |
| `timeout` | 单次接口超时（秒） |
| `max_context_messages` | 对话上下文保留条数 |
| `learning_dir` | AI 学习数据根目录；内部自动使用 `ad_detection`、`assistant`、`chat` 三个子目录。相对路径按项目目录解析，可在控制台修改并自动迁移已有 JSONL |
| `providers` | 首次启动时写入数据库的 AI 接口种子 |

接口对象字段：

| 字段 | 说明 |
| --- | --- |
| `name` | 唯一名称，如 `primary`、`backup` |
| `label` | 显示名称 |
| `base_url` | OpenAI 兼容地址，如 `https://api.openai.com/v1`；也可直接填完整的 `/chat/completions` |
| `api_key` | 接口密钥，可留空（本地模型） |
| `model` | 模型名，如 `gpt-4o-mini`、`deepseek-chat` |
| `priority` | 优先级，数值越小越优先 |
| `enabled` | 是否启用 |
| `builtin` | 是否为内置接口标记 |

> 配置中的 `providers` 只在数据库中不存在同名接口时写入。之后以数据库为准，优先在控制台「AI 接口」页面修改，避免配置与数据库不一致。

---

## 机器人权限

将机器人设为群管理员，并至少授予：

- 删除消息
- 封禁用户
- 邀请用户（进群验证、邀请链接需要）
- 限制成员（禁言需要）

若使用「必关频道」，还需要把机器人加入对应频道并设为管理员，否则无法查询成员是否已关注。

> Telegram Bot API 限制：机器人无法读取用户简介（bio），因此「简介检测」只能对姓名和用户名做关键词近似匹配。

---

## 命令列表

Telegram 命令面板按聊天类型分别注册：私聊只显示私聊命令，群组只显示群组命令。输入 `/ads`（群组）或 `/start`（私聊）可随时打开对应菜单。

### 私聊命令

| 命令 | 说明 |
| --- | --- |
| `/start` | 快速开始并自动建立控制台账号 |
| `/console` | 获取订阅控制台登录地址（仅私聊，群内不公开） |
| `/sub` | 查看当前套餐、到期时间、绑定群组与有效功能 |
| `/unbanme` | 自助解除机器人自动执行的封禁 |
| `/unmuteme` | 自助解除机器人自动执行的禁言 |
| `/cjtz` | 开启/关闭抽奖私聊推送，可带 `on` / `off` |
| `/jptz` | 开启/关闭竞拍私聊推送，可带 `on` / `off` |

> `/unbanme` 与 `/unmuteme` 不会覆盖管理员手动封禁/禁言，也不会绕过全局黑名单或入群自动禁言。控制台地址与 Token 只在私聊显示。

### 群组命令

| 命令 | 说明 |
| --- | --- |
| `/ads` | 总菜单 |
| `/ban` | 回复广告消息封禁（也支持 `@用户名` / 用户 ID） |
| `/iunban` | 全局解封并尝试跨群解除限制 |
| `/jy 用户ID 10m` | 手动禁言；支持 `10`、`10m`、`1h`、`2d` 及中文单位 |
| `/jcjy` | 解除禁言（回复被禁言用户） |
| `/wladd` | 回复用户添加白名单 |
| `/wlrm` | 回复用户移除白名单 |
| `/fy` | 查询某人的近 10 天、近 30 天和累计发言 |
| `/cf` | 查询封禁历史、处置原因和当前限制状态 |
| `/jf` | 查看自己的积分与排名 |
| `/id` | 查询用户 ID、群 ID |
| `/cj 标题 \| 奖品 [参与费] [分钟]` | 发起抽奖 |
| `/jp 物品 \| 起拍价 [加价幅度] [分钟]` | 发起竞拍 |

### 兼容命令与高级命令

以下命令仍可使用，但不会出现在 Telegram 命令面板中：

- 控制台旧别名：`/panel`、`/token`、`/subscription`。
- 群组旧别名：`/mute`、`/unmute`、`/whitelist`、`/unwhitelist`、`/points`、`/lottery`、`/auction` 等。
- 群组管理扩展：`/unban`、`/kick`、`/warn`、`/unwarn`、`/resetwarns`、`/del`、`/addkeyword`、`/addreply`、`/keywords`、`/delkeyword`、`/logs`、`/settings`。
- 设置：`/setrules`、`/setwarn`、`/setflood`、`/setmute`、`/setwelcome`、`/setverify`、`/setai`、`/setquota`、`/setcrossban`、`/setreport`。
- 进群与邀请：`/verify`、`/invite`、`/invites`。
- 频道、联动与协助管理：`/channels`、`/addchannel`、`/delchannel`、`/assistant`、`/crosslink`、`/reportlink`、`/gblacklist`。
- 积分与玩法扩展：`/checkin`、`/shop`、`/buy`、`/redpacket`、`/bid`。

> 抽奖、竞拍、红包的时间在机器人消息中按 UTC 显示；数据库与控制台同样存储/显示 UTC 时间。

## AI 功能

### 三种能力

| 能力 | 开关字段 | 行为 |
| --- | --- | --- |
| 广告检测 | `ai_ad_enabled` | 本地规则未命中时，将消息送 AI 判断；命中按警告处理 |
| 管理助理 | `ai_assistant_enabled` | 群内 @机器人 或 `/ask` 时，以助理身份回答，可带入群规 |
| 闲聊 | `ai_chat_enabled` | 未开启助理但开启闲聊时，以轻松语气对话 |

优先级：@机器人 时若开启管理助理则使用助理，否则使用闲聊。`/ask` 同理。

### 接口优先级与备用

- 所有启用的接口按 `priority` 升序尝试，数值越小越优先；
- 遇到网络错误、HTTP 4xx/5xx、空响应时自动尝试下一个；
- 广告检测在接口拒绝 `response_format` 时会自动去掉 JSON 模式重试一次；
- 控制台「AI 接口」页以列表模式展示所有接口，卡片头部集中放置测试连通、删除、编辑、优先级徽标与启用开关；
- 列表页点击「添加供应商与模型」进入独立添加页，避免把列表页拉得过长；
- 添加接口时先选择供应商模板，面板会自动填入接口地址，并可通过「获取供应商模型」拉取 `/models` 列表；接口地址缺少协议时会自动补全（公网默认 `https://`，本机与内网地址默认 `http://`）；
- 每个接口卡片直接提供供应商下拉框和模型下拉框：供应商变更后立即保存；点击模型下拉框时会先调用 `/ai/models` 拉取该接口的全部模型，再在列表中切换，样式与供应商下拉框一致；也可直接在搜索框中输入自定义模型名后按回车应用，下拉菜单底部提供「重新获取」；拉取期间显示加载状态。

兼容任意 OpenAI 风格 `/chat/completions` 接口，例如：

```text
https://api.openai.com/v1
https://api.deepseek.com/v1
https://openrouter.ai/api/v1
https://api.siliconflow.cn/v1
http://127.0.0.1:11434/v1
```

### AI 学习数据

学习数据用于减少重复内容消耗的 token，同时作为机器人的长期训练数据，默认位于 `data/ai_learning/`。广告检测、管理助理和闲聊分开存放，互不混用：

```text
data/ai_learning/
├── ad_detection/          # AI 广告检测
│   ├── group_<群ID>.jsonl
│   └── global.jsonl
├── assistant/             # AI 管理助理
│   ├── group_<群ID>.jsonl
│   └── global.jsonl
└── chat/                  # AI 闲聊
    ├── group_<群ID>.jsonl
    └── global.jsonl
```

- 每个用途、每个群组各一个 `group_<群ID>.jsonl` 文件，私聊与全局数据写入对应目录的 `global.jsonl`；
- 同一用途、同一群组内按规范化后的文本精确匹配，大小写与多余空白不影响命中；
- **长期保存，不自动过期**：广告判断、管理助理回答、闲聊数据都会一直保留；
- 仅保存成功的 AI 结果，调用失败不会写入；
- 控制台「AI 接口」页显示已学习条目、命中次数和三个分类目录，可修改存储根目录；更换目录时会自动迁移三个子目录中的 JSONL 文件；
- 旧版本直接放在 `data/ai_learning/*.jsonl` 的文件会在启动或首次读取时按 `kind` 自动拆分到对应子目录，合并时按缓存键去重；写入失败时保留原文件，不会丢数据；
- 控制台不提供清空入口。需要删除时请在服务器上操作对应子目录，且不会影响机器人运行。

> 学习数据用于性能优化和历史训练，不是强制规则来源。管理员在「警告」页将 AI 判定标记为误杀后，会覆盖对应广告判断并把该消息记为正常消息；标记「确认违规」会恢复警告并写回广告判定。

### 每群每日额度

| 额度 | 说明 |
| --- | --- |
| `ai_daily_quota` | 每日 AI 请求数。助理、闲聊、广告检测都消耗；一次请求失败切换到备用接口时会按尝试次数计费 |
| `sample_daily_quota` | 每日广告检测抽样数。只统计送审消息，用于控制审核成本 |

额度按 UTC 自然日重置，`0` 表示不限制。额度用完后：

- 广告检测跳过，本地规则仍然生效；
- 助理/闲聊回复额度提示。

控制台的「AI」和「统计」页面可查看请求数、样本数、命中数与接口延迟。

---

## 积分体系与玩法

### 获取积分

- 发言奖励：每条消息获得 `points_per_message`，受 `points_cooldown` 冷却限制；
- 每日签到：`/checkin` 获得 `daily_bonus`；
- 管理员在控制台群组详情「积分」标签中手动增减。

### 积分商城

在控制台「积分玩法」标签添加商品：名称、价格、库存、奖励类型与奖励文案。成员用 `/shop` 查看、`/buy 编号` 或点击按钮购买。库存为 0 时不可购买。

### 积分红包

`/redpacket 总额 个数 [备注]`，先到先得，每人限领一次。24 小时未领完自动过期，剩余积分退回发送者。

### 抽奖

```text
/lottery 周末抽奖 | 一个月会员 10 30
```

表示参与费 10 积分、30 分钟后开奖。成员点击按钮报名，后台任务到期自动开奖并公布中奖者。若无人参与则流奖。

### 竞拍

```text
/auction 稀有头像框 | 100 10 30
```

表示起拍价 100、加价幅度 10、30 分钟结束。成员用 `/bid 金额` 出价；出价时扣除积分，被更高价超过后自动退回。到期最高价成交。

---

## Web 控制台

登录要求：

1. Telegram ID 必须存在于 `bot.admin_ids`；
2. 密码必须等于 `web.password`。

页面说明：

| 页面 | 内容 |
| --- | --- |
| 总览 | 群组、成员、消息、AI、举报等汇总与近期动态 |
| 群组 | 搜索群组、手动添加群组、进入详情 |
| 群组详情 | 基础设置、反广告与警告、AI、进群验证、协助管理、成员、警告、关键词、白名单、必关频道、联动与通报、积分玩法、管理日志、举报，共 14 个标签；成员与违规记录页可直接对账号执行禁言/解禁/封禁/解封/踢出，清警告在成员页操作；警告页显示处置时间与处置方式，支持按记录 ID、成员、处置、来源和 AI 审核状态筛选，并可对 AI 判定标记误杀并学习 |
| AI 接口 | 供应商模板、独立添加页、拉取模型、接口列表增删改、模型切换、学习数据统计与存储目录设置、优先级、启用状态、连通性测试、14 天用量与事件 |
| 黑名单 | 全局黑名单增删 |
| 通报 | 举报记录查看与删除 |
| 统计 | 群组日统计、AI 调用与命中分析 |

群组设置采用分片保存：每个标签只提交自己的字段，不会清空其他标签的配置。

群组记录会在机器人被拉入群组、或收到群内消息时自动创建。若列表中没有目标群组，也可在「群组」页输入群组 ID 或 `@群用户名` 手动添加；添加时会通过 `getChatMember` 校验机器人是否真的在群内，不在群内会拒绝添加。群详情页顶部会显示机器人当前状态，包括是否在群、是否管理员、是否具备封禁/限制成员权限。页面上的「邀请机器人入群」按钮使用 `config.json` 的 `bot.username` 生成入群链接。

「成员」页只显示机器人记录过的人（进群、发言、验证或被执行过管理操作）。Telegram Bot API 没有“列出群内全部成员”的接口，机器人无法自动拉取完整成员名单；页面会同时显示 Telegram 群当前人数。点击「同步成员状态」可刷新已知成员的在群/封禁状态，并补录群管理员。需要查看已离开或被封禁的成员时，勾选「含已离开」。

---

## 部署

### 方式一：systemd 同进程（推荐）

同进程运行 Bot 与控制台，共享一个事件循环和数据库连接，部署最简单。

```bash
sudo useradd --system --home /opt/groupguard --shell /usr/sbin/nologin groupguard
sudo chown -R groupguard:groupguard /opt/groupguard
sudo cp deploy/groupguard.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now groupguard
sudo systemctl status groupguard
```

查看日志：

```bash
journalctl -u groupguard -f
```

### 方式二：systemd 分离部署

Bot 与 Web 分成两个服务，适合需要单独重启控制台的场景：

```bash
sudo cp deploy/groupguard-bot.service deploy/groupguard-web.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now groupguard-web groupguard-bot
```

> 两个进程会同时访问同一个 SQLite 数据库。项目已启用 WAL 与 busy timeout，通常没有问题；但首次建表建议先启动 Web 再启动 Bot，或直接使用同进程模式。

### 方式三：Docker Compose

```bash
cp .env.example .env
# 准备好 config.json
docker compose up -d --build
docker compose logs -f
```

说明：

- `./config.json` 以只读方式挂载到容器 `/app/config.json`；
- `./data` 挂载到 `/app/data`，数据库持久化；
- 默认映射 `8090:8090`，可用 `.env` 中的 `WEB_PORT` 修改宿主机端口；
- 容器内运行 `python -m app.main`，Bot 与控制台同进程。

停止与更新：

```bash
docker compose down
docker compose up -d --build
```

### Nginx + HTTPS 反向代理

参考 `deploy/nginx.conf.example`。要点：

- 将域名反向代理到 `127.0.0.1:8090`；
- 传递 `Host`、`X-Forwarded-For`、`X-Forwarded-Proto`；
- 将 `config.json` 的 `web.base_url` 改为 `https://你的域名`，会话 Cookie 会自动启用 `Secure`；
- 建议只开放 80/443，8090 仅监听本机或通过防火墙限制。

---

## 安全建议

- **不要提交 `config.json`**：其中包含 Bot Token、面板密码和会话密钥。项目已将其加入 `.gitignore`。
- 使用强随机 `web.secret_key`，不要沿用示例值。
- 面板密码使用长密码，并尽量通过 HTTPS 访问。
- `bot.admin_ids` 只填写可信账号；全局管理员可以操作所有群组和全局黑名单。
- 不要把 8090 端口直接暴露到公网；使用 Nginx/Caddy 反向代理并配置防火墙。
- 定期备份 `data/groupguard.db`（以及 WAL 文件）。备份前可先停止服务，或使用 SQLite 在线备份命令。
- 若 Token 泄露，立即在 @BotFather 中撤销并重新生成。
- AI 接口密钥保存在数据库中，请限制数据库文件权限。

---

## 已知限制

- **无法读取用户简介**：Telegram Bot API 不提供 bio，简介检测只能检查姓名与用户名。
- **抽奖/竞拍/红包没有资金托管**：奖品由管理员线下发放，机器人只记录结果。
- **AI 可能误判**：广告检测结果仅作为辅助，重要群组建议保留人工复核；可在控制台查看 AI 事件。
- **反刷屏与重复检测为进程内内存状态**：重启后清空；多进程部署时各进程独立统计。
- **时间显示为 UTC**：抽奖、竞拍、红包截止时间与控制台时间均为 UTC。
- **SQLite 单机限制**：适合中小型群组；大规模多实例部署建议改用 PostgreSQL 并调整数据库驱动。
- **必关频道依赖机器人权限**：机器人必须是频道管理员，否则无法查询成员关注状态。

---

## 常见问题

**机器人没有反应？**

1. 检查 Token 是否正确、服务器能否访问 `api.telegram.org`；
2. 确认机器人已被拉进群并设为管理员；
3. 查看 `journalctl -u groupguard -f` 或容器日志；
4. 私聊机器人发送 `/ping`。

**控制台登录失败？**

- Telegram ID 必须在 `bot.admin_ids` 中；
- 密码必须与 `web.password` 完全一致；
- 修改配置后需要重启服务；
- 清除浏览器 Cookie 后重试。

**群组没有出现在控制台？**

机器人需要在群里收到过消息或成员变动事件。把机器人拉进群后发送一条消息，或让成员进群触发事件。

**AI 一直不可用？**

- 在「AI 接口」页确认至少有一个接口已启用；
- 检查 `base_url` 是否包含正确的 `/v1` 路径；
- 使用「测试」按钮验证连通性；
- 确认该群当日额度未用完（`0` 表示不限）。

**验证码答错后怎么办？**

每个验证码只能尝试一次，答错会立即更换新验证码；同一轮验证中累计答错 3 次会被移出群组。更换验证码不会重置 5 分钟验证时限。

**被封禁或移出的成员重新入群后为什么不能说话？**

这是预期的保护行为。成员被管理员封禁、移出，或因警告达到上限被踢出后，再次入群会自动禁言，即使通过了进群验证也不会自动解除。管理员可在群组「成员」页点击「解禁」恢复其发言权限。

**验证超时后成员被踢出？**

这是预期行为。可在群组「进群验证」中调整超时时间，或关闭验证。超时时间最小 60 秒。验证超时移出不会触发“再次入群自动禁言”，用户重新入群后可正常完成验证。

如果机器人缺少「封禁用户」权限，超时移出会失败：群内会提示管理员手动处理，控制台会记录 `verify_timeout_kick_failed`。修正机器人权限后，让用户重新进群即可重新验证。

**如何备份数据？**

```bash
sqlite3 data/groupguard.db ".backup 'backup-$(date +%F).db'"
```

或直接备份整个 `data/` 目录（建议停服）。

---

## 许可

本项目为私有部署项目，未附带开源许可证。使用前请确认符合你的实际需求与所在地法律法规。
