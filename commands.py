"""
Команды управления фильтрами, которые notify-бот принимает в личке.

Отвечает только владельцу (NOTIFY_CHAT_ID) — иначе любой, кто найдёт бота,
смог бы переписать критерии поиска. Каждая изменяющая команда сначала
валидирует ввод и только потом пишет config.json, поэтому кривая регулярка или
несуществующий чат не ломают уже работающий мониторинг.
"""

from __future__ import annotations

import logging
import re
import time

from telethon import events

import config
from chats import ResolveError, resolve

log = logging.getLogger("klumba-monitor")

HELP = """Команды:

/status — что сейчас настроено
/budget 850 — максимальная итоговая цена в месяц, $
/rate 2.7 — курс GEL за 1 USD

/chats — список отслеживаемых чатов
/chats add @username — добавить публичный чат
/chats add -1001234567890 — добавить приватный (по id)
/chats rm @username — убрать

/criteria — показать критерии поиска
/criteria <текст> — заменить (обычный текст, его читает Claude)

/prefilter — показать regex-предфильтр
/prefilter on|off — включить/выключить
/prefilter listing <regex> — заменить одну из регулярок
   (listing — признак сдачи, bedroom — тип жилья, seeking — признак поиска)

/ssge — состояние опроса сайта ss.ge
/ssge on|off — включить/выключить
/ssge interval 10 — как часто опрашивать, минут
/ssge pages 2 — сколько страниц выдачи проверять
/ssge rooms 1 — число комнат в фильтре сайта"""


def register(bot, user_client, state: config.State, owner_id: int) -> None:
    @bot.on(events.NewMessage(incoming=True))
    async def on_command(event) -> None:
        if event.sender_id != owner_id:
            return

        text = (event.raw_text or "").strip()
        if not text.startswith("/"):
            return

        cmd, _, args = text.partition(" ")
        cmd = cmd.split("@", 1)[0].lower()  # /budget@mybot -> /budget
        args = args.strip()

        handler = _HANDLERS.get(cmd)
        if handler is None:
            await event.respond(f"Не знаю команду {cmd}.\n\n{HELP}")
            return

        try:
            await handler(event, user_client, state, args)
        except Exception as e:  # noqa: BLE001 — команда не должна ронять процесс
            log.exception("команда %s упала", cmd)
            await event.respond(f"⚠️ Ошибка при выполнении {cmd}: {e}")


# ---------------------------------------------------------------------------
# Обработчики
# ---------------------------------------------------------------------------
async def _help(event, user_client, state, args) -> None:
    await event.respond(HELP)


async def _status(event, user_client, state, args) -> None:
    cfg = state.cfg
    now = time.time()
    calls_24h = sum(1 for t in state.api_calls if now - t < 86400)

    if cfg.chats:
        chats = "\n".join(f"  {i}. {c.label()}" for i, c in enumerate(cfg.chats, 1))
    else:
        chats = "  (пусто — добавь через /chats add)"

    pf = "включён" if cfg.prefilter["enabled"] else "ВЫКЛЮЧЕН (все сообщения идут в Claude)"

    if cfg.ssge["enabled"]:
        ssge_line = f"каждые {cfg.ssge['poll_minutes']} мин"
        if state.ssge_last_poll:
            ssge_line += f", последний {int((now - state.ssge_last_poll) / 60)} мин назад"
        if state.ssge_last_error:
            ssge_line += " ⚠️ с ошибкой"
    else:
        ssge_line = "выключен"

    await event.respond(
        "📊 Klumba monitor\n\n"
        f"Бюджет: {cfg.budget_usd}$\n"
        f"Курс: {cfg.gel_per_usd} GEL/USD\n"
        f"Предфильтр (чаты): {pf}\n"
        f"ss.ge: {ssge_line}\n"
        f"Вызовов Claude за 24ч: {calls_24h}\n\n"
        f"Чаты ({len(cfg.chats)}):\n{chats}\n\n"
        f"Критерии:\n{cfg.criteria}"
    )


