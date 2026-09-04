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
# Ключи, привязанные к личности (identity-linked), требуют указывать рабочее
# пространство отдельным заголовком, иначе API отвечает 400. Для обычных
# ключей переменная не нужна и заголовок не отправляется.
ANTHROPIC_WORKSPACE_ID = os.environ.get("ANTHROPIC_WORKSPACE_ID", "").strip()

DEDUP_TTL = 24 * 3600
LAST_KEPT = 15  # сколько последних разборов помнить для /last

anthropic_client = AsyncAnthropic(
    api_key=ANTHROPIC_API_KEY,
    default_headers=(
        {"anthropic-workspace-id": ANTHROPIC_WORKSPACE_ID}
        if ANTHROPIC_WORKSPACE_ID
        else None
    ),
)

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
        # Считаем отказы отдельно от успехов. Раньше счётчик рос до запроса, и
        # сплошные отказы выглядели как успешная работа: «разборы есть, а
        # уведомлений нет» — без единой подсказки, что виноват API.
        state.stats.bump("claude_failed")
        first = state.last_claude_error is None
        state.last_claude_error = f"{type(e).__name__}: {e}"
        log.warning("extract() failed: %s", state.last_claude_error)
        # Один раз на серию: молчащий бот, который «вроде работает», — худшее,
        # что тут может быть. Спам при этом не нужен, поэтому только на переходе.
        if first:
            await notify_owner(
                "⚠️ Claude отвечает ошибкой, объявления не разбираются:\n\n"
                f"{state.last_claude_error[:300]}\n\n"
                "Сбор продолжается. Проверь ключ и баланс — /stats покажет счётчик."
            )
        return None

    state.stats.bump("claude_extract")
    state.last_claude_error = None
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
        state.stats.bump("claude_failed")
        state.last_claude_error = f"{type(e).__name__}: {e}"
        log.warning("personal_check() failed: %s", state.last_claude_error)
        return True, "пожелания проверить не удалось"

    state.stats.bump("claude_match")
    return bool(verdict.get("match")), str(verdict.get("reason") or "")


async def fan_out(facts: dict, source: str, link: str | None, origin: str,
                  listing_text: str = "") -> int:
    """Рассылает объявление всем, чьим фильтрам оно отвечает. Возвращает счёт."""
    # Страховка на случай, если объявление всё же добралось сюда при опущенном
    # стоп-кране: наверх по стеку он уже проверен, но рассылка — последний
    # рубеж, и лучше ей знать про стоп самой.
    if state.cfg.stopped:
        return 0
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
        if sub.description and not state.cfg.claude_enabled:
            # Пожелания — свободный текст, сверить их без модели нечем. Не
            # проверяем, но и не делаем вид, что проверили.
            note = "⚠️ не проверялись, Claude выключен"
        elif sub.description and listing_text:
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


def min_active_price() -> int:
    """
    Самая низкая нижняя граница цены среди активных.

    Дешевле неё объявление не нужно НИКОМУ, поэтому его можно отбросить до
    вызова Claude. Если хоть у одного человека границы нет (0), отсев не
    работает — и это правильно, ему такие объявления нужны.
    """
    subs = state.cfg.active()
    return min((s.min_price_usd for s in subs), default=0) if subs else 0


