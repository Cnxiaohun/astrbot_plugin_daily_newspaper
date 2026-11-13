"""Offline client tests: unittest + httpx.MockTransport, no real APIs or DNS."""

from __future__ import annotations

import base64
import copy
import io
import json
import socket
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from PIL import Image

# Work both with `unittest discover -s tests` and direct invocation.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.http_utils import (  # noqa: E402
    AmbiguousRequestError,
    ClientError,
    api_endpoint,
    extract_json_object,
    read_bounded,
)
from core.image_client import (  # noqa: E402
    ImageClient,
    ImageError,
    ImageResult,
    download_public_image,
    inspect_image,
)
from core.qa import QAError, check_image  # noqa: E402
from core.research import DISCLAIMER, ResearchClient, ResearchError  # noqa: E402

API = {
    "base_url": "http://127.0.0.1:8000",
    "api_key": "secret-test-key",
    "image_model": "primary",
    "fallback_image_models": ["backup"],
    "size": "1536x1024",
    "quality": "medium",
}
SETTINGS = {"model": "codex/research", "web_search": True, "max_repair_retries": 1}


def image_bytes(fmt: str = "PNG", size: tuple[int, int] = (32, 24)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, "navy").save(buffer, format=fmt)
    return buffer.getvalue()


def brief_fixture() -> dict:
    return {
        "edition": "测试刊物",
        "date": "2026-11-12",
        "weekday": "星期四",
        "cover": {"headline": "测试头条", "summary": "这是测试简报摘要。"},
        "top_stories": [
            {"title": f"测试新闻{i}", "summary": f"第{i}条测试摘要。", "source": "测试媒体", "url": f"https://news.example/article/{i}", "published_at": "2026-11-12T05:00:00+08:00"}
            for i in range(3)
        ],
        "world_in_60_seconds": ["一", "二", "三"],
        "on_this_day": [],
        "sources": ["https://news.example/article/0"],
        "fact_check_notes": "模型候选说明",
    }


def response_fixture(text: str | None = None, *, phase: str | None = "final_answer", search: bool = True, search_status: str = "completed") -> dict:
    output = []
    if search:
        output.append({"type": "web_search_call", "status": search_status, "action": {"type": "search", "sources": [{"url": "https://news.example/article/0"}]}})
    message = {
        "type": "message", "role": "assistant", "status": "completed",
        "content": [{"type": "output_text", "text": text if text is not None else json.dumps(brief_fixture(), ensure_ascii=False)}],
    }
    if phase is not None:
        message["phase"] = phase
    output.append(message)
    return {"status": "completed", "output": output, "usage": {"input_tokens": 10, "output_tokens": 5}}


def dns_records(*addresses: str) -> list:
    return [
        (socket.AF_INET6 if ":" in address else socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))
        for address in addresses
    ]


class JsonTests(unittest.TestCase):
    def test_extract_plain_fenced_and_prose(self):
        for text in ('{"x":1}', '```json\n{"x":1}\n```', '结果：{"x":"a}b"} 完成'):
            self.assertIn("x", extract_json_object(text))

    def test_reject_ambiguous_duplicate_nonfinite_and_array(self):
        for text in ('{} {}', '{"x":1,"x":2}', '{"x":NaN}', '[{"x":1}]', 'invalid', '{"a":0,"a":1,"child":{"pass":true}}'):
            with self.subTest(text=text), self.assertRaises(ClientError):
                extract_json_object(text)

    def test_endpoint_no_duplicate_v1(self):
        self.assertEqual(api_endpoint("http://localhost:42/v1/", "responses"), "http://localhost:42/v1/responses")
        self.assertEqual(api_endpoint("https://host/prefix", "images/generations"), "https://host/prefix/v1/images/generations")
        for url in ("file:///etc/passwd", "http://user:secret@host", "https://host/?key=abc"):
            with self.assertRaises(ClientError):
                api_endpoint(url, "responses")


