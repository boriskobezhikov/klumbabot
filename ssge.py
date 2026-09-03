"""
Мониторинг объявлений об аренде на ss.ge.

В отличие от Telegram здесь нет живой подписки — сайт опрашивается по таймеру.
Но парсить вёрстку не приходится: ss.ge рендерится на сервере и кладёт готовый
JSON со всеми объявлениями страницы в <script id="__NEXT_DATA__">, который сам
же использует для гидратации React. Мы читаем этот JSON — он переживает
редизайны куда лучше, чем CSS-селекторы, и не требует headless-браузера.

Объявления приходят структурированными (цена сразу в USD, спальни, площадь,
район), поэтому по бюджету отсекаем арифметикой, до обращения к Claude.
Regex-предфильтр к этому источнику НЕ применяется: он написан под русские
сообщения из чата, а описания на ss.ge на грузинском — он отсёк бы всё.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass

import httpx

log = logging.getLogger("klumba-monitor")

BASE = "https://home.ss.ge"
SEARCH_PATH = "/ka/udzravi-qoneba/l/bina/qiravdeba"  # долгосрочная аренда квартир
DETAIL_BASE = f"{BASE}/ka/udzravi-qoneba/"

# Посуточная аренда живёт на отдельном пути (.../qiravdeba-dgiurad), так что
# сюда она не попадает — но Claude всё равно перепроверяет по тексту.
ORDER_NEWEST_FIRST = 1
CITY_TBILISI = 95

# Без него ss.ge отдаёт редирект вместо страницы.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

_NEXT_DATA = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)


class SsgeError(Exception):
    """Сайт ответил не тем, чего мы ждём — обычно смена структуры страницы."""


@dataclass
class Listing:
    id: int
    price_usd: int | None
    bedrooms: int | None
    area_m2: int | None
    city: str | None
    district: str | None
    street: str | None
    title: str
    description: str
    order_date: str | None
    slug: str = ""

    @property
    def url(self) -> str:
        return f"{DETAIL_BASE}{self.slug}" if self.slug else f"{DETAIL_BASE}{self.id}"

    def location(self) -> str:
        parts = [p for p in (self.city, self.district, self.street) if p]
        return ", ".join(parts) or "не указан"

    def as_prompt(self) -> str:
        """Карточка в том виде, в каком её читает Claude."""
        price = f"{self.price_usd}$/мес" if self.price_usd else "не указана"
        return (
            "Источник: сайт ss.ge (карточка объявления)\n"
            f"Заголовок: {self.title}\n"
            f"Цена по данным сайта: {price}\n"
            f"Спален: {self.bedrooms if self.bedrooms is not None else '?'}\n"
            f"Площадь: {self.area_m2 if self.area_m2 is not None else '?'} м²\n"
            f"Район: {self.location()}\n\n"
            f"Описание:\n{self.description}"
        )


def build_url(page: int, city_id: int, rooms: str) -> str:
    return (
        f"{BASE}{SEARCH_PATH}"
        f"?cityIdList={city_id}&rooms={rooms}"
        f"&page={page}&order={ORDER_NEWEST_FIRST}"
    )


def parse(html: str) -> list[Listing]:
    """Достаёт объявления из __NEXT_DATA__. Кидает SsgeError, если структура ушла."""
    m = _NEXT_DATA.search(html)
    if not m:
        raise SsgeError("на странице нет __NEXT_DATA__ (вёрстка изменилась?)")

    try:
        data = json.loads(m.group(1))
        items = data["props"]["pageProps"]["applicationList"]["realStateItemModel"]
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        raise SsgeError(f"неожиданная структура данных: {e}") from e

    listings = []
    for it in items:
        try:
            listings.append(_to_listing(it))
        except (KeyError, TypeError) as e:  # noqa: PERF203
            log.warning("ss.ge: пропускаю объявление, кривые поля: %s", e)
    return listings


def _to_listing(it: dict) -> Listing:
    addr = it.get("address") or {}
    price = it.get("price") or {}

    return Listing(
        id=int(it["applicationId"]),
        price_usd=price.get("priceUsd") or None,
        bedrooms=it.get("numberOfBedrooms"),
        area_m2=it.get("totalArea"),
        city=addr.get("cityTitle"),
        district=addr.get("subdistrictTitle") or addr.get("districtTitle"),
        street=addr.get("streetTitle"),
        title=(it.get("title") or "").strip(),
        description=(it.get("description") or "").strip(),
        order_date=it.get("orderDate"),
        slug=it.get("detailUrl") or "",
    )


async def fetch_page(http: httpx.AsyncClient, url: str) -> list[Listing]:
    r = await http.get(url, headers={"User-Agent": USER_AGENT}, follow_redirects=True)
    if r.status_code != 200:
        raise SsgeError(f"HTTP {r.status_code} на {url}")
    return parse(r.text)


async def fetch_listings(pages: int, city_id: int, rooms: str) -> list[Listing]:
    """
    Обходит первые `pages` страниц выдачи, отсортированной по свежести.

    По каждому числу комнат ходим ОТДЕЛЬНЫМ запросом. Сайт принимает и
    `rooms=1,2`, но серверный рендер такой фильтр молча игнорирует и отдаёт
    вообще всё подряд (мультивыбор у них доезжает только клиентским
    дозапросом, а мы читаем именно серверный HTML). Одиночное значение
    фильтруется корректно, поэтому объединяем результаты сами.
    """
    values = [r.strip() for r in rooms.split(",") if r.strip()] or ["1"]

    out: list[Listing] = []
    seen_ids: set[int] = set()
    first = True

    async with httpx.AsyncClient(timeout=30) as http:
        for rooms_value in values:
            for page in range(1, pages + 1):
                if not first:
                    await asyncio.sleep(1)  # не долбим сайт очередью запросов
                first = False
                url = build_url(page, city_id, rooms_value)
                for listing in await fetch_page(http, url):
                    if listing.id not in seen_ids:
                        seen_ids.add(listing.id)
                        out.append(listing)

    return out
