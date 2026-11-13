"""Short persona-toned heads-up before a long generation task.

The announcement is best-effort: any failure falls back to a plain sentence and
never blocks or fails the actual report generation.
"""
from __future__ import annotations

import asyncio
import inspect
import logging
import re
from typing import Any

DEFAULT_TEXTS = {
    "scheduled": "开始制作 {{date}}（{{weekday}}）{{time}} 的{{slot_name}}，稍后把图片端上来～",
    "manual": "马上开始制作 {{date}}（{{weekday}}）{{time}} 的{{slot_name}}，完成后把图片发到这里～",
    "force": "收到强制重做，马上重新生成 {{date}}（{{weekday}}）{{time}} 的{{slot_name}}，会重新检索并排版，请稍等～",
    "reuse": "{{date}}（{{weekday}}）的{{slot_name}}今天已经做好啦，直接把这一版发过来～",
}

_PLACEHOLDER_RE = re.compile(r"\{\{\s*([a-zA-Z_][\w]*)\s*\}\}")


def render(template: str, ctx: dict[str, Any]) -> str:
    def sub(match: re.Match[str]) -> str:
        return str(ctx.get(match.group(1), ""))
    return _PLACEHOLDER_RE.sub(sub, template)


async def _persona_rewrite(context, umo: str, base_text: str, timeout: int,
                           logger: logging.Logger) -> str:
    """Ask the session chat provider to rephrase one line in its active persona."""
    try:
        async with asyncio.timeout(timeout):
            persona_prompt = ""
            manager = getattr(context, "persona_manager", None)
            if manager is not None:
                persona = await manager.get_default_persona_v3(umo=umo)
                if isinstance(persona, dict):
                    persona_prompt = str(persona.get("prompt") or "")
            provider = await context.get_using_provider_async(umo=umo)
            if provider is None:
                return base_text
            instruction = (
                "请完全保持你当前的人格与说话口吻，把下面这句通知改写成 1-2 句"
                "简短、自然、适合直接发在群里的消息。必须保留其中的日期、星期、"
                "时间和时段信息，不要添加任何新事实、数字或来源，不要提问，不要解释，"
                "不要加引号、标题或前后缀，只输出改写后的消息正文。\n"
                f"原句：{base_text}"
            )
            response = await provider.text_chat(
                prompt=instruction,
                system_prompt=persona_prompt or None,
            )
            text = str(getattr(response, "completion_text", "") or "").strip()
            if text and len(text) <= 200:
                return text
            return base_text
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - never block generation
        logger.warning("每日图报人设告知改写失败，改用原句：%s", type(exc).__name__)
        return base_text


async def announce(
    context,
    umos: list[str],
    *,
    settings: dict,
    slot_name: str,
    date: str,
    weekday: str,
    time: str,
    edition: str,
    action: str,
    reuse: bool,
    sender,
    logger: logging.Logger | None = None,
) -> None:
    """Send a short heads-up to each target before generation (or before reuse)."""
    log = logger or logging.getLogger(__name__)
    try:
        if not settings.get("enabled", True):
            return
        key = "reuse" if reuse else (action if action in DEFAULT_TEXTS else "scheduled")
        template = str(settings.get(f"{key}_text") or "").strip() or DEFAULT_TEXTS[key]
        ctx = {"slot_name": slot_name, "date": date, "weekday": weekday,
               "time": time, "edition": edition}
        base = render(template, ctx)
        if not base.strip() or not umos:
            return
        use_persona = bool(settings.get("use_persona", True))
        timeout = settings.get("timeout_seconds", 25)
        try:
            timeout = max(1, min(int(timeout), 120))
        except (TypeError, ValueError):
            timeout = 25
        for umo in umos:
            text = base
            if use_persona:
                text = await _persona_rewrite(context, umo, base, timeout, log)
            try:
                value = sender(context, umo, text)
                if inspect.isawaitable(value):
                    await value
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("每日图报告知发送失败：%s", type(exc).__name__)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        log.warning("每日图报告知流程失败（不影响生成）：%s", type(exc).__name__)