async def _budget(event, user_client, state, args) -> None:
    if not args:
        await event.respond(f"Бюджет: {state.cfg.budget_usd}$\nИзменить: /budget 850")
        return
    try:
        value = int(args)
    except ValueError:
        await event.respond("Нужно целое число, например: /budget 850")
        return
    if value <= 0:
        await event.respond("Бюджет должен быть больше нуля.")
        return

    state.cfg.budget_usd = value
    await config.save(state.cfg)
    await event.respond(f"✅ Бюджет: {value}$")


async def _rate(event, user_client, state, args) -> None:
    if not args:
        await event.respond(f"Курс: {state.cfg.gel_per_usd} GEL/USD\nИзменить: /rate 2.7")
        return
    try:
        value = float(args.replace(",", "."))
    except ValueError:
        await event.respond("Нужно число, например: /rate 2.7")
        return
    if value <= 0:
        await event.respond("Курс должен быть больше нуля.")
        return

    state.cfg.gel_per_usd = value
    await config.save(state.cfg)
    await event.respond(f"✅ Курс: {value} GEL/USD")


async def _chats(event, user_client, state, args) -> None:
    cfg = state.cfg
    sub, _, rest = args.partition(" ")
    sub = sub.lower()
    rest = rest.strip()

    if not sub:
        if not cfg.chats:
            await event.respond("Список чатов пуст.\nДобавить: /chats add @username")
            return
        lines = "\n".join(f"  {i}. {c.label()}" for i, c in enumerate(cfg.chats, 1))
        await event.respond(f"📋 Отслеживаю ({len(cfg.chats)}):\n{lines}")
        return

    if sub == "add":
        if not rest:
            await event.respond("Укажи чат: /chats add @username или /chats add -1001234567890")
            return
        if cfg.chat_by_ref(rest):
            await event.respond("Этот чат уже в списке.")
            return
        try:
            chat = await resolve(user_client, rest)
        except ResolveError as e:
            await event.respond(f"⚠️ {e}")
            return
        if cfg.chat_by_peer_id(chat.peer_id):
            await event.respond("Этот чат уже в списке (под другим написанием).")
            return

        cfg.chats.append(chat)
        await config.save(cfg)
        await event.respond(f"✅ Добавлен: {chat.label()}\nВсего чатов: {len(cfg.chats)}")
        return

    if sub in ("rm", "remove", "del", "delete"):
        if not rest:
            await event.respond("Укажи чат: /chats rm @username")
            return
        chat = cfg.chat_by_ref(rest)
        if chat is None:
            await event.respond(f"Чата «{rest}» нет в списке. Посмотреть: /chats")
            return

        cfg.chats.remove(chat)
        await config.save(cfg)
        await event.respond(f"✅ Убран: {chat.label()}\nОсталось чатов: {len(cfg.chats)}")
        return

    await event.respond("Не понял. Есть /chats, /chats add <чат>, /chats rm <чат>")


async def _criteria(event, user_client, state, args) -> None:
    if not args:
        await event.respond(
            f"Критерии поиска:\n{state.cfg.criteria}\n\n"
            "Заменить целиком: /criteria <новый текст>"
        )
        return

    state.cfg.criteria = args
    await config.save(state.cfg)
    await event.respond(f"✅ Критерии обновлены:\n{args}")


