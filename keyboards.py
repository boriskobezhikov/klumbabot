"""
Инлайн-клавиатуры бота.

Здесь только раскладки и разбор callback-данных; логика — в commands.py.

Формат callback: "действие:аргумент". Telegram ограничивает эти данные 64
байтами, поэтому префиксы короткие, а названия районов передаются как индекс
в districts.BUTTON_ORDER, а не текстом — кириллица в UTF-8 съедала бы лимит.
"""

from __future__ import annotations

from telethon import Button

import districts
import users as users_mod
from users import Subscriber

BACK = "← Назад"


def _toggle(label: str, on: bool) -> str:
    return f"{'✅' if on else '▫️'} {label}"


# ---------------------------------------------------------------------------
# Главное меню
# ---------------------------------------------------------------------------
def main_menu(sub: Subscriber, stopped: bool = False) -> list[list[Button]]:
    rows = [
        [Button.inline("⚙️ Мои фильтры", b"m:filters")],
        [
            Button.inline("📊 Что настроено", b"m:status"),
            Button.inline(
                "▶️ Включить" if sub.paused else "⏸ Пауза", b"m:pause"
            ),
        ],
    ]
    if sub.is_owner:
        rows.append(
            [
                Button.inline("👥 Люди", b"m:people"),
                Button.inline("🛠 Сбор", b"m:admin"),
            ]
        )
        rows.append([Button.inline("📈 Статистика", b"m:stats")])
        # Отдельной строкой и последним: это рубильник для всех, а не ещё
        # одна настройка. Когда он опущен — это первое, что должно быть видно.
        rows.append(
            [
                Button.inline(
                    "▶️ ВОЗОБНОВИТЬ РАБОТУ" if stopped else "⛔ Остановить всё",
                    b"m:stopall",
                )
            ]
        )
    return rows


def stopall_menu(stopped: bool) -> list[list[Button]]:
    """Подтверждение стопа. Кнопка одна и названа тем, что произойдёт."""
    if stopped:
        return [
            [Button.inline("▶️ Возобновить работу", b"stop:no")],
            [Button.inline(BACK, b"m:main")],
        ]
    return [
        [Button.inline("⛔ Да, остановить для всех", b"stop:yes")],
        [Button.inline("← Отмена", b"m:main")],
    ]


def filters_menu(sub: Subscriber) -> list[list[Button]]:
    return [
        [
            Button.inline(f"🛏 Спален: {_short(sub.bedrooms)}", b"m:bed"),
            Button.inline(f"🚪 Комнат: {_short(sub.rooms)}", b"m:room"),
        ],
        [
            Button.inline(f"💰 Цена: {sub.price_range()}", b"m:budget"),
            Button.inline(f"📐 Площадь: {sub.area_range()}", b"m:area"),
        ],
        [Button.inline(f"📍 Районы: {len(sub.district_list) or 'все'}", b"m:dist")],
        [
            Button.inline(f"🌐 Источники: {len(sub.sources)}/2", b"m:src"),
            Button.inline(
                "📝 Пожелания ✅" if sub.description else "📝 Пожелания", b"m:desc"
            ),
        ],
        [Button.inline(BACK, b"m:main")],
    ]


def description_menu(sub: Subscriber) -> list[list[Button]]:
    rows = [[Button.inline("✏️ Написать заново", b"desc:edit")]]
    if sub.description:
        rows.append([Button.inline("🗑 Убрать пожелания", b"desc:clear")])
    rows.append([Button.inline(BACK, b"m:filters")])
    return rows


def _short(values: list[int]) -> str:
    return ",".join(str(v) for v in sorted(values)) if values else "любое"


# ---------------------------------------------------------------------------
# Числовые тумблеры
# ---------------------------------------------------------------------------
def bedrooms_menu(sub: Subscriber) -> list[list[Button]]:
    row = [
        Button.inline(
            _toggle(f"{n}{'+' if n == max(users_mod.BEDROOM_CHOICES) else ''}", n in sub.bedrooms),
            f"bed:{n}".encode(),
        )
        for n in users_mod.BEDROOM_CHOICES
    ]
    return [row, [Button.inline(BACK, b"m:filters")]]


def rooms_menu(sub: Subscriber) -> list[list[Button]]:
    row = [
        Button.inline(
            _toggle(f"{n}{'+' if n == max(users_mod.ROOM_CHOICES) else ''}", n in sub.rooms),
            f"room:{n}".encode(),
        )
        for n in users_mod.ROOM_CHOICES
    ]
    return [row, [Button.inline(BACK, b"m:filters")]]


def budget_menu(sub: Subscriber) -> list[list[Button]]:
    """
    Две границы на одном экране: нижняя и верхняя.

    Средняя кнопка в каждом ряду не действие, а показание — на неё повешен
    шаг 0, чтобы нажатие ничего не меняло, но и не выглядело сломанным.
    """
    low = f"от {sub.min_price_usd}$" if sub.min_price_usd else "снизу любая"
    rows = [
        [
            Button.inline("−100", b"minp:-100"),
            Button.inline("−50", b"minp:-50"),
            Button.inline(low, b"minp:0"),
            Button.inline("+50", b"minp:50"),
            Button.inline("+100", b"minp:100"),
        ],
        [
            Button.inline("−100", b"budget:-100"),
            Button.inline("−50", b"budget:-50"),
            Button.inline(f"до {sub.budget_usd}$", b"budget:0"),
            Button.inline("+50", b"budget:50"),
            Button.inline("+100", b"budget:100"),
        ],
    ]
    if sub.min_price_usd:
        rows.append([Button.inline("🚫 Убрать нижнюю границу", b"minp:=0")])
    rows.append(
        [
            Button.inline("до 500$", b"budget:=500"),
            Button.inline("до 700$", b"budget:=700"),
            Button.inline("до 1000$", b"budget:=1000"),
            Button.inline("до 1500$", b"budget:=1500"),
        ]
    )
    rows.append([Button.inline(BACK, b"m:filters")])
    return rows


