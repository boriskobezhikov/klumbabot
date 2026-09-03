"""
Конфигурация: общие настройки сбора + подписчики с личными фильтрами.

Что где лежит:
  .env         — секреты (токены, ключи). Меняются редко, руками.
  config.json  — всё остальное. Единственный источник правды, правится ботом.

Общее (владелец): список чатов, параметры опроса ss.ge, курс лари.
Личное (каждый): бюджет, спальни, комнаты, районы, источники.

Предфильтр для чатов и параметр rooms для ss.ge здесь НЕ хранятся — они
выводятся из объединения фильтров подписчиков (см. users.py). Иначе они бы
разъезжались с кнопками, которые люди нажимают.
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import districts
import stats as stats_mod
import users as users_mod
from users import Subscriber

log = logging.getLogger("klumba-monitor")

CONFIG_PATH = Path(
    os.environ.get("KLUMBA_CONFIG") or Path(__file__).with_name("config.json")
)

CONFIG_VERSION = 2

DEFAULT_SSGE = {
    "enabled": True,
    "poll_minutes": 10,
    "pages": 2,
    "city_id": 95,  # Тбилиси
}


@dataclass
class ChatRef:
    """Один отслеживаемый чат. peer_id=None означает «ещё не зарезолвлен»."""

    ref: str
    peer_id: int | None = None
    title: str | None = None
    username: str | None = None

    def label(self) -> str:
        name = self.title or self.username or self.ref
        if self.username:
            return f"{name} (@{self.username})"
        if self.peer_id is None:
            return f"{name} (не подключён)"
        return f"{name} (id {self.peer_id})"


@dataclass
class Config:
    gel_per_usd: float = 2.6
    prefilter_enabled: bool = True
    chats: list[ChatRef] = field(default_factory=list)
    ssge: dict = field(default_factory=lambda: dict(DEFAULT_SSGE))
    subscribers: list[Subscriber] = field(default_factory=list)
    # Текст критериев из однопользовательской версии. Больше не участвует в
    # фильтрации (её заменили кнопки), но не выбрасываем молча.
    legacy_criteria: str = ""

    # -- подписчики --------------------------------------------------------
    def user(self, user_id: int) -> Subscriber | None:
        for s in self.subscribers:
            if s.user_id == user_id:
                return s
        return None

    def owner(self) -> Subscriber | None:
        for s in self.subscribers:
            if s.is_owner:
                return s
        return None

    def active(self) -> list[Subscriber]:
        return [s for s in self.subscribers if s.receives]

    def pending(self) -> list[Subscriber]:
        return [s for s in self.subscribers if s.status == users_mod.STATUS_PENDING]

    # -- производные настройки сбора --------------------------------------
    def bedroom_regex(self) -> str:
        return users_mod.build_bedroom_regex(self.subscribers)

    def ssge_rooms(self) -> str:
        return users_mod.ssge_rooms_param(self.subscribers)

    def compiled_prefilter(self) -> tuple[re.Pattern, re.Pattern, re.Pattern]:
        return _compile_prefilter(self.bedroom_regex())

    # -- чаты --------------------------------------------------------------
    def chat_by_peer_id(self, peer_id: int) -> ChatRef | None:
        for chat in self.chats:
            if chat.peer_id == peer_id:
                return chat
        return None

    def chat_by_ref(self, ref: str) -> ChatRef | None:
        needle = ref.strip().lstrip("@").casefold()
        for chat in self.chats:
            candidates = {chat.ref.lstrip("@").casefold()}
            if chat.username:
                candidates.add(chat.username.casefold())
            if chat.peer_id is not None:
                candidates.add(str(chat.peer_id))
            if needle in candidates:
                return chat
        return None

    # -- сериализация ------------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "version": CONFIG_VERSION,
            "gel_per_usd": self.gel_per_usd,
            "prefilter_enabled": self.prefilter_enabled,
            "chats": [c.__dict__ for c in self.chats],
            "ssge": self.ssge,
            "users": [s.to_dict() for s in self.subscribers],
            "legacy_criteria": self.legacy_criteria,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> Config:
        ssge = dict(DEFAULT_SSGE)
        ssge.update(raw.get("ssge") or {})
        ssge.pop("rooms", None)  # переехало к подписчикам

        return cls(
            gel_per_usd=float(raw.get("gel_per_usd", 2.6)),
            prefilter_enabled=bool(raw.get("prefilter_enabled", True)),
            chats=[ChatRef(**c) for c in raw.get("chats") or []],
            ssge=ssge,
            subscribers=[Subscriber.from_dict(u) for u in raw.get("users") or []],
            legacy_criteria=raw.get("legacy_criteria") or "",
        )


@dataclass
class State:
    """Общее изменяемое состояние процесса."""

    cfg: Config
    api_calls: list[float] = field(default_factory=list)
    seen: dict[str, float] = field(default_factory=dict)
    # Настоящий объект по умолчанию: счётчики не должны требовать инициализации
    # снаружи, иначе любой путь до неё падал бы на None.
    stats: stats_mod.Stats = field(default_factory=stats_mod.Stats)

    ssge_last_poll: float | None = None
    ssge_last_new: int = 0
    ssge_seen_count: int = 0
    ssge_last_error: str | None = None
    last_claude_error: str | None = None

    # user_id -> чего ждём от следующего сообщения (сейчас только "description")
    awaiting: dict[int, str] = field(default_factory=dict)
    # Последние разборы с вердиктом по каждому подписчику — для /last
    recent_verdicts: list[dict] = field(default_factory=list)


@functools.lru_cache(maxsize=8)
def _compile_prefilter(bedroom: str):
    """
    Три регулярки предфильтра. Признак сдачи и признак поиска — фиксированные
    (они про глагол, а не про планировку), а bedroom собирается из кнопок.
    """
    listing = r"сда(ю|ётся|ется|м|дим)\b|в\s*аренду|сниму\s+не|for\s+rent|rent\s+out"
    seeking = r"\bищу\b|\bсниму\b|\bнужна?\s+квартир|\bразыскива"
    return (
        re.compile(listing, re.IGNORECASE),
        re.compile(bedroom, re.IGNORECASE),
        re.compile(seeking, re.IGNORECASE),
    )


# ---------------------------------------------------------------------------
# Загрузка / сохранение
# ---------------------------------------------------------------------------
_save_lock = asyncio.Lock()


def load(owner_id: int | None = None) -> Config:
    """
    Читает config.json. Если файла нет — создаёт. Если файл старого формата —
    мигрирует, сохраняя настройки владельца.
    """
    if CONFIG_PATH.exists():
        with CONFIG_PATH.open(encoding="utf-8") as f:
            raw = json.load(f)
        cfg = _migrate(raw, owner_id) if raw.get("version") != CONFIG_VERSION else Config.from_dict(raw)
        if owner_id is not None:
            _ensure_owner(cfg, owner_id)
        _write(cfg)
        return cfg

    cfg = Config(gel_per_usd=float(os.environ.get("GEL_PER_USD", "2.6")))
    legacy_group = os.environ.get("TG_GROUP", "").strip()
    if legacy_group:
        cfg.chats.append(ChatRef(ref=legacy_group))
    if owner_id is not None:
        _ensure_owner(cfg, owner_id)
    _write(cfg)
    log.info("Конфиг создан: %s", CONFIG_PATH)
    return cfg


def _migrate(raw: dict, owner_id: int | None) -> Config:
    """Переносит однопользовательский конфиг (v1) в многопользовательский."""
    log.info("Мигрирую config.json из старого формата в v%s", CONFIG_VERSION)

    ssge = dict(DEFAULT_SSGE)
    old_ssge = raw.get("ssge") or {}
    for k in ("enabled", "poll_minutes", "pages", "city_id"):
        if k in old_ssge:
            ssge[k] = old_ssge[k]

    cfg = Config(
        gel_per_usd=float(raw.get("gel_per_usd", 2.6)),
        prefilter_enabled=bool((raw.get("prefilter") or {}).get("enabled", True)),
        chats=[ChatRef(**c) for c in raw.get("chats") or []],
        ssge=ssge,
        legacy_criteria=raw.get("criteria") or "",
    )

    # Настройки владельца переезжают в его личный профиль.
    if owner_id is not None:
        old_rooms = str(old_ssge.get("rooms") or "1,2")
        rooms = [int(r) for r in old_rooms.split(",") if r.strip().isdigit()] or [1, 2]
        cfg.subscribers.append(
            Subscriber(
                user_id=owner_id,
                role=users_mod.ROLE_OWNER,
                status=users_mod.STATUS_ACTIVE,
                budget_usd=int(raw.get("budget_usd", 700)),
                rooms=rooms,
                bedrooms=[1],
            )
        )
        log.info(
            "Настройки владельца перенесены: бюджет %s$, комнаты %s",
            raw.get("budget_usd", 700), old_rooms,
        )
    if cfg.legacy_criteria:
        log.info(
            "Текстовые критерии сохранены в legacy_criteria, но больше не "
            "применяются — фильтрация теперь кнопками"
        )
    return cfg


def _ensure_owner(cfg: Config, owner_id: int) -> None:
    """Владелец всегда существует и всегда активен."""
    sub = cfg.user(owner_id)
    if sub is None:
        cfg.subscribers.insert(
            0,
            Subscriber(
                user_id=owner_id,
                role=users_mod.ROLE_OWNER,
                status=users_mod.STATUS_ACTIVE,
            ),
        )
        log.info("Владелец %s добавлен в подписчики", owner_id)
        return
    sub.role = users_mod.ROLE_OWNER
    sub.status = users_mod.STATUS_ACTIVE


def _write(cfg: Config) -> None:
    tmp = CONFIG_PATH.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(cfg.to_dict(), f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG_PATH)


async def save(cfg: Config) -> None:
    async with _save_lock:
        _write(cfg)


# ---------------------------------------------------------------------------
# Просмотренные объявления ss.ge (переживают перезапуск)
# ---------------------------------------------------------------------------
SEEN_PATH = CONFIG_PATH.with_name("ssge_seen.json")
SEEN_LIMIT = 5000


def load_seen_ids() -> list[int]:
    if not SEEN_PATH.exists():
        return []
    try:
        with SEEN_PATH.open(encoding="utf-8") as f:
            return [int(i) for i in json.load(f).get("ids", [])]
    except (json.JSONDecodeError, ValueError, TypeError) as e:
        log.warning("ssge_seen.json битый (%s) — начинаю с чистого списка", e)
        return []


def save_seen_ids(ids: list[int]) -> None:
    tmp = SEEN_PATH.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump({"ids": ids[:SEEN_LIMIT]}, f)
    os.replace(tmp, SEEN_PATH)


# ---------------------------------------------------------------------------
# Промпт: Claude только ИЗВЛЕКАЕТ факты, решение принимает код.
#
# Так вызов один на объявление независимо от числа подписчиков. Никаких
# «подходит/не подходит» — у каждого свой бюджет и свои районы, и сравнивать
# их арифметикой дешевле и предсказуемее, чем спрашивать модель.
# ---------------------------------------------------------------------------
def build_extraction_prompt(cfg: Config) -> str:
    return f"""Ты разбираешь объявления об аренде жилья в Тбилиси — из Telegram-чатов и с сайта ss.ge.
