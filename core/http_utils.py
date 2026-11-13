"""Bounded HTTP/JSON utilities. Never echo remote bodies or credentials in errors."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator
from urllib.parse import urlsplit, urlunsplit

import httpx

MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_JSON_IMAGE_BYTES = ((MAX_IMAGE_BYTES + 2) // 3) * 4 + 1024 * 1024
MAX_RESEARCH_BYTES = 2 * 1024 * 1024


class ClientError(RuntimeError):
    """Safe user-facing error: messages contain no remote bodies/URLs/API keys."""


class AmbiguousRequestError(ClientError):
    """A request may already have been billed; do not automatically repeat it."""


class HTTPRequestError(ClientError):
    def __init__(self, status: int, retryable: bool = False):
        self.status = status
        self.retryable = retryable
        super().__init__(f"API HTTP {status}")


class ConnectionFailure(ClientError):
    """Connection establishment failed before a response was obtained."""


def positive_int(value: Any, default: int, maximum: int) -> int:
    try:
        return max(1, min(int(value), maximum))
    except (ValueError, TypeError, OverflowError):
        return default


def api_endpoint(base_url: str, resource: str) -> str:
    """Accept either an API root or a root ending in /v1, never duplicate /v1."""
    raw = str(base_url or "").strip().rstrip("/")
    try:
        parsed = urlsplit(raw)
        if (
            parsed.scheme not in {"https", "http"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError
        parsed.port  # Validate malformed ports without exposing the supplied URL.
    except ValueError:
        raise ClientError("API base_url 不合法（只允许无凭据的 HTTP/HTTPS 根地址）") from None
    path = parsed.path.rstrip("/")
    if not path.endswith("/v1"):
        path += "/v1"
    return urlunsplit((parsed.scheme, parsed.netloc, path + "/" + resource.lstrip("/"), "", ""))


@asynccontextmanager
async def use_client(client: httpx.AsyncClient | None) -> AsyncIterator[httpx.AsyncClient]:
    """Only close clients created here; disable environment proxies for local gateways."""
    if client is not None:
        yield client
    else:
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as created:
            yield created


async def read_bounded(response: httpx.Response, limit: int) -> bytes:
    # We request identity encoding. Refuse unexpected compression instead of
    # allocating an arbitrarily large decompressed chunk before enforcing limits.
    if response.headers.get("content-encoding", "identity").lower() not in {"", "identity"}:
        raise ClientError("响应使用未允许的压缩编码")
    length = response.headers.get("content-length")
    if length:
        try:
            if int(length) > limit:
                raise ClientError("响应超过大小上限")
        except ValueError:
            raise ClientError("响应 Content-Length 不合法") from None
    data = bytearray()
    async for chunk in response.aiter_bytes():
        if len(data) + len(chunk) > limit:
            raise ClientError("响应超过大小上限")
        data.extend(chunk)
    return bytes(data)


def _no_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _invalid_constant(_: str) -> None:
    raise ValueError("non-finite JSON number")


def strict_json_loads(text: str | bytes) -> Any:
    return json.loads(text, object_pairs_hook=_no_duplicate_pairs, parse_constant=_invalid_constant)


def extract_json_object(text: str) -> dict[str, Any]:
    """Extract exactly one JSON object, tolerating fences and surrounding prose.

    Duplicate keys, NaN/Infinity, multiple competing objects, arrays and oversized
    outputs are rejected instead of silently selecting an arbitrary candidate.
    """
    if not isinstance(text, str) or len(text) > MAX_RESEARCH_BYTES:
        raise ClientError("模型 JSON 文本为空或过大")
    decoder = json.JSONDecoder(object_pairs_hook=_no_duplicate_pairs, parse_constant=_invalid_constant)
    text = text.strip().lstrip("\ufeff")
    try:
        value = strict_json_loads(text)
    except (ValueError, UnicodeError, RecursionError):
        value = None
    else:
        if isinstance(value, dict):
            return value
        raise ClientError("模型 JSON 必须是对象")
    # Scan whole outer objects, including malformed ones. Never salvage a nested
    # child from an invalid/duplicate-key parent and accidentally accept it.
    candidates: list[dict[str, Any]] = []
    position = 0
    while position < len(text):
        start = text.find("{", position)
        if start < 0:
            break
        depth = 0
        quoted = False
        escaped = False
        end = len(text)
        for index in range(start, len(text)):
            char = text[index]
            if quoted:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    quoted = False
            elif char == '"':
                quoted = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    end = index + 1
                    break
        try:
            obj = decoder.decode(text[start:end])
        except (ValueError, RecursionError):
            position = end
            continue
        if isinstance(obj, dict):
            candidates.append(obj)
        position = end
    if len(candidates) != 1:
        raise ClientError("无法提取唯一合法 JSON 对象")
    return candidates[0]


def response_text(response: dict[str, Any]) -> str:
    """Prefer final_answer messages; gateways without phase use the last message."""
    output = response.get("output")
    if not isinstance(output, list):
        raise ClientError("Responses API 缺少 output 列表")
    messages = [
        item for item in output
        if isinstance(item, dict)
        and item.get("type") == "message"
        and item.get("role", "assistant") == "assistant"
        and item.get("status", "completed") == "completed"
    ]
    finals = [m for m in messages if m.get("phase") == "final_answer"]
    selected = finals[-1:] if finals else [m for m in messages if not m.get("phase")][-1:]
    chunks: list[str] = []
    for message in selected:
        for content in message.get("content", []):
            if isinstance(content, dict) and content.get("type") == "output_text":
                if isinstance(content.get("text"), str):
                    chunks.append(content["text"])
    if not chunks:
        raise ClientError("Responses API 缺少最终文本")
    return "\n".join(chunks)


def require_completed(response: dict[str, Any]) -> None:
    if response.get("error") or response.get("status", "completed") != "completed":
        raise ClientError("Responses API 未正常完成")


def _retry_delay(response: httpx.Response | None, attempt: int) -> float:
    if response is not None:
        try:
            return max(0.0, min(float(response.headers.get("retry-after", "")), 30.0))
        except ValueError:
            pass
    return min(float(2**attempt), 8.0)


async def post_json(
    client: httpx.AsyncClient,
    url: str,
    key: str,
    payload: dict[str, Any],
    *,
    timeout: int,
    max_bytes: int,
    attempts: int = 3,
) -> dict[str, Any]:
    """Retry only 429/5xx and pre-connection failures; never retry read/write timeouts.

    Requests are built directly to avoid inheriting caller auth, cookies or base
    URLs. Remote redirects are not followed, so gateway credentials cannot leak.
    """
    if not key:
        raise ClientError("尚未配置 API Key")
    for attempt in range(attempts):
        response: httpx.Response | None = None
        try:
            request = httpx.Request(
                "POST", url,
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json", "Accept-Encoding": "identity"},
                json=payload,
                extensions={"timeout": httpx.Timeout(timeout).as_dict()},
            )
            response = await client.send(request, auth=None, follow_redirects=False, stream=True)
            if response.status_code >= 300:
                retryable = response.status_code == 429 or response.status_code >= 500
                if retryable and attempt + 1 < attempts:
                    delay = _retry_delay(response, attempt)
                    await response.aclose()
                    await asyncio.sleep(delay)
                    continue
                raise HTTPRequestError(response.status_code, retryable)
            raw = await read_bounded(response, max_bytes)
            try:
                value = strict_json_loads(raw)
            except (ValueError, UnicodeError, RecursionError):
                raise ClientError("API 返回的 JSON 不合法") from None
            if not isinstance(value, dict):
                raise ClientError("API JSON 响应不是对象")
            return value
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
            if attempt + 1 >= attempts:
                raise ConnectionFailure("API 连接失败（未取得响应）") from None
            await asyncio.sleep(_retry_delay(None, attempt))
        except (httpx.ReadTimeout, httpx.ReadError, httpx.WriteTimeout, httpx.WriteError, httpx.RemoteProtocolError):
            raise AmbiguousRequestError(
                "API 响应中断或读写超时，任务可能已提交计费；停止自动重试，请人工确认"
            ) from None
        except httpx.HTTPError:
            raise ClientError("API 网络请求失败") from None
        finally:
            if response is not None:
                await response.aclose()
    raise ConnectionFailure("API 连接失败")
