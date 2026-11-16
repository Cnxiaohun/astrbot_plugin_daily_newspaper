"""Authenticated operations must reuse the normal receipt/locking path."""
import sys
import types
import unittest
from unittest.mock import AsyncMock, patch

from core.operations import DashboardOperations, delivery_summary


class OperationsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.service = types.SimpleNamespace(
            resend=AsyncMock(return_value={"run_id": "20261116-morning-0123456789ab", "status": "partial"}),
            logger=types.SimpleNamespace(info=lambda *a: None),
            store=types.SimpleNamespace(recent=lambda n: []),
        )
        self.ops = DashboardOperations(types.SimpleNamespace(_ready=lambda: self.service, config={}))
        self.request = types.SimpleNamespace(username="admin", json=AsyncMock())
        self.web = types.ModuleType("astrbot.api.web")
        self.web.request = self.request

    async def test_unauthenticated_request_never_sends(self):
        self.request.username = None
        with patch.dict(sys.modules, {"astrbot.api.web": self.web}):
            self.assertEqual((await self.ops.resend())["status"], "error")
            self.assertEqual((await self.ops.recent())["status"], "error")
        self.service.resend.assert_not_awaited()

    async def test_no_arbitrary_targets_paths_or_string_confirmation(self):
        cases = [None, [], {"run_id": "id", "targets": ["stranger"]},
                 {"run_id": "id", "path": "/tmp/image.png"},
                 {"run_id": "id", "retry_uncertain": "false"}]
        with patch.dict(sys.modules, {"astrbot.api.web": self.web}):
            for case in cases:
                self.request.json.return_value = case
                self.assertEqual((await self.ops.resend())["status"], "error")
        self.service.resend.assert_not_awaited()

    async def test_uncertainty_requires_explicit_boolean_and_preserves_partial(self):
        self.request.json.return_value = {"run_id": "saved", "retry_uncertain": True}
        with patch.dict(sys.modules, {"astrbot.api.web": self.web}):
            result = await self.ops.resend()
        self.service.resend.assert_awaited_once_with("saved", retry_uncertain=True, image_format="original")
        self.assertEqual(result["data"]["status"], "partial")

    async def test_default_does_not_force_retry(self):
        self.request.json.return_value = {"run_id": "saved"}
        with patch.dict(sys.modules, {"astrbot.api.web": self.web}):
            await self.ops.resend()
        self.service.resend.assert_awaited_once_with("saved", retry_uncertain=False, image_format="original")

    def test_diagnostic_response_redacts_secrets_and_omits_config(self):
        record = {"api_key": "private-secret", "prompt": "private prompt", "receipts": {
            "group": {"status": "uncertain", "error": "private-secret https://private.test/key"}}}
        result = delivery_summary(record, {"api": {"api_key": "private-secret"}})
        self.assertNotIn("private-secret", str(result))
        self.assertNotIn("private.test", str(result))
        self.assertNotIn("prompt", result)
