"""AstrBot API contract tests using explicit SDK stubs; NOT live NapCat acceptance."""
from __future__ import annotations

import asyncio
import importlib.util
import re
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from test_service import default_config, slot_entry


class _StubLogger:
    """替身日志器：插件只调用 info/warning/error 等方法，测试不引入内置 logging。"""

    def debug(self, *args, **kwargs) -> None:
        pass

    def info(self, *args, **kwargs) -> None:
        pass

    def warning(self, *args, **kwargs) -> None:
        pass

    def error(self, *args, **kwargs) -> None:
        pass

    def exception(self, *args, **kwargs) -> None:
        pass


def load_entry():
    class Star:
        def __init__(self, context):
            self.context = context
    class Chain:
        def __init__(self, chain=None):
            self.chain = chain or []
        def message(self, text):
            self.chain.append(text)
            return self
    class Image:
        @staticmethod
        def fromFileSystem(path):
            return ("image", path)
    class Filter:
        PermissionType = types.SimpleNamespace(ADMIN="admin")
        EventMessageType = types.SimpleNamespace(ALL="all")
        @staticmethod
        def permission_type(permission):
            def wrap(func):
                func.test_permission = permission
                return func
            return wrap
        @staticmethod
        def command(name, **kwargs):
            def wrap(func):
                func.test_command = name
                func.test_priority = kwargs.get("priority", 0)
                return func
            return wrap
        @staticmethod
        def regex(pattern, **kwargs):
            def wrap(func):
                func.test_regex = pattern
                func.test_priority = kwargs.get("priority", 0)
                return func
            return wrap
    modules = {}
    for name in ("astrbot", "astrbot.api", "astrbot.api.event", "astrbot.api.star", "astrbot.api.message_components", "daily_plugin_test"):
        modules[name] = types.ModuleType(name)
    modules["daily_plugin_test"].__path__ = [str(Path(__file__).parents[1])]
    # main.py 里 `from . import __version__`，桩包需要这个属性。
    _init = Path(__file__).parents[1] / "__init__.py"
    modules["daily_plugin_test"].__version__ = re.search(
        r'__version__\s*=\s*"([\d.]+)"', _init.read_text(encoding="utf-8")
    ).group(1)
    modules["astrbot.api"].AstrBotConfig = dict
    modules["astrbot.api"].logger = _StubLogger()
    eventmod = modules["astrbot.api.event"]
    eventmod.AstrMessageEvent = object
    eventmod.filter = Filter
    eventmod.MessageChain = Chain
    starmod = modules["astrbot.api.star"]
    starmod.Star = Star
    starmod.Context = object
    starmod.StarTools = object
    modules["astrbot.api.message_components"].Image = Image
    spec = importlib.util.spec_from_file_location("daily_plugin_test.main", Path(__file__).parents[1] / "main.py")
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, modules):
        spec.loader.exec_module(module)
    return module, modules


ENTRY, SDK = load_entry()


class Event:
    def __init__(self, message):
        self.message_str = message
        self.unified_msg_origin = "bot-instance:GroupMessage:789_123"
        self.stopped = False
    def stop_event(self):
        self.stopped = True
    def get_platform_id(self):
        return "bot-instance"
    def get_platform_name(self):
        return "aiocqhttp"
    def get_group_id(self):
        return "123"
    def is_admin(self):
        return True
    def plain_result(self, text):
        return text


def fake_service(run):
    tasks = []
    def spawn(coro, name):
        task = asyncio.create_task(coro)
        tasks.append(task)
        return task
    return types.SimpleNamespace(run=run, spawn=spawn), tasks


