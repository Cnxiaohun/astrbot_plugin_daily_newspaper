"""Strict lightweight prompt rendering with one trusted run clock."""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

from .config import SlotConfig, get_image_prompt, get_research_prompt, validate_timezone
from .models import MAX_PROMPT_CHARS, DailyBrief, text

_PLACEHOLDER_RE = re.compile(r"\{\{\s*([a-zA-Z_][\w.]*)\s*\}\}")
_WEEKDAYS = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")


def now_parts(tz_name: str, now: datetime | None = None) -> dict[str, str]:
    """Convert an aware datetime, rejecting ambiguous naive clocks/invalid zones."""
    tz = validate_timezone(tz_name)
    if now is None:
        now = datetime.now(tz)
    elif now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now 必须是带时区 datetime")
    now = now.astimezone(tz)
    return {
        "date": now.strftime("%Y-%m-%d"), "time": now.strftime("%H:%M"),
        "weekday": _WEEKDAYS[now.weekday()], "year": now.strftime("%Y"),
        "month": now.strftime("%m"), "day": now.strftime("%d"),
        "cutoff": now.isoformat(),
    }


def _lookup(ctx: dict[str, Any], path: str) -> Any:
    value: Any = ctx
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            raise ValueError(f"未知模板占位符：{path}")
        value = value[part]
    return value


def render(template: str, ctx: dict[str, Any]) -> str:
    """Substitute {{key}} / {{a.b}} once; reject typos/unsupported Jinja syntax."""
    template = text(template, "prompt template", MAX_PROMPT_CHARS)
    # Validate source syntax before replacement; inserted news is never evaluated.
    remainder = _PLACEHOLDER_RE.sub("", template)
    if "{{" in remainder or "}}" in remainder or "{%" in remainder:
        raise ValueError("模板仅支持 {{name}} / {{object.field}}，不支持循环/表达式")

    def sub(match: re.Match[str]) -> str:
        value = _lookup(ctx, match.group(1))
        if value is None:
            raise ValueError(f"模板占位符无值：{match.group(1)}")
        if isinstance(value, (list, tuple)):
            return "\n".join(str(v) for v in value)
        return str(value)

    result = _PLACEHOLDER_RE.sub(sub, template)
    if len(result) > MAX_PROMPT_CHARS:
        raise ValueError(f"渲染提示词超过 {MAX_PROMPT_CHARS} 字符，不能静默截断")
    return result


def _clean_visual(value: Any) -> str:
    return re.sub(r"(AI\s*生成)?示意图?|AI\s*示意", "", str(value or "")).strip(" ：:，,")


def _brief_context(brief: DailyBrief) -> dict[str, Any]:
    stories = []
    for index, story in enumerate(brief.stories, 1):
        section = f"【{story.section}】" if story.section else ""
        lines = [
            f"重点新闻{index} {section}[{story.tag}]：{story.display_title or story.title}",
            f"摘要：{story.summary}",
        ]
        if story.tag == "UPDATE":
            lines.append(f"新增进展（UPDATE）：{story.update or story.summary}")
        if story.source:
            lines.append(f"来源：{story.source}")
        visual = _clean_visual(story.image_brief)
        if not visual:
            place = story.locations[0] if story.locations else "事件相关地点"
            visual = f"{place}，与“{story.display_title or story.title}”直接相关的新闻场景，不要通用图标或抽象拼贴"
        lines.append(f"配图画面（必须与该条新闻直接相关，具体到地点/主体/动作/环境）：{visual}")
        stories.append("\n".join(lines))
    history = "；".join(f"{item['year']}年{item['event']}（{item['source']}）" for item in brief.on_this_day)
    places = list(brief.world_map)
    if not places:
        seen = set()
        for index, story in enumerate(brief.stories, 1):
            for location in story.locations:
                if location not in seen:
                    places.append({"location": location, "label": f"新闻{index}"})
                    seen.add(location)
    map_text = "\n".join(
        f"地图标注{index}：{item['location']} — {item.get('label', '')}"
        for index, item in enumerate(places, 1)
    )
    history_text = f"历史上的今天｜ON THIS DAY：{history}" if history else ""
    return {
        "cover": {"headline": brief.cover_headline, "summary": brief.cover_summary, "image_brief": _clean_visual(brief.cover_image_brief)},
        "world_in_60_seconds": "\n".join(f"短讯{i}：{value}" for i, value in enumerate(brief.world_in_60_seconds, 1)),
        "top_stories": "\n\n".join(stories),
        "on_this_day": history_text,
        "on_this_day_block": history_text,
        "why_it_matters": brief.why_it_matters, "local_lens": brief.local_lens,
        "watch_next": brief.watch_next,
        "watch_next_block": f"明日关注（WATCH NEXT）：{brief.watch_next}" if brief.watch_next else "",
        "sources_short": brief.source_names(limit=6),
        "world_map": map_text or "本期无已核实地点，省略地点标记，不可猜测。",
        "world_map_block": f"世界地图（重点事件标注）：\n{map_text}" if map_text else "",
        "locations": map_text,
    }


