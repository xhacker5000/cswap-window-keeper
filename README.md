# cswap-window-keeper

[中文文档](./README_CN.md)

A lightweight, zero-dependency background daemon that keeps your **5-hour rolling usage windows** ticking automatically for both:

- **[`cswap`](https://github.com/realiti4/claude-swap)** (Claude Code multi-account switcher)
- **`aswap`** (Antigravity CLI / `agy` multi-account switcher — tracks both `gemini` and `3p` Claude/GPT model pools independently)

## Why?

Subscription 5-hour rolling windows reset **5 hours after the first message sent following a reset**. If a window resets while you're away and sits idle for 2 hours, those 2 hours don't count toward the next reset.

`cswap-window-keeper` solves this cleanly without noisy polling or disturbing your active logins:

1. **Precision per-provider scheduling (zero noisy polling)**:
   - Reads `cswap list --json` and/or `aswap list --probe-all --json` (`--providers auto` automatically monitors whichever tools are installed).
   - Each provider is scheduled independently: waking up for a `cswap` reset never triggers unnecessary `aswap` probes, and vice versa.
   - Records each active 5-hour `resetsAt` and sleeps until **`resetsAt + 60s`** (e.g. if reset is in 2h 10m, it wakes at 2h 11m).
   - Uses bounded wall-clock sleep chunks (≤ 30s) so macOS sleep/wake cycles never cause missed resets.
2. **Minimal-token wake-up without disturbing your global login**:
   - **`cswap` (Claude Code)**: runs in isolated session mode (`cswap run <slot>`):
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
     Typical cost per wake-up: **~2 input + 3 output tokens (~$0.0001)**.
   - **`aswap` (Antigravity CLI / `agy`)**: tracks each quota pool (`gemini` and `3p`) separately, automatically skips weekly-only tiers (`fiveHour: null`), and runs `aswap run <slot>` (preserves global active login):
     ```bash
     # Gemini pool (default: gemini-3.8-flash-low)
     aswap run <slot> -- -p "say test" --model gemini-3.8-flash-low --disable-slash-commands --output-format json

     # 3P Claude/GPT pool (default: gpt-oss-120b-medium)
     aswap run <slot> -- -p "say test" --model gpt-oss-120b-medium --disable-slash-commands --output-format json
     ```
3. **Cache-aware & rate-limit safe**:
   - For `cswap`, respects the 180s usage cache TTL (`SERVE_TTL_S`): avoids misreading stale pre-reset/pre-ping cache, waits ~192s after a ping to confirm the exact server-assigned `resetsAt`, and respects HTTP 429 `usageRetryAt`.
   - For `aswap`, probes live via `--probe-all` and confirms the new `resetsAt` ~15s after pinging.
   - Enforces a 10-minute per-target ping cooldown, skips disabled accounts, API-key accounts, accounts needing re-login, weekly-only tiers, and targets whose 7-day weekly quota is at 100%.
   - Uses an exclusive file lock (`flock`) so duplicate instances can never race.

---

## Prerequisites & Dependencies

`cswap-window-keeper` uses **Python 3.9+ standard library only** (zero third-party Python packages). Install the CLI provider(s) you want to keep alive:

### 1. Python 3.9+
- **macOS**: `/usr/bin/python3` (built-in / Xcode Command Line Tools) works out of the box.
- **Linux**: `python3 --version` (3.9 or newer).

### 2. For `cswap` (Claude Code)
- Install [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code):
  ```bash
  npm install -g @anthropic-ai/claude-code
  claude --version
  ```
- Install [`realiti4/claude-swap`](https://github.com/realiti4/claude-swap) (v0.27+ recommended):
  ```bash
  # Recommended: using uv
  uv tool install claude-swap
  # Or using pipx
  pipx install claude-swap

  # Register your Claude subscription accounts
  cswap add
  cswap list --json
  ```

### 3. For `aswap` (Antigravity CLI / `agy`, optional)
- Install Antigravity CLI (`agy`) and an `aswap`-enabled build of `claude-swap`.
- Register your `agy` accounts and verify:
  ```bash
  aswap add
  aswap list --probe-all --json
  ```

> **Note**: Make sure `~/.local/bin` (where `cswap`, `aswap`, `agy`, and `cswap-window-keeper` live) is in your shell `PATH`. When `--providers auto` (the default) is used, any uninstalled provider is automatically ignored.

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

Then enable and start it:
```bash
systemctl --user daemon-reload
systemctl --user enable --now cswap-window-keeper
```

---

## Usage

Once installed on macOS (or invoke via `python3 cswap_window_keeper.py ...`):

```bash
# Show daemon status, tracked cswap/aswap accounts, 5h reset times, and next wake time
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
| `--providers LIST` | `auto` | Providers to monitor: `auto` (all installed), `cswap`, `aswap`, or `cswap,aswap` |
| `--grace SECONDS` | `60` | Seconds to wait after `resetsAt` before checking/pinging |
| `--cooldown SECONDS` | `600` | Minimum seconds between pings for the same account/pool |
| `--model ALIAS` | `haiku` | Claude model alias for `cswap` minimal-token pings |
| `--aswap-pools LIST` | `gemini,3p` | Comma-separated `aswap` pools to monitor (`gemini`, `3p`) |
| `--aswap-gemini-model ID` | `gemini-3.8-flash-low` | `agy` model ID used to wake the `gemini` pool |
| `--aswap-3p-model ID` | `gpt-oss-120b-medium` | `agy` model ID used to wake the `3p` (Claude/GPT) pool |
| `--prompt TEXT` | `say test` | Prompt sent when waking an idle window |
| `--accounts LIST` | *(all enabled)* | Accounts to monitor (supports `1,2` or prefixed `cswap:1,aswap:1`) |
| `--state-dir PATH` | `~/.local/state/cswap-window-keeper` | Directory for `state.json`, `keeper.log`, and lock file |
| `-v, --verbose` | `false` | Enable verbose debug logging |

Examples:
```bash
# Monitor only aswap (gemini pool only)
python3 cswap_window_keeper.py --providers aswap --aswap-pools gemini install

# Monitor both cswap and aswap with custom grace period
python3 cswap_window_keeper.py --providers cswap,aswap --grace 90 install
```

---

## Privacy & Security

- This tool **never reads, stores, or transmits credentials or OAuth tokens**. All authentication is delegated to your local `cswap`, `aswap`, `claude`, and `agy` installations.
- Local runtime files live strictly under `~/.local/state/cswap-window-keeper/` (`state.json`, rotating `keeper.log`, `daemon.lock`).
- Wake-up pings run inside an empty working directory with minimal flags (`--safe-mode --tools "" --no-session-persistence` for `claude`; `--disable-slash-commands` for `agy`).

## Development & Tests

```bash
python3 -m unittest -v test_cswap_window_keeper.py
```

## License

[MIT](./LICENSE)
