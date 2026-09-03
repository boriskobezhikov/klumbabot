"""
Диалог с ботом: инлайн-меню для подписчиков и одобрение доступа владельцем.

Регулярки руками больше никто не пишет — человек тыкает «1 спальня», «до 700$»,
«Ваке», а предфильтр для чатов и параметр rooms для ss.ge собираются из этих
кнопок автоматически (users.build_bedroom_regex / ssge_rooms_param).

Владелец видит два лишних пункта: «Люди» (одобрить/отключить) и «Сбор»
(параметры опроса, общие для всех). Личные фильтры у владельца такие же, как
у остальных.
"""

from __future__ import annotations

import logging
import time

from telethon import events

import config
import districts
import keyboards as kb
import stats as stats_mod
import users as users_mod
from chats import ResolveError, resolve
from users import Subscriber

log = logging.getLogger("klumba-monitor")

HELP = """Я слежу за объявлениями об аренде в Тбилиси — в Telegram-чатах и на ss.ge —
и присылаю те, что подходят под твои фильтры.

/menu — настройки кнопками (спальни, комнаты, бюджет, районы, источники,
       а также свободные пожелания текстом)
/status — что сейчас настроено
/stop — приостановить уведомления, /start — возобновить"""

OWNER_HELP = """
Команды владельца:
/chats — список отслеживаемых чатов
/chats add @username — добавить чат (или по id для приватного)
/chats rm @username — убрать
/users — список людей и заявок
/stats — сколько собрано и во что обошёлся Claude
/last — последние разборы и почему они кому-то не подошли
/rate 2.7 — курс GEL за 1 USD"""


def register(bot, user_client, state: config.State, owner_id: int) -> None:
    async def notify_owner(text: str, buttons=None) -> None:
        try:
            await bot.send_message(owner_id, text, buttons=buttons, link_preview=False)
        except Exception as e:  # noqa: BLE001
            log.warning("не смог написать владельцу: %s", e)

    # -- регистрация и текстовые команды ----------------------------------
    @bot.on(events.NewMessage(incoming=True))
    async def on_message(event) -> None:
        text = (event.raw_text or "").strip()
        uid_early = event.sender_id

        # Ждём от этого человека текст пожеланий — принимаем его как есть.
        if state.awaiting.get(uid_early) == "description" and text and not text.startswith("/"):
            state.awaiting.pop(uid_early, None)
            sub_early = state.cfg.user(uid_early)
            if sub_early is not None:
                sub_early.description = text[:1000]
                await config.save(state.cfg)
                await event.respond(
                    "✅ Пожелания сохранены:\n\n"
                    f"{sub_early.description}\n\n"
                    "Теперь я буду отдельно сверять с ними каждое объявление, "
                    "которое прошло остальные фильтры.",
                    buttons=kb.filters_menu(sub_early),
                )
            return

        if not text.startswith("/"):
            return

        cmd = text.split()[0].split("@", 1)[0].lower()
        args = text[len(text.split()[0]):].strip()
        uid = event.sender_id
        cfg = state.cfg
        sub = cfg.user(uid)

        if cmd == "/start":
            await _start(event, notify_owner, state, uid)
            return

        if sub is None or sub.status != users_mod.STATUS_ACTIVE:
            # Незнакомым и отклонённым не показываем ничего, кроме /start.
            if sub is not None and sub.status == users_mod.STATUS_PENDING:
                await event.respond("Заявка отправлена владельцу, ждём решения.")
            return

        handler = _HANDLERS.get(cmd)
        if handler is None:
            await event.respond(f"Не знаю команду {cmd}.\n\n{_help_for(sub)}")
            return
        try:
            await handler(event, user_client, state, sub, args)
        except Exception as e:  # noqa: BLE001
            log.exception("команда %s упала", cmd)
            await event.respond(f"⚠️ Ошибка при выполнении {cmd}: {e}")

    # -- кнопки ------------------------------------------------------------
    @bot.on(events.CallbackQuery)
    async def on_callback(event) -> None:
        cfg = state.cfg
        uid = event.sender_id
        sub = cfg.user(uid)
        action, arg = kb.parse(event.data)

        # Решения по заявкам принимает только владелец.
        if action in ("ok", "no"):
            if sub is None or not sub.is_owner:
                await event.answer("Это может только владелец.", alert=True)
                return
            await _decide(event, state, arg, allow=action == "ok")
            return

        if sub is None or sub.status != users_mod.STATUS_ACTIVE:
            await event.answer("Нет доступа. Напиши /start.", alert=True)
            return

        try:
            await _route(event, state, sub, action, arg)
        except Exception as e:  # noqa: BLE001
            log.exception("callback %s:%s упал", action, arg)
            await event.answer(f"Ошибка: {e}", alert=True)


