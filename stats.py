"""
Счётчики работы: сколько собрано, сколько дошло до Claude, во что это встало.

Две шкалы. Скользящие окна (час / сутки) живут в памяти и отвечают на вопрос
«что происходит сейчас». Итоги за всё время пишутся на диск и переживают
перезапуск — по ним видно тренд и накопленные расходы.

Оценка стоимости приблизительная: считаем по числу вызовов и среднему размеру
запроса, а не по фактическим токенам из ответа API. Для «дорого ли мне это
обходится» точности хватает, для бухгалтерии — нет.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("klumba-monitor")

HOUR = 3600
DAY = 24 * HOUR

# Что считаем. Порядок — как показывать в сводке.
COUNTERS = (
    ("tg_seen", "сообщений в чатах"),
    ("tg_prefiltered", "прошло предфильтр"),
    ("ssge_fetched", "получено с ss.ge"),
    ("ssge_new", "из них новых"),
    ("ssge_price_cut", "отсеяно по цене"),
    ("ssge_detail", "карточек догружено"),
    ("dup", "дублей отброшено"),
    ("claude_extract", "разборов в Claude"),
    ("claude_match", "личных проверок"),
    ("claude_failed", "ОШИБОК Claude"),
    ("no_match", "никому не подошло"),
    ("notified", "уведомлений разослано"),
)

# Haiku 4.5: $1 за 1M входных, $5 за 1M выходных.
# Прикидка на один вызов — по типичному размеру промпта и ответа. Замерено на
# живых карточках ss.ge: системный промпт + догруженная карточка дают ~1000
# входных токенов на разбор и ~950 на личную проверку.
COST_PER_CALL = {
    "claude_extract": 1000 / 1_000_000 * 1.0 + 200 / 1_000_000 * 5.0,
    "claude_match": 950 / 1_000_000 * 1.0 + 100 / 1_000_000 * 5.0,
}


@dataclass
class Stats:
    # имя счётчика -> отметки времени событий (для окон)
    recent: dict[str, list[float]] = field(default_factory=dict)
    # имя счётчика -> сколько всего за всё время
    totals: dict[str, int] = field(default_factory=dict)
    started_at: float = field(default_factory=time.time)
    since: float = field(default_factory=time.time)  # когда начали копить итоги

    def bump(self, name: str, n: int = 1) -> None:
        now = time.time()
        marks = self.recent.setdefault(name, [])
        marks.extend([now] * n)
        # Чистим на записи, чтобы список не рос бесконечно на долгом простое.
        if len(marks) > 50_000:
            cutoff = now - DAY
            self.recent[name] = [t for t in marks if t >= cutoff]
        self.totals[name] = self.totals.get(name, 0) + n

    def count(self, name: str, window: float) -> int:
        cutoff = time.time() - window
        return sum(1 for t in self.recent.get(name, ()) if t >= cutoff)

    def total(self, name: str) -> int:
        return self.totals.get(name, 0)

    def cost(self, window: float | None = None) -> float:
        """Оценка расходов на Claude: за окно или за всё время."""
        out = 0.0
        for name, price in COST_PER_CALL.items():
            n = self.total(name) if window is None else self.count(name, window)
            out += n * price
        return out

    def prune(self) -> None:
        cutoff = time.time() - DAY
        for name, marks in self.recent.items():
            self.recent[name] = [t for t in marks if t >= cutoff]

    # -- сохранение --------------------------------------------------------
    def to_dict(self) -> dict:
        return {"totals": self.totals, "since": self.since}

    @classmethod
    def from_dict(cls, raw: dict) -> Stats:
        return cls(
            totals={k: int(v) for k, v in (raw.get("totals") or {}).items()},
            since=float(raw.get("since") or time.time()),
        )


def path_for(config_path: Path) -> Path:
    return config_path.with_name("stats.json")


def load(config_path: Path) -> Stats:
    p = path_for(config_path)
    if not p.exists():
        return Stats()
    try:
        with p.open(encoding="utf-8") as f:
            return Stats.from_dict(json.load(f))
    except (json.JSONDecodeError, ValueError, TypeError) as e:
        log.warning("stats.json битый (%s) — начинаю счёт заново", e)
        return Stats()


def save(config_path: Path, st: Stats) -> None:
    p = path_for(config_path)
    tmp = p.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(st.to_dict(), f, ensure_ascii=False)
    os.replace(tmp, p)


# ---------------------------------------------------------------------------
def render(st: Stats, last_error: str | None = None) -> str:
    """Сводка для владельца."""
    lines = ["📈 Статистика", ""]
    if last_error:
        lines += [
            "⚠️ Claude отвечает ошибкой — объявления не разбираются:",
            f"   {last_error[:200]}",
            "",
        ]
    lines.append(f"{'':24s}{'час':>6s}{'сутки':>8s}{'всего':>9s}")
    for name, label in COUNTERS:
        h, d, t = st.count(name, HOUR), st.count(name, DAY), st.total(name)
        if h == 0 and d == 0 and t == 0:
            continue
        lines.append(f"{label:24s}{h:>6d}{d:>8d}{t:>9d}")

    day_cost = st.cost(DAY)
    total_cost = st.cost()
    lines += [
        "",
        f"Claude за сутки: ~${day_cost:.2f}",
        f"Claude за всё время: ~${total_cost:.2f}",
        f"В месяц по темпу суток: ~${day_cost * 30:.2f}",
    ]

    uptime_h = (time.time() - st.started_at) / HOUR
    days = (time.time() - st.since) / DAY
    lines.append("")
    lines.append(f"Процесс работает: {uptime_h:.1f} ч")
    lines.append(f"Итоги копятся: {days:.1f} дн")

    extracted = st.count("claude_extract", DAY)
    seen = st.count("tg_seen", DAY) + st.count("ssge_new", DAY)
    if seen:
        lines.append(f"Доля дошедшего до Claude за сутки: {extracted / seen * 100:.0f}%")
    return "\n".join(lines)
