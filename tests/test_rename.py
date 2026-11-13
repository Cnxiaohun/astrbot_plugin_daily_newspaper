"""Rename safety: the new plugin name must survive id validation and data migration."""
from __future__ import annotations

import json
import tempfile
import types
import unittest
from pathlib import Path

import test_main  # noqa: F401  (installs the AstrBot SDK stubs)

ENTRY, _ = test_main.load_entry()


class RenameTests(unittest.TestCase):
    def setUp(self):
        self.plugin = ENTRY.DailyImageReport.__new__(ENTRY.DailyImageReport)

    def test_plugin_name_is_the_new_one(self):
        self.assertEqual(ENTRY.PLUGIN_NAME, "astrbot_plugin_daily_newspaper")
        self.assertEqual(ENTRY.LEGACY_PLUGIN_NAME, "astrbot_plugin_daily_image_report")

    def test_legacy_history_is_copied_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy = root / ENTRY.LEGACY_PLUGIN_NAME
            (legacy / "runs" / "20261113-morning-0123456789ab").mkdir(parents=True)
            (legacy / "state.json").write_text("{}", encoding="utf-8")
            (legacy / "runs" / "20261113-morning-0123456789ab" / "meta.json").write_text("{}", encoding="utf-8")
            (legacy / ".instance.lock").write_text("", encoding="utf-8")
            target = root / ENTRY.PLUGIN_NAME
            target.mkdir()

            self.assertTrue(self.plugin._migrate_legacy_data(target))
            self.assertTrue((target / "state.json").is_file())
            self.assertTrue((target / "runs" / "20261113-morning-0123456789ab" / "meta.json").is_file())
            # The process lock must never be carried over.
            self.assertFalse((target / ".instance.lock").exists())
            # Second call is a no-op because state.json now exists.
            self.assertFalse(self.plugin._migrate_legacy_data(target))

    def test_migration_never_overwrites_existing_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy = root / ENTRY.LEGACY_PLUGIN_NAME
            legacy.mkdir()
            (legacy / "state.json").write_text('{"old": 1}', encoding="utf-8")
            target = root / ENTRY.PLUGIN_NAME
            target.mkdir()
            (target / "state.json").write_text('{"new": 1}', encoding="utf-8")

            self.assertFalse(self.plugin._migrate_legacy_data(target))
            self.assertEqual(json.loads((target / "state.json").read_text()), {"new": 1})

    def test_missing_legacy_dir_is_harmless(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / ENTRY.PLUGIN_NAME
            target.mkdir()
            self.assertFalse(self.plugin._migrate_legacy_data(target))


class InitializeTests(unittest.IsolatedAsyncioTestCase):
    async def test_initialize_actually_runs(self):
        """initialize() 必须能真跑一遍：占位符这类错误只有在这里才会暴露。"""
        import tempfile
        from unittest.mock import AsyncMock, patch

        from test_service import default_config

        with tempfile.TemporaryDirectory() as tmp:
            cfg = test_main.Config(default_config())
            plugin = ENTRY.DailyImageReport(None, cfg)
            calls = {}

            class FakeService:
                def __init__(self, context, config, data, logger=None):
                    calls["data"] = data

                async def start(self, **kwargs):
                    calls["start"] = True

                async def close(self):
                    calls["close"] = True

            with patch.object(ENTRY, "ReportService", FakeService), \
                 patch.object(ENTRY, "StarTools") as tools, \
                 patch.object(ENTRY.DailyImageReport, "_persist", new=AsyncMock()):
                tools.get_data_dir.return_value = Path(tmp) / ENTRY.PLUGIN_NAME
                await plugin.initialize()

            self.assertTrue(calls.get("start"))
            self.assertEqual(calls["data"].name, ENTRY.PLUGIN_NAME)


if __name__ == "__main__":
    unittest.main()
