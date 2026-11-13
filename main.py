"""AstrBot entrypoint. All publication commands require AstrBot ADMIN permission."""
from __future__ import annotations

import asyncio
import difflib
import re
import shutil
import sys
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools

from .core.config import (
    MAX_SLOTS,
    SlotConfig,
    find_slot,
    get_slot,
    load_slots,
    normalize_schedules,
    validate_targets,
    write_slots,
)
from .core.scheduler import next_time
from .core.service import ReportService, safe_error

PLUGIN_NAME = "astrbot_plugin_daily_newspaper"
LEGACY_PLUGIN_NAME = "astrbot_plugin_daily_image_report"

# Every user-facing synonym maps to one canonical slot id.
SLOT_ALIASES: dict[str, str] = {
    "早": "morning", "早报": "morning", "早上": "morning", "morning": "morning", "m": "morning", "am": "morning",
    "午": "noon", "午报": "noon", "中午": "noon", "中": "noon", "noon": "noon", "n": "noon",
    "晚": "evening", "晚报": "evening", "晚上": "evening", "evening": "evening", "e": "evening", "pm": "evening",
}
SLOT_WORD = {"morning": "早报", "noon": "午报", "evening": "晚报"}
# Actions that must name a slot; anything else there is a typo, not a shortcut.
SLOT_ACTIONS = {"preview", "run", "force", "enable", "disable", "bind", "unbind"}
_TIME_TOKEN_RE = re.compile(r"(?:[01]\d|2[0-3]):[0-5]\d")

# Actions that legitimately carry free-form arguments instead of a slot token.
FLEX_ARG_ACTIONS = {"help", "status", "targets", "history", "resend", "list", "add", "time"}

ACTION_ALIASES: dict[str, str] = {
    "帮助": "help", "help": "help", "?": "help", "？": "help",
    "状态": "status", "status": "status",
    "测试": "preview", "预览": "preview", "test": "preview", "preview": "preview",
    "立即": "run", "执行": "run", "run": "run", "now": "run",
    "强制": "force", "重做": "force", "force": "force",
    "历史": "history", "history": "history", "log": "history",
    "重发": "resend", "resend": "resend",
    "启用": "enable", "enable": "enable",
    "停用": "disable", "disable": "disable",
    "绑定": "bind", "bind": "bind",
    "解绑": "unbind", "unbind": "unbind",
    "目标": "targets", "targets": "targets",
    "列表": "list", "清单": "list", "list": "list",
    "新增": "add", "添加": "add", "加": "add", "add": "add", "new": "add",
    "删除": "remove", "移除": "remove", "remove": "remove", "del": "remove",
    "时间": "time", "改时间": "time", "time": "time",
}

HELP = """每日报纸 · 用法
统一写法：/日报 <动作> [时段]，动作和时段顺序随意。

▌查看
  /日报                      本帮助
  /日报 列表                 当前有哪些时段（id、时间、模式、开关、群数）
  /日报 状态                 各时段开关、目标数、下次计划、上次结果
  /日报 历史 [1-20]          最近运行记录与失败原因
  /日报 目标                 当前群绑定了哪些时段

▌生成（时段用 /日报 列表 里看到的名字）
  /日报 测试 早报            只发到当前会话，不写正式记录
  /日报 立即 早报            发到已绑定群；当天已有图会直接复用
  /日报 早报                 等同「立即」
  /日报 强制 早报            重新检索并生图（会再次计费）

▌时段管理（不限三档，最多 12 档）
  /日报 新增 夜报 21:30      加一档（默认 agent 模式）
  /日报 删除 夜报            移除一档
  /日报 时间 早报 07:30      改时间
  /日报 启用 早报            或 /日报 停用 早报

▌目标群
  /日报 绑定 早报|全部       把当前群加入该时段
  /日报 解绑 早报|全部       从该时段移除当前群
  /日报 重发 <run_id> [确认] 复用旧图重发；「确认」允许重发结果不明的目标

▌说明
  时段可写名称或 id；内置三档还支持 早/午/晚、morning/noon/evening。
  旧写法 `/日报测试 早报`、`/日报绑定 全部` 仍可用。
  默认三档：早报 08:00、午报 12:30、晚报 19:00，都可改可删。
  定时推送只发图片；命令反馈和失败说明是文字。
  模式（agent/static）、研究/图片提示词、API Key 在 WebUI 配置。
"""

def _nearest(token: str, candidates: list[str]) -> str:
    match = difflib.get_close_matches(token, candidates, n=1, cutoff=0.4)
    return match[0] if match else ""