Твоя задача — ИЗВЛЕЧЬ ФАКТЫ. Не решай, подходит ли объявление кому-то: это делает код.

Тебе присылают ОДНО объявление. Описания с ss.ge бывают на грузинском —
переводить не нужно, просто пойми смысл. Верни СТРОГО json без markdown-обёртки:

{{"is_rental_offer": true/false,
  "is_long_term": true/false,
  "price_usd": число_или_null,
  "bedrooms": число_или_null,
  "rooms": число_или_null,
  "area_m2": число_или_null,
  "district": "строка_или_null",
  "summary": "одно предложение по-русски: что сдаётся и за сколько"}}

Правила:
- is_rental_offer: true, только если жильё СДАЮТ. Сообщения о поиске жилья
  («ищу», «сниму»), продажа, коммерческие помещения — false.
- is_long_term: true для аренды от месяца и дольше. Посуточная, почасовая,
  посуточная «на неделю», саблет на пару недель — false. Если срок не указан
  вовсе, считай true: в чатах его часто не пишут.
- price_usd: итоговая цена за месяц в долларах. Если цена в лари — переведи
  по курсу ~{cfg.gel_per_usd} GEL за 1 USD. Если коммуналка указана явно —
  прибавь её. Если цена не указана — null, не выдумывай.
