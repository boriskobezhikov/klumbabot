"""
Klumba rental-listing monitor.

Слушает в реальном времени несколько Telegram-чатов (от вашего аккаунта, через
Telethon), дёшево отсеивает нерелевантное regex-предфильтром, прогоняет
кандидатов через Claude (Haiku) и присылает уведомление notify-ботом на каждое
совпадение.

Тот же notify-бот принимает команды управления фильтрами — /help покажет
список. Настройки живут в config.json и переживают перезапуск.

Первый запуск делается интерактивно (Telethon спросит номер и код), дальше
процесс работает как сервис. Подробности в README.md.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import time

from anthropic import AsyncAnthropic
from dotenv import load_dotenv
from telethon import TelegramClient, events

import chats as chats_mod
import commands
import config

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("klumba-monitor")

# ---------------------------------------------------------------------------
# Секреты (из .env). Всё, что относится к критериям поиска, — в config.json.
# ---------------------------------------------------------------------------
API_ID = int(os.environ["TG_API_ID"])
API_HASH = os.environ["TG_API_HASH"]
SESSION_NAME = os.environ.get("TG_SESSION_NAME", "klumba_userbot")

BOT_TOKEN = os.environ["NOTIFY_BOT_TOKEN"]
NOTIFY_CHAT_ID = int(os.environ["NOTIFY_CHAT_ID"])

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-haiku-4-5-20251001")

DEDUP_TTL = 24 * 3600  # одно и то же объявление часто кросс-постят в разные чаты

anthropic_client = AsyncAnthropic(api_key=ANTHROPIC_API_KEY)

# Userbot читает чаты, bot — шлёт уведомления и принимает команды.
user_client = TelegramClient(SESSION_NAME, API_ID, API_HASH)
bot = TelegramClient(f"{SESSION_NAME}_bot", API_ID, API_HASH)
bot.parse_mode = None  # в тексты попадают regex — markdown бы их поломал

state = config.State(cfg=config.load())


async def notify(text: str) -> None:
    try:
        await bot.send_message(NOTIFY_CHAT_ID, text, link_preview=False)
    except Exception as e:  # noqa: BLE001
        log.warning("notify() failed: %s", e)


async def classify(text: str) -> dict | None:
    now = time.time()
    state.api_calls = [t for t in state.api_calls if now - t < 86400]
    state.api_calls.append(now)

    try:
        resp = await anthropic_client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=250,
            system=config.build_system_prompt(state.cfg),
            messages=[{"role": "user", "content": text[:2000]}],
        )
        raw = resp.content[0].text.strip()
        raw = re.sub(r"^```(json)?|```$", "", raw, flags=re.MULTILINE).strip()
        return json.loads(raw)
    except Exception as e:  # noqa: BLE001
        log.warning("classify() failed: %s", e)
        return None


# ---------------------------------------------------------------------------
# Дедупликация по тексту
# ---------------------------------------------------------------------------
_WHITESPACE = re.compile(r"\s+")


def _dedup_key(text: str) -> str:
    return hashlib.sha256(
        _WHITESPACE.sub(" ", text.strip().casefold()).encode()
    ).hexdigest()


def _seen_recently(key: str) -> bool:
    """Помечает текст как виденный и говорит, встречался ли он за сутки."""
    now = time.time()
    for k, ts in list(state.seen.items()):
        if now - ts > DEDUP_TTL:
            del state.seen[k]
    if key in state.seen:
        return True
    state.seen[key] = now
    return False


# ---------------------------------------------------------------------------
# Мониторинг. Список чатов не зашит в декоратор, чтобы /chats add работал без
# перезапуска — сверяем chat_id с конфигом уже внутри обработчика.
# ---------------------------------------------------------------------------
@user_client.on(events.NewMessage)
async def handler(event) -> None:
    cfg = state.cfg
    chat = cfg.chat_by_peer_id(event.chat_id)
    if chat is None:
        return

    text = event.raw_text or ""
    if not text:
        return

    if cfg.prefilter["enabled"]:
        try:
            listing, bedroom, seeking = cfg.compiled_prefilter()
        except re.error as e:
            log.warning("битый regex в конфиге, предфильтр пропущен: %s", e)
        else:
            if seeking.search(text) and not listing.search(text):
                return
            if not listing.search(text):
                return
            if not bedroom.search(text):
                return

    if _seen_recently(_dedup_key(text)):
        log.info("dup #%s в %s — пропуск", event.id, chat.label())
        return

    log.info(
        "candidate #%s [%s]: %.70s", event.id, chat.label(), text.replace("\n", " ")
    )

    verdict = await classify(text)
    if not verdict:
        return

    if verdict.get("match"):
        msg = (
            "🔥 Новое подходящее объявление\n\n"
            f"Чат: {chat.label()}\n"
            f"{verdict.get('reason', '')}\n"
            f"Цена: {verdict.get('price_usd', '?')}$ | "
            f"Площадь: {verdict.get('area_m2', '?')} м² | "
            f"Район: {verdict.get('district', '?')}"
        )
        link = chats_mod.message_link(chat, event.id)
        if link:
            msg += f"\n\n{link}"
        await notify(msg)
        log.info("-> notified for #%s", event.id)
    else:
        log.info("-> skip #%s (%s)", event.id, verdict.get("reason", ""))


async def resolve_pending() -> list[str]:
    """Доставляет peer_id чатам, добавленным без него (в т.ч. из старого .env)."""
    problems: list[str] = []
    changed = False

    for chat in state.cfg.chats:
        if chat.peer_id is not None:
            continue
        try:
            resolved = await chats_mod.resolve(user_client, chat.ref)
        except chats_mod.ResolveError as e:
            problems.append(str(e))
            continue
        chat.peer_id = resolved.peer_id
        chat.title = resolved.title
        chat.username = resolved.username
        changed = True

    if changed:
        await config.save(state.cfg)
    return problems


async def main() -> None:
    await bot.start(bot_token=BOT_TOKEN)
    await user_client.start()

    problems = await resolve_pending()
    commands.register(bot, user_client, state, NOTIFY_CHAT_ID)

    active = [c for c in state.cfg.chats if c.peer_id is not None]
    listed = ", ".join(c.label() for c in active) or "ни одного"
    log.info("Запущен. Слушаю: %s", listed)

    msg = (
        "✅ Мониторинг запущен.\n"
        f"Чаты: {listed}\n"
        f"Бюджет: {state.cfg.budget_usd}$\n\n"
        "/help — список команд"
    )
    if problems:
        msg += "\n\n⚠️ Не удалось подключить:\n" + "\n".join(f"• {p}" for p in problems)
    await notify(msg)

    await asyncio.gather(
        user_client.run_until_disconnected(),
        bot.run_until_disconnected(),
    )


if __name__ == "__main__":
    asyncio.run(main())
