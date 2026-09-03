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
from dataclasses import dataclass, field

import httpx

log = logging.getLogger("klumba-monitor")

BASE = "https://home.ss.ge"
SEARCH_PATH = "/ka/udzravi-qoneba/l/bina/qiravdeba"  # долгосрочная аренда квартир
# Карточку берём в русской локали: сайт сам переводит состояние ремонта и тип
# дома («Новый ремонт», «Новостройка» вместо грузинского), и ссылка в
# уведомлении открывается на понятном языке. Поиск остаётся на /ka/ — там
# локаль ни на что не влияет, а менять рабочий путь без нужды не стоит.
DETAIL_BASE = f"{BASE}/ru/udzravi-qoneba/"

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

# Удобства с карточки объявления: поле в JSON -> как назвать его для Claude.
# ВАЖНО: значение False здесь значит «автор не отметил галочку», а НЕ «этого
# нет». Убедиться легко: у квартиры на 14-м этаже water/electricity/sewage
# приходят false. Поэтому в промпт уходят только отмеченные (true) удобства,
# а про остальные мы честно говорим «неизвестно».
FEATURE_LABELS: dict[str, str] = {
    "balcony": "балкон",
    "elevator": "лифт",
    "furniture": "мебель",
    "airConditioning": "кондиционер",
    "heating": "отопление",
    "hotWater": "горячая вода",
    "naturalGas": "газ",
    "internet": "интернет",
    "wiFi": "Wi-Fi",
    "cableTelevision": "кабельное ТВ",
    "tv": "телевизор",
    "fridge": "холодильник",
    "washingMachine": "стиральная машина",
    "withBuiltInKitchen": "встроенная кухня",
    "glazedWindows": "стеклопакеты",
    "ironDoor": "железная дверь",
    "securityAlarm": "сигнализация",
    "garage": "гараж/парковка",
    "storage": "кладовая",
    "basement": "подвал",
    "withPool": "бассейн",
    "isPetFriendly": "можно с животными",
    "viewOnYard": "окна во двор",
    "viewOnStreet": "окна на улицу",
    "lastFloor": "последний этаж",
}


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

    # Заполняется отдельным запросом к детальной странице (fetch_details).
    # В выдаче поиска этих полей нет — там только описание и цена, из-за чего
    # балкон, мебель и прочее было видно лишь на фото, то есть никому.
    detailed: bool = False
    rooms: int | None = None
    floor: str | None = None
    floors: str | None = None
    condition: str | None = None  # состояние ремонта, как его пишет сайт
    building: str | None = None  # новостройка / старый фонд
    features: list[str] = field(default_factory=list)

    @property
    def url(self) -> str:
        return f"{DETAIL_BASE}{self.slug}" if self.slug else f"{DETAIL_BASE}{self.id}"

    def location(self) -> str:
        parts = [p for p in (self.city, self.district, self.street) if p]
        return ", ".join(parts) or "не указан"

    def as_prompt(self) -> str:
        """Карточка в том виде, в каком её читает Claude."""
        price = f"{self.price_usd}$/мес" if self.price_usd else "не указана"
        lines = [
            "Источник: сайт ss.ge (карточка объявления)",
            f"Заголовок: {self.title}",
            f"Цена по данным сайта: {price}",
            f"Спален: {self.bedrooms if self.bedrooms is not None else '?'}",
        ]
        if self.rooms is not None:
            lines.append(f"Комнат: {self.rooms}")
        lines.append(f"Площадь: {self.area_m2 if self.area_m2 is not None else '?'} м²")
        if self.floor:
            lines.append(f"Этаж: {self.floor}" + (f" из {self.floors}" if self.floors else ""))
        if self.condition:
            lines.append(f"Состояние: {self.condition}")
        if self.building:
            lines.append(f"Тип дома: {self.building}")
        lines.append(f"Район: {self.location()}")

        if self.detailed:
            lines += [
                "",
                "Удобства, отмеченные на сайте: "
                + (", ".join(self.features) if self.features else "ни одного"),
                "Про удобства, которых нет в этом списке, сайт молчит — это"
                " «неизвестно», а не «отсутствует».",
            ]

        lines += ["", f"Описание:\n{self.description}"]
        return "\n".join(lines)


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


def _int(value) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def parse_detail(html: str) -> dict:
    """Достаёт applicationData с детальной страницы объявления."""
    m = _NEXT_DATA.search(html)
    if not m:
        raise SsgeError("на детальной странице нет __NEXT_DATA__")
    try:
        data = json.loads(m.group(1))
        ad = data["props"]["pageProps"]["applicationData"]
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        raise SsgeError(f"неожиданная структура детальной страницы: {e}") from e
    if not isinstance(ad, dict):
        raise SsgeError("applicationData не объект")
    # На несуществующий slug ss.ge отвечает HTTP 200 и карточкой из одних
    # null — не 404. Если это пропустить, объявление получит «удобств ни
    # одного» как утверждение сайта, хотя сайт вообще ничего не сказал.
    if ad.get("applicationId") is None:
        raise SsgeError("карточка пустая (объявление снято или slug неверный)")
    return ad


def apply_detail(listing: Listing, ad: dict) -> None:
    """Переносит поля детальной карточки в объявление. Ничего не затирает пустым."""
    listing.features = [
        label for key, label in FEATURE_LABELS.items() if ad.get(key) is True
    ]
    listing.rooms = _int(ad.get("rooms"))
    listing.floor = str(ad["floor"]).strip() if ad.get("floor") else None
    listing.floors = str(ad["floors"]).strip() if ad.get("floors") else None
    listing.condition = (ad.get("state") or "").strip() or None
    listing.building = (ad.get("realEstateStatus") or "").strip() or None

    if listing.bedrooms is None:
        listing.bedrooms = _int(ad.get("bedrooms"))
    if listing.area_m2 is None:
        listing.area_m2 = _int(ad.get("totalArea"))

    # В русской локали адрес приходит уже по-русски («Исани» вместо «ისანი»).
    # Район отсюда точнее для districts.normalize() и понятнее в уведомлении.
    addr = ad.get("address")
    if isinstance(addr, dict):
        listing.city = addr.get("cityTitle") or listing.city
        listing.district = (
            addr.get("subdistrictTitle") or addr.get("districtTitle") or listing.district
        )
        listing.street = addr.get("streetTitle") or listing.street

    # Описание на детальной странице многоязычное. Русский и английский Claude
    # читает точнее, чем грузинский, поэтому берём их, если автор их заполнил.
    desc = ad.get("description")
    if isinstance(desc, dict):
        for lang in ("ru", "en", "ka"):
            text = (desc.get(lang) or "").strip()
            if text:
                listing.description = text
                break

    listing.detailed = True


async def fetch_details(http: httpx.AsyncClient, listing: Listing) -> bool:
    """
    Догружает карточку объявления: удобства, этаж, состояние, описание.

    Возвращает True, если получилось. Сбой не должен терять объявление —
    лучше отдать Claude то, что уже есть, чем не отдать ничего.
    """
    try:
        r = await http.get(
            listing.url, headers={"User-Agent": USER_AGENT}, follow_redirects=True
        )
        if r.status_code != 200:
            raise SsgeError(f"HTTP {r.status_code} на {listing.url}")
        ad = parse_detail(r.text)
        got = _int(ad.get("applicationId"))
        if got != listing.id:
            raise SsgeError(f"карточка чужая: ждали #{listing.id}, пришло #{got}")
        apply_detail(listing, ad)
        return True
    except (SsgeError, httpx.HTTPError) as e:
        log.warning("ss.ge #%s: не догрузил карточку (%s)", listing.id, e)
        return False


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
