"""
Klumba rental-listing monitor.

Listens in real time to a Telegram group (as your own account, via Telethon),
pre-filters new messages with cheap keyword rules, asks Claude (Haiku) to
judge the ones that look like real listings against your criteria, and pushes
a Telegram notification via a bot for every match.

Run once interactively the first time (to log in with your phone number and
the code Telegram sends you) — after that it can run unattended as a service.
See README.md for full setup.
"""

import asyncio
import json
import logging
import os
import re

import httpx
from anthropic import Anthropic
from dotenv import load_dotenv
from telethon import TelegramClient, events

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("klumba-monitor")

# ---------------------------------------------------------------------------
# Config (all from .env — see .env.example)
# ---------------------------------------------------------------------------
API_ID = int(os.environ["TG_API_ID"])
API_HASH = os.environ["TG_API_HASH"]
SESSION_NAME = os.environ.get("TG_SESSION_NAME", "klumba_userbot")

GROUP = os.environ.get("TG_GROUP", "kkklumba")  # group username, no @

BOT_TOKEN = os.environ["NOTIFY_BOT_TOKEN"]
NOTIFY_CHAT_ID = os.environ["NOTIFY_CHAT_ID"]

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-haiku-4-5-20251001")

BUDGET_USD = int(os.environ.get("BUDGET_USD", "700"))
GEL_PER_USD = float(os.environ.get("GEL_PER_USD", "2.6"))

# ---------------------------------------------------------------------------
# Cheap pre-filter so we don't burn API calls on chit-chat / "ищу квартиру" posts
# ---------------------------------------------------------------------------
LISTING_HINT = re.compile(r"сда(ю|ётся|ется|м)\b|в\s*аренду", re.IGNORECASE)
BEDROOM_HINT = re.compile(r"спальн|студ|1[\s\-]*комнат", re.IGNORECASE)
SEEKING_HINT = re.compile(r"\bищу\b|\bсниму\b|\bнужна?\s+квартир", re.IGNORECASE)

anthropic_client = Anthropic(api_key=ANTHROPIC_API_KEY)

SYSTEM_PROMPT = f"""Ты фильтруешь объявления об аренде квартир из тбилисского Telegram-чата "Клумба".

Пользователь ищет: 1-комнатную/1-спальную квартиру в ДОЛГОСРОЧНУЮ аренду (от нескольких месяцев,
не посуточно и не саблет на пару недель), итоговая стоимость в месяц (аренда + коммуналка,
если коммуналка указана явно) не больше ${BUDGET_USD}.

Тебе присылают текст ОДНОГО сообщения из чата. Верни СТРОГО json без markdown-обёртки, вида:
{{"match": true/false, "reason": "коротко по-русски почему да/нет", "price_usd": число_или_null,
"area_m2": число_или_null, "district": "строка_или_null"}}

Правила:
- Если коммуналка указана в лари — переведи в доллары (~{GEL_PER_USD} GEL = 1 USD) и прибавь к аренде
  для итоговой суммы, на основании которой сравниваешь с бюджетом.
- Если это сообщение о ПОИСКЕ жилья ("ищу", "сниму"), а не о СДАЧЕ — match всегда false.
- Если это посуточная/недельная аренда или саблет на пару недель без явного "от N месяцев" — match false.
- Если это не квартира вовсе (комната в общаге, коммерческое помещение и т.п. без явного "1 спальня") — match false.
- Будь придирчив: лучше пропустить сомнительное объявление, чем прислать ложный матч."""


def classify(text: str) -> dict | None:
    try:
        resp = anthropic_client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=250,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": text[:2000]}],
        )
        raw = resp.content[0].text.strip()
        raw = re.sub(r"^```(json)?|```$", "", raw, flags=re.MULTILINE).strip()
        return json.loads(raw)
    except Exception as e:  # noqa: BLE001
        log.warning("classify() failed: %s", e)
        return None


async def notify(text: str) -> None:
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    async with httpx.AsyncClient(timeout=15) as http_client:
        r = await http_client.post(
            url,
            json={
                "chat_id": NOTIFY_CHAT_ID,
                "text": text,
                "disable_web_page_preview": True,
            },
        )
        if r.status_code != 200:
            log.warning("notify() failed: %s %s", r.status_code, r.text)


client = TelegramClient(SESSION_NAME, API_ID, API_HASH)


@client.on(events.NewMessage(chats=GROUP))
async def handler(event) -> None:
    text = event.raw_text or ""
    if not text:
        return
    if SEEKING_HINT.search(text) and not LISTING_HINT.search(text):
        return
    if not LISTING_HINT.search(text):
        return
    if not BEDROOM_HINT.search(text):
        return

    log.info("candidate #%s: %.70s", event.id, text.replace("\n", " "))

    verdict = classify(text)
    if not verdict:
        return

    if verdict.get("match"):
        link = f"https://t.me/{GROUP}/{event.id}"
        msg = (
            "🔥 Новое подходящее объявление в Клумбе\n\n"
            f"{verdict.get('reason', '')}\n"
            f"Цена: {verdict.get('price_usd', '?')}$ | "
            f"Площадь: {verdict.get('area_m2', '?')} м² | "
            f"Район: {verdict.get('district', '?')}\n\n"
            f"{link}"
        )
        await notify(msg)
        log.info("-> notified for #%s", event.id)
    else:
        log.info("-> skip #%s (%s)", event.id, verdict.get("reason", ""))


async def main() -> None:
    await client.start()
    log.info("Logged in. Listening to '%s' for new listings...", GROUP)
    await notify("✅ Мониторинг Клумбы запущен.")
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
