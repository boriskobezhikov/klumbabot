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

import httpx
from anthropic import AsyncAnthropic
from dotenv import load_dotenv
from telethon import TelegramClient, events

import chats as chats_mod
import commands
import config
import ssge

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


# ---------------------------------------------------------------------------
# Опрос ss.ge
#
# Regex-предфильтр сюда не применяется — он написан под русские сообщения из
# чата и отсёк бы грузинские описания целиком. Вместо него отсекаем по цене:
# сайт отдаёт её сразу в долларах, так что это бесплатно и точно.
# ---------------------------------------------------------------------------
async def _handle_ssge_listing(listing: ssge.Listing) -> None:
    cfg = state.cfg

    if listing.price_usd and listing.price_usd > cfg.budget_usd:
        log.info(
            "ss.ge #%s: %s$ > бюджета %s$ — пропуск",
            listing.id, listing.price_usd, cfg.budget_usd,
        )
        return

    log.info(
        "ss.ge candidate #%s: %s$ %sм² %s",
        listing.id, listing.price_usd, listing.area_m2, listing.location(),
    )

    verdict = await classify(listing.as_prompt())
    if not verdict:
        return

    if not verdict.get("match"):
        log.info("-> skip ss.ge #%s (%s)", listing.id, verdict.get("reason", ""))
        return

    price = verdict.get("price_usd") or listing.price_usd or "?"
    area = verdict.get("area_m2") or listing.area_m2 or "?"
    district = verdict.get("district") or listing.location()
    await notify(
        "🔥 Новое подходящее объявление\n\n"
        "Источник: ss.ge\n"
        f"{verdict.get('reason', '')}\n"
        f"Цена: {price}$ | Площадь: {area} м² | Район: {district}\n\n"
        f"{listing.url}"
    )
    log.info("-> notified for ss.ge #%s", listing.id)


async def poll_ssge() -> None:
    """Опрашивает ss.ge, пока жив процесс. Ошибки сети не должны его ронять."""
    seen_ids = config.load_seen_ids()
    seen = set(seen_ids)
    # Пустой список = первый запуск. Тогда первый проход только запоминает
    # текущую выдачу, иначе пользователь получил бы 30 уведомлений разом.
    priming = not seen
    state.ssge_seen_count = len(seen)

    while True:
        s = state.cfg.ssge
        if not s["enabled"]:
            await asyncio.sleep(60)
            continue

        try:
            listings = await ssge.fetch_listings(
                pages=int(s["pages"]),
                city_id=int(s["city_id"]),
                rooms=str(s["rooms"]),
            )
            state.ssge_last_error = None
        except (ssge.SsgeError, httpx.HTTPError, asyncio.TimeoutError) as e:
            state.ssge_last_error = str(e)
            log.warning("ss.ge: опрос не удался: %s", e)
            await asyncio.sleep(max(60, int(s["poll_minutes"]) * 60))
            continue

        fresh = [l for l in listings if l.id not in seen]
        for listing in fresh:
            seen.add(listing.id)
            seen_ids.insert(0, listing.id)
        config.save_seen_ids(seen_ids)

        state.ssge_last_poll = time.time()
        state.ssge_last_new = len(fresh)
        state.ssge_seen_count = len(seen)

        if priming:
            priming = False
            log.info("ss.ge: запомнил %s объявлений, слежу за новыми", len(listings))
            await notify(
                f"🌐 ss.ge подключён: проиндексировано {len(listings)} объявлений.\n"
                "Уведомления пойдут только про новые."
            )
        else:
            log.info("ss.ge: получено %s, новых %s", len(listings), len(fresh))
            for listing in fresh:
                await _handle_ssge_listing(listing)

        await asyncio.sleep(int(s["poll_minutes"]) * 60)


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
    ssge_note = (
        f"каждые {state.cfg.ssge['poll_minutes']} мин"
        if state.cfg.ssge["enabled"]
        else "выключен"
    )
    log.info("Запущен. Слушаю: %s | ss.ge: %s", listed, ssge_note)

    msg = (
        "✅ Мониторинг запущен.\n"
        f"Чаты: {listed}\n"
        f"ss.ge: {ssge_note}\n"
        f"Бюджет: {state.cfg.budget_usd}$\n\n"
        "/help — список команд"
    )
    if problems:
        msg += "\n\n⚠️ Не удалось подключить:\n" + "\n".join(f"• {p}" for p in problems)
    await notify(msg)

    await asyncio.gather(
        user_client.run_until_disconnected(),
        bot.run_until_disconnected(),
        poll_ssge(),
    )


if __name__ == "__main__":
    asyncio.run(main())