class DailyImageReport(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.service: ReportService | None = None
        self._command_lock = asyncio.Lock()
        self._active_commands: set[asyncio.Task] = set()

    async def initialize(self):
        # 旧版 schedules 是固定三档对象，这里无损转成动态数组并落盘。
        try:
            if normalize_schedules(self.config):
                await self._persist()
                logger.info("每日报纸：已将旧版三档配置迁移为动态时段列表（目标群与提示词原样保留）")
        except Exception as exc:  # noqa: BLE001 - never let migration kill the plugin
            logger.error("每日报纸：时段配置迁移失败，保持原状：%s", safe_error(exc, self.config))
        data = Path(StarTools.get_data_dir(PLUGIN_NAME))
        # 插件改过一次名，旧数据目录里有去重历史与发送收据，先搬过来再用。
        try:
            moved = self._migrate_legacy_data(data)
        except Exception as exc:  # noqa: BLE001 - 迁移失败不该拦住启动
            logger.warning("%s：旧数据目录迁移失败，将使用新目录：%s", CN_NEW, safe_error(exc, self.config))
            moved = False
        if moved:
            logger.info("%s：已把旧数据目录的历史与收据迁移到新目录", CN_NEW)
        self.service = ReportService(self.context, self.config, data, logger=logger)
        try:
            await self.service.start()
        except BaseException:
            await self.service.close()
            raise
        try:
            plan = " / ".join(f"{x.name} {x.time}" for x in self._slot_list())
        except Exception:  # noqa: BLE001
            plan = "配置待修正"
        logger.info("每日报纸 v0.2.0 已加载：%s；未绑定目标时不调用付费API", plan)

    async def terminate(self):
        current = asyncio.current_task()
        commands = [t for t in self._active_commands if t is not current]
        for task in commands:
            task.cancel()
        await asyncio.gather(*commands, return_exceptions=True)
        if self.service is not None:
            await self.service.close()
            self.service = None

    def _migrate_legacy_data(self, target: Path) -> bool:
        """Copy run history from the pre-rename data directory, once.

        Only runs when the new directory has no state yet and the legacy one
        still exists, so it can never overwrite current data.
        """
        if (target / "state.json").exists():
            return False
        legacy = target.parent / LEGACY_PLUGIN_NAME
        if legacy == target or not legacy.is_dir() or not (legacy / "state.json").exists():
            return False
        # The in-flight lock file must not be copied; everything else is history.
        for item in legacy.iterdir():
            if item.name in {".instance.lock"}:
                continue
            dest = target / item.name
            if item.is_dir():
                shutil.copytree(item, dest, dirs_exist_ok=True)
            else:
                shutil.copy2(item, dest)
        return True

    def _ready(self) -> ReportService:
        if self.service is None:
            raise ValueError("插件尚未初始化，请检查配置并重载插件")
        return self.service

    async def _persist(self):
        async_saver = getattr(self.config, "save_config_async", None)
        if async_saver is not None:
            await async_saver()
            return
        saver = getattr(self.config, "save_config", None)
        if saver is None:
            raise RuntimeError("当前 AstrBot 未提供配置保存接口")
        await asyncio.to_thread(saver)

    # ------------------------------------------------------------------ 解析

    @staticmethod
    def _args(event: AstrMessageEvent) -> list[str]:
        """Tokens after the command word.

        Handles `/日报 测试 早报`, `/日报测试 早报`, `日报绑定 全部` and the
        fallback form where AstrBot already stripped the slash.
        """
        parts = (event.message_str or "").strip().split()
        if not parts:
            return []
        head = parts[0].lstrip("/")
        if head.startswith("日报"):
            rest = head[2:]  # "测试", "绑定", ... or "" for bare 日报
            parts = ([rest] + parts[1:]) if rest else parts[1:]
        return [p for p in parts if p]

    def _parse(self, tokens: list[str]) -> tuple[str, str, list[str]]:
        """Return (action, slot_token, extras); order-insensitive, alias-aware.

        Both `/日报 测试 早报` and `/日报 早报 测试` parse the same way, and the
        legacy `/日报测试 早报` form works because the command word is stripped.
        """
        action = ""
        slot_token = ""
        extras: list[str] = []
        for token in tokens:
            key = token.strip().lower()
            mapped = ACTION_ALIASES.get(key)
            if mapped and not action:
                action = mapped
                continue
            if not slot_token and self._is_slot_token(token):
                slot_token = token
                continue
            extras.append(token)
        return action, slot_token, extras

    def _is_slot_token(self, token: str) -> bool:
        """True when the token names a configured slot (or means "every slot")."""
        if token in {"全部", "all", "所有"}:
            return True
        if token.lower() in SLOT_ALIASES:
            return True
        try:
            return find_slot(load_slots(self.config), token) is not None
        except Exception:  # noqa: BLE001 - invalid config surfaces on the action itself
            return False

    def _slot_list(self) -> list[SlotConfig]:
        return load_slots(self.config)

    def _slot_hint(self) -> str:
        slots = self._slot_list()
        return "、".join(f"{s.name}({s.time})" for s in slots) or "（当前没有时段）"

    def _slot_or_hint(self, token: str) -> SlotConfig:
        if not token:
            raise ValueError(f"请指定时段：{self._slot_hint()}")
        slot = find_slot(self._slot_list(), token)
        if slot is None:
            raise ValueError(f"未知时段「{token}」；可用：{self._slot_hint()}")
        return slot

    # ------------------------------------------------------------------ 主命令

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("日报", priority=sys.maxsize)
    async def report(self, event: AstrMessageEvent):
        """管理每日报纸：状态、测试、立即、强制、历史、重发、启停、绑定。"""
        event.stop_event()
        logger.info("每日报纸命令：%s", (event.message_str or "")[:200])
        task = asyncio.current_task()
        self._active_commands.add(task)
        try:
            tokens = self._args(event)
            action, slot_token, extras = self._parse(tokens)
            if not tokens or action == "help":
                yield event.plain_result(HELP)
                return
            if not action and slot_token:
                action = "run"  # `/日报 早报` means publish it now
            # Fail loudly on typos instead of silently publishing: an unrecognized
            # extra token must never be treated as "just run it".
            if action in SLOT_ACTIONS:
                if not slot_token:
                    if extras:
                        raise ValueError(f"未知时段「{extras[0]}」；可用：{self._slot_hint()}")
                    raise ValueError(f"请指定时段：{self._slot_hint()}"
                                   + (" 或 全部" if action in {"bind", "unbind"} else ""))
                if extras:
                    hint = _nearest(extras[0], list(ACTION_ALIASES))
                    extra = f"；是不是「{hint}」？" if hint else ""
                    raise ValueError(f"无法识别的参数「{extras[0]}」{extra}\n/日报 查看全部用法")
            elif action and action not in FLEX_ARG_ACTIONS and extras:
                raise ValueError(f"无法识别的参数「{extras[0]}」\n/日报 查看全部用法")
            service = self._ready()
            if action == "status":
                now = service.clock()
                lines = [f"每日报纸｜{self.config.get('timezone', 'Asia/Shanghai')}｜总开关：{self.config.get('enabled', True)}"]
                for slot in load_slots(self.config):
                    latest = next((r for r in service.store.recent(100) if r["slot_id"] == slot.id and not r.get("preview")), None)
                    last = f"{latest['status']} {latest['run_id']}" if latest else "尚无正式运行"
                    lines.append(f"{slot.name} {slot.time} {slot.mode} {'启用' if slot.enabled else '停用'}｜{len(slot.targets)}群\n下次计划 {next_time(slot, now):%m-%d %H:%M}（需启用且有目标）\n上次：{last}")
                yield event.plain_result("\n\n".join(lines))
            elif action in {"preview", "run", "force"}:
                slot = self._slot_or_hint(slot_token)
                preview = action == "preview"
                if not preview and not slot.targets:
                    raise ValueError("该时段没有目标群，请先 /日报 绑定 早报 或在 WebUI 配置")
                # Long generation must not occupy this event pipeline; run in background
                # so the bot can keep answering normal group messages.
                service.spawn(
                    service.run(slot.id,
                                preview_target=event.unified_msg_origin if preview else "",
                                force=action == "force",
                                action="force" if action == "force" else "manual"),
                    name=f"daily-report-command-{slot.id}",
                )
                if preview:
                    yield event.plain_result("已开始生成，通常需要 2-5 分钟；完成后会直接把图片发到本会话。")
                return
            elif action == "history":
                count = 6
                if extras:
                    try:
                        count = int(extras[0])
                    except ValueError:
                        raise ValueError("格式：/日报 历史 [1-20]") from None
                if not 1 <= count <= 20:
                    raise ValueError("历史数量须为 1-20")
                rows = service.store.recent(count)
                lines = [
                    f"{'预览' if r.get('preview') else '正式'} {r['slot_name']} {r['status']}\n"
                    f"{r['run_id']} {r.get('image_model', '')}\n{r.get('error', '')}"
                    for r in rows
                ]
                yield event.plain_result("\n\n".join(lines) or "暂无运行记录")
            elif action == "resend":
                if not extras:
                    raise ValueError("格式：/日报 重发 <run_id> [确认]")
                confirm = bool(extras[1:]) and extras[1] in {"确认", "confirm", "yes"}
                result = await service.resend(extras[0], retry_uncertain=confirm)
                if result.get("status") != "success":
                    yield event.plain_result(result.get("message") or result.get("error") or "重发未完成")
            elif action in {"enable", "disable"}:
                slot = self._slot_or_hint(slot_token)
                async with self._command_lock:
                    slots = self._slot_list()
                    for item in slots:
                        if item.id == slot.id:
                            item.enabled = action == "enable"
                    write_slots(self.config, slots)
                    await self._persist()
                word = "启用" if action == "enable" else "停用"
                yield event.plain_result(f"{slot.name}已{word}；正在执行的任务使用原配置快照，重载/停用插件可取消")
            elif action == "list":
                lines = [f"{i + 1}. {s.name}｜{s.id}｜{s.time}｜{s.mode}｜"
                         f"{'启用' if s.enabled else '停用'}｜{len(s.targets)}群"
                         for i, s in enumerate(self._slot_list())]
                yield event.plain_result("当前时段：\n" + "\n".join(lines))
            elif action == "add":
                yield event.plain_result(await self._add_slot(slot_token, extras))
            elif action == "remove":
                yield event.plain_result(await self._remove_slot(slot_token))
            elif action == "time":
                yield event.plain_result(await self._set_time(slot_token, extras))
            elif action in {"bind", "unbind"}:
                yield event.plain_result(await self._do_bind(event, action == "unbind", slot_token))
            elif action == "targets":
                yield event.plain_result(self._targets_text(event))
            else:
                hint = _nearest(tokens[0] if tokens else "", list(ACTION_ALIASES))
                extra = f"；是不是「{hint}」？" if hint else ""
                raise ValueError(f"未知操作「{tokens[0] if tokens else ''}」{extra}\n/日报 查看全部用法")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            yield event.plain_result("每日报纸：" + safe_error(exc, self.config))
        finally:
            self._active_commands.discard(task)

    @filter.regex(r"^/日报", priority=sys.maxsize)
    async def slash_fallback(self, event: AstrMessageEvent):
        """兼容未配置 slash wake_prefix 的群：手动把 /日报 交给统一解析器。"""
        msg = (event.message_str or "").strip()
        if not msg.startswith("/日报"):
            return
        if not event.is_admin():
            event.stop_event()
            yield event.plain_result("您没有权限使用每日报纸指令。")
            return
        logger.info("每日报纸 slash fallback：%s", msg[:200])
        event.message_str = msg[1:].strip()
        async for result in self.report(event):
            yield result

    # 旧命令名保留为别名入口，内部走同一套解析与实现。
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("日报绑定", priority=sys.maxsize)
    async def bind(self, event: AstrMessageEvent):
        """绑定当前 NapCat 群到一个或全部图报时段（等价 /日报 绑定）。"""
        event.stop_event()
        try:
            action, slot_token, _ = self._parse(self._args(event))
            yield event.plain_result(await self._do_bind(event, action == "unbind", slot_token))
        except Exception as exc:
            yield event.plain_result(safe_error(exc, self.config))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("日报解绑", priority=sys.maxsize)
    async def unbind(self, event: AstrMessageEvent):
        """解绑当前群，不影响其他群（等价 /日报 解绑）。"""
        event.stop_event()
        try:
            action, slot_token, _ = self._parse(self._args(event))
            yield event.plain_result(await self._do_bind(event, action != "bind", slot_token))
        except Exception as exc:
            yield event.plain_result(safe_error(exc, self.config))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("日报目标", priority=sys.maxsize)
    async def targets(self, event: AstrMessageEvent):
        """查询当前群已绑定的时段（等价 /日报 目标）。"""
        event.stop_event()
        try:
            self._ready()
            yield event.plain_result(self._targets_text(event))
        except Exception as exc:
            yield event.plain_result(safe_error(exc, self.config))

    # ------------------------------------------------------- 时段增删改

    @staticmethod
    def _slot_id_for(name: str, when: str, existing: list[str]) -> str:
        """Derive a readable, stable storage id from the name (or its time)."""
        ascii_part = re.sub(r"[^a-z0-9_]+", "_", name.strip().lower()).strip("_")
        base = ascii_part if re.fullmatch(r"[a-z][a-z0-9_]{1,23}", ascii_part or "") else f"slot{when.replace(':', '')}"
        candidate, index = base, 2
        while candidate in existing:
            suffix = f"_{index}"
            candidate = base[: 24 - len(suffix)] + suffix
            index += 1
        return candidate

    async def _add_slot(self, slot_token: str, extras: list[str]) -> str:
        usage = "格式：/日报 新增 <名称> <HH:MM>，例如 /日报 新增 夜报 21:30"
        # The name may already have been captured as a slot token when it collides
        # with an existing edition, so reassemble the raw argument list here.
        tokens = ([slot_token] if slot_token else []) + list(extras)
        when = next((t for t in tokens if _TIME_TOKEN_RE.fullmatch(t)), "")
        names = [t for t in tokens if t != when]
        if not when or len(names) != 1:
            raise ValueError(usage)
        name = names[0]
        async with self._command_lock:
            slots = self._slot_list()
            if len(slots) >= MAX_SLOTS:
                raise ValueError(f"最多 {MAX_SLOTS} 个时段")
            if find_slot(slots, name):
                raise ValueError(f"已存在时段「{name}」")
            existing = [x.id for x in slots]
            slot = SlotConfig(
                id=self._slot_id_for(name, when, existing), name=name, enabled=True, time=when,
                mode="agent", targets=[],
            )
            write_slots(self.config, slots + [slot])
            await self._persist()
        return (f"已新增 {slot.name}（{slot.time}，agent 模式）。"
                f"发 /日报 绑定 {slot.name} 选目标群；"
                "要改模式或提示词去 WebUI 的时段列表。")

    async def _remove_slot(self, token: str) -> str:
        slot = self._slot_or_hint(token)
        service = self._ready()
        if self._lock_busy(slot.id):
            raise ValueError(f"{slot.name}正在生成，先等它结束或删除该运行后再移除")
        async with self._command_lock:
            slots = self._slot_list()
            if len(slots) <= 1:
                raise ValueError("至少保留一个时段")
            kept = [x for x in slots if x.id != slot.id]
            write_slots(self.config, kept)
            await self._persist()
        service.slot_locks.pop(slot.id, None)
        service._scheduled.discard(slot.id)
        return f"已删除 {slot.name}（{slot.time}）。历史运行记录仍保留，只是不再定时。"

    async def _set_time(self, token: str, extras: list[str]) -> str:
        usage = "格式：/日报 时间 <名称> <HH:MM>"
        slot = self._slot_or_hint(token)
        when = next((t for t in extras if _TIME_TOKEN_RE.fullmatch(t)), "")
        if not when or len(extras) != 1:
            raise ValueError(usage)
        async with self._command_lock:
            slots = self._slot_list()
            for item in slots:
                if item.id == slot.id:
                    item.time = when
            write_slots(self.config, slots)
            await self._persist()
        return f"{slot.name} 已改到 {when}。"

    def _lock_busy(self, slot_id: str) -> bool:
        lock = self._ready().slot_locks.get(slot_id)
        return bool(lock and lock.locked())

    # ------------------------------------------------------------------ 绑定

    def _current_group_umo(self, event) -> str:
        if event.get_platform_name() != "aiocqhttp" or not event.get_group_id():
            raise ValueError("群绑定命令请在 NapCat / OneBot v11 群聊内使用")
        # Some AstrBot session isolation modes append user IDs. Build a GROUP route
        # using the platform INSTANCE ID and real group ID, never an individual UMO.
        umo = f"{event.get_platform_id()}:GroupMessage:{event.get_group_id()}"
        validate_targets([umo])
        return umo

    def _targets_text(self, event) -> str:
        if not event.get_group_id():
            raise ValueError("请在群聊中查询目标")
        umo = f"{event.get_platform_id()}:GroupMessage:{event.get_group_id()}"
        names = [s.name for s in load_slots(self.config) if umo in s.targets]
        return "本群图报：" + ("、".join(names) or "未绑定（发送 /日报 绑定 全部）")

    async def _do_bind(self, event, remove: bool, slot_token: str) -> str:
        self._ready()
        umo = self._current_group_umo(event)
        if not slot_token:
            raise ValueError(f"请指定时段：{self._slot_hint()} / 全部")
        if slot_token in {"全部", "all"}:
            slots = load_slots(self.config)
        else:
            slots = [self._slot_or_hint(slot_token)]
        async with self._command_lock:
            current = {x.id: x for x in self._slot_list()}
            for slot in slots:
                item = current[slot.id]
                targets = list(item.targets)
                if remove:
                    targets = [t for t in targets if t != umo]
                elif umo not in targets:
                    targets.append(umo)
                item.targets = validate_targets(targets)
            write_slots(self.config, list(current.values()))
            await self._persist()
        word = "解绑" if remove else "绑定"
        tail = "后续发送前也会重新核对绑定。" if remove else "正式推送只发送图片。"
        return f"已{word}：{'、'.join(s.name for s in slots)}。{tail}"
