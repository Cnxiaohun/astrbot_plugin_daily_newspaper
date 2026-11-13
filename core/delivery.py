"""Images only. An exception after dispatch is ambiguous and is not auto-retried."""
from __future__ import annotations

from pathlib import Path


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
