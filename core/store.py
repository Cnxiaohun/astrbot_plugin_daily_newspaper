"""Atomic durable run records. Sent/uncertain receipts survive process restarts."""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

_RUN_ID = re.compile(r"^\d{8}-([a-z][a-z0-9_]{1,23})-[0-9a-f]{12}$")


def _fsync_dir(path: Path) -> None:
    """Make the rename durable across host crash on POSIX; best-effort elsewhere."""
    if os.name == "nt":
        return
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temp.open("x", encoding="utf-8") as f:
            os.chmod(temp, 0o600)
            json.dump(value, f, ensure_ascii=False, indent=2, allow_nan=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, path)
        _fsync_dir(path.parent)
    finally:
        if temp.exists():
            temp.unlink()


class Store:
    """Small JSON index and one directory per run; callers serialize pipelines."""

    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.runs = self.root / "runs"
        self.runs.mkdir(exist_ok=True)
        self.index_path = self.root / "state.json"
        self.lock = asyncio.Lock()
        self.index: dict[str, Any] = {"version": 1, "runs": {}}
        if self.index_path.exists():
            # A corrupt index must NOT silently reset deduplication history.
            self.index = json.loads(self.index_path.read_text(encoding="utf-8"))
            if self.index.get("version") != 1 or not isinstance(self.index.get("runs"), dict):
                raise ValueError("运行索引损坏；请先备份并修复 state.json，禁止静默重置去重状态")
        self._lease = None

    def acquire(self) -> None:
        """OS lock protects against overlapping plugin instances on one host."""
        if self._lease is not None:
            return
        f = (self.root / ".instance.lock").open("a+b")
        try:
            if os.name == "nt":
                import msvcrt
                f.seek(0)
                f.write(b"0")
                f.flush()
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            f.close()
            raise RuntimeError("同一数据目录已有每日报纸实例运行，拒绝重复启动") from exc
        self._lease = f

    def release(self) -> None:
        if self._lease is not None:
            self._lease.close()
            self._lease = None

    def run_dir(self, run_id: str) -> Path:
        if not _RUN_ID.fullmatch(run_id):
            raise ValueError("无效 run_id")
        path = (self.runs / run_id).resolve()
        if path.parent != self.runs.resolve():
            raise ValueError("运行路径越界")
        return path

    async def create(self, slot, now: datetime, targets: list[str], preview: bool) -> dict:
        run_id = f"{now:%Y%m%d}-{slot.id}-{uuid.uuid4().hex[:12]}"
        record = {
            "run_id": run_id, "slot_id": slot.id, "slot_name": slot.name,
            "date": now.date().isoformat(), "started_at": now.isoformat(),
            "finished_at": "", "mode": slot.mode, "preview": preview,
            "status": "running", "stage": "pending", "targets": list(targets),
            "receipts": {}, "image_file": "", "error": "",
        }
        self.run_dir(run_id).mkdir()
        await self.save(record)
        return record

    async def save(self, record: dict) -> None:
        async with self.lock:
            run_id = record["run_id"]
            atomic_json(self.run_dir(run_id) / "meta.json", record)
            self.index["runs"][run_id] = json.loads(json.dumps(record))
            atomic_json(self.index_path, self.index)

    async def artifact_json(self, record: dict, name: str, value: Any) -> None:
        if name not in {"brief.json", "evidence.json", "qa.json"}:
            raise ValueError("未许可的产物名称")
        await asyncio.to_thread(atomic_json, self.run_dir(record["run_id"]) / name, value)

    def get(self, run_id: str) -> dict | None:
        self.run_dir(run_id)  # validates untrusted command arguments
        record = self.index["runs"].get(run_id)
        return json.loads(json.dumps(record)) if record else None

    def latest(self, slot_id: str, date: str, *, preview: bool = False) -> dict | None:
        rows = [r for r in self.index["runs"].values()
                if r.get("slot_id") == slot_id and r.get("date") == date
                and bool(r.get("preview")) == preview]
        row = max(rows, key=lambda r: r["started_at"], default=None)
        return json.loads(json.dumps(row)) if row else None

    def receipts(self, slot_id: str, date: str) -> dict[str, str]:
        """Any sent or uncertain receipt prevents automatic duplicate delivery."""
        out: dict[str, str] = {}
        for r in sorted(self.index["runs"].values(), key=lambda r: r["started_at"]):
            if r.get("preview") or r.get("slot_id") != slot_id or r.get("date") != date:
                continue
            for target, receipt in r.get("receipts", {}).items():
                state = receipt.get("status", "")
                if state in {"sent", "sending", "uncertain"}:
                    # A later explicit successful send settles an earlier uncertainty.
                    if out.get(target) != "sent" or state == "sent":
                        out[target] = state
        return out

    def count_today(self, date: str) -> int:
        return sum(r.get("date") == date for r in self.index["runs"].values())

    def recent(self, limit: int = 8) -> list[dict]:
        return sorted(self.index["runs"].values(), key=lambda r: r["started_at"], reverse=True)[:limit]

    def _brief_for_run(self, record: dict) -> dict | None:
        try:
            path = self.run_dir(record["run_id"]) / "brief.json"
            if not path.is_file():
                return None
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else None
        except (OSError, ValueError, KeyError):
            return None

    def history(self, now: datetime, days: int = 3) -> str:
        """Include every non-preview edition with a saved brief, even partial delivery.

        This is the semantic dedup context for later editions; it must not depend on
        the image having reached every group.
        """
        cutoff = (now.date() - timedelta(days=days)).isoformat()
        lines = []
        for r in sorted(self.index["runs"].values(), key=lambda r: r["started_at"]):
            if r.get("preview") or r.get("date", "") < cutoff:
                continue
            b = self._brief_for_run(r)
            if b is None:
                continue
            stories = [{k: story.get(k, "") for k in ("title", "event_key", "tag", "update", "published_at")}
                       for story in b.get("top_stories", []) if isinstance(story, dict)]
            lines.append({"date": r["date"], "slot": r["slot_id"],
                          "cover_headline": (b.get("cover") or {}).get("headline", ""),
                          "top_stories": stories, "on_this_day": b.get("on_this_day")})
        while lines and len(json.dumps(lines, ensure_ascii=False)) > 18000:
            lines.pop(0)
        return json.dumps(lines, ensure_ascii=False) if lines else "（尚无历史刊物）"

    def same_day_events(self, date: str) -> tuple[set[str], set[str]]:
        """Return event keys and normalized titles already used today (non-preview)."""
        keys: set[str] = set()
        titles: set[str] = set()
        for r in self.index["runs"].values():
            if r.get("preview") or r.get("date") != date:
                continue
            b = self._brief_for_run(r)
            if b is None:
                continue
            cover = (b.get("cover") or {}).get("headline", "")
            if cover:
                titles.add(re.sub(r"[^\w]+", "", str(cover)).casefold())
            for story in b.get("top_stories", []):
                if not isinstance(story, dict):
                    continue
                key = str(story.get("event_key", "") or "").strip()
                if key:
                    keys.add(key)
                for field in ("title", "display_title"):
                    title = str(story.get(field, "") or "").strip()
                    if title:
                        titles.add(re.sub(r"[^\w]+", "", title).casefold())
        return keys, titles

    async def recover(self) -> None:
        for r in list(self.index["runs"].values()):
            changed = False
            if r.get("status") == "running":
                r["status"] = "interrupted"
                r["error"] = "上次进程退出中断；已生成图片可重发，未生成任务请人工重试"
                changed = True
            for receipt in r.get("receipts", {}).values():
                if receipt.get("status") == "sending":
                    receipt["status"] = "uncertain"
                    changed = True
            if changed:
                await self.save(r)

    async def cleanup(self, now: datetime, retention_days: int) -> None:
        cutoff = (now.date() - timedelta(days=retention_days)).isoformat()
        async with self.lock:
            for run_id, record in list(self.index["runs"].items()):
                if record.get("date", "") >= cutoff or record.get("status") == "running":
                    continue
                target = self.run_dir(run_id)
                # run_dir verifies absolute containment before deleting.
                if target.exists():
                    await asyncio.to_thread(shutil.rmtree, target)
                del self.index["runs"][run_id]
            atomic_json(self.index_path, self.index)
