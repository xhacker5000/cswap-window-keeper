#!/usr/bin/env python3
"""Smart background keeper for 5-hour rolling usage windows via cswap & aswap.

Supports both:
- `cswap` (Claude Code multi-account switcher): tracks each enabled subscription
  account's 5-hour window (`usage.fiveHour`), waking at `resetsAt + grace` and
  pinging idle windows via `cswap run <slot> -- -p "say test" --model haiku ...`.
- `aswap` (Antigravity CLI / `agy` multi-account switcher): probes all enabled
  accounts via `aswap list --probe-all --json`, tracks each pool with a 5-hour
  window (`gemini`, `3p`) independently, skips weekly-only tiers (`fiveHour: null`),
  and pings idle pools via `aswap run <slot> -- -p "say test" --model <pool_model> ...`
  without changing the active global login.

Each provider is scheduled independently so waking up for `cswap` never triggers
unnecessary `aswap` probes (and vice versa). Sleeps in short wall-clock-aware
chunks (<= 30s) so macOS sleep/wake cycles never cause missed resets.
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

SUPPORTED_PROVIDERS = ("cswap", "aswap")

# cswap serves cached usage younger than SERVE_TTL_S (180s) without hitting
# Anthropic's /api/oauth/usage endpoint. We respect this TTL so we never
# mistake pre-reset or pre-ping cached data for fresh state.
CSWAP_CACHE_TTL_S = 180.0
CACHE_BUFFER_S = 12.0  # extra margin past SERVE_TTL_S

# aswap list --probe-all queries live `agy -p /usage` directly (no 180s cache),
# so we only need a short settle delay after pinging before confirming resetsAt.
ASWAP_VERIFY_DELAY_S = 15.0

DEFAULT_RESET_GRACE_S = 60.0       # Check 1 minute after reset (e.g. 2h10m -> 2h11m)
DEFAULT_PING_COOLDOWN_S = 600.0    # Never ping the same target twice within 10 min
DEFAULT_RETRY_ERROR_S = 300.0      # Retry interval for transient usage/command errors
DEFAULT_AUTH_RETRY_S = 1800.0      # Slow retry for relogin_required / no_credentials
DEFAULT_MAX_SLEEP_S = 1800.0       # Safety ceiling if no resets are known
SLEEP_CHUNK_S = 30.0               # Wall-clock chunk so macOS sleep/wake wakes promptly
POST_RESET_STALE_TOLERANCE_S = 30.0

PING_PROMPT = "say test"
CSWAP_PING_MODEL = "haiku"
ASWAP_DEFAULT_POOLS = ("gemini", "3p")
ASWAP_GEMINI_MODEL = "gemini-3.8-flash-low"
ASWAP_3P_MODEL = "gpt-oss-120b-medium"

PING_TIMEOUT_S = 120
LIST_TIMEOUT_S = 60
ASWAP_PROBE_TIMEOUT_S = 120

SKIP_FOREVER_STATUSES = frozenset({"api_key"})
SLOW_RETRY_STATUSES = frozenset({
    "relogin_required",
    "no_credentials",
    "foreign_credential",
    "keychain_unavailable",
})
TRANSIENT_STATUSES = frozenset({"unavailable", "token_expired", "error", "unknown"})


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
    """Ensure cswap, aswap, claude (including nvm), and agy are on PATH."""
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


def detect_available_providers(env: Optional[Dict[str, str]] = None) -> List[str]:
    e = env or build_subprocess_env()
    found: List[str] = []
    for name in SUPPORTED_PROVIDERS:
        if shutil.which(name, path=e.get("PATH")):
            found.append(name)
    return found


def resolve_providers(spec: str, env: Optional[Dict[str, str]] = None) -> List[str]:
    cleaned = (spec or "auto").strip().lower()
    if cleaned in ("", "auto"):
        found = detect_available_providers(env)
        return found or ["cswap"]
    out: List[str] = []
    for token in cleaned.split(","):
        p = token.strip()
        if not p:
            continue
        if p not in SUPPORTED_PROVIDERS:
            raise ValueError(
                f"Unknown provider {p!r}; choose from 'auto' or {', '.join(SUPPORTED_PROVIDERS)}"
            )
        if p not in out:
            out.append(p)
    if not out:
        raise ValueError("At least one provider must be specified")
    return out


# ---- Configuration & State -------------------------------------------------

@dataclasses.dataclass
class KeeperConfig:
    state_dir: Path = DEFAULT_STATE_DIR
    providers: List[str] = dataclasses.field(default_factory=lambda: ["cswap"])
    reset_grace_s: float = DEFAULT_RESET_GRACE_S
    ping_cooldown_s: float = DEFAULT_PING_COOLDOWN_S
    retry_error_s: float = DEFAULT_RETRY_ERROR_S
    auth_retry_s: float = DEFAULT_AUTH_RETRY_S
    ping_prompt: str = PING_PROMPT
    ping_model: str = CSWAP_PING_MODEL
    aswap_pools: List[str] = dataclasses.field(default_factory=lambda: list(ASWAP_DEFAULT_POOLS))
    aswap_gemini_model: str = ASWAP_GEMINI_MODEL
    aswap_3p_model: str = ASWAP_3P_MODEL
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

    def model_for(self, provider: str, pool: Optional[str] = None) -> str:
        if provider == "cswap":
            return self.ping_model
        if pool == "3p":
            return self.aswap_3p_model
        return self.aswap_gemini_model


def target_key(provider: str, acct: Dict[str, Any], pool: Optional[str] = None) -> str:
    email = str(acct.get("email") or f"slot-{acct.get('number')}")
    org = str(acct.get("organizationUuid") or "")
    base = f"{email}|{org}" if org else email
    if pool:
        return f"{provider}:{base}:{pool}"
    return f"{provider}:{base}"


def matches_filter(acct: Dict[str, Any], flt: Optional[List[str]], provider: str = "cswap") -> bool:
    if not flt:
        return True
    num = str(acct.get("number", ""))
    email = str(acct.get("email", "")).lower()
    alias = str(acct.get("alias", "")).lower()
    for item in flt:
        token = item.strip().lower()
        # Allow optional provider prefix like "cswap:1" or "aswap:2"
        if ":" in token:
            pref, rest = token.split(":", 1)
            if pref in SUPPORTED_PROVIDERS:
                if pref != provider:
                    continue
                token = rest
        if token in (num, email, alias):
            return True
    return False


def load_state(path: Path) -> Dict[str, Any]:
    empty: Dict[str, Any] = {"version": 2, "providers": {}, "targets": {}}
    if not path.exists():
        return empty
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return empty
        if isinstance(data.get("targets"), dict):
            data.setdefault("providers", {})
            data["version"] = 2
            return data
        # Migrate v1 state (`accounts` dict from cswap-only release)
        if isinstance(data.get("accounts"), dict):
            targets: Dict[str, Any] = {}
            for old_key, st in data["accounts"].items():
                if not isinstance(st, dict):
                    continue
                new_key = old_key if old_key.startswith("cswap:") else f"cswap:{old_key}"
                migrated = dict(st)
                migrated.setdefault("provider", "cswap")
                migrated.setdefault("pool", None)
                targets[new_key] = migrated
            providers: Dict[str, Any] = {}
            if data.get("last_check_at") or data.get("next_wake_at"):
                providers["cswap"] = {
                    "last_check_at": data.get("last_check_at"),
                    "last_check_at_iso": data.get("last_check_at_iso"),
                    "next_wake_at": data.get("next_wake_at"),
                    "next_wake_at_iso": data.get("next_wake_at_iso"),
                    "last_error": data.get("last_error"),
                }
            return {
                "version": 2,
                "providers": providers,
                "targets": targets,
                "last_check_at": data.get("last_check_at"),
                "last_check_at_iso": data.get("last_check_at_iso"),
                "next_wake_at": data.get("next_wake_at"),
                "next_wake_at_iso": data.get("next_wake_at_iso"),
                "last_error": data.get("last_error"),
            }
    except (OSError, ValueError):
        pass
    return empty


def save_state(path: Path, state: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


# ---- JSON output parsers ---------------------------------------------------

def parse_trailing_json_object(stdout: str) -> Optional[Dict[str, Any]]:
    """Extract the final JSON object emitted by `claude` or `agy` `--output-format json`.

    `cswap run` / `aswap run` may print a preamble line before launching the CLI,
    so we scan lines in reverse for a JSON object (and fall back to full text).
    """
    text = (stdout or "").strip()
    if not text:
        return None
    for line in reversed(text.splitlines()):
        stripped = line.strip()
        if not stripped or not stripped.startswith("{"):
            continue
        try:
            obj = json.loads(stripped)
            if isinstance(obj, dict):
                return obj
        except ValueError:
            continue
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except ValueError:
        pass
    return None


# Backwards-compatible alias
parse_claude_json_output = parse_trailing_json_object


# ---- Provider Clients (injectable for tests) -------------------------------

class CswapClient:
    provider = "cswap"
    cache_ttl_s = CSWAP_CACHE_TTL_S
    verify_delay_s = CSWAP_CACHE_TTL_S + CACHE_BUFFER_S

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
        model: str = CSWAP_PING_MODEL,
        pool: Optional[str] = None,
    ) -> Dict[str, Any]:
        _ = pool
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
        parsed = parse_trailing_json_object(proc.stdout)
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


class AswapClient:
    provider = "aswap"
    # `aswap list --probe-all --json` always queries live `agy -p /usage` for
    # every slot (no 180s cache wait needed), so verify delay is short.
    cache_ttl_s = 0.0
    verify_delay_s = ASWAP_VERIFY_DELAY_S

    def __init__(self, env: Optional[Dict[str, str]] = None, run_cwd: Optional[Path] = None):
        self.env = env or build_subprocess_env()
        self.run_cwd = run_cwd

    def list_accounts(self) -> Dict[str, Any]:
        aswap_bin = resolve_binary("aswap", self.env)
        proc = subprocess.run(
            [aswap_bin, "list", "--probe-all", "--json"],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=ASWAP_PROBE_TIMEOUT_S,
            env=self.env,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"aswap list --probe-all --json failed (rc={proc.returncode}): "
                f"{(proc.stderr or proc.stdout).strip()}"
            )
        return json.loads(proc.stdout)

    def ping_account(
        self,
        slot_number: int,
        prompt: str = PING_PROMPT,
        model: str = ASWAP_GEMINI_MODEL,
        pool: Optional[str] = None,
    ) -> Dict[str, Any]:
        _ = pool
        aswap_bin = resolve_binary("aswap", self.env)
        if self.run_cwd:
            self.run_cwd.mkdir(parents=True, exist_ok=True)
        # Note: agy encodes effort in the model ID (e.g. gemini-3.8-flash-low,
        # gpt-oss-120b-medium), so we deliberately omit `--effort`.
        cmd = [
            aswap_bin,
            "run",
            str(slot_number),
            "--",
            "-p",
            prompt,
            "--model",
            model,
            "--disable-slash-commands",
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
        parsed = parse_trailing_json_object(proc.stdout)
        if proc.returncode != 0:
            err_detail = ""
            if parsed and (parsed.get("error") or parsed.get("response")):
                err_detail = str(parsed.get("error") or parsed.get("response"))
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
                "error": f"unparseable agy JSON output: {proc.stdout.strip()[-200:]}",
                "raw": None,
            }
        status = str(parsed.get("status") or "").upper()
        if status and status != "SUCCESS":
            return {
                "ok": False,
                "returncode": proc.returncode,
                "error": str(parsed.get("error") or f"agy returned status={status}"),
                "raw": parsed,
            }
        usage = parsed.get("usage") or {}
        dur_s = parsed.get("duration_seconds")
        duration_ms = int(round(float(dur_s) * 1000)) if isinstance(dur_s, (int, float)) else None
        return {
            "ok": True,
            "returncode": 0,
            "result": str(parsed.get("response", "")).strip(),
            "model": model,
            "input_tokens": usage.get("input_tokens", 0),
            "output_tokens": usage.get("output_tokens", 0),
            "cache_creation_tokens": 0,
            "cache_read_tokens": usage.get("cache_read_tokens", 0),
            "cost_usd": None,
            "duration_ms": duration_ms,
        }


# ---- Core Decision Engine --------------------------------------------------

@dataclasses.dataclass
class WindowTarget:
    provider: str
    key: str
    slot: int
    email: str
    active: bool
    disabled: bool
    usage_status: str
    pool: Optional[str]
    pool_name: Optional[str]
    five_hour: Optional[Dict[str, Any]]
    five_hour_missing_reason: Optional[str]
    seven_day: Optional[Dict[str, Any]]
    usage_fetched_at: Optional[float]
    usage_error: Optional[str]
    usage_retry_at: Optional[float]
    raw_account: Dict[str, Any]
    cache_ttl_s: float
    verify_delay_s: float


@dataclasses.dataclass
class AccountDecision:
    provider: str
    key: str
    slot: int
    email: str
    action: str  # "wait_reset" | "ping" | "wait_cache" | "wait_verify" | "wait_7d" | "skip" | "retry"
    next_check_at: Optional[float]
    reason: str
    resets_at: Optional[float] = None
    pool: Optional[str] = None
    pool_name: Optional[str] = None


def extract_targets(
    provider: str,
    payload: Dict[str, Any],
    cfg: KeeperConfig,
    cache_ttl_s: float,
    verify_delay_s: float,
) -> List[WindowTarget]:
    """Normalize provider-specific `list --json` payload into uniform WindowTargets."""
    targets: List[WindowTarget] = []
    accounts = payload.get("accounts") or []

    for acct in accounts:
        slot = int(acct.get("number", 0))
        email = str(acct.get("email") or f"slot-{slot}")
        active = bool(acct.get("active"))
        disabled = bool(acct.get("disabled"))
        status = str(acct.get("usageStatus") or "unavailable")
        fetched_at = parse_iso_ts(acct.get("usageFetchedAt"))
        usage_error = acct.get("usageError")
        usage_retry_at = parse_iso_ts(acct.get("usageRetryAt"))
        usage = acct.get("usage") or {}

        if provider == "cswap":
            five_hour = usage.get("fiveHour") if isinstance(usage, dict) else None
            seven_day = usage.get("sevenDay") if isinstance(usage, dict) else None
            targets.append(
                WindowTarget(
                    provider="cswap",
                    key=target_key("cswap", acct, None),
                    slot=slot,
                    email=email,
                    active=active,
                    disabled=disabled,
                    usage_status=status,
                    pool=None,
                    pool_name=None,
                    five_hour=five_hour if isinstance(five_hour, dict) else None,
                    five_hour_missing_reason=(
                        "fiveHour window missing in usage payload"
                        if status == "ok" and not isinstance(five_hour, dict)
                        else None
                    ),
                    seven_day=seven_day if isinstance(seven_day, dict) else None,
                    usage_fetched_at=fetched_at,
                    usage_error=str(usage_error) if usage_error else None,
                    usage_retry_at=usage_retry_at,
                    raw_account=acct,
                    cache_ttl_s=cache_ttl_s,
                    verify_delay_s=verify_delay_s,
                )
            )
            continue

        # provider == "aswap"
        if acct.get("credentialsMissing"):
            status = "no_credentials"

        groups = usage.get("groups") if isinstance(usage, dict) else None
        if status != "ok" or not isinstance(groups, list) or not groups:
            targets.append(
                WindowTarget(
                    provider="aswap",
                    key=target_key("aswap", acct, "all"),
                    slot=slot,
                    email=email,
                    active=active,
                    disabled=disabled,
                    usage_status=status,
                    pool="all",
                    pool_name="all pools",
                    five_hour=None,
                    five_hour_missing_reason=(
                        "no quota groups in usage payload" if status == "ok" else None
                    ),
                    seven_day=None,
                    usage_fetched_at=fetched_at,
                    usage_error=str(usage_error) if usage_error else None,
                    usage_retry_at=usage_retry_at,
                    raw_account=acct,
                    cache_ttl_s=cache_ttl_s,
                    verify_delay_s=verify_delay_s,
                )
            )
            continue

        allowed_pools = {p.strip().lower() for p in cfg.aswap_pools if p.strip()}
        for group in groups:
            if not isinstance(group, dict):
                continue
            pool = str(group.get("pool") or "").strip().lower() or "gemini"
            pool_name = str(group.get("name") or pool)
            if allowed_pools and pool not in allowed_pools:
                continue
            five_hour = group.get("fiveHour")
            seven_day = group.get("sevenDay")
            # On weekly-only agy tiers, `fiveHour` is explicitly `null`.
            no_5h_reason = (
                "no 5h window on this tier (weekly-only)"
                if five_hour is None
                else None
            )
            targets.append(
                WindowTarget(
                    provider="aswap",
                    key=target_key("aswap", acct, pool),
                    slot=slot,
                    email=email,
                    active=active,
                    disabled=disabled,
                    usage_status=status,
                    pool=pool,
                    pool_name=pool_name,
                    five_hour=five_hour if isinstance(five_hour, dict) else None,
                    five_hour_missing_reason=no_5h_reason,
                    seven_day=seven_day if isinstance(seven_day, dict) else None,
                    usage_fetched_at=fetched_at,
                    usage_error=str(usage_error) if usage_error else None,
                    usage_retry_at=usage_retry_at,
                    raw_account=acct,
                    cache_ttl_s=cache_ttl_s,
                    verify_delay_s=verify_delay_s,
                )
            )

    return targets


def evaluate_target(
    target: WindowTarget,
    target_state: Dict[str, Any],
    now: float,
    cfg: KeeperConfig,
) -> AccountDecision:
    def _dec(action: str, next_check_at: Optional[float], reason: str, resets_at: Optional[float] = None) -> AccountDecision:
        return AccountDecision(
            provider=target.provider,
            key=target.key,
            slot=target.slot,
            email=target.email,
            pool=target.pool,
            pool_name=target.pool_name,
            action=action,
            next_check_at=next_check_at,
            reason=reason,
            resets_at=resets_at,
        )

    if target.disabled:
        return _dec("skip", None, f"account disabled in {target.provider}")

    if not matches_filter(target.raw_account, cfg.account_filter, provider=target.provider):
        return _dec("skip", None, "excluded by --accounts filter")

    status = target.usage_status
    if status in SKIP_FOREVER_STATUSES:
        return _dec("skip", None, f"usageStatus={status}")

    if status in SLOW_RETRY_STATUSES:
        return _dec("retry", now + cfg.auth_retry_s, f"usageStatus={status} (needs manual attention)")

    if status in TRANSIENT_STATUSES or status != "ok":
        if target.usage_retry_at is not None and target.usage_retry_at > now:
            wake = target.usage_retry_at + 15.0
            err = target.usage_error or status
            return _dec("retry", wake, f"usageStatus={status} ({err}), respecting usageRetryAt")
        return _dec("retry", now + cfg.retry_error_s, f"usageStatus={status} ({target.usage_error or 'transient'})")

    # status == "ok"
    if target.five_hour is None:
        if target.provider == "aswap" and target.five_hour_missing_reason and "weekly-only" in target.five_hour_missing_reason:
            return _dec("skip", None, target.five_hour_missing_reason)
        return _dec(
            "retry",
            now + cfg.retry_error_s,
            target.five_hour_missing_reason or "fiveHour window missing in usage payload",
        )

    five_hour = target.five_hour
    fetched_at = target.usage_fetched_at
    resets_at = parse_iso_ts(five_hour.get("resetsAt"))
    pct = float(five_hour.get("pct") or 0.0)
    last_ping_at: Optional[float] = target_state.get("last_ping_at")
    cache_ttl = target.cache_ttl_s
    verify_delay = target.verify_delay_s

    # Case 1: Active 5-hour window with resetsAt in the future.
    if resets_at is not None and resets_at > now:
        return _dec(
            "wait_reset",
            resets_at + cfg.reset_grace_s,
            f"active 5h window (pct={pct:.1f}%, resets {fmt_local(resets_at)})",
            resets_at=resets_at,
        )

    # Case 2: resetsAt is in the past -> likely served from cache taken before
    # reset occurred (or backend rollover lag). Wait before deciding.
    if resets_at is not None and resets_at <= now:
        fetched_after_reset = (
            fetched_at is not None
            and fetched_at >= resets_at + POST_RESET_STALE_TOLERANCE_S
        )
        if not fetched_after_reset:
            cache_expires = (
                (fetched_at + cache_ttl + CACHE_BUFFER_S)
                if (fetched_at is not None and cache_ttl > 0)
                else (now + 30.0)
            )
            wake = max(now + 30.0, cache_expires)
            return _dec(
                "wait_cache",
                wake,
                f"cached pre-reset reading; waiting for fresh {target.provider} fetch",
                resets_at=resets_at,
            )
        if pct > 0.0:
            return _dec(
                "wait_cache",
                now + max(60.0, verify_delay),
                f"backend rollover lag (pct={pct:.1f}% with past resetsAt)",
                resets_at=resets_at,
            )
        # Else: fresh fetch after reset shows pct == 0 -> fall through to idle handling.

    # Case 3: No future resetsAt (window has reset & is idle, e.g. fiveHour={"pct": 0.0}).
    if pct > 0.0:
        return _dec(
            "retry",
            now + cfg.retry_error_s,
            f"no resetsAt but pct={pct:.1f}%; waiting for next refresh",
        )

    # Guard A: Did we JUST ping this target and reading predates the ping?
    if last_ping_at is not None:
        if fetched_at is None or fetched_at < last_ping_at + 2.0:
            base = max(fetched_at or last_ping_at, last_ping_at)
            wake = max(now + 15.0, base + verify_delay)
            if now - last_ping_at < cfg.ping_cooldown_s:
                return _dec(
                    "wait_verify",
                    wake,
                    "waiting for post-ping usage refresh to record new resetsAt",
                )
        # Guard B: Cooldown protection against rapid repeat pings.
        if now - last_ping_at < cfg.ping_cooldown_s:
            wake = min(
                last_ping_at + cfg.ping_cooldown_s,
                max(now + 60.0, (fetched_at or now) + verify_delay),
            )
            return _dec(
                "wait_verify",
                wake,
                f"ping cooldown active ({fmt_duration(cfg.ping_cooldown_s - (now - last_ping_at))} left)",
            )

    # Guard C: If 7-day quota is completely exhausted (>= 100%), pinging would fail.
    if isinstance(target.seven_day, dict):
        seven_pct = float(target.seven_day.get("pct") or 0.0)
        if seven_pct >= 100.0:
            seven_reset = parse_iso_ts(target.seven_day.get("resetsAt"))
            wake = (
                (seven_reset + cfg.reset_grace_s)
                if (seven_reset is not None and seven_reset > now)
                else (now + cfg.auth_retry_s)
            )
            return _dec(
                "wait_7d",
                wake,
                f"7-day quota exhausted ({seven_pct:.1f}%); skipping ping until weekly reset",
            )

    # Guard D: Make sure the idle reading wasn't cached BEFORE the previous known reset.
    prev_resets_at: Optional[float] = target_state.get("last_resets_at")
    if (
        cache_ttl > 0
        and prev_resets_at is not None
        and fetched_at is not None
        and fetched_at < prev_resets_at
        and now < fetched_at + cache_ttl + CACHE_BUFFER_S
    ):
        wake = fetched_at + cache_ttl + CACHE_BUFFER_S
        return _dec(
            "wait_cache",
            wake,
            "idle reading predates last known reset; waiting for fresh fetch",
        )

    # Ready to ping!
    return _dec(
        "ping",
        now + verify_delay,
        "5h window reset and idle (pct=0%, no active resetsAt)",
    )


def evaluate_account(
    acct: Dict[str, Any],
    acct_state: Dict[str, Any],
    now: float,
    cfg: KeeperConfig,
) -> AccountDecision:
    """Backwards-compatible single-account evaluator for cswap payloads."""
    targets = extract_targets(
        "cswap",
        {"accounts": [acct]},
        cfg,
        cache_ttl_s=CSWAP_CACHE_TTL_S,
        verify_delay_s=CSWAP_CACHE_TTL_S + CACHE_BUFFER_S,
    )
    return evaluate_target(targets[0], acct_state, now, cfg)


def format_target_label(decision: AccountDecision) -> str:
    pool_part = f" pool={decision.pool}" if decision.pool else ""
    return f"[{decision.provider} Account-{decision.slot} {decision.email}{pool_part}]"


# ---- Keeper Daemon ---------------------------------------------------------

class WindowKeeper:
    def __init__(
        self,
        cfg: KeeperConfig,
        client: Optional[CswapClient] = None,
        clients: Optional[Dict[str, Any]] = None,
        clock: Callable[[], float] = time.time,
        logger: Optional[logging.Logger] = None,
    ):
        self.cfg = cfg
        if clients is not None:
            self.clients = dict(clients)
        elif client is not None:
            # Backwards-compatible single-client injection (tests)
            prov = getattr(client, "provider", "cswap")
            self.clients = {prov: client}
        else:
            env = build_subprocess_env()
            self.clients = {}
            for prov in cfg.providers:
                if prov == "cswap":
                    self.clients["cswap"] = CswapClient(env=env, run_cwd=cfg.run_cwd)
                elif prov == "aswap":
                    self.clients["aswap"] = AswapClient(env=env, run_cwd=cfg.run_cwd)

        self.clock = clock
        self.log = logger or logging.getLogger("cswap_window_keeper")
        self.state = load_state(cfg.state_file)
        self.consecutive_errors: Dict[str, int] = {}
        self.provider_next_wake: Dict[str, float] = {}

    def tick_provider(self, provider: str) -> float:
        """Evaluate all accounts/pools for one provider; return its next wake epoch."""
        client = self.clients[provider]
        now = self.clock()
        providers_state: Dict[str, Any] = self.state.setdefault("providers", {})
        pstate: Dict[str, Any] = providers_state.setdefault(provider, {})

        try:
            payload = client.list_accounts()
            self.consecutive_errors[provider] = 0
        except Exception as exc:
            err_n = self.consecutive_errors.get(provider, 0) + 1
            self.consecutive_errors[provider] = err_n
            backoff = min(600.0, 60.0 * (2 ** min(err_n - 1, 4)))
            self.log.error(
                "[%s] Failed to query account usage: %s (retrying in %s)",
                provider,
                exc,
                fmt_duration(backoff),
            )
            pstate["last_error"] = str(exc)
            pstate["last_check_at"] = now
            pstate["last_check_at_iso"] = fmt_iso_utc(now)
            pstate["next_wake_at"] = now + backoff
            pstate["next_wake_at_iso"] = fmt_iso_utc(now + backoff)
            return now + backoff

        cache_ttl_s = float(getattr(client, "cache_ttl_s", CSWAP_CACHE_TTL_S))
        verify_delay_s = float(getattr(client, "verify_delay_s", CSWAP_CACHE_TTL_S + CACHE_BUFFER_S))
        targets = extract_targets(provider, payload, self.cfg, cache_ttl_s, verify_delay_s)

        targets_state: Dict[str, Any] = self.state.setdefault("targets", {})
        next_wake: Optional[float] = None
        seen_keys = set()

        for target in targets:
            seen_keys.add(target.key)
            st = targets_state.setdefault(target.key, {})
            st["provider"] = provider
            st["slot"] = target.slot
            st["email"] = target.email
            st["pool"] = target.pool
            st["pool_name"] = target.pool_name
            st["active"] = target.active
            st["usage_status"] = target.usage_status
            st["last_seen_at"] = now

            decision = evaluate_target(target, st, now, self.cfg)
            label = format_target_label(decision)
            st["last_action"] = decision.action
            st["last_reason"] = decision.reason

            if decision.action == "wait_reset" and decision.resets_at is not None:
                prev = st.get("last_resets_at")
                if prev != decision.resets_at:
                    if st.get("pending_verify"):
                        self.log.info(
                            "%s Confirmed new 5h window: resets at %s (next check in %s)",
                            label,
                            fmt_local(decision.resets_at),
                            fmt_duration((decision.next_check_at or now) - now),
                        )
                    else:
                        self.log.info(
                            "%s Tracking 5h window: resets at %s (next check at %s, in %s)",
                            label,
                            fmt_local(decision.resets_at),
                            fmt_local(decision.next_check_at),
                            fmt_duration((decision.next_check_at or now) - now),
                        )
                st["last_resets_at"] = decision.resets_at
                st["last_resets_at_iso"] = fmt_iso_utc(decision.resets_at)
                st["pending_verify"] = False
                st["next_check_at"] = decision.next_check_at

            elif decision.action == "ping":
                model = self.cfg.model_for(provider, decision.pool)
                if self.cfg.dry_run:
                    self.log.info(
                        "%s [DRY-RUN] Would ping via `%s run %d` model=%s (%s)",
                        label,
                        provider,
                        decision.slot,
                        model,
                        decision.reason,
                    )
                    decision.next_check_at = now + self.cfg.retry_error_s
                    st["next_check_at"] = decision.next_check_at
                else:
                    self.log.info(
                        "%s 5h window is idle -> pinging via `%s run %d` with %r (model=%s)",
                        label,
                        provider,
                        decision.slot,
                        self.cfg.ping_prompt,
                        model,
                    )
                    ping_now = self.clock()
                    result = client.ping_account(
                        decision.slot,
                        prompt=self.cfg.ping_prompt,
                        model=model,
                        pool=decision.pool,
                    )
                    if result.get("ok"):
                        st["last_ping_at"] = ping_now
                        st["last_ping_at_iso"] = fmt_iso_utc(ping_now)
                        st["last_ping_result"] = result
                        st["last_ping_error"] = None
                        st["ping_count"] = int(st.get("ping_count", 0)) + 1
                        st["pending_verify"] = True
                        st["estimated_resets_at"] = ping_now + 5 * 3600
                        verify_at = ping_now + verify_delay_s
                        st["next_check_at"] = verify_at
                        decision.next_check_at = verify_at
                        cost_val = result.get("cost_usd")
                        cost_part = (
                            f" cost=${cost_val:.6f}"
                            if isinstance(cost_val, (int, float))
                            else ""
                        )
                        self.log.info(
                            "%s Ping OK: reply=%r model=%s in=%s out=%s%s duration=%sms -> will verify exact resetsAt at %s",
                            label,
                            result.get("result"),
                            result.get("model"),
                            result.get("input_tokens"),
                            result.get("output_tokens"),
                            cost_part,
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
                            "%s Ping FAILED: %s (retrying at %s)",
                            label,
                            result.get("error"),
                            fmt_local(retry_at),
                        )

            else:
                st["next_check_at"] = decision.next_check_at
                level = logging.WARNING if decision.action == "retry" else logging.INFO
                self.log.log(
                    level,
                    "%s %s: %s%s",
                    label,
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

        # Prune targets for this provider that no longer exist
        for old_key in list(targets_state.keys()):
            if targets_state[old_key].get("provider") == provider and old_key not in seen_keys:
                del targets_state[old_key]

        if next_wake is None:
            next_wake = now + DEFAULT_MAX_SLEEP_S
        next_wake = max(next_wake, self.clock() + 15.0)

        pstate["last_check_at"] = now
        pstate["last_check_at_iso"] = fmt_iso_utc(now)
        pstate["next_wake_at"] = next_wake
        pstate["next_wake_at_iso"] = fmt_iso_utc(next_wake)
        pstate["last_error"] = None
        return next_wake

    def tick(self, *, force_all: bool = False) -> float:
        """Run evaluation for due providers (or all on startup/force_all); return earliest wake."""
        now = self.clock()
        due_providers: List[str] = []
        for prov in self.clients.keys():
            scheduled = self.provider_next_wake.get(prov)
            if force_all or scheduled is None or now >= scheduled - 1.0:
                due_providers.append(prov)

        for prov in due_providers:
            self.provider_next_wake[prov] = self.tick_provider(prov)

        # Prune providers no longer configured
        active_set = set(self.clients.keys())
        providers_state = self.state.setdefault("providers", {})
        for old_p in list(providers_state.keys()):
            if old_p not in active_set:
                del providers_state[old_p]
        targets_state = self.state.setdefault("targets", {})
        for old_k in list(targets_state.keys()):
            if targets_state[old_k].get("provider") not in active_set:
                del targets_state[old_k]

        next_wake = min(self.provider_next_wake.values()) if self.provider_next_wake else (now + DEFAULT_MAX_SLEEP_S)
        next_wake = max(next_wake, self.clock() + 15.0)

        self.state["last_check_at"] = now
        self.state["last_check_at_iso"] = fmt_iso_utc(now)
        self.state["next_wake_at"] = next_wake
        self.state["next_wake_at_iso"] = fmt_iso_utc(next_wake)
        errors = [
            f"{p}: {ps['last_error']}"
            for p, ps in providers_state.items()
            if ps.get("last_error")
        ]
        self.state["last_error"] = "; ".join(errors) if errors else None

        if not self.cfg.dry_run:
            save_state(self.cfg.state_file, self.state)
        return next_wake

    def run_forever(self, stop_event: Optional[threading.Event] = None) -> None:
        stop = stop_event or threading.Event()
        self.log.info(
            "cswap-window-keeper started (pid=%d, providers=%s, grace=%ds, cooldown=%ds, state=%s)",
            os.getpid(),
            ",".join(self.clients.keys()),
            int(self.cfg.reset_grace_s),
            int(self.cfg.ping_cooldown_s),
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

def build_launchd_plist(script_path: Path, cfg: KeeperConfig, providers_spec: str) -> Dict[str, Any]:
    env = build_subprocess_env()
    program_args = [
        "/usr/bin/python3",
        str(script_path.resolve()),
        "--state-dir",
        str(cfg.state_dir),
        "--providers",
        providers_spec,
        "--grace",
        str(int(cfg.reset_grace_s)),
        "--cooldown",
        str(int(cfg.ping_cooldown_s)),
        "--model",
        cfg.ping_model,
        "--aswap-pools",
        ",".join(cfg.aswap_pools),
        "--aswap-gemini-model",
        cfg.aswap_gemini_model,
        "--aswap-3p-model",
        cfg.aswap_3p_model,
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


def cmd_install(cfg: KeeperConfig, providers_spec: str) -> int:
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
    plist_data = build_launchd_plist(run_script, cfg, providers_spec)

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
    print(f"  Providers: {', '.join(cfg.providers)} (spec={providers_spec})")
    print(f"  Binary:    {run_script}")
    print(f"  Plist:     {PLIST_PATH}")
    print(f"  State:     {cfg.state_file}")
    print(f"  Log:       {cfg.log_file}")
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


def systemd_runtime_status() -> Tuple[str, Optional[str], Optional[str]]:
    """Return (scope, active_state, pid) for systemd on Linux."""
    unit = "cswap-window-keeper.service"
    for scope, base_cmd in (
        ("system", ["systemctl"]),
        ("user", ["systemctl", "--user"]),
    ):
        try:
            res = subprocess.run(
                [*base_cmd, "show", unit, "-p", "LoadState", "-p", "ActiveState", "-p", "MainPID"],
                capture_output=True,
                text=True,
                timeout=5,
            )
        except OSError:
            continue
        if res.returncode != 0:
            continue
        vals: Dict[str, str] = {}
        for line in res.stdout.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                vals[k.strip()] = v.strip()
        if vals.get("LoadState") == "loaded":
            pid = vals.get("MainPID")
            return scope, vals.get("ActiveState") or "unknown", (pid if pid and pid != "0" else None)
    return "none", None, None


def cmd_status(cfg: KeeperConfig) -> int:
    if sys.platform == "darwin":
        loaded, pid = service_runtime_status()
        plist_exists = PLIST_PATH.exists()
        print(f"Service: {SERVICE_LABEL}")
        print(f"  Plist:   {PLIST_PATH} ({'installed' if plist_exists else 'not installed'})")
        print(f"  Status:  {'running (pid=' + pid + ')' if loaded and pid else ('loaded (not running)' if loaded else 'stopped')}")
    else:
        scope, active_state, pid = systemd_runtime_status()
        print("Service: cswap-window-keeper.service (systemd)")
        if scope == "none":
            print("  Status:  not installed")
        else:
            pid_part = f", pid={pid}" if pid else ""
            print(f"  Status:  {active_state} ({scope}{pid_part})")
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

    targets = state.get("targets") or {}
    if not targets:
        print("\nNo tracked accounts yet. Run `once` or `install` to initialize.")
        return 0

    providers_state = state.get("providers") or {}
    by_provider: Dict[str, List[Dict[str, Any]]] = {}
    for st in targets.values():
        p = str(st.get("provider") or "cswap")
        by_provider.setdefault(p, []).append(st)

    for prov in sorted(by_provider.keys(), key=lambda x: (0 if x == "cswap" else 1, x)):
        pinfo = providers_state.get(prov) or {}
        pwake = pinfo.get("next_wake_at")
        pwake_str = f" (next check {fmt_local(pwake)}, in {fmt_duration(pwake - now)})" if pwake else ""
        print(f"\n[{prov}]{pwake_str}")
        rows = sorted(
            by_provider[prov],
            key=lambda x: (int(x.get("slot") or 999), str(x.get("pool") or "")),
        )
        for st in rows:
            slot = st.get("slot", "?")
            email = st.get("email", "?")
            pool = st.get("pool")
            pool_tag = f" [{pool}]" if pool else ""
            active_tag = " [active]" if st.get("active") else ""
            action = st.get("last_action", "-")
            resets_at = st.get("last_resets_at")
            next_chk = st.get("next_check_at")
            ping_count = st.get("ping_count", 0)
            last_ping = st.get("last_ping_at")
            print(f"  Account-{slot} ({email}){pool_tag}{active_tag}")
            print(f"    Action:     {action} — {st.get('last_reason', '')}")
            if resets_at:
                print(f"    5h Resets:  {fmt_local(resets_at)} ({fmt_duration(resets_at - now)})")
            if next_chk:
                print(f"    Next Check: {fmt_local(next_chk)} ({fmt_duration(next_chk - now)})")
            if last_ping:
                res = st.get("last_ping_result") or {}
                cost = res.get("cost_usd")
                model_used = res.get("model")
                extras = [f"total pings={ping_count}"]
                if model_used:
                    extras.append(f"model={model_used}")
                if isinstance(cost, (int, float)):
                    extras.append(f"cost=${cost:.6f}")
                print(f"    Last Ping:  {fmt_local(last_ping)} ({', '.join(extras)})")
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
        description="Smart background daemon that keeps 5-hour usage windows ticking via cswap and aswap.",
    )
    p.add_argument(
        "--state-dir",
        type=Path,
        default=DEFAULT_STATE_DIR,
        help=f"Directory for state.json and logs (default: {DEFAULT_STATE_DIR})",
    )
    p.add_argument(
        "--providers",
        default="auto",
        help="Providers to monitor: 'auto' (all installed), 'cswap', 'aswap', or 'cswap,aswap' (default: auto)",
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
        help=f"Minimum seconds between pings for the same account/pool (default: {int(DEFAULT_PING_COOLDOWN_S)})",
    )
    p.add_argument(
        "--model",
        default=CSWAP_PING_MODEL,
        help=f"Claude model alias for cswap minimal-token ping (default: {CSWAP_PING_MODEL})",
    )
    p.add_argument(
        "--aswap-pools",
        default=",".join(ASWAP_DEFAULT_POOLS),
        help=f"Comma-separated aswap pools to monitor (default: {','.join(ASWAP_DEFAULT_POOLS)})",
    )
    p.add_argument(
        "--aswap-gemini-model",
        default=ASWAP_GEMINI_MODEL,
        help=f"agy model ID for pinging the gemini pool (default: {ASWAP_GEMINI_MODEL})",
    )
    p.add_argument(
        "--aswap-3p-model",
        default=ASWAP_3P_MODEL,
        help=f"agy model ID for pinging the 3p (Claude/GPT) pool (default: {ASWAP_3P_MODEL})",
    )
    p.add_argument(
        "--prompt",
        default=PING_PROMPT,
        help=f"Prompt text sent when waking an idle window (default: {PING_PROMPT!r})",
    )
    p.add_argument(
        "--accounts",
        default=None,
        help="Comma-separated accounts to monitor (e.g. '1,2' or 'cswap:1,aswap:1')",
    )
    p.add_argument("-v", "--verbose", action="store_true", help="Verbose logging")

    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("run", help="Run the keeper daemon in the foreground")

    p_once = sub.add_parser("once", help="Run a single check pass across configured providers and exit")
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

    env = build_subprocess_env()
    try:
        providers = resolve_providers(args.providers, env=env)
    except ValueError as exc:
        parser.error(str(exc))

    acct_filter = None
    if args.accounts:
        acct_filter = [x.strip() for x in args.accounts.split(",") if x.strip()]

    aswap_pools = [x.strip().lower() for x in args.aswap_pools.split(",") if x.strip()]

    cfg = KeeperConfig(
        state_dir=args.state_dir.expanduser(),
        providers=providers,
        reset_grace_s=args.grace,
        ping_cooldown_s=args.cooldown,
        ping_model=args.model,
        aswap_pools=aswap_pools or list(ASWAP_DEFAULT_POOLS),
        aswap_gemini_model=args.aswap_gemini_model,
        aswap_3p_model=args.aswap_3p_model,
        ping_prompt=args.prompt,
        account_filter=acct_filter,
        dry_run=bool(getattr(args, "dry_run", False)),
    )

    if args.command == "status":
        return cmd_status(cfg)
    if args.command == "install":
        return cmd_install(cfg, providers_spec=args.providers)
    if args.command == "uninstall":
        return cmd_uninstall()
    if args.command == "logs":
        return cmd_logs(cfg, args.lines, args.follow)

    logger = setup_logging(cfg.log_file, also_stderr=True, verbose=args.verbose)

    if args.command == "once":
        keeper = WindowKeeper(cfg, logger=logger)
        next_wake = keeper.tick(force_all=True)
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
