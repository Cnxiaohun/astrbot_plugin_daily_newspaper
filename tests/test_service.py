"""No real API or group calls. End-to-end orchestration with injected clients."""
from __future__ import annotations

import asyncio
import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from core.config import get_slot
from core.models import DailyBrief
from core.research import validate_brief
from core.scheduler import is_due, next_time
from core.service import ReportService
from core.store import Store

class NullLogger:
    """测试用最小日志器。插件内部不允许使用内置 logging，测试同样不引入。"""

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


NOW = datetime(2026, 11, 12, 8, 0, 10, tzinfo=ZoneInfo("Asia/Shanghai"))
TARGET = "napcat-instance:GroupMessage:123456"
TARGET2 = "napcat-instance:GroupMessage:456789"


def default_config():
    schema = json.loads((Path(__file__).parents[1] / "_conf_schema.json").read_text())
    def defaults(items):
        out = {}
        for key, v in items.items():
            value = defaults(v["items"]) if v["type"] == "object" else v.get("default")
            out[key] = copy.deepcopy(value)
        return out
    cfg = defaults(schema)
    cfg["api"]["api_key"] = "not-a-real-secret"
    slot_entry(cfg, "morning")["targets"] = [TARGET]
    return cfg


def slot_entry(cfg, slot_id):
    """Fetch one schedule entry from the dynamic template_list form."""
    return next(x for x in cfg["schedules"] if x["id"] == slot_id)


def brief():
    return DailyBrief.from_dict({
        "edition": "THE WORLD TODAY｜寰球三刻", "date": "2026-11-12", "weekday": "星期四",
        "cover": {"headline": "机构发布新公告", "summary": "测试摘要，不代表真实新闻。"},
        "top_stories": [dict(title=f"测试新闻{i}", display_title=f"测试新闻{i}", summary="仅为自动化测试数据。", source="测试来源", url=f"https://example.com/{i}") for i in range(3)],
        "world_in_60_seconds": ["测试一", "测试二", "测试三"],
        "why_it_matters": "测试栏目", "local_lens": "测试视角", "watch_next": "测试关注",
        "on_this_day": [], "sources": ["https://example.com/a"],
    })


class FakeResearch:
    calls = 0
    def __init__(self, *args, **kwargs):
        self.last_evidence = {"web_search_calls": ["mock"]}
    async def research(self, *args, **kwargs):
        type(self).calls += 1
        return brief(), {"input_tokens": 100}
    async def close(self):
        pass


class FakeImage:
    calls = 0
    def __init__(self, *args, **kwargs):
        pass
    async def generate(self, *args):
        type(self).calls += 1
        return SimpleNamespace(data=b"fake_image_for_mock_sender", model="fake-image", mime="image/png", width=1536, height=1024, usage={})
    async def close(self):
        pass


class PipelineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.cfg = default_config()
        self.sent = []
        FakeResearch.calls = FakeImage.calls = 0
        async def sender(ctx, target, path):
            self.sent.append(target)
            self.assertTrue(path.is_file())
            return True
        self.alerts = []
        async def alerter(context, umo, text):
            self.alerts.append((umo, text))
        self.announcements = []
        async def announcer(context, umos, **kwargs):
            self.announcements.append((list(umos), kwargs))
        self.service = ReportService(None, self.cfg, Path(self.temp.name), logger=NullLogger(),
                                     clock=lambda: NOW,
            sender=sender, alerter=alerter, research_factory=FakeResearch, image_factory=FakeImage,
            announcer=announcer)
        await self.service.start(schedule=False)
    async def asyncTearDown(self):
        await self.service.close()
        self.temp.cleanup()

    async def test_generate_once_dedupe_and_persist(self):
        r = await self.service.run("morning")
        self.assertEqual(r["status"], "success")
        self.assertEqual(FakeResearch.calls, 1)
        self.assertEqual(FakeImage.calls, 1)
        self.assertEqual(self.sent, [TARGET])
        self.assertTrue((self.service.store.run_dir(r["run_id"]) / "evidence.json").is_file())
        second = await self.service.run("morning")
        self.assertEqual(second["status"], "skipped")
        self.assertEqual(FakeImage.calls, 1)
        self.assertIn("测试新闻", self.service.store.history(NOW))

    async def test_preview_is_not_production_or_history(self):
        r = await self.service.run("morning", preview_target=TARGET)
        self.assertTrue(r["preview"])
        self.assertEqual(self.service.store.receipts("morning", NOW.date().isoformat()), {})
        self.assertNotIn("测试新闻", self.service.store.history(NOW))
        await self.service.run("morning")
        self.assertEqual(FakeImage.calls, 2)

    async def test_partial_uses_existing_image_for_new_target(self):
        await self.service.run("morning")
        slot_entry(self.cfg, "morning")["targets"].append(TARGET2)
        r = await self.service.run("morning")
        self.assertEqual(r["status"], "success")
        self.assertEqual(self.sent, [TARGET, TARGET2])
        self.assertEqual(FakeImage.calls, 1)

    async def test_send_exception_is_uncertain_not_retried(self):
        async def ambiguous(*args):
            self.sent.append(TARGET)
            raise TimeoutError("adapter may have sent before timeout")
        self.service.sender = ambiguous
        r = await self.service.run("morning")
        self.assertEqual(r["receipts"][TARGET]["status"], "uncertain")
        with self.assertRaisesRegex(ValueError, "结果不明"):
            await self.service.run("morning")
        with self.assertRaisesRegex(ValueError, "结果不明"):
            await self.service.resend(r["run_id"])
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(FakeImage.calls, 1)

    async def test_concurrent_explicit_resend_never_duplicates(self):
        async def ambiguous(*args):
            raise TimeoutError("adapter may have sent before timeout")
        self.service.sender = ambiguous
        r = await self.service.run("morning")
        success_sends = []
        async def good(ctx, target, path):
            success_sends.append(target)
            return True
        self.service.sender = good
        results = await asyncio.gather(
            self.service.resend(r["run_id"], retry_uncertain=True),
            self.service.resend(r["run_id"], retry_uncertain=True),
            return_exceptions=True,
        )
        self.assertEqual(len(success_sends), 1)
        statuses = sorted(x.get("status") for x in results if isinstance(x, dict))
        self.assertIn("success", statuses)
        self.assertIn("skipped", statuses)

    async def test_no_target_costs_nothing(self):
        slot_entry(self.cfg, "morning")["targets"] = []
        with self.assertRaisesRegex(ValueError, "目标群"):
            await self.service.run("morning")
        self.assertEqual(FakeImage.calls, 0)
        self.assertEqual(FakeResearch.calls, 0)

    async def test_research_failure_never_generates_fake_news(self):
        class Broken(FakeResearch):
            async def research(self, *args, **kwargs):
                raise RuntimeError("no search evidence")
        self.service.research_factory = Broken
        r = await self.service.run("morning")
        self.assertEqual(r["status"], "failed")
        self.assertEqual(FakeImage.calls, 0)
        self.assertEqual(self.sent, [])

    async def test_corrupt_state_fails_closed(self):
        self.service.store.index_path.write_text("bad")
        with self.assertRaises(json.JSONDecodeError):
            Store(Path(self.temp.name))

    async def test_single_host_lease(self):
        other = Store(Path(self.temp.name))
        with self.assertRaises(RuntimeError):
            other.acquire()
        other.release()

    async def test_recover_sending_as_uncertain(self):
        slot = get_slot(self.cfg, "早报")
        r = await self.service.store.create(slot, NOW, [TARGET], False)
        r["receipts"] = {TARGET: {"status": "sending"}}
        await self.service.store.save(r)
        await self.service.store.recover()
        row = self.service.store.get(r["run_id"])
        self.assertEqual(row["status"], "interrupted")
        self.assertEqual(row["receipts"][TARGET]["status"], "uncertain")

    async def test_global_daily_cap_includes_previews(self):
        self.cfg["limits"]["max_runs_per_day"] = 1
        await self.service.run("morning", preview_target=TARGET)
        with self.assertRaisesRegex(ValueError, "每日运行次数"):
            await self.service.run("morning")

    async def test_scheduler_does_not_repeat_failed_paid_calls(self):
        class Broken(FakeResearch):
            async def research(self, *args, **kwargs):
                raise RuntimeError("failed research")
        self.service.research_factory = Broken
        await self.service.run("morning")
        await self.service.tick()
        self.assertEqual(self.service._scheduled, set())

    async def test_qa_failure_blocks_publication(self):
        async def badqa(*args, **kwargs):
            return {"pass": False, "missing": ["date"]}
        self.cfg["qa"]["enabled"] = True
        self.cfg["qa"]["retry_on_fail"] = 0
        self.service.qa_checker = badqa
        r = await self.service.run("morning")
        self.assertEqual(r["status"], "failed")
        self.assertEqual(self.sent, [])

    async def test_target_unbound_cannot_be_resent(self):
        async def unavailable(*args):
            return False
        self.service.sender = unavailable
        r = await self.service.run("morning")
        slot_entry(self.cfg, "morning")["targets"] = []
        with self.assertRaisesRegex(ValueError, "解绑"):
            await self.service.resend(r["run_id"])

    async def test_path_traversal_rejected(self):
        with self.assertRaises(ValueError):
            self.service.store.get("../../etc/passwd")


    async def test_failure_is_reported_to_chat_not_silent(self):
        class Broken(FakeResearch):
            async def research(self, *args, **kwargs):
                raise RuntimeError("研究网关超时")
        self.service.research_factory = Broken
        r = await self.service.run("morning")
        self.assertEqual(r["status"], "failed")
        # announce happened first, then a failure notice must also reach the group
        self.assertEqual(len(self.announcements), 1)
        self.assertTrue(self.alerts, "失败时必须在群里说明，不能只有告知")
        umo, text = self.alerts[0]
        self.assertEqual(umo, TARGET)
        self.assertIn("没做出来", text)
        self.assertIn("强制", text)
        self.assertEqual(FakeImage.calls, 0)

    async def test_research_transport_failure_retries_once(self):
        calls = {"n": 0}
        from core.http_utils import AmbiguousRequestError
        class Flaky(FakeResearch):
            async def research(self, *args, **kwargs):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise AmbiguousRequestError("读取超时")
                return brief(), {"input_tokens": 1}
        self.service.research_factory = Flaky
        r = await self.service.run("morning")
        self.assertEqual(r["status"], "success")
        self.assertEqual(calls["n"], 2)
        self.assertEqual(FakeImage.calls, 1)

    async def test_announce_before_generation_and_reuse(self):
        r = await self.service.run("morning", action="scheduled")
        self.assertEqual(r["status"], "success")
        self.assertEqual(len(self.announcements), 1)
        umos, kwargs = self.announcements[0]
        self.assertIn(TARGET, umos)
        self.assertEqual(kwargs["date"], "2026-11-12")
        self.assertEqual(kwargs["weekday"], "星期四")
        self.assertEqual(kwargs["slot_name"], "早报")
        self.assertFalse(kwargs["reuse"])
        self.assertEqual(self.announcements[0][1]["action"], "scheduled")
        # A preview later today forwards the existing formal edition instead
        # of paying for a second generation, and announces the reuse.
        r2 = await self.service.run("morning", preview_target=TARGET2, action="manual")
        self.assertEqual(r2["status"], "success")
        self.assertEqual(r2.get("message"), "已转发今日已生成版本")
        self.assertEqual(FakeImage.calls, 1)
        self.assertEqual(len(self.announcements), 2)
        self.assertTrue(self.announcements[1][1]["reuse"])

    async def test_force_regenerates_and_announces_force(self):
        await self.service.run("morning", action="scheduled")
        r = await self.service.run("morning", force=True, action="force")
        self.assertEqual(r["status"], "success")
        self.assertEqual(FakeImage.calls, 2)
        self.assertEqual(self.announcements[-1][1]["action"], "force")
        self.assertFalse(self.announcements[-1][1]["reuse"])

    async def test_duplicate_event_rejected_by_research_validation(self):
        from core.research import validate_brief
        data = brief().to_dict()
        with self.assertRaisesRegex(ValueError, "重复"):
            validate_brief(data, "2026-11-12", "刊", forbidden_event_keys=set(),
                           forbidden_titles={__import__('re').sub(r"[^\w]+", "", data["cover"]["headline"]).casefold()})
        keys, titles = self.service.store.same_day_events("2026-11-12")
        self.assertEqual(keys, set())
        self.assertEqual(titles, set())