# ---------------------------------------------------------------------------
# Регистрация
# ---------------------------------------------------------------------------
async def _start(event, notify_owner, state: config.State, uid: int) -> None:
    cfg = state.cfg
    sub = cfg.user(uid)
    sender = await event.get_sender()
    name = " ".join(
        p for p in (getattr(sender, "first_name", ""), getattr(sender, "last_name", "")) if p
    ) or getattr(sender, "username", "") or str(uid)

    if sub is None:
        sub = Subscriber(user_id=uid, name=name, status=users_mod.STATUS_PENDING)
        cfg.subscribers.append(sub)
        await config.save(cfg)
        await event.respond(
            "Привет! Я слежу за объявлениями об аренде в Тбилиси.\n\n"
            "Отправил заявку владельцу — как только одобрит, вернусь с настройками."
        )
        await notify_owner(
            f"👤 Новая заявка на доступ\n\n{name}\nid: {uid}",
            buttons=kb.approval_buttons(uid),
        )
        log.info("заявка на доступ: %s (%s)", name, uid)
        return

    if sub.status == users_mod.STATUS_PENDING:
        await event.respond("Заявка уже отправлена, ждём решения владельца.")
        return
    if sub.status == users_mod.STATUS_DENIED:
        await event.respond("Доступ закрыт.")
        return

    if sub.name != name:
        sub.name = name
    if sub.paused:
        sub.paused = False
        await config.save(cfg)
        await event.respond("Уведомления снова включены.")
    await _show_main(event, sub, respond=True)


async def _decide(event, state: config.State, arg: str, allow: bool) -> None:
    cfg = state.cfg
    try:
        target_id = int(arg)
    except ValueError:
        await event.answer("Неверный id.", alert=True)
        return

    sub = cfg.user(target_id)
    if sub is None:
        await event.answer("Такого человека уже нет.", alert=True)
        return
    if sub.is_owner:
        await event.answer("Владельца отключить нельзя.", alert=True)
        return

    sub.status = users_mod.STATUS_ACTIVE if allow else users_mod.STATUS_DENIED
    await config.save(cfg)

    await event.edit(
        f"{'✅ Доступ разрешён' if allow else '🚫 Доступ закрыт'}\n\n"
        f"{sub.name}\nid: {sub.user_id}"
    )
    try:
        if allow:
            await event.client.send_message(
                target_id,
                "✅ Доступ открыт! Настрой, что тебе искать:",
                buttons=kb.main_menu(sub),
            )
        else:
            await event.client.send_message(target_id, "Владелец отклонил заявку.")
    except Exception as e:  # noqa: BLE001
        log.warning("не смог уведомить %s о решении: %s", target_id, e)
    await event.answer()


