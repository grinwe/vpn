"""Страница «починить и продлить» на домене саб-ссылки — без Telegram.

Зачем: человек со сломанным VPN заперт в круге — кнопка «VPN не работает» и
оплата живут в Telegram-боте, а Telegram без VPN бывает недоступен. Домен
саб-ссылки доступен без VPN по определению (с него клиент и так тянет
конфиги), поэтому на нём же отдаём страницу, которая по sub_token умеет
три действия: шаг лестницы ротации, счёт на продление картой и обратную
связь по сделанной починке (оператор связи, «помогло / не помогло»).

Решения, которые здесь важны:

* **query-параметр, а не новый путь.** CF Worker матчит строго один сегмент
  пути (``/<token>``), но метод и query-строку пробрасывает как есть —
  ``?fix=1`` работает без правки воркера. Тело POST воркер не форвардит,
  поэтому форма без полей: всё в query.
* **починка — POST, не GET.** GET дёргают браузерные префетчеры, антивирусы
  и превью-фетчеры; каждый такой вызов сжигал бы шаг лестницы, а он конечен.
* **HTML только для браузера.** Дискриминатор — ``Accept: text/html``:
  человек, вставивший ссылку с ``?fix=1`` в VPN-клиент как подписку, всё
  равно получит нормальный base64-конфиг, а не HTML-мусор в профиле.
* **неизвестный токен = camo-лендинг**, тот же, что на корне домена.
  Никаких «подписка не найдена»: разница в статусе/размере/тексте сама
  становится оракулом для пробера.
"""
from __future__ import annotations

import hashlib
import hmac
import html
import logging
import math
import os
import re
import time
from string import Template

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from .. import models
from ..services import self_repair
from ..time_utils import utcnow

logger = logging.getLogger("app.api.sub_fix")

# Окно nonce. Это CSRF-токен (защита от «чужая страница отправила форму от
# имени пользователя»), а НЕ аутентификация: секрет всё равно сам токен.
_NONCE_WINDOW_SEC = 900


def page_enabled() -> bool:
    return (os.getenv("SUB_FIX_PAGE") or "0").strip().lower() in (
        "1", "true", "on", "yes",
    )


def pay_enabled() -> bool:
    return (os.getenv("SUB_FIX_PAY") or "0").strip().lower() in (
        "1", "true", "on", "yes",
    )


def wants_html(request: Request) -> bool:
    """Просят ли HTML.

    VPN-клиенты (Happ, Hiddify, v2rayNG) ``text/html`` в Accept не шлют, а
    браузеры шлют всегда. Без этой проверки человек, случайно вставивший
    ссылку с ``?fix=1`` в клиент как подписку, получил бы HTML вместо
    конфигов и сломал себе профиль.
    """
    return "text/html" in (request.headers.get("accept") or "").lower()


def _throttle_sec() -> int:
    """Окно повторов — тонкая обёртка над единой политикой ядра.

    Своих настроек у страницы больше нет: SUB_FIX_THROTTLE_SEC /
    SUB_FIX_DAILY_MAX ядро читает само как fallback к SELF_REPAIR_*, и окно
    одно на бот, кабинет и страницу. Обёртки оставлены ради текста «через
    N мин.» и тестов, которые на них ссылаются.
    """
    return self_repair.default_throttle_sec()


def _daily_max() -> int:
    """Суточный потолок починок — см. ``_throttle_sec``."""
    return self_repair.default_daily_max()


def pay_provider(method: str = "card") -> str:
    """Каким провайдером платит человек СО СТРАНИЦЫ.

    Пинить обязательно: без явного имени ``get_provider`` берёт провайдера из
    общей ротации, а её дефолт в проде — cryptobot. Страница существует ровно
    для того, у кого нет Telegram и, скорее всего, нет криптокошелька: он
    пришёл платить картой или по СБП. ``card`` → ``SUB_FIX_PROVIDER``
    (дефолт lava_top), ``sbp`` → ``SUB_FIX_SBP_PROVIDER`` (дефолт lava_top_sbp).
    """
    if method == "sbp":
        return (os.getenv("SUB_FIX_SBP_PROVIDER") or "lava_top_sbp").strip() or "lava_top_sbp"
    return (os.getenv("SUB_FIX_PROVIDER") or "lava_top").strip() or "lava_top"