class PromptBuilder:
    """Use one aware datetime/parts per run, never model-provided date/branding.

    Existing positional call signatures remain supported. A builder freezes its
    default clock at construction; the orchestrator should pass the same `parts`
    to both methods, including actual scheduled `window_start` and `cutoff`.
    """

    def __init__(
        self, edition: str, handle: str, timezone: str, *,
        now: datetime | None = None, parts: dict[str, Any] | None = None,
    ):
        self.edition = text(edition, "edition", 120)
        self.handle = text(handle, "handle", 100)
        if not self.edition or not self.handle:
            raise ValueError("刊头和群标识不得为空")
        self.timezone = timezone
        self.tz = validate_timezone(timezone)
        self._now = now if now is not None else datetime.now(self.tz)
        self._parts = dict(parts or {})
        now_parts(timezone, self._now)  # Validate the clock immediately.

    def _base_context(
        self, slot: SlotConfig, size: str,
        extra: dict[str, Any] | None = None, *,
        now: datetime | None = None, parts: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        current = now_parts(self.timezone, now or self._now)
        overrides = self._parts if parts is None else parts
        if not isinstance(overrides, dict):
            raise ValueError("parts 必须是程序构造的字典")
        current.update(overrides)
        # Date/time are trusted program inputs, but still validate their shape
        # and derive weekday/year/month/day rather than accepting inconsistencies.
        parsed = datetime.strptime(f"{current['date']} {current['time']}", "%Y-%m-%d %H:%M").replace(tzinfo=self.tz)
        current.update({"weekday": _WEEKDAYS[parsed.weekday()], "year": parsed.strftime("%Y"), "month": parsed.strftime("%m"), "day": parsed.strftime("%d")})
        if ("date" in overrides or "time" in overrides) and "cutoff" not in overrides:
            current["cutoff"] = parsed.isoformat()
        # Backward-compatible default windows; production scheduler supplies its
        # previous configured slot occurrence via parts.window_start.
        previous = {"morning": (19, 0), "noon": (8, 0), "evening": (12, 30)}[slot.id]
        start = parsed.replace(hour=previous[0], minute=previous[1], second=0, microsecond=0)
        if slot.id == "morning" or start >= parsed:
            start -= timedelta(days=1)
        current.setdefault("window_start", start.isoformat())
        for key in ("window_start", "cutoff"):
            try:
                instant = datetime.fromisoformat(str(current[key]).replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError(f"{key} 必须为带时区 ISO8601") from exc
            if instant.tzinfo is None or instant.utcoffset() is None:
                raise ValueError(f"{key} 必须带时区")
        if datetime.fromisoformat(str(current["window_start"]).replace("Z", "+00:00")) >= datetime.fromisoformat(str(current["cutoff"]).replace("Z", "+00:00")):
            raise ValueError("window_start 必须早于 cutoff")
        context: dict[str, Any] = dict(current)
        if extra:
            context.update(extra)
        # Never let research or untrusted extras replace program-owned identity.
        context.update({"edition": self.edition, "handle": self.handle, "brand_handle": self.handle, "timezone": self.timezone, "slot_id": slot.id, "slot_name": slot.name, "size": size})
        return context

    def build_research_prompt(
        self, slot: SlotConfig, recent_history: str, size: str = "", *,
        now: datetime | None = None, parts: dict[str, Any] | None = None,
    ) -> str:
        recent_history = text(recent_history, "recent_history", 20000)
        context = self._base_context(slot, size, {"recent_history": recent_history or "（无）"}, now=now, parts=parts)
        return render(get_research_prompt(slot), context)

    def build_image_prompt(
        self, slot: SlotConfig, brief: DailyBrief | None, size: str, *,
        now: datetime | None = None, parts: dict[str, Any] | None = None,
    ) -> str:
        context = self._base_context(slot, size, now=now, parts=parts)
        if slot.mode == "agent":
            if brief is None:
                raise ValueError("agent 模式不得用空新闻简报生图")
            # Validate direct dataclass inputs too, not only JSON-created briefs.
            brief = DailyBrief.from_dict(brief.to_dict())
            if not brief.cover_headline or not brief.cover_summary or not brief.stories:
                raise ValueError("资讯简报必须包含封面标题、摘要及有来源重点新闻")
            for story in brief.stories:
                if not story.title or not story.summary or not story.source or not story.url:
                    raise ValueError("重点新闻必须包含 title/summary/source/url")
            brief.validate_for_publication(context["date"])
            context.update(_brief_context(brief))
        elif brief is not None:
            raise ValueError("static 模式不应传入研究简报")
        result = render(get_image_prompt(slot), context)
        if len(result) > MAX_PROMPT_CHARS:
            raise ValueError("最终图片提示词过长")
        return result
