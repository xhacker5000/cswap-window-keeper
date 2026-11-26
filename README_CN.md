# cswap-window-keeper

[English README](./README.md)

一个轻量、零第三方依赖的后台守护脚本，自动保持你本机多账号的 **5 小时滚动使用窗口** 持续运转，同时支持：

- **[`cswap`](https://github.com/realiti4/claude-swap)**（Claude Code 多账号切换工具）
- **`aswap`**（Antigravity CLI / `agy` 多账号切换工具，独立追踪 `gemini` 与 `3p` Claude/GPT 两个额度池）

## 为什么需要它？

订阅账号的 5 小时窗口在重置后，**下一次重置时间是从重置后你发送第一条消息那一刻开始计算的**。如果窗口重置后一直没发消息，空置的时间就白白浪费了。

`cswap-window-keeper` 的解决方式既精准又克制：

1. **分 Provider 独立预约唤醒（不频繁轮询）**：
   - 支持 `cswap list --json` 和 `aswap list --probe-all --json`（默认 `--providers auto` 自动识别本机已安装的工具）。
   - 每个 provider 独立计时唤醒：`cswap` 到点检查时不会多余探测 `aswap`，反之亦然。
   - 记录每个活跃 5h 窗口的 `resetsAt` 并直接睡到 **`resetsAt + 60s`**（如 2h10m 后重置就在 2h11m 唤醒检查）。
   - 内部按 ≤ 30s 分段对比墙钟时间睡眠，Mac 合盖休眠唤醒后能第一时间补检，不会漏掉重置点。
2. **不干扰全局登录的最小 Token 唤醒**：
   - **`cswap`（Claude Code）**：使用隔离 session 模式（`cswap run <slot>`），不切换默认账号：
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
     单次唤醒约消耗 **2 input + 3 output tokens（约 $0.0001）**。
   - **`aswap`（Antigravity CLI / `agy`）**：按额度池（`gemini` / `3p`）分别监控，自动跳过无 5h 窗口的周限额套餐（`fiveHour: null`），通过 `aswap run <slot>` 唤醒并保持全局默认登录不变：
     ```bash
     # Gemini 池（默认模型：gemini-3.8-flash-low）
     aswap run <slot> -- -p "say test" --model gemini-3.8-flash-low --disable-slash-commands --output-format json

     # 3P Claude/GPT 池（默认模型：gpt-oss-120b-medium）
     aswap run <slot> -- -p "say test" --model gpt-oss-120b-medium --disable-slash-commands --output-format json
     ```
3. **尊重缓存与限流机制**：
   - 对 `cswap` 严格尊重 180s usage 缓存（`SERVE_TTL_S`），ping 后等待 ~192s 回查确认精确的新 `resetsAt`，并尊重 HTTP 429 的 `usageRetryAt`。
   - 对 `aswap` 通过 `--probe-all` 实时探测，ping 后约 15s 即可回查确认精确的新 `resetsAt`。
   - 内置同账号/同池 10 分钟防重复 ping 冷却、文件锁防多开；自动跳过已禁用账号、API Key 账号、需重登账号、无 5h 窗口的周限额套餐以及 7 天周配额已用尽（100%）的目标。

---

## 环境与依赖安装

`cswap-window-keeper` 本身仅依赖 **Python 3.9+ 标准库**（无需 `pip install` 任何第三方包）。按需安装你要保活的 CLI 工具即可：

### 1. Python 3.9+
- **macOS**：自带 `/usr/bin/python3`（或 Xcode Command Line Tools）即可直接运行。
- **Linux**：确保 `python3 --version` 为 3.9 及以上版本。

### 2. 使用 `cswap`（Claude Code）
- 安装官方 [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code)：
  ```bash
  npm install -g @anthropic-ai/claude-code
  claude --version
  ```
- 安装 [`realiti4/claude-swap`](https://github.com/realiti4/claude-swap)（建议 v0.27+）：
  ```bash
  # 推荐：使用 uv 安装
  uv tool install claude-swap
  # 或使用 pipx 安装
  pipx install claude-swap

  # 录入你的 Claude 订阅账号
  cswap add
  cswap list --json
  ```

### 3. 使用 `aswap`（Antigravity CLI / `agy`，可选）
- 安装 Antigravity CLI (`agy`) 以及支持 `aswap` 的 `claude-swap` 版本。
- 录入你的 `agy` 账号并验证：
  ```bash
  aswap add
  aswap list --probe-all --json
  ```

> **提示**：请确认 `~/.local/bin` 已加入你的 shell `PATH`。默认 `--providers auto` 会自动忽略未安装的 provider，只运行本机已装好的工具。

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
Description=cswap-window-keeper (5-hour window keeper for cswap & aswap)
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
# 查看后台服务状态、cswap/aswap 各账号 5h 重置时间与下次唤醒时间
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
| `--providers LIST` | `auto` | 监控的 provider：`auto`（自动检测全部）、`cswap`、`aswap` 或 `cswap,aswap` |
| `--grace SECONDS` | `60` | 5h 重置后延迟多少秒再检查/唤醒 |
| `--cooldown SECONDS` | `600` | 同账号/同额度池两次 ping 之间的最小冷却秒数 |
| `--model ALIAS` | `haiku` | `cswap` 唤醒时使用的 Claude 模型别名 |
| `--aswap-pools LIST` | `gemini,3p` | `aswap` 监控的额度池（逗号分隔：`gemini`、`3p`） |
| `--aswap-gemini-model ID` | `gemini-3.8-flash-low` | `aswap` 唤醒 `gemini` 池使用的 `agy` 模型 ID |
| `--aswap-3p-model ID` | `gpt-oss-120b-medium` | `aswap` 唤醒 `3p`（Claude/GPT）池使用的 `agy` 模型 ID |
| `--prompt TEXT` | `say test` | 唤醒空闲窗口时发送的提示词 |
| `--accounts LIST` | *(全部启用账号)* | 仅监控指定账号（支持 `1,2` 或带前缀 `cswap:1,aswap:1`） |
| `--state-dir PATH` | `~/.local/state/cswap-window-keeper` | 状态文件与日志存放目录 |
| `-v, --verbose` | `false` | 输出详细调试日志 |

示例：
```bash
# 仅监控 aswap 的 gemini 池
python3 cswap_window_keeper.py --providers aswap --aswap-pools gemini install

# 同时监控 cswap 和 aswap，重置后延迟 90 秒检查
python3 cswap_window_keeper.py --providers cswap,aswap --grace 90 install
```

---

## 隐私与安全说明

- 本工具**不读取、不保存、不上传任何账号凭证或 OAuth Token**，全部鉴权交由你本机的 `cswap`、`aswap`、`claude` 与 `agy` 完成。
- 运行状态与轮转日志仅保存在本机 `~/.local/state/cswap-window-keeper/`（`state.json`、`keeper.log`、`daemon.lock`）。
- 唤醒调用在独立空工作目录中以最小参数执行（`claude` 使用 `--safe-mode --tools "" --no-session-persistence`，`agy` 使用 `--disable-slash-commands`）。

## 开发与测试

```bash
python3 -m unittest -v test_cswap_window_keeper.py
```

## 开源协议

[MIT](./LICENSE)