def area_menu(sub: Subscriber) -> list[list[Button]]:
    return [
        [
            Button.inline("−10", b"area:-10"),
            Button.inline("−5", b"area:-5"),
            Button.inline(
                f"от {sub.min_area_m2} м²" if sub.min_area_m2 else "любая", b"area:0"
            ),
            Button.inline("+5", b"area:5"),
            Button.inline("+10", b"area:10"),
        ],
        [
            Button.inline("🚫 Любая", b"area:=0"),
            Button.inline("40 м²", b"area:=40"),
            Button.inline("50 м²", b"area:=50"),
            Button.inline("70 м²", b"area:=70"),
        ],
        [Button.inline(BACK, b"m:filters")],
    ]


def sources_menu(sub: Subscriber) -> list[list[Button]]:
    return [
        [
            Button.inline(
                _toggle("Telegram-чаты", users_mod.SOURCE_TELEGRAM in sub.sources),
                b"src:telegram",
            )
        ],
        [
            Button.inline(
                _toggle("Сайт ss.ge", users_mod.SOURCE_SSGE in sub.sources),
                b"src:ssge",
            )
        ],
        [Button.inline(BACK, b"m:filters")],
    ]


# ---------------------------------------------------------------------------
# Районы: 18 кнопок по две в ряд + «любые»
# ---------------------------------------------------------------------------
def districts_menu(sub: Subscriber) -> list[list[Button]]:
    rows: list[list[Button]] = [
        [
            Button.inline(
                _toggle("Любые районы", not sub.district_list), b"dist:all"
            )
        ]
    ]
    order = districts.BUTTON_ORDER
    for i in range(0, len(order), 2):
        row = [
            Button.inline(
                _toggle(name, name in sub.district_list), f"dist:{idx}".encode()
            )
            for idx, name in ((i + k, order[i + k]) for k in (0, 1) if i + k < len(order))
        ]
        rows.append(row)
    rows.append([Button.inline(BACK, b"m:filters")])
    return rows


# ---------------------------------------------------------------------------
# Владельцу
# ---------------------------------------------------------------------------
def approval_buttons(user_id: int) -> list[list[Button]]:
    return [
        [
            Button.inline("✅ Разрешить", f"ok:{user_id}".encode()),
            Button.inline("🚫 Отклонить", f"no:{user_id}".encode()),
        ]
    ]


def people_menu(subs: list[Subscriber]) -> list[list[Button]]:
    rows = []
    for s in subs:
        if s.is_owner:
            continue
        mark = {"active": "✅", "pending": "⏳", "denied": "🚫"}.get(s.status, "?")
        rows.append(
            [
                Button.inline(f"{mark} {s.name or s.user_id}", f"who:{s.user_id}".encode()),
                Button.inline(
                    "🚫" if s.status == users_mod.STATUS_ACTIVE else "✅",
                    f"{'no' if s.status == users_mod.STATUS_ACTIVE else 'ok'}:{s.user_id}".encode(),
                ),
            ]
        )
    if not rows:
        rows.append([Button.inline("Пока никого нет", b"m:people")])
    rows.append([Button.inline(BACK, b"m:main")])
    return rows


def admin_menu(cfg) -> list[list[Button]]:
    s = cfg.ssge
    return [
        [
            Button.inline(
                _toggle("Опрос ss.ge", s["enabled"]), b"adm:ssge"
            ),
            Button.inline(
                _toggle("Предфильтр чатов", cfg.prefilter_enabled), b"adm:prefilter"
            ),
        ],
        [
            Button.inline(
                _toggle("Claude (разбор объявлений)", cfg.claude_enabled),
                b"adm:claude",
            )
        ],
        [
            Button.inline("− мин", b"adm:int-5"),
            Button.inline(f"{s['poll_minutes']} мин", b"adm:noop"),
            Button.inline("+ мин", b"adm:int5"),
        ],
        [
            Button.inline("− стр.", b"adm:pg-1"),
            Button.inline(f"{s['pages']} стр.", b"adm:noop"),
            Button.inline("+ стр.", b"adm:pg1"),
        ],
        [Button.inline(BACK, b"m:main")],
    ]


# ---------------------------------------------------------------------------
def stats_menu() -> list[list[Button]]:
    return [
        [
            Button.inline("🔄 Обновить", b"m:stats"),
            Button.inline("🔍 Последние разборы", b"m:last"),
        ],
        [Button.inline(BACK, b"m:main")],
    ]


def parse(data: bytes) -> tuple[str, str]:
    """b'bed:2' -> ('bed', '2')"""
    text = data.decode("utf-8", "replace")
    action, _, arg = text.partition(":")
    return action, arg