# ---------------------------------------------------------------------------
# Роутинг кнопок
# ---------------------------------------------------------------------------
async def _route(event, state: config.State, sub: Subscriber, action: str, arg: str) -> None:
    cfg = state.cfg
    changed = True

    if action == "m":
        changed = False
        await _menu(event, state, sub, arg)
        return

    if action == "bed":
        _toggle_num(sub.bedrooms, int(arg))
        await _menu(event, state, sub, "bed")
    elif action == "room":
        _toggle_num(sub.rooms, int(arg))
        await _menu(event, state, sub, "room")
    elif action == "budget":
        if arg.startswith("="):
            sub.budget_usd = int(arg[1:])
        elif int(arg) != 0:
            sub.budget_usd = max(
                users_mod.BUDGET_MIN, min(users_mod.BUDGET_MAX, sub.budget_usd + int(arg))
            )
        await _menu(event, state, sub, "budget")
    elif action == "dist":
        if arg == "all":
            sub.district_list.clear()
        else:
            name = districts.BUTTON_ORDER[int(arg)]
            if name in sub.district_list:
                sub.district_list.remove(name)
            else:
                sub.district_list.append(name)
        await _menu(event, state, sub, "dist")
    elif action == "src":
        if arg in sub.sources:
            sub.sources.remove(arg)
        else:
            sub.sources.append(arg)
        await _menu(event, state, sub, "src")
    elif action == "desc":
        if arg == "edit":
            state.awaiting[sub.user_id] = "description"
            changed = False
            await event.edit(
                "📝 Опиши, что тебе важно, обычным текстом — следующим сообщением.\n\n"
                "Цену, комнаты и район указывать не нужно, они уже настроены кнопками.\n"
                "Пиши про остальное, например:\n"
                "«с балконом, можно с котом, не первый этаж, есть стиральная машина»\n\n"
                "Учти: каждое такое пожелание проверяется отдельным запросом к Claude, "
                "но только для объявлений, прошедших остальные фильтры.",
                buttons=kb.description_menu(sub),
            )
        elif arg == "clear":
            sub.description = ""
            state.awaiting.pop(sub.user_id, None)
            await _menu(event, state, sub, "desc")
    elif action == "adm":
        if not sub.is_owner:
            await event.answer("Только владелец.", alert=True)
            return
        changed = _admin_action(cfg, arg)
        await _menu(event, state, sub, "admin")
    elif action == "who":
        changed = False
        target = cfg.user(int(arg))
        await event.answer(
            target.summary() if target else "Нет такого", alert=True
        )
        return
    else:
        changed = False
        await event.answer()
        return

    if changed:
        await config.save(cfg)
    await event.answer()


def _toggle_num(values: list[int], n: int) -> None:
    if n in values:
        values.remove(n)
    else:
        values.append(n)
    values.sort()


def _admin_action(cfg: config.Config, arg: str) -> bool:
    s = cfg.ssge
    if arg == "ssge":
        s["enabled"] = not s["enabled"]
    elif arg == "prefilter":
        cfg.prefilter_enabled = not cfg.prefilter_enabled
    elif arg.startswith("int"):
        s["poll_minutes"] = max(2, min(180, s["poll_minutes"] + int(arg[3:])))
    elif arg.startswith("pg"):
        s["pages"] = max(1, min(10, s["pages"] + int(arg[2:])))
    else:
        return False
    return True


# ---------------------------------------------------------------------------
# Экраны
# ---------------------------------------------------------------------------
async def _menu(event, state: config.State, sub: Subscriber, screen: str) -> None:
    cfg = state.cfg

    if screen in ("main", ""):
        await event.edit(_main_text(sub), buttons=kb.main_menu(sub))
    elif screen == "filters":
        await event.edit(
            "⚙️ Твои фильтры\n\n" + sub.summary(), buttons=kb.filters_menu(sub)
        )
    elif screen == "bed":
        await event.edit(
            "🛏 Сколько спален подходит?\n\n"
            "Отметь все варианты. «3+» — три и больше.\n"
            "Важно: 2-комнатная квартира — это обычно ОДНА спальня и гостиная.",
            buttons=kb.bedrooms_menu(sub),
        )
    elif screen == "room":
        await event.edit(
            "🚪 Сколько комнат подходит?\n\n"
            "Комнаты считаются вместе с гостиной. Если не важно — отметь несколько.",
            buttons=kb.rooms_menu(sub),
        )
    elif screen == "budget":
        await event.edit(
            f"💰 Максимум {sub.budget_usd}$ в месяц\n\n"
            "Считается вся сумма: аренда плюс коммуналка, если она указана.",
            buttons=kb.budget_menu(sub),
        )
    elif screen == "dist":
        chosen = ", ".join(sub.district_list) if sub.district_list else "любые"
        await event.edit(
            f"📍 Районы: {chosen}\n\n"
            "Если не выбран ни один — присылаю по всему Тбилиси.\n"
            "Объявления без указания района проходят всегда.",
            buttons=kb.districts_menu(sub),
        )
    elif screen == "desc":
        if sub.description:
            body = f"📝 Твои пожелания:\n\n{sub.description}"
        else:
            body = (
                "📝 Пожелания не заданы.\n\n"
                "Это свободный текст про то, что не выражается кнопками: балкон, "
                "животные, этаж, техника. Claude сверяет его с каждым объявлением, "
                "уже прошедшим остальные фильтры."
            )
        await event.edit(body, buttons=kb.description_menu(sub))
    elif screen == "stats":
        if not sub.is_owner:
            await event.answer("Только владелец.", alert=True)
            return
        await event.edit(stats_mod.render(state.stats, state.last_claude_error), buttons=kb.stats_menu())
    elif screen == "last":
        if not sub.is_owner:
            await event.answer("Только владелец.", alert=True)
            return
        await event.edit(_last_text(state), buttons=kb.stats_menu())
    elif screen == "src":
        await event.edit(
            "🌐 Откуда брать объявления", buttons=kb.sources_menu(sub)
        )
    elif screen == "status":
        await event.edit(_status_text(state, sub), buttons=kb.filters_menu(sub))
    elif screen == "pause":
        sub.paused = not sub.paused
        await config.save(cfg)
        await event.edit(_main_text(sub), buttons=kb.main_menu(sub))
    elif screen == "people":
        await event.edit(
            f"👥 Люди ({len(cfg.subscribers)})\n\n"
            "✅ активен · ⏳ ждёт решения · 🚫 закрыт\n"
            "Кнопка справа переключает доступ, имя — показывает фильтры.",
            buttons=kb.people_menu(cfg.subscribers),
        )
    elif screen == "admin":
        await event.edit(_admin_text(cfg), buttons=kb.admin_menu(cfg))


