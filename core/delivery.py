"""Images only. An exception after dispatch is ambiguous and is not auto-retried."""
from __future__ import annotations

from pathlib import Path


def jpeg_delivery_copy(path: Path) -> Path:
    """Explicit manual transport alternative; preserve the original and resolution.

    This never runs automatically after an ambiguous send. It changes encoding,
    not newspaper content, and never calls a model.
    """
    import io
    from PIL import Image

    with Image.open(path) as source:
        if source.width * source.height > 24_000_000:
            raise ValueError("图片过大，不能转换传输副本")
        source.load()
        rgba = source.convert("RGBA")
        rgb = Image.new("RGB", source.size, "white")
        rgb.paste(rgba, mask=rgba.getchannel("A"))
        output = io.BytesIO()
        rgb.save(output, format="JPEG", quality=94, optimize=True, progressive=False)
    target = path.with_name("delivery-jpeg.jpg")
    target.write_bytes(output.getvalue())
    return target


async def send_image(context, umo: str, path: Path) -> bool:
    """Return platform-match status, NOT an end-user read or delivery receipt."""
    from astrbot.api.event import MessageChain
    from astrbot.api.message_components import Image

    chain = MessageChain(chain=[Image.fromFileSystem(str(path.resolve()))])
    return await context.send_message(umo, chain)


async def send_alert(context, umo: str, text: str) -> None:
    if not umo:
        return
    from astrbot.api.event import MessageChain

    await context.send_message(umo, MessageChain().message(text))
