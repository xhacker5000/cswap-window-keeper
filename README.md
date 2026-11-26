# cswap-window-keeper

[中文文档](./README_CN.md)

A lightweight, zero-dependency background daemon that keeps your Claude subscription accounts' **5-hour rolling usage windows** ticking automatically via [`claude-swap` (`cswap`)](https://github.com/realiti4/claude-swap).

## Why?

Claude subscription plans reset their 5-hour usage window **5 hours after the first message sent following a reset**. If an account resets while you're away and sits idle for 2 hours, those 2 hours don't count toward the next reset.

`cswap-window-keeper` solves this cleanly without polling aggressively or disturbing your active Claude Code login:

1. **Precision wake-up scheduling (no noisy polling)**:
   - Reads all enabled subscription accounts via `cswap list --json`.
   - If an account's 5-hour window is active (`fiveHour.resetsAt` in the future), it records the exact reset timestamp and sleeps until **`resetsAt + 60s`** (e.g. if reset is in 2h 10m, it wakes at 2h 11m).
   - Uses bounded wall-clock sleep chunks (≤ 30s) so macOS sleep/wake cycles never cause missed resets.
2. **Minimal-token wake-up in isolated session mode**:
   - After a reset, if nobody has used the account yet (`pct == 0` and no active `resetsAt`), it triggers a single minimal-token ping using `cswap run <slot>` (session mode — **never switches your active default login**):
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
   - Typical cost per wake-up: **~2 input + 3 output tokens (~$0.0001)**.
3. **Cache-aware & rate-limit safe**:
   - Respects `cswap`'s 180-second usage cache TTL (`SERVE_TTL_S`): avoids misreading stale pre-reset or pre-ping snapshots, waits ~192s after a ping to confirm and record the exact server-assigned `resetsAt`, then goes back to deep sleep for ~5 hours.
   - Respects `usageRetryAt` on HTTP 429 responses.
   - Enforces a 10-minute per-account ping cooldown, skips disabled accounts, API-key accounts, accounts requiring re-login, and accounts whose 7-day weekly quota is at 100%.
   - Uses an exclusive file lock (`flock`) so duplicate instances can never race.

---

## Prerequisites & Dependencies

`cswap-window-keeper` itself uses **Python 3.9+ standard library only** (zero third-party Python packages). It relies on the following CLI tools being installed and configured on your machine:

### 1. Python 3.9+
- **macOS**: `/usr/bin/python3` (built-in / Xcode Command Line Tools) works out of the box.
- **Linux**: `python3 --version` (3.9 or newer).

### 2. Claude Code CLI (`claude`)
Install [Claude Code](https://docs.anthropic.com/en/docs/claude-code):
```bash
# Via npm (Node.js 18+)
npm install -g @anthropic-ai/claude-code

# Verify
claude --version
```

### 3. `claude-swap` (`cswap`)
Install [`realiti4/claude-swap`](https://github.com/realiti4/claude-swap) (v0.27+ recommended, with `list --json` and `run` session mode support):

```bash
# Recommended: using uv
uv tool install claude-swap

# Or using pipx
pipx install claude-swap

# Verify
cswap --version
```

Register the Claude subscription accounts you want to manage:
```bash
# Log into Claude Code with each subscription account, then add it to cswap:
cswap add

# Verify cswap can see all accounts and their 5-hour windows:
cswap list --json
```

> **Note**: Make sure `~/.local/bin` (where `cswap` and `cswap-window-keeper` are installed) is in your shell `PATH`.

---

## Installation & Quick Start

### Clone the repository

```bash
git clone https://github.com/xhacker5000/cswap-window-keeper.git
cd cswap-window-keeper
```

### Verify with a dry run (does not send any messages)

```bash
python3 cswap_window_keeper.py once --dry-run
```

### macOS: Install as a background LaunchAgent (recommended)

```bash
python3 cswap_window_keeper.py install
```

What `install` does:
- Copies `cswap_window_keeper.py` to `~/.local/bin/cswap-window-keeper` (avoids macOS TCC permission blocks on `~/Documents` or `~/Desktop` and makes `cswap-window-keeper` globally callable).
- Creates and starts `~/Library/LaunchAgents/com.cswap-window-keeper.agent.plist` (`RunAtLoad` + `KeepAlive`, runs in your user GUI session with Keychain access).

### Linux: Run with `systemd` user service

Create `~/.config/systemd/user/cswap-window-keeper.service`:

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

Then enable and start it:
```bash
systemctl --user daemon-reload
systemctl --user enable --now cswap-window-keeper
```

---

## Usage

Once installed on macOS (or invoke via `python3 cswap_window_keeper.py ...`):

```bash
# Show daemon status, tracked accounts, 5h reset times, and next wake time
cswap-window-keeper status

# View recent logs / follow logs in real time
cswap-window-keeper logs -n 50
cswap-window-keeper logs -f

# Run a single check pass (or dry run without pinging)
cswap-window-keeper once
cswap-window-keeper once --dry-run

# Run daemon in the foreground
cswap-window-keeper run

# Stop and remove the macOS LaunchAgent
cswap-window-keeper uninstall
```

### CLI Options

Place global options **before** the subcommand (`install`, `run`, `once`, etc.):

| Option | Default | Description |
| --- | --- | --- |
| `--grace SECONDS` | `60` | Seconds to wait after `resetsAt` before checking/pinging |
| `--cooldown SECONDS` | `600` | Minimum seconds between pings for the same account |
| `--model ALIAS` | `haiku` | Claude model alias used for minimal-token wake-up pings |
| `--prompt TEXT` | `say test` | Prompt sent when waking an idle window |
| `--accounts LIST` | *(all enabled)* | Comma-separated slot numbers, emails, or aliases to monitor |
| `--state-dir PATH` | `~/.local/state/cswap-window-keeper` | Directory for `state.json`, `keeper.log`, and lock file |
| `-v, --verbose` | `false` | Enable verbose debug logging |

Example — monitor only accounts `1` and `2` with a 90-second post-reset grace period:
```bash
python3 cswap_window_keeper.py --grace 90 --accounts 1,2 install
```

---

## Privacy & Security

- This tool **never reads, stores, or transmits credentials or OAuth tokens**. All authentication is delegated to your local `cswap` and `claude` installations.
- Local runtime files live strictly under `~/.local/state/cswap-window-keeper/` (`state.json`, rotating `keeper.log`, `daemon.lock`).
- Pings run with `--safe-mode --tools "" --no-session-persistence` inside an empty working directory, so no local project files, `CLAUDE.md`, hooks, or MCP servers are accessed.

## Development & Tests

```bash
python3 -m unittest -v test_cswap_window_keeper.py
```

## License

[MIT](./LICENSE)
