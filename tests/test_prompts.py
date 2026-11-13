"""Pure tests for validated config, factual data and trusted prompt rendering."""
from __future__ import annotations

import copy
import json
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from core.config import (
    DEFAULT_RESEARCH_PROMPTS,
    SlotConfig,
    get_image_prompt,
    get_slot,
    load_slots,
    validate_config,
    validate_targets,
)
from core.models import MAX_PROMPT_CHARS, DailyBrief, Story
from core.prompt_builder import PromptBuilder, now_parts, render

TZ = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 11, 12, 23, 59, 59, tzinfo=TZ)
TARGET = "napcat-main:GroupMessage:123456"


def defaults():
    schema = json.loads((Path(__file__).parents[1] / "_conf_schema.json").read_text())

    def walk(items):
        return {
            key: walk(value["items"]) if value["type"] == "object" else copy.deepcopy(value.get("default"))
            for key, value in items.items()
        }

    return walk(schema)


def brief_data():
    return {
        "edition": "不可信模型刊头", "date": "2099-01-01", "weekday": "星期一",
        "cover": {"headline": "双方达成原则协议", "summary": "据机构公告，具体安排仍待磋商。", "image_brief": "会议桌的AI示意"},
        "world_in_60_seconds": ["尚无最终协议", "更多细节待核实"],
        "top_stories": [
            {"title": "双方磋商出现新进展", "display_title": "磋商新进展", "summary": "双方称已达成原则协议，但条款未公开。", "source": "可靠媒体", "url": "https://example.com/report", "tag": "UPDATE", "update": "新增双方共同公告，未形成最终协议。", "event_key": "talks-2026", "published_at": "2026-11-12T17:30:00+08:00", "locations": ["日内瓦"]},
        ],
        "world_map": [{"location": "日内瓦", "label": "联合公告"}],
        "why_it_matters": "原则共识不等于最终协议。", "local_lens": "各方仍关注具体条款。", "watch_next": "观察双方是否公布文本。",
        "on_this_day": [], "sources": ["https://example.com/report"],
    }


