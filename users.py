"""
Подписчики и их персональные фильтры.

Ключевое архитектурное решение: Claude вызывается ОДИН раз на объявление и
только извлекает факты (цена, спальни, комнаты, район, долгосрочность). Сверка
с фильтрами каждого подписчика — обычная арифметика на Python. Поэтому расходы
на API зависят от числа объявлений, а не от числа людей: и с одним человеком,
и с полусотней они одинаковые.

Отсюда же следует, что фильтры структурные (кнопками), а не свободным текстом:
свободный текст нельзя сверить без ещё одного обращения к модели.

Глобальные настройки сбора — regex-предфильтр для чатов и параметр rooms для
ss.ge — выводятся автоматически как ОБЪЕДИНЕНИЕ запросов всех активных
подписчиков. Собираем то, что нужно хоть кому-то, а дальше раздаём по личным
фильтрам.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import districts

ROLE_OWNER = "owner"
ROLE_USER = "user"

STATUS_PENDING = "pending"
STATUS_ACTIVE = "active"
STATUS_DENIED = "denied"

# Варианты для кнопок
BEDROOM_CHOICES = (1, 2, 3)
ROOM_CHOICES = (1, 2, 3, 4)
BUDGET_STEP = 50
BUDGET_MIN = 100
BUDGET_MAX = 5000

# Нижние границы: 0 значит «не ограничено». Именно 0, а не None — так
# арифметика в matches() и в кнопках обходится без проверок на None.
MIN_PRICE_MAX = BUDGET_MAX
AREA_STEP = 5
AREA_MAX = 500

SOURCE_TELEGRAM = "telegram"
SOURCE_SSGE = "ssge"
SOURCES = (SOURCE_TELEGRAM, SOURCE_SSGE)


@dataclass
class Subscriber:
    user_id: int
    name: str = ""
    role: str = ROLE_USER
    status: str = STATUS_PENDING
    paused: bool = False

    budget_usd: int = 700
    # 0 = без нижней границы. Отсекает подвалы и «цена по запросу за 1$».
    min_price_usd: int = 0
    # 0 = без ограничения. Площадь сайт отдаёт полем, из чатов её вычитывает
    # Claude — и часто не находит; ненайденная площадь объявление не режет.
    min_area_m2: int = 0
    bedrooms: list[int] = field(default_factory=lambda: [1])
    rooms: list[int] = field(default_factory=lambda: [1, 2])
    # Пустой список = любой район. Так новый подписчик по умолчанию видит всё.
    district_list: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=lambda: list(SOURCES))
    # Свободный текст пожеланий. Проверяется отдельным вызовом Claude и только
    # для объявлений, уже прошедших структурные фильтры — см. main.personal_check.
    description: str = ""

    @property
    def is_owner(self) -> bool:
        return self.role == ROLE_OWNER

    @property
    def receives(self) -> bool:
        """Идут ли этому человеку уведомления прямо сейчас."""
        return self.status == STATUS_ACTIVE and not self.paused

    def label(self) -> str:
        who = self.name or str(self.user_id)
        return f"{who} ({self.user_id})"

    def price_range(self) -> str:
        if self.min_price_usd:
            return f"{self.min_price_usd}–{self.budget_usd}$"
        return f"до {self.budget_usd}$"

    def area_range(self) -> str:
        return f"от {self.min_area_m2} м²" if self.min_area_m2 else "любая"

    def summary(self) -> str:
        d = ", ".join(self.district_list) if self.district_list else "любые"
        src = ", ".join(self.sources) if self.sources else "НЕТ (уведомлений не будет)"
        out = (
            f"Цена: {self.price_range()}\n"
            f"Площадь: {self.area_range()}\n"
            f"Спален: {_nums(self.bedrooms)}\n"
            f"Комнат: {_nums(self.rooms)}\n"
            f"Районы: {d}\n"
            f"Источники: {src}"
        )
        if self.description:
            out += f"\nПожелания: {self.description}"
        return out

    def to_dict(self) -> dict:
        return {
            "user_id": self.user_id,
            "name": self.name,
            "role": self.role,
            "status": self.status,
            "paused": self.paused,
            "budget_usd": self.budget_usd,
            "min_price_usd": self.min_price_usd,
            "min_area_m2": self.min_area_m2,
            "bedrooms": self.bedrooms,
            "rooms": self.rooms,
            "district_list": self.district_list,
            "sources": self.sources,
            "description": self.description,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> Subscriber:
        return cls(
            user_id=int(raw["user_id"]),
            name=raw.get("name") or "",
            role=raw.get("role") or ROLE_USER,
            status=raw.get("status") or STATUS_PENDING,
            paused=bool(raw.get("paused", False)),
            budget_usd=int(raw.get("budget_usd", 700)),
            # Старые конфиги этих полей не знают — отсутствие значит «не задано».
            min_price_usd=int(raw.get("min_price_usd") or 0),
            min_area_m2=int(raw.get("min_area_m2") or 0),
            bedrooms=[int(b) for b in raw.get("bedrooms") or [1]],
            rooms=[int(r) for r in raw.get("rooms") or [1, 2]],
            district_list=list(raw.get("district_list") or []),
            sources=list(raw.get("sources") or SOURCES),
            description=raw.get("description") or "",
        )


def _nums(values: list[int]) -> str:
    return ", ".join(str(v) for v in sorted(values)) if values else "любое"


# ---------------------------------------------------------------------------
# Сверка извлечённых фактов с фильтрами подписчика
# ---------------------------------------------------------------------------
def matches(sub: Subscriber, facts: dict, source: str) -> tuple[bool, str]:
    """
    Подходит ли объявление подписчику. Возвращает (да/нет, причина отказа).

    Неизвестные факты трактуются в пользу объявления: лучше показать лишнее,
    чем молча потерять подходящее из-за того, что в тексте не было цифры.
    """
    if source not in sub.sources:
        return False, f"источник {source} отключён"

    if not facts.get("is_rental_offer", True):
        return False, "не объявление о сдаче"
    if not facts.get("is_long_term", True):
        return False, "не долгосрочная аренда"

    price = facts.get("price_usd")
    if isinstance(price, (int, float)) and price > sub.budget_usd:
        return False, f"{int(price)}$ дороже бюджета {sub.budget_usd}$"
    if isinstance(price, (int, float)) and sub.min_price_usd and price < sub.min_price_usd:
        return False, f"{int(price)}$ дешевле нижней границы {sub.min_price_usd}$"

    area = facts.get("area_m2")
    if isinstance(area, (int, float)) and sub.min_area_m2 and area < sub.min_area_m2:
        return False, f"{int(area)} м² меньше {sub.min_area_m2} м²"

    beds = facts.get("bedrooms")
    if isinstance(beds, int) and sub.bedrooms:
        # 3 в фильтре означает «три и больше»
        wanted = max(sub.bedrooms)
        if beds not in sub.bedrooms and not (wanted >= max(BEDROOM_CHOICES) and beds >= wanted):
            return False, f"спален {beds}, нужно {_nums(sub.bedrooms)}"

    rooms = facts.get("rooms")
    if isinstance(rooms, int) and sub.rooms:
        wanted = max(sub.rooms)
        if rooms not in sub.rooms and not (wanted >= max(ROOM_CHOICES) and rooms >= wanted):
            return False, f"комнат {rooms}, нужно {_nums(sub.rooms)}"

    if sub.district_list:
        canon = districts.normalize(facts.get("district"))
        if canon is not None and canon not in sub.district_list:
            return False, f"район {canon} не в списке"

    return True, ""


# ---------------------------------------------------------------------------
# Глобальные настройки сбора = объединение запросов всех активных подписчиков
# ---------------------------------------------------------------------------
def union_rooms(subs: list[Subscriber]) -> list[int]:
    out: set[int] = set()
    for s in subs:
        if s.receives:
            out.update(s.rooms)
    return sorted(out) or [1, 2]


def union_bedrooms(subs: list[Subscriber]) -> list[int]:
    out: set[int] = set()
    for s in subs:
        if s.receives:
            out.update(s.bedrooms)
    return sorted(out) or [1]


def ssge_rooms_param(subs: list[Subscriber]) -> str:
    """Значение для /ssge rooms — то, что нужно хоть одному подписчику."""
    return ",".join(str(r) for r in union_rooms(subs))


# Числительные словами: в чатах пишут «двухкомнатная» не реже, чем «2-комнатная».
_WORD_ROOMS = {
    1: ("одно", "1"),
    2: ("двух", "2"),
    3: ("трёх", "трех", "3"),
    4: ("четырёх", "четырех", "4"),
}


def build_bedroom_regex(subs: list[Subscriber]) -> str:
    """
    Собирает regex-предфильтр для чатов из кнопочных настроек подписчиков.

    Пользователь не пишет регулярки руками — он тыкает «1 спальня», «2 комнаты»,
    а это превращается в шаблон, отсекающий явно чужие планировки до вызова
    Claude. Предфильтр намеренно щедрый: его задача убрать болтовню, а не
    заменить собой точный разбор.
    """
    rooms = union_rooms(subs)
    beds = union_bedrooms(subs)

    parts = ["спальн", "студи", "апартамент"]
    for n in rooms:
        for word in _WORD_ROOMS.get(n, (str(n),)):
            if word.isdigit():
                parts.append(rf"{word}[\s\-]*(комнат|ком\b)")
            else:
                parts.append(rf"{word}[\s\-]*комнат")
    for n in beds:
        parts.append(rf"{n}[\s\-]*спальн")

    # dict.fromkeys — уникальные значения с сохранением порядка
    return "|".join(dict.fromkeys(parts))