- bedrooms: число СПАЛЕН. Внимание: в грузинских и русских объявлениях
  «2-комнатная» обычно означает одну спальню плюс гостиную, «3-комнатная» —
  две спальни. Студия — 1 спальня.
- rooms: общее число комнат, как написано в объявлении.
- district: район Тбилиси строго из списка, максимально близкий по смыслу:
  {districts.prompt_list()}.
  Если район не назван или не из списка — null.
- Не угадывай. Любое поле, которого нет в тексте, — null."""


# ---------------------------------------------------------------------------
# Персональная проверка по свободному тексту пожеланий.
#
# Второй вызов Claude, и он платный — поэтому делается лениво: только для
# объявлений, уже прошедших структурные фильтры этого человека, и только если
# он вообще задал пожелания. Кто описание не заполнил — не платит ничего.
# ---------------------------------------------------------------------------
def build_match_prompt(description: str) -> str:
    return f"""Ты проверяешь, подходит ли объявление об аренде под пожелания конкретного человека.

Его пожелания, дословно:
\"\"\"{description}\"\"\"

Цена, число комнат и район уже проверены отдельно — их перепроверять не нужно.
Твоя задача: сверить только то, о чём сказано в пожеланиях выше.

Верни СТРОГО json без markdown-обёртки:
{{"match": true/false, "reason": "одно короткое предложение по-русски"}}

Правила:
- Если про требование из пожеланий в объявлении НИЧЕГО не сказано — считай, что
  оно не нарушено, и ставь true. Молчание не значит отказ.
- false ставь, только когда объявление ЯВНО противоречит пожеланиям.
- reason пиши всегда: при true — чем подходит, при false — что именно не так."""
