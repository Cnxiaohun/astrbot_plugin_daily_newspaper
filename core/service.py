"""Research -> explicit copy -> image -> optional QA -> image-only delivery."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import inspect
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

import httpx

from .announce import announce
from .config import get_slot, load_slots, previous_edition_time, validate_config, validate_targets
from .delivery import send_alert, send_image
from .http_utils import AmbiguousRequestError, ConnectionFailure
from .image_client import ImageClient
from .prompt_builder import PromptBuilder, now_parts
from .qa import check_image
from .research import ResearchClient
from .scheduler import is_due
from .store import Store


def safe_error(exc: BaseException, config: dict) -> str:
    """Never expose API keys, signed image URLs or provider response bodies."""
    text = f"{type(exc).__name__}: {exc}"
    for section in ("api", "research"):
        secret = (config.get(section) or {}).get("api_key", "")
        if secret:
            text = text.replace(secret, "[REDACTED]")
    import re
    text = re.sub(r"https?://\S+", "[URL]", text)
    text = re.sub(r"\bsk-[A-Za-z0-9_-]+", "[REDACTED]", text)
    return text[:400]


def research_window(slot_id: str, now: datetime, config: dict) -> datetime:
    """Start of the window covered by this edition: the previous edition's time.

    Slots are dynamic, so the predecessor is whichever enabled slot is scheduled
    just before this one by time-of-day (wrapping to yesterday). With a single
    slot the window is the previous 24 hours. Overlap is resolved via history.
    """
    slots = load_slots(config)
    slot = next((x for x in slots if x.id == slot_id), None)
    if slot is None:
        raise ValueError(f"未知时段 {slot_id}")
    value, _ = previous_edition_time(slots, slot, now)
    return value


class ReportService:
    def __init__(self, context, config: dict, root: Path, *, logger: Any,
                 clock: Callable | None = None, sender=None, alerter=None,
                 research_factory=ResearchClient, image_factory=ImageClient,
                 qa_checker=check_image, announcer=announce):
        # 日志器必须由调用方传入 astrbot.api 的 logger；插件内不使用内置 logging。
        if logger is None:
            raise ValueError("ReportService 需要调用方传入 logger（AstrBot 内请用 astrbot.api.logger）")
        validate_config(config)
        self.context = context
        self.config = config
        self.store = Store(root)
        self.logger = logger
        self.clock = clock or (lambda: datetime.now(ZoneInfo(self.config.get("timezone", "Asia/Shanghai"))))
        self.sender = sender or send_image
        self.alerter = alerter or send_alert
        self.research_factory = research_factory
        self.image_factory = image_factory
        self.qa_checker = qa_checker
        self.announcer = announcer
        self.slot_locks: dict[str, asyncio.Lock] = {}
        self.capacity = asyncio.Semaphore((config.get("limits") or {}).get("max_concurrent_generations", 1))
        self.quota_lock = asyncio.Lock()
        self.tasks: set[asyncio.Task] = set()
        self._scheduled: set[str] = set()
        self._scheduler_task = None
        self._closed = False
        self._cleanup_date = ""
        self.client = httpx.AsyncClient(follow_redirects=False, trust_env=False)

    async def start(self, *, schedule: bool = True) -> None:
        self.store.acquire()
        await self.store.recover()
        if schedule:
            self._scheduler_task = self.spawn(self._scheduler(), "daily-report-scheduler")

    def _lock_for(self, slot_id: str) -> asyncio.Lock:
        """Locks are created on demand because the slot set is dynamic."""
        return self.slot_locks.setdefault(slot_id, asyncio.Lock())

    def spawn(self, coro, name: str) -> asyncio.Task:
        if self._closed:
            coro.close()
            raise RuntimeError("插件已停止")
        task = asyncio.create_task(coro, name=name)
        self.tasks.add(task)
        task.add_done_callback(self._task_done)
        return task

    def _task_done(self, task: asyncio.Task) -> None:
        self.tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            self.logger.error("每日报纸后台任务异常：%s", safe_error(task.exception(), self.config))

    async def close(self) -> None:
        self._closed = True
        pending = [t for t in self.tasks if t is not asyncio.current_task()]
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        await self.client.aclose()
        self.store.release()

    async def _scheduler(self) -> None:
        # Allow platform adapters to finish starting; no replacement servers.
        await asyncio.sleep(10)
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.logger.error("每日报纸调度检查失败：%s", safe_error(exc, self.config))
            await asyncio.sleep(25)

    async def tick(self) -> None:
        now = self.clock()
        limits = self.config.get("limits") or {}
        if self._cleanup_date != now.date().isoformat():
            await self.store.cleanup(now, limits.get("retention_days", 7))
            self._cleanup_date = now.date().isoformat()
        if not self.config.get("enabled", True):
            return
        for slot in load_slots(self.config):
            if not slot.enabled or not slot.targets or slot.id in self._scheduled:
                continue
            if not is_due(slot, now, limits.get("catchup_window_minutes", 30)):
                continue
            # No automatic rerun after an ambiguous paid request, cancellation or failure.
            if self.store.latest(slot.id, now.date().isoformat()) is not None:
                continue
            self._scheduled.add(slot.id)
            self.spawn(self._scheduled_run(slot.id), f"daily-report-{slot.id}")

    async def _scheduled_run(self, slot_id: str) -> None:
        try:
            await self.run(slot_id)
        finally:
            self._scheduled.discard(slot_id)

    async def _new_record(self, slot, now: datetime, targets: list[str], preview: bool, cfg: dict) -> dict:
        async with self.quota_lock:
            maximum = (cfg.get("limits") or {}).get("max_runs_per_day", 12)
            if maximum and self.store.count_today(now.date().isoformat()) >= maximum:
                raise ValueError("已达到每日运行次数上限（预览也计入），请调整 limits 或次日重试")
            return await self.store.create(slot, now, targets, preview)

    async def _announce(self, cfg: dict, umos: list[str], slot, now: datetime,
                        date: str, action: str, reuse: bool) -> None:
        """Best-effort persona-toned heads-up; never blocks/fails generation."""
        try:
            parts = now_parts(cfg.get("timezone", "Asia/Shanghai"), now)
            value = self.announcer(
                self.context, list(dict.fromkeys(umos)),
                settings=(cfg.get("announce") or {}),
                slot_name=slot.name, date=date,
                weekday=parts["weekday"], time=parts["time"],
                edition=(cfg.get("brand") or {}).get("edition", ""),
                action=action, reuse=reuse,
                sender=self.alerter, logger=self.logger,
            )
            if inspect.isawaitable(value):
                await value
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("每日报纸告知失败（不影响生成）：%s", type(exc).__name__)

    async def run(self, slot_id: str, *, preview_target: str = "", force: bool = False,
                  action: str = "scheduled") -> dict:
        if self._closed:
            raise RuntimeError("插件已停止")
        slot = get_slot(self.config, slot_id)
        if slot is None:
            raise ValueError("未知时段；使用 早报、午报、晚报")
        lock = self._lock_for(slot.id)
        if lock.locked():
            raise ValueError(f"{slot.name}已有任务在运行，请等结束后再试")
        async with lock, self.capacity:
            # Take one immutable snapshot and clock for this edition, even across midnight.
            cfg = copy.deepcopy(dict(self.config))
            validate_config(cfg)
            slot = get_slot(cfg, slot.id)
            preview = bool(preview_target)
            if not preview and (not cfg.get("enabled", True) or not slot.enabled):
                raise ValueError("插件或该时段已停用，请先启用或使用 测试")
            targets = validate_targets([preview_target] if preview else slot.targets, group_only=not preview)
            if not targets:
                raise ValueError("该时段没有目标群，请先在群内 /日报绑定 早报 或在 WebUI 配置")
            now = self.clock()
            date = now.date().isoformat()
            # A same-day edition is generated at most once. For previews, forward
            # the already generated formal edition to the current chat; for formal
            # runs, only deliver the saved image to any targets that still need it.
            if not preview and not force:
                prior = self.store.receipts(slot.id, date)
                if all(prior.get(t) == "sent" for t in targets):
                    return {"status": "skipped", "message": "今日该时段已向全部目标提交，未重复生成或发送"}
                if any(prior.get(t) in {"sending", "uncertain"} for t in targets):
                    raise ValueError("存在结果不明的发送记录，自动重发可能重复；请查看历史并显式 重发/强制")
            latest = self.store.latest(slot.id, date, preview=False)
            reuse = None
            if not force and latest and latest.get("image_file"):
                saved = self.store.run_dir(latest["run_id"]) / latest["image_file"]
                if saved.is_file():
                    reuse = (latest, saved)
            if not preview and not force and latest and reuse is None:
                raise ValueError("今日该时段已尝试但未生成图片；请用 /日报 强制 早报 明确重新调用API")
            announce_umos = [preview_target] if preview else targets
            await self._announce(cfg, announce_umos, slot, now, date, action,
                                 reuse=reuse is not None)
            if reuse is not None:
                latest, saved = reuse
                if preview:
                    matched = await self.sender(self.context, preview_target, saved)
                    return {"status": "success" if matched else "partial",
                            "message": "已转发今日已生成版本", "run_id": latest["run_id"]}
                return await self._deliver(latest, targets, cfg, force=force)
            record = await self._new_record(slot, now, targets, preview, cfg)
            try:
                timeout = (cfg.get("limits") or {}).get("stale_run_minutes", 15) * 60
                async with asyncio.timeout(timeout):
                    return await self._pipeline(slot, now, cfg, record, force)
            except asyncio.CancelledError:
                record.update(status="interrupted", error="任务被卸载/重载/关闭中断；不要盲目重复付费调用")
                await self.store.save(record)
                raise
            except Exception as exc:
                record.update(status="failed", error=safe_error(exc, cfg), finished_at=self.clock().isoformat())
                await self.store.save(record)
                self.logger.error("图报 %s 失败：%s", record["run_id"], record["error"])
                await self._notify_failure(record, cfg, targets, preview, preview_target)
                await self._alert(record, cfg)
                return record

    async def _notify_failure(self, record: dict, cfg: dict, targets: list[str],
                              preview: bool, preview_target: str) -> None:
        """Tell the chat that this edition failed, so a silent announce is not the last word."""
        settings = cfg.get("announce") or {}
        if not settings.get("notify_on_failure", True):
            return
        umos = [preview_target] if preview else list(targets)
        if not umos:
            return
        reason = str(record.get("error") or "未知错误")
        reason = reason.split("：", 1)[-1] if "：" in reason else reason
        stage = record.get("stage") or ""
        stage_label = {"research": "检索资讯", "image": "生成图片",
                       "qa": "图片检查", "delivery": "发送"}.get(stage, stage or "生成")
        text = (f"{record.get('slot_name', '图报')}没做出来（{stage_label}阶段失败）："
                f"{reason[:120]}\n稍后可以发 /日报 强制 {record.get('slot_name', '')} 重试。")
        for umo in dict.fromkeys(umos):
            try:
                value = self.alerter(self.context, umo, text)
                if inspect.isawaitable(value):
                    await value
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.logger.warning("每日报纸失败通知发送失败：%s", type(exc).__name__)

    async def _pipeline(self, slot, now: datetime, cfg: dict, record: dict, force: bool) -> dict:
        api = cfg.get("api") or {}
        research = cfg.get("research") or {}
        brand = cfg.get("brand") or {}
        edition = brand.get("edition", "THE WORLD TODAY｜寰球三刻")
        handle = brand.get("handle", "@吴某的乐子群")
        size = slot.size or api.get("size", "1024x1536")
        parts = {"window_start": research_window(slot.id, now, cfg).isoformat(), "cutoff": now.isoformat()}
        builder = PromptBuilder(edition, handle, cfg.get("timezone", "Asia/Shanghai"), now=now, parts=parts)
        folder = self.store.run_dir(record["run_id"])
        brief = None
        if slot.mode == "agent":
            if not research.get("enabled", True):
                raise ValueError("该时段为 agent 模式，但 research.enabled 已关闭；不允许使用模型记忆生成新闻")
            record["stage"] = "research"
            await self.store.save(record)
            prior_keys, prior_titles = self.store.same_day_events(now.date().isoformat())
            prompt = builder.build_research_prompt(slot, self.store.history(now), size)
            await asyncio.to_thread((folder / "research_prompt.txt").write_text, prompt, encoding="utf-8")
            # Research is retried once on transport-level failure (timeout/connection):
            # a lost research call only wastes tokens and never sends anything, unlike
            # an image request. Content/validation failures are NOT retried here.
            attempts = 2 if research.get("retry_on_timeout", True) else 1
            evidence_attempts: list[dict] = []
            for attempt in range(1, attempts + 1):
                researcher = self.research_factory(research, api, client=self.client)
                try:
                    brief, usage = await researcher.research(
                        prompt, now.date().isoformat(), edition,
                        forbidden_event_keys=prior_keys, forbidden_titles=prior_titles,
                    )
                    record["research_model"] = research.get("model", "codex/gpt-6.1-sol")
                    record["research_usage"] = usage
                    await self.store.artifact_json(record, "brief.json", brief.to_dict())
                    break
                except (AmbiguousRequestError, ConnectionFailure) as exc:
                    if attempt >= attempts:
                        raise type(exc)(f"{exc}（研究已重试 {attempt} 次仍失败）") from None
                    record["stage"] = "research-retry"
                    await self.store.save(record)
                    self.logger.warning("研究请求中断，15秒后重试：%s", type(exc).__name__)
                    await asyncio.sleep(15)
                finally:
                    evidence = getattr(researcher, "last_evidence", {})
                    if evidence:
                        evidence_attempts.append({"attempt": attempt, **evidence})
                    await researcher.close()
            if evidence_attempts:
                await self.store.artifact_json(
                    record, "evidence.json",
                    evidence_attempts[0] if len(evidence_attempts) == 1 else {"attempts": evidence_attempts},
                )
        prompt = builder.build_image_prompt(slot, brief, size)
        if not prompt.strip():
            raise ValueError("最终图片提示词为空")
        await asyncio.to_thread((folder / "image_prompt.txt").write_text, prompt, encoding="utf-8")
        record.update(stage="image", prompt_chars=len(prompt))
        await self.store.save(record)
        generator = self.image_factory(api, client=self.client)
        try:
            result = await generator.generate(prompt, slot.model, size, slot.quality)
            qa = cfg.get("qa") or {}
            if qa.get("enabled", False):
                required = brief.required_strings(edition, now.date().isoformat(), handle) if brief else [edition, now.date().isoformat(), handle]
                for attempt in range(qa.get("retry_on_fail", 1) + 1):
                    record["stage"] = "qa"
                    await self.store.save(record)
                    qa_settings = research | {k: v for k, v in qa.items() if v != ""}
                    outcome = await self.qa_checker(qa_settings, api, result, required, client=self.client)
                    record["qa"] = outcome
                    await self.store.artifact_json(record, "qa.json", outcome)
                    if outcome.get("pass") is True:
                        break
                    if attempt >= qa.get("retry_on_fail", 1):
                        raise ValueError("图片质量检查未通过，已停止群推送；请查看 qa.json 后人工处理")
                    result = await generator.generate(prompt + "\n请重新生成，严格修正缺失或错误文字：" + str(outcome.get("missing", []))[:500], slot.model, size, slot.quality)
        finally:
            await generator.close()
        ext = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}.get(result.mime)
        if ext is None:
            raise ValueError("图片格式不支持")
        filename = "image" + ext
        await asyncio.to_thread((folder / filename).write_bytes, result.data)
        record.update(image_file=filename, image_size=f"{result.width}x{result.height}",
                      image_model=result.model, image_bytes=len(result.data),
                      image_sha256=hashlib.sha256(result.data).hexdigest(), image_usage=result.usage)
        await self.store.save(record)
        return await self._deliver(record, record["targets"], cfg, force=force)

    async def _deliver(self, record: dict, targets: list[str], cfg: dict, *, force: bool) -> dict:
        image = self.store.run_dir(record["run_id"]) / record["image_file"]
        if image.name != record["image_file"] or not image.is_file():
            raise ValueError("历史图片不存在或路径无效，无法重发")
        record.update(stage="delivery", status="running", targets=list(dict.fromkeys(record.get("targets", []) + targets)))
        prior = {} if record.get("preview") else self.store.receipts(record["slot_id"], record["date"])
        for target in targets:
            if not record.get("preview"):
                current_slot = get_slot(self.config, record["slot_id"])
                if (current_slot is None or not self.config.get("enabled", True)
                        or not current_slot.enabled or target not in current_slot.targets):
                    record["receipts"][target] = {"status": "revoked", "at": self.clock().isoformat()}
                    await self.store.save(record)
                    continue
            state = prior.get(target)
            # A successful receipt is final: even an explicit force/retry must
            # never send the same edition to the same group twice.
            if state == "sent":
                continue
            if not force and state in {"sending", "uncertain"}:
                continue
            record["receipts"][target] = {"status": "sending", "at": self.clock().isoformat()}
            await self.store.save(record)  # persist BEFORE the side effect
            try:
                matched = await self.sender(self.context, target, image)
            except asyncio.CancelledError:
                record["receipts"][target]["status"] = "uncertain"
                await self.store.save(record)
                raise
            except Exception as exc:
                record["receipts"][target].update(status="uncertain", error=safe_error(exc, cfg))
            else:
                record["receipts"][target]["status"] = "sent" if matched is True else "unmatched"
            await self.store.save(record)
        combined = self.store.receipts(record["slot_id"], record["date"]) if not record.get("preview") else {
            t: v.get("status") for t, v in record["receipts"].items()}
        success = all(combined.get(t) == "sent" for t in targets)
        record.update(status="success" if success else "partial", stage="finished", finished_at=self.clock().isoformat())
        if not success:
            record["error"] = "部分平台未匹配或发送结果不明；不自动重发以避免重复。查看 receipts 后人工处理。"
        else:
            record["error"] = ""
        await self.store.save(record)
        if not success:
            await self._alert(record, cfg)
        self.logger.info("图报 %s %s（%s）", record["run_id"], record["status"], record.get("image_model", ""))
        return record

    async def resend(self, run_id: str, *, retry_uncertain: bool = False) -> dict:
        if not self.config.get("enabled", True):
            raise ValueError("插件停用中，不能重发")
        initial = self.store.get(run_id)
        if not initial or not initial.get("image_file"):
            raise ValueError("没有该运行的已生成图片")
        if initial.get("preview"):
            raise ValueError("预览不允许发布到生产群；请使用 立即 或 强制")
        slot = get_slot(self.config, initial["slot_id"])
        if slot is None:
            raise ValueError("未知时段")
        # Everything decision-related is re-read INSIDE the per-slot lock so two
        # concurrent resend commands cannot both act on stale "uncertain" state.
        async with self._lock_for(slot.id):
            if not self.config.get("enabled", True):
                raise ValueError("插件停用中，不能重发")
            record = self.store.get(run_id)
            if not record or not record.get("image_file"):
                raise ValueError("没有该运行的已生成图片")
            if record.get("preview"):
                raise ValueError("预览不允许发布到生产群；请使用 立即 或 强制")
            slot = get_slot(self.config, record["slot_id"])
            if slot is None or not slot.enabled:
                raise ValueError("该时段停用中，不能重发")
            # Only CURRENT authorized targets, never stale bindings from a previous edition.
            targets = [t for t in slot.targets if t in record.get("targets", [])]
            if not targets:
                raise ValueError("原目标已解绑或没有目标，拒绝重发")
            prior = self.store.receipts(slot.id, record["date"])
            targets = [t for t in targets if prior.get(t) != "sent"]
            if not targets:
                return {"status": "skipped", "message": "所有原目标已有成功记录，未重复发送"}
            if not retry_uncertain and any(prior.get(t) in {"sending", "uncertain"} for t in targets):
                raise ValueError("存在结果不明记录；确定接受可能重复，请 /日报 重发 run_id 确认")
            return await self._deliver(record, targets, copy.deepcopy(dict(self.config)), force=retry_uncertain)

    async def _alert(self, record: dict, cfg: dict) -> None:
        target = cfg.get("alert_umo", "")
        if not target:
            return
        try:
            text = f"每日报纸 {record['slot_name']} {record['run_id']}\n阶段：{record.get('stage')}\n{record.get('error', '')}"
            value = self.alerter(self.context, target, text)
            if inspect.isawaitable(value):
                await value
        except Exception as exc:
            self.logger.warning("每日报纸告警发送失败：%s", safe_error(exc, cfg))
