"""Single-image generation with explicit failover, bounded decoding and SSRF-safe downloads."""

from __future__ import annotations

import asyncio
import base64
import binascii
import io
import ipaddress
import socket
import warnings
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx
from PIL import Image, UnidentifiedImageError

from .http_utils import (
    MAX_IMAGE_BYTES,
    MAX_JSON_IMAGE_BYTES,
    AmbiguousRequestError,
    ClientError,
    ConnectionFailure,
    HTTPRequestError,
    api_endpoint,
    positive_int,
    post_json,
    read_bounded,
    use_client,
)

MAX_PIXELS = 32_000_000
MAX_EDGE = 8192
MAX_REDIRECTS = 4


class ImageError(ClientError):
    """Generation/validation failed; safe to show without leaking prompt or credentials."""


@dataclass
class ImageResult:
    data: bytes
    model: str
    mime: str
    width: int
    height: int
    usage: dict[str, Any] = field(default_factory=dict)


def inspect_image(data: bytes) -> tuple[str, int, int]:
    """Verify both container and pixels; never accept SVG, animated images or huge rasters."""
    if not data or len(data) > MAX_IMAGE_BYTES:
        raise ImageError("图片为空或超过 20MB 上限")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                fmt = image.format
                width, height = image.size
                if fmt not in {"PNG", "JPEG", "WEBP"}:
                    raise ImageError("仅接受 PNG/JPEG/WebP 图片")
                if width < 1 or height < 1 or max(width, height) > MAX_EDGE or width * height > MAX_PIXELS:
                    raise ImageError("图片尺寸超过安全上限")
                if getattr(image, "n_frames", 1) != 1:
                    raise ImageError("只接受单帧图片")
                image.verify()
            # verify checks structure; load additionally catches truncated pixel streams.
            with Image.open(io.BytesIO(data)) as image:
                image.load()
        return {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}[fmt], width, height
    except ImageError:
        raise
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError, Image.DecompressionBombWarning, Image.DecompressionBombError):
        raise ImageError("生成结果不是完整有效的图片") from None