class ConfigTests(unittest.TestCase):
    def test_schema_defaults_valid_and_all_agent(self):
        config = defaults()
        validate_config(config)
        slots = load_slots(config)
        self.assertEqual([s.time for s in slots], ["08:00", "12:30", "19:00"])
        self.assertTrue(all(s.mode == "agent" for s in slots))
        self.assertFalse(config["qa"]["enabled"])
        self.assertEqual(config["api"]["api_key"], "")

    def test_bad_time_not_silently_defaulted(self):
        for value in ("24:00", "08:60", "abc", "", "8:00", None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                load_slots({"schedules": {"morning": {"time": value}}})

    def test_bad_mode(self):
        for mode in ("manual", "", None):
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                load_slots({"schedules": {"morning": {"mode": mode}}})

    def test_invalid_timezone_rejected(self):
        with self.assertRaises(ValueError):
            validate_config({"timezone": "Asia/NotARealCity"})
        with self.assertRaises(ValueError):
            now_parts("Asia/NotARealCity")

    def test_numeric_limits_are_integers_not_bools_or_coerced(self):
        bads = [
            {"limits": {"max_concurrent_generations": 0}},
            {"limits": {"max_runs_per_day": -1}},
            {"limits": {"retention_days": 0}},
            {"limits": {"catchup_window_minutes": 721}},
            {"limits": {"stale_run_minutes": "15"}},
            {"api": {"timeout_seconds": True}},
            {"research": {"max_repair_retries": 4}},
            {"qa": {"retry_on_fail": 3}},
        ]
        for config in bads:
            with self.subTest(config=config), self.assertRaises(ValueError):
                validate_config(config)

    def test_targets_array_full_group_umo_and_deduplicate(self):
        self.assertEqual(validate_targets([TARGET, TARGET]), [TARGET])
        for value in (TARGET, ["123456"], ["napcat:FriendMessage:123456"], [None], ["napcat:GroupMessage:"]):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_targets(value)

    def test_static_even_disabled_needs_own_prompt(self):
        for enabled in (True, False):
            with self.assertRaises(ValueError):
                validate_config({"schedules": {"morning": {"mode": "static", "enabled": enabled}}})
        slot = get_slot({"schedules": {"morning": {"mode": "static", "image_prompt": "问候图 {{date}}"}}}, "早报")
        self.assertEqual(get_image_prompt(slot), "问候图 {{date}}")

    def test_bad_nested_types_and_fallback_array_rejected(self):
        for config in ({"api": "bad"}, {"schedules": []}, {"schedules": {"morning": None}}, {"api": {"fallback_image_models": "hunyuan-image-alpha"}}):
            with self.subTest(config=config), self.assertRaises(ValueError):
                validate_config(config)

    def test_extra_body_cannot_replace_news_prompt(self):
        with self.assertRaises(ValueError):
            validate_config({"api": {"extra_body": {"prompt": "wrong"}}})
        with self.assertRaises(ValueError):
            validate_config({"api": {"extra_body": {"bad": float("nan")}}})

    def test_long_prompt_rejected(self):
        with self.assertRaises(ValueError):
            validate_config({"schedules": {"morning": {"image_prompt": "字" * (MAX_PROMPT_CHARS + 1)}}})


class ModelTests(unittest.TestCase):
    def test_legacy_fields_and_metadata_roundtrip(self):
        data = brief_data()
        brief = DailyBrief.from_dict(data)
        second = DailyBrief.from_dict(brief.to_dict())
        self.assertEqual(second.to_dict(), brief.to_dict())
        self.assertEqual(second.stories[0].published_at, data["top_stories"][0]["published_at"])
        self.assertEqual(second.stories[0].event_key, "talks-2026")
        legacy = DailyBrief.from_dict({"stories": [{"title": "旧字段标题"}]})
        self.assertEqual(legacy.stories[0].title, "旧字段标题")

    def test_bad_arrays_not_char_by_char(self):
        for key in ("sources", "top_stories", "world_in_60_seconds", "on_this_day", "world_map"):
            with self.subTest(key=key), self.assertRaises(ValueError):
                DailyBrief.from_dict({key: "not-an-array"})
        with self.assertRaises(ValueError):
            Story.from_dict({"locations": "日内瓦"})

    def test_wrong_nested_types_rejected(self):
        for data in ({"cover": []}, {"top_stories": ["bad"]}, {"sources": [1]}, {"top_stories": [{"summary": ["bad"]}]}):
            with self.subTest(data=data), self.assertRaises(ValueError):
                DailyBrief.from_dict(data)

    def test_facts_not_truncated(self):
        title = "据双方公告磋商取得原则共识但仍需进一步协商重要细节" * 2
        summary = "尚未达成最终协议，" * 8
        story = Story.from_dict({"title": title, "summary": summary})
        self.assertEqual(story.title, title)
        self.assertEqual(story.summary, summary)
        self.assertTrue(story.display_title.endswith("…"))
        with self.assertRaises(ValueError):
            Story.from_dict({"summary": "字" * 241})

    def test_published_at_must_include_timezone(self):
        for value in ("2026-11-12T17:00:00", "2026-11-12", "not-time"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                Story.from_dict({"published_at": value})
        self.assertEqual(Story.from_dict({"published_at": "2026-11-12T09:00:00Z"}).published_at, "2026-11-12T09:00:00Z")

    def test_history_source_year_calendar_validation(self):
        item = {"year": 1866, "month": 11, "day": 12, "event": "某历史事件", "source": "历史档案", "url": "https://example.com/history"}
        brief = DailyBrief.from_dict({"on_this_day": [item]})
        brief.validate_for_publication("2026-11-12")
        self.assertEqual(brief.to_dict()["on_this_day"][0]["url"], item["url"])
        for change in ({"day": 13}, {"source": ""}, {"year": 2027}, {"date": "1865-11-12"}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                DailyBrief.from_dict({"on_this_day": [dict(item, **change)]}).validate_for_publication("2026-11-12")

    def test_history_complete_date_is_supported(self):
        brief = DailyBrief.from_dict({"on_this_day": [{"year": 1866, "date": "1866-11-12", "event": "核实史实", "source": "档案", "url": "https://example.com/history"}]})
        brief.validate_for_publication("2026-11-12")
        with self.assertRaises(ValueError):
            DailyBrief.from_dict({"on_this_day": [{"year": 1866, "event": "无来源史实"}]}).validate_for_publication("2026-11-12")

    def test_source_urls_and_coordinates_validated(self):
        for data in ({"sources": ["javascript:bad"]}, {"world_map": [{"location": "日内瓦", "latitude": 1000}]}):
            with self.subTest(data=data), self.assertRaises(ValueError):
                DailyBrief.from_dict(data)


class PromptTests(unittest.TestCase):
    def setUp(self):
        self.slot = get_slot({}, "早报")
        self.brief = DailyBrief.from_dict(brief_data())
        self.builder = PromptBuilder("程序可信刊名", "@指定群", "Asia/Shanghai", now=NOW)

    def test_trusted_brand_and_date_not_replaced_by_research(self):
        prompt = self.builder.build_image_prompt(self.slot, self.brief, "1536x1024")
        self.assertIn("程序可信刊名", prompt)
        self.assertIn("2026-11-12 星期四", prompt)
        self.assertNotIn("2099-01-01", prompt)
        self.assertNotIn("不可信模型刊头", prompt)

    def test_one_run_frozen_across_midnight(self):
        first = self.builder.build_research_prompt(self.slot, "无", "1536x1024")
        image = self.builder.build_image_prompt(self.slot, self.brief, "1536x1024")
        self.assertIn("2026-11-12", first)
        self.assertIn("2026-11-12", image)
        tomorrow = NOW + timedelta(minutes=1)
        changed = self.builder.build_image_prompt(self.slot, self.brief, "1536x1024", now=tomorrow)
        self.assertIn("2026-11-13", changed)

    def test_parts_use_run_date_and_derive_weekday(self):
        parts = {"date": "2026-11-10", "time": "08:00", "weekday": "错误星期", "window_start": "2026-11-09T19:00:00+08:00", "cutoff": "2026-11-10T08:00:00+08:00"}
        prompt = self.builder.build_image_prompt(self.slot, self.brief, "1536x1024", parts=parts)
        self.assertIn("2026-11-10 星期二", prompt)
        self.assertNotIn("错误星期", prompt)

    def test_window_fields_in_research_and_themes_distinct(self):
        parts = {"window_start": "2026-11-12T12:30:00+08:00", "cutoff": NOW.isoformat()}
        prompt = self.builder.build_research_prompt(self.slot, "前刊内容", parts=parts)
        self.assertIn(parts["window_start"], prompt)
        self.assertIn(parts["cutoff"], prompt)
        self.assertIn("前刊内容", prompt)
        self.assertIn("published_at", prompt)
        self.assertIn("event_key", prompt)
        self.assertIn("month", prompt)
        self.assertEqual(len(set(DEFAULT_RESEARCH_PROMPTS.values())), 3)

    def test_summary_update_map_and_synthetic_labels_in_prompt(self):
        prompt = self.builder.build_image_prompt(self.slot, self.brief, "1536x1024")
        self.assertIn(self.brief.stories[0].summary, prompt)
        self.assertIn("[UPDATE]", prompt)
        self.assertIn(self.brief.stories[0].update, prompt)
        self.assertIn("地图标注1：日内瓦 — 联合公告", prompt)
        self.assertNotIn("AI示意", prompt)
        self.assertNotIn("非现场照片", prompt)
        self.assertNotIn("ON THIS DAY", prompt)

    def test_map_fallback_from_story_locations(self):
        self.brief.world_map = []
        prompt = self.builder.build_image_prompt(self.slot, self.brief, "1536x1024")
        self.assertIn("地图标注1：日内瓦 — 新闻1", prompt)

    def test_missing_brief_rejected_not_fake_blank_news(self):
        for brief in (None, DailyBrief()):
            with self.subTest(brief=brief), self.assertRaises(ValueError):
                self.builder.build_image_prompt(self.slot, brief, "1536x1024")
        data = brief_data()
        data["top_stories"][0]["source"] = ""
        with self.assertRaises(ValueError):
            self.builder.build_image_prompt(self.slot, DailyBrief.from_dict(data), "1536x1024")

    def test_static_user_prompt_has_date_and_no_research(self):
        slot = SlotConfig("morning", "早报", mode="static", image_prompt="节日海报 {{date}} {{handle}}")
        prompt = self.builder.build_image_prompt(slot, None, "1536x1024")
        self.assertEqual(prompt, "节日海报 2026-11-12 @指定群")

    def test_unknown_placeholder_and_jinja_are_errors(self):
        for template in ("{{typo}}", "{{ cover.wrong }}", "{{ title | upper }}", "{% for x in list %}"):
            with self.subTest(template=template), self.assertRaises(ValueError):
                render(template, {"cover": {"headline": "test"}})

    def test_inserted_placeholder_not_evaluated_recursively(self):
        self.assertEqual(render("{{value}}", {"value": "{{not_a_variable}}"}), "{{not_a_variable}}")

    def test_output_and_input_length_limits(self):
        with self.assertRaises(ValueError):
            render("x" * (MAX_PROMPT_CHARS + 1), {})
        with self.assertRaises(ValueError):
            render("{{value}}", {"value": "x" * (MAX_PROMPT_CHARS + 1)})
        with self.assertRaises(ValueError):
            self.builder.build_research_prompt(self.slot, "x" * 20001)

    def test_naive_datetime_and_invalid_window_rejected(self):
        with self.assertRaises(ValueError):
            PromptBuilder("刊头", "@群", "Asia/Shanghai", now=datetime(2026, 11, 12))
        with self.assertRaises(ValueError):
            self.builder.build_research_prompt(self.slot, "", parts={"window_start": "2027-01-01T00:00:00+08:00"})

    def test_history_missing_evidence_rejected_before_image(self):
        self.brief.on_this_day = [{"year": 1866, "event": "未核实史实"}]
        with self.assertRaises(ValueError):
            self.builder.build_image_prompt(self.slot, self.brief, "1536x1024")


if __name__ == "__main__":
    unittest.main()
