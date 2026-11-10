# token-ledger · Token 消耗记录与可视化

记录每个 agent 的每笔 Token 消耗：一份完全离线、零依赖的本机 Token 消耗台账，
把 Claude Code / Codex / Pi 的会话日志逐笔解析、去重、落库，再按需聚合成可视化看板。

零 npm、零 CDN、零外部字体 —— Python 3 标准库 + 原生 HTML/CSS/JS。
所有数据留在本地，不联网、无遥测。

## ▶ 在线演示

<https://github.com/user-attachments/assets/04799a96-5304-41df-a0b1-83fe13dfb652>

产品宣传片（8 场景 · 47 秒 · 中文旁白）。无需安装、无需启动服务，也可直接打开可交互的在线演示页：

- **[产品演示 · 11 场景动画](https://0xamoxicillin.github.io/token-ledger/demo.html)**
- [协作历程演示 · 6 场景动画](https://0xamoxicillin.github.io/token-ledger/demo-orca.html)

由 GitHub Pages 托管（源：`main` 分支 `/docs`）。空格暂停、←/→ 切场景、倍速、重播；
两页控制条均提供常驻互跳链接，无需等待动画播放。分镜与旁白见 `docs/demo-script.md`。

## 特性

- **三数据源**：Claude Code / Codex / Pi 的本机 jsonl 会话日志
- **幂等增量记录**：稳定 dedup_key + INSERT OR IGNORE，重复运行不产生重复行
- **登录保护**：PBKDF2-HMAC-SHA256 200,000 轮 + HttpOnly / SameSite=Strict 会话 Cookie，
  首次登录强制改密
- **可视化**：KPI 卡片、按 agent 横向条形图、环形占比、按天堆叠趋势、
  模型分布、会话明细表、7/30/90/全部 时间范围
- **暗色设计语言**：对齐 shadcn/ui 的间距、圆角与空态规范
- **零依赖在线演示**：`docs/` 两页动画由 Pages 托管

## 快速开始

要求：Python 3.10+（仅标准库）、macOS / Linux。

    git clone https://github.com/0xAmoxicillin/token-ledger.git
    cd token-ledger
    ./start.sh

打开 <http://127.0.0.1:8765/>。账号 `admin`，初始随机密码见
`data/initial-credential.txt`（权限 600，仅一行），首次登录后必须修改。

## 数据源与统计口径

| Agent | 日志位置 | token 字段 |
|---|---|---|
| Claude Code | `~/.claude/projects/**/*.jsonl` | `assistant` 行 `message.usage.*` |
| Codex | `~/.codex/sessions/**/*.jsonl` | `event_msg` / `token_count` 的 `last_token_usage` |
| Pi | `~/.pi/agent/sessions/**/*.jsonl` | `assistant` 消息 `message.usage.*` |

- Codex 取 `last_token_usage`（单次增量），不取累计的 `total_token_usage`。
- 去重：Claude 按 `message.id`、Pi 按消息内容哈希、Codex 按来源行号；跨文件副本会收敛。
- 成本仅 Pi 日志提供；缓存读取是输入子集，不与输入/输出相加求总量。

## 认证

`POST /api/auth/login`（`username` + `pw`）、`/api/auth/logout`、
`GET /api/auth/status`、`POST /api/auth/change-pw`（`old_pw` + `new_pw`）。
除 login/status 外所有 `/api/*` 需有效会话；未登录访问静态页 `302 → /login.html`，
访问接口返回 `401 {"ok":false,"error":"unauthorized"}`。

## API

统一 `{"ok":true,"data":...}` / `{"ok":false,"error":"..."}` 包装。
`GET /api/overview`、`/api/by-agent`、`/api/timeseries`、`/api/by-model`、
`/api/sessions`、`/api/meta`，`POST /api/refresh`。`days<=0` 表示全部时间。

## 隐私

- 所有用量数据只存本机 `data/`，不联网、无遥测、无第三方依赖。
- `data/`（用量库、账号库、明文凭据）由 `.gitignore` 排除，**绝不入库**。
- `docs/demo-data.js` 为**演示用合成数据**，不含任何实际用量、会话 ID、模型标识或成本信息；`docs/demo.html` 内联同一份数据以便 `file://` 直接打开。

## 目录结构

    collector.py   日志扫描 / 规范化 / SQLite 写入（幂等）
    auth.py        账号 / 口令哈希 / 会话与 Cookie 鉴权
    server.py      HTTP API + 静态服务 + 登录态拦截
    start.sh       一键启动（HOST / PORT）
    web/           看板与登录页
    docs/          GitHub Pages 在线演示
    data/          运行时生成（已忽略）

## License

MIT
