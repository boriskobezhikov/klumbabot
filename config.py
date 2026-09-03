"""
Runtime-изменяемая конфигурация мониторинга.

Всё, что можно поменять командой боту (бюджет, курс, чаты, критерии,
regex-предфильтр), живёт в config.json рядом с этим файлом. Секреты (токены,
ключи, TG_API_ID) остаются в .env — их менять на лету незачем.

Если config.json ещё нет, он собирается из значений .env при первом запуске,
поэтому обновление со старой версии не требует ручной миграции.
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

log = logging.getLogger("klumba-monitor")

CONFIG_PATH = Path(
    os.environ.get("KLUMBA_CONFIG") or Path(__file__).with_name("config.json")
)

DEFAULT_CRITERIA = (
    "1-комнатную/1-спальную квартиру в ДОЛГОСРОЧНУЮ аренду "
    "(от нескольких месяцев, не посуточно и не саблет на пару недель)."
)

# Дефолты предфильтра — ровно те регулярки, что были захардкожены раньше.
DEFAULT_PREFILTER = {
    "enabled": True,
    "listing": r"сда(ю|ётся|ется|м)\b|в\s*аренду",
    "bedroom": r"спальн|студ|1[\s\-]*комнат",
    "seeking": r"\bищу\b|\bсниму\b|\bнужна?\s+квартир",
}

PREFILTER_FIELDS = ("listing", "bedroom", "seeking")


@dataclass
class ChatRef:
    """Один отслеживаемый чат. peer_id=None означает «ещё не зарезолвлен»."""

    ref: str  # как его ввёл пользователь: @kkklumba, kkklumba или -100123...
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
    budget_usd: int = 700
    gel_per_usd: float = 2.6
    criteria: str = DEFAULT_CRITERIA
    chats: list[ChatRef] = field(default_factory=list)
    prefilter: dict = field(default_factory=lambda: dict(DEFAULT_PREFILTER))

    # -- поиск по чатам ----------------------------------------------------
    def chat_by_peer_id(self, peer_id: int) -> ChatRef | None:
        for chat in self.chats:
            if chat.peer_id == peer_id:
                return chat
        return None

    def chat_by_ref(self, ref: str) -> ChatRef | None:
        """Ищет по тому, как чат записан, по username и по числовому id."""
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

    # -- предфильтр --------------------------------------------------------
    def compiled_prefilter(self) -> tuple[re.Pattern, re.Pattern, re.Pattern]:
        return _compile_prefilter(
            self.prefilter["listing"],
            self.prefilter["bedroom"],
            self.prefilter["seeking"],
        )

    # -- сериализация ------------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "budget_usd": self.budget_usd,
            "gel_per_usd": self.gel_per_usd,
            "criteria": self.criteria,
            "chats": [asdict(c) for c in self.chats],
            "prefilter": self.prefilter,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> Config:
        prefilter = dict(DEFAULT_PREFILTER)
        prefilter.update(raw.get("prefilter") or {})
        return cls(
            budget_usd=int(raw.get("budget_usd", 700)),
            gel_per_usd=float(raw.get("gel_per_usd", 2.6)),
            criteria=raw.get("criteria") or DEFAULT_CRITERIA,
            chats=[ChatRef(**c) for c in raw.get("chats") or []],
            prefilter=prefilter,
        )


@dataclass
class State:
    """Общее изменяемое состояние процесса: конфиг + счётчики для /status."""

    cfg: Config
    api_calls: list[float] = field(default_factory=list)  # unix-время вызовов Claude
    seen: dict[str, float] = field(default_factory=dict)  # дедуп: хеш текста -> время


@functools.lru_cache(maxsize=8)
def _compile_prefilter(listing: str, bedroom: str, seeking: str):
    return (
        re.compile(listing, re.IGNORECASE),
        re.compile(bedroom, re.IGNORECASE),
        re.compile(seeking, re.IGNORECASE),
    )


def validate_regex(pattern: str) -> str | None:
    """Возвращает текст ошибки, если regex не компилируется, иначе None."""
    try:
        re.compile(pattern, re.IGNORECASE)
    except re.error as e:
        return str(e)
    return None


# ---------------------------------------------------------------------------
# Загрузка / сохранение
# ---------------------------------------------------------------------------
_save_lock = asyncio.Lock()


def load() -> Config:
    """Читает config.json; если его нет — собирает из .env и сразу сохраняет."""
    if CONFIG_PATH.exists():
        with CONFIG_PATH.open(encoding="utf-8") as f:
            cfg = Config.from_dict(json.load(f))
        log.info("Конфиг загружен из %s", CONFIG_PATH)
        return cfg

    cfg = Config(
        budget_usd=int(os.environ.get("BUDGET_USD", "700")),
        gel_per_usd=float(os.environ.get("GEL_PER_USD", "2.6")),
    )
    legacy_group = os.environ.get("TG_GROUP", "kkklumba").strip()
    if legacy_group:
        cfg.chats.append(ChatRef(ref=legacy_group))
    _write(cfg)
    log.info("Конфиг создан из .env: %s", CONFIG_PATH)
    return cfg


def _write(cfg: Config) -> None:
    """Атомарная запись, чтобы падение на середине не оставило битый файл."""
    tmp = CONFIG_PATH.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(cfg.to_dict(), f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG_PATH)


async def save(cfg: Config) -> None:
    async with _save_lock:
        _write(cfg)


# ---------------------------------------------------------------------------
# Промпт для Claude — собирается на каждой классификации, поэтому правки
# критериев/бюджета применяются без перезапуска.
# ---------------------------------------------------------------------------
def build_system_prompt(cfg: Config) -> str:
    return f"""Ты фильтруешь объявления об аренде квартир из тбилисских Telegram-чатов.

Пользователь ищет: {cfg.criteria}

Бюджет: итоговая стоимость в месяц (аренда + коммуналка, если коммуналка указана
явно) не больше ${cfg.budget_usd}.

Тебе присылают текст ОДНОГО сообщения из чата. Верни СТРОГО json без markdown-обёртки, вида:
{{"match": true/false, "reason": "коротко по-русски почему да/нет", "price_usd": число_или_null,
"area_m2": число_или_null, "district": "строка_или_null"}}

Правила:
- Если коммуналка указана в лари — переведи в доллары (~{cfg.gel_per_usd} GEL = 1 USD) и прибавь
  к аренде для итоговой суммы, на основании которой сравниваешь с бюджетом.
- Если это сообщение о ПОИСКЕ жилья ("ищу", "сниму"), а не о СДАЧЕ — match всегда false.
- Если это посуточная/недельная аренда или саблет на пару недель без явного "от N месяцев" — match false.
- Если объявление не подходит под то, что ищет пользователь — match false.
- Будь придирчив: лучше пропустить сомнительное объявление, чем прислать ложный матч."""
