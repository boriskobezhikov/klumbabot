"""
Klumba rental monitor.

Следит за объявлениями об аренде в Тбилиси из двух источников — Telegram-чатов
(живая подписка через Telethon) и сайта ss.ge (опрос по таймеру) — и рассылает
подходящие подписчикам.

Экономика построена вокруг одного решения: Claude вызывается ОДИН раз на
объявление и только извлекает факты. Кому это объявление подходит, решает
обычная арифметика в users.matches(). Поэтому расходы на API не зависят от
числа подписчиков — что один человек, что пятьдесят.

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
import districts
import ssge
import stats as stats_mod
import users as users_mod

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("klumba-monitor")

API_ID = int(os.environ["TG_API_ID"])
API_HASH = os.environ["TG_API_HASH"]
SESSION_NAME = os.environ.get("TG_SESSION_NAME", "klumba_userbot")

BOT_TOKEN = os.environ["NOTIFY_BOT_TOKEN"]
OWNER_ID = int(os.environ["NOTIFY_CHAT_ID"])

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-haiku-4-5-20251001")

DEDUP_TTL = 24 * 3600
LAST_KEPT = 15  # сколько последних разборов помнить для /last

anthropic_client = AsyncAnthropic(api_key=ANTHROPIC_API_KEY)

user_client = TelegramClient(SESSION_NAME, API_ID, API_HASH)
bot = TelegramClient(f"{SESSION_NAME}_bot", API_ID, API_HASH)
bot.parse_mode = None  # в тексты попадают regex — markdown бы их поломал

state = config.State(cfg=config.load(owner_id=OWNER_ID))
state.stats = stats_mod.load(config.CONFIG_PATH)


async def notify(user_id: int, text: str, buttons=None) -> None:
    try:
        await bot.send_message(user_id, text, buttons=buttons, link_preview=False)
    except Exception as e:  # noqa: BLE001
        log.warning("notify(%s) failed: %s", user_id, e)


async def notify_owner(text: str) -> None:
    await notify(OWNER_ID, text)


async def extract(text: str) -> dict | None:
    """Один вызов Claude на объявление: только факты, без решения о матче."""
    now = time.time()
    state.api_calls = [t for t in state.api_calls if now - t < 86400]
    state.api_calls.append(now)
    state.stats.bump("claude_extract")

    try:
        resp = await anthropic_client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=400,
            system=config.build_extraction_prompt(state.cfg),
            messages=[{"role": "user", "content": text[:2500]}],
        )
        raw = resp.content[0].text.strip()
        raw = re.sub(r"^```(json)?|```$", "", raw, flags=re.MULTILINE).strip()
        facts = json.loads(raw)
    except Exception as e:  # noqa: BLE001
        log.warning("extract() failed: %s", e)
        return None

    facts["district"] = districts.normalize(facts.get("district"))
    return facts


# ---------------------------------------------------------------------------
# Раздача подписчикам
# ---------------------------------------------------------------------------
async def personal_check(sub, listing_text: str) -> tuple[bool, str]:
    """
    Сверяет объявление со свободным текстом пожеланий подписчика.

    Это ВТОРОЙ вызов Claude и он платный, поэтому вызывается лениво: только
    после того, как объявление прошло структурные фильтры этого человека.
    У кого пожелания не заданы, сюда вообще не попадают.
    """
    state.stats.bump("claude_match")
    try:
        resp = await anthropic_client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=200,
            system=config.build_match_prompt(sub.description),
            messages=[{"role": "user", "content": listing_text[:2500]}],
        )
        raw = resp.content[0].text.strip()
        raw = re.sub(r"^```(json)?|```$", "", raw, flags=re.MULTILINE).strip()
        verdict = json.loads(raw)
    except Exception as e:  # noqa: BLE001
        # Сбой проверки не должен глотать объявление, которое уже подошло
        # по всем измеримым признакам — лучше прислать с оговоркой.
        log.warning("personal_check() failed: %s", e)
        return True, "пожелания проверить не удалось"

    return bool(verdict.get("match")), str(verdict.get("reason") or "")


async def fan_out(facts: dict, source: str, link: str | None, origin: str,
                  listing_text: str = "") -> int:
    """Рассылает объявление всем, чьим фильтрам оно отвечает. Возвращает счёт."""
    sent = 0
    verdicts: list[str] = []
    for sub in state.cfg.active():
        ok, why = users_mod.matches(sub, facts, source)
        if not ok:
            # Именно INFO, а не DEBUG: «почему мне ничего не пришло» — самый
            # частый вопрос к боту, и ответ на него должен быть в логах.
            log.info("  %s: НЕ подходит — %s", sub.label(), why)
            verdicts.append(f"{sub.name or sub.user_id}: нет — {why}")
            continue

        note = ""
        if sub.description and listing_text:
            ok, reason = await personal_check(sub, listing_text)
            if not ok:
                log.info("  %s: НЕ подходит по пожеланиям — %s", sub.label(), reason)
                verdicts.append(f"{sub.name or sub.user_id}: нет по пожеланиям — {reason}")
                continue
            note = reason

        price = facts.get("price_usd")
        parts = [
            "🔥 Новое подходящее объявление",
            "",
            f"Источник: {origin}",
            facts.get("summary") or "",
            f"Цена: {price if price is not None else '?'}$"
            f" | Спален: {facts.get('bedrooms') or '?'}"
            f" | Комнат: {facts.get('rooms') or '?'}"
            f" | {facts.get('area_m2') or '?'} м²"
            f" | Район: {facts.get('district') or 'не указан'}",
        ]
        if note:
            parts.append(f"По твоим пожеланиям: {note}")
        if link:
            parts += ["", link]
        await notify(sub.user_id, "\n".join(parts))
        state.stats.bump("notified")
        verdicts.append(f"{sub.name or sub.user_id}: ОТПРАВЛЕНО")
        sent += 1

    if sent == 0:
        state.stats.bump("no_match")
    _remember(facts, origin, link, verdicts)
    return sent


def _remember(facts: dict, origin: str, link: str | None, verdicts: list[str]) -> None:
    """Кольцевой журнал последних разборов — источник ответа для /last."""
    state.recent_verdicts.insert(0, {
        "at": time.time(),
        "origin": origin,
        "link": link,
        "facts": facts,
        "verdicts": verdicts,
    })
    del state.recent_verdicts[LAST_KEPT:]


def max_active_budget() -> int:
    """Самый щедрый бюджет среди активных — граница дешёвого отсева."""
    budgets = [s.budget_usd for s in state.cfg.active()]
    return max(budgets) if budgets else 0


# ---------------------------------------------------------------------------
# Дедупликация по тексту
# ---------------------------------------------------------------------------
_WHITESPACE = re.compile(r"\s+")


def _dedup_key(text: str) -> str:
    return hashlib.sha256(
        _WHITESPACE.sub(" ", text.strip().casefold()).encode()
    ).hexdigest()


def _seen_recently(key: str) -> bool:
    now = time.time()
    for k, ts in list(state.seen.items()):
        if now - ts > DEDUP_TTL:
            del state.seen[k]
    if key in state.seen:
        return True
    state.seen[key] = now
    return False


# ---------------------------------------------------------------------------
# Telegram-чаты
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
    state.stats.bump("tg_seen")

    if cfg.prefilter_enabled:
        try:
            listing, bedroom, seeking = cfg.compiled_prefilter()
        except re.error as e:
            log.warning("битый regex предфильтра, пропускаю его: %s", e)
        else:
            if seeking.search(text) and not listing.search(text):
                return
            if not listing.search(text):
                return
            if not bedroom.search(text):
                return

    state.stats.bump("tg_prefiltered")

    if _seen_recently(_dedup_key(text)):
        state.stats.bump("dup")
        log.info("dup #%s в %s — пропуск", event.id, chat.label())
        return

    log.info("candidate #%s [%s]: %.70s", event.id, chat.label(), text.replace("\n", " "))

    facts = await extract(text)
    if not facts:
        return

    link = chats_mod.message_link(chat, event.id)
    sent = await fan_out(
        facts, users_mod.SOURCE_TELEGRAM, link, chat.label(), listing_text=text
    )
    log.info("-> #%s разослано %s подписчикам", event.id, sent)


# ---------------------------------------------------------------------------
# Опрос ss.ge
#
# Regex-предфильтр сюда не применяется — он про русские сообщения из чата, а
# описания на ss.ge грузинские. Дешёвый отсев здесь по цене: сайт отдаёт её
# сразу в долларах. Порог — самый щедрый бюджет среди активных подписчиков.
# ---------------------------------------------------------------------------
async def _handle_ssge_listing(listing: ssge.Listing) -> None:
    ceiling = max_active_budget()
    if ceiling and listing.price_usd and listing.price_usd > ceiling:
        state.stats.bump("ssge_price_cut")
        log.info(
            "ss.ge #%s: %s$ дороже самого щедрого бюджета %s$ — пропуск",
            listing.id, listing.price_usd, ceiling,
        )
        return

    log.info(
        "ss.ge candidate #%s: %s$ %sм² %s",
        listing.id, listing.price_usd, listing.area_m2, listing.location(),
    )

    facts = await extract(listing.as_prompt())
    if not facts:
        return

    # Структурные поля сайта точнее, чем вычитанные из текста.
    if listing.price_usd:
        facts["price_usd"] = listing.price_usd
    if listing.area_m2:
        facts["area_m2"] = listing.area_m2
    # Число спален сайт отдаёт полем, а не прозой — ему доверия больше, чем
    # тому, как модель прочитала грузинский заголовок.
    if listing.bedrooms is not None:
        facts["bedrooms"] = listing.bedrooms
    if facts.get("district") is None:
        facts["district"] = districts.normalize(listing.district)

    sent = await fan_out(
        facts, users_mod.SOURCE_SSGE, listing.url, "ss.ge",
        listing_text=listing.as_prompt(),
    )
    log.info("-> ss.ge #%s разослано %s подписчикам", listing.id, sent)


async def poll_ssge() -> None:
    """Опрашивает ss.ge, пока жив процесс. Ошибки сети не должны его ронять."""
    seen_ids = config.load_seen_ids()
    seen = set(seen_ids)
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
                rooms=state.cfg.ssge_rooms(),
            )
            state.ssge_last_error = None
        except (ssge.SsgeError, httpx.HTTPError, asyncio.TimeoutError) as e:
            state.ssge_last_error = str(e)
            log.warning("ss.ge: опрос не удался: %s", e)
            await asyncio.sleep(max(60, int(s["poll_minutes"]) * 60))
            continue

        state.stats.bump("ssge_fetched", len(listings))
        fresh = [l for l in listings if l.id not in seen]
        for listing in fresh:
            seen.add(listing.id)
            seen_ids.insert(0, listing.id)
        config.save_seen_ids(seen_ids)

        state.stats.bump("ssge_new", len(fresh))
        state.ssge_last_poll = time.time()
        state.ssge_last_new = len(fresh)
        state.ssge_seen_count = len(seen)

        if priming:
            priming = False
            log.info("ss.ge: запомнил %s объявлений, слежу за новыми", len(listings))
            await notify_owner(
                f"🌐 ss.ge подключён: проиндексировано {len(listings)} объявлений.\n"
                "Уведомления пойдут только про новые."
            )
        else:
            log.info("ss.ge: получено %s, новых %s", len(listings), len(fresh))
            for listing in fresh:
                await _handle_ssge_listing(listing)

        stats_mod.save(config.CONFIG_PATH, state.stats)
        await asyncio.sleep(int(s["poll_minutes"]) * 60)


async def resolve_pending() -> list[str]:
    """Доставляет peer_id чатам, добавленным без него."""
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
    commands.register(bot, user_client, state, OWNER_ID)

    cfg = state.cfg
    active = [c for c in cfg.chats if c.peer_id is not None]
    listed = ", ".join(c.label() for c in active) or "ни одного"
    log.info(
        "Запущен. Чаты: %s | ss.ge rooms=%s | подписчиков: %s",
        listed, cfg.ssge_rooms(), len(cfg.active()),
    )
    log.info("Предфильтр чатов собран из кнопок: %s", cfg.bedroom_regex())

    msg = (
        "✅ Мониторинг запущен.\n"
        f"Чаты: {listed}\n"
        f"ss.ge: {'каждые ' + str(cfg.ssge['poll_minutes']) + ' мин' if cfg.ssge['enabled'] else 'выключен'}\n"
        f"Подписчиков активных: {len(cfg.active())}\n\n"
        "/menu — настройки"
    )
    if problems:
        msg += "\n\n⚠️ Не удалось подключить:\n" + "\n".join(f"• {p}" for p in problems)
    if cfg.pending():
        msg += f"\n\n⏳ Ждут решения: {len(cfg.pending())} — /users"
    await notify_owner(msg)

    await asyncio.gather(
        user_client.run_until_disconnected(),
        bot.run_until_disconnected(),
        poll_ssge(),
    )


if __name__ == "__main__":
    asyncio.run(main())