class ResearchTests(unittest.IsolatedAsyncioTestCase):
    async def test_valid_research_and_evidence(self):
        requests = []
        def handler(request):
            requests.append(json.loads(request.content))
            return httpx.Response(200, json=response_fixture())
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            research = ResearchClient(SETTINGS, API, http)
            brief, usage = await research.research("检索今日新闻", "2026-11-12", "刊物")
            self.assertEqual(len(brief.stories), 3)
            self.assertIn(DISCLAIMER, brief.fact_check_notes)
            self.assertEqual(brief.stories[0].published_at, "2026-11-12T05:00:00+08:00")
            self.assertEqual(usage["input_tokens"], 10)
            self.assertEqual(len(research.last_evidence["web_search_calls"]), 1)
            self.assertFalse(research.last_evidence["truth_guarantee"])
            self.assertEqual(research.last_evidence["raw_brief"]["date"], "2026-11-12")
            self.assertEqual(requests[0]["tools"], [{"type": "web_search"}])
            self.assertIn("不可信", requests[0]["instructions"])
            self.assertNotIn("AI生成示意图", requests[0]["instructions"])
            await research.close()
            self.assertFalse(http.is_closed)

    async def test_phase_preference_over_commentary(self):
        result = response_fixture()
        result["output"].append({"type": "message", "role": "assistant", "phase": "commentary", "content": [{"type": "output_text", "text": "not json"}]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=result))) as http:
            brief, _ = await ResearchClient(SETTINGS, API, http).research("prompt", "2026-11-12", "刊物")
            self.assertEqual(brief.date, "2026-11-12")

    async def test_compatibility_without_phase(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=response_fixture(phase=None)))) as http:
            brief, _ = await ResearchClient(SETTINGS, API, http).research("prompt", "2026-11-12", "刊物")
            self.assertEqual(len(brief.stories), 3)

    async def test_search_evidence_mandatory_no_repair(self):
        for response in (response_fixture(search=False), response_fixture(search_status="in_progress")):
            calls = []
            def handler(request):
                calls.append(request)
                return httpx.Response(200, json=response)
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
                research = ResearchClient(SETTINGS, API, http)
                with self.assertRaisesRegex(ResearchError, "web_search_call"):
                    await research.research("prompt", "2026-11-12", "刊物")
                self.assertEqual(len(calls), 1)
                self.assertIn("final_text", research.last_evidence)

    async def test_disabled_search_rejects_before_network(self):
        def handler(request):
            self.fail("network must not be called")
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            with self.assertRaises(ResearchError):
                await ResearchClient({**SETTINGS, "web_search": False}, API, http).research("prompt", "2026-11-12", "刊物")

    async def test_json_repair_and_accumulate_usage(self):
        outputs = [response_fixture("not json"), response_fixture(search=False)]
        requests = []
        def handler(request):
            requests.append(json.loads(request.content))
            return httpx.Response(200, json=outputs.pop(0))
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            brief, usage = await ResearchClient(SETTINGS, API, http).research("prompt", "2026-11-12", "刊物")
            self.assertEqual(len(brief.stories), 3)
            self.assertEqual(usage["input_tokens"], 20)
            self.assertIn("未通过", requests[1]["input"])

    async def test_validation_failures_stop_without_static_fallback(self):
        mutations = [
            lambda d: d.update(date="2026-11-11"),
            lambda d: d.update(top_stories=d["top_stories"][:2]),
            lambda d: d["top_stories"][0].update(url="javascript:alert(1)"),
            lambda d: d["top_stories"][0].update(source=""),
            lambda d: d["cover"].update(summary=""),
            lambda d: d["top_stories"][0].update(summary="超" * 5000),
            lambda d: d.update(on_this_day=[{"year": 1948, "event": "测试史实", "source": "媒体", "url": "https://news.example/history", "month": 11, "day": 11}]),
        ]
        for mutate in mutations:
            data = brief_fixture()
            mutate(data)
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=response_fixture(json.dumps(data))))) as http:
                with self.assertRaises(ResearchError):
                    await ResearchClient({**SETTINGS, "max_repair_retries": 0}, API, http).research("prompt", "2026-11-12", "刊物")

    async def test_http_error_does_not_echo_secrets(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(401, text="secret-test-key private body"))) as http:
            with self.assertRaises(ResearchError) as error:
                await ResearchClient(SETTINGS, API, http).research("prompt", "2026-11-12", "刊物")
            self.assertNotIn("secret-test-key", str(error.exception))
            self.assertNotIn("private body", str(error.exception))