class EditorialCopyTests(unittest.TestCase):
    def test_internal_editorial_phrases_rejected_from_visible_copy(self):
        raw = brief().to_dict()
        for field, value in (
            ("headline", "午间候选：政策与产业背景"),
            ("summary", "上午新增证据不足，本期补充当日背景"),
        ):
            data = brief().to_dict()
            data["cover"][field] = value
            with self.assertRaisesRegex(ValueError, "编辑过程"):
                validate_brief(data, "2026-11-12", "THE WORLD TODAY｜寰球三刻")
        data = brief().to_dict()
        data["world_in_60_seconds"][0] = "背景｜路透：市场走弱"
        with self.assertRaisesRegex(ValueError, "编辑过程"):
            validate_brief(data, "2026-11-12", "THE WORLD TODAY｜寰球三刻")
        data = brief().to_dict()
        data["top_stories"][0]["summary"] = "此前报道：旧闻"
        with self.assertRaisesRegex(ValueError, "编辑过程"):
            validate_brief(data, "2026-11-12", "THE WORLD TODAY｜寰球三刻")


class SchedulerTests(unittest.TestCase):
    def test_window_and_next_time(self):
        slot = get_slot(default_config(), "早报")
        self.assertTrue(is_due(slot, NOW, 30))
        self.assertFalse(is_due(slot, NOW - timedelta(minutes=1), 30))
        self.assertFalse(is_due(slot, NOW + timedelta(minutes=31), 30))
        self.assertTrue(is_due(slot, NOW, 0))
        self.assertFalse(is_due(slot, NOW + timedelta(minutes=1), 0))
        self.assertEqual(next_time(slot, NOW).date(), (NOW + timedelta(days=1)).date())


if __name__ == "__main__":
    unittest.main()
