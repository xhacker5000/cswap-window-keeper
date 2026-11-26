#!/usr/bin/env python3
"""Unit tests for cswap_window_keeper."""

from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path
import tempfile
import unittest
from typing import Any, Dict, List

import cswap_window_keeper as keeper_mod


def iso_utc(epoch: float) -> str:
    return (
        dt.datetime.fromtimestamp(epoch, tz=dt.timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


class FakeCswapClient:
    def __init__(self, payloads: List[Dict[str, Any]], ping_results: List[Dict[str, Any]] | None = None):
        self.payloads = list(payloads)
        self.ping_results = list(ping_results or [])
        self.pings: List[tuple[int, str, str]] = []
        self.list_calls = 0

    def list_accounts(self) -> Dict[str, Any]:
        self.list_calls += 1
        if len(self.payloads) > 1:
            return self.payloads.pop(0)
        return self.payloads[0]

    def ping_account(self, slot_number: int, prompt: str = "say test", model: str = "haiku") -> Dict[str, Any]:
        self.pings.append((slot_number, prompt, model))
        if self.ping_results:
            return self.ping_results.pop(0)
        return {
            "ok": True,
            "returncode": 0,
            "result": "test",
            "model": "claude-haiku-5-5",
            "input_tokens": 2,
            "output_tokens": 3,
            "cache_creation_tokens": 553,
            "cache_read_tokens": 0,
            "cost_usd": 0.0001123,
            "duration_ms": 1500,
        }


class TestWindowKeeper(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.state_dir = Path(self.tmpdir.name)
        self.cfg = keeper_mod.KeeperConfig(state_dir=self.state_dir, reset_grace_s=60.0)
        self.logger = logging.getLogger("test_keeper")
        self.logger.handlers.clear()
        self.logger.addHandler(logging.NullHandler())

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_parse_claude_json_output_with_preamble(self) -> None:
        stdout = (
            "Launching Account-2 (user2@example.com) [session mode]\n"
            '{"type":"result","is_error":false,"result":"test","total_cost_usd":0.0001}\n'
        )
        parsed = keeper_mod.parse_claude_json_output(stdout)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["result"], "test")
        self.assertFalse(parsed["is_error"])

    def test_active_window_schedules_at_reset_plus_grace(self) -> None:
        now = 1_800_000_000.0
        reset_at = now + 2 * 3600 + 10 * 60  # 2h 10m from now
        acct = {
            "number": 1,
            "email": "a@example.com",
            "organizationUuid": "org-1",
            "active": True,
            "usageStatus": "ok",
            "usage": {"fiveHour": {"pct": 9.0, "resetsAt": iso_utc(reset_at)}},
            "usageFetchedAt": iso_utc(now),
        }
        decision = keeper_mod.evaluate_account(acct, {}, now, self.cfg)
        self.assertEqual(decision.action, "wait_reset")
        self.assertAlmostEqual(decision.next_check_at, reset_at + 60.0, places=1)

    def test_idle_window_triggers_ping_then_waits_for_cache_ttl(self) -> None:
        now = 1_800_000_000.0
        new_reset = now + 5 * 3600

        # Tick 1: Account 2 is idle (no resetsAt, pct=0) -> pings!
        payload_idle = {
            "accounts": [
                {
                    "number": 2,
                    "email": "b@example.com",
                    "organizationUuid": "org-2",
                    "active": False,
                    "usageStatus": "ok",
                    "usage": {"fiveHour": {"pct": 0.0}, "sevenDay": {"pct": 40.0}},
                    "usageFetchedAt": iso_utc(now),
                }
            ]
        }
        # Tick 2 (60s later): cswap still serves cached pre-ping data -> must NOT double-ping!
        payload_still_cached = {
            "accounts": [
                {
                    "number": 2,
                    "email": "b@example.com",
                    "organizationUuid": "org-2",
                    "active": False,
                    "usageStatus": "ok",
                    "usage": {"fiveHour": {"pct": 0.0}, "sevenDay": {"pct": 40.0}},
                    "usageFetchedAt": iso_utc(now),  # pre-ping timestamp!
                }
            ]
        }
        # Tick 3 (195s later): fresh fetch shows new 5h resetsAt -> tracks it & sleeps ~5h!
        payload_confirmed = {
            "accounts": [
                {
                    "number": 2,
                    "email": "b@example.com",
                    "organizationUuid": "org-2",
                    "active": False,
                    "usageStatus": "ok",
                    "usage": {
                        "fiveHour": {"pct": 1.0, "resetsAt": iso_utc(new_reset)},
                        "sevenDay": {"pct": 40.0},
                    },
                    "usageFetchedAt": iso_utc(now + 195.0),
                }
            ]
        }

        cur_time = [now]
        client = FakeCswapClient([payload_idle, payload_still_cached, payload_confirmed])
        keeper = keeper_mod.WindowKeeper(
            self.cfg, client=client, clock=lambda: cur_time[0], logger=self.logger
        )

        # Tick 1 -> should ping once
        wake1 = keeper.tick()
        self.assertEqual(len(client.pings), 1)
        self.assertEqual(client.pings[0], (2, "say test", "haiku"))
        self.assertAlmostEqual(wake1, now + keeper_mod.CSWAP_CACHE_TTL_S + keeper_mod.CACHE_BUFFER_S, delta=2.0)

        # Tick 2 (early check at +60s) -> must NOT ping again
        cur_time[0] = now + 60.0
        wake2 = keeper.tick()
        self.assertEqual(len(client.pings), 1)
        self.assertGreater(wake2, cur_time[0])

        # Tick 3 (+195s) -> confirms new resetsAt and schedules reset + 60s
        cur_time[0] = now + 195.0
        wake3 = keeper.tick()
        self.assertEqual(len(client.pings), 1)
        self.assertAlmostEqual(wake3, new_reset + 60.0, delta=2.0)

    def test_stale_pre_reset_cache_does_not_ping_prematurely(self) -> None:
        now = 1_800_000_000.0
        past_reset = now - 60.0        # Reset happened 1 min ago
        fetched_before = now - 90.0    # Cached 30s before reset
        acct = {
            "number": 1,
            "email": "a@example.com",
            "usageStatus": "ok",
            "usage": {"fiveHour": {"pct": 15.0, "resetsAt": iso_utc(past_reset)}},
            "usageFetchedAt": iso_utc(fetched_before),
        }
        decision = keeper_mod.evaluate_account(acct, {}, now, self.cfg)
        self.assertEqual(decision.action, "wait_cache")
        self.assertAlmostEqual(
            decision.next_check_at,
            fetched_before + keeper_mod.CSWAP_CACHE_TTL_S + keeper_mod.CACHE_BUFFER_S,
            delta=2.0,
        )

    def test_exhausted_seven_day_skips_ping(self) -> None:
        now = 1_800_000_000.0
        weekly_reset = now + 86400.0
        acct = {
            "number": 1,
            "email": "a@example.com",
            "usageStatus": "ok",
            "usage": {
                "fiveHour": {"pct": 0.0},
                "sevenDay": {"pct": 100.0, "resetsAt": iso_utc(weekly_reset)},
            },
            "usageFetchedAt": iso_utc(now),
        }
        decision = keeper_mod.evaluate_account(acct, {}, now, self.cfg)
        self.assertEqual(decision.action, "wait_7d")
        self.assertAlmostEqual(decision.next_check_at, weekly_reset + 60.0, delta=2.0)

    def test_unavailable_respects_usage_retry_at(self) -> None:
        now = 1_800_000_000.0
        retry_at = now + 900.0
        acct = {
            "number": 1,
            "email": "a@example.com",
            "usageStatus": "unavailable",
            "usageError": "http-429",
            "usageRetryAt": iso_utc(retry_at),
        }
        decision = keeper_mod.evaluate_account(acct, {}, now, self.cfg)
        self.assertEqual(decision.action, "retry")
        self.assertAlmostEqual(decision.next_check_at, retry_at + 15.0, delta=2.0)

    def test_disabled_and_api_key_skipped(self) -> None:
        now = 1_800_000_000.0
        d1 = keeper_mod.evaluate_account(
            {"number": 1, "email": "a@example.com", "disabled": True, "usageStatus": "ok"},
            {}, now, self.cfg,
        )
        d2 = keeper_mod.evaluate_account(
            {"number": 2, "email": "b@example.com", "usageStatus": "api_key"},
            {}, now, self.cfg,
        )
        self.assertEqual(d1.action, "skip")
        self.assertEqual(d2.action, "skip")


if __name__ == "__main__":
    unittest.main()
