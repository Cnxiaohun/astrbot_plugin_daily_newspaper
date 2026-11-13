"""Validated data models; no AstrBot imports and no silent editing of news facts."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date as Date
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

# Recommended layout lengths are smaller (18/45); these are hard safety ceilings.
MAX_HEADLINE = 80
MAX_SUMMARY = 240
MAX_SHORT = 120
MAX_SECTION = 240
MAX_PROMPT_CHARS = 32000


def text(value: Any, name: str, limit: int = 1000) -> str:
    """Accept text only; reject overlong facts instead of silently cutting them."""
    if not isinstance(value, str):
        raise ValueError(f"{name} 必须是字符串")
    value = value.strip()
    if len(value) > limit:
        raise ValueError(f"{name} 超过 {limit} 字符上限，请精简后重试")
    return value


def clip(value: Any, limit: int) -> str:
    """Explicit display-only abbreviation. Never use for facts or stored titles."""
    value = text(value, "display text", MAX_PROMPT_CHARS)
    return value if len(value) <= limit else value[: limit - 1].rstrip() + "…"


def _object(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{name} 必须是对象")
    return value


def _list(value: Any, name: str, maximum: int = 20) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{name} 必须是数组")
    if len(value) > maximum:
        raise ValueError(f"{name} 最多 {maximum} 项")
    return value


def _url(value: Any, name: str) -> str:
    value = text(value, name, 2000)
    if value:
        parsed = urlsplit(value)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username:
            raise ValueError(f"{name} 必须是有效 HTTP(S) 来源链接")
    return value


def _published_at(value: Any, name: str) -> str:
    value = text(value, name, 80)
    if value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"{name} 必须是带时区 ISO8601 时间") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError(f"{name} 必须带时区")
    return value


@dataclass
class Story:
    """News item with optional retrieval metadata (legacy fields remain valid)."""

    title: str = ""
    display_title: str = ""
    summary: str = ""
    source: str = ""
    url: str = ""
    tag: str = "NEW"
    section: str = ""
    image_brief: str = ""
    published_at: str = ""
    event_key: str = ""
    update: str = ""
    locations: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Story":
        d = _object(d, "story")
        title = text(d.get("title", ""), "story.title", 240)
        display = text(d.get("display_title", ""), "story.display_title", MAX_HEADLINE)
        # Only the separate display label may be abbreviated; title remains intact.
        display = display or clip(title, 30)
        tag = text(d.get("tag", "NEW"), "story.tag", 10).upper()
        if tag not in ("NEW", "UPDATE"):
            raise ValueError("story.tag 必须为 NEW 或 UPDATE")
        locations = _list(d.get("locations", []), "story.locations", 6)
        return cls(
            title=title,
            display_title=display,
            summary=text(d.get("summary", ""), "story.summary", MAX_SUMMARY),
            source=text(d.get("source", ""), "story.source", 100),
            url=_url(d.get("url", ""), "story.url"),
            tag=tag,
            section=text(d.get("section", ""), "story.section", 20),
            image_brief=text(d.get("image_brief", ""), "story.image_brief", 300),
            published_at=_published_at(d.get("published_at", ""), "story.published_at"),
            event_key=text(d.get("event_key", ""), "story.event_key", 200),
            update=text(d.get("update", ""), "story.update", MAX_SUMMARY),
            locations=[text(v, "story.locations[]", 100) for v in locations],
        )

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


def _history_item(item: Any) -> dict[str, Any]:
    # Preserve old string entries on load; publication validation will reject
    # missing year/source/date instead of publishing unsupported history facts.
    if isinstance(item, str):
        return {"year": "", "event": text(item, "on_this_day.event", 160)}
    item = _object(item, "on_this_day[]")
    out = dict(item)
    out["event"] = text(item.get("event", ""), "on_this_day.event", 160)
    year = item.get("year", "")
    if year != "" and (type(year) is not int or not 1 <= year <= 9999):
        raise ValueError("on_this_day.year 必须是 1..9999 的整数年份")
    out["year"] = year
    out["source"] = text(item.get("source", ""), "on_this_day.source", 100)
    out["url"] = _url(item.get("url", ""), "on_this_day.url")
    for key, maximum in (("month", 12), ("day", 31)):
        value = item.get(key)
        if value is not None and (type(value) is not int or not 1 <= value <= maximum):
            raise ValueError(f"on_this_day.{key} 必须是有效整数")
    if "date" in item:
        out["date"] = text(item["date"], "on_this_day.date", 10)
        try:
            Date.fromisoformat(out["date"])
        except ValueError as exc:
            raise ValueError("on_this_day.date 必须是 YYYY-MM-DD") from exc
    return out


def _map_item(item: Any) -> dict[str, Any]:
    if isinstance(item, str):
        return {"location": text(item, "world_map.location", 100), "label": ""}
    item = _object(item, "world_map[]")
    out = dict(item)
    out["location"] = text(item.get("location", item.get("name", "")), "world_map.location", 100)
    out["label"] = text(item.get("label", ""), "world_map.label", 120)
    if not out["location"]:
        raise ValueError("world_map.location 不得为空")
    for key, low, high in (("latitude", -90, 90), ("longitude", -180, 180)):
        value = item.get(key)
        if value is not None and (type(value) not in (int, float) or not low <= value <= high):
            raise ValueError(f"world_map.{key} 超出有效范围")
    return out


@dataclass
class DailyBrief:
    """Structured research result; metadata is stored but never trusted as branding/date."""

    edition: str = ""
    date: str = ""
    weekday: str = ""
    cover_headline: str = ""
    cover_summary: str = ""
    cover_image_brief: str = ""
    world_in_60_seconds: list[str] = field(default_factory=list)
    stories: list[Story] = field(default_factory=list)
    why_it_matters: str = ""
    local_lens: str = ""
    watch_next: str = ""
    on_this_day: list[dict[str, Any]] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    fact_check_notes: str = ""
    world_map: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict[str, Any], fallback_edition: str = "") -> "DailyBrief":
        d = _object(d, "brief")
        cover = _object(d.get("cover", {}), "cover")
        stories_raw = d.get("top_stories", d.get("stories", []))
        stories = [Story.from_dict(v) for v in _list(stories_raw, "top_stories", 8)]
        shorts = []
        for item in _list(d.get("world_in_60_seconds", []), "world_in_60_seconds", 6):
            if isinstance(item, dict):
                item = item.get("text", "")
            shorts.append(text(item, "world_in_60_seconds[]", MAX_SHORT))
        sources = [_url(v, "sources[]") for v in _list(d.get("sources", []), "sources", 30)]
        for story in stories:
            if story.url and story.url not in sources:
                sources.append(story.url)
        world_map = [_map_item(v) for v in _list(d.get("world_map", d.get("locations", [])), "world_map", 10)]
        return cls(
            edition=text(d.get("edition", "") or fallback_edition, "edition", 120),
            date=text(d.get("date", ""), "date", 10),
            weekday=text(d.get("weekday", ""), "weekday", 10),
            cover_headline=text(cover.get("headline", ""), "cover.headline", MAX_HEADLINE),
            cover_summary=text(cover.get("summary", ""), "cover.summary", MAX_SUMMARY),
            cover_image_brief=text(cover.get("image_brief", ""), "cover.image_brief", 300),
            world_in_60_seconds=shorts,
            stories=stories,
            why_it_matters=text(d.get("why_it_matters", ""), "why_it_matters", MAX_SECTION),
            local_lens=text(d.get("local_lens", ""), "local_lens", MAX_SECTION),
            watch_next=text(d.get("watch_next", ""), "watch_next", MAX_SECTION),
            on_this_day=[_history_item(v) for v in _list(d.get("on_this_day", []), "on_this_day", 4)],
            sources=list(dict.fromkeys(v for v in sources if v)),
            fact_check_notes=text(d.get("fact_check_notes", ""), "fact_check_notes", 2000),
            world_map=world_map,
        )

    def validate_for_publication(self, publication_date: str) -> None:
        """Check history source/year/month/day against the program's trusted date.

        This checks structure/calendar consistency only, not historical truth;
        the research client must still retrieve and verify the cited page.
        """
        today = Date.fromisoformat(publication_date)
        for item in self.on_this_day:
            item = _history_item(item)
            if not item.get("event") or not item.get("source") or not item.get("url"):
                raise ValueError("历史上的今天必须包含 event/year/source/url 和核验后的月日")
            year = item.get("year")
            if type(year) is not int or not 1 <= year < today.year:
                raise ValueError("历史上的今天年份必须早于刊物年份")
            month, day = item.get("month"), item.get("day")
            if item.get("date"):
                historical = Date.fromisoformat(item["date"])
                if historical.year != year:
                    raise ValueError("历史条目的 date 与 year 不一致")
                if month is not None and month != historical.month:
                    raise ValueError("历史条目的 date 与 month 不一致")
                if day is not None and day != historical.day:
                    raise ValueError("历史条目的 date 与 day 不一致")
                month, day = historical.month, historical.day
            if (month, day) != (today.month, today.day):
                raise ValueError("历史上的今天月日与程序刊物日期不一致或缺失")
            Date(year, month, day)

    def to_dict(self) -> dict[str, Any]:
        return {
            "edition": self.edition, "date": self.date, "weekday": self.weekday,
            "cover": {"headline": self.cover_headline, "summary": self.cover_summary, "image_brief": self.cover_image_brief},
            "world_in_60_seconds": self.world_in_60_seconds,
            "top_stories": [s.to_dict() for s in self.stories],
            "why_it_matters": self.why_it_matters, "local_lens": self.local_lens,
            "watch_next": self.watch_next, "on_this_day": self.on_this_day,
            "sources": self.sources, "fact_check_notes": self.fact_check_notes,
            "world_map": self.world_map,
        }

    def source_names(self, limit: int = 6) -> str:
        names = list(dict.fromkeys(s.source for s in self.stories if s.source))
        return "、".join(names[:limit]) if names else "来源见研究归档"

    def history_titles(self) -> list[str]:
        return list(dict.fromkeys([v for v in [self.cover_headline, *(s.title for s in self.stories)] if v]))

    def required_strings(self, edition: str, date: str, handle: str) -> list[str]:
        return [v for v in [edition, date, handle, self.cover_headline, *(s.display_title or s.title for s in self.stories)] if v]


@dataclass
class RunRecord:
    """一次运行的元数据（保留初始字段以兼容存储/编排器）。"""

    run_id: str
    slot_id: str
    slot_name: str
    date: str
    mode: str
    status: str = "running"
    started_at: str = ""
    finished_at: str = ""
    research_model: str = ""
    image_model: str = ""
    image_size: str = ""
    image_bytes: int = 0
    image_file: str = ""
    prompt_chars: int = 0
    targets: list[str] = field(default_factory=list)
    send_result: dict[str, bool] = field(default_factory=dict)
    error: str = ""
    qa: dict[str, Any] = field(default_factory=dict)
    usage: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "RunRecord":
        allowed = cls.__dataclass_fields__
        values = {key: value for key, value in _object(d, "run").items() if key in allowed}
        for key in ("run_id", "slot_id", "slot_name", "date", "mode"):
            values.setdefault(key, "")
        values.setdefault("status", "failed")
        return cls(**values)
