#!/usr/bin/env python3
"""Smart background keeper for Claude subscription 5-hour usage windows via cswap.

How it works:
1. Reads all managed accounts from `cswap list --json`.
2. For each active subscription account:
   - If the 5-hour window is currently active (`fiveHour.resetsAt` in the future),
     records `resetsAt` and schedules the next check at `resetsAt + reset_grace_s`
     (default: 60s, e.g. reset in 2h10m -> check at 2h11m).
   - If the 5-hour window has reset and is idle (no `resetsAt` and `pct == 0`),
     verifies the measurement is fresh (respecting cswap's 180s usage cache TTL),
     then triggers a minimal-token ping via `cswap run <slot> -- -p "say test" ...`
     using haiku + --safe-mode + --tools "" + --system-prompt "" + --effort low
     + --no-session-persistence.
   - After pinging, waits just past cswap's 180s cache TTL (~190s) to fetch and
     record the exact new server-reported `resetsAt`, then sleeps until the next
     reset + grace window.
3. Sleeps in short wall-clock-aware chunks (<= 60s) so macOS sleep/wake cycles
   never cause missed resets.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import fcntl
import glob
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import plistlib
import shutil
import signal
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple


# ---- Constants -------------------------------------------------------------

SERVICE_LABEL = "com.cswap-window-keeper.agent"
DEFAULT_STATE_DIR = Path.home() / ".local" / "state" / "cswap-window-keeper"
INSTALLED_BIN_PATH = Path.home() / ".local" / "bin" / "cswap-window-keeper"
PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / f"{SERVICE_LABEL}.plist"

# cswap serves cached usage younger than SERVE_TTL_S (180s) without hitting
# Anthropic's /api/oauth/usage endpoint. We respect this TTL so we never
# mistake pre-reset or pre-ping cached data for fresh state.
CSWAP_CACHE_TTL_S = 180.0
CACHE_BUFFER_S = 12.0  # extra margin past SERVE_TTL_S

DEFAULT_RESET_GRACE_S = 60.0       # Check 1 minute after reset (e.g. 2h10m -> 2h11m)
DEFAULT_PING_COOLDOWN_S = 600.0    # Never ping the same account twice within 10 min
DEFAULT_RETRY_ERROR_S = 300.0      # Retry interval for transient usage/command errors
DEFAULT_AUTH_RETRY_S = 1800.0      # Slow retry for relogin_required / no_credentials
DEFAULT_MAX_SLEEP_S = 1800.0       # Safety ceiling if no resets are known
SLEEP_CHUNK_S = 30.0               # Wall-clock chunk so macOS sleep/wake wakes promptly
POST_RESET_STALE_TOLERANCE_S = 30.0

PING_PROMPT = "say test"
PING_MODEL = "haiku"
PING_TIMEOUT_S = 120
LIST_TIMEOUT_S = 60

SKIP_FOREVER_STATUSES = frozenset({"api_key"})
SLOW_RETRY_STATUSES = frozenset({
    "relogin_required",
    "no_credentials",
    "foreign_credential",
    "keychain_unavailable",
})
TRANSIENT_STATUSES = frozenset({"unavailable", "token_expired"})


# ---- Time & formatting helpers ---------------------------------------------

def parse_iso_ts(value: Optional[str]) -> Optional[float]:
    if not value or not isinstance(value, str):
        return None
    try:
        s = value.strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return dt.datetime.fromisoformat(s).timestamp()
    except ValueError:
        return None


def fmt_local(ts: Optional[float]) -> str:
    if ts is None:
        return "-"
    local_dt = dt.datetime.fromtimestamp(ts).astimezone()
    return local_dt.strftime("%Y-%m-%d %H:%M:%S %Z")


def fmt_iso_utc(ts: Optional[float]) -> Optional[str]:
    if ts is None:
        return None
    return (
        dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def fmt_duration(seconds: float) -> str:
    neg = seconds < 0
    total = int(round(abs(seconds)))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    parts = []
    if h:
        parts.append(f"{h}h")
    if m or h:
        parts.append(f"{m}m")
    if not h and (s or not parts):
        parts.append(f"{s}s")
    out = " ".join(parts)
    return f"-{out}" if neg else out


# ---- Environment & binary resolution ---------------------------------------

def build_subprocess_env() -> Dict[str, str]:
    """Ensure cswap and claude (including nvm-installed node) are on PATH."""
    env = dict(os.environ)
    extra_dirs: List[str] = []

    home = Path.home()
    candidates = [
        str(home / ".local" / "bin"),
        "/opt/homebrew/bin",
        "/usr/local/bin",
        "/usr/bin",
        "/bin",
    ]
    # Add newest nvm node bin if present (for claude CLI)
    nvm_bins = sorted(glob.glob(str(home / ".nvm" / "versions" / "node" / "*" / "bin")))
    if nvm_bins:
        candidates.insert(0, nvm_bins[-1])

    current_path = env.get("PATH", "")
    existing = [p for p in current_path.split(":") if p]
    for d in candidates:
        if os.path.isdir(d) and d not in existing and d not in extra_dirs:
            extra_dirs.append(d)

    env["PATH"] = ":".join(existing + extra_dirs)
    env.setdefault("HOME", str(home))
    return env


def resolve_binary(name: str, env: Dict[str, str]) -> str:
    found = shutil.which(name, path=env.get("PATH"))
    if found:
        return found
    raise FileNotFoundError(f"Could not locate '{name}' in PATH={env.get('PATH')}")


# ---- Configuration & State -------------------------------------------------

@dataclasses.dataclass
class KeeperConfig:
    state_dir: Path = DEFAULT_STATE_DIR
    reset_grace_s: float = DEFAULT_RESET_GRACE_S
    ping_cooldown_s: float = DEFAULT_PING_COOLDOWN_S
    retry_error_s: float = DEFAULT_RETRY_ERROR_S
    auth_retry_s: float = DEFAULT_AUTH_RETRY_S
    ping_prompt: str = PING_PROMPT
    ping_model: str = PING_MODEL
    account_filter: Optional[List[str]] = None
    dry_run: bool = False

    @property
    def state_file(self) -> Path:
        return self.state_dir / "state.json"

    @property
    def log_file(self) -> Path:
        return self.state_dir / "keeper.log"

    @property
    def lock_file(self) -> Path:
        return self.state_dir / "daemon.lock"

    @property
    def run_cwd(self) -> Path:
        return self.state_dir / "run-cwd"


def account_key(acct: Dict[str, Any]) -> str:
    email = str(acct.get("email") or f"slot-{acct.get('number')}")
    org = str(acct.get("organizationUuid") or "")
    return f"{email}|{org}" if org else email


def matches_filter(acct: Dict[str, Any], flt: Optional[List[str]]) -> bool:
    if not flt:
        return True
    num = str(acct.get("number", ""))
    email = str(acct.get("email", "")).lower()
    alias = str(acct.get("alias", "")).lower()
    for item in flt:
        token = item.strip().lower()
        if token in (num, email, alias):
            return True
    return False


def load_state(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {"version": 1, "accounts": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("accounts"), dict):
            return data
    except (OSError, ValueError):
        pass
    return {"version": 1, "accounts": {}}


def save_state(path: Path, state: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


# ---- Parsing claude --output-format json -----------------------------------

def parse_claude_json_output(stdout: str) -> Optional[Dict[str, Any]]:
    """Extract the final JSON object emitted by `claude --output-format json`.

    `cswap run` prints a preamble line (e.g. 'Launching Account-2 ... [session mode]')
    before exec'ing claude, so we scan lines in reverse for a JSON object.
    """
    for line in reversed(stdout.splitlines()):
        stripped = line.strip()
        if not stripped or not stripped.startswith("{"):
            continue
        try:
            obj = json.loads(stripped)
            if isinstance(obj, dict):
                return obj
        except ValueError:
            continue
    return None


# ---- Command Runner (injectable for tests) ---------------------------------

class CswapClient:
    def __init__(self, env: Optional[Dict[str, str]] = None, run_cwd: Optional[Path] = None):
        self.env = env or build_subprocess_env()
        self.run_cwd = run_cwd

    def list_accounts(self) -> Dict[str, Any]:
        cswap_bin = resolve_binary("cswap", self.env)
        proc = subprocess.run(
            [cswap_bin, "list", "--json"],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=LIST_TIMEOUT_S,
            env=self.env,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"cswap list --json failed (rc={proc.returncode}): "
                f"{(proc.stderr or proc.stdout).strip()}"
            )
        return json.loads(proc.stdout)

    def ping_account(
        self,
        slot_number: int,
        prompt: str = PING_PROMPT,
        model: str = PING_MODEL,
    ) -> Dict[str, Any]:
        cswap_bin = resolve_binary("cswap", self.env)
        if self.run_cwd:
            self.run_cwd.mkdir(parents=True, exist_ok=True)
        cmd = [
            cswap_bin,
            "run",
            str(slot_number),
            "--",
            "-p",
            prompt,
            "--model",
            model,
            "--tools",
            "",
            "--system-prompt",
            "",
            "--safe-mode",
            "--no-session-persistence",
            "--effort",
            "low",
            "--output-format",
            "json",
        ]
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            cwd=str(self.run_cwd) if self.run_cwd else None,
            timeout=PING_TIMEOUT_S,
            env=self.env,
        )
        parsed = parse_claude_json_output(proc.stdout)
        if proc.returncode != 0:
            err_detail = ""
            if parsed and parsed.get("result"):
                err_detail = str(parsed.get("result"))
            else:
                err_detail = (proc.stderr or proc.stdout).strip()[-400:]
            return {
                "ok": False,
                "returncode": proc.returncode,
                "error": err_detail or f"exit code {proc.returncode}",
                "raw": parsed,
            }
        if not parsed:
            return {
                "ok": False,
                "returncode": proc.returncode,
                "error": f"unparseable claude JSON output: {proc.stdout.strip()[-200:]}",
                "raw": None,
            }
        if parsed.get("is_error"):
            return {
                "ok": False,
                "returncode": proc.returncode,
                "error": str(parsed.get("result") or parsed.get("subtype") or "claude returned is_error=true"),
                "raw": parsed,
            }
        usage = parsed.get("usage") or {}
        model_usage = parsed.get("modelUsage") or {}
        model_name = next(iter(model_usage.keys()), model)
        return {
            "ok": True,
            "returncode": 0,
            "result": str(parsed.get("result", "")).strip(),
            "model": model_name,
            "input_tokens": usage.get("input_tokens", 0),
            "output_tokens": usage.get("output_tokens", 0),
            "cache_creation_tokens": usage.get("cache_creation_input_tokens", 0),
            "cache_read_tokens": usage.get("cache_read_input_tokens", 0),
            "cost_usd": parsed.get("total_cost_usd"),
            "duration_ms": parsed.get("duration_ms"),
        }


# ---- Core Decision Engine --------------------------------------------------

@dataclasses.dataclass
class AccountDecision:
    key: str
    slot: int
    email: str
    action: str  # "wait_reset" | "ping" | "wait_cache" | "wait_verify" | "wait_7d" | "skip" | "retry"
    next_check_at: Optional[float]
    reason: str
    resets_at: Optional[float] = None


def evaluate_account(
    acct: Dict[str, Any],
    acct_state: Dict[str, Any],
    now: float,
    cfg: KeeperConfig,
) -> AccountDecision:
    key = account_key(acct)
    slot = int(acct.get("number", 0))
    email = str(acct.get("email") or f"slot-{slot}")

    if acct.get("disabled"):
        return AccountDecision(
            key=key, slot=slot, email=email,
            action="skip", next_check_at=None, reason="account disabled in cswap",
        )

    if not matches_filter(acct, cfg.account_filter):
        return AccountDecision(
            key=key, slot=slot, email=email,
            action="skip", next_check_at=None, reason="excluded by --accounts filter",
        )

    status = str(acct.get("usageStatus") or "unavailable")
    if status in SKIP_FOREVER_STATUSES:
        return AccountDecision(
            key=key, slot=slot, email=email,
            action="skip", next_check_at=None, reason=f"usageStatus={status}",
        )

    if status in SLOW_RETRY_STATUSES:
        return AccountDecision(
            key=key, slot=slot, email=email,
            action="retry", next_check_at=now + cfg.auth_retry_s,
            reason=f"usageStatus={status} (needs manual attention)",
        )

    if status in TRANSIENT_STATUSES or status != "ok":
        retry_at = parse_iso_ts(acct.get("usageRetryAt"))
        if retry_at is not None and retry_at > now:
            wake = retry_at + 15.0
            err = acct.get("usageError") or status
            return AccountDecision(
                key=key, slot=slot, email=email,
                action="retry", next_check_at=wake,
                reason=f"usageStatus={status} ({err}), respecting usageRetryAt",
            )
        return AccountDecision(
            key=key, slot=slot, email=email,
            action="retry", next_check_at=now + cfg.retry_error_s,
            reason=f"usageStatus={status} ({acct.get('usageError') or 'transient'})",
        )

    # status == "ok"
    usage = acct.get("usage") or {}
    five_hour = usage.get("fiveHour")
    if not isinstance(five_hour, dict):
        return AccountDecision(
            key=key, slot=slot, email=email,
            action="retry", next_check_at=now + cfg.retry_error_s,
            reason="fiveHour window missing in usage payload",
        )

    fetched_at = parse_iso_ts(acct.get("usageFetchedAt"))
    resets_at = parse_iso_ts(five_hour.get("resetsAt"))
    pct = float(five_hour.get("pct") or 0.0)
    last_ping_at: Optional[float] = acct_state.get("last_ping_at")

    # Case 1: Active 5-hour window with resetsAt in the future.
    if resets_at is not None and resets_at > now:
        return AccountDecision(
            key=key, slot=slot, email=email,
            action="wait_reset",
            next_check_at=resets_at + cfg.reset_grace_s,
            resets_at=resets_at,
            reason=f"active 5h window (pct={pct:.0f}%, resets {fmt_local(resets_at)})",
        )

    # Case 2: resetsAt is in the past -> likely served from cswap's 180s cache
    # taken before the reset occurred. Wait for cache to expire before deciding.
    if resets_at is not None and resets_at <= now:
        fetched_after_reset = (
            fetched_at is not None
            and fetched_at >= resets_at + POST_RESET_STALE_TOLERANCE_S
        )
        if not fetched_after_reset:
            cache_expires = (
                (fetched_at + CSWAP_CACHE_TTL_S + CACHE_BUFFER_S)
                if fetched_at is not None
                else (now + 60.0)
            )
            wake = max(now + 30.0, cache_expires)
            return AccountDecision(
                key=key, slot=slot, email=email,
                action="wait_cache",
                next_check_at=wake,
                resets_at=resets_at,
                reason="cached pre-reset reading; waiting for cswap cache TTL to expire",
            )
        if pct > 0.0:
            # Fresh fetch after reset still reports >0% and past resetsAt;
            # wait briefly for Anthropic's backend to finish rolling the window.
            return AccountDecision(
                key=key, slot=slot, email=email,
                action="wait_cache",
                next_check_at=now + CSWAP_CACHE_TTL_S + CACHE_BUFFER_S,
                resets_at=resets_at,
                reason=f"backend rollover lag (pct={pct:.0f}% with past resetsAt)",
            )
        # Else: fresh fetch after reset shows pct == 0 -> fall through to idle handling.

    # Case 3: No future resetsAt (window has reset & is idle, e.g. fiveHour={"pct": 0.0}).
    if pct > 0.0:
        # Unusual: usage > 0 without resetsAt; do not ping blindly.
        return AccountDecision(
            key=key, slot=slot, email=email,
            action="retry",
            next_check_at=now + cfg.retry_error_s,
            reason=f"no resetsAt but pct={pct:.0f}%; waiting for next refresh",
        )

    # Guard A: Did we JUST ping this account and cswap is still serving pre-ping cache?
    if last_ping_at is not None:
        if fetched_at is None or fetched_at < last_ping_at + 5.0:
            base = max(fetched_at or last_ping_at, last_ping_at)
            wake = max(now + 30.0, base + CSWAP_CACHE_TTL_S + CACHE_BUFFER_S)
            if now - last_ping_at < cfg.ping_cooldown_s:
                return AccountDecision(
                    key=key, slot=slot, email=email,
                    action="wait_verify",
                    next_check_at=wake,
                    reason="waiting for post-ping usage refresh to record new resetsAt",
                )
        # Guard B: Cooldown protection against rapid repeat pings (e.g. backend delay).
        if now - last_ping_at < cfg.ping_cooldown_s:
            wake = min(
                last_ping_at + cfg.ping_cooldown_s,
                max(now + 60.0, (fetched_at or now) + CSWAP_CACHE_TTL_S + CACHE_BUFFER_S),
            )
            return AccountDecision(
                key=key, slot=slot, email=email,
                action="wait_verify",
                next_check_at=wake,
                reason=f"ping cooldown active ({fmt_duration(cfg.ping_cooldown_s - (now - last_ping_at))} left)",
            )

    # Guard C: If 7-day quota is completely exhausted (>= 100%), pinging would fail.
    seven_day = usage.get("sevenDay")
    if isinstance(seven_day, dict):
        seven_pct = float(seven_day.get("pct") or 0.0)
        if seven_pct >= 100.0:
            seven_reset = parse_iso_ts(seven_day.get("resetsAt"))
            wake = (
                (seven_reset + cfg.reset_grace_s)
                if (seven_reset is not None and seven_reset > now)
                else (now + cfg.auth_retry_s)
            )
            return AccountDecision(
                key=key, slot=slot, email=email,
                action="wait_7d",
                next_check_at=wake,
                reason=f"7-day quota exhausted ({seven_pct:.0f}%); skipping ping until weekly reset",
            )

    # Guard D: Make sure the idle reading wasn't cached BEFORE the previous known reset.
    prev_resets_at: Optional[float] = acct_state.get("last_resets_at")
    if (
        prev_resets_at is not None
        and fetched_at is not None
        and fetched_at < prev_resets_at
        and now < fetched_at + CSWAP_CACHE_TTL_S + CACHE_BUFFER_S
    ):
        wake = fetched_at + CSWAP_CACHE_TTL_S + CACHE_BUFFER_S
        return AccountDecision(
            key=key, slot=slot, email=email,
            action="wait_cache",
            next_check_at=wake,
            reason="idle reading predates last known reset; waiting for fresh fetch",
        )

    # Ready to ping!
    return AccountDecision(
        key=key, slot=slot, email=email,
        action="ping",
        next_check_at=now + CSWAP_CACHE_TTL_S + CACHE_BUFFER_S,
        reason="5h window reset and idle (pct=0%, no active resetsAt)",
    )


# ---- Keeper Daemon ---------------------------------------------------------

class WindowKeeper:
    def __init__(
        self,
        cfg: KeeperConfig,
        client: Optional[CswapClient] = None,
        clock: Callable[[], float] = time.time,
        logger: Optional[logging.Logger] = None,
    ):
        self.cfg = cfg
        self.client = client or CswapClient(run_cwd=cfg.run_cwd)
        self.clock = clock
        self.log = logger or logging.getLogger("cswap_window_keeper")
        self.state = load_state(cfg.state_file)
        self.consecutive_list_errors = 0

    def tick(self) -> float:
        """Run one evaluation pass across all accounts; return next wake epoch."""
        now = self.clock()
        try:
            payload = self.client.list_accounts()
            self.consecutive_list_errors = 0
        except Exception as exc:
            self.consecutive_list_errors += 1
            backoff = min(600.0, 60.0 * (2 ** min(self.consecutive_list_errors - 1, 4)))
            self.log.error(
                "Failed to query cswap list --json: %s (retrying in %s)",
                exc,
                fmt_duration(backoff),
            )
            self.state["last_error"] = str(exc)
            self.state["last_check_at"] = now
            save_state(self.cfg.state_file, self.state)
            return now + backoff

        accounts = payload.get("accounts") or []
        accounts_state: Dict[str, Any] = self.state.setdefault("accounts", {})
        next_wake: Optional[float] = None
        seen_keys = set()

        for acct in accounts:
            key = account_key(acct)
            seen_keys.add(key)
            st = accounts_state.setdefault(key, {})
            st["slot"] = acct.get("number")
            st["email"] = acct.get("email")
            st["active"] = bool(acct.get("active"))
            st["usage_status"] = acct.get("usageStatus")
            st["last_seen_at"] = now

            decision = evaluate_account(acct, st, now, self.cfg)
            st["last_action"] = decision.action
            st["last_reason"] = decision.reason

            if decision.action == "wait_reset" and decision.resets_at is not None:
                prev = st.get("last_resets_at")
                if prev != decision.resets_at:
                    if st.get("pending_verify"):
                        self.log.info(
                            "[Account-%d %s] Confirmed new 5h window: resets at %s (next check in %s)",
                            decision.slot,
                            decision.email,
                            fmt_local(decision.resets_at),
                            fmt_duration((decision.next_check_at or now) - now),
                        )
                    else:
                        self.log.info(
                            "[Account-%d %s] Tracking 5h window: resets at %s (next check at %s, in %s)",
                            decision.slot,
                            decision.email,
                            fmt_local(decision.resets_at),
                            fmt_local(decision.next_check_at),
                            fmt_duration((decision.next_check_at or now) - now),
                        )
                st["last_resets_at"] = decision.resets_at
                st["last_resets_at_iso"] = fmt_iso_utc(decision.resets_at)
                st["pending_verify"] = False
                st["next_check_at"] = decision.next_check_at

            elif decision.action == "ping":
                if self.cfg.dry_run:
                    self.log.info(
                        "[Account-%d %s] [DRY-RUN] Would ping via `cswap run %d` (%s)",
                        decision.slot,
                        decision.email,
                        decision.slot,
                        decision.reason,
                    )
                    decision.next_check_at = now + self.cfg.retry_error_s
                    st["next_check_at"] = decision.next_check_at
                else:
                    self.log.info(
                        "[Account-%d %s] 5h window is idle -> pinging via `cswap run %d` with %r (%s)",
                        decision.slot,
                        decision.email,
                        decision.slot,
                        self.cfg.ping_prompt,
                        self.cfg.ping_model,
                    )
                    ping_now = self.clock()
                    result = self.client.ping_account(
                        decision.slot,
                        prompt=self.cfg.ping_prompt,
                        model=self.cfg.ping_model,
                    )
                    if result.get("ok"):
                        st["last_ping_at"] = ping_now
                        st["last_ping_at_iso"] = fmt_iso_utc(ping_now)
                        st["last_ping_result"] = result
                        st["last_ping_error"] = None
                        st["ping_count"] = int(st.get("ping_count", 0)) + 1
                        st["pending_verify"] = True
                        st["estimated_resets_at"] = ping_now + 5 * 3600
                        verify_at = ping_now + CSWAP_CACHE_TTL_S + CACHE_BUFFER_S
                        st["next_check_at"] = verify_at
                        decision.next_check_at = verify_at
                        cost_val = result.get("cost_usd")
                        cost_fmt = f"{cost_val:.6f}" if isinstance(cost_val, (int, float)) else str(cost_val)
                        self.log.info(
                            "[Account-%d %s] Ping OK: reply=%r model=%s in=%s out=%s cache_create=%s cost=$%s duration=%sms -> will verify exact resetsAt at %s",
                            decision.slot,
                            decision.email,
                            result.get("result"),
                            result.get("model"),
                            result.get("input_tokens"),
                            result.get("output_tokens"),
                            result.get("cache_creation_tokens"),
                            cost_fmt,
                            result.get("duration_ms"),
                            fmt_local(verify_at),
                        )
                    else:
                        retry_at = ping_now + self.cfg.retry_error_s
                        st["last_ping_error"] = result.get("error")
                        st["last_ping_error_at"] = ping_now
                        st["next_check_at"] = retry_at
                        decision.next_check_at = retry_at
                        self.log.error(
                            "[Account-%d %s] Ping FAILED: %s (retrying at %s)",
                            decision.slot,
                            decision.email,
                            result.get("error"),
                            fmt_local(retry_at),
                        )

            else:
                st["next_check_at"] = decision.next_check_at
                level = logging.WARNING if decision.action == "retry" else logging.INFO
                self.log.log(
                    level,
                    "[Account-%d %s] %s: %s%s",
                    decision.slot,
                    decision.email,
                    decision.action,
                    decision.reason,
                    (
                        f" (next check at {fmt_local(decision.next_check_at)})"
                        if decision.next_check_at
                        else ""
                    ),
                )

            if decision.next_check_at is not None:
                if next_wake is None or decision.next_check_at < next_wake:
                    next_wake = decision.next_check_at

        # Prune removed accounts from state so status stays clean
        for old_key in list(accounts_state.keys()):
            if old_key not in seen_keys:
                del accounts_state[old_key]

        if next_wake is None:
            next_wake = now + DEFAULT_MAX_SLEEP_S

        # Ensure we don't spin tightly on past timestamps
        next_wake = max(next_wake, self.clock() + 15.0)

        self.state["last_check_at"] = now
        self.state["last_check_at_iso"] = fmt_iso_utc(now)
        self.state["next_wake_at"] = next_wake
        self.state["next_wake_at_iso"] = fmt_iso_utc(next_wake)
        self.state["last_error"] = None
        if not self.cfg.dry_run:
            save_state(self.cfg.state_file, self.state)
        return next_wake

    def run_forever(self, stop_event: Optional[threading.Event] = None) -> None:
        stop = stop_event or threading.Event()
        self.log.info(
            "cswap-window-keeper started (pid=%d, grace=%ds, cooldown=%ds, model=%s, state=%s)",
            os.getpid(),
            int(self.cfg.reset_grace_s),
            int(self.cfg.ping_cooldown_s),
            self.cfg.ping_model,
            self.cfg.state_dir,
        )
        while not stop.is_set():
            next_wake = self.tick()
            remaining = next_wake - self.clock()
            self.log.info(
                "Sleeping until %s (%s)",
                fmt_local(next_wake),
                fmt_duration(max(0.0, remaining)),
            )
            # Chunked wall-clock sleep so macOS system sleep/wake never overshoots.
            while not stop.is_set():
                rem = next_wake - self.clock()
                if rem <= 0:
                    break
                stop.wait(timeout=min(SLEEP_CHUNK_S, rem))
        self.log.info("cswap-window-keeper stopped cleanly.")


# ---- Logging & Locking -----------------------------------------------------

def setup_logging(log_file: Path, also_stderr: bool = True, verbose: bool = False) -> logging.Logger:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("cswap_window_keeper")
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    logger.handlers.clear()

    fmt = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    fh = RotatingFileHandler(
        log_file, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    if also_stderr:
        sh = logging.StreamHandler(sys.stderr)
        sh.setFormatter(fmt)
        logger.addHandler(sh)

    return logger


def acquire_singleton_lock(lock_file: Path):
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    fp = open(lock_file, "a+", encoding="utf-8")
    try:
        fcntl.flock(fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fp.seek(0)
        holder = fp.read().strip()
        fp.close()
        raise RuntimeError(
            f"Another cswap-window-keeper instance is already running (lock={lock_file}, pid={holder or '?'})"
        )
    fp.seek(0)
    fp.truncate()
    fp.write(f"{os.getpid()}\n")
    fp.flush()
    return fp


# ---- macOS LaunchAgent Management ------------------------------------------

def build_launchd_plist(script_path: Path, cfg: KeeperConfig) -> Dict[str, Any]:
    env = build_subprocess_env()
    program_args = [
        "/usr/bin/python3",
        str(script_path.resolve()),
        "--state-dir",
        str(cfg.state_dir),
        "--grace",
        str(int(cfg.reset_grace_s)),
        "--cooldown",
        str(int(cfg.ping_cooldown_s)),
        "--model",
        cfg.ping_model,
        "--prompt",
        cfg.ping_prompt,
    ]
    if cfg.account_filter:
        program_args.extend(["--accounts", ",".join(cfg.account_filter)])
    program_args.append("run")

    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    launchd_log = str(cfg.state_dir / "launchd.log")
    return {
        "Label": SERVICE_LABEL,
        "ProgramArguments": program_args,
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 30,
        "ProcessType": "Background",
        "StandardOutPath": launchd_log,
        "StandardErrorPath": launchd_log,
        "EnvironmentVariables": {
            "PATH": env["PATH"],
            "HOME": env["HOME"],
            "PYTHONUNBUFFERED": "1",
        },
    }


def launchctl_domain() -> str:
    return f"gui/{os.getuid()}"


def cmd_install(cfg: KeeperConfig) -> int:
    if sys.platform != "darwin":
        print(
            "LaunchAgent install is macOS-only. On Linux, run `cswap_window_keeper.py run` "
            "under systemd (see README.md for a systemd user service example).",
            file=sys.stderr,
        )
        return 1
    src_script = Path(__file__).resolve()
    # Install a copy into ~/.local/bin/cswap-window-keeper so launchd is never
    # blocked by macOS TCC permissions on ~/Documents or ~/Desktop, and so the
    # user can run `cswap-window-keeper status|logs` from any terminal.
    INSTALLED_BIN_PATH.parent.mkdir(parents=True, exist_ok=True)
    if src_script != INSTALLED_BIN_PATH.resolve():
        shutil.copy2(src_script, INSTALLED_BIN_PATH)
        INSTALLED_BIN_PATH.chmod(0o755)
    run_script = INSTALLED_BIN_PATH

    PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    plist_data = build_launchd_plist(run_script, cfg)

    # Unload any previous instance first (ignore failure if not loaded)
    subprocess.run(
        ["launchctl", "bootout", f"{launchctl_domain()}/{SERVICE_LABEL}"],
        capture_output=True,
        text=True,
    )
    time.sleep(0.5)

    with open(PLIST_PATH, "wb") as fp:
        plistlib.dump(plist_data, fp)

    res = subprocess.run(
        ["launchctl", "bootstrap", launchctl_domain(), str(PLIST_PATH)],
        capture_output=True,
        text=True,
    )
    if res.returncode != 0:
        print(f"Error loading LaunchAgent: {(res.stderr or res.stdout).strip()}", file=sys.stderr)
        return 1

    print(f"Installed & started LaunchAgent: {SERVICE_LABEL}")
    print(f"  Binary: {run_script}")
    print(f"  Plist:  {PLIST_PATH}")
    print(f"  State:  {cfg.state_file}")
    print(f"  Log:    {cfg.log_file}")
    return 0


def cmd_uninstall() -> int:
    if sys.platform != "darwin":
        print("LaunchAgent uninstall is macOS-only.", file=sys.stderr)
        return 1
    subprocess.run(
        ["launchctl", "bootout", f"{launchctl_domain()}/{SERVICE_LABEL}"],
        capture_output=True,
        text=True,
    )
    if PLIST_PATH.exists():
        PLIST_PATH.unlink()
        print(f"Stopped & removed LaunchAgent: {PLIST_PATH}")
    else:
        print(f"LaunchAgent plist not found ({PLIST_PATH}); ensured unloaded.")
    return 0


def service_runtime_status() -> Tuple[bool, Optional[str]]:
    if sys.platform != "darwin":
        return False, None
    res = subprocess.run(
        ["launchctl", "print", f"{launchctl_domain()}/{SERVICE_LABEL}"],
        capture_output=True,
        text=True,
    )
    if res.returncode != 0:
        return False, None
    pid = None
    for line in res.stdout.splitlines():
        s = line.strip()
        if s.startswith("pid = "):
            pid = s.split("=", 1)[1].strip()
            break
    return True, pid


def cmd_status(cfg: KeeperConfig) -> int:
    loaded, pid = service_runtime_status()
    plist_exists = PLIST_PATH.exists()
    print(f"Service: {SERVICE_LABEL}")
    print(f"  Plist:   {PLIST_PATH} ({'installed' if plist_exists else 'not installed'})")
    print(f"  Status:  {'running (pid=' + pid + ')' if loaded and pid else ('loaded (not running)' if loaded else 'stopped')}")
    print(f"  State:   {cfg.state_file}")
    print(f"  Log:     {cfg.log_file}")

    state = load_state(cfg.state_file)
    now = time.time()
    last_check = state.get("last_check_at")
    next_wake = state.get("next_wake_at")
    print()
    print(f"Last check: {fmt_local(last_check)}")
    if next_wake:
        print(f"Next wake:  {fmt_local(next_wake)} (in {fmt_duration(next_wake - now)})")
    if state.get("last_error"):
        print(f"Last error: {state['last_error']}")

    accounts = state.get("accounts") or {}
    if not accounts:
        print("\nNo tracked accounts yet. Run `once` or `install` to initialize.")
        return 0

    print("\nTracked accounts:")
    rows = sorted(accounts.values(), key=lambda x: int(x.get("slot") or 999))
    for st in rows:
        slot = st.get("slot", "?")
        email = st.get("email", "?")
        active_tag = " [active]" if st.get("active") else ""
        action = st.get("last_action", "-")
        resets_at = st.get("last_resets_at")
        next_chk = st.get("next_check_at")
        ping_count = st.get("ping_count", 0)
        last_ping = st.get("last_ping_at")
        print(f"  Account-{slot} ({email}){active_tag}")
        print(f"    Action:     {action} — {st.get('last_reason', '')}")
        if resets_at:
            print(f"    5h Resets:  {fmt_local(resets_at)} ({fmt_duration(resets_at - now)})")
        if next_chk:
            print(f"    Next Check: {fmt_local(next_chk)} ({fmt_duration(next_chk - now)})")
        if last_ping:
            cost = (st.get("last_ping_result") or {}).get("cost_usd")
            cost_str = f", cost=${cost:.6f}" if isinstance(cost, (int, float)) else ""
            print(f"    Last Ping:  {fmt_local(last_ping)} (total pings={ping_count}{cost_str})")
        if st.get("last_ping_error"):
            print(f"    Ping Error: {st['last_ping_error']}")
    return 0


def cmd_logs(cfg: KeeperConfig, lines: int, follow: bool) -> int:
    if not cfg.log_file.exists():
        print(f"Log file does not exist yet: {cfg.log_file}")
        return 0
    args = ["tail", f"-n{lines}"]
    if follow:
        args.append("-f")
    args.append(str(cfg.log_file))
    return subprocess.call(args)


# ---- CLI -------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="cswap-window-keeper",
        description="Smart background daemon that keeps Claude 5-hour windows ticking via cswap.",
    )
    p.add_argument(
        "--state-dir",
        type=Path,
        default=DEFAULT_STATE_DIR,
        help=f"Directory for state.json and logs (default: {DEFAULT_STATE_DIR})",
    )
    p.add_argument(
        "--grace",
        type=float,
        default=DEFAULT_RESET_GRACE_S,
        help=f"Seconds after 5h reset before checking/pinging (default: {int(DEFAULT_RESET_GRACE_S)})",
    )
    p.add_argument(
        "--cooldown",
        type=float,
        default=DEFAULT_PING_COOLDOWN_S,
        help=f"Minimum seconds between pings for the same account (default: {int(DEFAULT_PING_COOLDOWN_S)})",
    )
    p.add_argument(
        "--model",
        default=PING_MODEL,
        help=f"Claude model alias for minimal-token ping (default: {PING_MODEL})",
    )
    p.add_argument(
        "--prompt",
        default=PING_PROMPT,
        help=f"Prompt text sent when waking an idle window (default: {PING_PROMPT!r})",
    )
    p.add_argument(
        "--accounts",
        default=None,
        help="Comma-separated account numbers/emails/aliases to monitor (default: all enabled accounts)",
    )
    p.add_argument("-v", "--verbose", action="store_true", help="Verbose logging")

    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("run", help="Run the keeper daemon in the foreground")

    p_once = sub.add_parser("once", help="Run a single check pass and exit")
    p_once.add_argument("--dry-run", action="store_true", help="Log actions without actually pinging")

    sub.add_parser("status", help="Show LaunchAgent and tracked account status")
    sub.add_parser("install", help="Install and start the macOS LaunchAgent")
    sub.add_parser("uninstall", help="Stop and remove the macOS LaunchAgent")

    p_logs = sub.add_parser("logs", help="View keeper logs")
    p_logs.add_argument("-n", "--lines", type=int, default=40, help="Number of lines to show")
    p_logs.add_argument("-f", "--follow", action="store_true", help="Follow log output")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    acct_filter = None
    if args.accounts:
        acct_filter = [x.strip() for x in args.accounts.split(",") if x.strip()]

    cfg = KeeperConfig(
        state_dir=args.state_dir.expanduser(),
        reset_grace_s=args.grace,
        ping_cooldown_s=args.cooldown,
        ping_model=args.model,
        ping_prompt=args.prompt,
        account_filter=acct_filter,
        dry_run=bool(getattr(args, "dry_run", False)),
    )

    if args.command == "status":
        return cmd_status(cfg)
    if args.command == "install":
        return cmd_install(cfg)
    if args.command == "uninstall":
        return cmd_uninstall()
    if args.command == "logs":
        return cmd_logs(cfg, args.lines, args.follow)

    logger = setup_logging(cfg.log_file, also_stderr=True, verbose=args.verbose)

    if args.command == "once":
        keeper = WindowKeeper(cfg, logger=logger)
        next_wake = keeper.tick()
        logger.info("Single pass done. Suggested next wake: %s", fmt_local(next_wake))
        return 0

    if args.command == "run":
        try:
            lock_fp = acquire_singleton_lock(cfg.lock_file)
        except RuntimeError as exc:
            logger.error("%s", exc)
            return 1

        stop_event = threading.Event()

        def _handle_signal(signum: int, _frame: Any) -> None:
            logger.info("Received signal %d; shutting down...", signum)
            stop_event.set()

        signal.signal(signal.SIGTERM, _handle_signal)
        signal.signal(signal.SIGINT, _handle_signal)

        try:
            keeper = WindowKeeper(cfg, logger=logger)
            keeper.run_forever(stop_event=stop_event)
        finally:
            try:
                fcntl.flock(lock_fp.fileno(), fcntl.LOCK_UN)
                lock_fp.close()
            except OSError:
                pass
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
