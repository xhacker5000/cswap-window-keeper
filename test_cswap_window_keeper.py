#!/usr/bin/env python3
"""Unit tests for cswap_window_keeper (cswap + aswap)."""

from __future__ import annotations

import datetime as dt
import json
import logging
from pathlib import Path
import tempfile
import unittest
from typing import Any, Dict, List, Optional

import cswap_window_keeper as keeper_mod


def iso_utc(epoch: float) -> str:
    return (
        dt.datetime.fromtimestamp(epoch, tz=dt.timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


class FakeProviderClient:
    def __init__(
        self,
        provider: str,
        payloads: List[Dict[str, Any]],
        ping_results: Optional[List[Dict[str, Any]]] = None,
        cache_ttl_s: Optional[float] = None,
        verify_delay_s: Optional[float] = None,
    ):
        self.provider = provider
        self.payloads = list(payloads)
        self.ping_results = list(ping_results or [])
        self.pings: List[tuple[int, str, str, Optional[str]]] = []
        self.list_calls = 0
        self.cache_ttl_s = (
            keeper_mod.CSWAP_CACHE_TTL_S if cache_ttl_s is None and provider == "cswap"
            else (0.0 if cache_ttl_s is None else cache_ttl_s)
        )
        self.verify_delay_s = (
            (keeper_mod.CSWAP_CACHE_TTL_S + keeper_mod.CACHE_BUFFER_S)
            if verify_delay_s is None and provider == "cswap"
            else (keeper_mod.ASWAP_VERIFY_DELAY_S if verify_delay_s is None else verify_delay_s)
        )

    def list_accounts(self) -> Dict[str, Any]:
        self.list_calls += 1
        if len(self.payloads) > 1:
            return self.payloads.pop(0)
        return self.payloads[0]

    def ping_account(
        self,
        slot_number: int,
        prompt: str = "say test",
        model: str = "haiku",
        pool: Optional[str] = None,
    ) -> Dict[str, Any]:
        self.pings.append((slot_number, prompt, model, pool))
        if self.ping_results:
            return self.ping_results.pop(0)
        return {
            "ok": True,
            "returncode": 0,
            "result": "test",
            "model": model,
            "input_tokens": 2,
            "output_tokens": 3,
            "cache_creation_tokens": 0,
            "cache_read_tokens": 0,
            "cost_usd": 0.000112 if self.provider == "cswap" else None,
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

    def test_parse_claude_and_agy_json_output_with_preamble(self) -> None:
        claude_stdout = (
            "Launching Account-2 (user2@example.com) [session mode]\n"
            '{"type":"result","is_error":false,"result":"test","total_cost_usd":0.0001}\n'
        )
        parsed = keeper_mod.parse_claude_json_output(claude_stdout)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["result"], "test")
        self.assertFalse(parsed["is_error"])

        agy_stdout = (
            "Launching agy #1 (user1@example.com) (keeping global user2@example.com)\n"
            '{"conversation_id":"abc","status":"SUCCESS","response":"test\\n","duration_seconds":2.5}\n'
        )
        parsed_agy = keeper_mod.parse_trailing_json_object(agy_stdout)
        self.assertIsNotNone(parsed_agy)
        self.assertEqual(parsed_agy["status"], "SUCCESS")
        self.assertEqual(parsed_agy["response"], "test\n")

    def test_cswap_active_window_schedules_at_reset_plus_grace(self) -> None:
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

    def test_cswap_idle_window_triggers_ping_then_waits_for_cache_ttl(self) -> None:
        now = 1_800_000_000.0
        new_reset = now + 5 * 3600

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
        client = FakeProviderClient("cswap", [payload_idle, payload_still_cached, payload_confirmed])
        keeper = keeper_mod.WindowKeeper(
            self.cfg, client=client, clock=lambda: cur_time[0], logger=self.logger
        )

        wake1 = keeper.tick()
        self.assertEqual(len(client.pings), 1)
        self.assertEqual(client.pings[0], (2, "say test", "haiku", None))
        self.assertAlmostEqual(wake1, now + keeper_mod.CSWAP_CACHE_TTL_S + keeper_mod.CACHE_BUFFER_S, delta=2.0)

        # Early forced check at +60s must NOT double-ping
        cur_time[0] = now + 60.0
        wake2 = keeper.tick(force_all=True)
        self.assertEqual(len(client.pings), 1)
        self.assertGreater(wake2, cur_time[0])

        # Fresh fetch at +195s confirms new resetsAt and schedules reset + 60s
        cur_time[0] = now + 195.0
        wake3 = keeper.tick()
        self.assertEqual(len(client.pings), 1)
        self.assertAlmostEqual(wake3, new_reset + 60.0, delta=2.0)

    def test_cswap_stale_pre_reset_cache_does_not_ping_prematurely(self) -> None:
        now = 1_800_000_000.0
        past_reset = now - 60.0
        fetched_before = now - 90.0
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

    def test_aswap_multi_pool_and_weekly_only_tier(self) -> None:
        now = 1_800_000_000.0
        gem_reset = now + 4 * 3600
        tp_new_reset = now + 5 * 3600

        payload1 = {
            "provider": "agy",
            "accounts": [
                {
                    "number": 1,
                    "email": "pro@example.com",
                    "active": False,
                    "usageStatus": "ok",
                    "usageFetchedAt": iso_utc(now),
                    "usage": {
                        "groups": [
                            {
                                "name": "Gemini Models",
                                "pool": "gemini",
                                "fiveHour": {"pct": 8.6, "resetsAt": iso_utc(gem_reset)},
                                "sevenDay": {"pct": 11.6, "resetsAt": iso_utc(now + 6 * 86400)},
                            },
                            {
                                "name": "Claude and GPT models",
                                "pool": "3p",
                                "fiveHour": {"pct": 0.0, "resetsAt": None},
                                "sevenDay": {"pct": 0.0, "resetsAt": None},
                            },
                        ]
                    },
                },
                {
                    "number": 2,
                    "email": "free@example.com",
                    "active": True,
                    "usageStatus": "ok",
                    "usageFetchedAt": iso_utc(now),
                    "usage": {
                        "groups": [
                            {
                                "name": "Gemini Models",
                                "pool": "gemini",
                                "fiveHour": None,
                                "sevenDay": {"pct": 0.7, "resetsAt": iso_utc(now + 7 * 86400)},
                            },
                            {
                                "name": "Claude and GPT models",
                                "pool": "3p",
                                "fiveHour": None,
                                "sevenDay": {"pct": 0.0, "resetsAt": iso_utc(now + 7 * 86400)},
                            },
                        ]
                    },
                },
            ],
        }
        payload2 = {
            "provider": "agy",
            "accounts": [
                {
                    "number": 1,
                    "email": "pro@example.com",
                    "active": False,
                    "usageStatus": "ok",
                    "usageFetchedAt": iso_utc(now + 20.0),
                    "usage": {
                        "groups": [
                            {
                                "name": "Gemini Models",
                                "pool": "gemini",
                                "fiveHour": {"pct": 8.6, "resetsAt": iso_utc(gem_reset)},
                                "sevenDay": {"pct": 11.6, "resetsAt": iso_utc(now + 6 * 86400)},
                            },
                            {
                                "name": "Claude and GPT models",
                                "pool": "3p",
                                "fiveHour": {"pct": 0.0, "resetsAt": iso_utc(tp_new_reset)},
                                "sevenDay": {"pct": 0.0, "resetsAt": iso_utc(now + 7 * 86400)},
                            },
                        ]
                    },
                },
                payload1["accounts"][1],
            ],
        }

        cur_time = [now]
        aswap_client = FakeProviderClient("aswap", [payload1, payload2])
        keeper = keeper_mod.WindowKeeper(
            self.cfg, clients={"aswap": aswap_client}, clock=lambda: cur_time[0], logger=self.logger
        )

        # Tick 1: Account 1's 3p pool is idle -> pinged with gpt-oss-120b-medium;
        # Account 1's gemini pool is active; Account 2 is weekly-only -> skipped!
        wake1 = keeper.tick()
        self.assertEqual(len(aswap_client.pings), 1)
        self.assertEqual(aswap_client.pings[0], (1, "say test", keeper_mod.ASWAP_3P_MODEL, "3p"))
        self.assertAlmostEqual(wake1, now + keeper_mod.ASWAP_VERIFY_DELAY_S, delta=2.0)

        # Tick 2 (+20s): verifies new 3p resetsAt; earliest wake is gemini reset + 60s
        cur_time[0] = now + 20.0
        wake2 = keeper.tick()
        self.assertEqual(len(aswap_client.pings), 1)
        self.assertAlmostEqual(wake2, gem_reset + 60.0, delta=2.0)

    def test_independent_provider_scheduling_avoids_unnecessary_probes(self) -> None:
        now = 1_800_000_000.0
        cswap_reset = now + 1800.0  # 30m
        aswap_reset = now + 7200.0  # 2h

        cswap_payload = {
            "accounts": [
                {
                    "number": 1,
                    "email": "c@example.com",
                    "usageStatus": "ok",
                    "usage": {"fiveHour": {"pct": 10.0, "resetsAt": iso_utc(cswap_reset)}},
                    "usageFetchedAt": iso_utc(now),
                }
            ]
        }
        aswap_payload = {
            "provider": "agy",
            "accounts": [
                {
                    "number": 1,
                    "email": "a@example.com",
                    "usageStatus": "ok",
                    "usageFetchedAt": iso_utc(now),
                    "usage": {
                        "groups": [
                            {
                                "name": "Gemini Models",
                                "pool": "gemini",
                                "fiveHour": {"pct": 5.0, "resetsAt": iso_utc(aswap_reset)},
                                "sevenDay": {"pct": 5.0},
                            }
                        ]
                    },
                }
            ],
        }

        cur_time = [now]
        c_client = FakeProviderClient("cswap", [cswap_payload])
        a_client = FakeProviderClient("aswap", [aswap_payload])
        keeper = keeper_mod.WindowKeeper(
            self.cfg,
            clients={"cswap": c_client, "aswap": a_client},
            clock=lambda: cur_time[0],
            logger=self.logger,
        )

        # Startup tick queries both providers once
        wake1 = keeper.tick()
        self.assertEqual(c_client.list_calls, 1)
        self.assertEqual(a_client.list_calls, 1)
        self.assertAlmostEqual(wake1, cswap_reset + 60.0, delta=2.0)

        # Wake up at cswap_reset + 60s -> MUST only query cswap, NOT aswap!
        cur_time[0] = cswap_reset + 60.0
        keeper.tick()
        self.assertEqual(c_client.list_calls, 2)
        self.assertEqual(a_client.list_calls, 1)

    def test_v1_state_migration(self) -> None:
        v1 = {
            "version": 1,
            "last_check_at": 1700000000.0,
            "next_wake_at": 1700003600.0,
            "accounts": {
                "a@example.com|org-1": {
                    "slot": 1,
                    "email": "a@example.com",
                    "ping_count": 2,
                }
            },
        }
        self.cfg.state_file.write_text(json.dumps(v1), encoding="utf-8")
        loaded = keeper_mod.load_state(self.cfg.state_file)
        self.assertEqual(loaded["version"], 2)
        self.assertIn("cswap:a@example.com|org-1", loaded["targets"])
        self.assertEqual(loaded["targets"]["cswap:a@example.com|org-1"]["ping_count"], 2)


if __name__ == "__main__":
    unittest.main()
