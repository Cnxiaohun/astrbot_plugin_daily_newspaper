"""上架规则守卫：插件内只允许 astrbot.api 的 logger，禁止内置 logging 模块。

AstrBot 插件审查要求日志器必须且只能从 astrbot.api 导入。这个测试扫描整个插件包，
防止以后再引入 `import logging` 或 `logging.getLogger` 之类的兜底写法。
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

PLUGIN_ROOT = Path(__file__).parents[1]
# 拼出模块名，避免本文件自身命中规则。
_MOD = "log" + "ging"
_SELF = Path(__file__).name
FORBIDDEN = (
    re.compile(r"^\s*import\s+" + _MOD + r"\b", re.M),
    re.compile(r"^\s*from\s+" + _MOD + r"\s+import\b", re.M),
    re.compile(r"\b" + _MOD + r"\.(?:getLogger|Logger|basicConfig|info|warning|error|debug)\b"),
)


def plugin_python_files():
    for path in sorted(PLUGIN_ROOT.rglob("*.py")):
        if ".git" in path.parts or "__pycache__" in path.parts:
            continue
        if path.name == _SELF:  # 本文件保存规则本身
            continue
        yield path


class LoggingPolicyTests(unittest.TestCase):
    def test_no_builtin_logging_anywhere(self):
        offenders = []
        for path in plugin_python_files():
            text = path.read_text(encoding="utf-8")
            for pattern in FORBIDDEN:
                match = pattern.search(text)
                if match:
                    offenders.append(f"{path.relative_to(PLUGIN_ROOT)}: {match.group(0).strip()}")
        self.assertEqual(offenders, [], "插件内不得使用内置 logging 模块：\n" + "\n".join(offenders))

    def test_entrypoint_imports_logger_from_astrbot_api(self):
        main = (PLUGIN_ROOT / "main.py").read_text(encoding="utf-8")
        self.assertRegex(main, r"from astrbot\.api import [^\n]*\blogger\b")

    def test_service_requires_injected_logger(self):
        from core.service import ReportService

        with self.assertRaises(ValueError):
            ReportService(None, {}, Path("/tmp/unused"), logger=None)


class AnnounceLoggerTests(unittest.IsolatedAsyncioTestCase):
    async def test_announce_without_logger_is_rejected(self):
        from core.announce import announce

        with self.assertRaises(ValueError):
            await announce(
                None, ["umo"], settings={"enabled": True}, slot_name="早报",
                date="2026-11-13", weekday="星期五", time="08:00", edition="刊",
                action="scheduled", reuse=False, sender=lambda *a: None, logger=None,
            )

    async def test_announce_works_with_injected_logger(self):
        from core.announce import announce

        sent = []

        class Shim:
            def warning(self, *a, **k):
                pass

        async def sender(context, umo, text):
            sent.append((umo, text))

        await announce(
            None, ["g:GroupMessage:1"], settings={"enabled": True, "use_persona": False},
            slot_name="早报", date="2026-11-13", weekday="星期五", time="08:00",
            edition="刊", action="scheduled", reuse=False, sender=sender, logger=Shim(),
        )
        self.assertEqual(len(sent), 1)
        self.assertIn("2026-11-13", sent[0][1])
        self.assertIn("早报", sent[0][1])


if __name__ == "__main__":
    unittest.main()