async def _ssge(event, user_client, state, args) -> None:
    cfg = state.cfg
    s = cfg.ssge
    sub, _, rest = args.partition(" ")
    sub = sub.lower()
    rest = rest.strip()

    if not sub:
        if state.ssge_last_poll:
            ago = int((time.time() - state.ssge_last_poll) / 60)
            last = f"{ago} мин назад, новых за проход: {state.ssge_last_new}"
        else:
            last = "ещё не опрашивался"
        body = (
            f"🌐 ss.ge: {'включён' if s['enabled'] else 'выключен'}\n"
            f"Интервал: {s['poll_minutes']} мин\n"
            f"Страниц за проход: {s['pages']}\n"
            f"Комнат: {s['rooms']}\n"
            f"Последний опрос: {last}\n"
            f"Известных объявлений: {state.ssge_seen_count}"
        )
        if state.ssge_last_error:
            body += f"\n⚠️ Последняя ошибка: {state.ssge_last_error}"
        await event.respond(body)
        return

    if sub in ("on", "off"):
        s["enabled"] = sub == "on"
        await config.save(cfg)
        await event.respond(f"✅ ss.ge {'включён' if s['enabled'] else 'выключен'}.")
        return

    if sub == "interval":
        try:
            value = int(rest)
        except ValueError:
            await event.respond("Нужно целое число минут, например: /ssge interval 10")
            return
        if value < 2:
            await event.respond("Минимум 2 минуты — чаще опрашивать сайт незачем.")
            return
        s["poll_minutes"] = value
        await config.save(cfg)
        await event.respond(
            f"✅ Интервал: {value} мин.\nПрименится после текущей паузы опроса."
        )
        return

    if sub == "pages":
        try:
            value = int(rest)
        except ValueError:
            await event.respond("Нужно целое число, например: /ssge pages 2")
            return
        if not 1 <= value <= 10:
            await event.respond("Разумный диапазон — от 1 до 10 страниц.")
            return
        s["pages"] = value
        await config.save(cfg)
        await event.respond(f"✅ Страниц за проход: {value} (~{value * 16} объявлений).")
        return

    if sub == "rooms":
        if not rest or not re.fullmatch(r"\d+(,\d+)*", rest):
            await event.respond("Например: /ssge rooms 1 или /ssge rooms 1,2")
            return
        s["rooms"] = rest
        await config.save(cfg)
        await event.respond(f"✅ Комнат: {rest}")
        return

    await event.respond(
        f"Не понял «{sub}». Есть: /ssge, /ssge on|off, /ssge interval N, "
        "/ssge pages N, /ssge rooms N"
    )


async def _prefilter(event, user_client, state, args) -> None:
    cfg = state.cfg
    sub, _, rest = args.partition(" ")
    sub = sub.lower()
    rest = rest.strip()

    if not sub:
        status = "включён" if cfg.prefilter["enabled"] else "выключен"
        body = "\n".join(f"  {f}: {cfg.prefilter[f]}" for f in config.PREFILTER_FIELDS)
        await event.respond(
            f"🔍 Предфильтр {status}\n{body}\n\n"
            "Сообщение проходит дальше, если совпало с listing и bedroom "
            "и не выглядит как seeking.\n"
            "Выключение отправляет в Claude каждое сообщение — это заметно дороже."
        )
        return

    if sub in ("on", "off"):
        cfg.prefilter["enabled"] = sub == "on"
        await config.save(cfg)
        if sub == "on":
            await event.respond("✅ Предфильтр включён.")
        else:
            await event.respond(
                "✅ Предфильтр выключен. Теперь КАЖДОЕ сообщение из всех чатов "
                "уходит в Claude — следи за расходами через /status."
            )
        return

    if sub in config.PREFILTER_FIELDS:
        if not rest:
            await event.respond(f"Текущее значение {sub}: {cfg.prefilter[sub]}")
            return
        err = config.validate_regex(rest)
        if err:
            await event.respond(f"⚠️ Некорректный regex, ничего не изменил: {err}")
            return

        cfg.prefilter[sub] = rest
        await config.save(cfg)
        await event.respond(f"✅ {sub}: {rest}")
        return

    await event.respond(
        f"Не понял «{sub}». Есть: /prefilter, /prefilter on|off, "
        f"/prefilter {'|'.join(config.PREFILTER_FIELDS)} <regex>"
    )


_HANDLERS = {
    "/start": _help,
    "/help": _help,
    "/status": _status,
    "/budget": _budget,
    "/rate": _rate,
    "/chats": _chats,
    "/criteria": _criteria,
    "/prefilter": _prefilter,
    "/ssge": _ssge,
}