def min_active_area() -> int:
    """То же для площади: меньше неё объявление не подходит никому."""
    subs = state.cfg.active()
    return min((s.min_area_m2 for s in subs), default=0) if subs else 0


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
    # Стоп-кран проверяется до всего остального: ни счётчиков, ни дедупликации,
    # ни тем более обращений к Claude. Остановлено значит остановлено.
    if cfg.stopped:
        return
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

    if state.cfg.claude_enabled:
        facts = await extract(text)
        if not facts:
            return
    else:
        # Из сообщения в чате без Claude не достать ни цены, ни района —
        # только сам факт, что оно похоже на объявление. Отдаём как есть,
        # пометив, что не разобрано: молча проглотить было бы хуже, а тихо
        # выдать за разобранное — совсем плохо.
        facts = {
            "is_rental_offer": True, "is_long_term": True,
            "price_usd": None, "bedrooms": None, "rooms": None,
            "area_m2": None, "district": None,
            "summary": "⚠️ Не разобрано (Claude выключен):\n" + text[:600],
        }
        state.stats.bump("no_claude")

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
async def _handle_ssge_listing(
    listing: ssge.Listing, http: httpx.AsyncClient | None = None
) -> None:
    ceiling = max_active_budget()
    if ceiling and listing.price_usd and listing.price_usd > ceiling:
        state.stats.bump("ssge_price_cut")
        log.info(
            "ss.ge #%s: %s$ дороже самого щедрого бюджета %s$ — пропуск",
            listing.id, listing.price_usd, ceiling,
        )
        return

    floor = min_active_price()
    if floor and listing.price_usd and listing.price_usd < floor:
        state.stats.bump("ssge_price_cut")
        log.info(
            "ss.ge #%s: %s$ дешевле нижней границы всех подписчиков %s$ — пропуск",
            listing.id, listing.price_usd, floor,
        )
        return

    small = min_active_area()
    if small and listing.area_m2 and listing.area_m2 < small:
        state.stats.bump("ssge_area_cut")
        log.info(
            "ss.ge #%s: %sм² меньше нужного всем %sм² — пропуск",
            listing.id, listing.area_m2, small,
        )
        return

    log.info(
        "ss.ge candidate #%s: %s$ %sм² %s",
        listing.id, listing.price_usd, listing.area_m2, listing.location(),
    )

    # Догружаем карточку только для тех, кто прошёл по цене: в выдаче поиска
    # нет ни удобств, ни этажа, ни русского описания — а именно про них люди и
    # пишут в пожеланиях («нужен балкон», «с мебелью»).
    if http is not None and await ssge.fetch_details(http, listing):
        state.stats.bump("ssge_detail")
        log.info(
            "ss.ge #%s: удобства — %s",
            listing.id, ", ".join(listing.features) or "ни одного не отмечено",
        )

    if state.cfg.claude_enabled:
        facts = await extract(listing.as_prompt())
        if not facts:
            return
    else:
        # Без Claude ss.ge почти ничего не теряет: цену, спальни, площадь и
        # район сайт отдаёт полями, а долгосрочность следует из адреса выдачи.
        # Все фильтры работают как обычно, только бесплатно.
        facts = listing.as_facts()
        # Тот же шаг, что делает extract(): фильтр по районам сверяется с
        # каноном, а сайт отдаёт «Диди Дигоми» или «ვაკე».
        facts["district"] = districts.normalize(facts["district"])
        state.stats.bump("no_claude")

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
    # Переиндексаций может быть несколько (после каждого стопа), но «ss.ge
    # подключён» уместно сказать один раз — при самом первом запуске.
    first_prime = priming
    state.ssge_seen_count = len(seen)

    while True:
        s = state.cfg.ssge
        # Пока стоп-кран опущен — ни одного запроса к сайту.
        if state.cfg.stopped:
            await asyncio.sleep(30)
            continue
        # Возобновились после стопа: первый проход только переиндексирует.
        # Иначе он посчитал бы новым всё, что накопилось за простой, и вывалил
        # бы пачкой. Флаг снимаем здесь же и сразу пишем на диск — если процесс
        # умрёт между переиндексацией и сохранением, повторная ничего не
        # испортит, а вот потерянный флаг обернулся бы той самой пачкой.
        if state.cfg.reprime_pending:
            priming = True
            state.cfg.reprime_pending = False
            await config.save(state.cfg)
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
            if first_prime:
                first_prime = False
                await notify_owner(
                    f"🌐 ss.ge подключён: проиндексировано {len(listings)} объявлений.\n"
                    "Уведомления пойдут только про новые."
                )
            elif fresh:
                # Возобновление после стопа. Про накопившееся молчим, но честно
                # говорим сколько его было — чтобы это не выглядело пропажей.
                await notify_owner(
                    f"🌐 ss.ge переиндексирован: за простой набралось "
                    f"{len(fresh)} объявлений, их не присылаю.\n"
                    "Дальше — только про новые."
                )
        else:
            log.info("ss.ge: получено %s, новых %s", len(listings), len(fresh))
            # Один клиент на весь проход: карточек за раз бывает много, а
            # каждое новое соединение — лишнее TLS-рукопожатие к ss.ge.
            async with httpx.AsyncClient(timeout=30) as http:
                for listing in fresh:
                    await _handle_ssge_listing(listing, http)

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
