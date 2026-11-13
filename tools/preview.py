"""Explicit opt-in paid API smoke test; never imports AstrBot or sends to a group.

Run from plugin root: DAILY_REPORT_API_KEY=... python tools/preview.py --slot morning
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.service import ReportService  # noqa: E402


def schema_defaults(items):
    return {k: schema_defaults(v["items"]) if v["type"] == "object" else v.get("default")
            for k, v in items.items()}


async def main():
    parser = argparse.ArgumentParser(description="真实API预览（会产生模型用量，不发送任何群消息）")
    parser.add_argument("--slot", choices=["morning", "noon", "evening"], default="morning")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--output", default="preview-output")
    parser.add_argument("--qa", action="store_true")
    parser.add_argument("--at", default="", help="模拟计划时刻 HH:MM（用于测试不同时段窗口，实际日期仍取今天）")
    parser.add_argument("--image-model", default="", help="覆盖主生图模型；设置后不使用备用模型")
    args = parser.parse_args()
    key = os.environ.get("DAILY_REPORT_API_KEY", "")
    if not key:
        parser.error("请通过 DAILY_REPORT_API_KEY 环境变量提供key；不要把key写入源码或命令参数")
    cfg = schema_defaults(json.loads((Path(__file__).parents[1] / "_conf_schema.json").read_text(encoding="utf-8")))
    cfg["api"]["base_url"] = args.base_url
    cfg["api"]["api_key"] = key
    cfg["qa"]["enabled"] = args.qa
    if args.image_model:
        cfg["api"]["image_model"] = args.image_model
        cfg["api"]["fallback_image_models"] = []
    target = "preview-only:GroupMessage:local"

    def clock():
        now = datetime.now(ZoneInfo(cfg["timezone"]))
        if args.at:
            hour, minute = args.at.split(":")
            now = now.replace(hour=int(hour), minute=int(minute), second=0, microsecond=0)
        return now

    async def no_send(context, umo, path):
        if umo != target:
            raise AssertionError("预览脚本禁止群发送")
        print("LOCAL_PREVIEW", str(path.resolve()))
        return True
    service = ReportService(None, cfg, Path(args.output), clock=clock, sender=no_send)
    try:
        await service.start(schedule=False)
        record = await service.run(args.slot, preview_target=target)
        print(json.dumps({k: record.get(k) for k in ("run_id", "status", "stage", "error", "image_model", "image_size", "research_usage", "image_usage")}, ensure_ascii=False, indent=2))
        if record.get("status") != "success":
            raise SystemExit(1)
    finally:
        await service.close()


if __name__ == "__main__":
    asyncio.run(main())