class ImageTests(unittest.IsolatedAsyncioTestCase):
    async def test_base64_actual_dimensions_and_reserved_parameters(self):
        data = image_bytes()
        requests = []
        def handler(request):
            requests.append(json.loads(request.content))
            return httpx.Response(200, json={"model": "provider/actual", "data": [{"b64_json": base64.b64encode(data).decode(), "mime_type": "wrong/ignored"}], "usage": {"output_tokens": 7}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            api = {**API, "extra_body": {"n": 99, "model": "evil", "prompt": "wrong"}}
            client = ImageClient(api, http)
            result = await client.generate("draw", model_override="override", size_override="auto", quality_override="high")
            self.assertEqual((result.width, result.height, result.mime), (32, 24, "image/png"))
            self.assertEqual(result.model, "provider/actual")
            self.assertEqual(result.usage["output_tokens"], 7)
            self.assertEqual(requests[0]["n"], 1)
            self.assertEqual(requests[0]["model"], "override")
            self.assertEqual(requests[0]["prompt"], "draw")
            self.assertEqual(requests[0]["quality"], "high")
            await client.close()
            self.assertFalse(http.is_closed)

    async def test_url_download_pinned_and_without_auth_cookie(self):
        data = image_bytes()
        seen = []
        def handler(request):
            seen.append(request)
            if request.method == "POST":
                return httpx.Response(200, json={"data": [{"url": "https://cdn.example/image.png?signature=hidden"}]})
            self.assertEqual(request.url.host, "93.184.216.34")
            self.assertEqual(request.headers["host"], "cdn.example")
            self.assertEqual(request.extensions["sni_hostname"], "cdn.example")
            self.assertNotIn("authorization", request.headers)
            self.assertNotIn("cookie", request.headers)
            return httpx.Response(200, content=data)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), auth=("user", "secret"), headers={"Authorization": "wrong"}, cookies={"secret": "cookie"}) as http:
            with patch("core.image_client.socket.getaddrinfo", return_value=dns_records("93.184.216.34")) as dns:
                result = await ImageClient(API, http).generate("draw")
            self.assertEqual(result.data, data)
            self.assertEqual(dns.call_count, 1)
            self.assertEqual(len(seen), 2)

    async def test_retries_429_then_explicit_fallback_order(self):
        models = []
        data = image_bytes()
        def handler(request):
            model = json.loads(request.content)["model"]
            models.append(model)
            if model == "primary":
                return httpx.Response(429, headers={"retry-after": "0"})
            return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(data).decode()}]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            result = await ImageClient(API, http).generate("draw")
            self.assertEqual(models, ["primary", "primary", "primary", "backup"])
            self.assertEqual(result.model, "backup")

    async def test_500_retry_success(self):
        calls = []
        def handler(request):
            calls.append(request)
            if len(calls) == 1:
                return httpx.Response(503, headers={"retry-after": "0"})
            return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(image_bytes()).decode()}]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            await ImageClient(API, http).generate("draw")
        self.assertEqual(len(calls), 2)

    async def test_connect_error_retry_before_fallback(self):
        models = []
        def handler(request):
            model = json.loads(request.content)["model"]
            models.append(model)
            if model == "primary":
                raise httpx.ConnectError("secret details", request=request)
            return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(image_bytes()).decode()}]})
        with patch("core.http_utils.asyncio.sleep", new_callable=AsyncMock):
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
                await ImageClient(API, http).generate("draw")
        self.assertEqual(models, ["primary", "primary", "primary", "backup"])

    async def test_read_timeout_never_retries_or_fails_over(self):
        calls = []
        def handler(request):
            calls.append(request)
            raise httpx.ReadTimeout("signed-url-secret", request=request)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            with self.assertRaises(AmbiguousRequestError) as error:
                await ImageClient(API, http).generate("draw")
        self.assertEqual(len(calls), 1)
        self.assertNotIn("signed-url-secret", str(error.exception))

    async def test_auth_failure_stops_all_models(self):
        for status in (401, 403):
            calls = []
            def handler(request):
                calls.append(request)
                return httpx.Response(status, text="secret-test-key")
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
                with self.assertRaises(ImageError) as error:
                    await ImageClient(API, http).generate("draw")
            self.assertEqual(len(calls), 1)
            self.assertNotIn("secret-test-key", str(error.exception))

    async def test_bad_image_responses_do_not_regenerate(self):
        for entries in ([], [{"b64_json": "!@#"}], [{"b64_json": base64.b64encode(b"<html>not an image").decode()}], [{"b64_json": "x"}, {"b64_json": "y"}]):
            calls = []
            def handler(request):
                calls.append(request)
                return httpx.Response(200, json={"data": entries})
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
                with self.assertRaises(ImageError):
                    await ImageClient(API, http).generate("draw")
            self.assertEqual(len(calls), 1)

    async def test_base64_size_limit(self):
        encoded = base64.b64encode(image_bytes()).decode()
        with patch("core.image_client.MAX_IMAGE_BYTES", 16):
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"data": [{"b64_json": encoded}]}))) as http:
                with self.assertRaisesRegex(ImageError, "20MB"):
                    await ImageClient(API, http).generate("draw")

    async def test_dns_private_mixed_and_redirect_ssrf_blocked(self):
        requests = []
        def handler(request):
            requests.append(request)
            return httpx.Response(302, headers={"location": "http://127.0.0.1/private?key=secret"})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            for url in ("file:///etc/passwd", "http://127.0.0.1/a", "http://[::1]/a", "http://169.254.169.254/a", "http://public.example:22/a", "http://user:secret@public.example/a"):
                with self.assertRaises(ImageError):
                    await download_public_image(url, http)
            self.assertEqual(requests, [])
            with patch("core.image_client.socket.getaddrinfo", return_value=dns_records("93.184.216.34", "10.0.0.2")):
                with self.assertRaises(ImageError):
                    await download_public_image("https://cdn.example/a", http)
            self.assertEqual(requests, [])
            with patch("core.image_client.socket.getaddrinfo", return_value=dns_records("93.184.216.34")):
                with self.assertRaises(ImageError) as error:
                    await download_public_image("https://cdn.example/a?key=secret", http)
            self.assertEqual(len(requests), 1)
            self.assertNotIn("secret", str(error.exception))

    async def test_redirect_checks_each_host_and_host_header(self):
        hosts = []
        def handler(request):
            hosts.append(request.headers["host"])
            if len(hosts) == 1:
                return httpx.Response(302, headers={"location": "https://other.example/final"})
            return httpx.Response(200, content=image_bytes())
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            with patch("core.image_client.socket.getaddrinfo", return_value=dns_records("93.184.216.34")) as dns:
                await download_public_image("https://cdn.example/initial", http)
            self.assertEqual(dns.call_count, 2)
            self.assertEqual(hosts, ["cdn.example", "other.example"])

    async def test_image_formats_and_max_dimensions(self):
        for fmt, mime in (("PNG", "image/png"), ("JPEG", "image/jpeg"), ("WEBP", "image/webp")):
            self.assertEqual(inspect_image(image_bytes(fmt))[0], mime)
        with patch("core.image_client.MAX_EDGE", 16):
            with self.assertRaisesRegex(ImageError, "尺寸"):
                inspect_image(image_bytes())
        with self.assertRaises(ImageError):
            inspect_image(image_bytes("GIF"))

    async def test_bounded_response_by_header_and_stream(self):
        response = httpx.Response(200, content=b"1234", headers={"content-length": "100"})
        with self.assertRaises(ClientError):
            await read_bounded(response, 10)
        with self.assertRaises(ClientError):
            await read_bounded(httpx.Response(200, content=b"123456789012"), 10)