def _main_text(sub: Subscriber) -> str:
    head = "🏠 Klumba\n\n"
    if sub.paused:
        head += "⏸ Уведомления на паузе.\n\n"
    return head + sub.summary()


def _status_text(state: config.State, sub: Subscriber) -> str:
    cfg = state.cfg
    now = time.time()
    calls = sum(1 for t in state.api_calls if now - t < 86400)

    lines = ["📊 Что настроено\n", sub.summary(), ""]
    if sub.is_owner:
        chats = ", ".join(c.label() for c in cfg.chats if c.peer_id) or "нет"
        if cfg.ssge["enabled"]:
            ssge = f"каждые {cfg.ssge['poll_minutes']} мин"
            if state.ssge_last_poll:
                ssge += f", последний {int((now - state.ssge_last_poll) / 60)} мин назад"
            if state.ssge_last_error:
                ssge += " ⚠️"
        else:
            ssge = "выключен"
        lines += [
            "— общее —",
            f"Чаты: {chats}",
            f"ss.ge: {ssge} (rooms={cfg.ssge_rooms()})",
            f"Подписчиков активных: {len(cfg.active())}",
            f"Вызовов Claude за 24ч: {calls}",
        ]
        if state.ssge_last_error:
            lines.append(f"⚠️ ss.ge: {state.ssge_last_error}")
    return "\n".join(lines)


def _admin_text(cfg: config.Config) -> str:
    return (
        "🛠 Общие настройки сбора\n\n"
        f"Предфильтр чатов собран из кнопок подписчиков:\n{cfg.bedroom_regex()}\n\n"
        f"ss.ge запрашивает комнаты: {cfg.ssge_rooms()}\n"
        "Эти два значения меняются сами, когда люди правят свои фильтры."
    )


async def _show_main(event, sub: Subscriber, respond: bool = False) -> None:
    await event.respond(_main_text(sub), buttons=kb.main_menu(sub))


def _help_for(sub: Subscriber) -> str:
    return HELP + (OWNER_HELP if sub.is_owner else "")


# ---------------------------------------------------------------------------
# Текстовые команды
# ---------------------------------------------------------------------------
async def _cmd_menu(event, user_client, state, sub, args) -> None:
    await _show_main(event, sub)


async def _cmd_help(event, user_client, state, sub, args) -> None:
    await event.respond(_help_for(sub))


async def _cmd_status(event, user_client, state, sub, args) -> None:
    await event.respond(_status_text(state, sub), buttons=kb.filters_menu(sub))