class Config(dict):
    """Minimal AstrBotConfig stand-in: records that a save was requested."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.saved = False

    def save_config(self):
        self.saved = True


class MainTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.cfg = Config(default_config())
        self.plugin = ENTRY.DailyImageReport(None, self.cfg)
        self.plugin.service = object()

    async def test_all_command_handlers_have_admin_decorator(self):
        for name in ("report", "bind", "unbind", "targets"):
            self.assertEqual(getattr(getattr(self.plugin, name), "test_permission"), "admin")
            self.assertEqual(getattr(getattr(self.plugin, name), "test_priority"), sys.maxsize)

    async def test_slash_fallback_handles_command_when_wake_prefix_empty(self):
        calls = []
        async def run(slot, **kwargs):
            calls.append((slot, kwargs))
            return {"status": "success"}
        self.plugin.service, tasks = fake_service(run)
        event = Event("/日报 测试 早报")
        messages = [r async for r in self.plugin.slash_fallback(event)]
        await asyncio.gather(*tasks)
        self.assertIn("已开始生成", messages[0])
        self.assertEqual(calls[0][0], "morning")
        self.assertEqual(calls[0][1]["preview_target"], event.unified_msg_origin)

    async def test_both_orders_and_shorthand_parse_identically(self):
        results = []
        for command in ("/日报 测试 早报", "/日报 早报 测试", "/日报测试 早报", "/日报 test morning"):
            calls = []
            async def run(slot, **kwargs):
                calls.append((slot, kwargs))
                return {"status": "success"}
            self.plugin.service, tasks = fake_service(run)
            messages = [r async for r in self.plugin.report(Event(command))]
            await asyncio.gather(*tasks)
            results.append((command, calls[0][0], calls[0][1]["preview_target"] != ""))
            self.assertIn("已开始生成", messages[0])
        for command, slot, is_preview in results:
            self.assertEqual(slot, "morning", command)
            self.assertTrue(is_preview, command)

    async def test_bare_slot_means_publish_now(self):
        slot_entry(self.cfg, "morning")["targets"] = ["bot-instance:GroupMessage:123"]
        calls = []
        async def run(slot, **kwargs):
            calls.append((slot, kwargs))
            return {"status": "success"}
        self.plugin.service, tasks = fake_service(run)
        _ = [r async for r in self.plugin.report(Event("/日报 早报"))]
        await asyncio.gather(*tasks)
        self.assertEqual(calls[0][0], "morning")
        self.assertEqual(calls[0][1]["preview_target"], "")
        self.assertFalse(calls[0][1]["force"])

    async def test_bind_via_unified_action(self):
        event = Event("/日报 绑定 全部")
        messages = [r async for r in self.plugin.report(event)]
        self.assertIn("已绑定", messages[0])
        for slot in self.cfg["schedules"]:
            self.assertIn("bot-instance:GroupMessage:123", slot["targets"])

    async def test_unbind_via_unified_action(self):
        slot_entry(self.cfg, "morning")["targets"] += ["bot-instance:GroupMessage:123"]
        messages = [r async for r in self.plugin.report(Event("/日报 解绑 早报"))]
        self.assertIn("已解绑", messages[0])
        self.assertNotIn("bot-instance:GroupMessage:123", slot_entry(self.cfg, "morning")["targets"])

    async def test_typo_is_rejected_not_silently_published(self):
        calls = []
        async def run(slot, **kwargs):
            calls.append(slot)
            return {"status": "success"}
        self.plugin.service, tasks = fake_service(run)
        messages = [r async for r in self.plugin.report(Event("/日报 强制重做 早报"))]
        await asyncio.gather(*tasks)
        self.assertEqual(calls, [], "拼错的动作绝不能变成静默发布")
        self.assertIn("无法识别的参数", messages[0])
        self.assertIn("强制", messages[0], "应给出最接近的正确动作")

    async def test_unknown_slot_lists_valid_options(self):
        self.plugin.service = types.SimpleNamespace()
        messages = [r async for r in self.plugin.report(Event("/日报 测试 半夜报"))]
        self.assertIn("未知时段", messages[0])
        self.assertIn("早报", messages[0])

    async def test_missing_slot_asks_for_one(self):
        self.plugin.service = types.SimpleNamespace()
        messages = [r async for r in self.plugin.report(Event("/日报 测试"))]
        self.assertIn("请指定时段", messages[0])

    async def test_binding_uses_real_group_not_isolated_umo(self):
        event = Event("/日报绑定 全部")
        responses = [r async for r in self.plugin.bind(event)]
        self.assertTrue(event.stopped)
        self.assertTrue(self.cfg.saved)
        for slot in self.cfg["schedules"]:
            self.assertIn("bot-instance:GroupMessage:123", slot["targets"])
            self.assertNotIn(event.unified_msg_origin, slot["targets"])
        self.assertIn("已绑定", responses[0])

    async def test_unbind_removes_only_current_group(self):
        slot_entry(self.cfg, "morning")["targets"] += ["bot-instance:GroupMessage:123"]
        event = Event("/日报解绑 早报")
        responses = [r async for r in self.plugin.unbind(event)]
        self.assertNotIn("bot-instance:GroupMessage:123", slot_entry(self.cfg, "morning")["targets"])
        self.assertIn("napcat-instance:GroupMessage:123456", slot_entry(self.cfg, "morning")["targets"])
        self.assertIn("已解绑", responses[0])

    async def test_preview_success_emits_no_extra_text(self):
        calls = []
        async def run(slot, **kwargs):
            calls.append((slot, kwargs))
            return {"status": "success"}
        self.plugin.service, tasks = fake_service(run)
        event = Event("/日报 测试 早报")
        messages = [r async for r in self.plugin.report(event)]
        await asyncio.gather(*tasks)
        self.assertIn("已开始生成", messages[0])
        self.assertEqual(calls[0][1]["preview_target"], event.unified_msg_origin)
        self.assertFalse(calls[0][1]["force"])
        self.assertTrue(event.stopped)

    async def test_force_is_explicit(self):
        calls = []
        async def run(slot, **kwargs):
            calls.append(kwargs)
            return {"status": "success"}
        slot_entry(self.cfg, "noon")["targets"] = ["bot-instance:GroupMessage:123"]
        self.plugin.service, tasks = fake_service(run)
        _ = [r async for r in self.plugin.report(Event("/日报 强制 午报"))]
        await asyncio.gather(*tasks)
        self.assertTrue(calls[0]["force"])
        self.assertEqual(calls[0]["preview_target"], "")

    async def test_delivery_chain_contains_only_image(self):
        from core.delivery import send_image
        captured = []
        class Context:
            async def send_message(self, umo, chain):
                captured.append(chain.chain)
                return True
        with patch.dict(sys.modules, SDK):
            self.assertTrue(await send_image(Context(), "bot:GroupMessage:123", Path(__file__)))
        self.assertEqual(len(captured[0]), 1)
        self.assertEqual(captured[0][0][0], "image")

    async def test_terminate_waits_for_active_command(self):
        started = asyncio.Event()
        async def slow():
            started.set()
            await asyncio.Future()
        command = asyncio.create_task(slow())
        await started.wait()
        self.plugin._active_commands.add(command)
        class Service:
            closed = False
            async def close(self):
                self.closed = True
        service = Service()
        self.plugin.service = service
        await self.plugin.terminate()
        self.assertTrue(command.cancelled())
        self.assertTrue(service.closed)
        self.assertIsNone(self.plugin.service)


if __name__ == "__main__":
    unittest.main()