async def _public_destination(url: str) -> tuple[httpx.URL, str, str]:
    """Resolve once, reject every non-public answer, then pin the checked IP for I/O.

    Checking and pinning each redirect prevents DNS rebinding between validation
    and connect. TLS still verifies the original hostname using SNI.
    """
    try:
        parsed = urlsplit(url)
        if (
            parsed.scheme not in {"https", "http"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or any(c.isspace() for c in url)
        ):
            raise ValueError
        host = parsed.hostname.encode("idna").decode("ascii").rstrip(".")
        if not host or "%" in host:
            raise ValueError
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        if port not in {80, 443}:
            raise ValueError
        original = httpx.URL(url)
    except (ValueError, UnicodeError, httpx.InvalidURL):
        raise ImageError("图片下载 URL 不合法（仅允许公开 HTTP/HTTPS 的 80/443 端口）") from None
    try:
        direct = ipaddress.ip_address(host)
        addresses = [str(direct)]
    except ValueError:
        try:
            records = await asyncio.wait_for(
                asyncio.to_thread(socket.getaddrinfo, host, port, type=socket.SOCK_STREAM),
                timeout=10,
            )
        except (OSError, TimeoutError):
            raise ImageError("图片下载域名解析失败") from None
        addresses = list(dict.fromkeys(record[4][0] for record in records))
    if not addresses:
        raise ImageError("图片下载域名没有可用地址")
    for address in addresses:
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            raise ImageError("图片下载域名返回非法 IP 地址") from None
        if not ip.is_global or ip.is_multicast or ip.is_unspecified:
            raise ImageError("图片下载地址不是公网 IP，已阻止请求")
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
            if not ip.ipv4_mapped.is_global:
                raise ImageError("图片下载地址映射到非公网 IP，已阻止请求")
    selected = next((address for address in addresses if ":" not in address), addresses[0])
    # Host preserves the public hostname; the URL passed to transport uses only a
    # previously validated literal IP and can no longer be rebound by DNS.
    authority = original.netloc.decode("ascii")
    return original.copy_with(host=selected), authority, host


async def download_public_image(url: str, client: httpx.AsyncClient | None = None) -> bytes:
    """No Authorization/Cookie/Referer headers, no inherited auth, every redirect checked."""
    current = url
    try:
        async with use_client(client) as http:
            for redirect in range(MAX_REDIRECTS + 1):
                pinned, authority, hostname = await _public_destination(current)
                request = httpx.Request(
                    "GET", pinned,
                    headers={
                        "Host": authority,
                        "Accept": "image/png,image/jpeg,image/webp",
                        "Accept-Encoding": "identity",
                        "Connection": "close",
                    },
                    extensions={
                        "sni_hostname": hostname,
                        "timeout": httpx.Timeout(60, connect=10).as_dict(),
                    },
                )
                response = await http.send(request, stream=True, auth=None, follow_redirects=False)
                try:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("location")
                        if not location or redirect >= MAX_REDIRECTS:
                            raise ImageError("图片下载重定向过多或缺少目标")
                        current = urljoin(current, location)
                        continue
                    if response.status_code != 200:
                        raise ImageError(f"图片下载 HTTP {response.status_code}")
                    return await read_bounded(response, MAX_IMAGE_BYTES)
                finally:
                    await response.aclose()
    except ImageError:
        raise
    except ClientError as exc:
        raise ImageError(str(exc)) from None
    except (httpx.HTTPError, httpx.InvalidURL):
        # Never log signed URL query strings or transport exception messages.
        raise ImageError("图片下载网络请求失败（已隐藏签名 URL）") from None
    raise ImageError("图片下载失败")


def _decode_base64(value: Any) -> bytes:
    if not isinstance(value, str):
        raise ImageError("图片 base64 字段不是字符串")
    max_encoded = ((MAX_IMAGE_BYTES + 2) // 3) * 4
    if len(value) > max_encoded + 100:
        raise ImageError("图片 base64 超过 20MB 上限")
    if value.startswith("data:"):
        prefix, separator, value = value.partition(",")
        if not separator or prefix not in {
            "data:image/png;base64", "data:image/jpeg;base64", "data:image/webp;base64"
        }:
            raise ImageError("图片 data URL 格式不合法")
    if len(value) > max_encoded:
        raise ImageError("图片 base64 超过 20MB 上限")
    try:
        data = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        raise ImageError("图片 base64 解码失败") from None
    if len(data) > MAX_IMAGE_BYTES:
        raise ImageError("图片超过 20MB 上限")
    return data


class ImageClient:
    def __init__(self, api: dict, client: httpx.AsyncClient | None = None):
        self.api = api
        self.client = client

    async def close(self) -> None:
        """Injected clients belong to the caller; temporary clients close per call."""

    async def generate(
        self,
        prompt: str,
        model_override: str = "",
        size_override: str = "",
        quality_override: str = "",
    ) -> ImageResult:
        if not isinstance(prompt, str) or not prompt.strip():
            raise ImageError("生图提示词不能为空")
        primary = str(model_override or self.api.get("image_model") or "").strip()
        if not primary:
            raise ImageError("尚未配置生图模型")
        fallbacks = self.api.get("fallback_image_models") or []
        if not isinstance(fallbacks, list) or any(not isinstance(m, str) for m in fallbacks):
            raise ImageError("备用模型配置必须为字符串列表")
        models = list(dict.fromkeys([primary] + [m.strip() for m in fallbacks if m.strip()]))
        extra = self.api.get("extra_body") or {}
        if not isinstance(extra, dict):
            raise ImageError("生图额外参数必须为 JSON 对象")
        timeout = positive_int(self.api.get("timeout_seconds", 300), 300, 900)
        endpoint = api_endpoint(self.api.get("base_url", ""), "images/generations")
        last_error: ClientError | None = None
        async with use_client(self.client) as client:
            for model in models:
                payload = dict(extra)
                # Reserved fields cannot be overridden through extra_body.
                payload.update({"model": model, "prompt": prompt, "n": 1})
                size = size_override or self.api.get("size", "")
                quality = quality_override or self.api.get("quality", "")
                if size:
                    payload["size"] = size
                if quality:
                    payload["quality"] = quality
                try:
                    response = await post_json(
                        client, endpoint, self.api.get("api_key", ""), payload,
                        timeout=timeout, max_bytes=MAX_JSON_IMAGE_BYTES,
                    )
                except AmbiguousRequestError:
                    # No fallback: a read/write timeout may already have billed this image.
                    raise
                except HTTPRequestError as exc:
                    if exc.status in {401, 403}:
                        raise ImageError(f"生图 API HTTP {exc.status}：认证/权限失败，未自动重试") from None
                    last_error = exc
                    continue
                except ConnectionFailure as exc:
                    last_error = exc
                    continue
                except ClientError as exc:
                    raise ImageError(str(exc)) from None
                entries = response.get("data")
                if not isinstance(entries, list) or len(entries) != 1 or not isinstance(entries[0], dict):
                    raise ImageError("生图 API 应返回且仅返回一张图片")
                entry = entries[0]
                # Once an image response exists, failures in download/decoding do
                # not trigger another paid generation automatically.
                if entry.get("b64_json") is not None:
                    data = _decode_base64(entry["b64_json"])
                elif isinstance(entry.get("url"), str) and entry["url"]:
                    data = await download_public_image(entry["url"], client=self.client)
                else:
                    raise ImageError("生图 API 缺少 b64_json/url")
                mime, width, height = await asyncio.to_thread(inspect_image, data)
                usage = response.get("usage") if isinstance(response.get("usage"), dict) else {}
                actual_model = response.get("model") if isinstance(response.get("model"), str) else model
                return ImageResult(data, actual_model, mime, width, height, usage)
        detail = str(last_error) if last_error is not None else "无可用模型"
        raise ImageError(f"所有显式配置的生图模型均失败：{detail}")