async def _cmd_stop(event, user_client, state, sub, args) -> None:
    sub.paused = True
    await config.save(state.cfg)
    await event.respond("⏸ Уведомления остановлены. /start — включить обратно.")


async def _cmd_rate(event, user_client, state, sub, args) -> None:
    if not sub.is_owner:
        return
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


async def _cmd_users(event, user_client, state, sub, args) -> None:
    if not sub.is_owner:
        return
    cfg = state.cfg
    await event.respond(
        f"👥 Люди ({len(cfg.subscribers)})", buttons=kb.people_menu(cfg.subscribers)
    )


async def _cmd_chats(event, user_client, state, sub, args) -> None:
    if not sub.is_owner:
        return
    cfg = state.cfg
    action, _, rest = args.partition(" ")
    action = action.lower()
    rest = rest.strip()

    if not action:
        if not cfg.chats:
            await event.respond("Список чатов пуст.\nДобавить: /chats add @username")
            return
        lines = "\n".join(f"  {i}. {c.label()}" for i, c in enumerate(cfg.chats, 1))
        await event.respond(f"📋 Отслеживаю ({len(cfg.chats)}):\n{lines}")
        return

    if action == "add":
        if not rest:
            await event.respond("Укажи чат: /chats add @username или id")
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

    if action in ("rm", "remove", "del", "delete"):
        if not rest:
            await event.respond("Укажи чат: /chats rm @username")
            return
        chat = cfg.chat_by_ref(rest)
        if chat is None:
            await event.respond(f"Чата «{rest}» нет в списке. Посмотреть: /chats")
            return
        cfg.chats.remove(chat)
        await config.save(cfg)
        await event.respond(f"✅ Убран: {chat.label()}\nОсталось: {len(cfg.chats)}")
        return

    await event.respond("Есть /chats, /chats add <чат>, /chats rm <чат>")


def _last_text(state: config.State) -> str:
    """Последние разборы и кому они ушли — ответ на «почему мне ничего не пришло»."""
    entries = state.recent_verdicts
    if not entries:
        if state.last_claude_error:
            return (
                "Пока ни одного разбора, потому что Claude отвечает ошибкой:\n\n"
                f"{state.last_claude_error[:300]}\n\n"
                "Объявления собираются, но разобрать их не получается. "
                "Проверь ANTHROPIC_API_KEY и баланс на platform.claude.com."
            )
        return (
            "Пока ни одного разбора.\n\n"
            "Объявление попадает сюда, только когда дошло до Claude: прошло "
            "предфильтр (чаты) или отсев по цене (ss.ge) и не оказалось дублем."
        )

    lines = [f"🔍 Последние разборы ({len(entries)})", ""]
    for e in entries:
        f = e["facts"]
        ago = int((time.time() - e["at"]) / 60)
        lines.append(
            f"— {e['origin']}, {ago} мин назад\n"
            f"  {f.get('summary') or '(без описания)'}\n"
            f"  цена {f.get('price_usd') or '?'}$ · спален {f.get('bedrooms') or '?'}"
            f" · комнат {f.get('rooms') or '?'} · {f.get('district') or 'район не указан'}"
            f" · долгосрочно: {'да' if f.get('is_long_term', True) else 'НЕТ'}"
        )
        for v in e["verdicts"] or ["(активных подписчиков не было)"]:
            lines.append(f"  · {v}")
        if e.get("link"):
            lines.append(f"  {e['link']}")
        lines.append("")
    return "\n".join(lines)[:4000]


async def _cmd_last(event, user_client, state, sub, args) -> None:
    if not sub.is_owner:
        return
    await event.respond(_last_text(state))


async def _cmd_stats(event, user_client, state, sub, args) -> None:
    if not sub.is_owner:
        return
    await event.respond(stats_mod.render(state.stats, state.last_claude_error), buttons=kb.stats_menu())


_HANDLERS = {
    "/menu": _cmd_menu,
    "/stats": _cmd_stats,
    "/last": _cmd_last,
    "/help": _cmd_help,
    "/status": _cmd_status,
    "/stop": _cmd_stop,
    "/rate": _cmd_rate,
    "/users": _cmd_users,
    "/chats": _cmd_chats,
}
