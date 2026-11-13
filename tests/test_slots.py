"""Dynamic slot set: add/remove/reschedule, legacy migration, window derivation."""
from __future__ import annotations

import asyncio
import types
import unittest
from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

from test_main import Config, Event, load_entry
from test_service import TARGET, default_config

from core.config import (
    MAX_SLOTS,
    SlotConfig,
    find_slot,
    get_image_prompt,
    get_research_prompt,
    load_slots,
    normalize_schedules,
    previous_edition_time,
    validate_config,
    write_slots,
)
from core.service import research_window
from core.store import _RUN_ID

NOW = datetime(2026, 11, 13, 13, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
ENTRY, _ = load_entry()


def slots_of(cfg):
    return load_slots(cfg)


class MigrationTests(unittest.TestCase):
    def test_legacy_object_becomes_list_without_losing_bindings(self):
        cfg = {"schedules": {
            "morning": {"time": "07:30", "targets": [TARGET], "enabled": False,
                        "research_prompt": "自定义"},
            "noon": {"time": "12:30", "targets": []},
        }}
        self.assertTrue(normalize_schedules(cfg))
        slots = slots_of(cfg)
        self.assertEqual([s.id for s in slots], ["morning", "noon"])
        morning = slots[0]
        self.assertEqual((morning.name, morning.time, morning.enabled), ("早报", "07:30", False))
        self.assertEqual(morning.targets, [TARGET])
        self.assertEqual(morning.research_prompt, "自定义")
        # A deleted legacy edition stays deleted instead of silently reappearing.
        self.assertIsNone(find_slot(slots, "晚报"))

    def test_migration_is_idempotent_and_rejects_corrupt_entries(self):
        cfg = {"schedules": {"morning": {"time": "08:00"}}}
        self.assertTrue(normalize_schedules(cfg))
        self.assertFalse(normalize_schedules(cfg))
        for bad in ({"schedules": {"morning": None}}, {"schedules": []}, {"schedules": "x"}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                slots_of(bad)

    def test_missing_schedules_seeds_the_three_defaults(self):
        self.assertEqual([s.id for s in slots_of({})], ["morning", "noon", "evening"])

    def test_round_trip_preserves_every_field(self):
        slots = slots_of(default_config())
        fresh = {}
        write_slots(fresh, slots)
        self.assertEqual(slots_of(fresh), slots)


class ValidationTests(unittest.TestCase):
    def test_ids_and_names_must_be_unique_and_well_formed(self):
        cases = [
            [{"id": "morning", "name": "早报"}, {"id": "morning", "name": "午报"}],
            [{"id": "morning", "name": "早报"}, {"id": "late", "name": "早报"}],
            [{"id": "夜报", "name": "夜报"}],
            [{"id": "a", "name": "x"}],
            [{"id": "Morning", "name": "x"}],
        ]
        for items in cases:
            with self.subTest(items=items), self.assertRaises(ValueError):
                slots_of({"schedules": items})

    def test_slot_count_is_capped(self):
        items = [{"id": f"slot{i}", "name": f"档{i}", "time": "08:00"}
                 for i in range(MAX_SLOTS + 1)]
        with self.assertRaises(ValueError):
            slots_of({"schedules": items})

    def test_static_requires_own_prompt_even_when_disabled(self):
        with self.assertRaises(ValueError):
            validate_config({"schedules": [{"id": "late", "name": "夜报", "mode": "static",
                                           "enabled": False}]})

    def test_custom_slots_fall_back_to_the_builtin_templates(self):
        slot = SlotConfig(id="latenight", name="夜报", time="23:00", mode="agent")
        self.assertIn("报纸", get_image_prompt(slot))
        self.assertIn("联网搜索", get_research_prompt(slot))


class WindowTests(unittest.TestCase):
    def build(self, *specs):
        return [SlotConfig(id=i, name=n, time=t) for i, n, t in specs]

    def test_window_starts_at_the_previous_edition(self):
        slots = self.build(("morning", "早报", "08:00"), ("noon", "午报", "12:30"),
                           ("evening", "晚报", "19:00"))
        noon = next(s for s in slots if s.id == "noon")
        start, is_other = previous_edition_time(slots, noon, NOW)
        self.assertEqual(start.isoformat(), "2026-11-13T08:00:00+08:00")
        self.assertTrue(is_other)

    def test_first_edition_wraps_to_yesterdays_last(self):
        slots = self.build(("morning", "早报", "08:00"), ("noon", "午报", "12:30"),
                           ("evening", "晚报", "19:00"))
        start, _ = previous_edition_time(slots, slots[0], NOW)
        self.assertEqual(start.isoformat(), "2026-11-12T19:00:00+08:00")

    def test_single_slot_window_is_24_hours(self):
        only = self.build(("noon", "午报", "12:30"))
        start, is_other = previous_edition_time(only, only[0], NOW)
        self.assertEqual(start.isoformat(), "2026-11-12T12:30:00+08:00")
        self.assertFalse(is_other)

    def test_added_slot_joins_the_ordering(self):
        slots = self.build(("morning", "早报", "08:00"), ("latenight", "夜报", "23:00"),
                           ("noon", "午报", "12:30"))
        late = next(s for s in slots if s.id == "latenight")
        start, _ = previous_edition_time(slots, late, NOW)
        self.assertEqual(start.isoformat(), "2026-11-13T12:30:00+08:00")

    def test_service_window_follows_the_configured_set(self):
        cfg = default_config()
        write_slots(cfg, slots_of(cfg) + [SlotConfig(id="latenight", name="夜报", time="23:00")])
        # 23:00 的上一档是 19:00 晚报，不是中午。
        self.assertEqual(research_window("latenight", NOW, cfg).hour, 19)

    def test_unknown_slot_id_is_rejected(self):
        with self.assertRaises(ValueError):
            research_window("ghost", NOW, default_config())


class RunIdTests(unittest.TestCase):
    def test_custom_ids_produce_valid_run_ids(self):
        self.assertTrue(_RUN_ID.fullmatch("20261113-latenight-0123456789ab"))
        self.assertTrue(_RUN_ID.fullmatch("20261113-morning-0123456789ab"))
        self.assertTrue(_RUN_ID.fullmatch("20261113-slot2130-0123456789ab"))

    def test_hostile_ids_are_rejected(self):
        for bad in ("20261113-../../etc-0123456789ab", "20261113-A-0123456789ab",
                    "20261113--0123456789ab"):
            with self.subTest(bad=bad):
                self.assertFalse(_RUN_ID.fullmatch(bad))


class CommandTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.cfg = Config(default_config())
        self.plugin = ENTRY.DailyImageReport(None, self.cfg)
        self.plugin.service = types.SimpleNamespace(
            slot_locks={}, _scheduled=set(), clock=lambda: NOW,
            store=types.SimpleNamespace(recent=lambda n: []))

    async def run_cmd(self, text, persist=True):
        """persist=True keeps the write in memory only, for pure parse assertions."""
        if persist:
            return [r async for r in self.plugin.report(Event(text))]
        with patch.object(ENTRY.DailyImageReport, "_persist"):
            return [r async for r in self.plugin.report(Event(text))]

    async def test_add_list_remove_round_trip(self):
        # 真走持久化分支，确认新增会落盘。
        self.assertIn("已新增 夜报", (await self.run_cmd("/日报 新增 夜报 21:30"))[0])
        added = find_slot(slots_of(self.cfg), "夜报")
        self.assertIsNotNone(added)
        self.assertEqual((added.time, added.mode, added.enabled), ("21:30", "agent", True))
        self.assertTrue(self.cfg.saved)

        listed = (await self.run_cmd("/日报 列表", persist=False))[0]
        self.assertIn("4. 夜报｜slot2130｜21:30｜agent｜启用｜0群", listed)
        self.assertIn("1. 早报｜morning｜08:00｜agent｜启用｜1群", listed)

        self.assertIn("已删除 夜报", (await self.run_cmd("/日报 删除 夜报"))[0])
        self.assertIsNone(find_slot(slots_of(self.cfg), "夜报"))

    async def test_add_validates_input_and_duplicates(self):
        self.assertIn("格式", (await self.run_cmd("/日报 新增 夜报", persist=False))[0])
        self.assertIn("HH:MM", (await self.run_cmd("/日报 新增 夜报 25:00", persist=False))[0])
        await self.run_cmd("/日报 新增 夜报 21:30", persist=False)
        self.assertIn("已存在", (await self.run_cmd("/日报 新增 夜报 22:00", persist=False))[0])

    async def test_ascii_names_get_readable_ids(self):
        await self.run_cmd("/日报 新增 late 22:00", persist=False)
        self.assertEqual(find_slot(slots_of(self.cfg), "late").id, "late")
        await self.run_cmd("/日报 新增 夜报 23:00", persist=False)
        self.assertEqual(find_slot(slots_of(self.cfg), "夜报").id, "slot2300")

    async def test_time_command_moves_a_slot(self):
        self.assertIn("07:15", (await self.run_cmd("/日报 时间 早报 07:15", persist=False))[0])
        self.assertEqual(find_slot(slots_of(self.cfg), "早报").time, "07:15")
        self.assertIn("HH:MM", (await self.run_cmd("/日报 时间 早报 明早", persist=False))[0])

    async def test_last_slot_cannot_be_removed(self):
        write_slots(self.cfg, [SlotConfig(id="only", name="唯一档", time="09:00")])
        self.assertIn("至少保留一个时段", (await self.run_cmd("/日报 删除 唯一档", persist=False))[0])

    async def test_running_slot_cannot_be_removed(self):
        busy = asyncio.Lock()
        await busy.acquire()
        self.plugin.service.slot_locks["morning"] = busy
        try:
            self.assertIn("正在生成", (await self.run_cmd("/日报 删除 早报", persist=False))[0])
        finally:
            busy.release()
        self.assertIsNotNone(find_slot(slots_of(self.cfg), "早报"))

    async def test_enable_disable_keep_list_shape(self):
        await self.run_cmd("/日报 停用 午报", persist=False)
        self.assertFalse(find_slot(slots_of(self.cfg), "午报").enabled)
        self.assertIsInstance(self.cfg["schedules"], list)
        await self.run_cmd("/日报 启用 午报", persist=False)
        self.assertTrue(find_slot(slots_of(self.cfg), "午报").enabled)

    async def test_bind_all_covers_added_slots(self):
        await self.run_cmd("/日报 新增 夜报 21:30", persist=False)
        messages = await self.run_cmd("/日报 绑定 全部", persist=False)
        self.assertIn("夜报", messages[0])
        for item in self.cfg["schedules"]:
            self.assertIn("bot-instance:GroupMessage:123", item["targets"])

    async def test_added_slot_can_be_generated(self):
        calls = []

        async def run(slot, **kwargs):
            calls.append((slot, kwargs))
            return {"status": "success"}

        tasks = []

        def spawn(coro, name):
            task = asyncio.create_task(coro)
            tasks.append(task)
            return task

        await self.run_cmd("/日报 新增 夜报 21:30", persist=False)
        self.plugin.service = types.SimpleNamespace(
            run=run, spawn=spawn, clock=lambda: NOW, slot_locks={}, _scheduled=set(),
            store=types.SimpleNamespace(recent=lambda n: []))
        messages = [r async for r in self.plugin.report(Event("/日报 测试 夜报"))]
        await asyncio.gather(*tasks)
        self.assertEqual(calls[0][0], "slot2130")
        self.assertEqual(calls[0][1]["preview_target"], "bot-instance:GroupMessage:789_123")
        self.assertIn("已开始生成", messages[0])

    async def test_unknown_slot_error_lists_current_set(self):
        await self.run_cmd("/日报 新增 夜报 21:30", persist=False)
        message = (await self.run_cmd("/日报 测试 半夜报", persist=False))[0]
        self.assertIn("未知时段", message)
        self.assertIn("夜报(21:30)", message)


if __name__ == "__main__":
    unittest.main()
