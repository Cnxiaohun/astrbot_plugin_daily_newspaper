"""Read-only OneBot health snapshots, never chat history or delivery probes.

Callers must supply only currently configured target UMOs, never request targets.
A successful status/group query is not evidence of delivery or absence of a mute.
"""
from __future__ import annotations

import asyncio
import re

_ALLOWED_ACTIONS = frozenset({"get_status", "get_version_info", "get_login_info", "get_group_info"})
_CALL_TIMEOUT_SECONDS = 5
_GROUP = re.compile(r"[0-9]{1,20}\Z")
_UMO = re.compile(r"([^:\s]{1,128}):GroupMessage:([^:\s]{1,128})\Z")


def _safe_text(value, config: dict | None) -> str | None:
    if not isinstance(value, str):
        return None
    for section in ("api", "research"):
        settings = (config or {}).get(section) or {}
        secret = settings.get("api_key") if isinstance(settings, dict) else None
        if isinstance(secret, str) and secret:
            value = value.replace(secret, "[REDACTED]")
    value = re.sub(r"https?://\S+", "[URL]", value)
    value = re.sub(r"(?i)\b(?:sk-[a-z0-9_-]+|bearer\s+\S+|(?:token|password|secret|api_key)\s*[:=]\s*\S+)", "[REDACTED]", value)
    return re.sub(r"[\x00-\x1f\x7f]", "", value)[:120]


def _error(exc: Exception) -> str:
    # Exception bodies may contain entire OneBot responses, signed URLs or tokens.
    # Only recognize fixed labels; never echo an arbitrary class name or str(exc).
    name = type(exc).__name__
    if isinstance(exc, TimeoutError):
        return "TimeoutError: health query exceeded time limit"
    if name in {"ApiNotAvailable", "ActionFailed", "NetworkError"}:
        return f"{name}: health query failed"
    return "HealthQueryError: health query failed"


async def _query(bot, action: str, **params) -> dict:
    if action not in _ALLOWED_ACTIONS:
        raise ValueError("unsupported health action")
    try:
        async with asyncio.timeout(_CALL_TIMEOUT_SECONDS):
            result = await bot.call_action(action, **params)
        if not isinstance(result, dict):
            return {"error": "InvalidResponse: expected an object"}
        return {"value": result}
    except Exception as exc:
        return {"error": _error(exc)}


async def platform_health(context, targets: list[str], *, config: dict | None = None) -> dict:
    """Query only four read-only actions for distinct configured aiocqhttp targets.

    ``targets`` is a trusted snapshot of current configuration, not an API input.
    Cancellation propagates; each individual OneBot call has a five-second cap.
    No returned raw response, nickname, group name, token, or exception body leaks.
    """
    grouped: dict[str, list[str]] = {}
    invalid_targets = 0
    for target in targets:
        match = _UMO.fullmatch(target) if isinstance(target, str) else None
        if not match:
            invalid_targets += 1
            continue
        platform_id, session_id = match.groups()
        group_id = session_id.rsplit("_", 1)[-1]
        if not _GROUP.fullmatch(group_id) or int(group_id) <= 0:
            invalid_targets += 1
            continue
        group_id = str(int(group_id))
        groups = grouped.setdefault(platform_id, [])
        if group_id not in groups:
            groups.append(group_id)
    platforms = []
    for platform_id, group_ids in grouped.items():
        item = {"platform_id": platform_id, "matched": False, "groups": []}
        platforms.append(item)
        try:
            platform = context.get_platform_inst(platform_id)
            if platform is None:
                item["error"] = "PlatformUnavailable: no matching platform instance"
                continue
            item["matched"] = True
            if platform.meta().name != "aiocqhttp":
                item["error"] = "UnsupportedPlatform: health queries require aiocqhttp"
                continue
            bot = platform.bot
        except Exception as exc:
            item["error"] = _error(exc)
            continue
        status = await _query(bot, "get_status")
        if "error" in status:
            item["status"] = {"error": status["error"]}
        else:
            item["status"] = {key: value for key in ("online", "good")
                              if type(value := status["value"].get(key)) is bool}
        version = await _query(bot, "get_version_info")
        if "error" in version:
            item["version"] = {"error": version["error"]}
        else:
            item["version"] = {key: text for key in ("app_name", "app_version", "protocol_version")
                               if (text := _safe_text(version["value"].get(key), config)) is not None}
        login = await _query(bot, "get_login_info")
        if "error" in login:
            item["login"] = {"error": login["error"]}
        else:
            user_id = login["value"].get("user_id")
            if type(user_id) in (int, str) and _GROUP.fullmatch(str(user_id)) and int(user_id) > 0:
                item["login"] = {"user_id": str(user_id)}
            else:
                item["login"] = {"error": "InvalidResponse: invalid self user_id"}
        for group_id in group_ids:
            info = await _query(bot, "get_group_info", group_id=int(group_id))
            group = {"group_id": group_id, "query_ok": False}
            if "error" in info:
                group["error"] = info["error"]
            else:
                returned_id = info["value"].get("group_id")
                if type(returned_id) in (int, str) and str(returned_id) == group_id:
                    group["query_ok"] = True
                else:
                    group["error"] = "InvalidResponse: group_id mismatch"
            item["groups"].append(group)
    return {"platforms": platforms, "invalid_targets": invalid_targets,
            "note": "Health queries do not prove message delivery, group membership or mute state."}
