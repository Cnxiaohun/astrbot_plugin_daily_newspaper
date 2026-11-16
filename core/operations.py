"""Dashboard-authenticated diagnostics and resend; no arbitrary paths or targets."""
from __future__ import annotations

from .service import safe_error


def delivery_summary(record: dict, config: dict) -> dict:
    """Expose only delivery state, not keys, prompts, sources or disk paths."""
    return {
        "run_id": record.get("run_id"), "date": record.get("date"),
        "slot_name": record.get("slot_name"), "status": record.get("status"),
        "stage": record.get("stage"), "finished_at": record.get("finished_at"),
        "receipts": {
            target: {**{k: receipt[k] for k in ("status", "at", "attempts", "image_format") if k in receipt},
                     "error": safe_error(RuntimeError(str(receipt.get("error", ""))), config)
                     if receipt.get("error") else ""}
            for target, receipt in record.get("receipts", {}).items()
        },
    }


class DashboardOperations:
    """Routes run ONLY behind AstrBot's authenticated plugin extension dispatcher."""
    def __init__(self, plugin):
        self.plugin = plugin

    async def recent(self):
        from astrbot.api.web import request
        if not request.username:
            return {"status": "error", "message": "需要登录 AstrBot 管理后台", "data": {}}
        service = self.plugin._ready()
        return {"status": "ok", "data": [delivery_summary(r, self.plugin.config)
                                          for r in service.store.recent(20)]}

    async def health(self):
        from astrbot.api.web import request
        if not request.username:
            return {"status": "error", "message": "需要登录 AstrBot 管理后台", "data": {}}
        from .health import platform_health
        from .config import load_slots
        self.plugin._ready()
        targets = list(dict.fromkeys(t for s in load_slots(self.plugin.config) for t in s.targets))
        return {"status": "ok", "data": await platform_health(self.plugin.context, targets, config=self.plugin.config)}

    async def resend(self):
        from astrbot.api.web import request
        if not request.username:
            return {"status": "error", "message": "需要登录 AstrBot 管理后台", "data": {}}
        body = await request.json()
        if (not isinstance(body, dict) or set(body) - {"run_id", "retry_uncertain", "image_format"}
                or not isinstance(body.get("run_id"), str)
                or type(body.get("retry_uncertain", False)) is not bool
                or body.get("image_format", "original") not in ("original", "jpeg")):
            return {"status": "error", "message": "需要 run_id、布尔值 retry_uncertain；image_format 只允许 original/jpeg", "data": {}}
        service = self.plugin._ready()
        service.logger.info("后台人工补发 user=%s run=%s retry_uncertain=%s",
                            request.username, body["run_id"][:80], body.get("retry_uncertain", False))
        try:
            record = await service.resend(body["run_id"], retry_uncertain=body.get("retry_uncertain", False),
                                          image_format=body.get("image_format", "original"))
            return {"status": "ok", "data": delivery_summary(record, self.plugin.config)}
        except Exception as exc:
            return {"status": "error", "message": safe_error(exc, self.plugin.config), "data": {}}
