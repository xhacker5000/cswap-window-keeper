# cswap-window-keeper

[English README](./README.md)

一个轻量、零第三方依赖的后台守护脚本，基于 [`claude-swap` (`cswap`)](https://github.com/realiti4/claude-swap) 自动保持你本机所有 Claude 订阅账号的 **5 小时使用窗口** 持续运转。

## 为什么需要它？

Claude 订阅账号的 5 小时使用窗口在重置后，**下一次重置时间是从重置后你发送第一条消息那一刻开始算的**。如果窗口重置后一直没发消息，空置的时间就白白浪费了。

`cswap-window-keeper` 的解决方式既精准又克制：

1. **精准预约唤醒（不频繁轮询）**：
   - 调用 `cswap list --json` 获取所有已启用订阅账号的 5 小时窗口状态（`usage.fiveHour.resetsAt`）。
   - 若账号当前 5h 窗口活跃，记录其 `resetsAt` 并直接睡到 **`resetsAt + 60s`**（例如 2h10m 后重置，就在 2h11m 唤醒检查），几乎零多余请求。
   - 内部按 ≤ 30s 分段对比墙钟时间睡眠，Mac 合盖休眠唤醒后能第一时间补检，不会漏掉重置点。
2. **Session 模式最小 Token 唤醒（不干扰当前登录账号）**：
   - 到达重置点后若确认该账号空闲（`pct == 0` 且无活跃 `resetsAt`），通过 `cswap run <slot>`（session 模式，不会切换你当前默认账号）发送一条极简消息：
     ```bash
     cswap run <slot> -- \
       -p "say test" \
       --model haiku \
       --tools "" \
       --system-prompt "" \
       --safe-mode \
       --no-session-persistence \
       --effort low \
       --output-format json
     ```
   - 单次唤醒约消耗 **2 input + 3 output tokens（约 $0.0001）**。
3. **尊重缓存与限流机制**：
   - 严格尊重 `cswap` 的 180s usage 缓存（`SERVE_TTL_S`）：不会把重置前或 ping 前的旧缓存误判为实时状态；ping 完成后等待 ~192s 自动回查一次，锁定服务端分配的精确新 `resetsAt`，随后进入下一轮 ~5 小时休眠，周而复始。
   - 尊重 HTTP 429 的 `usageRetryAt` 退避时间。
   - 内置同账号 10 分钟防重复 ping 冷却、文件锁防多开；自动跳过已禁用账号、API Key 账号、需重新登录的账号以及 7 天周配额已用尽（100%）的账号。

---

## 环境与依赖安装

`cswap-window-keeper` 本身仅依赖 **Python 3.9+ 标准库**（无需 `pip install` 任何第三方包），运行前请确保本机已安装并配置好以下工具：

### 1. Python 3.9+
- **macOS**：自带 `/usr/bin/python3`（或 Xcode Command Line Tools）即可直接运行。
- **Linux**：确保 `python3 --version` 为 3.9 及以上版本。

### 2. Claude Code CLI (`claude`)
安装官方 [Claude Code](https://docs.anthropic.com/en/docs/claude-code)：
```bash
# 通过 npm 安装（需要 Node.js 18+）
npm install -g @anthropic-ai/claude-code

# 验证安装
claude --version
```

### 3. `claude-swap` (`cswap`)
安装 [`realiti4/claude-swap`](https://github.com/realiti4/claude-swap)（建议 v0.27+，支持 `list --json` 与 `run` session 模式）：

```bash
# 推荐：使用 uv 安装
uv tool install claude-swap

# 或使用 pipx 安装
pipx install claude-swap

# 验证安装
cswap --version
```

将你的 Claude 订阅账号录入 `cswap`：
```bash
# 依次在 Claude Code 中登录各个订阅账号后执行：
cswap add

# 确认 cswap 能正常输出 JSON 与 5 小时窗口信息：
cswap list --json
```

> **提示**：请确认 `~/.local/bin` 已加入你的 shell `PATH` 环境变量。

---

## 安装与快速开始

### 1. 克隆仓库

```bash
git clone https://github.com/xhacker5000/cswap-window-keeper.git
cd cswap-window-keeper
```

### 2. 先模拟检查一次（不会真正发消息）

```bash
python3 cswap_window_keeper.py once --dry-run
```

### 3. macOS：一键安装为后台服务（LaunchAgent，推荐）

```bash
python3 cswap_window_keeper.py install
```

`install` 会自动完成：
- 将脚本复制到 `~/.local/bin/cswap-window-keeper`（规避 macOS TCC 对 `~/Documents` / `~/Desktop` 的后台读取限制，并支持在任意目录直接运行 `cswap-window-keeper`）。
- 写入并启动 `~/Library/LaunchAgents/com.cswap-window-keeper.agent.plist`（开机自启 + 异常自动拉起，运行在当前用户 GUI 会话中可正常访问 Keychain）。

### 4. Linux：使用 `systemd` 用户服务

创建 `~/.config/systemd/user/cswap-window-keeper.service`：

```ini
[Unit]
Description=cswap-window-keeper (Claude 5-hour window keeper)
After=network-online.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 /path/to/cswap-window-keeper/cswap_window_keeper.py run
Restart=on-failure
RestartSec=30

[Install]
WantedBy=default.target
```

启用并启动服务：
```bash
systemctl --user daemon-reload
systemctl --user enable --now cswap-window-keeper
```

---

## 常用命令

macOS 执行 `install` 后可直接使用全局命令 `cswap-window-keeper`（也可使用 `python3 cswap_window_keeper.py ...`）：

```bash
# 查看后台服务状态、各账号 5h 重置时间与下次唤醒时间
cswap-window-keeper status

# 查看运行日志 / 实时跟踪日志
cswap-window-keeper logs -n 50
cswap-window-keeper logs -f

# 手动执行一次检查（加 --dry-run 只模拟不发消息）
cswap-window-keeper once
cswap-window-keeper once --dry-run

# 前台运行守护进程
cswap-window-keeper run

# 停止并卸载 macOS LaunchAgent
cswap-window-keeper uninstall
```

### 可选参数

全局参数需放在子命令（`install` / `run` / `once` 等）**之前**：

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--grace SECONDS` | `60` | 5h 重置后延迟多少秒再检查/唤醒 |
| `--cooldown SECONDS` | `600` | 同账号两次 ping 之间的最小冷却秒数 |
| `--model ALIAS` | `haiku` | 唤醒时使用的 Claude 模型别名 |
| `--prompt TEXT` | `say test` | 唤醒空闲窗口时发送的提示词 |
| `--accounts LIST` | *(全部启用账号)* | 仅监控指定账号（逗号分隔的槽位号/邮箱/别名） |
| `--state-dir PATH` | `~/.local/state/cswap-window-keeper` | 状态文件与日志存放目录 |
| `-v, --verbose` | `false` | 输出详细调试日志 |

示例——仅监控账号 1 和 2，重置后延迟 90 秒检查：
```bash
python3 cswap_window_keeper.py --grace 90 --accounts 1,2 install
```

---

## 隐私与安全说明

- 本工具**不读取、不保存、不上传任何账号凭证或 OAuth Token**，全部鉴权交由你本机的 `cswap` 与 `claude` 完成。
- 运行状态与轮转日志仅保存在本机 `~/.local/state/cswap-window-keeper/`（`state.json`、`keeper.log`、`daemon.lock`）。
- 唤醒调用在独立空工作目录中以 `--safe-mode --tools "" --no-session-persistence` 执行，不会读取你的项目文件、`CLAUDE.md`、hooks 或 MCP 服务。

## 开发与测试

```bash
python3 -m unittest -v test_cswap_window_keeper.py
```

## 开源协议

[MIT](./LICENSE)