class QATests(unittest.IsolatedAsyncioTestCase):
    async def test_valid_qa_and_data_url(self):
        requests = []
        result = {"pass": True, "missing": [], "garbled": [], "notes": "文字完整"}
        def handler(request):
            requests.append(json.loads(request.content))
            return httpx.Response(200, json=response_fixture(json.dumps(result), search=False))
        image = ImageResult(image_bytes(), "primary", "image/png", 32, 24)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            output = await check_image({"model": "vision"}, API, image, ["刊头"], client=http)
        self.assertTrue(output["pass"])
        content = requests[0]["input"][0]["content"]
        self.assertTrue(content[1]["image_url"].startswith("data:image/png;base64,"))
        self.assertEqual(requests[0]["model"], "vision")

    async def test_qa_false_is_valid_result(self):
        data = {"pass": False, "missing": ["刊头"], "garbled": [], "notes": "遗漏"}
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=response_fixture(json.dumps(data), search=False)))) as http:
            result = await check_image({}, API, ImageResult(image_bytes(), "x", "image/png", 32, 24), ["刊头"], http)
        self.assertFalse(result["pass"])

    async def test_qa_strict_pass_and_unavailability(self):
        malformed = [
            {"pass": "true", "missing": [], "garbled": [], "notes": ""},
            {"pass": 1, "missing": [], "garbled": [], "notes": ""},
            {"pass": True, "missing": ["标题"], "garbled": [], "notes": ""},
            {"pass": True, "missing": "none", "garbled": [], "notes": ""},
            {"pass": True, "missing": [], "garbled": []},
        ]
        image = ImageResult(image_bytes(), "x", "image/png", 32, 24)
        for data in malformed:
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=response_fixture(json.dumps(data), search=False)))) as http:
                with self.assertRaises(QAError):
                    await check_image({}, API, image, ["标题"], http)
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(403))) as http:
            with self.assertRaises(QAError):
                await check_image({}, API, image, ["标题"], http)


if __name__ == "__main__":
    unittest.main()