def make_nonce(token: str, *, offset: int = 0) -> str:
    """CSRF-nonce, привязанный к токену и 15-минутному окну."""
    secret = (os.getenv("APP_SECRET_KEY") or "").encode() or b"dev-insecure"
    bucket = int(time.time() // _NONCE_WINDOW_SEC) - offset
    digest = hmac.new(secret, f"{token}:{bucket}".encode(), hashlib.sha256)
    return digest.hexdigest()[:16]


def nonce_valid(token: str, nonce: str | None, *, buckets: int = 2) -> bool:
    """Текущее окно и предыдущее: страница, открытая 14 минут назад, обязана
    работать — иначе человек с плохой связью не успеет нажать кнопку.

    ``buckets`` — сколько 15-минутных окон принимать (2 = 15–30 мин). Формы
    обратной связи просят шире: человек уходит в клиент переподключаться и
    возвращается через полчаса-час.
    """
    if not nonce:
        return False
    return any(
        hmac.compare_digest(nonce, make_nonce(token, offset=off))
        for off in range(max(1, buckets))
    )


# ── HTML ────────────────────────────────────────────────────────────────
#
# Страница самодостаточна: ни внешнего CSS, ни шрифтов, ни картинок, ни JS.
# Кнопка — обычная форма, работает без JavaScript. Причина простая: её
# открывают с телефона на дохлом мобильном интернете и часто в вебвью
# VPN-клиента, где половина внешних ресурсов не загрузится.

_CAMO = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Internal Tools Portal</title>
<style>
body{font-family:system-ui,sans-serif;max-width:720px;margin:4rem auto;padding:2rem;color:#333;line-height:1.6}
h1{color:#1a1a1a;border-bottom:1px solid #ddd;padding-bottom:.5rem}
.meta{color:#888;font-size:.9rem;margin-top:3rem}
</style>
</head>
<body>
<h1>Internal Tools Portal</h1>
<p>This domain hosts internal operational tooling. Access is restricted to authorized personnel.</p>
<p>If you believe you reached this page in error, please contact your system administrator.</p>
<div class="meta">&copy; Internal Tools &middot; maintained by operations</div>
</body>
</html>"""

_PAGE = Template("""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex,nofollow">
$refresh
<title>Подключение</title>
<style>
:root{color-scheme:light dark}
body{font-family:system-ui,-apple-system,sans-serif;margin:0;padding:1.5rem;
     line-height:1.5;background:#f5f6f8;color:#16181d}
main{max-width:30rem;margin:0 auto}
.card{background:#fff;border-radius:14px;padding:1.25rem;margin-bottom:1rem;
      box-shadow:0 1px 3px rgba(0,0,0,.08)}
h1{font-size:1.25rem;margin:0 0 .75rem}
p{margin:.5rem 0}
.muted{color:#6b7280;font-size:.9rem}
.ok{color:#0f7b34}
.warn{color:#b45309}
.bad{color:#b91c1c}
button,.btn{display:block;box-sizing:border-box;width:100%;
       padding:.85rem 1rem;font-size:1rem;font-weight:600;text-align:center;
       text-decoration:none;border:0;border-radius:10px;
       background:#2563eb;color:#fff;cursor:pointer}
button.secondary,.btn.secondary{background:#e5e7eb;color:#16181d}
button:disabled{opacity:.45;cursor:not-allowed}
form{margin:0 0 .6rem}
.btn{margin-bottom:.6rem}
ol{padding-left:1.2rem}
a{color:#2563eb}
@media (prefers-color-scheme:dark){
  body{background:#0f1115;color:#e5e7eb}
  .card{background:#181b21;box-shadow:none}
  button.secondary,.btn.secondary{background:#2a2f3a;color:#e5e7eb}
  .muted{color:#9ca3af}
}
</style>
</head>
<body><main>
<div class="card">
<h1>$title</h1>
$body
</div>
$actions
<p class="muted">Эта страница работает без VPN и без Telegram.</p>
</main></body>
</html>""")


def _render(
    *,
    title: str,
    body: str,
    actions: str = "",
    refresh_sec: int | None = None,
    refresh_url: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    refresh = ""
    if refresh_sec:
        target = f";url={html.escape(refresh_url)}" if refresh_url else ""
        refresh = f'<meta http-equiv="refresh" content="{refresh_sec}{target}">'
    page = _PAGE.substitute(
        title=html.escape(title), body=body, actions=actions, refresh=refresh
    )
    return HTMLResponse(
        content=page,
        status_code=status_code,
        headers={
            "cache-control": "no-store, private",
            "pragma": "no-cache",
            # Иначе токен утечёт в Referer при переходе в Telegram/на оплату.
            "referrer-policy": "no-referrer",
            "x-content-type-options": "nosniff",
            "x-frame-options": "DENY",
            "x-robots-tag": "noindex",
        },
    )


def camo_response(status_code: int = 200) -> HTMLResponse:
    """Скучный лендинг — ответ на неизвестный токен и на выключенную страницу.

    Ответ обязан совпадать с корнем домена ПОБАЙТОВО и по заголовкам, включая
    код 200: иначе сам код ответа становится оракулом («404 → токена нет,
    200 → есть»), а ради этого camo и существует. ``status_code`` оставлен
    параметром только для вызовов, где смысл именно в отказе.

    Кэш публичный и часовой — как на корне; для НАШЕГО человека это не
    страшно, потому что путь с валидным токеном сюда не попадает.
    """
    return HTMLResponse(
        content=_CAMO,
        status_code=status_code,
        headers={
            "content-type": "text/html; charset=utf-8",
            "cache-control": "public, max-age=3600",
            "strict-transport-security": "max-age=31536000",
        },
    )


def _button_form(token: str, label: str, *, extra: str = "", secondary=False) -> str:
    """Кнопка-форма. Все параметры в query: тело POST через CF Worker не едет."""
    cls = ' class="secondary"' if secondary else ""
    return (
        f'<form method="post" action="?fix=1&n={html.escape(make_nonce(token))}{extra}">'
        f"<button{cls}>{html.escape(label)}</button></form>"
    )


def _page_base_url() -> str:
    """Публичный адрес самой страницы — домен саб-ссылки."""
    return (os.getenv("SUB_LINK_BASE_URL") or "").strip().rstrip("/")


def _return_url(token: str, invoice_id: int) -> str | None:
    """URL возврата с оплаты: тот же токен + ``?paid=<id>``."""
    base = _page_base_url()
    if not base:
        return None
    return f"{base}/{token}?fix=1&paid={invoice_id}"


def _support_url() -> str | None:
    """Диалог с админом: ``t.me/<bot>?start=support`` открывает FSM поддержки —
    человек пишет одно сообщение, оно уходит админу, ответ приходит туда же."""
    bot = (os.getenv("BOT_USERNAME") or "").strip()
    return f"https://t.me/{bot}?start=support" if bot else None


def _help_button(*, enabled: bool, label: str = "Не помогло? Напишите нам") -> str:
    """Кнопка «Написать в поддержку» — активная только после починки.

    Порядок намеренный: сначала человек жмёт «Починить», получает новый набор
    серверов и пробует. Живой админ — дорогой ресурс, и звать его до того,
    как отработала автоматика, значит тратить его на случаи, которые
    чинятся кнопкой за секунду. Пока не починили — кнопка неактивна и прямо
    объясняет, что нажать сначала.
    """
    url = _support_url()
    if not url:
        return ""
    if not enabled:
        return (
            '<button class="secondary" disabled>Не помогло? Напишите нам</button>'
            '<p class="muted">Сначала нажмите «Починить подключение». '
            "Если не помогло, эта кнопка станет активной.</p>"
        )
    return (
        f'<a class="btn secondary" href="{html.escape(url)}">'
        f"{html.escape(label)}</a>"
    )


def _telegram_link() -> str:
    bot = (os.getenv("BOT_USERNAME") or "").strip()
    if not bot:
        return ""
    return (
        f'<p class="muted">Если Telegram доступен, '
        f'<a href="https://t.me/{html.escape(bot)}?start=account">открыть личный кабинет</a>.</p>'
    )


def _expiry_line(sub) -> str:
    if not sub.expires_at:
        return ""
    expires = sub.expires_at
    from datetime import timedelta, timezone

    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    msk = expires.astimezone(timezone(timedelta(hours=3)))
    left = expires - utcnow().replace(tzinfo=timezone.utc)
    if left.total_seconds() <= 0:
        return f'<p class="bad">Подписка закончилась {msk.strftime("%d.%m.%Y")}.</p>'
    days = left.days
    if days >= 1:
        return f'<p class="muted">Активна до {msk.strftime("%d.%m.%Y")} ({days} дн.).</p>'
    hours = max(1, int(left.total_seconds() // 3600))
    return f'<p class="warn">Заканчивается через {hours} ч.</p>'


# ── Экраны ──────────────────────────────────────────────────────────────


# Имена, которые ставит провижининг, а не человек: primary, device-6,
# default-2… Показывать их нельзя: «Мы переключим device-6» — это разговор
# с инженером, а не с пользователем. Паттерн, а не список: нумерованные
# варианты плодятся при каждом новом устройстве.
_TECH_NAME_RE = re.compile(
    r"^(primary|device|устройство|default|user)[\s_-]*\d*$", re.IGNORECASE
)


def render_start(
    sub, token: str, *, device_name: str | None, repairable: bool = True
) -> HTMLResponse:
    """Главный экран: кнопка починки (и продления, если включено)."""
    name = (device_name or "").strip()
    if not name or _TECH_NAME_RE.match(name):
        name = "это устройство"
    who = html.escape(name)
    if repairable:
        body = (
            f"<p>Не подключается? Нажмите кнопку ниже. "
            f"Мы переключим <b>{who}</b> на другой способ связи или другой сервер.</p>"
        )
    else:
        # Legacy-подписочный токен: устройства за ним нет, чинить нечего —
        # кнопка, которая всегда отвечает «не нашли устройство», хуже, чем
        # её отсутствие.
        body = (
            "<p>По этой ссылке автоматическая починка недоступна. "
            "Обновите профиль в клиенте из личного кабинета.</p>"
        )
    body += _expiry_line(sub)
    actions = _button_form(token, "Починить подключение") if repairable else ""
    if pay_enabled():
        actions += _renew_button(sub, token)
    # Пока не чинили — «Напишите нам» неактивна (см. _help_button). Для
    # legacy-токена чинить нечего, поэтому там она сразу доступна.
    actions += _help_button(enabled=not repairable)
    return _render(title="Что-то не работает?", body=body, actions=actions)


def _renew_button(sub, token: str) -> str:
    """Кнопка продления С ЦЕНОЙ на ней.

    Сумму человек обязан увидеть ДО платёжного виджета: иначе он жмёт кнопку
    и попадает сразу на списание, не зная ни цены, ни срока.
    """
    from ..services.balance import total_renewal_cost_kopecks

    rub = total_renewal_cost_kopecks(sub) / 100 if sub.plan else 0
    days = sub.plan.duration_days if sub.plan else 0
    if rub <= 0:
        return ""
    # Две кнопки, как в кабинете и боте: СБП (lava_top_sbp) и карта
    # (lava_top). Единой «карта / СБП» больше нет — lava закрыл карту у
    # агрегатора PAY2ME (2026-09-19).
    button = _button_form(
        token, f"Продлить по СБП на {days} дн. за {rub:g} ₽",
        extra="&pay=sbp", secondary=True,
    )
    button += _button_form(
        token, f"Продлить картой на {days} дн. за {rub:g} ₽",
        extra="&pay=1", secondary=True,
    )
    # Сумма выше цены плана — значит в ней доплата за дополнительные
    # устройства. Без расшифровки человек видит цифру, не совпадающую с
    # тарифом, и решает, что мы ошиблись.
    slots = sub.extra_device_slots or 0
    if slots:
        base = float(sub.plan.price)
        extra = rub - base
        button += (
            f'<p class="muted">{base:g} ₽ тариф плюс {extra:g} ₽ '
            f"за дополнительные устройства ({slots}).</p>"
        )
    return button


# Подписи операторов — те же, что в боте (_OPERATOR_LABELS) и кабинете
# (VPN_OPERATORS): человек видит одни и те же слова во всех трёх каналах.
# Значения — ключи self_repair.OPERATORS; порядок = порядок кнопок.
_OPERATOR_LABELS = (
    ("mts", "МТС"),
    ("beeline", "Билайн"),
    ("megafon", "МегаФон (Yota)"),
    ("tele2", "Tele2 (Т-Мобайл)"),
    ("home_wifi", "Домашний Wi-Fi"),
    ("other", "Другое"),
)

_REFRESH_HINT = (
    "<ol>"
    "<li>Откройте ваш VPN-клиент</li>"
    "<li>Нажмите 🔄 (обновить подписку) на профиле</li>"
    "<li>Подключитесь заново</li>"
    "</ol>"
    '<p class="muted">Без обновления клиент ещё какое-то время будет '
    "показывать старые серверы.</p>"
)


def _retry_button(token: str, *, secondary: bool = False) -> str:
    """«Попробовать ещё раз» — обычная починка. Клиентских блокировок нет
    намеренно: сервер сам ответит «уже чиним», если окно не вышло."""
    return _button_form(token, "Попробовать ещё раз", secondary=secondary)


def _retry_minutes(outcome) -> int:
    """Через сколько минут звать снова — из ``retry_after_sec`` ядра,
    вверх: «через 0 мин.» человек прочитает как «прямо сейчас»."""
    sec = outcome.retry_after_sec or _throttle_sec()
    return max(1, math.ceil(sec / 60))


def _operator_block(token: str, report_id: int) -> str:
    """Вопрос об операторе — шесть кнопок и «Пропустить».

    Ответ ложится в ``OperatorNodeReport.operator`` и идёт в крауд-матрицу
    node×operator: по ней видно «легло у МТС, а у Билайна живо», то есть
    блокировку, а не смерть ноды. «Пропустить» ведёт на тот же экран
    обратной связи, что и ответ: вопрос не должен быть шлагбаумом.
    """
    forms = "".join(
        _button_form(token, label, extra=f"&report={report_id}&op={value}")
        for value, label in _OPERATOR_LABELS
    )
    return forms + _button_form(
        token, "Пропустить", extra=f"&report={report_id}&op=skip", secondary=True
    )


def render_outcome(outcome, sub, token: str) -> HTMLResponse:
    """Экран результата починки — текст зависит от шага лестницы.

    Семантика исходов и тексты едины с ботом и кабинетом (унификация
    2026-09-12), отличие только в «вы». После успешного шага — вопрос об
    операторе, затем экран «помогло / не помогло»; на любом неуспехе —
    повтор и живой человек, тупиковых экранов нет.
    """
    if outcome.repaired:
        if outcome.action == "reshuffled":
            lead = '<p class="ok">Переключили вас на другой способ связи.</p>'
        elif outcome.action == "migrated":
            # Без имени ноды: внутренние имена (ufo-ru-01) — разговор с
            # инженером, а не с пользователем; бот и кабинет их не показывают.
            lead = '<p class="ok">Перевели вас на другой сервер.</p>'
        else:  # duplicated
            lead = (
                '<p class="ok">Добавили запасной сервер. В клиенте появится ещё '
                "одна строка.</p>"
            )
        body = lead + _REFRESH_HINT
        if outcome.report_id is None:
            # Репорта нет — ни оператора, ни «помогло/не помогло» привязать
            # не к чему. Каждый шаг лестницы репорт пишет, так что это
            # страховка, но экран без действий недопустим.
            actions = _retry_button(token, secondary=True) + _help_button(enabled=True)
            return _render(title="Готово", body=body, actions=actions)
        body += (
            "<p>Чтобы мы быстрее ловили блокировки, подскажите: "
            "какой у вас интернет?</p>"
        )
        actions = _operator_block(token, outcome.report_id) + _help_button(enabled=True)
        return _render(title="Готово", body=body, actions=actions)

    if outcome.action == "throttled":
        title, body = "Уже чиним", (
            '<p class="warn">Мы переключали вас пару минут назад.</p>'
            "<p>Откройте клиент и нажмите 🔄: новые серверы уже там. "
            "Если не помогло, попробуйте ещё раз через "
            f"{_retry_minutes(outcome)} мин.</p>"
        )
    elif outcome.action == "daily_limit":
        title, body = "Слишком часто", (
            '<p class="warn">Сегодня мы уже несколько раз меняли вам серверы.</p>'
            "<p>Дальше нужна помощь человека. Напишите нам.</p>"
        )
    elif outcome.action == "no_target":
        title, body = "Сейчас не получилось", (
            '<p class="warn">Свободного сервера нет прямо сейчас.</p>'
            "<p>Попробуйте через 10 минут: они освобождаются постоянно.</p>"
        )
    elif outcome.action == "not_ready":
        # Устройство pending: кредов ещё нет, перетасовывать нечего, а
        # перенос запустил бы второй провижн поверх первого.
        title, body = "Ещё настраивается", (
            '<p class="warn">Устройство ещё настраивается.</p>'
            "<p>Подождите пару минут, нажмите 🔄 в клиенте и попробуйте снова.</p>"
        )
    else:  # no_subscription
        # Сюда попадает и «только что починили»: свежепровиженное устройство
        # какое-то время pending, и alias его ещё не видит. Текст обязан это
        # учитывать — иначе человек, у которого починка СРАБОТАЛА, читает
        # «ничего не нашли» и идёт жаловаться второй раз.
        title, body = "Проверьте клиент", (
            "<p>Похоже, серверы уже переключены.</p>" + _REFRESH_HINT
        )
    # Автоматика отработала (или упёрлась в потолок) — живой человек
    # уместен, кнопка активна. При суточном потолке она идёт первой: текст
    # выше прямо говорит, что дальше нужен человек.
    if outcome.action == "daily_limit":
        actions = _help_button(enabled=True) + _retry_button(token, secondary=True)
    else:
        actions = _retry_button(token) + _help_button(enabled=True)
    return _render(title=title, body=body, actions=actions)


def render_feedback_prompt(token: str, report_id: int) -> HTMLResponse:
    """«Помогло?» сразу после вопроса об операторе.

    Бот дожимает этот вопрос отложенным сообщением через 15 минут; страница
    пуш не умеет, поэтому спрашивает сразу и оставляет вкладку открытой:
    человек идёт в клиент, пробует подключиться и возвращается нажать одну
    из двух кнопок.
    """
    body = (
        '<p class="ok">Готово. Откройте клиент, нажмите 🔄 и подключитесь '
        "заново.</p><p>Получилось?</p>"
    )
    actions = (
        _button_form(token, "✅ Всё работает", extra=f"&report={report_id}&ok=1")
        + _button_form(
            token, "❌ Всё равно не работает",
            extra=f"&report={report_id}&still=1", secondary=True,
        )
        + _retry_button(token, secondary=True)
        + _help_button(enabled=True)
    )
    return _render(title="Проверьте подключение", body=body, actions=actions)


def render_still_broken(token: str) -> HTMLResponse:
    """«Всё равно не работает»: репорт закрыт как fail, зовём человека."""
    body = (
        "<p>Передаём в поддержку: напишите нам, опишите проблему и "
        "приложите модель устройства.</p>"
    )
    actions = _help_button(enabled=True, label="Написать в поддержку") + _retry_button(
        token, secondary=True
    )
    return _render(title="Жаль, что не помогло", body=body, actions=actions)


def render_all_good() -> HTMLResponse:
    """«Всё работает»: репорт закрыт как ok."""
    body = (
        '<p class="ok">Отлично, рады, что заработало!</p>'
        "<p>Если снова сломается, эта страница всегда под рукой.</p>"
    )
    return _render(
        title="Всё работает",
        body=body,
        actions=_help_button(enabled=True, label="Написать в поддержку"),
    )


def render_expired(sub, token: str) -> HTMLResponse:
    """Подписка кончилась — чинить нечего, показываем продление."""
    body = _expiry_line(sub) + (
        "<p>Пока подписка не продлена, VPN не подключается.</p>"
    )
    actions = _renew_button(sub, token) if pay_enabled() else ""
    if not actions:
        # Без включённой оплаты экран остался бы вообще без действий —
        # тупик ровно для того человека, ради которого страница написана.
        body += (
            "<p>Продлить можно в личном кабинете. Если Telegram недоступен, "
            "попробуйте открыть его через мобильный интернет другого "
            "оператора или Wi-Fi.</p>"
        )
    return _render(title="Подписка закончилась", body=body + _telegram_link(), actions=actions)


def render_rate_limited() -> HTMLResponse:
    """429 в человеческом виде (см. обработчик в main.py).

    Не тупик: кнопка поддержки и ссылка обратно на страницу (GET под лимит
    не попадает). Форму «Попробовать ещё раз» здесь ставить нельзя — она
    упёрлась бы в тот же лимит.
    """
    actions = (
        '<a class="btn secondary" href="?fix=1">Открыть страницу заново</a>'
        + _help_button(enabled=True, label="Написать в поддержку")
    )
    return _render(
        title="Слишком часто",
        body="<p>Вы нажимали кнопку несколько раз подряд.</p>"
        "<p>Подождите минуту и попробуйте снова. Предыдущее нажатие могло "
        "уже сработать: откройте клиент и нажмите 🔄.</p>" + _telegram_link(),
        actions=actions,
        status_code=429,
    )


def render_inactive(sub, token: str, *, banned: bool = False) -> HTMLResponse:
    """Пауза, блокировка или бан — объясняем и уводим к человеку.

    ``banned`` — глобальный бан владельца (``User.banned_at``): подписка
    может быть формально active, но чинить и платить ему нельзя, как в боте,
    где мидлвара дропает его апдейты. Причину на страницу не выносим.
    """
    actions = ""
    if banned:
        title = "Доступ приостановлен"
        body = "<p>Доступ к сервису ограничен. Разобраться поможет поддержка.</p>"
        actions = _help_button(enabled=True, label="Написать в поддержку")
    elif sub.status == models.SubscriptionStatus.frozen:
        title = "Подписка на паузе"
        body = "<p>Вы поставили подписку на паузу. Снять её можно в личном кабинете.</p>"
    else:
        title = "Доступ приостановлен"
        body = "<p>Подписка заблокирована. Разобраться поможет поддержка.</p>"
        # Текст зовёт в поддержку — кнопка обязана быть, иначе это тупик.
        actions = _help_button(enabled=True, label="Написать в поддержку")
    return _render(title=title, body=body + _telegram_link(), actions=actions)


def render_pay_pending(invoice_id: int, token: str, *, paid: bool) -> HTMLResponse:
    """Ожидание подтверждения оплаты.

    Автообновление раз в 5 с: подтверждение занимает 30–60 с, потому что
    вебхуки провайдера в проде не долетают и работает тик сверки раз в
    минуту. Человеку об этом говорим честно — иначе он решит, что деньги
    пропали, и заплатит второй раз.
    """
    if paid:
        return _render(
            title="Оплата получена",
            body='<p class="ok">Подписка продлена. Откройте VPN-клиент и нажмите 🔄.</p>'
            + _telegram_link(),
        )
    body = (
        "<p>Ждём подтверждения от банка, обычно меньше минуты.</p>"
        '<p class="muted">Страница обновится сама. Платить второй раз не нужно.</p>'
    )
    # refresh ведёт на СВОЙ URL (?paid=<id>), иначе через 5 секунд человек
    # видел бы стартовый экран и решил, что оплата потерялась.
    return _render(
        title="Проверяем оплату",
        body=body,
        refresh_sec=5,
        refresh_url=f"?fix=1&paid={invoice_id}",
    )


# ── Действия ────────────────────────────────────────────────────────────


def _start_again(found, token: str) -> HTMLResponse:
    """Стартовый экран для POST, которому нечего выполнять (битый или чужой
    репорт). Молча, без «не найдено»: разница в ответе была бы оракулом для
    перебора id репортов по утёкшему токену."""
    # Ленивый импорт: api_extensions импортирует этот модуль.
    from ..api_extensions import _device_label

    return render_start(
        found.sub, token,
        device_name=_device_label(found),
        repairable=not found.is_legacy,
    )


def do_repair(db: Session, found, token: str) -> HTMLResponse:
    """POST-починка: шаг лестницы через общее ядро."""
    from .client_control import COMPLAINT_DEDUP_SEC

    sub = found.sub
    user = db.get(models.User, sub.user_id)
    if user is None:
        return camo_response()
    if not self_repair.user_may_repair(user):
        # Ядро ответило бы no_subscription («проверьте клиент») — для
        # забаненного это ложь: чинить ему нельзя вовсе, как и на GET.
        return render_inactive(sub, token, banned=True)
    device = found.serve_device or found.token_device
    if device is None:
        # Legacy-подписочный токен: устройства за ним нет, чинить нечего.
        return render_outcome(
            self_repair.RepairOutcome(action="no_subscription"), sub, token
        )

    # Окно повторов и суточный потолок не передаём: единая политика ядра
    # (SELF_REPAIR_* с fallback на SUB_FIX_*), одна на все каналы.
    outcome = self_repair.handle_broken_device(
        db,
        device.id,
        user=user,
        dedup_sec=COMPLAINT_DEDUP_SEC,
        source="sub_page",
    )
    logger.info(
        "sub-fix page: repair sub=%s device=%s -> %s",
        sub.id, device.id, outcome.action,
    )
    if outcome.action == "no_subscription":
        # Подписка могла истечь/замёрзнуть между GET и POST — тогда честные
        # экраны продления/паузы, как на GET, а не «проверьте клиент».
        if sub.status == models.SubscriptionStatus.expired or (
            sub.expires_at and sub.expires_at < utcnow()
        ):
            return render_expired(sub, token)
        if sub.status != models.SubscriptionStatus.active:
            return render_inactive(sub, token)
    return render_outcome(outcome, sub, token)


def render_feedback_again(db: Session, found, token: str, report_id: str) -> HTMLResponse:
    """Протухший nonce на форме обратной связи: перерисовать тот же экран со
    свежим nonce, ничего не записывая. Стартовый экран здесь сбивал бы с
    толку («я же только что нажимал ✅»)."""
    try:
        rid = int(report_id)
    except (TypeError, ValueError):
        return _start_again(found, token)
    report = db.get(models.OperatorNodeReport, rid)
    if report is None or report.subscription_id != found.sub.id:
        return _start_again(found, token)
    return render_feedback_prompt(token, report.id)


def do_feedback(
    db: Session,
    found,
    token: str,
    *,
    report_id: str,
    operator: str | None,
    still: bool,
    ok: bool,
) -> HTMLResponse:
    """POST-обратная связь по сделанной починке: оператор, «помогло / нет».

    Зеркало бот-эндпоинтов report-operator / report-ok / report-still-broken,
    но без admin-токена: право даёт сам sub_token, поэтому репорт обязан
    принадлежать ЭТОЙ подписке — иначе утёкшая ссылка позволяла бы
    переписывать чужие репорты перебором id. Порядок шагов: сначала
    оператор (он может прийти вместе с ответом), потом исход.
    """
    sub = found.sub
    try:
        rid = int(report_id)
    except (TypeError, ValueError):
        return _start_again(found, token)
    report = db.get(models.OperatorNodeReport, rid)
    if report is None or report.subscription_id != sub.id:
        return _start_again(found, token)

    if operator and operator != "skip":
        # Мусор вне списка = «не указан»: поле идёт в крауд-матрицу
        # node×operator, и произвольная строка портила бы статистику.
        report.operator = operator if operator in self_repair.OPERATORS else "unknown"
        db.commit()
    if still:
        report.outcome = "fail"
        report.resolved_at = utcnow()
        from ..services.repair_alerts import alert_repair_not_fixed

        alert_repair_not_fixed(db, report, reason="fail")
        db.commit()
        logger.info("sub-fix page: report %s still broken (sub=%s)", report.id, sub.id)
        return render_still_broken(token)
    if ok:
        # Как в боте: закрытый репорт (ok/fail) не переписываем — «ok» после
        # «fail» уже ушёл в поддержку, а watcher мог закрыть его сам.
        if report.outcome not in ("ok", "fail"):
            report.outcome = "ok"
            report.resolved_at = utcnow()
            db.commit()
        return render_all_good()
    return render_feedback_prompt(token, report.id)


def do_pay(
    db: Session, found, token: str, *, method: str = "card"
) -> HTMLResponse | RedirectResponse:
    """POST-продление: счёт у провайдера и редирект на оплату.

    Всё денежное — в общем хелпере (services/payments/checkout.py): сумма со
    слотами, конвертация валют, реюз pay_url при повторном тапе. ``method`` —
    ``card`` (``?pay=1``) или ``sbp`` (``?pay=sbp``); счёт на продление один,
    Payment-строка у каждого способа своя.
    """
    from ..services.balance import total_renewal_cost_kopecks
    from ..services.payments import ProviderError
    from ..services.payments.checkout import checkout_pending_invoice

    sub = found.sub
    if sub.plan is None:
        return camo_response()

    # Дедуп: незакрытый счёт на продление этой подписки переиспользуем, иначе
    # каждый тап плодил бы счета, а человек путался бы, какой оплачивать.
    invoice = (
        db.query(models.Invoice)
        .filter(
            models.Invoice.subscription_id == sub.id,
            models.Invoice.action == models.InvoiceAction.renewal,
            models.Invoice.status == models.InvoiceStatus.pending,
        )
        .order_by(models.Invoice.id.desc())
        .first()
    )
    if invoice is None:
        invoice = models.Invoice(
            user_id=sub.user_id,
            plan_id=sub.plan_id,
            subscription_id=sub.id,
            amount=total_renewal_cost_kopecks(sub) / 100,
            currency="RUB",
            action=models.InvoiceAction.renewal,
        )
        db.add(invoice)
        db.commit()
        db.refresh(invoice)

    try:
        result = checkout_pending_invoice(
            db, invoice,
            provider_name=pay_provider(method),
            # Куда провайдер вернёт человека после оплаты. Без этого экран
            # «проверяем оплату» был бы недостижим: страница ждёт
            # подтверждения (вебхуки lava не долетают, работает тик сверки),
            # а человек остался бы на чужой вкладке банка.
            return_url=_return_url(token, invoice.id),
        )
    except ProviderError:
        logger.exception("sub-fix page: checkout failed for sub %s", sub.id)
        return _render(
            title="Оплата недоступна",
            body="<p>Сейчас не получилось открыть оплату. Попробуйте через "
            "несколько минут.</p>" + _telegram_link(),
        )
    if not result.pay_url:
        return render_pay_pending(invoice.id, token, paid=False)
    # 303: после POST переход обязан стать GET, иначе «назад» в браузере
    # предложит переотправить форму (и выписать ещё один счёт).
    return RedirectResponse(url=result.pay_url, status_code=303)


def render_paid_state(db: Session, found, token: str, invoice_id: int) -> HTMLResponse:
    """``?paid=<id>`` — экран ожидания/подтверждения оплаты."""
    invoice = db.get(models.Invoice, invoice_id)
    # Чужой счёт не раскрываем: показываем нейтральное ожидание.
    if invoice is None or invoice.subscription_id != found.sub.id:
        return render_pay_pending(invoice_id, token, paid=False)
    return render_pay_pending(
        invoice.id, token, paid=invoice.status == models.InvoiceStatus.paid
    )
