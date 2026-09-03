"""
Резолв чатов и построение ссылок на сообщения.

Раньше чат был один и публичный, поэтому ссылка собиралась как
t.me/<username>/<id>. Теперь чатов несколько, часть из них приватные — у них
нет username, и ссылка строится через внутренний id (t.me/c/...).
"""

from __future__ import annotations

import re

from telethon import utils
from telethon.errors import RPCError

from config import ChatRef

_NUMERIC = re.compile(r"^-?\d+$")
_TME_PRIVATE = re.compile(r"t\.me/c/(\d+)")
_TME_PUBLIC = re.compile(r"t\.me/([A-Za-z0-9_]+)")


class ResolveError(Exception):
    """Чат не удалось найти — сообщение уже готово для показа пользователю."""


def parse_ref(ref: str) -> str | int:
    """'@foo', 'foo', '-100123', 't.me/foo', 't.me/c/123/45' -> username или id."""
    ref = ref.strip()

    m = _TME_PRIVATE.search(ref)
    if m:
        return int(f"-100{m.group(1)}")

    if "t.me/" in ref:
        m = _TME_PUBLIC.search(ref)
        if m:
            return m.group(1)

    ref = ref.lstrip("@")
    if _NUMERIC.match(ref):
        return int(ref)
    return ref


async def resolve(client, ref: str) -> ChatRef:
    """Находит чат от лица userbot-аккаунта. Кидает ResolveError с текстом."""
    target = parse_ref(ref)

    entity = None
    try:
        entity = await client.get_entity(target)
    except (ValueError, TypeError):
        # Telethon не знает этот peer — приватные чаты часто резолвятся только
        # после того, как их подтянули из списка диалогов.
        try:
            await client.get_dialogs()
            entity = await client.get_entity(target)
        except (ValueError, TypeError):
            entity = None
        except RPCError as e:
            raise ResolveError(f"Telegram отказал: {e}") from e
    except RPCError as e:
        raise ResolveError(f"Telegram отказал: {e}") from e

    if entity is None:
        raise ResolveError(
            f"Чат «{ref}» не найден. Проверь написание; "
            "для приватного чата нужен числовой id, и твой аккаунт должен в нём состоять."
        )

    return ChatRef(
        ref=ref.strip(),
        peer_id=utils.get_peer_id(entity),
        title=getattr(entity, "title", None) or getattr(entity, "username", None),
        username=getattr(entity, "username", None),
    )


def message_link(chat: ChatRef, msg_id: int) -> str | None:
    """None для старых обычных групп — на их сообщения ссылок не существует."""
    if chat.username:
        return f"https://t.me/{chat.username}/{msg_id}"
    if chat.peer_id is not None and str(chat.peer_id).startswith("-100"):
        return f"https://t.me/c/{str(chat.peer_id)[4:]}/{msg_id}"
    return None
