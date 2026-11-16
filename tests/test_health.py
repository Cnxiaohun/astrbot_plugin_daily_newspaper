"""Read-only health tests: fake bots only, no live endpoints or file writes."""
from __future__ import annotations

import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from core.health import platform_health


class FakeBot:
    def __init__(self, responses=None):
        self.calls = []
        self.responses = responses or {}

    async def call_action(self, action, **params):
        if action not in {"get_status", "get_version_info", "get_login_info", "get_group_info"}:
            raise AssertionError("non-read-only action")
        self.calls.append((action, params))
        default = {
            "get_status": {"online": True, "good": True, "token": "never-expose"},
            "get_version_info": {"app_name": "NapCat", "app_version": "4.8.0", "protocol_version": "v11", "token": "never-expose"},
            "get_login_info": {"user_id": 12345, "nickname": "private-nickname"},
            "get_group_info": {"group_id": params.get("group_id"), "group_name": "private-group-name", "member_count": 25},
        }
        result = self.responses.get(action, default[action])
        if isinstance(result, BaseException):
            raise result
        return result


def context_for(bot, adapter="aiocqhttp"):
    platform = SimpleNamespace(bot=bot, meta=lambda: SimpleNamespace(name=adapter))
    looked_up = []
    def get_platform_inst(platform_id):
        looked_up.append(platform_id)
        return platform if platform_id == "bot" else None
    return SimpleNamespace(get_platform_inst=get_platform_inst), looked_up


class HealthTests(unittest.IsolatedAsyncioTestCase):
    async def test_allowlisted_snapshot_deduplicates_current_targets(self):
        bot = FakeBot()
        context, looked_up = context_for(bot)
        result = await platform_health(context, ["bot:GroupMessage:123", "bot:GroupMessage:999_123", "bot:GroupMessage:456"])
        self.assertEqual(looked_up, ["bot"])
        self.assertEqual(bot.calls, [
            ("get_status", {}), ("get_version_info", {}), ("get_login_info", {}),
            ("get_group_info", {"group_id": 123}), ("get_group_info", {"group_id": 456}),
        ])
        platform = result["platforms"][0]
        self.assertEqual(platform["status"], {"online": True, "good": True})
        self.assertEqual(platform["login"], {"user_id": "12345"})
        self.assertEqual(platform["groups"], [{"group_id": "123", "query_ok": True}, {"group_id": "456", "query_ok": True}])
        serialized = json.dumps(result)
        for private in ("never-expose", "private-nickname", "private-group-name", "member_count"):
            self.assertNotIn(private, serialized)

    async def test_invalid_targets_never_reach_platform_or_bot(self):
        bot = FakeBot()
        context, looked_up = context_for(bot)
        result = await platform_health(context, ["bot:GroupMessage:notnumeric", "bot:GroupMessage:0", "bot:FriendMessage:123", "broken", "bot:GroupMessage:１２３", "bot:GroupMessage:123:extra"])
        self.assertEqual(result["invalid_targets"], 6)
        self.assertEqual(result["platforms"], [])
        self.assertEqual(looked_up, [])
        self.assertEqual(bot.calls, [])

    async def test_missing_and_unsupported_platform_make_no_queries(self):
        bot = FakeBot()
        context, _ = context_for(bot, adapter="qq_official")
        result = await platform_health(context, ["missing:GroupMessage:123", "bot:GroupMessage:456"])
        self.assertFalse(result["platforms"][0]["matched"])
        self.assertIn("UnsupportedPlatform", result["platforms"][1]["error"])
        self.assertEqual(bot.calls, [])

    async def test_errors_never_echo_remote_bodies_or_stop_other_queries(self):
        class ActionFailed(Exception):
            pass
        bot = FakeBot({"get_status": ActionFailed("token=private-token https://secret.invalid/raw"), "get_login_info": ["invalid"]})
        context, _ = context_for(bot)
        result = await platform_health(context, ["bot:GroupMessage:123"])
        platform = result["platforms"][0]
        self.assertIn("ActionFailed", platform["status"]["error"])
        self.assertIn("InvalidResponse", platform["login"]["error"])
        self.assertTrue(platform["groups"][0]["query_ok"])
        self.assertNotIn("private-token", json.dumps(result))
        self.assertNotIn("secret.invalid", json.dumps(result))
        self.assertEqual(len(bot.calls), 4)

    async def test_scalar_allowlists_redact_known_keys_and_urls(self):
        secret = "private-provider-key" * 15
        bot = FakeBot({
            "get_status": {"online": False, "good": 1, "raw": "private"},
            "get_version_info": {"app_name": secret, "app_version": "https://example.invalid/?token=secret", "protocol_version": {"private": "body"}},
            "get_login_info": {"user_id": True},
            "get_group_info": {"group_id": 999, "group_name": "private"},
        })
        context, _ = context_for(bot)
        result = await platform_health(context, ["bot:GroupMessage:123"], config={"api": {"api_key": secret}})
        platform = result["platforms"][0]
        self.assertEqual(platform["status"], {"online": False})
        self.assertEqual(platform["version"], {"app_name": "[REDACTED]", "app_version": "[URL]"})
        self.assertIn("error", platform["login"])
        self.assertFalse(platform["groups"][0]["query_ok"])
        self.assertIn("mute", result["note"])
        self.assertNotIn("private-provider", json.dumps(result))

    async def test_each_query_times_out_without_sending(self):
        class SlowBot(FakeBot):
            async def call_action(self, action, **params):
                self.calls.append((action, params))
                await asyncio.Future()
        bot = SlowBot()
        context, _ = context_for(bot)
        with patch("core.health._CALL_TIMEOUT_SECONDS", 0.001):
            result = await platform_health(context, ["bot:GroupMessage:123"])
        platform = result["platforms"][0]
        self.assertIn("TimeoutError", platform["status"]["error"])
        self.assertIn("TimeoutError", platform["version"]["error"])
        self.assertIn("TimeoutError", platform["login"]["error"])
        self.assertIn("TimeoutError", platform["groups"][0]["error"])
        self.assertEqual(len(bot.calls), 4)

    async def test_cancellation_propagates(self):
        bot = FakeBot({"get_status": asyncio.CancelledError()})
        context, _ = context_for(bot)
        with self.assertRaises(asyncio.CancelledError):
            await platform_health(context, ["bot:GroupMessage:123"])
        self.assertEqual(len(bot.calls), 1)


if __name__ == "__main__":
    unittest.main()
