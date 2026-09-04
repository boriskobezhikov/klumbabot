"""
Самопроверка окружения: что настроено и что реально отвечает.

Запуск на VPS:
    /opt/klumba-bot/venv/bin/python /opt/klumba-bot/deploy/doctor.py

Ничего не меняет — только читает конфиг и делает по одному пробному запросу
к Claude и к ss.ge. Секреты не печатает: только длину и первые символы.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.chdir(Path(__file__).resolve().parent.parent)

from dotenv import load_dotenv

load_dotenv()

OK, BAD, WARN = "  ✅", "  ❌", "  ⚠️ "
problems: list[str] = []


def fail(msg: str) -> None:
    problems.append(msg)


print("\n=== 1. Переменные окружения ===")
for name, required in (
    ("TG_API_ID", True),
    ("TG_API_HASH", True),
    ("NOTIFY_BOT_TOKEN", True),
    ("NOTIFY_CHAT_ID", True),
    ("ANTHROPIC_API_KEY", True),
    ("CLAUDE_MODEL", False),
    ("ANTHROPIC_WORKSPACE_ID", False),
):
    val = os.environ.get(name)
    if not val:
        if required:
            print(f"{BAD} {name} не задан")
            fail(f"{name} отсутствует в .env")
        else:
            print(f"{OK} {name} не задан (будет значение по умолчанию)")
        continue
    if name in ("ANTHROPIC_API_KEY", "TG_API_HASH", "NOTIFY_BOT_TOKEN"):
        shown = f"{val[:12]}… ({len(val)} символов)"
    else:
        shown = val
    print(f"{OK} {name} = {shown}")

key = os.environ.get("ANTHROPIC_API_KEY", "")
if key and not key.startswith("sk-ant-"):
    print(f"{WARN} ANTHROPIC_API_KEY не начинается с sk-ant- — похоже, это не ключ Claude")
    fail("ANTHROPIC_API_KEY выглядит неправильно")

print("\n=== 2. Конфиг и подписчики ===")
try:
    import config

    cfg = config.load()
    print(f"{OK} config.json прочитан: {config.CONFIG_PATH}")
    active = cfg.active()
    print(f"{OK} подписчиков всего: {len(cfg.subscribers)}, активных: {len(active)}")
    if not active:
        print(f"{BAD} активных подписчиков нет — уведомления некому слать")
        fail("нет активных подписчиков")
    for s in cfg.subscribers:
        mark = OK if s.receives else WARN
        print(f"{mark} {s.label()} [{s.status}{', пауза' if s.paused else ''}]")
        print(f"        {s.summary().replace(chr(10), chr(10) + '        ')}")
    print(f"{OK} ss.ge: {'вкл' if cfg.ssge['enabled'] else 'ВЫКЛ'}, "
          f"каждые {cfg.ssge['poll_minutes']} мин, комнаты {cfg.ssge_rooms()}")
    if not cfg.ssge["enabled"]:
        print(f"{WARN} опрос ss.ge выключен")
    print(f"{OK} чатов отслеживается: {len([c for c in cfg.chats if c.peer_id])}")
    print(f"{OK} предфильтр чатов: {'вкл' if cfg.prefilter_enabled else 'выкл'}")
except Exception as e:  # noqa: BLE001
    print(f"{BAD} не смог прочитать конфиг: {e}")
    fail(f"конфиг не читается: {e}")
    cfg = None

print("\n=== 3. Claude API (один пробный запрос) ===")
model = os.environ.get("CLAUDE_MODEL", "claude-haiku-4-5-20251001")
print(f"     модель: {model}")
if not key:
    print(f"{BAD} ключа нет — запрос делать нечем")
    fail("ANTHROPIC_API_KEY не задан, разбор объявлений невозможен")
try:
    from anthropic import Anthropic

    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY пуст")
    ws = os.environ.get("ANTHROPIC_WORKSPACE_ID", "").strip()
    if ws:
        print(f"     рабочее пространство: {ws}")
    client = Anthropic(
        api_key=key,
        default_headers={"anthropic-workspace-id": ws} if ws else None,
    )
    resp = client.messages.create(
        model=model,
        max_tokens=20,
        messages=[{"role": "user", "content": "Ответь одним словом: работает"}],
    )
    text = "".join(b.text for b in resp.content if b.type == "text")
    print(f"{OK} ответ получен: {text.strip()[:60]}")
    print(f"{OK} токенов: {resp.usage.input_tokens} вход / {resp.usage.output_tokens} выход")
except Exception as e:  # noqa: BLE001
    name = type(e).__name__
    print(f"{BAD} {name}: {e}")
    if "anthropic-workspace-id" in str(e):
        if "must be a valid" in str(e):
            print(f"{WARN} id рабочего пространства задан, но неверный.")
            print("       Исправь ANTHROPIC_WORKSPACE_ID в /opt/klumba-bot/.env")
        else:
            print(f"{WARN} ключ привязан к личности и требует id рабочего пространства.")
            print("       Добавь в /opt/klumba-bot/.env строку:")
            print("           ANTHROPIC_WORKSPACE_ID=wrkspc_...")
        print("       Взять id: platform.claude.com -> Settings -> Workspaces,")
        print("       либо скопировать из адреса вида")
        print("       platform.claude.com/workspaces/<вот-этот-id>/...")
        print("       Затем: systemctl restart klumba-bot")
    hint = {
        "AuthenticationError": "ключ неверный или отозван — пересоздай на platform.claude.com",
        "PermissionDeniedError": "у ключа нет доступа к модели или не привязана карта",
        "NotFoundError": f"модель {model} не найдена — проверь CLAUDE_MODEL",
        "RateLimitError": "упёрся в лимиты — подожди или подними тариф",
        "APIConnectionError": "нет сети до api.anthropic.com (VPN/файрвол/DNS?)",
    }.get(name)
    if hint:
        print(f"{WARN} {hint}")
    fail(f"Claude недоступен ({name})")

print("\n=== 4. ss.ge (одна страница) ===")
try:
    import asyncio

    import ssge

    rooms = cfg.ssge_rooms() if cfg else "1,2"
    ls = asyncio.run(ssge.fetch_listings(pages=1, city_id=95, rooms=rooms))
    print(f"{OK} получено объявлений: {len(ls)}")
    if ls:
        l = ls[0]
        print(f"{OK} пример: {l.price_usd}$ · спален {l.bedrooms} · {l.location()}")
        print(f"        {l.url}")

        # Удобства (балкон, мебель) живут только на детальной странице. Если
        # догрузка не работает, Claude судит по одному описанию — и отсеивает
        # квартиры «потому что про балкон не написано».
        import httpx

        async def _detail():
            async with httpx.AsyncClient(timeout=30) as http:
                return await ssge.fetch_details(http, l)

        if asyncio.run(_detail()):
            print(f"{OK} карточка догружена: этаж {l.floor or '?'}, {l.condition or '?'}")
            print(f"{OK} удобства: {', '.join(l.features) or 'ни одного не отмечено'}")
        else:
            print(f"{WARN} карточку догрузить не удалось — Claude увидит только описание,")
            print("       и может отсеивать квартиры за неупомянутый балкон/мебель")
    else:
        print(f"{WARN} страница пуста — возможно, изменилась структура сайта")
except Exception as e:  # noqa: BLE001
    print(f"{BAD} {type(e).__name__}: {e}")
    fail(f"ss.ge недоступен ({type(e).__name__})")

print("\n=== 5. Сколько объявлений прошло бы фильтры ===")
try:
    if cfg and cfg.active() and "ls" in dir() and ls:
        import users as u

        for sub in cfg.active():
            passed, reasons = 0, {}
            for l in ls:
                facts = {
                    "is_rental_offer": True, "is_long_term": True,
                    "price_usd": l.price_usd, "bedrooms": l.bedrooms, "rooms": None,
                    "area_m2": l.area_m2,
                    "district": __import__("districts").normalize(l.district),
                }
                ok, why = u.matches(sub, facts, "ssge")
                if ok:
                    passed += 1
                else:
                    top = why.split(",")[0][:38]
                    reasons[top] = reasons.get(top, 0) + 1
            mark = OK if passed else BAD
            print(f"{mark} {sub.label()}: прошло {passed} из {len(ls)}")
            for why, n in sorted(reasons.items(), key=lambda x: -x[1])[:3]:
                print(f"        отсеяно {n}: {why}")
            if not passed:
                fail(f"фильтры {sub.label()} не пропускают ничего")
except Exception as e:  # noqa: BLE001
    print(f"{WARN} не смог посчитать: {e}")

print("\n" + "=" * 50)
if problems:
    print(f"НАЙДЕНО ПРОБЛЕМ: {len(problems)}")
    for p in problems:
        print(f"  • {p}")
    sys.exit(1)
print("Всё в порядке.")
